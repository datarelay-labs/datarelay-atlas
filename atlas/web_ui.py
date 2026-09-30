"""Read-only server-rendered Human UI for Atlas project knowledge."""
from __future__ import annotations
from dataclasses import dataclass
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit
from wsgiref.simple_server import make_server

from atlas.lifecycle_intelligence import lifecycle_view
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
:root{{font-family:Inter,ui-sans-serif,system-ui,sans-serif;color:#171717;background:#fafafa}}body{{margin:0}}header{{padding:18px 28px;border-bottom:1px solid #e5e5e5;background:#fff}}main{{max-width:1120px;margin:auto;padding:28px}}a{{color:#1457d9;text-decoration:none}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}}.card{{background:#fff;border:1px solid #e5e5e5;border-radius:12px;padding:18px}}.muted{{color:#666}}.pill{{display:inline-block;padding:3px 8px;border-radius:999px;background:#f0f0f0;font-size:12px}}input{{width:min(620px,70%);padding:10px;border:1px solid #bbb;border-radius:8px}}button{{padding:10px 14px;border:0;border-radius:8px;background:#171717;color:white}}code{{font-size:12px}}h1{{margin-top:0}}dl{{display:grid;grid-template-columns:max-content 1fr;gap:7px 16px}}dt{{color:#666}}dd{{margin:0;overflow-wrap:anywhere}}table{{width:100%;border-collapse:collapse}}th,td{{padding:9px 10px;border-bottom:1px solid #eee;text-align:left;vertical-align:top}}th{{font-size:12px;color:#666}}.hit{{margin:14px 0}}.snippet{{white-space:pre-wrap;overflow-wrap:anywhere}}
</style></head><body><header><strong>DataRelay Atlas</strong> <span class="muted">Engineering Knowledge &amp; Lifecycle</span></header><main>{body}</main></body></html>"""
    return html.encode("utf-8")

def _error(status: str, title: str, message: str) -> UiResponse:
    body = f'<h1>{escape(title)}</h1><p>{escape(message)}</p><p><a href="/">Back to projects</a></p>'
    return UiResponse(status, _page(title, body))

def _source_relation(provenance: dict[str, object]) -> str:
    if provenance.get("source_class") == "personal":
        return "personal reference / non-authoritative"
    if provenance.get("source_class", "engineering") == "engineering" and provenance.get("canonical") is False and provenance.get("derived") is True:
        return "canonical engineering source reference"
    return "UNKNOWN"


def _evidence_ref_html(value: str | None) -> str:
    if not value:
        return ""
    parsed = urlsplit(value)
    safe = escape(value)
    if parsed.scheme == "https" and parsed.netloc == "github.com":
        return f' · <a href="{escape(value, quote=True)}" rel="noreferrer">evidence ↗</a>'
    return f' · evidence <code>{safe}</code>'


def _canonical_source_link(provenance: dict[str, object]) -> str:
    repository = str(provenance.get("repository") or "")
    revision = str(provenance.get("source_revision") or "")
    source_path = str(provenance.get("source_path") or "")
    if "/" not in repository or len(revision) != 40 or not source_path:
        return ""
    url = f"https://github.com/{repository}/blob/{revision}/{quote(source_path, safe='/')}"
    return f'<a href="{escape(url, quote=True)}" rel="noreferrer">canonical source ↗</a>'


def render_projects(service: AtlasService) -> UiResponse:
    projects = service.list_projects()
    cards = []
    for project in projects:
        pid = quote(project.project_id, safe="")
        state = "enabled" if project.enabled else "disabled"
        lifecycle = lifecycle_view(service.data_root, project.repository)
        cards.append(
            f'<article class="card"><span class="pill">{state}</span> <span class="pill">{escape(lifecycle.work.state)}</span>'
            f'<h2><a href="/projects/{pid}">{escape(project.display_name)}</a></h2>'
            f'<p><code>{escape(project.repository)}</code></p>'
            f'<p class="muted">{len(project.sources)} configured sources</p>'
            f'<p class="muted">{escape(lifecycle.work.detail)}</p></article>'
        )
    listing = "".join(cards) if cards else '<div class="card"><p>No projects registered.</p></div>'
    enabled = sum(1 for project in projects if project.enabled)
    sources = sum(len(project.sources) for project in projects)
    body = f'<h1>Atlas Overview</h1><p class="muted">Read-only engineering knowledge and lifecycle navigation.</p><div class="grid"><section class="card"><h2>{len(projects)}</h2><p>Registered projects</p></section><section class="card"><h2>{enabled}</h2><p>Enabled projects</p></section><section class="card"><h2>{sources}</h2><p>Configured sources</p></section></div><p><a href="/search">Search across projects →</a> · <a href="/intelligence">Derived intelligence →</a></p><h2>Projects</h2><section class="grid">' + listing + "</section>"
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


def _engineering_system_observation(project, records: list[dict]) -> dict[str, str]:
    metadata_sources = {
        source_id: source for source_id, source in project.sources.items()
        if source.provider == "github" and source.enabled and source.source_path == project.engineering_metadata_path
    }
    if not metadata_sources:
        return {
            "state": "UNKNOWN",
            "detail": "engineering metadata source is not configured",
            "source_id": "",
            "source_revision": "",
            "sync_state": "UNKNOWN",
        }
    matching = [record for record in records if record.get("source_id") in metadata_sources]
    if not matching:
        return {
            "state": "UNKNOWN",
            "detail": "engineering metadata source is configured but has no projection evidence",
            "source_id": sorted(metadata_sources)[0],
            "source_revision": "",
            "sync_state": "UNKNOWN",
        }
    record = sorted(matching, key=lambda item: str(item.get("source_id", "")))[0]
    sync_state = str(record.get("sync_state") or "UNKNOWN").upper()
    revision = str(record.get("source_revision") or "")
    state = "OBSERVED" if sync_state in {"SUCCESS", "UNCHANGED", "OK"} and revision else ("UNAVAILABLE" if sync_state == "ERROR" else "UNKNOWN")
    detail = f"{project.engineering_metadata_path} · projection {sync_state.lower()}"
    if revision:
        detail += f" · source revision {revision}"
    return {
        "state": state,
        "detail": detail,
        "source_id": str(record.get("source_id") or ""),
        "source_revision": revision,
        "sync_state": sync_state,
    }


def _adoption_projection_state(project, records: list[dict]) -> str:
    return _engineering_system_observation(project, records)["state"]


def render_lifecycle(service: AtlasService, project_id: str) -> UiResponse:
    try:
        project = service.registry.get(project_id)
    except ValidationError as exc:
        if str(exc).startswith("unknown project_id:"):
            return _error("404 Not Found", "Project not found", "The requested project is not registered.")
        return _error("500 Internal Server Error", "Atlas state unavailable", "Project registry state could not be read safely.")
    lifecycle = lifecycle_view(service.data_root, project.repository)
    engineering = service.engineering_system_observation(project_id)
    rows = []
    for label, value in (("Work / PR", lifecycle.work), ("CI", lifecycle.ci), ("Tests", lifecycle.tests), ("Release", lifecycle.release), ("Surface Reconciliation", lifecycle.surface_reconciliation), ("Full User E2E", lifecycle.full_user_e2e)):
        head = f' <code>{escape(value.candidate_head)}</code>' if value.candidate_head else ""
        evidence = _evidence_ref_html(value.evidence_ref)
        rows.append(f'<dt>{label}</dt><dd><span class="pill">{escape(value.state)}</span>{head}<br><span class="muted">{escape(value.detail)}</span>{evidence}</dd>')
    packet_rows = []
    for packet in lifecycle.work_packets:
        issue_url = f"https://github.com/{project.repository}/issues/{packet.issue_number}"
        pr = "no PR"
        if packet.pr_number is not None:
            pr_url = f"https://github.com/{project.repository}/pull/{packet.pr_number}"
            pr_head = f' · <code>{escape(packet.pr_head)}</code>' if packet.pr_head else ""
            pr = f'<a href="{escape(pr_url, quote=True)}" rel="noreferrer">PR #{packet.pr_number}</a> {escape(packet.pr_state)}{pr_head}'
        reasons = f' · <span class="muted">{escape(", ".join(packet.reasons))}</span>' if packet.reasons else ""
        authority = "CANONICAL" if packet.canonical else "NONCANONICAL"
        packet_rows.append(
            f'<tr><td><a href="{escape(issue_url, quote=True)}" rel="noreferrer">#{packet.issue_number}</a></td>'
            f'<td><span class="pill">{escape(packet.packet_status)}</span> <span class="pill">{authority}</span>{reasons}</td>'
            f'<td><code>{escape(packet.branch)}</code></td><td><code>{escape(packet.head)}</code></td><td>{pr}</td></tr>'
        )
    packets = "".join(packet_rows) if packet_rows else '<tr><td colspan="5" class="muted">No Work Packet observation loaded.</td></tr>'
    engineering_rows = (
        f'<dt>State</dt><dd><span class="pill">{escape(str(engineering["state"]))}</span> <span class="muted">{escape(str(engineering["detail"]))}</span></dd>'
        f'<dt>Version</dt><dd><code>{escape(str(engineering["version"] or "UNKNOWN"))}</code></dd>'
        f'<dt>Baseline</dt><dd><code>{escape(str(engineering["baseline"] or "UNKNOWN"))}</code></dd>'
        f'<dt>Mode</dt><dd><code>{escape(str(engineering["mode"] or "UNKNOWN"))}</code></dd>'
        f'<dt>CI mode</dt><dd><code>{escape(str(engineering["ci_mode"] or "UNKNOWN"))}</code></dd>'
        f'<dt>Source revision</dt><dd><code>{escape(str(engineering["source_revision"] or "UNKNOWN"))}</code></dd>'
    )
    body = (
        f'<p><a href="/projects/{quote(project.project_id,safe="")}">← {escape(project.display_name)}</a></p>'
        '<h1>Lifecycle evidence</h1><section class="card"><dl>'
        + "".join(rows)
        + '</dl></section><section class="card" style="margin-top:16px"><h2>Engineering System compliance</h2><dl>'
        + engineering_rows
        + '</dl></section><section class="card" style="margin-top:16px"><h2>Work Packets</h2>'
        '<div style="overflow:auto"><table><thead><tr><th>Issue</th><th>Status</th><th>Branch</th><th>HEAD</th><th>PR</th></tr></thead>'
        f'<tbody>{packets}</tbody></table></div></section>'
        '<p class="muted">Read-only normalized evidence. Atlas does not infer one channel from another and does not become CI, GitHub, test, browser, or release authority.</p>'
    )
    return UiResponse("200 OK", _page(f"{project.display_name} lifecycle", body))


def render_intelligence_overview(service: AtlasService) -> UiResponse:
    try:
        intelligence = service.intelligence_overview()
    except ValidationError:
        return _error("500 Internal Server Error", "Derived intelligence unavailable", "Validated projection state could not be read safely.")

    project_cards = []
    for project in intelligence["projects"]:
        counts = project["counts"]
        pid = quote(project["project_id"], safe="")
        project_cards.append(
            f'<article class="card"><h2><a href="/projects/{pid}/intelligence">{escape(project["display_name"])}</a></h2>'
            f'<p><code>{escape(project["repository"])}</code></p><dl>'
            f'<dt>Concepts</dt><dd>{counts["concept_heading"]}</dd>'
            f'<dt>Cross-project links</dt><dd>{counts["cross_project_link"]}</dd>'
            f'<dt>ADR backlinks</dt><dd>{counts["decision_backlink"]}</dd>'
            f'<dt>Questions</dt><dd>{counts["unanswered_question"]}</dd>'
            f'<dt>Contradiction evidence</dt><dd>{counts["contradiction_evidence"]}</dd>'
            f'<dt>Knowledge gaps</dt><dd>{counts["knowledge_gap"]}</dd></dl></article>'
        )

    project_by_repository = {
        project["repository"]: project["project_id"]
        for project in intelligence["projects"]
    }
    cross_links = [
        item for item in intelligence["items"]
        if item["kind"] == "cross_project_link"
    ]
    link_rows = []
    for item in cross_links:
        prov = item["provenance"]
        target_project_id = project_by_repository.get(item["value"])
        target = f'<code>{escape(item["value"])}</code>'
        if target_project_id:
            target = f'<a href="/projects/{quote(target_project_id, safe="")}/intelligence"><code>{escape(item["value"])}</code></a>'
        source_link = _canonical_source_link(prov)
        source_path = f'<code>{escape(str(prov.get("source_path", "")))}</code>'
        if source_link:
            source_path += f' · {source_link}'
        link_rows.append(
            f'<tr><td><code>{escape(item["source_project_id"])}</code></td>'
            f'<td><code>{escape(str(prov.get("repository", "")))}</code></td>'
            f'<td>→ {target}</td>'
            f'<td>{source_path}</td></tr>'
        )
    links = "".join(link_rows) if link_rows else '<tr><td colspan="4" class="muted">No explicit cross-project repository links.</td></tr>'
    body = (
        '<p><a href="/">← Projects</a></p><h1>Derived intelligence</h1>'
        '<p class="muted"><span class="pill">DERIVED</span> Cross-project synthesis is rebuildable and never canonical authority.</p>'
        '<section class="grid">'
        f'<article class="card"><h2>{len(intelligence["entities"]["repositories"])}</h2><p>Repository entities</p></article>'
        f'<article class="card"><h2>{len(intelligence["entities"]["source_paths"])}</h2><p>Source-path entities</p></article>'
        f'<article class="card"><h2>{len(intelligence["entities"]["decisions"])}</h2><p>Decision entities</p></article>'
        '</section><h2>Projects</h2><section class="grid">' + "".join(project_cards) + '</section>'
        '<section class="card" style="margin-top:16px"><h2>Cross-project link graph</h2>'
        '<div style="overflow:auto"><table><thead><tr><th>Project</th><th>Source repository</th><th>Target repository</th><th>Source path</th></tr></thead>'
        f'<tbody>{links}</tbody></table></div></section>'
        '<section class="card" style="margin-top:16px"><h2>Contradictions</h2>'
        f'<span class="pill">{escape(str(intelligence["contradictions"]["state"]))}</span> '
        f'<span class="muted">{escape(str(intelligence["contradictions"]["detail"]))}</span> '
        f'<span class="pill">semantic {escape(str(intelligence["contradictions"]["semantic_state"]))}</span></section>'
    )
    return UiResponse("200 OK", _page("Derived intelligence", body))


def render_intelligence(service: AtlasService, project_id: str) -> UiResponse:
    try:
        project = service.registry.get(project_id)
        intelligence = service.project_intelligence(project_id)
    except ValidationError as exc:
        if str(exc).startswith("unknown project_id:"):
            return _error("404 Not Found", "Project not found", "The requested project is not registered.")
        return _error("500 Internal Server Error", "Derived intelligence unavailable", "Validated projection state could not be read safely.")

    kinds = {
        "concept_heading": [],
        "cross_project_link": [],
        "decision_backlink": [],
        "unanswered_question": [],
        "contradiction_evidence": [],
    }
    for item in intelligence["items"]:
        if item["kind"] in kinds:
            kinds[item["kind"]].append(item)

    def render_items(items: list[dict], empty: str) -> str:
        if not items:
            return f'<p class="muted">{escape(empty)}</p>'
        out = []
        for item in items:
            prov = item["provenance"]
            relation = _source_relation(prov)
            source_link = _canonical_source_link(prov)
            source_nav = f' · {source_link}' if source_link else ""
            out.append(
                f'<article class="hit"><strong>{escape(str(item["value"]))}</strong>'
                f'<p><span class="pill">DERIVED</span> <span class="pill">{escape(relation)}</span></p>'
                f'<p class="muted"><code>{escape(str(prov.get("repository", "")))} · '
                f'{escape(str(prov.get("source_path", "")))} · {escape(str(prov.get("source_revision", "")))}</code>{source_nav}</p>'
                f'<p class="muted">Projection <code>{escape(str(item["source_identity"]))}</code></p></article>'
            )
        return "".join(out)

    def render_decisions() -> str:
        if not intelligence["decision_backlinks"]:
            return '<p class="muted">No explicit ADR references.</p>'
        out = []
        for adr, backlinks in intelligence["decision_backlinks"].items():
            targets = intelligence["decision_targets"].get(adr, [])
            target_html = []
            for target in targets:
                prov = target["provenance"]
                link = _canonical_source_link(prov)
                target_html.append(
                    f'<li><code>{escape(str(target["project_id"]))}/{escape(str(target["source_identity"]))}</code>'
                    + (f' · {link}' if link else "")
                    + '</li>'
                )
            refs = ", ".join(
                f'{escape(str(row["source_project_id"]))}/{escape(str(row["source_identity"]))}'
                for row in backlinks
            )
            target_block = "<ul>" + "".join(target_html) + "</ul>" if target_html else '<p class="muted">Referenced ADR target is not present in validated projections.</p>'
            out.append(
                f'<article class="hit"><strong>{escape(adr)}</strong>'
                f'<p class="muted">Backlinks: {refs}</p>{target_block}</article>'
            )
        return "".join(out)

    gaps = intelligence["knowledge_gaps"]
    if gaps:
        gap_html = "".join(
            f'<article class="hit"><strong>{escape(str(gap["source_id"]))}</strong> '
            f'<span class="pill">{escape(str(gap["sync_state"]).upper())}</span>'
            f'<p class="muted"><code>{escape(str(gap["source_path"]))}</code> · {escape(str(gap["source_class"]))}</p></article>'
            for gap in gaps
        )
    else:
        gap_html = '<p class="muted">No configured-source projection gaps detected.</p>'

    body = (
        f'<p><a href="/projects/{quote(project.project_id, safe="")}">← {escape(project.display_name)}</a></p>'
        '<h1>Derived engineering intelligence</h1>'
        '<p class="muted"><span class="pill">DERIVED</span> Non-authoritative, deterministic navigation over validated projections.</p>'
        '<section class="grid">'
        f'<article class="card"><h2>{len(kinds["concept_heading"])}</h2><p>Concept anchors</p></article>'
        f'<article class="card"><h2>{len(intelligence["entities"]["source_paths"])}</h2><p>Explicit source entities</p></article>'
        f'<article class="card"><h2>{len(kinds["cross_project_link"])}</h2><p>Cross-project links</p></article>'
        f'<article class="card"><h2>{len(kinds["decision_backlink"])}</h2><p>Decision backlinks</p></article>'
        f'<article class="card"><h2>{len(kinds["unanswered_question"])}</h2><p>Unanswered questions</p></article>'
        f'<article class="card"><h2>{len(kinds["contradiction_evidence"])}</h2><p>Explicit contradiction evidence</p></article>'
        f'<article class="card"><h2>{len(gaps)}</h2><p>Knowledge gaps</p></article>'
        '</section>'
        '<section class="card" style="margin-top:16px"><h2>Concept anchors</h2>'
        + render_items(kinds["concept_heading"], "No explicit Markdown headings derived.")
        + '</section><section class="card" style="margin-top:16px"><h2>Entities</h2>'
        + (
            "".join(
                f'<article class="hit"><strong>{escape(path)}</strong><p class="muted">'
                + ", ".join(
                    f'{escape(str(row["source_identity"]))} · {escape(str(row["source_class"]))}'
                    for row in rows
                )
                + '</p></article>'
                for path, rows in intelligence["entities"]["source_paths"].items()
            )
            or '<p class="muted">No validated source entities.</p>'
        )
        + '</section><section class="card" style="margin-top:16px"><h2>Cross-project links</h2>'
        + render_items(kinds["cross_project_link"], "No explicit links to other registered engineering repositories.")
        + '</section><section class="card" style="margin-top:16px"><h2>Decision backlinks</h2>'
        + render_decisions()
        + '</section><section class="card" style="margin-top:16px"><h2>Unanswered questions</h2>'
        + render_items(kinds["unanswered_question"], "No explicit QUESTION/TODO markers.")
        + '</section><section class="card" style="margin-top:16px"><h2>Explicit contradiction evidence</h2>'
        + render_items(kinds["contradiction_evidence"], "No explicit contradiction markers observed.")
        + '</section><section class="card" style="margin-top:16px"><h2>Knowledge gaps</h2>'
        + gap_html
        + '</section><section class="card" style="margin-top:16px"><h2>Contradictions</h2>'
        f'<span class="pill">{escape(str(intelligence["contradictions"]["state"]))}</span> '
        f'<span class="muted">{escape(str(intelligence["contradictions"]["detail"]))}</span> '
        f'<span class="pill">semantic {escape(str(intelligence["contradictions"]["semantic_state"]))}</span></section>'
    )
    return UiResponse("200 OK", _page(f"{project.display_name} intelligence", body))


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
    engineering = service.engineering_system_observation(project_id)
    adoption_state = str(engineering["state"])
    lifecycle = lifecycle_view(service.data_root, project.repository)
    try:
        intelligence = service.project_intelligence(project_id)
    except ValidationError:
        intelligence = None
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
        f'<dt>Engineering System adoption</dt><dd><span class="pill">{escape(adoption_state)}</span> <span class="muted">{escape(str(engineering["detail"]))}</span></dd>'
        f'<dt>Engineering System</dt><dd><code>{escape(str(engineering["version"] or "UNKNOWN"))}</code> · <code>{escape(str(engineering["mode"] or "UNKNOWN"))}</code> · CI <code>{escape(str(engineering["ci_mode"] or "UNKNOWN"))}</code></dd>'
        f'<dt>Engineering baseline</dt><dd><code>{escape(str(engineering["baseline"] or "UNKNOWN"))}</code></dd>'
        f'<dt>GitHub Work / PR</dt><dd><span class="pill">{escape(lifecycle.work.state)}</span> <span class="muted">{escape(lifecycle.work.detail)}</span></dd>'
        f'<dt>CI</dt><dd><span class="pill">{escape(lifecycle.ci.state)}</span> <span class="muted">{escape(lifecycle.ci.detail)}</span></dd>'
        f'<dt>Tests</dt><dd><span class="pill">{escape(lifecycle.tests.state)}</span> <span class="muted">{escape(lifecycle.tests.detail)}</span></dd>'
        f'<dt>Release</dt><dd><span class="pill">{escape(lifecycle.release.state)}</span> <span class="muted">{escape(lifecycle.release.detail)}</span></dd>'
        f'<dt>Surface Reconciliation</dt><dd><span class="pill">{escape(lifecycle.surface_reconciliation.state)}</span> <span class="muted">{escape(lifecycle.surface_reconciliation.detail)}</span></dd>'
        f'<dt>Full User E2E</dt><dd><span class="pill">{escape(lifecycle.full_user_e2e.state)}</span> <span class="muted">{escape(lifecycle.full_user_e2e.detail)}</span></dd>'
        f'</dl><p><a href="/projects/{pid}/lifecycle">Open lifecycle evidence →</a></p></section></div>'
        f'<p><a href="/projects/{pid}/intelligence">Open derived engineering intelligence →</a></p>'
    )
    projected_source_ids = {str(r.get("source_id", "")) for r in records if r.get("sync_state") in {"success", "unchanged", "ok"}}
    configured_source_ids = set(project.sources)
    missing_projection_ids = sorted(configured_source_ids - projected_source_ids)
    coverage_state = "COMPLETE" if configured_source_ids and not missing_projection_ids else ("EMPTY" if not configured_source_ids else "GAPS")
    body += '<section class="card" style="margin-top:16px"><h2>Knowledge coverage</h2><dl>'
    body += f'<dt>Coverage</dt><dd><span class="pill">{coverage_state}</span> {len(projected_source_ids & configured_source_ids)}/{len(configured_source_ids)} configured sources projected</dd>'
    if missing_projection_ids:
        body += f'<dt>Projection gaps</dt><dd><code>{escape(", ".join(missing_projection_ids))}</code></dd>'
    if intelligence is None:
        body += '<dt>Derived intelligence</dt><dd><span class="pill">UNAVAILABLE</span> <span class="muted">validated derived-intelligence state could not be built</span></dd>'
    else:
        contradiction = intelligence["contradictions"]
        questions = intelligence["unanswered_questions"]
        body += (
            f'<dt>Contradictions</dt><dd><span class="pill">{escape(str(contradiction["state"]))}</span> '
            f'<span class="muted">{escape(str(contradiction["detail"]))}</span></dd>'
            f'<dt>Unanswered questions</dt><dd><span class="pill">{len(questions)}</span> explicit QUESTION/TODO item(s)</dd>'
            f'<dt>Knowledge gaps</dt><dd><span class="pill">{len(intelligence["knowledge_gaps"])}</span> configured-source gap(s)</dd>'
        )
    body += f'</dl><p><a href="/projects/{pid}/intelligence">Open derived intelligence →</a></p></section>'
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
            elif path == "/intelligence":
                response = render_intelligence_overview(service)
            elif path == "/search":
                query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True).get("q", [""])[0]
                response = _error("400 Bad Request", "Invalid search", "Search query is too long.") if len(query) > _MAX_QUERY else render_cross_project_search(service, query.strip())
            elif path.startswith("/projects/") and path.endswith("/lifecycle") and "/" not in path[len("/projects/"):-len("/lifecycle")]:
                project_id = unquote(path[len("/projects/"):-len("/lifecycle")])
                response = render_lifecycle(service, project_id)
            elif path.startswith("/projects/") and path.endswith("/intelligence") and "/" not in path[len("/projects/"):-len("/intelligence")]:
                project_id = unquote(path[len("/projects/"):-len("/intelligence")])
                response = render_intelligence(service, project_id)
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
