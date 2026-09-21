# Athena Repository Retirement Checklist

Status: Ready for owner archive/delete decision after independence PASS.
Related: ADR-0004, Work Packet datarelay-labs/datarelay-atlas#7

## Do not delete yet during ordinary implementation

Repository deletion of `datarelay-labs/athena` is an irreversible owner-approved final action.

## Preserved evidence (outside or independent of live Athena `main`)

- [x] Upstream snapshot tag `atlas-upstream-20260921` → `6720b948744d42f1332f86f9a8157ff588e40d6e`
- [x] PoC snapshot tag `atlas-poc-20260921` → `f38e20ec4d4ea22c71f1457d9a5361da1e92773a`
- [x] Generic hardening commit evidence `5b90969a7e0fecadae587144a0bd7f444352799f`
- [x] Atlas inventory: `docs/migration/athena-capability-inventory.json`
- [x] Atlas license audit evidence under `integrations/athena/`

## Open PR/issues disposition

- [x] `datarelay-labs/athena#1` closed unmerged as superseded by Atlas absorption
- [ ] Confirm no remaining Athena issues/PRs that block archive (owner sweep at delete time)

## Organization links/docs

- [x] Atlas README/ARCHITECTURE/AGENTS no longer describe Athena as a maintained runtime dependency
- [ ] Update any org-level README/link lists that still point to Athena as an active product (owner)
- [ ] Update `datarelay-atlas-docs` human docs if they still describe Athena as required (owner/docs follow-up)

## Final zero-dependency verification

- [x] `scripts/validate-athena-independence.py` PASS on Atlas
- [x] No Atlas submodule/subtree for Athena
- [x] Atlas unit/independence tests PASS without cloning Athena
- [ ] Re-run independence search across related org repos immediately before delete (owner)

## Owner final action

- [ ] Owner approves archive **or** delete of `datarelay-labs/athena`
- [ ] Perform archive/delete
- [ ] Record final action commit/date in this checklist

## Notes

Upstream `jannismilz/athena` remains third-party history and is unrelated to Data Relay Labs archive decisions.
