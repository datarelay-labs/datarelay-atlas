import socket
import ssl
import threading
import unittest
from unittest.mock import patch

from atlas.sni_ingress import (
    ClientHelloError,
    backend_for_server_name,
    handle_client,
    read_client_hello,
    tls_client_hello_server_name,
)


def client_hello_for(server_name: str) -> bytes:
    incoming = ssl.MemoryBIO()
    outgoing = ssl.MemoryBIO()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    tls = context.wrap_bio(incoming, outgoing, server_hostname=server_name)
    with unittest.TestCase().assertRaises(ssl.SSLWantReadError):
        tls.do_handshake()
    return outgoing.read()


class SniIngressTests(unittest.TestCase):
    def test_real_client_hello_routes_only_approved_names(self):
        mcp = client_hello_for("mcp.atlas.datarelay.run")
        auth = client_hello_for("auth.atlas.datarelay.run")
        self.assertEqual(tls_client_hello_server_name(mcp), "mcp.atlas.datarelay.run")
        self.assertEqual(tls_client_hello_server_name(auth), "auth.atlas.datarelay.run")
        self.assertEqual(backend_for_server_name("mcp.atlas.datarelay.run"), ("127.0.0.1", 8443))
        self.assertEqual(backend_for_server_name("auth.atlas.datarelay.run"), ("127.0.0.1", 9443))
        with self.assertRaises(ClientHelloError):
            backend_for_server_name("unknown.example")

    def test_fragmented_read_does_not_consume_next_record(self):
        hello = client_hello_for("mcp.atlas.datarelay.run")
        reader, writer = socket.socketpair()
        self.addCleanup(reader.close)
        self.addCleanup(writer.close)
        writer.sendall(hello[:3])
        writer.sendall(hello[3:] + b"NEXT")
        self.assertEqual(read_client_hello(reader), hello)
        self.assertEqual(reader.recv(4), b"NEXT")

    def test_serve_dispatches_connections_without_serializing_clients(self):
        from atlas.sni_ingress import serve

        class Listener:
            def __init__(self):
                self.calls = 0

            def accept(self):
                self.calls += 1
                if self.calls <= 2:
                    left, right = socket.socketpair()
                    right.close()
                    return left, None
                raise RuntimeError("stop")

        started = []

        class ImmediateThread:
            def __init__(self, *, target, args, **kwargs):
                self.target = target
                self.args = args
                started.append(self)

            def start(self):
                pass

        with patch("atlas.sni_ingress.threading.Thread", ImmediateThread):
            with self.assertRaises(RuntimeError):
                serve(Listener())
        self.assertEqual(len(started), 2)
        for thread in started:
            thread.args[0].close()

    def test_handle_client_forwards_client_hello_to_selected_backend(self):
        hello = client_hello_for("auth.atlas.datarelay.run")
        client, peer = socket.socketpair()
        backend, backend_peer = socket.socketpair()
        for sock in (client, peer, backend, backend_peer):
            self.addCleanup(sock.close)
        peer.sendall(hello)
        peer.shutdown(socket.SHUT_WR)
        calls = []

        def connect(address):
            calls.append(address)
            return backend

        handle_client(client, create_connection=connect)
        self.assertEqual(calls, [("127.0.0.1", 9443)])
        self.assertEqual(backend_peer.recv(len(hello)), hello)


if __name__ == "__main__":
    unittest.main()
