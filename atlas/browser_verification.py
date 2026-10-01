"""Provider-neutral, evidence-only browser verification contract."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Callable
from urllib.parse import urlsplit

from atlas.provenance import ValidationError
from atlas.secrets import contains_unsafe_secret, sanitize_durable_text

SCHEMA_VERSION = 1
REQUEST_KIND = "browser_verification_request"
RESULT_KIND = "browser_verification_result"
AUTHORITY = "BROWSER_EVIDENCE_ONLY"
SCENARIO = "ATLAS_OVERVIEW_TO_CONCURRENCY"
PROVIDERS = frozenset({"playwright", "stagehand"})
VARIATIONS = frozenset({"BASELINE", "WRAPPED_LAYOUT"})
OUTCOMES = frozenset({"PASS", "FAIL", "HUMAN_REQUIRED"})
MODEL_STATES = frozenset({"NOT_APPLICABLE", "OBSERVED", "UNAVAILABLE"})
_HEAD = re.compile(r"^[0-9a-f]{40}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@#+/-]{0,255}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@#+-]{0,255}$")
_MAX_STEPS = 16
_RESULT_MARKER = "ATLAS_BROWSER_RESULT="

CommandRunner = Callable[
    [list[str], str, str, dict[str, str]],
    subprocess.CompletedProcess[str],
]


def _reject(message: str) -> None:
    raise ValidationError(message)

def _canonical_digest(value: object) -> str:
    raw = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _identity(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or _ID.fullmatch(value) is None
        or contains_unsafe_secret(value)
    ):
        _reject(f"browser verification {label} is invalid")
    return value


def _run_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or _RUN_ID.fullmatch(value) is None
        or contains_unsafe_secret(value)
    ):
        _reject("browser verification run_id is invalid")
    return value


def _bounded_int(
    value: object,
    *,
    label: str,
    maximum: int,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= maximum
    ):
        _reject(f"browser verification {label} is invalid")
    return value


def _safe_text(value: object, *, label: str, maximum: int = 600) -> str:
    if not isinstance(value, str):
        _reject(f"browser verification {label} is invalid")
    try:
        return sanitize_durable_text(value, max_chars=maximum)
    except ValidationError as exc:
        raise ValidationError(
            f"browser verification {label} is unsafe"
        ) from exc

def _target_url(value: object) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        _reject("browser verification target_url is invalid")
    if contains_unsafe_secret(value):
        _reject("browser verification target_url contains secret material")
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"}:
        _reject("browser verification target_url scheme is invalid")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        _reject("browser verification PoC target must be loopback")
    if parsed.username or parsed.password or parsed.fragment:
        _reject("browser verification target_url contains forbidden parts")
    return value


def _evidence_ref(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 512:
        _reject(f"browser verification {label} is invalid")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        _reject(f"browser verification {label} must be repository-relative")
    if not value.startswith("evidence/browser-verification/"):
        _reject(f"browser verification {label} is outside evidence boundary")
    return value


def validate_browser_verification_request(
    payload: object,
) -> dict[str, object]:
    expected = {
        "schema_version",
        "kind",
        "run_id",
        "scenario_id",
        "provider",
        "target_url",
        "environment",
        "source_revision",
        "work_packet_issue",
        "variation",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        _reject("browser verification request schema is invalid")
    if (
        isinstance(payload.get("schema_version"), bool)
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("kind") != REQUEST_KIND
    ):
        _reject("browser verification request version/kind is invalid")
    provider = payload.get("provider")
    variation = payload.get("variation")
    environment = payload.get("environment")
    head = payload.get("source_revision")
    issue = payload.get("work_packet_issue")
    if provider not in PROVIDERS:
        _reject("browser verification provider is invalid")
    if variation not in VARIATIONS:
        _reject("browser verification variation is invalid")
    if environment not in {"LOCAL", "PREVIEW"}:
        _reject("browser verification environment is invalid")
    if not isinstance(head, str) or _HEAD.fullmatch(head) is None:
        _reject("browser verification source_revision is invalid")
    if isinstance(issue, bool) or not isinstance(issue, int) or issue < 1:
        _reject("browser verification work_packet_issue is invalid")
    result = {
        "schema_version": SCHEMA_VERSION,
        "kind": REQUEST_KIND,
        "run_id": _run_id(payload.get("run_id")),
        "scenario_id": _identity(
            payload.get("scenario_id"), label="scenario_id"
        ),
        "provider": provider,
        "target_url": _target_url(payload.get("target_url")),
        "environment": environment,
        "source_revision": head,
        "work_packet_issue": issue,
        "variation": variation,
    }
    if result["scenario_id"] != SCENARIO:
        _reject("browser verification scenario is unsupported")
    return result

def _validate_step(value: object) -> dict[str, object]:
    expected = {"step_id", "outcome", "detail", "evidence_ref"}
    if not isinstance(value, dict) or set(value) != expected:
        _reject("browser verification step schema is invalid")
    outcome = value.get("outcome")
    if outcome not in {"PASS", "FAIL", "SKIPPED"}:
        _reject("browser verification step outcome is invalid")
    return {
        "step_id": _identity(value.get("step_id"), label="step_id"),
        "outcome": outcome,
        "detail": _safe_text(value.get("detail"), label="step detail"),
        "evidence_ref": _evidence_ref(
            value.get("evidence_ref"), label="step evidence_ref"
        ),
    }


def _validate_model_metrics(value: object) -> dict[str, object]:
    expected = {
        "state",
        "provider",
        "model",
        "llm_call_count",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cost_microusd",
    }
    if not isinstance(value, dict) or set(value) != expected:
        _reject("browser verification model metrics schema is invalid")
    state = value.get("state")
    if state not in MODEL_STATES:
        _reject("browser verification model metrics state is invalid")
    result: dict[str, object] = {"state": state}
    for key in ("provider", "model"):
        raw = value.get(key)
        result[key] = (
            None
            if raw is None
            else _safe_text(raw, label=f"model metrics {key}", maximum=128)
        )
    for key in (
        "llm_call_count",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cost_microusd",
    ):
        raw = value.get(key)
        result[key] = (
            None
            if raw is None
            else _bounded_int(
                raw,
                label=f"model metrics {key}",
                maximum=10**12,
            )
        )
    if state == "NOT_APPLICABLE":
        if (
            result["provider"] is not None
            or result["model"] is not None
            or any(
                result[key] not in {0, None}
                for key in (
                    "llm_call_count",
                    "input_tokens",
                    "output_tokens",
                    "total_tokens",
                    "cost_microusd",
                )
            )
        ):
            _reject("browser verification non-model metrics are inconsistent")
    return result


def _validate_raw_provider_result(payload: object) -> dict[str, object]:
    expected = {
        "result",
        "browser_engine",
        "browser_version",
        "actual_browser_process",
        "duration_ms",
        "steps",
        "model_metrics",
        "trace_ref",
        "screenshot_ref",
        "error_code",
        "detail",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        _reject("browser verification provider result schema is invalid")
    outcome = payload.get("result")
    if outcome not in OUTCOMES:
        _reject("browser verification provider outcome is invalid")
    actual = payload.get("actual_browser_process")
    if not isinstance(actual, bool):
        _reject("browser verification browser process flag is invalid")
    steps = payload.get("steps")
    if (
        not isinstance(steps, list)
        or len(steps) > _MAX_STEPS
    ):
        _reject("browser verification steps are invalid")
    normalized_steps = [_validate_step(item) for item in steps]
    if outcome == "PASS" and (
        not actual
        or not normalized_steps
        or any(item["outcome"] != "PASS" for item in normalized_steps)
    ):
        _reject("browser verification PASS lacks actual-browser PASS evidence")
    return {
        "result": outcome,
        "browser_engine": _safe_text(
            payload.get("browser_engine"),
            label="browser_engine",
            maximum=64,
        ),
        "browser_version": _safe_text(
            payload.get("browser_version"),
            label="browser_version",
            maximum=128,
        ),
        "actual_browser_process": actual,
        "duration_ms": _bounded_int(
            payload.get("duration_ms"),
            label="duration_ms",
            maximum=60 * 60 * 1000,
        ),
        "steps": normalized_steps,
        "model_metrics": _validate_model_metrics(
            payload.get("model_metrics")
        ),
        "trace_ref": _evidence_ref(
            payload.get("trace_ref"), label="trace_ref"
        ),
        "screenshot_ref": _evidence_ref(
            payload.get("screenshot_ref"), label="screenshot_ref"
        ),
        "error_code": (
            None
            if payload.get("error_code") is None
            else _identity(payload.get("error_code"), label="error_code")
        ),
        "detail": _safe_text(payload.get("detail"), label="detail"),
    }


def validate_browser_verification_result(
    payload: object,
) -> dict[str, object]:
    expected = {
        "schema_version",
        "kind",
        "authority",
        "deterministic_gate_override",
        "run_id",
        "scenario_id",
        "provider",
        "target_url",
        "environment",
        "source_revision",
        "work_packet_issue",
        "variation",
        "result",
        "browser",
        "duration_ms",
        "steps",
        "model_metrics",
        "trace_ref",
        "screenshot_ref",
        "error_code",
        "detail",
        "result_digest",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        _reject("browser verification result schema is invalid")
    if (
        isinstance(payload.get("schema_version"), bool)
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("kind") != RESULT_KIND
        or payload.get("authority") != AUTHORITY
        or payload.get("deterministic_gate_override") != "NONE"
    ):
        _reject("browser verification result authority is invalid")
    request_part = {
        "schema_version": SCHEMA_VERSION,
        "kind": REQUEST_KIND,
        "run_id": payload.get("run_id"),
        "scenario_id": payload.get("scenario_id"),
        "provider": payload.get("provider"),
        "target_url": payload.get("target_url"),
        "environment": payload.get("environment"),
        "source_revision": payload.get("source_revision"),
        "work_packet_issue": payload.get("work_packet_issue"),
        "variation": payload.get("variation"),
    }
    validate_browser_verification_request(request_part)
    browser = payload.get("browser")
    if not isinstance(browser, dict) or set(browser) != {
        "engine", "version", "mode", "actual_process"
    }:
        _reject("browser verification browser schema is invalid")
    if browser.get("mode") not in {"HEADLESS", "HEADED", "NONE"}:
        _reject("browser verification browser mode is invalid")
    if not isinstance(browser.get("actual_process"), bool):
        _reject("browser verification browser actual_process is invalid")
    _safe_text(browser.get("engine"), label="browser engine", maximum=64)
    _safe_text(browser.get("version"), label="browser version", maximum=128)
    raw = {
        "result": payload.get("result"),
        "browser_engine": browser.get("engine"),
        "browser_version": browser.get("version"),
        "actual_browser_process": browser.get("actual_process"),
        "duration_ms": payload.get("duration_ms"),
        "steps": payload.get("steps"),
        "model_metrics": payload.get("model_metrics"),
        "trace_ref": payload.get("trace_ref"),
        "screenshot_ref": payload.get("screenshot_ref"),
        "error_code": payload.get("error_code"),
        "detail": payload.get("detail"),
    }
    _validate_raw_provider_result(raw)
    digest = payload.get("result_digest")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        _reject("browser verification result_digest is invalid")
    body = {key: value for key, value in payload.items() if key != "result_digest"}
    if _canonical_digest(body) != digest:
        _reject("browser verification result digest mismatch")
    return dict(payload)


_RUNTIME_ENV_KEYS = frozenset({
    "HOME",
    "TMPDIR",
    "TMP",
    "TEMP",
    "LD_LIBRARY_PATH",
    "PLAYWRIGHT_BROWSERS_PATH",
})
_FIXED_PROVIDER_PATH = "/usr/local/bin:/usr/bin:/bin"


def _runtime_environment(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not set(value).issubset(_RUNTIME_ENV_KEYS):
        _reject("browser verification runtime environment is invalid")
    result: dict[str, str] = {}
    for key, raw in value.items():
        if (
            not isinstance(raw, str)
            or not raw
            or len(raw) > 4096
            or any(ord(char) < 32 or ord(char) == 127 for char in raw)
            or contains_unsafe_secret(raw)
        ):
            _reject("browser verification runtime environment is invalid")
        result[str(key)] = raw
    return result


def _stagehand_api_key(value: object) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        _reject("browser verification Stagehand credential is invalid")
    return value


def _provider_environment(
    provider: str,
    *,
    runtime_env: object,
    stagehand_api_key: object,
    stagehand_model: object,
) -> dict[str, str]:
    env = {"PATH": _FIXED_PROVIDER_PATH}
    env.update(_runtime_environment(runtime_env))
    if provider != "stagehand":
        return env

    api_key = _stagehand_api_key(stagehand_api_key)
    if api_key is not None:
        env["OPENAI_API_KEY"] = api_key
    if stagehand_model is not None:
        env["ATLAS_STAGEHAND_MODEL"] = _identity(
            stagehand_model,
            label="Stagehand model",
        )
    return env


def _default_command_runner(
    argv: list[str],
    cwd: str,
    stdin_text: str,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        input=stdin_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=180,
        check=False,
        env=env,
    )

def _human_required_raw(code: str, detail: str) -> dict[str, object]:
    return {
        "result": "HUMAN_REQUIRED",
        "browser_engine": "UNKNOWN",
        "browser_version": "UNKNOWN",
        "actual_browser_process": False,
        "duration_ms": 0,
        "steps": [],
        "model_metrics": {
            "state": "UNAVAILABLE",
            "provider": None,
            "model": None,
            "llm_call_count": None,
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "cost_microusd": None,
        },
        "trace_ref": None,
        "screenshot_ref": None,
        "error_code": code,
        "detail": detail,
    }


def _provider_output(stdout: str) -> object:
    for line in reversed((stdout or "").splitlines()):
        if line.startswith(_RESULT_MARKER):
            try:
                return json.loads(line[len(_RESULT_MARKER):])
            except (ValueError, RecursionError) as exc:
                raise ValidationError(
                    "browser verification provider output is invalid JSON"
                ) from exc
    _reject("browser verification provider result marker is missing")


def run_browser_verification(
    request: object,
    *,
    repo_root: Path,
    command_runner: CommandRunner | None = None,
    runtime_env: dict[str, str] | None = None,
    stagehand_api_key: str | None = None,
    stagehand_model: str | None = None,
) -> dict[str, object]:
    normalized = validate_browser_verification_request(request)
    root = Path(repo_root).resolve()
    runner_path = root / "tools" / "browser-verification" / "runner.mjs"
    if not runner_path.is_file():
        _reject("browser verification optional runner is missing")
    runner = command_runner or _default_command_runner
    provider_env = _provider_environment(
        str(normalized["provider"]),
        runtime_env=runtime_env,
        stagehand_api_key=stagehand_api_key,
        stagehand_model=stagehand_model,
    )
    try:
        completed = runner(
            ["node", str(runner_path)],
            str(root),
            json.dumps(normalized, sort_keys=True),
            provider_env,
        )
    except (OSError, subprocess.SubprocessError):
        raw = _human_required_raw(
            "PROVIDER_PROCESS_UNAVAILABLE",
            "Optional browser provider process could not be executed.",
        )
    else:
        if completed.returncode != 0:
            raw = _human_required_raw(
                "PROVIDER_PROCESS_FAILED",
                "Optional browser provider process exited without trusted evidence.",
            )
        else:
            try:
                raw = _provider_output(completed.stdout)
            except ValidationError:
                raw = _human_required_raw(
                    "PROVIDER_OUTPUT_INVALID",
                    "Optional browser provider returned no valid bounded result.",
                )
    provider_result = _validate_raw_provider_result(raw)
    body = {
        "schema_version": SCHEMA_VERSION,
        "kind": RESULT_KIND,
        "authority": AUTHORITY,
        "deterministic_gate_override": "NONE",
        "run_id": normalized["run_id"],
        "scenario_id": normalized["scenario_id"],
        "provider": normalized["provider"],
        "target_url": normalized["target_url"],
        "environment": normalized["environment"],
        "source_revision": normalized["source_revision"],
        "work_packet_issue": normalized["work_packet_issue"],
        "variation": normalized["variation"],
        "result": provider_result["result"],
        "browser": {
            "engine": provider_result["browser_engine"],
            "version": provider_result["browser_version"],
            "mode": (
                "HEADLESS"
                if provider_result["actual_browser_process"]
                else "NONE"
            ),
            "actual_process": provider_result["actual_browser_process"],
        },
        "duration_ms": provider_result["duration_ms"],
        "steps": provider_result["steps"],
        "model_metrics": provider_result["model_metrics"],
        "trace_ref": provider_result["trace_ref"],
        "screenshot_ref": provider_result["screenshot_ref"],
        "error_code": provider_result["error_code"],
        "detail": provider_result["detail"],
    }
    result = {**body, "result_digest": _canonical_digest(body)}
    return validate_browser_verification_result(result)
