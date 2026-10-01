import json
import unittest
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator
from atlas.provenance import ValidationError
from atlas.provider_broker import plan_provider_routes, validate_provider_route_candidate
from atlas.provider_capacity import build_provider_capacity_input, validate_provider_capacity_input

ROOT=Path(__file__).resolve().parents[1]
CONTRACTS=ROOT/"docs"/"contracts"
FRESH_EVALUATED_AT="2026-09-28T00:00:00Z"
def _candidate(provider,route_id,capability_rank,stewardship_rank):
    descriptor={"schema_version":1,"kind":"provider_capability_descriptor","provider":provider,"runtime":"codex_cli","usage_mode":"chatgpt_plan","adapter":"CodexAuditProvider","capabilities":[{"name":"CODE_REVIEW","status":"SUPPORTED","result_contract":"audit_result_v1"}],"authority":"EVIDENCE_ONLY"}
    capacity=build_provider_capacity_input(provider=provider,source_kind="synthetic_test",window_start=FRESH_EVALUATED_AT,window_end=FRESH_EVALUATED_AT,event_count=1,total_tokens=1)
    capacity["signals"]["remaining_capacity"]={"status":"OBSERVED","value":"100","unit":"credits"}
    capacity["signals"]["active_inference_wip"]={"status":"OBSERVED","value":0}
    return {"schema_version":1,"kind":"provider_route_candidate","route_id":route_id,"capability_descriptor":descriptor,"capacity_input":validate_provider_capacity_input(capacity),"gates":{name:"ALLOW" for name in ("policy","trust","budget","usage_mode","blast_radius","wip")},"ranks":{"capability_preference":capability_rank,"stewardship_preference":stewardship_rank}}
from atlas.provider_capacity_operational import build_unknown_provider_capacity_operational, validate_provider_capacity_operational

def observed():
    return {"schema_version":1,"kind":"provider_capacity_operational","provider":"codex",
        "evidence":{"authority":"PROVIDER_AUTHORITATIVE","source_kind":"provider_status","source_ref":"status/api","source_digest":"a"*64,"observed_at":"2026-09-30T00:00:00Z"},
        "facts":{"reset_semantics":{"status":"OBSERVED","mode":"ROLLING_WINDOW","window_seconds":18000},
                 "health":{"status":"OBSERVED","value":"OPERATIONAL"},
                 "latency":{"status":"OBSERVED","metric":"ROUND_TRIP_MS","boundary":"provider_api","value_ms":420}}}

class ProviderCapacityOperationalTests(unittest.TestCase):
    def test_public_schema_fixture_runtime_parity(self):
        schema=json.loads((CONTRACTS/"provider-capacity-operational.schema.json").read_text())
        fixture=json.loads((CONTRACTS/"fixtures/provider-capacity-operational.example.json").read_text())
        Draft202012Validator.check_schema(schema); Draft202012Validator(schema).validate(fixture)
        self.assertEqual(validate_provider_capacity_operational(fixture),fixture)

    def test_unknown_builder(self):
        value=build_unknown_provider_capacity_operational(provider="codex",source_kind="unverified_observation")
        self.assertEqual(value["evidence"]["authority"],"UNVERIFIED")
        self.assertTrue(all(f["status"]=="UNKNOWN" for f in value["facts"].values()))
    def test_observed_requires_authority(self):
        value=observed(); value["evidence"].update(authority="UNVERIFIED",source_ref=None,source_digest=None,observed_at=None)
        with self.assertRaisesRegex(ValidationError,"PROVIDER_AUTHORITATIVE"): validate_provider_capacity_operational(value)
    def test_reset_semantics_does_not_duplicate_reset_at(self):
        value=validate_provider_capacity_operational(observed())
        self.assertNotIn("reset_at", value["facts"]["reset_semantics"])
        self.assertEqual(value["facts"]["reset_semantics"]["mode"],"ROLLING_WINDOW")
    def test_latency_requires_bounded_metric_boundary_value(self):
        for field,bad in (("metric","CLIENT_GUESS"),("boundary","sk-secret"),("value_ms",-1)):
            value=observed(); value["facts"]["latency"][field]=bad
            with self.subTest(field=field), self.assertRaises(ValidationError): validate_provider_capacity_operational(value)
    def test_health_is_provider_status_not_local_inference(self):
        value=observed(); value["facts"]["health"]["value"]="LOCAL_SUCCESS"
        with self.assertRaises(ValidationError): validate_provider_capacity_operational(value)
    def test_unverified_cannot_claim_provenance(self):
        value=build_unknown_provider_capacity_operational(provider="codex",source_kind="unverified_observation")
        for field,bad in (("source_ref","status/api"),("source_digest","a"*64),("observed_at","2026-09-30T00:00:00Z")):
            candidate=deepcopy(value); candidate["evidence"][field]=bad
            with self.subTest(field=field), self.assertRaises(ValidationError): validate_provider_capacity_operational(candidate)
    def test_provider_identity_is_bounded(self):
        value=observed(); value["provider"]="Codex Invalid"
        with self.assertRaises(ValidationError): validate_provider_capacity_operational(value)
    def test_optional_candidate_binding_and_provider_identity(self):
        candidate=_candidate("codex","codex-primary",capability_rank=0,stewardship_rank=10)
        candidate["capacity_operational"]=observed()
        normalized=validate_provider_route_candidate(candidate)
        self.assertEqual(normalized["capacity_operational"]["facts"]["health"]["value"],"OPERATIONAL")
        candidate["capacity_operational"]["provider"]="openai"
        with self.assertRaisesRegex(ValidationError,"operational evidence"): validate_provider_route_candidate(candidate)
    def test_broker_plan_is_unchanged_by_operational_evidence(self):
        legacy=_candidate("codex","codex-primary",capability_rank=0,stewardship_rank=10)
        enriched=deepcopy(legacy); enriched["capacity_operational"]=observed()
        baseline=plan_provider_routes([legacy],required_capability="CODE_REVIEW",evaluated_at=FRESH_EVALUATED_AT,max_evidence_age_seconds=0)
        enriched_plan=plan_provider_routes([enriched],required_capability="CODE_REVIEW",evaluated_at=FRESH_EVALUATED_AT,max_evidence_age_seconds=0)
        self.assertEqual(enriched_plan,baseline)

if __name__=="__main__": unittest.main()
