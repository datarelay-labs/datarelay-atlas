"""Read-only server-rendered Human UI for Atlas project knowledge."""
from __future__ import annotations
from dataclasses import dataclass
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote
from wsgiref.simple_server import make_server

from atlas.cursor_usage import load_github_reconciliation_snapshot
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

def _lifecycle_for_repository(service: AtlasService, repository: str) -> tuple[str, str]:
    snapshot = service.data_root / "github-lifecycle.json"
    if not snapshot.is_file():
        return "UNKNOWN", "no trusted local lifecycle evidence"
    try:
        _, observations, metadata = load_github_reconciliation_snapshot(snapshot)
    except ValidationError:
        return "UNAVAILABLE", "local lifecycle evidence failed validation"
    matching = [item for item in observations if item.get("repository") == repository]
    if not matching:
        return "UNKNOWN", f"snapshot {metadata['observed_at']} has no project observation"
    canonical = [item for item in matching if item.get("canonical_fact") is True]
    if not canonical:
        return "UNAVAILABLE", f"snapshot {metadata['observed_at']} has no canonical lifecycle fact"
    active = [item for item in canonical if item.get("packet_status") == "ACTIVE"]
    if active:
        item = sorted(active, key=lambda value: int(value["issue_number"]))[0]
        pr = f"PR #{item['pr_number']} {item['pr_state']}" if item.get("pr_number") else "no PR"
        return "OBSERVED", f"AI Work #{item['issue_number']} ACTIVE · {pr} · snapshot {metadata['observed_at']}"
    return "OBSERVED", f"{len(canonical)} canonical packet fact(s) · snapshot {metadata['observed_at']}"


def _source_relation(provenance: dict[str, object]) -> str:
    if provenance.get("source_class") == "personal":
        return "personal reference / non-authoritative"
    if provenance.get("source_class", "engineering") == "engineering" and provenance.get("canonical") is False and provenance.get("derived") is True:
        return "canonical engineering source reference"
    return "UNKNOWN"


def render_projects(service: AtlasService) -> UiResponse:
    projects = service.list_projects()
    cards = []
    for project in projects:
        pid = quote(project.project_id, safe="")
        state = "enabled" if project.enabled else "disabled"
        cards.append(
            f'<article class="card"><span class="pill">{state}</span>'
            f'<h2><a href="/projects/{pid}">{escape(project.display_name)}</a></h2>'
            f'<p><code>{escape(project.repository)}</code></p>'
            f'<p class="muted">{len(project.sources)} configured sources</p></article>'
        )
    listing = "".join(cards) if cards else '<div class="card"><p>No projects registered.</p></div>'
    enabled = sum(1 for project in projects if project.enabled)
    sources = sum(len(project.sources) for project in projects)
    body = f'<h1>Atlas Overview</h1><p class="muted">Read-only engineering knowledge and lifecycle navigation.</p><div class="grid"><section class="card"><h2>{len(projects)}</h2><p>Registered projects</p></section><section class="card"><h2>{enabled}</h2><p>Enabled projects</p></section><section class="card"><h2>{sources}</h2><p>Configured sources</p></section></div><p><a href="/search">Search across projects →</a></p><h2>Projects</h2><section class="grid">' + listing + "</section>"
    return UiResponse("200 OK", _page("Projects", body))

def render_cross_project_search(service: AtlasService, query: str) -> UiResponse:
    body = '<p><a href="/">← Projects</a></p><h1>Cross-project search</h1>'
    body += f'<form method="get" action="/search"><input name="q" maxlength="{_MAX_QUERY}" value="{escape(query, quote=True)}" placeholder="Search all registered projects"><button type="submit">Search</button></form>'
    if query:
        total = 0
        for project in service.list_projects():
            if not project.enabled:
                continue
            try:
                hits = service.search(project.project_id, query, limit=5)
            except ValidationError:
                body += f'<section class="card hit"><h2>{escape(project.display_name)}</h2><span class="pill">UNAVAILABLE</span></section>'
                continue
            if not hits:
                continue
            total += len(hits)
            body += f'<section class="card hit"><h2><a href="/projects/{quote(project.project_id, safe="")}">{escape(project.display_name)}</a></h2>'
            for hit in hits:
                relation = _source_relation(hit.provenance)
                body += (
                    f'<article><h3>{escape(hit.title or hit.path)}</h3><p class="snippet">{escape(hit.content or "")}</p>'
                    f'<p><span class="pill">DERIVED</span> <span class="pill">{escape(relation)}</span></p>'
                    f'<p class="muted"><code>{escape(str(hit.provenance.get("repository", "")))} · {escape(str(hit.provenance.get("ref", "")))} · {escape(str(hit.provenance.get("source_path", "")))} · {escape(str(hit.provenance.get("source_revision", "")))}</code></p>'
                    f'<p class="muted">Projection <code>{escape(hit.identity)}</code></p></article>'
                )
            body += "</section>"
        body += f'<p class="muted">{total} attributable result(s) across enabled projects.</p>'
    return UiResponse("200 OK", _page("Cross-project search", body))


def _adoption_projection_state(project, records: list[dict]) -> str:
    metadata_source_ids = {
        source_id for source_id, source in project.sources.items()
        if source.provider == "github" and source.enabled and source.source_path == project.engineering_metadata_path
    }
    if not metadata_source_ids:
        return "UNKNOWN"
    for record in records:
        if record.get("source_id") in metadata_source_ids and record.get("sync_state") in {"success", "unchanged", "ok"}:
            return "OBSERVED"
    return "UNKNOWN"


def render_project(service: AtlasService, project_id: str, query: str = "") -> UiResponse:
    try:
        project = service.registry.get(project_id)
    except ValidationError as exc:
        if str(exc).startswith("unknown project_id:"):
            return _error("404 Not Found", "Project not found", "The requested project is not registered.")
        return _error("500 Internal Server Error", "Atlas state unavailable", "Project registry state could not be read safely.")
    try:
        records = service.projection_records(project_id)
    except (ValidationError, ValueError):
        return _error("500 Internal Server Error", "Atlas state unavailable", "Projection state could not be read safely.")
    good = sum(1 for r in records if r.get("sync_state") in {"success", "unchanged", "ok"})
    errors = sum(1 for r in records if r.get("sync_state") == "error")
    disabled = sum(1 for r in records if r.get("sync_state") == "disabled")
    adoption_state = _adoption_projection_state(project, records)
    github_state, github_detail = _lifecycle_for_repository(service, project.repository)
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
        f'<dt>Projection errors</dt><dd>{errors}</dd>'
        f'<dt>Disabled projections</dt><dd>{disabled}</dd></dl></section>'
        '<section class="card"><h2>Lifecycle evidence</h2><dl>'
        f'<dt>Engineering System adoption</dt><dd><span class="pill">{adoption_state}</span></dd>'
        f'<dt>GitHub Work / PR</dt><dd><span class="pill">{escape(github_state)}</span> <span class="muted">{escape(github_detail)}</span></dd>'
        '<dt>CI</dt><dd><span class="pill">UNKNOWN</span> <span class="muted">lifecycle snapshot does not authorize CI conclusions</span></dd>'
        '<dt>Tests</dt><dd><span class="pill">UNKNOWN</span> <span class="muted">no exact-candidate test evidence loaded</span></dd>'
        '<dt>Release</dt><dd><span class="pill">UNKNOWN</span> <span class="muted">no exact-candidate release evidence</span></dd>'
        '</dl></section></div>'
    )
    body += '<section class="card" style="margin-top:16px"><h2>Sources</h2><dl>'
    record_by_source = {str(r.get("source_id", "")): r for r in records}
    for source_id, source in sorted(project.sources.items()):
        record = record_by_source.get(source_id, {})
        sync_state = str(record.get("sync_state") or "UNKNOWN").upper()
        source_class = "PERSONAL / REFERENCE" if source.source_class == "personal" else "ENGINEERING"
        body += f'<dt>{escape(source.title or source_id)}</dt><dd><span class="pill">{escape(sync_state)}</span> <span class="pill">{source_class}</span> <code>{escape(source.source_path)}</code></dd>'
    if not project.sources:
        body += '<dt>Sources</dt><dd><span class="pill">NONE</span></dd>'
    body += '</dl></section>'
    body += f'<section class="card" style="margin-top:16px"><h2>Search knowledge</h2><form method="get" action="/projects/{pid}"><input name="q" maxlength="{_MAX_QUERY}" value="{escape(query, quote=True)}" placeholder="Search this project"><button type="submit">Search</button></form>'
    if query:
        try:
            hits = service.search(project_id, query, limit=8)
        except ValidationError as exc:
            return _error("400 Bad Request", "Search unavailable", str(exc))
        body += f'<p class="muted">{len(hits)} attributable result(s)</p>'
        for hit in hits:
            prov = hit.provenance
            source_kind = _source_relation(prov)
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

def _trusted_request_host(environ: dict) -> bool:
    authority = str(environ.get("HTTP_HOST") or "").strip().lower()
    if not authority:
        return False
    hostname = authority.rsplit(":", 1)[0] if ":" in authority else authority
    return hostname in {"127.0.0.1", "localhost"}


def create_app(data_root: Path):
    service = AtlasService(data_root)
    def app(environ, start_response):
        if not _trusted_request_host(environ):
            response = _error("400 Bad Request", "Untrusted request", "The Web UI accepts loopback Host authorities only.")
        elif environ.get("REQUEST_METHOD") != "GET":
            response = _error("405 Method Not Allowed", "Read-only UI", "Only GET requests are supported.")
        else:
            path = environ.get("PATH_INFO") or "/"
            if path == "/":
                response = render_projects(service)
            elif path == "/search":
                query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True).get("q", [""])[0]
                response = _error("400 Bad Request", "Invalid search", "Search query is too long.") if len(query) > _MAX_QUERY else render_cross_project_search(service, query.strip())
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
    if host not in {"127.0.0.1", "localhost"}:
        raise ValidationError("Web UI first slice is loopback-only")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValidationError("invalid Web UI port")
    with make_server(host, port, create_app(data_root)) as server:
        server.serve_forever()
