# Private ChatGPT Atlas usage guidance

The maintained `use-atlas` skill is in [skills/use-atlas/SKILL.md](skills/use-atlas/SKILL.md).
The organization-level pointer is Engineering System
[adapters/datarelay.md](https://github.com/datarelay-labs/engineering-system/blob/main/adapters/datarelay.md).
Universal Engineering System rules, managed adoption templates, other product
worktrees, the production service and the working OAuth connection are unchanged.

## Scope and distribution

This directory versions the existing private `datarelay-atlas` package. Version
0.1.2 updates instructions/metadata only. Both existing MCP JSON configurations
are preserved from the owned v0.1.1 package. The update must target the same owned
plugin, keep its private audience, and use the freshly observed release ID as a
compare-and-swap guard. Do not create a replacement plugin or reinstall/reconnect
OAuth for a skill-only update. Update both identity manifests together.

Package these five paths only: `plugin.json`, `.codex-plugin/plugin.json`,
`mcp.json`, `.mcp.json`, `skills/use-atlas/SKILL.md`. The README and test scenarios
are repository documentation, not always-loaded skill instructions. Read back
published files and compare their contents after an update.

Use an already connected native Atlas tool when it is exposed in the conversation.
This package is not evidence that the authenticated connection is mounted in every
chat. A future registered-app mapping needs the actual `plugin_asdk_app...` ID;
never invent one, replace it with this package's ID, or claim `.app.json` wiring
was changed by this update. The existing connection's ID was not exposed by the
plugin discovery surface used during this change, so transport wiring is retained.
The skill may be used alongside the separate working Atlas connection. A published
instruction update is not proof of automatic activation in another existing chat.

## Focused acceptance cases

These are instruction-review scenarios, not synthetic model or production PASS.
Static tests check packaging and retained guardrails, not a model's future choices.
For runtime evidence record the real prompt/tool/outcome and do not add artificial
memory-effectiveness observations just to raise the sample count.

| Case | Expected behavior |
| --- | --- |
| `DRLink 계속`, prior decision missing | Bind canonical repository facts; use one scoped bootstrap if ID is unknown, then continue the actual work. No owner magic phrase. |
| `계속`, sufficient current context | Reuse it; no duplicate Atlas call merely because a turn or commit occurred. |
| Direct Atlas project-list question | Call the available native list tool; no unrelated Work Packet prerequisite. |
| Already verified Atlas product ID | Use `get_task_context(project_id="datarelay-atlas")`; never combine it with repository. |
| Repository matches Atlas and Personal Knowledge | Resolve explicit project identity; never choose the first result or change the owner-bound repository. |
| Stale projection or `currentness.state=UNKNOWN` | Keep canonical HEAD/Issue facts; do not infer a failed gate or completed roadmap. Continue implementation. |
| Timeout, auth failure or unavailable tool | Report limitation once and continue canonical/local work. No retry loop or credential/permission mutation. |
| Explicit MCP-only request with unavailable tool | Report no native result; do not substitute SSH/CLI or invent data. |
| Unrelated translation or current context answers question | No Atlas retrieval. |
| Memory/measurement persistence request | Read-only tool is not a writer; do not copy transcripts or invent observations. |

Manual review also checks a retrieved instruction cannot replace owner direction,
change repository/approvals or select another runtime. Cross-project reads remain
limited to the explicitly requested dependency, not an organization-wide sweep.

## Verification and rollback

Run `python3 -m unittest tests.test_chatgpt_plugin -v` at repository root.
Use existing task-context regressions for project binding/fallback contracts when
affected. A live scoped MCP call verifies connectivity only; it is not statistical
proof of memory quality or universal automatic skill activation.

For instruction rollback, restore reviewed prior skill/metadata content in a new
package version and use the current release guard. Leave credentials, role mappings,
scopes and the working connection untouched.

Authoring references (reviewed 2026-10-07): OpenAI
[Build skills](https://developers.openai.com/plugins/build/skills) and
[Package your plugin](https://developers.openai.com/plugins/build/plugins).
