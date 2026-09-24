"""Shared secret detection/redaction for durable Atlas audit state.

Extracted from the Autonomous Work Controller hardened boundary so Chat Audit
and other Atlas surfaces reuse one fail-closed sanitizer instead of weaker
duplicate regexes.
"""

from __future__ import annotations

import re

from atlas.provenance import ValidationError

def _credential_name_pattern() -> str:
    """Shared credential-name alternation for assignment detection/redaction.

    The generic form is a bounded identifier that ends in a known suffix.
    An unbounded lazy prefix retries that suffix at every character and makes
    ordinary long text super-linear.
    """
    specific = (
        r"OPENAI_API_KEY|GITHUB_TOKEN|GH_TOKEN|"
        r"AWS_SECRET_ACCESS_KEY|AWS_ACCESS_KEY_ID|"
        r"AZURE_CLIENT_SECRET|NPM_TOKEN|"
        # Standalone names first so bare `password` / `secret` / `token` /
        # camelCase `apiKey` and hyphenated `api-key` / `X-API-Key` match.
        r"PASSWORD|SECRET|TOKEN|API_KEY|ACCESS_KEY_ID|ACCESS_KEY|apiKey|"
        r"X-API-Key|API-Key"
    )
    generic = (
        r"(?<![A-Za-z0-9_-])"
        r"[A-Za-z_]"
        r"[A-Za-z0-9_-]{0,80}?"
        r"(?:ACCESS_KEY_ID|API_KEY|ACCESS_KEY|PASSWORD|SECRET|TOKEN|ApiKey|API-Key)"
        r"(?![A-Za-z0-9_-])"
    )
    return rf"(?:(?:{specific})|(?:{generic}))"


def _normalize_json_quote_escapes(text: str) -> str:
    """Peel nested ``json.dumps`` quote-escapes in linear time.

    Collapses ``\\\\\"`` runs produced by repeated serialization into plain
    quotes so detectors stay linear-time, while preserving a single ``\\\"``
    escape inside credential values.
    """
    cur = text or ""
    prev = None
    while prev != cur:
        prev = cur
        cur = cur.replace('\\\\"', '"').replace("\\\\'", "'")
    return cur


def _is_colon_type_or_prose_value(value: str) -> bool:
    """True for type annotations / short prose, not credential-like colon values.

    Distinguishes ``token: str`` from ``access_token: bare-secret-value-12345``.
    """
    v = (value or "").strip()
    if not v:
        return True
    if re.fullmatch(
        r"(?:str|int|float|bool|bytes|None|True|False|Any|Optional|"
        r"List|Dict|Set|Tuple|Mapping|Sequence|Callable|Iterable|Iterator|"
        r"object|type|list|dict|set|tuple|[A-Z][A-Za-z0-9_]*)",
        v,
    ):
        return True
    # Short plain identifiers without digits/punctuation are type/prose, not secrets.
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", v) and len(v) < 16:
        return True
    return False


def _looks_like_secret(text: str) -> bool:
    """Detect likely live credentials, not mere documentation mentions."""
    name = _credential_name_pattern()
    scan = _normalize_json_quote_escapes(text)
    # Quoted values may contain whitespace/commas and the opposite quote
    # character; only the selected delimiter ends the value (escapes allowed).
    # Nested dumps are peeled first; at most one JSON ``\\\"`` remains.
    key_q = r'((?:\\?["\'])?)'
    val_q = r'(\\?["\'])'
    if re.search(
        rf'(?i){key_q}({name})\1\s*[:=]\s*{val_q}((?:\\.|(?!\3).)*)\3',
        scan,
    ):
        return True
    # Bare equals assignments; values may include internal whitespace.
    if re.search(
        rf'(?i){key_q}({name})\1\s*=\s*([^\n\r,"\'}}\]]+(?:\s+[^\n\r,"\'}}\]]+)*)',
        scan,
    ):
        return True
    # Bare colon: skip type annotations / short prose (``token: str``).
    for match in re.finditer(
        rf'(?i){key_q}({name})\1\s*:\s*([^\n\r,"\'}}\]]+(?:\s+[^\n\r,"\'}}\]]+)*)',
        scan,
    ):
        first_token = match.group(3).split()[0] if match.group(3).strip() else ""
        if not _is_colon_type_or_prose_value(first_token):
            return True
        # Multi-token unquoted colon values are never type annotations.
        if len(match.group(3).split()) > 1:
            return True
    if re.search(r"\bsk-[A-Za-z0-9]{20,}\b", scan):
        return True
    if re.search(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b", scan):
        return True
    if re.search(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b", scan):
        return True
    if re.search(r"\bAKIA[0-9A-Z]{16}\b", scan):
        return True
    if re.search(r"(?i)Bearer\s+[A-Za-z0-9\-._~+/]+=*", scan):
        return True
    if re.search(r"(?i)\bBasic\s+[A-Za-z0-9+/_-]{4,}={0,2}(?![A-Za-z0-9+/_-])", scan):
        return True
    if _URL_USERINFO_RE.search(scan):
        return True
    if _PEM_PRIVATE_KEY_RE.search(text or ""):
        return True
    return False


# Multi-segment absolute POSIX/Windows paths. Lookbehind excludes URL authorities
# (`https://...`) by rejecting a match that starts immediately after `:` or `/`.
_ABS_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9:/])(/(?:[^/\s\"'`]{1,255}/){1,}[^/\s\"'`]{1,255})"
    r"|([A-Za-z]:\\(?:[^\\\s\"'`]+\\)+[^\\\s\"'`]+)"
)
_URL_RE = re.compile(r"https?://[^\s\"'`]+", re.IGNORECASE)
# Credential-bearing URI userinfo (any scheme), e.g. postgresql://u:p@host/db.
_URL_USERINFO_RE = re.compile(
    r"(?i)(?P<scheme>\b[a-z][a-z0-9+.-]*://)(?P<userinfo>[^/\s\"'`]+@)"
)
_PEM_PRIVATE_KEY_RE = re.compile(
    # Full PEM label grammar for private keys: optional hyphenated tokens
    # before "PRIVATE KEY", with a matching END label.
    r"-----BEGIN ((?:[A-Z0-9][A-Z0-9-]* )*)PRIVATE KEY-----"
    r"[\s\S]*?"
    r"-----END \1PRIVATE KEY-----"
)

_CREDENTIAL_NAME = _credential_name_pattern()
# Quoted value first so whitespace/commas and the opposite quote inside the
# selected delimiter are fully captured (including escaped delimiters).
# Bound escape depth (no ``\\\\*``) to keep redaction linear-time; detection
# normalizes deeper nesting before matching.
_SECRET_KV_QUOTED_RE = re.compile(
    rf'(?i)(?P<kq>(?:(?:\\){{0,8}}["\'])?)(?P<key>{_CREDENTIAL_NAME})(?P=kq)'
    rf'\s*[:=]\s*(?P<vq>(?:\\){{0,8}}["\'])(?P<val>(?:\\.|(?!(?P=vq))[\s\S])*)(?P=vq)'
)
# Bare key=value / key:value. Unquoted values may include internal whitespace;
# stop only at newline or JSON/shell structural delimiters so suffixes like
# ``PASSWORD=hunter2 more-secret`` cannot leak past the first token.
_SECRET_KV_BARE_RE = re.compile(
    rf'(?i)(?P<kq>(?:(?:\\){{0,8}}["\'])?)(?P<key>{_CREDENTIAL_NAME})(?P=kq)'
    rf'\s*[:=]\s*(?P<val>[^\n\r,"\'}}\]]+(?:\s+[^\n\r,"\'}}\]]+)*)'
)
_SECRET_TOKEN_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})\b"
)
_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE)
_BASIC_AUTH_RE = re.compile(
    r"\bBasic\s+[A-Za-z0-9+/_-]{4,}={0,2}(?![A-Za-z0-9+/_-])",
    re.IGNORECASE,
)


def redact_absolute_paths(text: str) -> str:
    """Replace host-local absolute paths before GitHub Work Packet persistence.

    Canonical ``http(s)://`` URLs are preserved; only filesystem paths are
    replaced with ``<local-path>``.
    """
    urls: list[str] = []

    def _park_url(match: re.Match[str]) -> str:
        urls.append(match.group(0))
        return f"__URL_{len(urls) - 1}__"

    protected = _URL_RE.sub(_park_url, text or "")
    protected = _ABS_PATH_RE.sub("<local-path>", protected)
    for index, url in enumerate(urls):
        protected = protected.replace(f"__URL_{index}__", url)
    return protected


def _redact_secret_kv(match: re.Match[str]) -> str:
    """Preserve credential key syntax; replace only the secret value."""
    key_q = match.group("kq") or ""
    key = match.group("key")
    val_q = match.groupdict().get("vq") or ""
    after_key = match.group(0)[len(key_q) + len(key) + len(key_q) :]
    separator = ":" if after_key.lstrip().startswith(":") else "="
    if separator == ":" and not val_q:
        bare_val = match.groupdict().get("val") or ""
        first_token = bare_val.split()[0] if bare_val.strip() else ""
        # Preserve true type annotations (``token: str``) only; multi-token
        # unquoted values are treated as secrets.
        if len(bare_val.split()) <= 1 and _is_colon_type_or_prose_value(first_token):
            return match.group(0)
    return f"{key_q}{key}{key_q}{separator}{val_q}<redacted>{val_q}"


def redact_sensitive_audit_text(text: str, *, max_chars: int = 300) -> str:
    """Redact secrets and absolute paths for durable AuditResult findings."""
    cleaned = redact_absolute_paths(text or "")
    cleaned = _PEM_PRIVATE_KEY_RE.sub("<redacted-private-key>", cleaned)
    # Normalize nested JSON quote-escapes before KV matching so deep dumps
    # still redact without unbounded backtracking.
    cleaned = _normalize_json_quote_escapes(cleaned)
    cleaned = _SECRET_KV_QUOTED_RE.sub(_redact_secret_kv, cleaned)
    cleaned = _SECRET_KV_BARE_RE.sub(_redact_secret_kv, cleaned)
    cleaned = _SECRET_TOKEN_RE.sub("<redacted>", cleaned)
    cleaned = _BEARER_RE.sub("Bearer <redacted>", cleaned)
    cleaned = _BASIC_AUTH_RE.sub("Basic <redacted>", cleaned)
    cleaned = _URL_USERINFO_RE.sub(r"\g<scheme><redacted>@", cleaned)
    cleaned = cleaned.strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[:max_chars]


def _strip_safe_redaction_placeholders(text: str) -> str:
    """Remove exact safe redaction markers before secret classification.

    Assignments like ``OPENAI_API_KEY=<redacted>``, ``Bearer <redacted>``, and
    ``<redacted-private-key>`` are intentional sanitizer output and must not
    trip the Codex prompt guard or packet persistence rejector.
    """
    cleaned = _normalize_json_quote_escapes(text)
    name = _credential_name_pattern()
    # Value must end at the placeholder. JSON ``"key":`` after a comma is only
    # accepted for colon assignments (not shell ``PASSWORD="...", "x":``).
    key_q = r'((?:\\?["\'])?)'
    val_q = r'(\\?["\'])'
    common_end = (
        r'$|\s|\\["n]'
        r'|[\}\]](?=$|[\s,\}\]]|\\["n])'
        r'|\\?["\'](?=$|[\s,\}\]]|\\["n])'
    )
    value_end_eq = (
        rf'(?={common_end}|,(?=$|[\s\}}]|\\["n]))'
    )
    value_end_colon = (
        rf'(?={common_end}|,(?:$|[\s\}}]|\\["n]|\\?["\'][^"\']+?\\?["\']\s*:))'
    )
    for sep, value_end in (("=", value_end_eq), (":", value_end_colon)):
        cleaned = re.sub(
            rf'(?i){key_q}({name})\1\s*{re.escape(sep)}\s*{val_q}<redacted>\3'
            rf'{value_end}',
            "",
            cleaned,
        )
        cleaned = re.sub(
            rf'(?i){key_q}({name})\1\s*{re.escape(sep)}\s*<redacted>{value_end}',
            "",
            cleaned,
        )
    cleaned = re.sub(
        rf'(?i)Bearer\s+<redacted>(?={common_end}|,(?=$|[\s\}}]|\\["n]))',
        "",
        cleaned,
    )
    cleaned = re.sub(
        rf'(?i)Basic\s+<redacted>(?={common_end}|,(?=$|[\s\}}]|\\["n]))',
        "",
        cleaned,
    )
    # Safe URL form after userinfo redaction: scheme://<redacted>@host...
    cleaned = re.sub(
        r"(?i)\b[a-z][a-z0-9+.-]*://<redacted>@",
        "",
        cleaned,
    )
    cleaned = cleaned.replace("<redacted-private-key>", "")
    return cleaned


def _contains_unsafe_secret(text: str) -> bool:
    """True when text still looks like a live credential after safe markers."""
    cleaned = _strip_safe_redaction_placeholders(text)
    if _looks_like_secret(cleaned):
        return True
    # Any ``Bearer <redacted>`` that survived stripping still has a live suffix
    # (for example ``Bearer <redacted>,hunter2`` or ``Bearer <redacted>"hunter2"``).
    if re.search(r"(?i)Bearer\s+<redacted>", cleaned):
        return True
    if re.search(r"(?i)Basic\s+<redacted>", cleaned):
        return True
    return False


def contains_unsafe_secret(text: str) -> bool:
    """Public alias for durable-state secret classification."""
    return _contains_unsafe_secret(text)


def sanitize_durable_text(text: str, *, max_chars: int = 4000) -> str:
    """Redact secrets for durable persistence; fail closed if unsafe residue remains.

    Over-limit raw text is rejected before scanning so a cut-off assignment
    cannot be persisted as an ordinary truncated field.
    """
    raw = text or ""
    if len(raw) > max_chars:
        raise ValidationError(
            f"durable text exceeds {max_chars} characters; "
            "refusing to persist a truncated field"
        )
    cleaned = redact_sensitive_audit_text(raw, max_chars=max_chars)
    if contains_unsafe_secret(cleaned):
        raise ValidationError(
            "refusing to persist text that still looks like secrets after redaction"
        )
    return cleaned
