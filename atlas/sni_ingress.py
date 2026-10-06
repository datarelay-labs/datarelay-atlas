"""Socket-activated TLS SNI passthrough for Atlas production ingress."""

from __future__ import annotations

import selectors
import socket
import struct
import threading

_BACKENDS = {
    "mcp.atlas.datarelay.run": ("127.0.0.1", 8443),
    "auth.atlas.datarelay.run": ("127.0.0.1", 9443),
}
_MAX_TLS_RECORD = 65540


class ClientHelloError(ValueError):
    pass


def backend_for_server_name(name: str) -> tuple[str, int]:
    try:
        return _BACKENDS[name.rstrip(".").lower()]
    except KeyError as exc:
        raise ClientHelloError("unapproved TLS server_name") from exc


def tls_client_hello_server_name(data: bytes) -> str:
    if len(data) < 5 or data[0] != 22:
        raise ClientHelloError("expected TLS handshake record")
    record_len = int.from_bytes(data[3:5], "big")
    if len(data) != 5 + record_len:
        raise ClientHelloError("incomplete TLS record")
    hello = memoryview(data)[5:]
    if len(hello) < 4 or hello[0] != 1:
        raise ClientHelloError("expected TLS ClientHello")
    body_len = int.from_bytes(hello[1:4], "big")
    body = hello[4 : 4 + body_len]
    if len(body) != body_len or len(body) < 35:
        raise ClientHelloError("incomplete TLS ClientHello")
    pos = 34
    pos += 1 + body[pos]
    if pos + 2 > len(body):
        raise ClientHelloError("invalid cipher suites")
    cipher_len = int.from_bytes(body[pos : pos + 2], "big")
    pos += 2 + cipher_len
    if pos >= len(body):
        raise ClientHelloError("invalid compression methods")
    pos += 1 + body[pos]
    if pos + 2 > len(body):
        raise ClientHelloError("missing extensions")
    extensions_len = int.from_bytes(body[pos : pos + 2], "big")
    pos += 2
    end = pos + extensions_len
    if end > len(body):
        raise ClientHelloError("incomplete extensions")
    while pos + 4 <= end:
        ext_type, ext_len = struct.unpack("!HH", body[pos : pos + 4])
        pos += 4
        ext = body[pos : pos + ext_len]
        pos += ext_len
        if ext_type != 0:
            continue
        if len(ext) < 5:
            raise ClientHelloError("invalid server_name extension")
        names_len = int.from_bytes(ext[0:2], "big")
        name_type = ext[2]
        name_len = int.from_bytes(ext[3:5], "big")
        if names_len + 2 > len(ext) or name_type != 0 or 5 + name_len > len(ext):
            raise ClientHelloError("invalid server_name entry")
        try:
            return bytes(ext[5 : 5 + name_len]).decode("ascii").lower()
        except UnicodeDecodeError as exc:
            raise ClientHelloError("non-ASCII server_name") from exc
    raise ClientHelloError("TLS ClientHello has no SNI")


def read_client_hello(client: socket.socket) -> bytes:
    data = bytearray()
    while len(data) < 5:
        chunk = client.recv(5 - len(data))
        if not chunk:
            raise ClientHelloError("connection closed before TLS header")
        data.extend(chunk)
    total = 5 + int.from_bytes(data[3:5], "big")
    if total > _MAX_TLS_RECORD:
        raise ClientHelloError("TLS record exceeds ingress limit")
    while len(data) < total:
        chunk = client.recv(total - len(data))
        if not chunk:
            raise ClientHelloError("connection closed during ClientHello")
        data.extend(chunk)
    tls_client_hello_server_name(bytes(data))
    return bytes(data)


def relay(left: socket.socket, right: socket.socket) -> None:
    selector = selectors.DefaultSelector()
    try:
        selector.register(left, selectors.EVENT_READ, right)
        selector.register(right, selectors.EVENT_READ, left)
        while True:
            for key, _ in selector.select():
                chunk = key.fileobj.recv(65536)
                if not chunk:
                    return
                key.data.sendall(chunk)
    finally:
        selector.close()


def handle_client(client: socket.socket, *, create_connection=None) -> None:
    hello = read_client_hello(client)
    name = tls_client_hello_server_name(hello)
    connect = socket.create_connection if create_connection is None else create_connection
    backend = connect(backend_for_server_name(name))
    try:
        backend.sendall(hello)
        relay(client, backend)
    finally:
        backend.close()


def _handle_and_close(client: socket.socket) -> None:
    try:
        handle_client(client)
    except (ClientHelloError, OSError):
        pass
    finally:
        client.close()


def serve(listener: socket.socket) -> None:
    while True:
        client, _ = listener.accept()
        threading.Thread(
            target=_handle_and_close,
            args=(client,),
            daemon=True,
            name="atlas-sni-ingress",
        ).start()


def main() -> int:
    listener = socket.socket(fileno=3)
    serve(listener)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
