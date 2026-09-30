"""Provider transition one-shot effect boundary regressions."""

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
from atlas.provider_transition_effect import (
    commit_provider_transition_effect,
    provider_transition_effect_receipt_digest,
    validate_provider_transition_effect_receipt,
)
from atlas.provenance import ValidationError
from tests.test_provider_broker import (
    FRESH_EVALUATED_AT,
    FRESH_MAX_EVIDENCE_AGE_SECONDS,
)
from tests.test_provider_transition import _candidates, _plan_transition

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "docs/contracts/provider-transition-effect-receipt.schema.json"
OLD_REVISION = "1" * 64
NEW_REVISION = "2" * 64


def _state(route: str = "codex-primary", revision: str = OLD_REVISION, epoch: int = 7):
    return {
        "schema_version": 1,
        "kind": "provider_route_effect_state",
        "route_id": route,
        "state_revision": revision,
        "effect_epoch": epoch,
    }


def _plan(max_attempts: int = 3):
    return _plan_transition(
        _candidates(),
        required_capability="CODE_REVIEW",
        current_route_id="codex-primary",
        failure_reason="QUOTA_EXHAUSTED",
        max_attempts=max_attempts,
    )


def _auth(plan, state):
    return authorize_provider_transition_effect(
        plan,
        state,
        consumed_at=FRESH_EVALUATED_AT,
        expected_max_evidence_age_seconds=FRESH_MAX_EVIDENCE_AGE_SECONDS,
        expected_transition_plan_digest=provider_transition_plan_digest(plan),
        expected_current_state_digest=provider_transition_effect_state_digest(state),
    )

class FakePort:
    def __init__(self, result=None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls = []

    def commit(self, request):
        self.calls.append(deepcopy(request))
        if self.error is not None:
            raise self.error
        return deepcopy(self.result)


def _commit(auth, state, port):
    return commit_provider_transition_effect(
        auth,
        state,
        expected_authorization_digest=provider_transition_effect_authorization_digest(auth),
        expected_transition_plan_digest=auth["transition_plan_digest"],
        expected_current_state_digest=provider_transition_effect_state_digest(state),
        effect_port=port,
    )


class ProviderTransitionEffectTests(unittest.TestCase):
    def test_authorized_exact_state_commits_once(self):
        state = _state()
        auth = _auth(_plan(), state)
        new_state = _state("codex-secondary", NEW_REVISION, 8)
        port = FakePort({"outcome": "COMMITTED", "new_state": new_state})

        result = _commit(auth, state, port)

        self.assertEqual(result["outcome"], "PASS")
        self.assertEqual(result["reason"], "COMMITTED")
        self.assertEqual(len(port.calls), 1)
        self.assertEqual(port.calls[0]["from_route_id"], "codex-primary")
        self.assertEqual(port.calls[0]["to_route_id"], "codex-secondary")
        self.assertEqual(result["new_state_revision"], NEW_REVISION)
        self.assertEqual(result["new_effect_epoch"], 8)

    def test_human_authorization_never_invokes_effect(self):
        state = _state()
        auth = _auth(_plan(max_attempts=1), state)
        port = FakePort()

        result = _commit(auth, state, port)

        self.assertEqual(result["outcome"], "HUMAN_REQUIRED")
        self.assertEqual(result["reason"], "AUTHORIZATION_REQUIRED")
        self.assertEqual(port.calls, [])

    def test_stale_replay_against_advanced_state_fails_before_effect(self):
        original = _state()
        auth = _auth(_plan(), original)
        advanced = _state("codex-secondary", NEW_REVISION, 8)
        port = FakePort()

        with self.assertRaises(ValidationError):
            _commit(auth, advanced, port)

        self.assertEqual(port.calls, [])

    def test_changed_revision_same_route_fails_before_effect(self):
        original = _state()
        auth = _auth(_plan(), original)
        changed = _state("codex-primary", NEW_REVISION, 8)
        port = FakePort()

        with self.assertRaises(ValidationError):
            _commit(auth, changed, port)

        self.assertEqual(port.calls, [])

    def test_refusal_unknown_error_and_malformed_result_are_human_required(self):
        state = _state()
        auth = _auth(_plan(), state)
        cases = (
            (FakePort({"outcome": "REFUSED", "new_state": None}), "EFFECT_REFUSED"),
            (FakePort({"outcome": "UNKNOWN", "new_state": None}), "EFFECT_AMBIGUOUS"),
            (FakePort(error=RuntimeError("boom")), "EFFECT_ERROR"),
            (FakePort({"unexpected": True}), "EFFECT_RESULT_INVALID"),
        )
        for port, reason in cases:
            with self.subTest(reason=reason):
                result = _commit(auth, state, port)
                self.assertEqual(result["outcome"], "HUMAN_REQUIRED")
                self.assertEqual(result["reason"], reason)
                self.assertEqual(len(port.calls), 1)
                self.assertIsNone(result["new_state_digest"])

    def test_committed_result_must_advance_exact_target_revision_and_epoch(self):
        state = _state()
        auth = _auth(_plan(), state)
        bad_states = (
            _state("openai-fallback", NEW_REVISION, 8),
            _state("codex-secondary", OLD_REVISION, 8),
            _state("codex-secondary", NEW_REVISION, 9),
        )
        for bad in bad_states:
            port = FakePort({"outcome": "COMMITTED", "new_state": bad})
            result = _commit(auth, state, port)
            self.assertEqual(result["outcome"], "HUMAN_REQUIRED")
            self.assertEqual(result["reason"], "EFFECT_RESULT_INVALID")
            self.assertEqual(len(port.calls), 1)

    def test_receipt_digest_and_schema(self):
        state = _state()
        auth = _auth(_plan(), state)
        port = FakePort(
            {"outcome": "COMMITTED", "new_state": _state("codex-secondary", NEW_REVISION, 8)}
        )
        result = _commit(auth, state, port)
        digest = provider_transition_effect_receipt_digest(result)
        validated = validate_provider_transition_effect_receipt(
            result,
            expected_receipt_digest=digest,
            expected_authorization_digest=provider_transition_effect_authorization_digest(auth),
        )
        self.assertEqual(validated, result)
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        Draft202012Validator(schema).validate(result)

        bad = {**result, "new_effect_epoch": 10}
        with self.assertRaises(JsonSchemaValidationError):
            Draft202012Validator(schema).validate(
                {**bad, "outcome": "HUMAN_REQUIRED", "reason": "EFFECT_RESULT_INVALID"}
            )

    def test_module_has_no_concrete_provider_transport_dependency(self):
        source = (ROOT / "atlas/provider_transition_effect.py").read_text(encoding="utf-8")
        for forbidden in (
            "subprocess", "requests", "urllib", "PtyPersist",
            "start_resume", "agent persist", "os.environ",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
