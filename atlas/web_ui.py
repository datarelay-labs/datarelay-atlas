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
:root{{font-family:Inter,ui-sans-serif,system-ui,sans-serif;color:#171717;background:#fafafa}}body{{margin:0}}header{{padding:18px 28px;border-bottom:1px solid #e5e5e5;background:#fff}}main{{max-width:1120px;margin:auto;padding:28px}}a{{color:#1457d9;text-decoration:none}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}}.card{{background:#fff;border:1px solid #e5e5e5;border-radius:12px;padding:18px}}.muted{{color:#666}}.pill{{display:inline-block;padding:3px 8px;border-radius:999px;background:#f0f0f0;font-size:12px}}input{{width:min(620px,70%);padding:10px;border:1px solid #bbb;border-radius:8px}}button{{padding:10px 14px;border:0;border-radius:8px;background:#171717;color:white}}code{{font-size:12px}}h1{{margin-top:0}}dl{{display:grid;grid-template-columns:max-content 1fr;gap:7px 16px}}dt{{color:#666}}dd{{margin:0;overflow-wrap:anywhere}}table{{width:100%;border-collapse:collapse}}th,td{{padding:9px 10px;border-bottom:1px solid #eee;text-align:left;vertical-align:top}}th{{font-size:12px;color:#666}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#f7f7f7;border-radius:8px;padding:14px;max-height:70vh;overflow:auto}}.hit{{margin:14px 0}}.snippet{{white-space:pre-wrap;overflow-wrap:anywhere}}
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


def _source_detail_url(project_id: str, identity: str) -> str:
    source_id, separator, _ref = identity.partition("@")
    if not separator or not source_id:
        return ""
    return f'/projects/{quote(project_id, safe="")}/sources/{quote(source_id, safe="")}'


def _canonical_source_link(provenance: dict[str, object]) -> str:
    repository = str(provenance.get("repository") or "")
    revision = str(provenance.get("source_revision") or "")
    source_path = str(provenance.get("source_path") or "")
    if "/" not in repository or len(revision) != 40 or not source_path:
        return ""
    url = f"https://github.com/{repository}/blob/{revision}/{quote(source_path, safe='/')}"
    return f'<a href="{escape(url, quote=True)}" rel="noreferrer">canonical source ↗</a>'


def _projection_source_link(project_id: str, identity: str) -> str:
    source_id, separator, _ref = identity.partition("@")
    if not separator or not source_id:
        return ""
    href = f'/projects/{quote(project_id, safe="")}/sources/{quote(source_id, safe="")}'
    return f'<a href="{href}">Atlas source →</a>'


def render_projects(service: AtlasService) -> UiResponse:
    projects = service.list_projects()
    try:
        operations = service.operations_readiness()
    except ValidationError:
        operations = {
            "state": "UNAVAILABLE",
            "runtime": {"state": "UNAVAILABLE"},
            "release_readiness": {"state": "UNKNOWN"},
        }
    try:
        providers = service.provider_dashboard()
    except ValidationError:
        providers = {"state": "UNAVAILABLE", "routes": [], "plan": None}
    try:
        decision_plane = service.decision_plane_dashboard()
    except ValidationError:
        decision_plane = {"state": "UNAVAILABLE", "rollout_state": "SHADOW"}
    try:
        instruction_governance = service.instruction_governance_dashboard()
    except ValidationError:
        instruction_governance = {"state": "UNAVAILABLE", "audit_count": 0}
    try:
        concurrency = service.concurrency_dashboard()
    except ValidationError:
        concurrency = {"state": "UNAVAILABLE", "dispatch_authority": "NO_DISPATCH_AUTHORITY"}
    try:
        personal = service.personal_knowledge_dashboard()
    except ValidationError:
        personal = {"state": "UNAVAILABLE", "totals": {"personal_sources": 0}}
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
    body = f'<h1>Atlas Overview</h1><p class="muted">Read-only engineering knowledge and lifecycle navigation.</p><div class="grid"><section class="card"><h2>{len(projects)}</h2><p>Registered projects</p></section><section class="card"><h2>{enabled}</h2><p>Enabled projects</p></section><section class="card"><h2>{sources}</h2><p>Configured sources</p></section><section class="card"><h2>{escape(str(operations["runtime"]["state"]))}</h2><p>Runtime readiness</p></section><section class="card"><h2>{escape(str(operations["state"]))}</h2><p>Deployment profile</p></section><section class="card"><h2>{escape(str(operations["release_readiness"]["state"]))}</h2><p>Release readiness</p></section><section class="card"><h2>{escape(str(providers["state"]))}</h2><p>Provider capacity</p></section><section class="card"><h2>{escape(str(decision_plane["rollout_state"]))}</h2><p>Decision Plane</p></section><section class="card"><h2>{escape(str(instruction_governance["state"]))}</h2><p>Instruction governance</p></section><section class="card"><h2>{escape(str(concurrency["state"]))}</h2><p>Concurrency admission</p></section><section class="card"><h2>{personal["totals"]["personal_sources"]}</h2><p>Personal knowledge sources</p></section></div><p><a href="/search">Search across projects →</a> · <a href="/intelligence">Derived intelligence →</a> · <a href="/operations">Operations readiness →</a> · <a href="/providers">Provider capacity →</a> · <a href="/decision-plane">Decision Plane →</a> · <a href="/instruction-governance">Instruction governance →</a> · <a href="/concurrency">Concurrency →</a> · <a href="/personal">Personal knowledge →</a></p><h2>Projects</h2><section class="grid">' + listing + "</section>"
    return UiResponse("200 OK", _page("Projects", body))

def render_personal_knowledge(service: AtlasService, query: str = "", project_id: str = "") -> UiResponse:
    try:
        dashboard = service.personal_knowledge_dashboard()
    except ValidationError:
        return _error(
            "500 Internal Server Error",
            "Personal knowledge unavailable",
            "Personal/reference inventory or import metadata could not be validated safely.",
        )
    totals = dashboard["totals"]
    project_rows = ""
    for project in dashboard["projects"]:
        project_rows += (
            f'<tr><td><code>{escape(str(project["project_id"]))}</code></td>'
            f'<td>{escape(str(project["display_name"]))}</td>'
            f'<td>{project["personal_source_count"]}</td>'
            f'<td>{project["engineering_source_count"]}</td>'
            f'<td>{"enabled" if project["enabled"] else "disabled"}</td></tr>'
        )
    project_rows = project_rows or '<tr><td colspan="5" class="muted">No personal/reference sources registered.</td></tr>'

    manifest = dashboard["import_manifest"]
    manifest_rows = ""
    for item in manifest["items"]:
        manifest_rows += (
            f'<tr><td><code>{escape(str(item["external_id"] or "NONE"))}</code></td>'
            f'<td><code>{escape(str(item["source_id"] or "NONE"))}</code></td>'
            f'<td>{escape(str(item["title"] or ""))}</td>'
            f'<td><span class="pill">{escape(str(item["state"]))}</span></td>'
            f'<td><code>{escape(str(item["reason_code"] or ""))}</code></td></tr>'
        )
    manifest_rows = manifest_rows or '<tr><td colspan="5" class="muted">No bounded import/quarantine item metadata.</td></tr>'

    body = (
        '<p><a href="/">← Projects</a></p><h1>Personal Knowledge Plane</h1>'
        '<p class="muted"><span class="pill">PERSONAL_REFERENCE_ONLY</span> '
        'Personal notes are derived context, never canonical engineering authority. No Tela runtime dependency is required.</p>'
        '<section class="grid">'
        f'<article class="card"><h2>{totals["personal_sources"]}</h2><p>Personal sources</p></article>'
        f'<article class="card"><h2>{totals["engineering_sources"]}</h2><p>Engineering sources (excluded from personal search)</p></article>'
        f'<article class="card"><h2>{totals["successful_projections"]}</h2><p>Successful personal projections</p></article>'
        f'<article class="card"><h2>{totals["snapshots_present"]}</h2><p>Durable snapshots present</p></article>'
        f'<article class="card"><h2>{totals["projection_errors"] + totals["projection_gaps"]}</h2><p>Projection errors / gaps</p></article>'
        '</section>'
        '<section class="card" style="margin-top:16px"><h2>Projects</h2><div style="overflow:auto"><table>'
        '<thead><tr><th>Project</th><th>Name</th><th>Personal</th><th>Engineering</th><th>State</th></tr></thead>'
        f'<tbody>{project_rows}</tbody></table></div></section>'
        '<section class="card" style="margin-top:16px"><h2>Import / quarantine metadata</h2>'
        f'<p><span class="pill">{escape(str(manifest["state"]))}</span> '
        f'<span class="muted">{escape(str(manifest["detail"]))}</span></p>'
        '<div style="overflow:auto"><table><thead><tr><th>External ID</th><th>Source ID</th><th>Title</th><th>State</th><th>Reason</th></tr></thead>'
        f'<tbody>{manifest_rows}</tbody></table></div></section>'
        '<section class="card" style="margin-top:16px"><h2>Search personal knowledge</h2>'
        f'<form method="get" action="/personal"><input name="q" maxlength="{_MAX_QUERY}" value="{escape(query, quote=True)}" placeholder="Search personal/reference knowledge">'
        f'<input name="project" maxlength="128" value="{escape(project_id, quote=True)}" placeholder="Project ID (required when searching)">'
        '<button type="submit">Search</button></form>'
    )
    if query:
        if not project_id:
            body += '<p class="muted">Select an explicit personal project ID to search.</p>'
        else:
            try:
                hits = service.personal_search(project_id, query, limit=8)
            except ValidationError as exc:
                return _error("400 Bad Request", "Personal search unavailable", str(exc))
            body += f'<p class="muted">{len(hits)} personal/reference result(s)</p>'
            for hit in hits:
                prov = hit.provenance
                body += (
                    f'<article class="hit"><h3>{escape(hit.title or hit.path)}</h3>'
                    f'<p class="snippet">{escape(hit.content or "")}</p><dl>'
                    f'<dt>Source</dt><dd><code>{escape(str(prov.get("source_path", "")))}</code></dd>'
                    f'<dt>Revision</dt><dd><code>{escape(str(prov.get("source_revision", "")))}</code></dd>'
                    f'<dt>Authority</dt><dd>{escape(_source_relation(prov))}</dd>'
                    f'<dt>Projection</dt><dd><span class="pill">DERIVED</span> <code>{escape(hit.identity)}</code></dd></dl></article>'
                )
            if not hits:
                body += '<p>No personal/reference matches.</p>'
    body += '</section>'
    return UiResponse("200 OK", _page("Personal Knowledge", body))


def render_concurrency(service: AtlasService) -> UiResponse:
    try:
        dashboard = service.concurrency_dashboard()
    except ValidationError:
        return _error(
            "500 Internal Server Error",
            "Concurrency admission unavailable",
            "Concurrency admission or measurement evidence could not be read safely.",
        )

    plan = dashboard.get("plan")
    try:
        executions = service.concurrency_execution_dashboard()
    except ValidationError:
        executions = {
            "state": "UNAVAILABLE",
            "authority": "ORCHESTRATION_EVIDENCE_ONLY",
            "pass_authority": "NONE",
            "execution_count": 0,
            "stage_counts": {
                "IN_PROGRESS": 0,
                "AWAITING_JOIN": 0,
                "COMPLETE": 0,
                "PARTIAL": 0,
                "FAILED": 0,
                "HUMAN_REQUIRED": 0,
            },
            "latest_execution": None,
        }
    try:
        dispatch_joins = service.concurrency_dispatch_join_dashboard()
    except ValidationError:
        dispatch_joins = {
            "state": "UNAVAILABLE",
            "authority": "MEASUREMENT_ONLY",
            "pass_authority": "MEASUREMENT_ONLY",
            "join_count": 0,
            "result_counts": {"PASS": 0, "PARTIAL": 0, "FAILED": 0, "HUMAN_REQUIRED": 0},
            "latest_join": None,
        }
    try:
        dispatch_effects = service.concurrency_dispatch_effect_dashboard()
    except ValidationError:
        dispatch_effects = {
            "state": "UNAVAILABLE",
            "authority": "DISPATCH_EFFECT_RECEIPT_ONLY",
            "effect_count": 0,
            "terminal_count": 0,
            "in_progress_count": 0,
            "latest_effect": None,
            "join_authority": "NONE",
            "pass_authority": "NONE",
        }
    try:
        dispatch_authorization = service.concurrency_dispatch_authorization_dashboard()
    except ValidationError:
        dispatch_authorization = {
            "state": "UNAVAILABLE",
            "binding_state": "STALE",
            "authority": "DISPATCH_AUTHORIZATION_ONLY",
            "dispatch_effect_authority": "NONE",
            "detail": "authorization state unavailable",
            "authorization": None,
        }
    assignment_rows = ""
    blocked_rows = ""
    eligible_slot_rows = ""
    ineligible_slot_rows = ""
    plan_html = '<p class="muted">No concurrency admission snapshot loaded.</p>'
    if isinstance(plan, dict):
        assignment_rows = "".join(
            f'<tr><td><code>{escape(str(item["node_id"]))}</code></td>'
            f'<td><code>{escape(str(item["repository"]))}#{item["issue_number"]}</code></td>'
            f'<td><code>{escape(str(item["head"]))}</code></td>'
            f'<td><code>{escape(str(item["slot_id"]))}</code></td>'
            f'<td><code>{escape(str(item["worker_id"]))}</code></td>'
            f'<td>{escape(str(item["provider"]))} / <code>{escape(str(item["runtime"]))}</code></td>'
            f'<td><code>{escape(str(item["route_id"] or "NONE"))}</code></td></tr>'
            for item in plan["assignments"]
        ) or '<tr><td colspan="7" class="muted">No nodes admitted.</td></tr>'
        blocked_rows = "".join(
            f'<tr><td><code>{escape(str(item["node_id"]))}</code></td>'
            f'<td><code>{escape(str(item["repository"]))}#{item["issue_number"]}</code></td>'
            f'<td>{escape(", ".join(item["reasons"]))}</td></tr>'
            for item in plan["blocked_selected_nodes"]
        ) or '<tr><td colspan="3" class="muted">No graph-selected nodes blocked by the admission layer.</td></tr>'
        eligible_slot_rows = "".join(
            f'<tr><td><code>{escape(str(item["slot_id"]))}</code></td>'
            f'<td><code>{escape(str(item["worker_id"]))}</code></td>'
            f'<td>{escape(str(item["provider"]))}</td>'
            f'<td><code>{escape(str(item["runtime"]))}</code></td>'
            f'<td><code>{escape(str(item["route_id"] or "NONE"))}</code></td>'
            f'<td><code>{escape(str(item["evidence_ref"]))}</code></td></tr>'
            for item in plan["eligible_slots"]
        ) or '<tr><td colspan="6" class="muted">No eligible execution slots.</td></tr>'
        ineligible_slot_rows = "".join(
            f'<tr><td><code>{escape(str(item["slot_id"]))}</code></td>'
            f'<td><code>{escape(str(item["worker_id"]))}</code></td>'
            f'<td>{escape(str(item["provider"]))}</td>'
            f'<td>{escape(", ".join(item["reasons"]))}</td></tr>'
            for item in plan["ineligible_slots"]
        ) or '<tr><td colspan="4" class="muted">No ineligible execution slots.</td></tr>'
        project_limits = ", ".join(
            f'{item["repository"]}:{item["max_wip"]}'
            for item in plan["project_limits"]
        )
        plan_html = (
            '<dl>'
            f'<dt>Authority</dt><dd><span class="pill">{escape(str(plan["authority"]))}</span></dd>'
            f'<dt>Dispatch authority</dt><dd><span class="pill">{escape(str(plan["dispatch_authority"]))}</span></dd>'
            f'<dt>Plan digest</dt><dd><code>{escape(str(plan["plan_digest"]))}</code></dd>'
            f'<dt>Graph state</dt><dd><span class="pill">{escape(str(plan["graph_state"]))}</span> {escape(", ".join(plan["graph_reasons"]) or "no graph blockers")}</dd>'
            f'<dt>Global WIP</dt><dd>{plan["active_count"]}/{plan["global_max_wip"]}</dd>'
            f'<dt>Max parallel admission</dt><dd>{plan["max_parallel_admission"]}</dd>'
            f'<dt>Project WIP limits</dt><dd><code>{escape(project_limits)}</code></dd>'
            f'<dt>Graph selected</dt><dd><code>{escape(", ".join(plan["graph_selected_node_ids"]) or "NONE")}</code></dd>'
            f'<dt>Admitted</dt><dd>{len(plan["assignments"])}</dd>'
            '</dl>'
        )

    run_rows = "".join(
        f'<tr><td><code>{escape(str(item["run_id"]))}</code></td>'
        f'<td><span class="pill">{escape(str(item["result"]))}</span></td>'
        f'<td>{len(item["outcomes"])}</td>'
        f'<td>{item["complete_count"]}/{item["failed_count"]}/{item["human_required_count"]}</td>'
        f'<td>{item["wall_time_ms"]}</td>'
        f'<td>{item["sum_node_duration_ms"]}</td>'
        f'<td>{item["parallelism_basis_points"] / 10000:.2f}x</td></tr>'
        for item in dashboard["runs"]
    ) or '<tr><td colspan="7" class="muted">No measured concurrency runs recorded.</td></tr>'

    latest = dashboard["latest_run"]
    latest_html = '<p class="muted">No run has been measured yet.</p>'
    if isinstance(latest, dict):
        latest_outcomes = "".join(
            f'<li><code>{escape(str(item["node_id"]))}</code> → '
            f'<span class="pill">{escape(str(item["outcome"]))}</span> '
            f'<code>{escape(str(item["worker_id"]))}</code> / {escape(str(item["provider"]))} '
            f'({item["duration_ms"]} ms)</li>'
            for item in latest["outcomes"]
        )
        latest_html = (
            f'<p><span class="pill">{escape(str(latest["result"]))}</span> '
            f'<code>{escape(str(latest["run_id"]))}</code></p>'
            f'<ul>{latest_outcomes}</ul>'
        )

    body = (
        '<p><a href="/">← Projects</a></p><h1>Measured concurrency</h1>'
        '<p class="muted">Provider-neutral advisory admission and measurement. Existing single-effect authorization remains unchanged.</p>'
        '<section class="grid">'
        f'<article class="card"><h2>{escape(str(dashboard["state"]))}</h2><p>Admission state</p></article>'
        f'<article class="card"><h2>{escape(str(dashboard["dispatch_authority"]))}</h2><p>Dispatch authority</p></article>'
        f'<article class="card"><h2>{dashboard["run_count"]}</h2><p>Measured runs</p></article>'
        f'<article class="card"><h2>{dashboard["completed_nodes"]}/{dashboard["total_measured_nodes"]}</h2><p>Completed measured nodes</p></article>'
        f'<article class="card"><h2>{dashboard["partial_failure_runs"]}</h2><p>Partial/HUMAN runs</p></article>'
        f'<article class="card"><h2>{dashboard["max_parallelism_basis_points"] / 10000:.2f}x</h2><p>Max measured work/wall ratio</p></article>'
        '</section>'
        '<section class="card" style="margin-top:16px"><h2>Admission plan</h2>'
        + plan_html
        + '<p class="muted">Assignments are evidence only. This surface cannot activate packets or dispatch workers.</p></section>'
        '<section class="card" style="margin-top:16px"><h2>Admitted assignments</h2>'
        '<div style="overflow:auto"><table><thead><tr><th>Node</th><th>Packet</th><th>HEAD</th><th>Slot</th><th>Worker</th><th>Provider/runtime</th><th>Route</th></tr></thead>'
        f'<tbody>{assignment_rows or "<tr><td colspan=7>No snapshot.</td></tr>"}</tbody></table></div></section>'
        '<section class="card" style="margin-top:16px"><h2>Execution cycles</h2><dl>'
        f'<dt>State</dt><dd><span class="pill">{escape(str(executions["state"]))}</span></dd>'
        f'<dt>Cycles</dt><dd>{executions["execution_count"]}</dd>'
        f'<dt>Authority</dt><dd><code>{escape(str(executions["authority"]))}</code></dd>'
        f'<dt>PASS authority</dt><dd><code>{escape(str(executions["pass_authority"]))}</code></dd>'
        f'<dt>Stages</dt><dd><code>{escape(str(executions["stage_counts"]))}</code></dd></dl>'
        '<p class="muted">Execution cycles bind one current plan to one authorization/effect and reject plan replay before external dispatch. COMPLETE is orchestration evidence only, never engineering final PASS.</p></section>'
        '<section class="card" style="margin-top:16px"><h2>Multi-node dispatch authorization</h2><dl>'
        f'<dt>State</dt><dd><span class="pill">{escape(str(dispatch_authorization["state"]))}</span></dd>'
        f'<dt>Binding</dt><dd><span class="pill">{escape(str(dispatch_authorization["binding_state"]))}</span></dd>'
        f'<dt>Authority</dt><dd><code>{escape(str(dispatch_authorization["authority"]))}</code></dd>'
        f'<dt>Dispatch effect authority</dt><dd><code>{escape(str(dispatch_authorization["dispatch_effect_authority"]))}</code></dd>'
        f'<dt>Detail</dt><dd>{escape(str(dispatch_authorization["detail"]))}</dd></dl>'
        '<p class="muted">Authorization only. This surface does not spawn workers, mutate GitHub, invoke providers, retry effects, or declare join/PASS.</p></section>'
        '<section class="card" style="margin-top:16px"><h2>Dispatch effect receipts</h2><dl>'
        f'<dt>State</dt><dd><span class="pill">{escape(str(dispatch_effects["state"]))}</span></dd>'
        f'<dt>Effects</dt><dd>{dispatch_effects["effect_count"]}</dd>'
        f'<dt>Terminal / in progress</dt><dd>{dispatch_effects["terminal_count"]} / {dispatch_effects["in_progress_count"]}</dd>'
        f'<dt>Authority</dt><dd><code>{escape(str(dispatch_effects["authority"]))}</code></dd>'
        f'<dt>Join authority</dt><dd><code>{escape(str(dispatch_effects["join_authority"]))}</code></dd>'
        f'<dt>PASS authority</dt><dd><code>{escape(str(dispatch_effects["pass_authority"]))}</code></dd></dl>'
        '<p class="muted">Each authorized assignment may be dispatched once. Completion/join truth remains in measured concurrency run evidence.</p></section>'
        '<section class="card" style="margin-top:16px"><h2>Dispatch-bound join evidence</h2><dl>'
        f'<dt>State</dt><dd><span class="pill">{escape(str(dispatch_joins["state"]))}</span></dd>'
        f'<dt>Joins</dt><dd>{dispatch_joins["join_count"]}</dd>'
        f'<dt>Authority</dt><dd><code>{escape(str(dispatch_joins["authority"]))}</code></dd>'
        f'<dt>PASS authority</dt><dd><code>{escape(str(dispatch_joins["pass_authority"]))}</code></dd>'
        f'<dt>Results</dt><dd><code>{escape(str(dispatch_joins["result_counts"]))}</code></dd></dl>'
        '<p class="muted">PASS here means all actually dispatched assignments completed; it is measurement evidence, not engineering final PASS.</p></section>'
        '<section class="card" style="margin-top:16px"><h2>Admission-blocked selected nodes</h2>'
        '<div style="overflow:auto"><table><thead><tr><th>Node</th><th>Packet</th><th>Reasons</th></tr></thead>'
        f'<tbody>{blocked_rows or "<tr><td colspan=3>No snapshot.</td></tr>"}</tbody></table></div></section>'
        '<section class="card" style="margin-top:16px"><h2>Execution slots</h2><h3>Eligible</h3>'
        '<div style="overflow:auto"><table><thead><tr><th>Slot</th><th>Worker</th><th>Provider</th><th>Runtime</th><th>Route</th><th>Evidence</th></tr></thead>'
        f'<tbody>{eligible_slot_rows or "<tr><td colspan=6>No snapshot.</td></tr>"}</tbody></table></div>'
        '<h3>Ineligible</h3><div style="overflow:auto"><table><thead><tr><th>Slot</th><th>Worker</th><th>Provider</th><th>Reasons</th></tr></thead>'
        f'<tbody>{ineligible_slot_rows or "<tr><td colspan=4>No snapshot.</td></tr>"}</tbody></table></div></section>'
        '<section class="card" style="margin-top:16px"><h2>Latest join/reconciliation</h2>'
        + latest_html
        + '</section>'
        '<section class="card" style="margin-top:16px"><h2>Measured runs</h2>'
        '<div style="overflow:auto"><table><thead><tr><th>Run</th><th>Result</th><th>Nodes</th><th>Complete/Fail/Human</th><th>Wall ms</th><th>Node ms sum</th><th>Work/wall</th></tr></thead>'
        f'<tbody>{run_rows}</tbody></table></div></section>'
        '<p class="muted">Worker/provider identity is attribution only. Maximum agent count is never a target; admission is bounded by graph safety, WIP, project limits, slot gates and evidence freshness.</p>'
    )
    return UiResponse("200 OK", _page("Measured concurrency", body))


def render_instruction_governance(service: AtlasService) -> UiResponse:
    try:
        dashboard = service.instruction_governance_dashboard()
    except ValidationError:
        return _error(
            "500 Internal Server Error",
            "Instruction governance unavailable",
            "Managed instruction inventory or audit history could not be read safely.",
        )

    surface_rows = "".join(
        f'<tr><td><code>{escape(str(item["path"]))}</code></td>'
        f'<td>{escape(str(item["category"]))}</td>'
        f'<td>{item["size_bytes"]}</td>'
        f'<td><code>{escape(str(item["content_digest"]))}</code></td></tr>'
        for item in dashboard["managed_surfaces"]
    )
    audit_rows = "".join(
        f'<tr><td><code>{escape(str(item["evaluated_at"]))}</code></td>'
        f'<td><span class="pill">{escape(str(item["outcome"]))}</span></td>'
        f'<td>{escape(str(item["trigger_kind"]))}<br><code>{escape(str(item["trigger_revision"]))}</code></td>'
        f'<td>{escape(str(item["model_provider"]))} / <code>{escape(str(item["model_name"]))}</code><br><code>{escape(str(item["model_profile"]))}</code></td>'
        f'<td><code>{escape(str(item["engineering_system_revision"]))}</code></td>'
        f'<td>{len(item["candidate_changes"])}</td>'
        f'<td><code>{escape(str(item["evaluation_ref"]))}</code></td></tr>'
        for item in dashboard["audits"]
    ) or '<tr><td colspan="7" class="muted">No instruction-governance audits recorded.</td></tr>'

    counts = dashboard["outcome_counts"]
    try:
        routing = service.instruction_governance_routing()
    except ValidationError:
        routing = {
            "state": "UNAVAILABLE",
            "route_action": "HUMAN_REQUIRED",
            "authority": "ROUTING_ADVISORY_ONLY",
            "mutation_authority": "NONE",
            "reasons": ["ROUTING_STATE_UNAVAILABLE"],
        }
    latest = dashboard["latest_audit"]
    latest_html = '<p class="muted">No audit has been recorded yet.</p>'
    if isinstance(latest, dict):
        behavior = ", ".join(
            f'{item["scenario_id"]}:{item["outcome"]}'
            for item in latest["behavior_results"]
        )
        latest_html = (
            '<dl>'
            f'<dt>Outcome</dt><dd><span class="pill">{escape(str(latest["outcome"]))}</span></dd>'
            f'<dt>Audit identity</dt><dd><code>{escape(str(latest["audit_identity"]))}</code></dd>'
            f'<dt>Target HEAD</dt><dd><code>{escape(str(latest["target_head"]))}</code></dd>'
            f'<dt>Engineering System</dt><dd><code>{escape(str(latest["engineering_system_revision"]))}</code></dd>'
            f'<dt>Model profile</dt><dd>{escape(str(latest["model_provider"]))} / <code>{escape(str(latest["model_name"]))}</code> / <code>{escape(str(latest["model_profile"]))}</code></dd>'
            f'<dt>Harness</dt><dd><code>{escape(str(latest["harness_id"]))}</code> / <code>{escape(str(latest["harness_revision"]))}</code></dd>'
            f'<dt>Behavior</dt><dd><code>{escape(behavior or "NONE")}</code></dd>'
            f'<dt>Candidate changes</dt><dd>{len(latest["candidate_changes"])}</dd>'
            f'<dt>Canonical mutation</dt><dd><code>{str(bool(latest["canonical_mutation"])).lower()}</code></dd>'
            '</dl>'
        )

    body = (
        '<p><a href="/">← Projects</a></p><h1>Instruction governance</h1>'
        '<p class="muted">Engineering-System-referenced audit evidence only. Atlas does not define a competing prompting methodology and does not rewrite managed surfaces.</p>'
        '<section class="grid">'
        f'<article class="card"><h2>{escape(str(dashboard["state"]))}</h2><p>Governance state</p></article>'
        f'<article class="card"><h2>{dashboard["managed_surface_count"]}</h2><p>Managed surfaces</p></article>'
        f'<article class="card"><h2>{dashboard["audit_count"]}</h2><p>Recorded audits</p></article>'
        f'<article class="card"><h2>{counts["CANARY_READY"]}</h2><p>Canary-ready findings</p></article>'
        f'<article class="card"><h2>{counts["REJECTED"]}</h2><p>Rejected candidates</p></article>'
        f'<article class="card"><h2>{escape(str(dashboard["mutation_authority"]))}</h2><p>Mutation authority</p></article>'
        '</section>'
        '<section class="card" style="margin-top:16px"><h2>Candidate routing</h2><dl>'
        f'<dt>State</dt><dd><span class="pill">{escape(str(routing["state"]))}</span></dd>'
        f'<dt>Route</dt><dd><span class="pill">{escape(str(routing["route_action"]))}</span></dd>'
        f'<dt>Authority</dt><dd><code>{escape(str(routing["authority"]))}</code></dd>'
        f'<dt>Mutation authority</dt><dd><code>{escape(str(routing["mutation_authority"]))}</code></dd>'
        f'<dt>Reasons</dt><dd><code>{escape(", ".join(str(x) for x in routing.get("reasons", [])) or "NONE")}</code></dd></dl>'
        '<p class="muted">Routing is advisory only. CANARY_PR_REQUIRED still needs separate canary evidence and ordinary PR/adoption governance.</p></section>'
        '<section class="card" style="margin-top:16px"><h2>Latest audit</h2>'
        + latest_html
        + '</section>'
        '<section class="card" style="margin-top:16px"><h2>Managed surfaces</h2>'
        f'<p class="muted">Inventory digest <code>{escape(str(dashboard["inventory_digest"]))}</code></p>'
        '<div style="overflow:auto"><table><thead><tr><th>Path</th><th>Category</th><th>Bytes</th><th>SHA-256</th></tr></thead>'
        f'<tbody>{surface_rows}</tbody></table></div></section>'
        '<section class="card" style="margin-top:16px"><h2>Audit history</h2>'
        '<div style="overflow:auto"><table><thead><tr><th>Evaluated</th><th>Outcome</th><th>Trigger</th><th>Model/profile</th><th>Engineering System</th><th>Changes</th><th>Evidence</th></tr></thead>'
        f'<tbody>{audit_rows}</tbody></table></div></section>'
        '<p class="muted">Use CLI preflight/record with immutable AGENT_BASE and behavior-scenarios references. Exact revision/profile duplicates return DUPLICATE_NOOP before repeated evaluation.</p>'
    )
    return UiResponse("200 OK", _page("Instruction governance", body))


def render_decision_plane(
    service: AtlasService,
    *,
    optional_paths: list[str] | None = None,
    changed_paths: list[str] | None = None,
) -> UiResponse:
    try:
        dashboard = service.decision_plane_dashboard()
    except ValidationError:
        return _error(
            "500 Internal Server Error",
            "Decision Plane unavailable",
            "Decision Plane shadow/replay evidence could not be read safely.",
        )

    optional_paths = list(optional_paths or [])
    changed_paths = list(changed_paths or [])
    context_candidates = None
    context_error = ""
    check_candidates = None
    check_error = ""
    try:
        context_candidates = service.decision_plane_optional_context_candidates(
            optional_paths
        )
    except ValidationError as exc:
        context_error = str(exc)
    if changed_paths:
        try:
            check_candidates = service.decision_plane_focused_check_candidates(
                changed_paths
            )
        except ValidationError as exc:
            check_error = str(exc)

    class_counts: dict[str, dict[str, int]] = {}
    rows = []
    for record in dashboard["records"]:
        counts = class_counts.setdefault(
            record["decision_class"],
            {"total": 0, "valid": 0, "shadow": 0, "replay": 0},
        )
        counts["total"] += 1
        counts["valid"] += record["validation"] == "VALID"
        counts["shadow"] += record["mode"] == "SHADOW"
        counts["replay"] += record["mode"] == "REPLAY"
        reasons = ", ".join(record["validation_reasons"]) or "NONE"
        rows.append(
            f'<tr><td><code>{escape(record["record_id"])}</code></td>'
            f'<td><span class="pill">{escape(record["mode"])}</span></td>'
            f'<td>{escape(record["decision_class"])}</td>'
            f'<td><span class="pill">{escape(record["validation"])}</span><br><span class="muted">{escape(reasons)}</span></td>'
            f'<td><code>{escape(", ".join(record["current_choice_ids"]))}</code></td>'
            f'<td><code>{escape(", ".join(record["model_choice_ids"]) or "NONE")}</code></td>'
            f'<td>{escape(record["current_outcome"])} / {escape(record["model_outcome"])}</td>'
            f'<td>{record["cost_delta_milliunits"]} / {record["wall_time_delta_ms"]}</td></tr>'
        )
    record_rows = "".join(rows) if rows else '<tr><td colspan="8" class="muted">No Decision Plane observations loaded.</td></tr>'

    class_cards_parts = []
    for name, assessment in dashboard["class_assessments"].items():
        counts = class_counts.get(
            name,
            {"total": 0, "valid": 0, "shadow": 0, "replay": 0},
        )
        class_cards_parts.append(
            f'<article class="card"><h2>{escape(name.replace("_", " ").title())}</h2>'
            f'<p><span class="pill">{escape(str(assessment["assessment"]))}</span></p><dl>'
            f'<dt>Total</dt><dd>{counts["total"]}</dd>'
            f'<dt>Valid</dt><dd>{counts["valid"]}</dd>'
            f'<dt>Shadow</dt><dd>{counts["shadow"]}</dd>'
            f'<dt>Replay</dt><dd>{counts["replay"]}</dd>'
            f'<dt>Verified replay</dt><dd>{assessment["verified_replay_count"]}</dd>'
            f'<dt>Current/model success</dt><dd>{assessment["current_success_count"]}/{assessment["model_success_count"]}</dd>'
            f'<dt>False routing</dt><dd>{assessment["false_routing_count"]}</dd>'
            f'<dt>Cost/time Δ</dt><dd>{assessment["cost_delta_milliunits"]} / {assessment["wall_time_delta_ms"]}</dd>'
            f'<dt>Frontier/retry Δ</dt><dd>{assessment["frontier_call_delta"]} / {assessment["retry_delta"]}</dd>'
            f'<dt>Confidence range</dt><dd><code>{escape(str(assessment["confidence_min"] or "UNKNOWN"))}</code> – <code>{escape(str(assessment["confidence_max"] or "UNKNOWN"))}</code></dd>'
            '</dl></article>'
        )
    class_cards = "".join(class_cards_parts)

    canary = dashboard["canary_readiness"]
    try:
        canary_admission = service.decision_canary_dashboard()
    except ValidationError:
        canary_admission = {
            "state": "UNAVAILABLE",
            "detail": "canary admission snapshot failed validation",
            "authority": "CANARY_ADMISSION_ONLY",
            "activation_authority": "NO_ACTIVATION_AUTHORITY",
            "execution_authority": "NONE",
            "binding_state": "UNKNOWN",
            "effective_decision": "CANARY_NOT_ELIGIBLE",
            "admission": None,
        }
    canary_rows = "".join(
        f'<tr><td><code>{escape(str(item["decision_class"]))}</code></td>'
        f'<td><span class="pill">{escape(str(item["replay_assessment"]))}</span></td>'
        f'<td><span class="pill">{escape(str(item["readiness"]))}</span></td>'
        f'<td>{item["verified_replay_count"]}</td>'
        f'<td>{item["false_routing_count"]}</td>'
        f'<td><code>{escape(str(item["fallback"]))}</code></td></tr>'
        for item in canary["classes"]
    )

    context_html = (
        f'<p><span class="pill">UNAVAILABLE</span> <span class="muted">{escape(context_error)}</span></p>'
        if context_error
        else (
            '<dl>'
            f'<dt>Candidates</dt><dd><code>{escape(", ".join(context_candidates["candidate_ids"]))}</code></dd>'
            f'<dt>Mandatory</dt><dd><code>{escape(", ".join(context_candidates["required_candidate_ids"]))}</code></dd>'
            f'<dt>Authority</dt><dd><span class="pill">{escape(context_candidates["authority"])}</span></dd></dl>'
            if context_candidates is not None
            else '<p class="muted">No context candidate set available.</p>'
        )
    )
    check_html = '<p class="muted">Enter changed repository paths to prepare affected focused-check candidates.</p>'
    if check_error:
        check_html = f'<p><span class="pill">UNAVAILABLE</span> <span class="muted">{escape(check_error)}</span></p>'
    elif check_candidates is not None:
        check_html = (
            '<dl>'
            f'<dt>Affected domains</dt><dd><code>{escape(", ".join(check_candidates["affected_domains"]) or "NONE")}</code></dd>'
            f'<dt>Focused candidates</dt><dd><code>{escape(", ".join(check_candidates["candidate_ids"]) or "NONE")}</code></dd>'
            f'<dt>Terminal release gates</dt><dd><code>{escape(", ".join(check_candidates["terminal_required_ids"]) or "NONE")}</code></dd>'
            f'<dt>Authority</dt><dd><span class="pill">{escape(check_candidates["authority"])}</span></dd></dl>'
        )
    optional_value = escape(",".join(optional_paths), quote=True)
    changed_value = escape(",".join(changed_paths), quote=True)

    body = (
        '<p><a href="/">← Projects</a></p><h1>Decision Plane</h1>'
        '<p class="muted">Shadow/replay measurement only. Decision-model choices never replace current execution in this slice.</p>'
        '<section class="grid">'
        f'<article class="card"><h2>{escape(str(dashboard["rollout_state"]))}</h2><p>Rollout state</p></article>'
        f'<article class="card"><h2>{dashboard["record_count"]}</h2><p>Observations</p></article>'
        f'<article class="card"><h2>{dashboard["invalid_choice_count"]}</h2><p>Invalid model choices</p></article>'
        f'<article class="card"><h2>{dashboard["verified_replay_count"]}</h2><p>Verified replay cases</p></article>'
        f'<article class="card"><h2>{escape(str(dashboard["replay_gate"]))}</h2><p>Replay evidence gate</p></article>'
        f'<article class="card"><h2>{escape(str(dashboard["activation_authority"]))}</h2><p>Activation authority</p></article>'
        '</section>'
        '<section class="card" style="margin-top:16px"><h2>Optional context candidates</h2>'
        '<form method="get" action="/decision-plane">'
        f'<input name="optional_paths" value="{optional_value}" placeholder="docs/decisions/ADR-0008.md,docs/contracts/...">'
        f'<input type="hidden" name="changed_paths" value="{changed_value}">'
        '<button type="submit">Prepare context</button></form>'
        + context_html
        + '<p class="muted">AGENTS.md and .engineering/project.yaml remain mandatory when present.</p></section>'
        '<section class="card" style="margin-top:16px"><h2>Focused-check candidates</h2>'
        '<form method="get" action="/decision-plane">'
        f'<input name="changed_paths" value="{changed_value}" placeholder="atlas/service.py,atlas/web_ui.py">'
        f'<input type="hidden" name="optional_paths" value="{optional_value}">'
        '<button type="submit">Prepare checks</button></form>'
        + check_html
        + '<p class="muted">Terminal release gates remain deterministic and outside model selection authority.</p></section>'
        '<section class="card" style="margin-top:16px"><h2>Canary admission readiness</h2>'
        f'<p><span class="pill">{escape(str(canary["authority"]))}</span> '
        f'<span class="pill">rollout {escape(str(canary["rollout_state"]))}</span> '
        f'<span class="pill">activation {escape(str(canary["activation_authority"]))}</span></p>'
        f'<p class="muted">Evidence digest <code>{escape(str(canary["evidence_digest"]))}</code>. '
        'CANARY_REQUEST_ELIGIBLE only permits a later scoped admission request; no model choice is activated.</p>'
        '<div style="overflow:auto"><table><thead><tr><th>Decision class</th><th>Replay</th><th>Canary</th><th>Verified replay</th><th>False routing</th><th>Fallback</th></tr></thead>'
        f'<tbody>{canary_rows}</tbody></table></div></section>'
        '<section class="card" style="margin-top:16px"><h2>Canary admission</h2>'
        f'<p><span class="pill">{escape(str(canary_admission["state"]))}</span> '
        f'<span class="pill">{escape(str(canary_admission["effective_decision"]))}</span> '
        f'<span class="pill">binding {escape(str(canary_admission["binding_state"]))}</span></p>'
        f'<p class="muted">{escape(str(canary_admission["detail"]))}</p>'
        + (
            '<dl>'
            f'<dt>Authority</dt><dd><code>{escape(str(canary_admission["authority"]))}</code></dd>'
            f'<dt>Activation</dt><dd><code>{escape(str(canary_admission["activation_authority"]))}</code></dd>'
            f'<dt>Execution</dt><dd><code>{escape(str(canary_admission["execution_authority"]))}</code></dd>'
            f'<dt>Class</dt><dd><code>{escape(str(canary_admission["admission"]["decision_class"]))}</code></dd>'
            f'<dt>Scope project</dt><dd><code>{escape(str(canary_admission["admission"]["scope"]["project_id"]))}</code></dd>'
            f'<dt>Path prefixes</dt><dd><code>{escape(", ".join(canary_admission["admission"]["scope"]["path_prefixes"]))}</code></dd>'
            f'<dt>Task kinds</dt><dd><code>{escape(", ".join(canary_admission["admission"]["scope"]["task_kinds"]))}</code></dd>'
            f'<dt>Decision budget</dt><dd>{canary_admission["admission"]["max_canary_decisions"]}</dd>'
            f'<dt>Expires</dt><dd><code>{escape(str(canary_admission["admission"]["expires_at"]))}</code></dd>'
            f'<dt>Reasons</dt><dd><code>{escape(", ".join(canary_admission["admission"]["reasons"]) or "NONE")}</code></dd>'
            '</dl>'
            if isinstance(canary_admission.get("admission"), dict)
            else '<p class="muted">No bounded canary admission has been published.</p>'
        )
        + '<p class="muted">Admission remains non-executing. Any future effect boundary must re-check scope, expiry and evidence binding.</p></section>'
        '<section class="card" style="margin-top:16px"><h2>Replay metrics</h2><dl>'
        f'<dt>Current success rate</dt><dd><code>{escape(str(dashboard["current_success_rate_percent"] or "UNKNOWN"))}%</code></dd>'
        f'<dt>Model success rate</dt><dd><code>{escape(str(dashboard["model_success_rate_percent"] or "UNKNOWN"))}%</code></dd>'
        f'<dt>Cost delta</dt><dd><code>{dashboard["total_cost_delta_milliunits"]} milliunits</code></dd>'
        f'<dt>Wall-time delta</dt><dd><code>{dashboard["total_wall_time_delta_ms"]} ms</code></dd>'
        f'<dt>Frontier-call delta</dt><dd><code>{dashboard["total_frontier_call_delta"]}</code></dd>'
        f'<dt>Retry delta</dt><dd><code>{dashboard["total_retry_delta"]}</code></dd></dl>'
        '<p class="muted">REPLAY_PASS is evidence only. It does not activate the Decision Plane; promotion remains outside this slice.</p></section>'
        '<h2>Decision classes</h2><section class="grid">' + class_cards + '</section>'
        '<section class="card" style="margin-top:16px"><h2>Observations</h2>'
        '<div style="overflow:auto"><table><thead><tr><th>ID</th><th>Mode</th><th>Class</th><th>Validation</th><th>Current choice</th><th>Model choice</th><th>Outcomes</th><th>Cost / time Δ</th></tr></thead>'
        f'<tbody>{record_rows}</tbody></table></div></section>'
        '<p class="muted">Permissions, release/deploy, secrets, exact-head PASS and HUMAN_REQUIRED decisions remain outside Decision Plane authority.</p>'
    )
    return UiResponse("200 OK", _page("Decision Plane", body))


def render_providers(
    service: AtlasService,
    *,
    current_route: str = "",
    failure_reason: str = "",
    max_attempts: int = 3,
) -> UiResponse:
    try:
        dashboard = service.provider_dashboard()
    except ValidationError:
        return _error("500 Internal Server Error", "Provider capacity unavailable", "Provider capacity snapshot could not be validated safely.")

    quality = dashboard.get("route_quality") or service.provider_route_quality_dashboard()
    plan = dashboard.get("plan")
    selected = plan.get("selected_route_id") if isinstance(plan, dict) else None
    fallback = plan.get("fallback_route_ids", []) if isinstance(plan, dict) else []
    transition = None
    transition_error = ""
    if current_route or failure_reason:
        if not current_route or not failure_reason:
            transition_error = "current_route and failure_reason are both required"
        else:
            try:
                transition = service.provider_transition_preview(
                    current_route_id=current_route,
                    failure_reason=failure_reason,
                    max_attempts=max_attempts,
                )
            except ValidationError as exc:
                transition_error = str(exc)
    route_cards = []
    for route in dashboard["routes"]:
        capacity = route["capacity"]
        attribution = route["attribution"]
        operational = route["operational"]
        attribution_evidence = route.get("attribution_evidence")
        operational_evidence = route.get("operational_evidence")
        attribution_authority = (
            attribution_evidence.get("authority")
            if isinstance(attribution_evidence, dict)
            else "UNVERIFIED"
        )
        attribution_ref = (
            attribution_evidence.get("source_ref")
            if isinstance(attribution_evidence, dict)
            else None
        )
        attribution_observed = (
            attribution_evidence.get("observed_at")
            if isinstance(attribution_evidence, dict)
            else None
        )
        operational_authority = (
            operational_evidence.get("authority")
            if isinstance(operational_evidence, dict)
            else "UNVERIFIED"
        )
        operational_observed = (
            operational_evidence.get("observed_at")
            if isinstance(operational_evidence, dict)
            else None
        )
        route_role = "SELECTED" if route["route_id"] == selected else ("FALLBACK" if route["route_id"] in fallback else "CANDIDATE")
        capability_text = ", ".join(
            f'{item["name"]}:{item["status"]}'
            for item in route["capabilities"]
        )
        gate_text = ", ".join(
            f'{name}:{state}'
            for name, state in route["gates"].items()
        )
        route_cards.append(
            '<article class="card">'
            f'<span class="pill">{escape(route_role)}</span> <span class="pill">{escape(route["route_id"])}</span>'
            f'<h2>{escape(route["provider"])}</h2><p><code>{escape(route["runtime"])}</code> · <code>{escape(route["usage_mode"])}</code> · adapter <code>{escape(route["adapter"])}</code></p>'
            f'<p class="muted">Capabilities: <code>{escape(capability_text)}</code></p>'
            f'<p class="muted">Gates: <code>{escape(gate_text)}</code></p>'
            '<dl>'
            f'<dt>Remaining</dt><dd><span class="pill">{escape(capacity["remaining_capacity"]["status"])}</span> {escape(capacity["remaining_capacity"]["display"])}</dd>'
            f'<dt>Reset at</dt><dd><span class="pill">{escape(capacity["reset_at"]["status"])}</span> {escape(capacity["reset_at"]["display"])}</dd>'
            f'<dt>Capacity pool</dt><dd><span class="pill">{escape(capacity["capacity_pool"]["status"])}</span> {escape(capacity["capacity_pool"]["display"])}</dd>'
            f'<dt>Active WIP</dt><dd><span class="pill">{escape(capacity["active_inference_wip"]["status"])}</span> {escape(capacity["active_inference_wip"]["display"])}</dd>'
            f'<dt>Execution surface</dt><dd><span class="pill">{escape(attribution["execution_surface"]["status"])}</span> {escape(attribution["execution_surface"]["display"])}</dd>'
            f'<dt>Allowance domain</dt><dd><span class="pill">{escape(attribution["allowance_domain"]["status"])}</span> {escape(attribution["allowance_domain"]["display"])}</dd>'
            f'<dt>Shared allowance</dt><dd><span class="pill">{escape(attribution["shared_allowance"]["status"])}</span> {escape(attribution["shared_allowance"]["display"])}</dd>'
            f'<dt>Charging mode</dt><dd><span class="pill">{escape(attribution["charging_mode"]["status"])}</span> {escape(attribution["charging_mode"]["display"])}</dd>'
            f'<dt>Reset semantics</dt><dd><span class="pill">{escape(operational["reset_semantics"]["status"])}</span> {escape(operational["reset_semantics"]["display"])}</dd>'
            f'<dt>Health</dt><dd><span class="pill">{escape(operational["health"]["status"])}</span> {escape(operational["health"]["display"])}</dd>'
            f'<dt>Latency</dt><dd><span class="pill">{escape(operational["latency"]["status"])}</span> {escape(operational["latency"]["display"])}</dd>'
            f'<dt>Capacity evidence</dt><dd><code>{escape(str(route["capacity_evidence"]["source_kind"]))}</code> · <code>{escape(str(route["capacity_evidence"]["window_end"] or "UNKNOWN"))}</code></dd>'
            f'<dt>Attribution authority</dt><dd><code>{escape(str(attribution_authority))}</code></dd>'
            f'<dt>Attribution source</dt><dd><code>{escape(str(attribution_ref or "UNKNOWN"))}</code> · <code>{escape(str(attribution_observed or "UNKNOWN"))}</code></dd>'
            f'<dt>Operational authority</dt><dd><code>{escape(str(operational_authority))}</code></dd>'
            f'<dt>Operational observed</dt><dd><code>{escape(str(operational_observed or "UNKNOWN"))}</code></dd>'
            '</dl></article>'
        )

    quality_rows = "".join(
        f'<tr><td><code>{escape(str(row["route_id"]))}</code></td>'
        f'<td>{escape(str(row["provider"]))} / <code>{escape(str(row["runtime"]))}</code></td>'
        f'<td>{row["sample_count"]}</td>'
        f'<td>{row["verified_pass_count"]} / {row["verified_pass_rate_basis_points"] / 100:.2f}%</td>'
        f'<td>{row["first_pass_count"]} / {row["first_pass_rate_basis_points"] / 100:.2f}%</td>'
        f'<td>{row["rework_total"]} / {row["audit_finding_total"]}</td>'
        f'<td>{row["owner_intervention_total"]} / {row["quota_interruption_total"]}</td>'
        f'<td>{row["wall_time_average_ms"]}</td>'
        f'<td>{row["observed_cost_total_milliunits"]} ({row["observed_cost_count"]} observed; {row["unknown_cost_count"]} unknown)</td></tr>'
        for row in quality["routes"]
    ) or '<tr><td colspan="9" class="muted">No verified route-quality observations loaded.</td></tr>'

    strategy_rows = "".join(
        f'<tr><td><code>{escape(strategy_name)}</code></td>'
        f'<td><code>{escape(str(strategy_plan["selected_route_id"] or "NONE"))}</code></td>'
        f'<td><code>{escape(", ".join(strategy_plan["fallback_route_ids"]) or "NONE")}</code></td>'
        f'<td><code>{escape(str(strategy_plan["evidence_fresh_until"] or "NONE"))}</code></td></tr>'
        for strategy_name, strategy_plan in dashboard["strategy_plans"].items()
    ) or '<tr><td colspan="4" class="muted">No strategy comparison available.</td></tr>'

    configured_rows = "".join(
        f'<tr><td><code>{escape(route["route_id"])}</code></td>'
        f'<td><span class="pill">{"ENABLED" if route["enabled"] else "DISABLED"}</span></td>'
        f'<td>{escape(route["provider"])}</td>'
        f'<td><code>{escape(route["runtime"])}</code></td>'
        f'<td><code>{escape(route["usage_mode"])}</code></td>'
        f'<td><code>{escape(route["adapter"])}</code></td>'
        f'<td>{escape(", ".join(route["allowed_capabilities"]))}</td>'
        f'<td><code>{escape(str(route["ranks"]))}</code></td></tr>'
        for route in dashboard["configured_routes"]
    ) or '<tr><td colspan="8" class="muted">No approved route-set snapshot loaded.</td></tr>'

    eligible_rows = ""
    ineligible_rows = ""
    if isinstance(plan, dict):
        eligible_rows = "".join(
            f'<tr><td><code>{escape(item["route_id"])}</code></td><td>{escape(item["provider"])}</td>'
            f'<td><code>{escape(str(item["rank"]))}</code></td><td><code>{escape(str(item["fresh_until"]))}</code></td></tr>'
            for item in plan["eligible_routes"]
        )
        ineligible_rows = "".join(
            f'<tr><td><code>{escape(item["route_id"])}</code></td><td>{escape(item["provider"])}</td>'
            f'<td>{escape(", ".join(item["reasons"]))}</td></tr>'
            for item in plan["ineligible_routes"]
        )
    eligible_rows = eligible_rows or '<tr><td colspan="4" class="muted">No eligible routes.</td></tr>'
    ineligible_rows = ineligible_rows or '<tr><td colspan="3" class="muted">No ineligible routes.</td></tr>'

    plan_html = '<p class="muted">No provider capacity snapshot loaded; broker plan is UNKNOWN.</p>'
    if isinstance(plan, dict):
        plan_html = (
            '<dl>'
            f'<dt>Authority</dt><dd><span class="pill">{escape(plan["authority"])}</span></dd>'
            f'<dt>Required capability</dt><dd><code>{escape(plan["required_capability"])}</code></dd>'
            f'<dt>Strategy</dt><dd><code>{escape(plan["strategy"])}</code></dd>'
            f'<dt>Evaluated at</dt><dd><code>{escape(plan["evaluated_at"])}</code></dd>'
            f'<dt>Fresh until</dt><dd><code>{escape(str(plan["evidence_fresh_until"] or "NONE"))}</code></dd>'
            f'<dt>Selected</dt><dd><code>{escape(str(plan["selected_route_id"] or "NONE"))}</code></dd>'
            f'<dt>Fallbacks</dt><dd><code>{escape(", ".join(plan["fallback_route_ids"]) or "NONE")}</code></dd>'
            '</dl>'
        )

    route_options = []
    default_route = current_route or str(selected or "")
    for route in dashboard["routes"]:
        route_id = str(route["route_id"])
        selected_attr = " selected" if route_id == default_route else ""
        route_options.append(
            f'<option value="{escape(route_id, quote=True)}"{selected_attr}>{escape(route_id)}</option>'
        )
    reason_options = []
    for reason in dashboard["transition_failure_reasons"]:
        selected_attr = " selected" if reason == failure_reason else ""
        reason_options.append(
            f'<option value="{escape(reason, quote=True)}"{selected_attr}>{escape(reason)}</option>'
        )
    transition_form = (
        '<form method="get" action="/providers">'
        '<label>Current route <select name="current_route">'
        + "".join(route_options)
        + '</select></label> '
        '<label>Failure reason <select name="failure_reason"><option value="">Choose…</option>'
        + "".join(reason_options)
        + '</select></label> '
        f'<label>Max attempts <input name="max_attempts" type="number" min="1" max="32" value="{max_attempts}" style="width:80px"></label> '
        '<button type="submit">Preview failover</button></form>'
    )
    transition_html = '<p class="muted">Choose a failure reason to compute an advisory failover preview.</p>'
    if transition_error:
        transition_html = f'<p><span class="pill">UNAVAILABLE</span> <span class="muted">{escape(transition_error)}</span></p>'
    elif isinstance(transition, dict):
        transition_html = (
            '<dl>'
            f'<dt>Authority</dt><dd><span class="pill">{escape(str(transition["authority"]))}</span></dd>'
            f'<dt>Decision</dt><dd><span class="pill">{escape(str(transition["decision"]))}</span></dd>'
            f'<dt>Reason</dt><dd><code>{escape(str(transition["decision_reason"]))}</code></dd>'
            f'<dt>Failure</dt><dd><code>{escape(str(transition["failure_reason"]))}</code></dd>'
            f'<dt>From</dt><dd><code>{escape(str(transition["from_route_id"]))}</code></dd>'
            f'<dt>To</dt><dd><code>{escape(str(transition["to_route_id"] or "NONE"))}</code></dd>'
            f'<dt>Attempt</dt><dd>{transition["attempt"]}/{transition["max_attempts"]}</dd>'
            '</dl>'
        )

    body = (
        '<p><a href="/">← Projects</a></p><h1>Provider capacity</h1>'
        f'<p><span class="pill">{escape(str(dashboard["state"]))}</span> <span class="muted">{escape(str(dashboard["detail"]))}</span></p>'
        '<section class="card"><h2>Broker plan</h2>'
        + plan_html
        + '<p class="muted">ADVISORY_ONLY. This page cannot invoke providers, switch routes, commit transitions, or use credentials.</p></section>'
        '<section class="card" style="margin-top:16px"><h2>Failover preview</h2>'
        + transition_form
        + transition_html
        + '<p class="muted">Preview only. Effect authorization, sealed request, and one-shot commit remain separate authority boundaries.</p></section>'
        '<section class="card" style="margin-top:16px"><h2>Verified route outcomes</h2>'
        f'<p><span class="pill">{escape(str(quality["state"]))}</span> '
        f'<span class="pill">{escape(str(quality["authority"]))}</span> '
        f'<span class="muted">{escape(str(quality["detail"]))}</span></p>'
        '<div style="overflow:auto"><table><thead><tr><th>Route</th><th>Provider/runtime</th><th>Samples</th><th>PASS</th><th>First pass</th><th>Rework / findings</th><th>Owner / quota</th><th>Avg wall ms</th><th>Observed cost milliunits</th></tr></thead>'
        f'<tbody>{quality_rows}</tbody></table></div>'
        '<p class="muted">EVIDENCE_ONLY / broker influence NONE. These measurements never change eligibility, ranking or route selection.</p></section>'
        '<section class="card" style="margin-top:16px"><h2>Strategy comparison</h2>'
        '<div style="overflow:auto"><table><thead><tr><th>Strategy</th><th>Selected</th><th>Fallbacks</th><th>Fresh until</th></tr></thead>'
        f'<tbody>{strategy_rows}</tbody></table></div>'
        '<p class="muted">All strategies are advisory plans over the same validated candidates; no route is executed by this comparison.</p></section>'
        '<section class="card" style="margin-top:16px"><h2>Approved route configuration</h2>'
        f'<p><span class="pill">{escape(str(dashboard["configured_route_authority"]))}</span></p>'
        '<div style="overflow:auto"><table><thead><tr><th>Route</th><th>State</th><th>Provider</th><th>Runtime</th><th>Usage</th><th>Adapter</th><th>Capabilities</th><th>Ranks</th></tr></thead>'
        f'<tbody>{configured_rows}</tbody></table></div></section>'
        '<h2>Observed route candidates</h2><section class="grid">'
        + ("".join(route_cards) if route_cards else '<article class="card"><p>No validated provider routes loaded.</p></article>')
        + '</section>'
        '<section class="card" style="margin-top:16px"><h2>Eligible routes</h2>'
        '<div style="overflow:auto"><table><thead><tr><th>Route</th><th>Provider</th><th>Rank</th><th>Fresh until</th></tr></thead>'
        f'<tbody>{eligible_rows}</tbody></table></div></section>'
        '<section class="card" style="margin-top:16px"><h2>Ineligible routes</h2>'
        '<div style="overflow:auto"><table><thead><tr><th>Route</th><th>Provider</th><th>Reasons</th></tr></thead>'
        f'<tbody>{ineligible_rows}</tbody></table></div></section>'
    )
    return UiResponse("200 OK", _page("Provider capacity", body))


def render_operations(service: AtlasService) -> UiResponse:
    try:
        readiness = service.operations_readiness()
    except ValidationError:
        return _error("500 Internal Server Error", "Operations readiness unavailable", "Operations/release metadata could not be read safely.")

    commands = readiness["operations"]["commands"]
    command_rows = "".join(
        f'<tr><td>{escape(name.replace("_", " ").title())}</td>'
        f'<td><span class="pill">{escape(str(item["state"]))}</span></td>'
        f'<td><code>{escape(str(item["command"] or "not configured"))}</code></td></tr>'
        for name, item in commands.items()
    )
    runbook_rows = "".join(
        f'<tr><td><code>{escape(str(item["path"]))}</code></td>'
        f'<td><span class="pill">{escape(str(item["state"]))}</span></td></tr>'
        for item in readiness["operations"]["runbooks"]
    ) or '<tr><td colspan="2" class="muted">No runbooks declared.</td></tr>'

    gate_rows = "".join(
        f'<tr><td>{escape(name.replace("_", " ").title())}</td>'
        f'<td><span class="pill">{escape(str(item["state"]))}</span></td>'
        f'<td>{str(bool(item.get("required"))).lower()}</td>'
        f'<td>{str(bool(item.get("configured"))).lower()}</td>'
        f'<td><span class="pill">{escape(str(item.get("execution", "UNKNOWN")))}</span></td></tr>'
        for name, item in readiness["release"]["gates"].items()
    )
    unit_rows = "".join(
        f'<tr><td><code>{escape(str(item["unit"]))}</code></td>'
        f'<td><span class="pill">{escape(str(item["state"]))}</span></td></tr>'
        for item in readiness["deployment"]["units"]
    )
    blocker_rows = "".join(
        f'<tr><td>{escape(str(name).upper())}</td><td><code>{str(bool(value)).lower()}</code></td></tr>'
        for name, value in readiness["release"]["blockers"].items()
    )
    contract = readiness["deployment"]["contract"]
    dependency_rows = "".join(
        f'<tr><td><code>{escape(str(value))}</code></td></tr>'
        for value in readiness["dependencies"]["declared_dependencies"]
    ) or '<tr><td class="muted">No declared runtime dependencies found.</td></tr>'

    body = (
        '<p><a href="/">← Projects</a></p><h1>Operations readiness</h1>'
        f'<section class="grid"><article class="card"><h2>{escape(str(readiness["state"]))}</h2><p>Deployment profile</p></article>'
        f'<article class="card"><h2>{escape(str(readiness["runtime"]["state"]))}</h2><p>Current data-root runtime</p></article>'
        f'<article class="card"><h2>{escape(str(readiness["operations"]["runbook_state"]))}</h2><p>Runbooks</p></article>'
        f'<article class="card"><h2>{escape(str(readiness["deployment"]["state"]))}</h2><p>Deployment contract</p></article>'
        f'<article class="card"><h2>{escape(str(readiness["security_controls"]["state"]))}</h2><p>Security controls</p></article>'
        f'<article class="card"><h2>{escape(str(readiness["release_readiness"]["state"]))}</h2><p>Release readiness</p></article></section>'
        '<section class="card" style="margin-top:16px"><h2>Production claim boundary</h2><dl>'
        f'<dt>production_oriented</dt><dd><code>{str(bool(readiness["production_oriented"])).lower()}</code></dd>'
        f'<dt>Runtime</dt><dd><span class="pill">{escape(str(readiness["runtime"]["state"]))}</span></dd>'
        f'<dt>Configuration ready</dt><dd><code>{str(bool(readiness["operations"]["configuration_ready"])).lower()}</code></dd>'
        f'<dt>Release readiness</dt><dd><span class="pill">{escape(str(readiness["release_readiness"]["state"]))}</span> '
        f'<span class="muted">{escape(str(readiness["release_readiness"]["detail"]))}</span></dd>'
        f'<dt>Security controls</dt><dd><span class="pill">{escape(str(readiness["security_controls"]["state"]))}</span> '
        f'<span class="muted">{escape(str(readiness["security_controls"]["detail"]))}</span></dd>'
        f'<dt>Security review</dt><dd><span class="pill">{escape(str(readiness["security_review"]["state"]))}</span> '
        f'<span class="muted">{escape(str(readiness["security_review"]["detail"]))}</span></dd></dl></section>'
        '<section class="card" style="margin-top:16px"><h2>prod-atlas deployment contract</h2><dl>'
        f'<dt>Status</dt><dd><span class="pill">{escape(str(contract["status"]))}</span></dd>'
        f'<dt>Hostname</dt><dd><code>{escape(str(contract["hostname"]))}</code></dd>'
        f'<dt>MCP DNS</dt><dd><code>{escape(str(contract["mcp_dns"]))}</code></dd>'
        f'<dt>Resource URL</dt><dd><code>{escape(str(contract["resource_url"]))}</code></dd>'
        f'<dt>Ingress</dt><dd><code>{escape(str(contract["ingress_listen"]))}</code> → <code>{escape(str(contract["ingress_target"]))}</code></dd>'
        f'<dt>Production evidence</dt><dd><code>{str(bool(contract["production_evidence"])).lower()}</code></dd></dl>'
        '<div style="overflow:auto"><table><thead><tr><th>Systemd unit</th><th>Repository contract state</th></tr></thead>'
        f'<tbody>{unit_rows}</tbody></table></div></section>'
        '<section class="card" style="margin-top:16px"><h2>Operations commands</h2>'
        '<div style="overflow:auto"><table><thead><tr><th>Capability</th><th>State</th><th>Configured command</th></tr></thead>'
        f'<tbody>{command_rows}</tbody></table></div></section>'
        '<section class="card" style="margin-top:16px"><h2>Runbooks</h2>'
        '<div style="overflow:auto"><table><thead><tr><th>Path</th><th>State</th></tr></thead>'
        f'<tbody>{runbook_rows}</tbody></table></div></section>'
        '<section class="card" style="margin-top:16px"><h2>Dependency / supply-chain visibility</h2><dl>'
        f'<dt>Dependency declaration</dt><dd><span class="pill">{escape(str(readiness["dependencies"]["state"]))}</span> <code>{escape(str(readiness["dependencies"]["requirements_path"]))}</code></dd>'
        f'<dt>Declared dependencies</dt><dd>{escape(str(readiness["dependencies"]["declared_dependency_count"]))}</dd>'
        f'<dt>Third-party record</dt><dd><span class="pill">{escape(str(readiness["dependencies"]["third_party_state"]))}</span> <code>{escape(str(readiness["dependencies"]["third_party_path"]))}</code></dd>'
        f'<dt>SBOM</dt><dd><span class="pill">{escape(str(readiness["dependencies"]["sbom_state"]))}</span> <span class="muted">{escape(str(readiness["dependencies"]["detail"]))}</span></dd></dl>'
        '<div style="overflow:auto"><table><thead><tr><th>Declared runtime dependency</th></tr></thead>'
        f'<tbody>{dependency_rows}</tbody></table></div></section>'
        '<section class="card" style="margin-top:16px"><h2>Release gates</h2>'
        '<div style="overflow:auto"><table><thead><tr><th>Gate</th><th>Configuration state</th><th>Required</th><th>Configured</th><th>Execution</th></tr></thead>'
        f'<tbody>{gate_rows}</tbody></table></div></section>'
        '<section class="card" style="margin-top:16px"><h2>Release blockers</h2>'
        '<div style="overflow:auto"><table><thead><tr><th>Class</th><th>Blocks release</th></tr></thead>'
        f'<tbody>{blocker_rows}</tbody></table></div>'
        f'<p class="muted">Full E2E passes configured: <code>{escape(str(readiness["release"]["full_e2e_passes"]))}</code>; exact HEAD required: <code>{str(bool(readiness["release"]["exact_head_required"])).lower()}</code>.</p></section>'
        '<p class="muted">This page is observational only. It never executes backup, restore, upgrade, rollback, smoke, E2E, SBOM, provenance, or security-review actions.</p>'
    )
    return UiResponse("200 OK", _page("Operations readiness", body))


def render_cross_project_search(
    service: AtlasService,
    query: str,
    source_class: str = "all",
) -> UiResponse:
    normalized_class = source_class.strip().lower() or "all"
    if normalized_class not in {"all", "engineering", "personal"}:
        return _error(
            "400 Bad Request",
            "Invalid search",
            "source_class must be all, engineering, or personal.",
        )
    selected = {
        value: (" selected" if normalized_class == value else "")
        for value in ("all", "engineering", "personal")
    }
    body = '<p><a href="/">← Projects</a></p><h1>Cross-project search</h1>'
    body += (
        f'<form method="get" action="/search"><input name="q" maxlength="{_MAX_QUERY}" '
        f'value="{escape(query, quote=True)}" placeholder="Search all registered projects">'
        '<select name="source_class">'
        f'<option value="all"{selected["all"]}>All sources</option>'
        f'<option value="engineering"{selected["engineering"]}>Engineering only</option>'
        f'<option value="personal"{selected["personal"]}>Personal/reference only</option>'
        '</select><button type="submit">Search</button></form>'
    )
    if query:
        project_ids = [
            project.project_id
            for project in service.list_projects()
            if project.enabled
        ]
        if not project_ids:
            body += '<p class="muted">No enabled projects.</p>'
            return UiResponse("200 OK", _page("Cross-project search", body))
        try:
            result = service.search_across_projects(
                query,
                project_ids=project_ids,
                source_class=normalized_class,
                limit_per_project=5,
            )
        except ValidationError:
            return _error(
                "500 Internal Server Error",
                "Search unavailable",
                "Validated search state could not be read safely.",
            )
        for group in result["groups"]:
            if not group["hits"]:
                continue
            body += (
                f'<section class="card hit"><h2><a href="/projects/{quote(str(group["project_id"]), safe="")}">'
                f'{escape(str(group["display_name"]))}</a></h2>'
            )
            for hit in group["hits"]:
                provenance = hit["provenance"]
                relation = _source_relation(provenance)
                source_url = _source_detail_url(str(group["project_id"]), str(hit["identity"]))
                title = escape(str(hit["title"] or hit["path"]))
                title_html = f'<a href="{source_url}">{title}</a>' if source_url else title
                body += (
                    f'<article><h3>{title_html}</h3><p class="snippet">{escape(str(hit["content"] or ""))}</p>'
                    f'<p><span class="pill">DERIVED</span> <span class="pill">{escape(relation)}</span></p>'
                    f'<p class="muted"><code>{escape(str(provenance.get("repository", "")))} · '
                    f'{escape(str(provenance.get("ref", "")))} · '
                    f'{escape(str(provenance.get("source_path", "")))} · '
                    f'{escape(str(provenance.get("source_revision", "")))}</code></p>'
                    f'<p class="muted">Projection <code>{escape(str(hit["identity"]))}</code></p></article>'
                )
            body += "</section>"
        counts = result["class_counts"]
        body += (
            f'<p class="muted">{result["total"]} attributable result(s) across enabled projects · '
            f'engineering {counts["engineering"]} · personal {counts["personal"]} · '
            f'filter {escape(normalized_class)}.</p>'
        )
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


def render_decision_detail(service: AtlasService, decision_id: str) -> UiResponse:
    try:
        detail = service.decision_detail(decision_id)
    except ValidationError as exc:
        if str(exc).startswith("unknown decision_id:") or str(exc) == "invalid decision_id":
            return _error("404 Not Found", "Decision not found", "The requested ADR is not present in validated derived intelligence.")
        return _error("500 Internal Server Error", "Decision unavailable", "Decision intelligence could not be built safely.")

    target_html = []
    for target in detail["targets"]:
        prov = target["provenance"]
        atlas_link = _projection_source_link(
            str(target["project_id"]),
            str(target["source_identity"]),
        )
        canonical_link = _canonical_source_link(prov)
        nav = " · ".join(link for link in (atlas_link, canonical_link) if link)
        target_html.append(
            '<article class="hit">'
            f'<strong>{escape(str(target["project_id"]))}</strong>'
            f'<p><code>{escape(str(prov.get("repository", "")))} · {escape(str(prov.get("source_path", "")))}</code></p>'
            f'<p class="muted"><code>{escape(str(prov.get("source_revision", "")))}</code>'
            + (f' · {nav}' if nav else "")
            + '</p></article>'
        )
    backlink_html = []
    for item in detail["backlinks"]:
        prov = item["provenance"]
        atlas_link = _projection_source_link(
            str(item["source_project_id"]),
            str(item["source_identity"]),
        )
        canonical_link = _canonical_source_link(prov)
        nav = " · ".join(link for link in (atlas_link, canonical_link) if link)
        backlink_html.append(
            '<article class="hit">'
            f'<strong>{escape(str(item["source_project_id"]))}</strong>'
            f'<p><code>{escape(str(prov.get("repository", "")))} · {escape(str(prov.get("source_path", "")))}</code></p>'
            f'<p class="muted">Projection <code>{escape(str(item["source_identity"]))}</code>'
            + (f' · {nav}' if nav else "")
            + '</p></article>'
        )
    body = (
        '<p><a href="/intelligence">← Derived intelligence</a></p>'
        f'<h1>{escape(str(detail["decision_id"]))}</h1>'
        '<p class="muted"><span class="pill">DERIVED</span> ADR target/backlink navigation; canonical repository artifacts remain authoritative.</p>'
        '<section class="card"><h2>Decision targets</h2>'
        + ("".join(target_html) if target_html else '<p class="muted">No validated ADR target projection is registered.</p>')
        + '</section><section class="card" style="margin-top:16px"><h2>Backlinks</h2>'
        + ("".join(backlink_html) if backlink_html else '<p class="muted">No explicit backlinks.</p>')
        + '</section>'
    )
    return UiResponse("200 OK", _page(str(detail["decision_id"]), body))


def render_source_detail(service: AtlasService, project_id: str, source_id: str) -> UiResponse:
    try:
        project = service.registry.get(project_id)
        detail = service.source_detail(project_id, source_id)
    except ValidationError as exc:
        message = str(exc)
        if message.startswith("unknown project_id:") or message.startswith("unknown source_id:"):
            return _error("404 Not Found", "Source not found", "The requested project source is not registered.")
        return _error("500 Internal Server Error", "Source unavailable", "Source projection state could not be read safely.")

    source = detail["source"]
    projection = detail["projection"]
    prov = projection.get("provenance") or {}
    source_link = _canonical_source_link(prov) if prov else ""
    link_html = f' · {source_link}' if source_link else ""
    body = (
        f'<p><a href="/projects/{quote(project.project_id, safe="")}">← {escape(project.display_name)}</a></p>'
        f'<h1>{escape(str(source["title"] or source["source_id"]))}</h1>'
        '<section class="grid"><article class="card"><h2>Registered source</h2><dl>'
        f'<dt>Source ID</dt><dd><code>{escape(str(source["source_id"]))}</code></dd>'
        f'<dt>Provider</dt><dd><code>{escape(str(source["provider"]))}</code></dd>'
        f'<dt>Class</dt><dd><span class="pill">{escape(str(source["source_class"]))}</span></dd>'
        f'<dt>Path</dt><dd><code>{escape(str(source["source_path"]))}</code></dd>'
        f'<dt>Ref</dt><dd><code>{escape(str(source["ref"]))}</code></dd>'
        f'<dt>Enabled</dt><dd>{str(bool(source["enabled"])).lower()}</dd>'
        '</dl></article><article class="card"><h2>Projection</h2><dl>'
        f'<dt>State</dt><dd><span class="pill">{escape(str(projection["state"]))}</span></dd>'
        f'<dt>Identity</dt><dd><code>{escape(str(projection["identity"] or "UNKNOWN"))}</code></dd>'
        f'<dt>Revision</dt><dd><code>{escape(str(projection["source_revision"] or "UNKNOWN"))}</code>{link_html}</dd>'
        f'<dt>Digest</dt><dd><code>{escape(str(projection["content_digest"] or "UNKNOWN"))}</code></dd>'
        f'<dt>Fetched</dt><dd><code>{escape(str(projection["fetched_at"] or "UNKNOWN"))}</code></dd>'
        '</dl></article></section>'
    )
    if prov:
        body += (
            '<section class="card" style="margin-top:16px"><h2>Validated provenance</h2><dl>'
            f'<dt>Repository</dt><dd><code>{escape(str(prov.get("repository", "")))}</code></dd>'
            f'<dt>Ref</dt><dd><code>{escape(str(prov.get("ref", "")))}</code></dd>'
            f'<dt>Source path</dt><dd><code>{escape(str(prov.get("source_path", "")))}</code></dd>'
            f'<dt>Source revision</dt><dd><code>{escape(str(prov.get("source_revision", "")))}</code></dd>'
            f'<dt>Authority</dt><dd><span class="pill">{escape(_source_relation(prov))}</span></dd>'
            '</dl></section>'
        )
    if projection.get("body") is not None:
        truncation = " · content truncated for UI safety" if projection.get("body_truncated") else ""
        body += (
            '<section class="card" style="margin-top:16px"><h2>Derived projection content</h2>'
            f'<p class="muted">Read-only projection; canonical source remains authoritative{escape(truncation)}.</p>'
            f'<pre>{escape(str(projection["body"]))}</pre></section>'
        )
    else:
        body += '<section class="card" style="margin-top:16px"><h2>Derived projection content</h2><p class="muted">No validated current projection content is available.</p></section>'
    return UiResponse("200 OK", _page(f"{project.display_name} source", body))


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

    decision_rows = []
    for adr, targets in intelligence["decision_targets"].items():
        backlinks = intelligence["decision_backlinks"].get(adr, [])
        target_links = []
        for target in targets:
            atlas_link = _projection_source_link(
                str(target["project_id"]),
                str(target["source_identity"]),
            )
            canonical_link = _canonical_source_link(target["provenance"])
            nav = " · ".join(link for link in (atlas_link, canonical_link) if link)
            target_links.append(
                f'<code>{escape(str(target["project_id"]))}/{escape(str(target["source_identity"]))}</code>'
                + (f' · {nav}' if nav else "")
            )
        target_html = "<br>".join(target_links) if target_links else '<span class="muted">target not projected</span>'
        decision_href = f'/decisions/{quote(adr, safe="")}'
        decision_rows.append(
            f'<tr><td><a href="{decision_href}"><strong>{escape(adr)}</strong></a></td><td>{target_html}</td><td>{len(backlinks)}</td></tr>'
        )
    decisions = "".join(decision_rows) if decision_rows else '<tr><td colspan="3" class="muted">No ADR targets found in validated projections.</td></tr>'

    concept_rows = []
    for concept, occurrences in intelligence["concept_index"].items():
        source_links = []
        for occurrence in occurrences:
            atlas_link = _projection_source_link(
                occurrence["source_project_id"],
                occurrence["source_identity"],
            )
            source_links.append(
                atlas_link
                or f'<code>{escape(occurrence["source_project_id"])}/{escape(occurrence["source_identity"])}</code>'
            )
        concept_rows.append(
            f'<tr><td>{escape(concept)}</td><td>{len(occurrences)}</td><td>{"<br>".join(source_links)}</td></tr>'
        )
    concepts_html = "".join(concept_rows) if concept_rows else '<tr><td colspan="3" class="muted">No explicit heading concepts.</td></tr>'

    repository_rows = []
    for repository, occurrences in intelligence["entities"]["repositories"].items():
        projects = sorted({row["project_id"] for row in occurrences})
        source_paths = sorted({row["source_path"] for row in occurrences})
        repository_rows.append(
            f'<tr><td><code>{escape(repository)}</code></td>'
            f'<td>{", ".join(f"<code>{escape(project)}</code>" for project in projects)}</td>'
            f'<td>{len(source_paths)}</td></tr>'
        )
    repositories_html = "".join(repository_rows) if repository_rows else '<tr><td colspan="3" class="muted">No engineering repository entities.</td></tr>'

    source_entity_rows = []
    for source_path, occurrences in intelligence["entities"]["source_paths"].items():
        nav = []
        for occurrence in occurrences:
            link = _projection_source_link(
                occurrence["project_id"],
                occurrence["source_identity"],
            )
            nav.append(
                link
                or f'<code>{escape(occurrence["project_id"])}/{escape(occurrence["source_identity"])}</code>'
            )
        classes = sorted({row["source_class"] for row in occurrences})
        source_entity_rows.append(
            f'<tr><td><code>{escape(source_path)}</code></td>'
            f'<td>{", ".join(escape(value) for value in classes)}</td>'
            f'<td>{"<br>".join(nav)}</td></tr>'
        )
    source_entities_html = "".join(source_entity_rows) if source_entity_rows else '<tr><td colspan="3" class="muted">No validated source-path entities.</td></tr>'

    question_rows = []
    for project_id, questions in intelligence["unanswered_questions"].items():
        for question in questions:
            atlas_link = _projection_source_link(project_id, question["source_identity"])
            question_rows.append(
                f'<tr><td><code>{escape(project_id)}</code></td><td>{escape(question["value"])}</td>'
                f'<td>{atlas_link or f"<code>{escape(question["source_identity"])}</code>"}</td></tr>'
            )
    questions_html = "".join(question_rows) if question_rows else '<tr><td colspan="3" class="muted">No explicit QUESTION/TODO markers.</td></tr>'

    gap_rows = []
    for gap in intelligence["knowledge_gaps"]:
        gap_rows.append(
            f'<tr><td><code>{escape(gap["project_id"])}</code></td><td><code>{escape(gap["source_id"])}</code></td>'
            f'<td><code>{escape(gap["source_path"])}</code></td><td><span class="pill">{escape(gap["sync_state"].upper())}</span></td></tr>'
        )
    gaps_html = "".join(gap_rows) if gap_rows else '<tr><td colspan="4" class="muted">No enabled configured-source gaps.</td></tr>'

    body = (
        '<p><a href="/">← Projects</a></p><h1>Derived intelligence</h1>'
        '<p class="muted"><span class="pill">DERIVED</span> Cross-project synthesis is rebuildable and never canonical authority.</p>'
        f'<section class="card"><h2>Summary</h2><span class="pill">{escape(str(intelligence["summary"]["state"]))}</span> '
        f'<span class="muted">{escape(str(intelligence["summary"]["detail"]))}</span></section>'
        '<section class="grid" style="margin-top:16px">'
        f'<article class="card"><h2>{len(intelligence["entities"]["repositories"])}</h2><p>Repository entities</p></article>'
        f'<article class="card"><h2>{len(intelligence["entities"]["source_paths"])}</h2><p>Source-path entities</p></article>'
        f'<article class="card"><h2>{len(intelligence["entities"]["decisions"])}</h2><p>Decision entities</p></article>'
        '</section><h2>Projects</h2><section class="grid">' + "".join(project_cards) + '</section>'
        '<section class="card" style="margin-top:16px"><h2>Concept index</h2>'
        '<div style="overflow:auto"><table><thead><tr><th>Concept</th><th>Occurrences</th><th>Sources</th></tr></thead>'
        f'<tbody>{concepts_html}</tbody></table></div></section>'
        '<section class="card" style="margin-top:16px"><h2>Repository entities</h2>'
        '<div style="overflow:auto"><table><thead><tr><th>Repository</th><th>Projects</th><th>Source paths</th></tr></thead>'
        f'<tbody>{repositories_html}</tbody></table></div></section>'
        '<section class="card" style="margin-top:16px"><h2>Source-path entities</h2>'
        '<div style="overflow:auto"><table><thead><tr><th>Source path</th><th>Class</th><th>Projection</th></tr></thead>'
        f'<tbody>{source_entities_html}</tbody></table></div></section>'
        '<section class="card" style="margin-top:16px"><h2>Cross-project link graph</h2>'
        '<div style="overflow:auto"><table><thead><tr><th>Project</th><th>Source repository</th><th>Target repository</th><th>Source path</th></tr></thead>'
        f'<tbody>{links}</tbody></table></div></section>'
        '<section class="card" style="margin-top:16px"><h2>Decision index</h2>'
        '<div style="overflow:auto"><table><thead><tr><th>ADR</th><th>Validated target</th><th>Backlinks</th></tr></thead>'
        f'<tbody>{decisions}</tbody></table></div></section>'
        '<section class="card" style="margin-top:16px"><h2>Unanswered questions</h2>'
        '<div style="overflow:auto"><table><thead><tr><th>Project</th><th>Question</th><th>Source</th></tr></thead>'
        f'<tbody>{questions_html}</tbody></table></div></section>'
        '<section class="card" style="margin-top:16px"><h2>Knowledge gaps</h2>'
        '<div style="overflow:auto"><table><thead><tr><th>Project</th><th>Source</th><th>Path</th><th>State</th></tr></thead>'
        f'<tbody>{gaps_html}</tbody></table></div></section>'
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
            atlas_link = _projection_source_link(
                str(item["source_project_id"]),
                str(item["source_identity"]),
            )
            nav = " · ".join(link for link in (atlas_link, source_link) if link)
            source_nav = f" · {nav}" if nav else ""
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
                atlas_link = _projection_source_link(
                    str(target["project_id"]),
                    str(target["source_identity"]),
                )
                nav = " · ".join(value for value in (atlas_link, link) if value)
                target_html.append(
                    f'<li><code>{escape(str(target["project_id"]))}/{escape(str(target["source_identity"]))}</code>'
                    + (f' · {nav}' if nav else "")
                    + '</li>'
                )
            refs = ", ".join(
                f'{escape(str(row["source_project_id"]))}/{escape(str(row["source_identity"]))}'
                for row in backlinks
            )
            target_block = "<ul>" + "".join(target_html) + "</ul>" if target_html else '<p class="muted">Referenced ADR target is not present in validated projections.</p>'
            decision_href = f'/decisions/{quote(adr, safe="")}'
            out.append(
                f'<article class="hit"><strong><a href="{decision_href}">{escape(adr)}</a></strong>'
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
        f'<section class="card"><h2>Summary</h2><span class="pill">{escape(str(intelligence["summary"]["state"]))}</span> '
        f'<span class="muted">{escape(str(intelligence["summary"]["detail"]))}</span></section>'
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
        source_url = f'/projects/{pid}/sources/{quote(source_id, safe="")}'
        body += f'<dt><a href="{source_url}">{escape(source.title or source_id)}</a></dt><dd><span class="pill">{escape(sync_state)}</span> <span class="pill">{source_class}</span> <code>{escape(source.source_path)}</code></dd>'
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
            atlas_link = _projection_source_link(project_id, hit.identity)
            canonical_link = _canonical_source_link(prov)
            nav = " · ".join(link for link in (atlas_link, canonical_link) if link)
            body += (
                f'<article class="hit"><h3>{escape(hit.title or hit.path)}</h3>'
                f'<p class="snippet">{escape(hit.content or "")}</p><dl>'
                f'<dt>Source</dt><dd><code>{escape(str(prov.get("repository", "")))} · {escape(str(prov.get("source_path", "")))}</code>'
                + (f' · {nav}' if nav else "")
                + '</dd>'
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
            elif path == "/operations":
                response = render_operations(service)
            elif path == "/concurrency":
                response = render_concurrency(service)
            elif path == "/personal":
                params = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
                query = params.get("q", [""])[0]
                project_id = params.get("project", [""])[0].strip()
                if len(query) > _MAX_QUERY or len(project_id) > 128:
                    response = _error("400 Bad Request", "Invalid personal search", "Personal search input is too long.")
                else:
                    response = render_personal_knowledge(service, query.strip(), project_id)
            elif path == "/instruction-governance":
                response = render_instruction_governance(service)
            elif path == "/decision-plane":
                params = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
                optional_text = params.get("optional_paths", [""])[0].strip()
                changed_text = params.get("changed_paths", [""])[0].strip()
                if len(optional_text) > 4096 or len(changed_text) > 4096:
                    response = _error("400 Bad Request", "Invalid Decision Plane input", "Candidate input is too long.")
                else:
                    optional_paths = [item.strip() for item in optional_text.split(",") if item.strip()]
                    changed_paths = [item.strip() for item in changed_text.split(",") if item.strip()]
                    if len(optional_paths) > 128 or len(changed_paths) > 256:
                        response = _error("400 Bad Request", "Invalid Decision Plane input", "Too many candidate paths.")
                    else:
                        response = render_decision_plane(
                            service,
                            optional_paths=optional_paths,
                            changed_paths=changed_paths,
                        )
            elif path == "/providers":
                params = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
                current_route = params.get("current_route", [""])[0].strip()
                failure_reason = params.get("failure_reason", [""])[0].strip()
                max_attempts_text = params.get("max_attempts", ["3"])[0].strip() or "3"
                if len(current_route) > 64 or len(failure_reason) > 64 or len(max_attempts_text) > 2:
                    response = _error("400 Bad Request", "Invalid provider preview", "Provider preview input is invalid.")
                else:
                    try:
                        max_attempts = int(max_attempts_text)
                    except ValueError:
                        response = _error("400 Bad Request", "Invalid provider preview", "max_attempts must be an integer.")
                    else:
                        if not 1 <= max_attempts <= 32:
                            response = _error("400 Bad Request", "Invalid provider preview", "max_attempts must be 1-32.")
                        else:
                            response = render_providers(
                                service,
                                current_route=current_route,
                                failure_reason=failure_reason,
                                max_attempts=max_attempts,
                            )
            elif path == "/search":
                params = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
                query = params.get("q", [""])[0]
                source_class = params.get("source_class", ["all"])[0]
                response = (
                    _error("400 Bad Request", "Invalid search", "Search query is too long.")
                    if len(query) > _MAX_QUERY
                    else render_cross_project_search(service, query.strip(), source_class)
                )
            elif path.startswith("/decisions/") and "/" not in path[len("/decisions/"):]:
                response = render_decision_detail(service, unquote(path[len("/decisions/"):]))
            elif path.startswith("/projects/") and path.endswith("/lifecycle") and "/" not in path[len("/projects/"):-len("/lifecycle")]:
                project_id = unquote(path[len("/projects/"):-len("/lifecycle")])
                response = render_lifecycle(service, project_id)
            elif path.startswith("/projects/") and path.endswith("/intelligence") and "/" not in path[len("/projects/"):-len("/intelligence")]:
                project_id = unquote(path[len("/projects/"):-len("/intelligence")])
                response = render_intelligence(service, project_id)
            elif path.startswith("/projects/") and "/sources/" in path:
                remainder = path[len("/projects/"):]
                project_part, separator, source_part = remainder.partition("/sources/")
                if separator and project_part and source_part and "/" not in project_part and "/" not in source_part:
                    response = render_source_detail(service, unquote(project_part), unquote(source_part))
                else:
                    response = _error("404 Not Found", "Not found", "The requested UI route does not exist.")
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
