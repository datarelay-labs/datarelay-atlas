"""Exact-state provider transition effect-authorization regressions."""

from __future__ import annotations

import json
import unittest
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

from atlas.provider_transition import provider_transition_plan_digest
from atlas.provider_transition_authorization import (
    AUTHORIZED_AUTHORITY,
    NO_AUTHORITY,
    authorize_provider_transition_effect,
    provider_transition_effect_authorization_digest,
    provider_transition_effect_state_digest,
    validate_provider_transition_effect_authorization,
    validate_provider_transition_effect_state,
)
from atlas.provenance import ValidationError
from tests.test_provider_broker import (
    FRESH_EVALUATED_AT,
    FRESH_MAX_EVIDENCE_AGE_SECONDS,
)
from tests.test_provider_transition import _candidates, _plan_transition

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "docs" / "contracts"
AUTH_SCHEMA = CONTRACTS / "provider-transition-effect-authorization.schema.json"
STATE_SCHEMA = CONTRACTS / "provider-transition-effect-state.schema.json"

STATE_REVISION = "1" * 64
NEXT_STATE_REVISION = "2" * 64


def _state(
    route_id: str = "codex-primary",
    *,
    revision: str = STATE_REVISION,
    effect_epoch: int = 7,
) -> dict:
    return {
        "schema_version": 1,
        "kind": "provider_route_effect_state",
        "route_id": route_id,
        "state_revision": revision,
        "effect_epoch": effect_epoch,
    }

def _recommended_plan() -> dict:
    return _plan_transition(
        _candidates(),
        required_capability="CODE_REVIEW",
        current_route_id="codex-primary",
        failure_reason="QUOTA_EXHAUSTED",
        max_attempts=3,
    )


def _human_plan() -> dict:
    return _plan_transition(
        _candidates(),
        required_capability="CODE_REVIEW",
        current_route_id="codex-primary",
        failure_reason="QUOTA_EXHAUSTED",
        max_attempts=1,
    )


def _authorize(plan: dict, state: dict, *, consumed_at: str = FRESH_EVALUATED_AT) -> dict:
    return authorize_provider_transition_effect(
        plan,
        state,
        consumed_at=consumed_at,
        expected_max_evidence_age_seconds=FRESH_MAX_EVIDENCE_AGE_SECONDS,
        expected_transition_plan_digest=provider_transition_plan_digest(plan),
        expected_current_state_digest=provider_transition_effect_state_digest(state),
    )


class ProviderTransitionAuthorizationTests(unittest.TestCase):
    def test_exact_state_authorizes_without_performing_effect(self) -> None:
        plan = _recommended_plan()
        state = _state()

        result = _authorize(plan, state)

        self.assertEqual(result["decision"], "AUTHORIZED")
        self.assertEqual(result["decision_reason"], "EXACT_STATE_MATCH")
        self.assertEqual(result["authority"], AUTHORIZED_AUTHORITY)
        self.assertEqual(result["plan_from_route_id"], "codex-primary")
        self.assertEqual(result["plan_to_route_id"], "codex-secondary")
        self.assertEqual(result["current_route_id"], "codex-primary")
        self.assertEqual(result["state_revision"], STATE_REVISION)
        self.assertEqual(result["effect_epoch"], 7)
        self.assertEqual(result["required_capability"], "CODE_REVIEW")
        self.assertEqual(result["attempt"], 1)
        self.assertEqual(result["max_attempts"], 3)
        encoded = json.dumps(result, sort_keys=True)
        for forbidden in (
            "prompt",
            "transcript",
            "findings",
            "credential",
            "secret",
            "/home/",
            "session_id",
            "provider_response",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_authorization_digest_is_deterministic_and_out_of_band(self) -> None:
        plan = _recommended_plan()
        state = _state()
        first = _authorize(plan, state)
        second = _authorize(deepcopy(plan), deepcopy(state))

        self.assertEqual(first, second)
        digest = provider_transition_effect_authorization_digest(first)
        self.assertEqual(
            digest,
            provider_transition_effect_authorization_digest(second),
        )
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertNotIn("authorization_digest", first)
        validated = validate_provider_transition_effect_authorization(
            first,
            expected_authorization_digest=digest,
            expected_transition_plan_digest=provider_transition_plan_digest(plan),
            expected_current_state_digest=provider_transition_effect_state_digest(state),
        )
        self.assertEqual(validated, first)

    def test_wrong_current_route_returns_human_required_without_authority(self) -> None:
        result = _authorize(_recommended_plan(), _state("openai-fallback"))

        self.assertEqual(result["decision"], "HUMAN_REQUIRED")
        self.assertEqual(result["decision_reason"], "CURRENT_ROUTE_MISMATCH")
        self.assertEqual(result["authority"], NO_AUTHORITY)
        self.assertEqual(result["plan_from_route_id"], "codex-primary")
        self.assertEqual(result["plan_to_route_id"], "codex-secondary")
        self.assertEqual(result["current_route_id"], "openai-fallback")

    def test_human_transition_plan_cannot_be_upgraded_to_authorization(self) -> None:
        plan = _human_plan()
        self.assertEqual(plan["decision"], "HUMAN_REQUIRED")

        result = _authorize(plan, _state())

        self.assertEqual(result["decision"], "HUMAN_REQUIRED")
        self.assertEqual(result["decision_reason"], "PLAN_REQUIRES_HUMAN")
        self.assertEqual(result["authority"], NO_AUTHORITY)
        self.assertIsNone(result["plan_to_route_id"])

    def test_changed_state_revision_invalidates_old_state_identity_and_authorization(
        self,
    ) -> None:
        plan = _recommended_plan()
        original = _state()
        changed = _state(revision=NEXT_STATE_REVISION, effect_epoch=8)
        original_digest = provider_transition_effect_state_digest(original)
        changed_digest = provider_transition_effect_state_digest(changed)

        with self.assertRaisesRegex(ValidationError, "state digest"):
            validate_provider_transition_effect_state(
                changed,
                expected_state_digest=original_digest,
            )

        old_auth = _authorize(plan, original)
        old_auth_digest = provider_transition_effect_authorization_digest(old_auth)
        with self.assertRaisesRegex(ValidationError, "state digest is inconsistent"):
            validate_provider_transition_effect_authorization(
                old_auth,
                expected_authorization_digest=old_auth_digest,
                expected_transition_plan_digest=provider_transition_plan_digest(plan),
                expected_current_state_digest=changed_digest,
            )

        new_auth = _authorize(plan, changed)
        self.assertNotEqual(
            provider_transition_effect_authorization_digest(old_auth),
            provider_transition_effect_authorization_digest(new_auth),
        )

    def test_tampered_or_stale_transition_plan_fails_closed(self) -> None:
        plan = _recommended_plan()
        state = _state()
        trusted_plan_digest = provider_transition_plan_digest(plan)

        tampered = deepcopy(plan)
        tampered["failure_reason"] = "RATE_LIMITED"
        with self.assertRaisesRegex(ValidationError, "plan digest"):
            authorize_provider_transition_effect(
                tampered,
                state,
                consumed_at=FRESH_EVALUATED_AT,
                expected_max_evidence_age_seconds=FRESH_MAX_EVIDENCE_AGE_SECONDS,
                expected_transition_plan_digest=trusted_plan_digest,
                expected_current_state_digest=provider_transition_effect_state_digest(
                    state
                ),
            )

        with self.assertRaisesRegex(ValidationError, "stale|expired"):
            authorize_provider_transition_effect(
                plan,
                state,
                consumed_at="2026-09-28T00:10:01Z",
                expected_max_evidence_age_seconds=FRESH_MAX_EVIDENCE_AGE_SECONDS,
                expected_transition_plan_digest=trusted_plan_digest,
                expected_current_state_digest=provider_transition_effect_state_digest(
                    state
                ),
            )

    def test_state_contract_rejects_malformed_revision_epoch_and_extra_content(self) -> None:
        cases = [
            {**_state(), "state_revision": "not-a-digest"},
            {**_state(), "effect_epoch": 0},
            {**_state(), "effect_epoch": True},
            {**_state(), "prompt": "do not retain me"},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    validate_provider_transition_effect_state(
                        payload,
                        expected_state_digest=provider_transition_effect_state_digest(
                            payload
                        ),
                    )

    def test_authorization_contract_rejects_tamper_and_inconsistent_decisions(
        self,
    ) -> None:
        plan = _recommended_plan()
        state = _state()
        authorized = _authorize(plan, state)

        tampered = deepcopy(authorized)
        tampered["plan_to_route_id"] = "openai-fallback"
        with self.assertRaisesRegex(ValidationError, "authorization digest"):
            validate_provider_transition_effect_authorization(
                tampered,
                expected_authorization_digest=provider_transition_effect_authorization_digest(
                    authorized
                ),
                expected_transition_plan_digest=provider_transition_plan_digest(plan),
                expected_current_state_digest=provider_transition_effect_state_digest(
                    state
                ),
            )

        inconsistent = deepcopy(authorized)
        inconsistent["decision"] = "HUMAN_REQUIRED"
        inconsistent["authority"] = NO_AUTHORITY
        inconsistent["decision_reason"] = "PLAN_REQUIRES_HUMAN"
        with self.assertRaisesRegex(ValidationError, "cannot select a target route"):
            validate_provider_transition_effect_authorization(
                inconsistent,
                expected_authorization_digest=provider_transition_effect_authorization_digest(
                    inconsistent
                ),
                expected_transition_plan_digest=provider_transition_plan_digest(plan),
                expected_current_state_digest=provider_transition_effect_state_digest(
                    state
                ),
            )

    def test_json_schemas_match_runtime_contracts(self) -> None:
        state = _state()
        auth = _authorize(_recommended_plan(), state)
        state_schema = json.loads(STATE_SCHEMA.read_text(encoding="utf-8"))
        auth_schema = json.loads(AUTH_SCHEMA.read_text(encoding="utf-8"))

        Draft202012Validator(state_schema).validate(state)
        Draft202012Validator(auth_schema).validate(auth)

        bad_state = {**state, "effect_epoch": 0}
        with self.assertRaises(JsonSchemaValidationError):
            Draft202012Validator(state_schema).validate(bad_state)

        bad_auth = {**auth, "authority": NO_AUTHORITY}
        with self.assertRaises(JsonSchemaValidationError):
            Draft202012Validator(auth_schema).validate(bad_auth)

    def test_module_has_no_provider_transport_or_session_dependency(self) -> None:
        source = (
            ROOT / "atlas" / "provider_transition_authorization.py"
        ).read_text(encoding="utf-8")
        for forbidden in (
            "subprocess",
            "requests",
            "urllib",
            "PtyPersist",
            "start_resume",
            "agent persist",
            "os.environ",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
