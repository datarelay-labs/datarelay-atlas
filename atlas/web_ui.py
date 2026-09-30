"""Read-only server-rendered Human UI for Atlas project knowledge."""
from __future__ import annotations
from dataclasses import dataclass
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote
from wsgiref.simple_server import make_server

from atlas.provenance import ValidationError
from atlas.service import AtlasService

_MAX_QUERY = 256

@dataclass(frozen=True)
class UiResponse:
    status: str
    body: bytes
    content_type: str = "text/html; charset=utf-8"

def _page(title: str, body: str) -> bytes:
    safe_title = escape(title)
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{safe_title} · DataRelay Atlas</title><style>
:root{{font-family:Inter,ui-sans-serif,system-ui,sans-serif;color:#171717;background:#fafafa}}body{{margin:0}}header{{padding:18px 28px;border-bottom:1px solid #e5e5e5;background:#fff}}main{{max-width:1120px;margin:auto;padding:28px}}a{{color:#1457d9;text-decoration:none}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}}.card{{background:#fff;border:1px solid #e5e5e5;border-radius:12px;padding:18px}}.muted{{color:#666}}.pill{{display:inline-block;padding:3px 8px;border-radius:999px;background:#f0f0f0;font-size:12px}}input{{width:min(620px,70%);padding:10px;border:1px solid #bbb;border-radius:8px}}button{{padding:10px 14px;border:0;border-radius:8px;background:#171717;color:white}}code{{font-size:12px}}h1{{margin-top:0}}dl{{display:grid;grid-template-columns:max-content 1fr;gap:7px 16px}}dt{{color:#666}}dd{{margin:0;overflow-wrap:anywhere}}.hit{{margin:14px 0}}.snippet{{white-space:pre-wrap;overflow-wrap:anywhere}}
</style></head><body><header><strong>DataRelay Atlas</strong> <span class="muted">Engineering Knowledge &amp; Lifecycle</span></header><main>{body}</main></body></html>"""
    return html.encode("utf-8")

def _error(status: str, title: str, message: str) -> UiResponse:
    body = f'<h1>{escape(title)}</h1><p>{escape(message)}</p><p><a href="/">Back to projects</a></p>'
    return UiResponse(status, _page(title, body))

def render_projects(service: AtlasService) -> UiResponse:
    cards = []
    for project in service.list_projects():
        pid = quote(project.project_id, safe="")
        state = "enabled" if project.enabled else "disabled"
        cards.append(
            f'<article class="card"><span class="pill">{state}</span>'
            f'<h2><a href="/projects/{pid}">{escape(project.display_name)}</a></h2>'
            f'<p><code>{escape(project.repository)}</code></p>'
            f'<p class="muted">{len(project.sources)} configured sources</p></article>'
        )
    listing = "".join(cards) if cards else '<div class="card"><p>No projects registered.</p></div>'
    body = '<h1>Projects</h1><p class="muted">Read-only view of Atlas registered engineering knowledge.</p><section class="grid">' + listing + "</section>"
    return UiResponse("200 OK", _page("Projects", body))

def render_project(service: AtlasService, project_id: str, query: str = "") -> UiResponse:
    try:
        project = service.registry.get(project_id)
        records = service.projection_records(project_id)
    except (ValidationError, ValueError):
        return _error("404 Not Found", "Project not found", "The requested project is not registered.")
    good = sum(1 for r in records if r.get("sync_state") in {"success", "unchanged", "ok"})
    pid = quote(project.project_id, safe="")
    body = (
        f'<p><a href="/">← Projects</a></p><h1>{escape(project.display_name)}</h1>'
        '<div class="grid"><section class="card"><h2>Project</h2><dl>'
        f'<dt>ID</dt><dd><code>{escape(project.project_id)}</code></dd>'
        f'<dt>Repository</dt><dd><code>{escape(project.repository)}</code></dd>'
        f'<dt>Default ref</dt><dd><code>{escape(project.default_ref)}</code></dd>'
        f'<dt>State</dt><dd>{"enabled" if project.enabled else "disabled"}</dd></dl></section>'
        '<section class="card"><h2>Knowledge</h2><dl>'
        f'<dt>Configured sources</dt><dd>{len(project.sources)}</dd>'
        f'<dt>Successful projections</dt><dd>{good}</dd>'
        '<dt>Lifecycle</dt><dd><span class="pill">UNKNOWN</span> <span class="muted">not yet normalized in this UI slice</span></dd>'
        '</dl></section></div>'
        f'<section class="card" style="margin-top:16px"><h2>Search knowledge</h2><form method="get" action="/projects/{pid}">'
        f'<input name="q" maxlength="{_MAX_QUERY}" value="{escape(query, quote=True)}" placeholder="Search this project"><button type="submit">Search</button></form>'
    )
    if query:
        try:
            hits = service.search(project_id, query, limit=8)
        except ValidationError as exc:
            return _error("400 Bad Request", "Search unavailable", str(exc))
        body += f'<p class="muted">{len(hits)} attributable result(s)</p>'
        for hit in hits:
            prov = hit.provenance
            source_kind = "canonical source reference" if prov.get("canonical") is False and prov.get("derived") is True else "UNKNOWN"
            body += (
                f'<article class="hit"><h3>{escape(hit.title or hit.path)}</h3>'
                f'<p class="snippet">{escape(hit.content or "")}</p><dl>'
                f'<dt>Source</dt><dd><code>{escape(str(prov.get("repository", "")))} · {escape(str(prov.get("source_path", "")))}</code></dd>'
                f'<dt>Revision</dt><dd><code>{escape(str(prov.get("source_revision", "")))}</code></dd>'
                f'<dt>Source relation</dt><dd>{escape(source_kind)}</dd>'
                f'<dt>Projection</dt><dd><span class="pill">DERIVED</span> <code>{escape(hit.identity)}</code></dd></dl></article>'
            )
        if not hits:
            body += "<p>No attributable matches.</p>"
    body += "</section>"
    return UiResponse("200 OK", _page(project.display_name, body))

def create_app(data_root: Path):
    service = AtlasService(data_root)
    def app(environ, start_response):
        if environ.get("REQUEST_METHOD") != "GET":
            response = _error("405 Method Not Allowed", "Read-only UI", "Only GET requests are supported.")
        else:
            path = environ.get("PATH_INFO") or "/"
            if path == "/":
                response = render_projects(service)
            elif path.startswith("/projects/") and "/" not in path[len("/projects/"):]:
                project_id = unquote(path[len("/projects/"):])
                query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True).get("q", [""])[0]
                response = _error("400 Bad Request", "Invalid search", "Search query is too long.") if len(query) > _MAX_QUERY else render_project(service, project_id, query.strip())
            else:
                response = _error("404 Not Found", "Not found", "The requested UI route does not exist.")
        headers = [
            ("Content-Type", response.content_type), ("Content-Length", str(len(response.body))),
            ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"),
            ("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"),
        ]
        start_response(response.status, headers)
        return [response.body]
    return app

def serve_ui(data_root: Path, host: str = "127.0.0.1", port: int = 8788) -> None:
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValidationError("Web UI first slice is loopback-only")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValidationError("invalid Web UI port")
    with make_server(host, port, create_app(data_root)) as server:
        server.serve_forever()
