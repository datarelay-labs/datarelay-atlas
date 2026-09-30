"""Provider transition one-shot sealed-request effect regressions."""

from __future__ import annotations

import json
import unittest
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

from atlas.provider_transition_authorization import (
    provider_transition_effect_authorization_digest,
    provider_transition_effect_state_digest,
)
from atlas.provider_transition_effect import (
    commit_provider_transition_effect,
    provider_transition_effect_receipt_digest,
    validate_provider_transition_effect_receipt,
)
from atlas.provider_transition_effect_request import (
    provider_transition_effect_request_digest,
)
from atlas.provenance import ValidationError
from tests.test_provider_transition_authorization import (
    NEXT_STATE_REVISION,
    _recommended_plan,
    _state,
)
from tests.test_provider_transition_effect_request import _authorization, _seal

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "docs/contracts/provider-transition-effect-receipt.schema.json"
OLD_REVISION = "1" * 64
NEW_REVISION = "2" * 64


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


def _bundle():
    plan = _recommended_plan()
    state = _state()
    auth = _authorization(plan, state)
    request = _seal(auth, state, plan)
    return plan, state, auth, request


def _commit(request, state, auth, port):
    return commit_provider_transition_effect(
        request,
        state,
        expected_request_digest=provider_transition_effect_request_digest(request),
        expected_authorization_digest=provider_transition_effect_authorization_digest(auth),
        expected_current_state_digest=provider_transition_effect_state_digest(state),
        effect_port=port,
    )

class ProviderTransitionEffectTests(unittest.TestCase):
    def test_sealed_exact_state_commits_once(self):
        _plan, state, auth, request = _bundle()
        new_state = _state("codex-secondary", revision=NEW_REVISION, effect_epoch=8)
        port = FakePort({"outcome": "COMMITTED", "new_state": new_state})

        result = _commit(request, state, auth, port)

        self.assertEqual(result["outcome"], "PASS")
        self.assertEqual(result["reason"], "COMMITTED")
        self.assertEqual(len(port.calls), 1)
        call = port.calls[0]
        self.assertEqual(
            call["effect_request_digest"],
            provider_transition_effect_request_digest(request),
        )
        self.assertEqual(call["replay_key"], request["replay_key"])
        self.assertEqual(call["from_route_id"], "codex-primary")
        self.assertEqual(call["to_route_id"], "codex-secondary")
        self.assertEqual(result["new_state_revision"], NEW_REVISION)
        self.assertEqual(result["new_effect_epoch"], 8)

    def test_advanced_state_replay_fails_before_effect(self):
        _plan, _state0, auth, request = _bundle()
        advanced = _state("codex-secondary", revision=NEW_REVISION, effect_epoch=8)
        port = FakePort()

        with self.assertRaises(ValidationError):
            _commit(request, advanced, auth, port)

        self.assertEqual(port.calls, [])

    def test_changed_revision_same_route_fails_before_effect(self):
        _plan, _state0, auth, request = _bundle()
        changed = _state("codex-primary", revision=NEXT_STATE_REVISION, effect_epoch=8)
        port = FakePort()

        with self.assertRaises(ValidationError):
            _commit(request, changed, auth, port)

        self.assertEqual(port.calls, [])

    def test_tampered_request_fails_before_effect(self):
        _plan, state, auth, request = _bundle()
        trusted_request_digest = provider_transition_effect_request_digest(request)
        tampered = deepcopy(request)
        tampered["to_route_id"] = "openai-fallback"
        port = FakePort()

        with self.assertRaisesRegex(ValidationError, "request digest"):
            commit_provider_transition_effect(
                tampered,
                state,
                expected_request_digest=trusted_request_digest,
                expected_authorization_digest=provider_transition_effect_authorization_digest(
                    auth
                ),
                expected_current_state_digest=provider_transition_effect_state_digest(
                    state
                ),
                effect_port=port,
            )

        self.assertEqual(port.calls, [])

    def test_terminal_effect_epoch_fails_before_port_invocation(self):
        plan = _recommended_plan()
        state = _state(effect_epoch=2**31 - 1)
        auth = _authorization(plan, state)
        request = _seal(auth, state, plan)
        port = FakePort()

        with self.assertRaisesRegex(ValidationError, "cannot advance"):
            _commit(request, state, auth, port)

        self.assertEqual(port.calls, [])

    def test_receipt_runtime_rejects_unsafe_route_ids(self):
        _plan, state, auth, request = _bundle()
        port = FakePort(
            {
                "outcome": "COMMITTED",
                "new_state": _state(
                    "codex-secondary",
                    revision=NEW_REVISION,
                    effect_epoch=8,
                ),
            }
        )
        result = _commit(request, state, auth, port)
        for field, value in (
            ("from_route_id", ""),
            ("to_route_id", "sk-secret"),
        ):
            with self.subTest(field=field):
                bad = {**result, field: value}
                with self.assertRaises(ValidationError):
                    validate_provider_transition_effect_receipt(
                        bad,
                        expected_receipt_digest=provider_transition_effect_receipt_digest(
                            bad
                        ),
                        expected_request_digest=provider_transition_effect_request_digest(
                            request
                        ),
                        expected_authorization_digest=provider_transition_effect_authorization_digest(
                            auth
                        ),
                    )

    def test_refusal_unknown_error_and_malformed_result_are_human_required(self):
        _plan, state, auth, request = _bundle()
        cases = (
            (FakePort({"outcome": "REFUSED", "new_state": None}), "EFFECT_REFUSED"),
            (FakePort({"outcome": "UNKNOWN", "new_state": None}), "EFFECT_AMBIGUOUS"),
            (FakePort(error=RuntimeError("boom")), "EFFECT_ERROR"),
            (FakePort({"unexpected": True}), "EFFECT_RESULT_INVALID"),
        )
        for port, reason in cases:
            with self.subTest(reason=reason):
                result = _commit(request, state, auth, port)
                self.assertEqual(result["outcome"], "HUMAN_REQUIRED")
                self.assertEqual(result["reason"], reason)
                self.assertEqual(len(port.calls), 1)
                self.assertIsNone(result["new_state_digest"])
                self.assertEqual(
                    result["effect_request_digest"],
                    provider_transition_effect_request_digest(request),
                )

    def test_committed_result_must_advance_exact_target_revision_and_epoch(self):
        _plan, state, auth, request = _bundle()
        bad_states = (
            _state("openai-fallback", revision=NEW_REVISION, effect_epoch=8),
            _state("codex-secondary", revision=OLD_REVISION, effect_epoch=8),
            _state("codex-secondary", revision=NEW_REVISION, effect_epoch=9),
        )
        for bad in bad_states:
            port = FakePort({"outcome": "COMMITTED", "new_state": bad})
            result = _commit(request, state, auth, port)
            self.assertEqual(result["outcome"], "HUMAN_REQUIRED")
            self.assertEqual(result["reason"], "EFFECT_RESULT_INVALID")
            self.assertEqual(len(port.calls), 1)

    def test_receipt_digest_and_schema(self):
        _plan, state, auth, request = _bundle()
        port = FakePort(
            {"outcome": "COMMITTED", "new_state": _state("codex-secondary", revision=NEW_REVISION, effect_epoch=8)}
        )
        result = _commit(request, state, auth, port)
        digest = provider_transition_effect_receipt_digest(result)
        validated = validate_provider_transition_effect_receipt(
            result,
            expected_receipt_digest=digest,
            expected_request_digest=provider_transition_effect_request_digest(request),
            expected_authorization_digest=provider_transition_effect_authorization_digest(auth),
        )
        self.assertEqual(validated, result)
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        Draft202012Validator(schema).validate(result)

        bad = {
            **result,
            "outcome": "HUMAN_REQUIRED",
            "reason": "EFFECT_RESULT_INVALID",
            "new_effect_epoch": 10,
        }
        with self.assertRaises(JsonSchemaValidationError):
            Draft202012Validator(schema).validate(bad)

    def test_module_has_no_concrete_provider_transport_dependency(self):
        source = (ROOT / "atlas/provider_transition_effect.py").read_text(encoding="utf-8")
        for forbidden in (
            "subprocess", "requests", "urllib", "PtyPersist",
            "start_resume", "agent persist", "os.environ", "socket",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
