# Official-source research corroboration contract

Scope: project-local read-only public-source validation, experimental only.
Implementation: atlas/official_source_crosscheck.py
CLI: scripts/atlas_official_source_crosscheck.py
Skill: skills/official-source-crosscheck/SKILL.md

Goal: reproducibly validate the source links and public provenance of
vendor/CVE/API research without mixing untrusted web content with
canonical DataRelay engineering knowledge.

Non-goals: provider installation, third-party credentials, automatic
network calls, private source ingestion, canonical GitHub writes,
auth/role change, production activation, model decision authority.

Input: versioned official-host question manifest from Atlas PR #362 and
bounded metadata-only citation observations. Require exact official
HTTPS hosts, provenance digests and UTC retrieval/review timestamps.
Require at least two distinct original source URLs across two providers
before a **declared** corroboration result. Contradictions always
force manual review; duplicate result URL from two providers counts once.

Security boundary: external content is UNTRUSTED. No web body,
embedded instructions, scripts, tokens, private paths or customer data
may enter a production or canonical knowledge write by this route.

Output: EVALUATION_ONLY. Independent content/reviewer attestation is
always NOT_ATTESTED, canonical_knowledge_written=false, and all
claims require actual original-source corroboration by a reviewer.

Tests: isolated CLI no-data invocation; positive declared reference
case; duplicate, conflict, foreign URL/host, malformed timestamp,
privacy and traversal-like inputs; no network or live authorization.
