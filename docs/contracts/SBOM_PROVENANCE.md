# Atlas SBOM and Provenance Evidence

This contract defines the Phase 5 release-evidence boundary for a deterministic CycloneDX SBOM generated from the Python environment that is actually running Atlas.

## Truth boundary

requirements.txt and THIRD_PARTY.md remain declarations. They are not an SBOM and never become release evidence merely because those files exist.

A validated bundle contains two files:

- bom.cdx.json — CycloneDX 1.6 JSON with exact installed Python distribution names and versions from importlib.metadata.
- provenance.json — Atlas evidence binding the SBOM to repository identity, exact source HEAD, clean source state, source-file digests, Python runtime, component count, SBOM SHA-256, deterministic serial number, and a content-addressed provenance digest.

The generator records no environment variables, credentials, package install paths, prompts, source contents, tokens, or free-form package metadata.

## Source integrity

Normal CLI generation requires the datarelay-labs/datarelay-atlas repository, an exact 40-character source HEAD, a clean worktree, no assume-unchanged or skip-worktree hidden index state, and current requirements.txt plus THIRD_PARTY.md digests.

The release artifact destination must be a new absolute directory outside the repository. Existing, symlinked, or path-aliased destinations fail closed.

## Runtime integrity

The SBOM represents the resolved distributions visible to the executing Python runtime. Declared top-level requirement names are marked separately from other resolved or transitive distributions.

Validation used for Operations Readiness re-enumerates the current runtime and requires byte-equivalent normalized SBOM and provenance content. Re-signing a tampered SBOM and provenance record does not make it current-runtime evidence.

License evidence is conservative. Atlas includes only a small allowlist of strict known SPDX identifiers when installed distribution metadata exposes one directly. Free-form or ambiguous license text is omitted rather than interpreted.

## Determinism

For the same exact source HEAD, source-file digests, Python runtime identity, and normalized resolved distribution inventory, component ordering, CycloneDX serial number, SBOM SHA-256, and provenance digest are stable.

No wall-clock timestamp is part of the content-addressed core.

## Authority

SBOM and provenance artifacts have EVIDENCE_ONLY authority. They do not grant release, deploy, merge, security-review, or final PASS authority. They do not scan for vulnerabilities, make legal conclusions, sign artifacts, download packages, call provider APIs, or mutate the Atlas data root.

## CLI

Generate a new bundle from a clean exact source using the Python runtime whose installed distributions should be represented:

    python3 -m atlas ops sbom --repo-root /path/to/datarelay-atlas --dest /new/outside-repo/sbom-bundle

This slice does not configure a release-gate command and leaves sbom_required false. A later release-policy decision may wire the generator into managed release automation together with the required Engineering System release workflow.

Operations Readiness may inspect an explicit bundle:

    python3 -m atlas ops readiness --sbom-bundle /path/to/sbom-bundle

It reports VALIDATED_EVIDENCE only when the bundle matches the current clean source and current Python runtime. Declarations alone remain NOT_GENERATED.
