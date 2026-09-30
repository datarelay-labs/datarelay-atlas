"""Sealed provider transition effect-request regressions."""

from __future__ import annotations

import json
import unittest
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

from atlas.provider_transition import provider_transition_plan_digest
from atlas.provider_transition_authorization import (
    authorize_provider_transition_effect,
    provider_transition_effect_authorization_digest,
    provider_transition_effect_state_digest,
)
from atlas.provider_transition_effect_request import (
    AUTHORITY,
    provider_transition_effect_request_digest,
    seal_provider_transition_effect_request,
    validate_provider_transition_effect_request,
)
from atlas.provenance import ValidationError
from tests.test_provider_broker import (
    FRESH_EVALUATED_AT,
    FRESH_MAX_EVIDENCE_AGE_SECONDS,
)
from tests.test_provider_transition_authorization import (
    NEXT_STATE_REVISION,
    _human_plan,
    _recommended_plan,
    _state,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "docs/contracts/provider-transition-effect-request.schema.json"


def _authorization(plan: dict, state: dict) -> dict:
    return authorize_provider_transition_effect(
        plan,
        state,
        consumed_at=FRESH_EVALUATED_AT,
        expected_max_evidence_age_seconds=FRESH_MAX_EVIDENCE_AGE_SECONDS,
        expected_transition_plan_digest=provider_transition_plan_digest(plan),
        expected_current_state_digest=provider_transition_effect_state_digest(state),
    )

def _seal(auth: dict, state: dict, plan: dict) -> dict:
    return seal_provider_transition_effect_request(
        auth,
        state,
        expected_authorization_digest=provider_transition_effect_authorization_digest(
            auth
        ),
        expected_transition_plan_digest=provider_transition_plan_digest(plan),
        expected_current_state_digest=provider_transition_effect_state_digest(state),
    )


class ProviderTransitionEffectRequestTests(unittest.TestCase):
    def test_fresh_authorization_seals_one_content_free_request(self) -> None:
        plan = _recommended_plan()
        state = _state()
        auth = _authorization(plan, state)

        request = _seal(auth, state, plan)

        self.assertEqual(request["authority"], AUTHORITY)
        self.assertEqual(request["from_route_id"], "codex-primary")
        self.assertEqual(request["to_route_id"], "codex-secondary")
        self.assertEqual(request["state_revision"], state["state_revision"])
        self.assertEqual(request["effect_epoch"], state["effect_epoch"])
        self.assertRegex(request["replay_key"], r"^[0-9a-f]{64}$")
        encoded = json.dumps(request, sort_keys=True)
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

    def test_request_and_replay_identity_are_deterministic(self) -> None:
        plan = _recommended_plan()
        state = _state()
        auth = _authorization(plan, state)

        first = _seal(auth, state, plan)
        second = _seal(deepcopy(auth), deepcopy(state), deepcopy(plan))

        self.assertEqual(first, second)
        self.assertEqual(first["replay_key"], second["replay_key"])
        self.assertEqual(
            provider_transition_effect_request_digest(first),
            provider_transition_effect_request_digest(second),
        )

    def test_human_required_authorization_cannot_seal(self) -> None:
        plan = _human_plan()
        state = _state()
        auth = _authorization(plan, state)
        self.assertEqual(auth["decision"], "HUMAN_REQUIRED")

        with self.assertRaisesRegex(ValidationError, "does not permit"):
            _seal(auth, state, plan)

    def test_changed_current_state_fails_before_sealing(self) -> None:
        plan = _recommended_plan()
        original = _state()
        auth = _authorization(plan, original)

        cases = (
            _state("openai-fallback"),
            _state(revision=NEXT_STATE_REVISION),
            _state(effect_epoch=8),
        )
        for changed in cases:
            with self.subTest(changed=changed):
                with self.assertRaises(ValidationError):
                    seal_provider_transition_effect_request(
                        auth,
                        changed,
                        expected_authorization_digest=provider_transition_effect_authorization_digest(
                            auth
                        ),
                        expected_transition_plan_digest=provider_transition_plan_digest(
                            plan
                        ),
                        expected_current_state_digest=provider_transition_effect_state_digest(
                            changed
                        ),
                    )

    def test_tampered_authorization_is_rejected(self) -> None:
        plan = _recommended_plan()
        state = _state()
        auth = _authorization(plan, state)
        trusted_auth_digest = provider_transition_effect_authorization_digest(auth)

        tampered = deepcopy(auth)
        tampered["plan_to_route_id"] = "openai-fallback"

        with self.assertRaisesRegex(ValidationError, "authorization digest"):
            seal_provider_transition_effect_request(
                tampered,
                state,
                expected_authorization_digest=trusted_auth_digest,
                expected_transition_plan_digest=provider_transition_plan_digest(plan),
                expected_current_state_digest=provider_transition_effect_state_digest(
                    state
                ),
            )

    def test_tampered_request_and_replay_key_fail_closed(self) -> None:
        plan = _recommended_plan()
        state = _state()
        auth = _authorization(plan, state)
        request = _seal(auth, state, plan)
        trusted = provider_transition_effect_request_digest(request)
        auth_digest = provider_transition_effect_authorization_digest(auth)
        state_digest = provider_transition_effect_state_digest(state)

        tampered = deepcopy(request)
        tampered["to_route_id"] = "openai-fallback"
        with self.assertRaisesRegex(ValidationError, "request digest"):
            validate_provider_transition_effect_request(
                tampered,
                expected_request_digest=trusted,
                expected_authorization_digest=auth_digest,
                expected_current_state_digest=state_digest,
            )

        replay_tampered = deepcopy(request)
        replay_tampered["replay_key"] = "f" * 64
        with self.assertRaisesRegex(ValidationError, "replay key"):
            validate_provider_transition_effect_request(
                replay_tampered,
                expected_request_digest=provider_transition_effect_request_digest(
                    replay_tampered
                ),
                expected_authorization_digest=auth_digest,
                expected_current_state_digest=state_digest,
            )

    def test_schema_matches_runtime_contract(self) -> None:
        plan = _recommended_plan()
        state = _state()
        request = _seal(_authorization(plan, state), state, plan)
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))

        Draft202012Validator(schema).validate(request)

        invalid = {**request, "effect_epoch": 0}
        with self.assertRaises(JsonSchemaValidationError):
            Draft202012Validator(schema).validate(invalid)

    def test_module_has_no_provider_transport_or_session_dependency(self) -> None:
        source = (
            ROOT / "atlas/provider_transition_effect_request.py"
        ).read_text(encoding="utf-8")
        for forbidden in (
            "subprocess",
            "requests",
            "urllib",
            "PtyPersist",
            "start_resume",
            "agent persist",
            "os.environ",
            "socket",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
