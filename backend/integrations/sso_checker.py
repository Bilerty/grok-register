#!/usr/bin/env python3
"""Reusable, object-oriented SSO account-state checker.

The public API is intentionally small::

    from sso_checker import SsoChecker, SsoCredential

    checker = SsoChecker()
    result = checker.check(
        SsoCredential(sso_token="TOKEN", expected_email="name@example.com")
    )
    print(result.account.email, result.bot_flag.source, result.verdict.value)

The SSO token is used only to build the request session. It is excluded from
result objects, ``repr()``, JSON output, progress callbacks, and batch reports.

Runtime dependency for live requests:

    pip install curl_cffi>=0.13.0

The module itself can still be imported without curl_cffi, which makes the
data models and :class:`SsoPageParser` usable in lightweight projects/tests.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit
from zipfile import ZipFile

try:
    from curl_cffi import requests as curl_requests
except ImportError:  # The parser/data classes remain importable without HTTP extras.
    curl_requests = None


__all__ = [
    "AccountInfo",
    "BatchCheckReport",
    "BotFlagInfo",
    "JwtInfo",
    "ParsedPage",
    "SsoCheckConfig",
    "SsoCheckResult",
    "SsoChecker",
    "SsoCredential",
    "SsoCredentialLoader",
    "SsoPageParser",
    "SsoVerdict",
]


DEFAULT_HOME_URL = "https://grok.com/"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)
DEFAULT_COOKIE_DOMAINS = (
    ".x.ai",
    "accounts.x.ai",
    "auth.x.ai",
    ".grok.com",
    "grok.com",
)


def _utc_iso(timestamp: float | int | None = None) -> str:
    value = time.time() if timestamp is None else float(timestamp)
    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _safe_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_url(value: Any) -> str:
    """Keep request diagnostics while dropping query strings and fragments."""

    raw = _clean_text(value)
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    except ValueError:
        return ""


class SsoVerdict(str, Enum):
    """High-level result classification."""

    CLEAN = "clean"
    FLAGGED = "flagged"
    EMAIL_MISMATCH = "email_mismatch"
    FLAGGED_EMAIL_MISMATCH = "flagged_email_mismatch"
    INVALID_OR_UNKNOWN = "invalid_or_unknown"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class SsoCredential:
    """One SSO input.

    ``sso_token`` is deliberately hidden from ``repr``. ``metadata`` is for a
    caller-owned correlation ID or source label; it is copied to the result
    only when explicitly requested through :meth:`SsoCheckResult.to_dict`.
    """

    sso_token: str = field(repr=False)
    expected_email: str = ""
    label: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "sso_token", _clean_text(self.sso_token))
        object.__setattr__(self, "expected_email", _clean_text(self.expected_email))
        object.__setattr__(self, "label", _clean_text(self.label))


@dataclass(frozen=True, slots=True)
class JwtInfo:
    """Non-sensitive structural information about the SSO JWT."""

    valid: bool = False
    claim_keys: tuple[str, ...] = ()
    has_session_id: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "claim_keys": list(self.claim_keys),
            "has_session_id": self.has_session_id,
        }


@dataclass(frozen=True, slots=True)
class BotFlagInfo:
    """Parsed bot/risk flag information."""

    found: bool = False
    source: int | None = None
    details: str = ""
    policy: str = ""
    risk: float | None = None
    event: str = ""
    denied: bool = False

    def is_flagged(self, _flagged_sources: frozenset[int] = frozenset()) -> bool:
        """Treat zero as clean and every non-zero botFlagSource as flagged.

        ``flagged_sources`` is retained for source-file API compatibility, but
        the current rule is unconditional: zero is clean and non-zero is risk.
        """
        if self.source is None or self.source == 0:
            return False
        return True

    def to_dict(self, flagged_sources: frozenset[int] = frozenset()) -> dict[str, Any]:
        return {
            "found": self.found,
            "source": self.source,
            "details": self.details,
            "policy": self.policy,
            "risk": self.risk,
            "event": self.event,
            "denied": self.denied,
            "flagged": self.is_flagged(flagged_sources),
        }


@dataclass(frozen=True, slots=True)
class AccountInfo:
    """Account fields embedded in the authenticated home-page payload."""

    email: str = ""
    user_id: str = ""
    given_name: str = ""
    family_name: str = ""
    email_confirmed: bool | None = None
    session_tier_id: str = ""
    x_subscription_type: str = ""
    x_user_id: str = ""
    x_username: str = ""
    country_code: str = ""
    region: str = ""
    region_code: str = ""
    organization_id: str = ""
    organization_type: int | None = None
    tos_accepted_version: int | None = None
    create_time: int | None = None

    @property
    def display_name(self) -> str:
        return " ".join(part for part in (self.given_name, self.family_name) if part)

    @property
    def created_at(self) -> str:
        return _utc_iso(self.create_time) if self.create_time else ""

    def account_age_days(self, now: float | int | None = None) -> float | None:
        if not self.create_time:
            return None
        current = time.time() if now is None else float(now)
        return round((current - self.create_time) / 86400, 2)

    def to_dict(self, *, now: float | int | None = None) -> dict[str, Any]:
        return {
            "email": self.email,
            "user_id": self.user_id,
            "given_name": self.given_name,
            "family_name": self.family_name,
            "display_name": self.display_name,
            "email_confirmed": self.email_confirmed,
            "session_tier_id": self.session_tier_id,
            "x_subscription_type": self.x_subscription_type,
            "x_user_id": self.x_user_id,
            "x_username": self.x_username,
            "country_code": self.country_code,
            "region": self.region,
            "region_code": self.region_code,
            "organization_id": self.organization_id,
            "organization_type": self.organization_type,
            "tos_accepted_version": self.tos_accepted_version,
            "create_time": self.create_time,
            "created_at": self.created_at,
            "account_age_days": self.account_age_days(now),
        }


@dataclass(frozen=True, slots=True)
class ParsedPage:
    """Structured parser output; usually consumed through :class:`SsoChecker`."""

    profile_found: bool = False
    state_found: bool = False
    account: AccountInfo = field(default_factory=AccountInfo)
    bot_flag: BotFlagInfo = field(default_factory=BotFlagInfo)
    parse_mode: str = "none"


@dataclass(frozen=True, slots=True)
class SsoCheckConfig:
    """Runtime and verdict configuration for :class:`SsoChecker`."""

    home_url: str = DEFAULT_HOME_URL
    timeout: int = 20
    proxy: str = ""
    impersonate: str = "chrome"
    user_agent: str = DEFAULT_USER_AGENT
    accept: str = "text/html,application/xhtml+xml"
    accept_language: str = "en-US,en;q=0.9"
    cookie_names: tuple[str, ...] = ("sso", "sso-rw")
    cookie_domains: tuple[str, ...] = DEFAULT_COOKIE_DOMAINS
    # 保留来源脚本的配置字段以兼容调用方；当前规则固定为 0 正常、非 0 异常。
    flagged_sources: frozenset[int] = field(default_factory=frozenset)
    allow_redirects: bool = True
    verify_tls: bool = True

    def __post_init__(self) -> None:
        if not self.home_url.startswith(("http://", "https://")):
            raise ValueError("home_url must be an absolute HTTP(S) URL")
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")


@dataclass(frozen=True, slots=True)
class SsoCheckResult:
    """Credential-free result returned by :meth:`SsoChecker.check`."""

    label: str = ""
    expected_email: str = ""
    status_code: int = 0
    final_url: str = ""
    valid_session: bool = False
    email_match: bool | None = None
    account: AccountInfo = field(default_factory=AccountInfo)
    bot_flag: BotFlagInfo = field(default_factory=BotFlagInfo)
    jwt: JwtInfo = field(default_factory=JwtInfo)
    verdict: SsoVerdict = SsoVerdict.ERROR
    parse_mode: str = "none"
    error: str = ""
    response_ms: int = 0
    checked_at: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def ok(self) -> bool:
        return self.valid_session and self.verdict not in {
            SsoVerdict.ERROR,
            SsoVerdict.INVALID_OR_UNKNOWN,
        }

    @property
    def is_flagged(self) -> bool:
        return self.verdict in {
            SsoVerdict.FLAGGED,
            SsoVerdict.FLAGGED_EMAIL_MISMATCH,
        }

    def to_dict(
        self,
        *,
        include_metadata: bool = False,
        flagged_sources: frozenset[int] = frozenset(),
    ) -> dict[str, Any]:
        """Return a JSON-ready mapping that never contains the SSO token."""

        data: dict[str, Any] = {
            "label": self.label,
            "expected_email": self.expected_email,
            "status_code": self.status_code,
            "final_url": self.final_url,
            "valid_session": self.valid_session,
            "email_match": self.email_match,
            "verdict": self.verdict.value,
            "parse_mode": self.parse_mode,
            "error": self.error,
            "response_ms": self.response_ms,
            "checked_at": self.checked_at,
            "jwt": self.jwt.to_dict(),
            "account": self.account.to_dict(),
            "bot_flag": self.bot_flag.to_dict(flagged_sources),
        }
        if include_metadata:
            data["metadata"] = dict(self.metadata)
        return data

    def to_flat_dict(self) -> dict[str, Any]:
        """Return a CSV/database-friendly flat representation."""

        account = self.account.to_dict()
        bot = self.bot_flag.to_dict()
        return {
            "label": self.label,
            "expected_email": self.expected_email,
            "server_email": account["email"],
            "email_match": self.email_match,
            "display_name": account["display_name"],
            "given_name": account["given_name"],
            "family_name": account["family_name"],
            "user_id": account["user_id"],
            "email_confirmed": account["email_confirmed"],
            "session_tier_id": account["session_tier_id"],
            "x_subscription_type": account["x_subscription_type"],
            "country_code": account["country_code"],
            "region": account["region"],
            "region_code": account["region_code"],
            "created_at": account["created_at"],
            "account_age_days": account["account_age_days"],
            "bot_flag_source": bot["source"],
            "bot_flag_details": bot["details"],
            "risk": bot["risk"],
            "policy": bot["policy"],
            "event": bot["event"],
            "denied": bot["denied"],
            "status_code": self.status_code,
            "valid_session": self.valid_session,
            "verdict": self.verdict.value,
            "error": self.error,
            "response_ms": self.response_ms,
            "checked_at": self.checked_at,
        }


@dataclass(frozen=True, slots=True)
class BatchCheckReport:
    """Ordered results and aggregate counts for a batch check."""

    results: tuple[SsoCheckResult, ...]
    started_at: str
    finished_at: str
    elapsed_seconds: float

    @property
    def summary(self) -> dict[str, Any]:
        verdicts = Counter(result.verdict.value for result in self.results)
        flag_sources = Counter(
            str(result.bot_flag.source) if result.bot_flag.source is not None else "none"
            for result in self.results
        )
        total = len(self.results)
        valid = sum(result.valid_session for result in self.results)
        flagged = sum(result.is_flagged for result in self.results)
        return {
            "total": total,
            "valid_sessions": valid,
            "invalid_or_errors": total - valid,
            "email_matches": sum(result.email_match is True for result in self.results),
            "email_mismatches": sum(result.email_match is False for result in self.results),
            "flagged": flagged,
            "clean": verdicts.get(SsoVerdict.CLEAN.value, 0),
            "valid_rate": round(100 * valid / total, 2) if total else 0.0,
            "flagged_rate": round(100 * flagged / valid, 2) if valid else 0.0,
            "verdict_distribution": dict(sorted(verdicts.items())),
            "bot_flag_distribution": dict(sorted(flag_sources.items())),
        }

    def to_dict(self, *, include_metadata: bool = False) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_seconds": self.elapsed_seconds,
            "summary": self.summary,
            "results": [
                result.to_dict(include_metadata=include_metadata) for result in self.results
            ],
        }


class SsoPageParser:
    """Parse account data and botFlag fields from the authenticated HTML/RSC page."""

    INITIAL_DATA_MARKER = '"initialData":'

    @staticmethod
    def normalize(page_html: str) -> str:
        # Next.js embeds RSC JSON inside a JS string. Removing one escaping
        # layer matches the actual JSON object present in current responses.
        return str(page_html or "").replace('\\"', '"')

    def parse(self, page_html: str) -> ParsedPage:
        normalized = self.normalize(page_html)
        initial = self._find_initial_data(normalized)
        if initial is not None:
            user = initial.get("user") or {}
            account = AccountInfo(
                email=_clean_text(user.get("email")),
                user_id=_clean_text(user.get("userId")),
                given_name=_clean_text(user.get("givenName")),
                family_name=_clean_text(user.get("familyName")),
                email_confirmed=(
                    user.get("emailConfirmed")
                    if isinstance(user.get("emailConfirmed"), bool)
                    else None
                ),
                session_tier_id=_clean_text(user.get("sessionTierId")),
                x_subscription_type=_clean_text(user.get("xSubscriptionType")),
                x_user_id=_clean_text(user.get("xUserId")),
                x_username=_clean_text(user.get("xUsername")),
                country_code=_clean_text(initial.get("countryCode")),
                region=_clean_text(initial.get("region")),
                region_code=_clean_text(initial.get("regionCode")),
                organization_id=_clean_text(user.get("organizationId")),
                organization_type=_safe_int(user.get("organizationType")),
                tos_accepted_version=_safe_int(user.get("tosAcceptedVersion")),
                create_time=_safe_int(user.get("createTime")),
            )
            bot_flag = self._bot_flag_from_mapping(user)
            profile_found = bool(
                account.email or account.user_id or _clean_text(user.get("sessionId"))
            )
            return ParsedPage(
                profile_found=profile_found,
                state_found=bot_flag.found,
                account=account,
                bot_flag=bot_flag,
                parse_mode="initial_data",
            )

        # Compatibility fallback: still expose state fields if the page layout
        # changes, but do not call the session valid without a user profile.
        fallback = self._bot_flag_from_text(normalized)
        return ParsedPage(
            profile_found=False,
            state_found=fallback.found,
            bot_flag=fallback,
            parse_mode="bot_flag_fallback" if fallback.found else "none",
        )

    def _find_initial_data(self, normalized: str) -> dict[str, Any] | None:
        position = normalized.find(self.INITIAL_DATA_MARKER)
        decoder = json.JSONDecoder()
        while position >= 0:
            start = position + len(self.INITIAL_DATA_MARKER)
            try:
                value, _ = decoder.raw_decode(normalized[start:])
                if isinstance(value, dict) and isinstance(value.get("user"), dict):
                    return value
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
            position = normalized.find(self.INITIAL_DATA_MARKER, start)
        return None

    def _bot_flag_from_mapping(self, user: Mapping[str, Any]) -> BotFlagInfo:
        found = "botFlagSource" in user or "botFlagDetails" in user
        source = _safe_int(user.get("botFlagSource"))
        details = _clean_text(user.get("botFlagDetails"))
        return self._build_bot_flag(found=found, source=source, details=details)

    def _bot_flag_from_text(self, normalized: str) -> BotFlagInfo:
        source_match = re.search(r'botFlagSource"\s*:\s*(null|-?\d+)', normalized)
        details_match = re.search(
            r'botFlagDetails"\s*:\s*(?:null|"((?:\\.|[^"\\])*)")',
            normalized,
        )
        source = None
        if source_match and source_match.group(1) != "null":
            source = _safe_int(source_match.group(1))
        details = ""
        if details_match and details_match.group(1):
            try:
                details = json.loads(f'"{details_match.group(1)}"')
            except (TypeError, ValueError, json.JSONDecodeError):
                details = details_match.group(1)
        return self._build_bot_flag(
            found=bool(source_match or details_match),
            source=source,
            details=details,
        )

    @staticmethod
    def _build_bot_flag(*, found: bool, source: int | None, details: str) -> BotFlagInfo:
        def field_value(name: str) -> str:
            match = re.search(
                rf"(?:^|,)\s*{re.escape(name)}\s*=\s*([^,]+)",
                details,
                re.I,
            )
            return match.group(1).strip() if match else ""

        policy = field_value("policy").lower()
        event = field_value("event")
        risk = None
        risk_value = field_value("risk")
        try:
            risk = float(risk_value) if risk_value else None
        except ValueError:
            risk = None
        denied = policy == "deny" and event == "$registration"
        return BotFlagInfo(
            found=found,
            source=source,
            details=details,
            policy=policy,
            risk=risk,
            event=event,
            denied=denied,
        )


SessionFactory = Callable[[], Any]
ProgressCallback = Callable[[int, int, SsoCheckResult], None]


class SsoChecker:
    """Check one or many SSO sessions.

    A new HTTP session is created for every credential, so one checker instance
    can safely be shared by :meth:`check_many` worker threads.

    ``session_factory`` is injectable for tests or for projects that wrap
    ``curl_cffi`` with their own observability/transport layer.
    """

    def __init__(
        self,
        config: SsoCheckConfig | None = None,
        *,
        parser: SsoPageParser | None = None,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self.config = config or SsoCheckConfig()
        self.parser = parser or SsoPageParser()
        self._session_factory = session_factory

    @staticmethod
    def inspect_jwt(sso_token: str) -> JwtInfo:
        """Decode only JWT structure/claim names; token values are discarded."""

        try:
            parts = _clean_text(sso_token).split(".")
            if len(parts) != 3:
                return JwtInfo()
            payload_segment = parts[1] + "=" * (-len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_segment))
            if not isinstance(payload, dict):
                return JwtInfo()
            return JwtInfo(
                valid=True,
                claim_keys=tuple(sorted(str(key) for key in payload)),
                has_session_id=bool(payload.get("session_id")),
            )
        except Exception:
            return JwtInfo()

    def check(
        self,
        credential: SsoCredential | str,
        *,
        expected_email: str | None = None,
        label: str | None = None,
    ) -> SsoCheckResult:
        """Check one SSO token and return a credential-free result."""

        item = self._coerce_credential(
            credential,
            expected_email=expected_email,
            label=label,
        )
        started = time.perf_counter()
        checked_at = _utc_iso()
        jwt = self.inspect_jwt(item.sso_token)
        base = {
            "label": item.label,
            "expected_email": item.expected_email,
            "jwt": jwt,
            "checked_at": checked_at,
            "metadata": dict(item.metadata),
        }
        if not item.sso_token:
            return SsoCheckResult(
                **base,
                verdict=SsoVerdict.ERROR,
                error="SSO token is empty",
                response_ms=round((time.perf_counter() - started) * 1000),
            )

        try:
            session = self._new_session(item.sso_token)
            response = self._get_home(session, include_user_agent=True)
            status_code = int(getattr(response, "status_code", 0) or 0)
            if status_code in {403, 429, 503} and self.config.user_agent:
                # A fixed UA can drift from curl_cffi's current TLS fingerprint.
                # Retry with a fresh session and its impersonation-matched UA.
                session = self._new_session(item.sso_token)
                response = self._get_home(session, include_user_agent=False)
                status_code = int(getattr(response, "status_code", 0) or 0)
                base["metadata"]["edge_header_retry"] = True
            final_url = _safe_url(getattr(response, "url", ""))
            elapsed_ms = round((time.perf_counter() - started) * 1000)
            if status_code != 200:
                suffix = " (edge/proxy limit possible)" if status_code in {403, 429, 503} else ""
                return SsoCheckResult(
                    **base,
                    status_code=status_code,
                    final_url=final_url,
                    verdict=SsoVerdict.ERROR,
                    error=f"HTTP {status_code}{suffix}",
                    response_ms=elapsed_ms,
                )

            parsed = self.parser.parse(_clean_text(getattr(response, "text", "")))
            if not parsed.profile_found:
                error = "Authenticated account profile was not found in page data"
                if parsed.state_found:
                    error += "; botFlag fallback was parsed"
                return SsoCheckResult(
                    **base,
                    status_code=status_code,
                    final_url=final_url,
                    valid_session=False,
                    account=parsed.account,
                    bot_flag=parsed.bot_flag,
                    verdict=SsoVerdict.INVALID_OR_UNKNOWN,
                    parse_mode=parsed.parse_mode,
                    error=error,
                    response_ms=elapsed_ms,
                )

            email_match: bool | None = None
            if item.expected_email:
                email_match = bool(
                    parsed.account.email
                    and parsed.account.email.casefold() == item.expected_email.casefold()
                )
            flagged = parsed.bot_flag.is_flagged(self.config.flagged_sources)
            result_error = ""
            if not parsed.bot_flag.found or parsed.bot_flag.source is None:
                verdict = SsoVerdict.INVALID_OR_UNKNOWN
                result_error = "botFlagSource is missing or null"
            elif email_match is False:
                verdict = (
                    SsoVerdict.FLAGGED_EMAIL_MISMATCH
                    if flagged
                    else SsoVerdict.EMAIL_MISMATCH
                )
            else:
                verdict = SsoVerdict.FLAGGED if flagged else SsoVerdict.CLEAN
            return SsoCheckResult(
                **base,
                status_code=status_code,
                final_url=final_url,
                valid_session=True,
                email_match=email_match,
                account=parsed.account,
                bot_flag=parsed.bot_flag,
                verdict=verdict,
                parse_mode=parsed.parse_mode,
                error=result_error,
                response_ms=elapsed_ms,
            )
        except Exception as exc:
            error = str(exc)[:300]
            if item.sso_token:
                error = error.replace(item.sso_token, "[REDACTED]")
            return SsoCheckResult(
                **base,
                verdict=SsoVerdict.ERROR,
                error=error,
                response_ms=round((time.perf_counter() - started) * 1000),
            )

    def check_many(
        self,
        credentials: Any,
        *,
        max_workers: int = 8,
        progress: ProgressCallback | None = None,
    ) -> BatchCheckReport:
        """Check one or many values concurrently while preserving input order.

        ``credentials`` accepts the same flexible forms as
        :meth:`SsoCredentialLoader.load`, so passing a raw multiline string
        does not accidentally iterate over individual characters.
        """

        items = [
            self._coerce_credential(item)
            for item in SsoCredentialLoader.load(credentials)
        ]
        started_epoch = time.time()
        started_at = _utc_iso(started_epoch)
        started_perf = time.perf_counter()
        results: list[SsoCheckResult | None] = [None] * len(items)

        if max_workers <= 1:
            for index, item in enumerate(items):
                result = self.check(item)
                results[index] = result
                if progress:
                    progress(index + 1, len(items), result)
        else:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(self.check, item): index
                    for index, item in enumerate(items)
                }
                completed = 0
                for future in as_completed(futures):
                    index = futures[future]
                    result = future.result()
                    results[index] = result
                    completed += 1
                    if progress:
                        progress(completed, len(items), result)

        ordered = tuple(result for result in results if result is not None)
        return BatchCheckReport(
            results=ordered,
            started_at=started_at,
            finished_at=_utc_iso(),
            elapsed_seconds=round(time.perf_counter() - started_perf, 2),
        )

    def _new_session(self, sso_token: str) -> Any:
        if self._session_factory is not None:
            session = self._session_factory()
        else:
            if curl_requests is None:
                raise RuntimeError(
                    "curl_cffi is required for live checks; install with: "
                    "pip install 'curl_cffi>=0.13.0'"
                )
            session = curl_requests.Session()
        if self.config.proxy:
            session.proxies = {
                "http": self.config.proxy,
                "https": self.config.proxy,
            }
        for domain in self.config.cookie_domains:
            for name in self.config.cookie_names:
                session.cookies.set(name, sso_token, domain=domain)
        return session

    def _get_home(self, session: Any, *, include_user_agent: bool) -> Any:
        headers = {
            "Accept": self.config.accept,
            "Accept-Language": self.config.accept_language,
            "Upgrade-Insecure-Requests": "1",
        }
        if include_user_agent and self.config.user_agent:
            headers["User-Agent"] = self.config.user_agent
        return session.get(
            self.config.home_url,
            headers=headers,
            impersonate=self.config.impersonate,
            timeout=self.config.timeout,
            allow_redirects=self.config.allow_redirects,
            verify=self.config.verify_tls,
        )

    @staticmethod
    def _coerce_credential(
        credential: SsoCredential | str,
        *,
        expected_email: str | None = None,
        label: str | None = None,
    ) -> SsoCredential:
        item = (
            credential
            if isinstance(credential, SsoCredential)
            else SsoCredential(sso_token=_clean_text(credential))
        )
        changes: dict[str, Any] = {}
        if expected_email is not None:
            changes["expected_email"] = expected_email
        if label is not None:
            changes["label"] = label
        return replace(item, **changes) if changes else item


class SsoCredentialLoader:
    """Normalize paths, raw strings, collections, JSON/config, JSONL, or ZIP.

    Use :meth:`load` when the caller may supply either an in-memory value or a
    path. ``str`` values that point to an existing file are opened; all other
    strings are treated as credential content. Passing an explicit
    :class:`~pathlib.Path` keeps strict path semantics and raises when missing.
    """

    TOKEN_KEYS = ("sso_token", "sso", "sso_cookie")
    EMAIL_KEYS = ("email", "name")
    ZIP_EMAIL_RE = re.compile(r"^(?:\d+-)?(.+?)\.sso\.txt$", re.I)

    @classmethod
    def load(
        cls,
        source: Any,
        *,
        label_prefix: str = "account",
    ) -> list[SsoCredential]:
        """Load one or many credentials from an arbitrary in-memory source.

        Accepted forms include:

        - one raw SSO string;
        - a newline-delimited string containing multiple accounts;
        - ``list``/``tuple``/generator values containing strings, mappings, or
          :class:`SsoCredential` objects;
        - a single account mapping or ``{"accounts": [...]}`` config;
        - an existing path supplied as either ``str`` or :class:`Path`.
        """

        if source is None:
            return []
        if isinstance(source, SsoCredential):
            return [source]
        if isinstance(source, Path):
            return cls.from_path(source)
        if isinstance(source, bytes):
            return cls.from_text(
                source.decode("utf-8", "replace"),
                label_prefix=label_prefix,
            )
        if isinstance(source, str):
            existing_path = cls._existing_path(source)
            if existing_path is not None:
                return cls.from_path(existing_path)
            return cls.from_text(source, label_prefix=label_prefix)
        if isinstance(source, Mapping):
            return cls.from_json(source)

        try:
            iterator = iter(source)
        except TypeError as exc:
            raise TypeError(
                "source must be an SSO string, credential, mapping, iterable, or path"
            ) from exc

        output: list[SsoCredential] = []
        for index, value in enumerate(iterator, 1):
            nested_prefix = f"{label_prefix}-{index}"
            loaded = cls.load(value, label_prefix=nested_prefix)
            for nested_index, item in enumerate(loaded, 1):
                if item.label:
                    output.append(item)
                    continue
                suffix = "" if len(loaded) == 1 else f"-{nested_index}"
                output.append(replace(item, label=f"{nested_prefix}{suffix}"))
        return output

    @classmethod
    def from_source(
        cls,
        source: Any,
        *,
        label_prefix: str = "account",
    ) -> list[SsoCredential]:
        """Readable alias for :meth:`load`."""

        return cls.load(source, label_prefix=label_prefix)

    @classmethod
    def load_one(
        cls,
        source: Any,
        *,
        label: str = "",
    ) -> SsoCredential:
        """Normalize exactly one credential and reject zero/multiple values."""

        items = cls.load(source, label_prefix=label or "account")
        if len(items) != 1:
            raise ValueError(f"expected exactly one SSO credential, got {len(items)}")
        return replace(items[0], label=label) if label else items[0]

    @staticmethod
    def _existing_path(value: str) -> Path | None:
        """Return a file path only when a string unambiguously names one."""

        if not value or "\n" in value or "\r" in value or "\0" in value:
            return None
        try:
            candidate = Path(value).expanduser()
            return candidate if candidate.is_file() else None
        except (OSError, ValueError):
            return None

    @classmethod
    def from_text(
        cls,
        text: str,
        *,
        label_prefix: str = "account",
    ) -> list[SsoCredential]:
        """Load a raw single-account or multi-account text value."""

        raw = _clean_text(text)
        if not raw:
            return []

        # This supports compact or pretty-printed JSON passed directly from an
        # API without requiring an intermediate file.
        if raw.startswith(("{", "[")):
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                value = None
            if isinstance(value, (dict, list)):
                return cls.from_json(value)

        output: list[SsoCredential] = []
        for index, line in enumerate(raw.splitlines(), 1):
            item = cls.from_line(line, label=f"{label_prefix}-{index}")
            if item:
                output.append(item)
        return output

    @classmethod
    def from_line(cls, line: str, *, label: str = "") -> SsoCredential | None:
        raw = _clean_text(line)
        if not raw or raw.startswith("#"):
            return None
        if raw.startswith("{"):
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                value = None
            if isinstance(value, dict):
                return cls.from_mapping(value, label=label)

        email = ""
        token = raw
        if "----" in raw:
            parts = [part.strip() for part in raw.split("----")]
            email = parts[0] if len(parts) >= 2 else ""
            token = parts[-1]
        if token.startswith("sso="):
            token = token[4:].strip()
        return SsoCredential(token, expected_email=email, label=label or email)

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        label: str = "",
    ) -> SsoCredential | None:
        token = next((_clean_text(value.get(key)) for key in cls.TOKEN_KEYS if value.get(key)), "")
        if not token:
            return None
        email = next((_clean_text(value.get(key)) for key in cls.EMAIL_KEYS if value.get(key)), "")
        item_label = _clean_text(value.get("label")) or label or email
        return SsoCredential(token, expected_email=email, label=item_label)

    @classmethod
    def from_json(cls, value: Any) -> list[SsoCredential]:
        if isinstance(value, dict) and isinstance(value.get("accounts"), list):
            candidates = value["accounts"]
        elif isinstance(value, list):
            candidates = value
        else:
            candidates = [value]
        output: list[SsoCredential] = []
        for index, candidate in enumerate(candidates, 1):
            if isinstance(candidate, SsoCredential):
                item = candidate
            elif isinstance(candidate, Mapping):
                item = cls.from_mapping(candidate, label=f"account-{index}")
            elif isinstance(candidate, str):
                item = cls.from_line(candidate, label=f"account-{index}")
            else:
                item = None
            if item:
                output.append(item)
        return output

    @classmethod
    def from_path(cls, path: str | Path) -> list[SsoCredential]:
        """Load an explicit filesystem path.

        For a value that might instead be raw SSO content, call :meth:`load`.
        """

        source = Path(path)
        if source.suffix.lower() == ".zip":
            return cls.from_zip(source)
        text = source.read_text(encoding="utf-8")
        if source.suffix.lower() == ".json":
            return cls.from_json(json.loads(text))
        return cls.from_text(text, label_prefix="line")

    @classmethod
    def from_zip(cls, path: str | Path) -> list[SsoCredential]:
        """Read ZIP entries in memory; no archive content is extracted to disk."""

        output: list[SsoCredential] = []
        with ZipFile(path) as archive:
            for index, name in enumerate(archive.namelist(), 1):
                if name.endswith("/"):
                    continue
                pure = PurePosixPath(name)
                if pure.is_absolute() or ".." in pure.parts:
                    continue
                raw = archive.read(name).decode("utf-8", "replace")
                match = cls.ZIP_EMAIL_RE.match(pure.name)
                expected_email = match.group(1) if match else ""
                item = cls.from_line(raw, label=pure.name)
                if item:
                    if expected_email and not item.expected_email:
                        item = replace(item, expected_email=expected_email)
                    output.append(item)
        return output


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        Path(temp_name).unlink(missing_ok=True)
        raise


def _build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Object-oriented SSO account-state checker",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--sso-token", help="single SSO token (input file is preferred)")
    source.add_argument("--input", type=Path, help="TXT, JSON, JSONL, or ZIP input")
    parser.add_argument("--email", default="", help="expected email for --sso-token")
    parser.add_argument("--proxy", default="", help="HTTP(S) proxy URL")
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--output", type=Path, help="write credential-free JSON report")
    parser.add_argument("--summary-only", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_cli().parse_args(argv)
    if args.sso_token is not None:
        credentials = [SsoCredential(args.sso_token, expected_email=args.email)]
    else:
        credentials = SsoCredentialLoader.from_path(args.input)
    checker = SsoChecker(
        SsoCheckConfig(
            timeout=args.timeout,
            proxy=args.proxy,
        )
    )
    report = checker.check_many(credentials, max_workers=args.workers)
    payload: dict[str, Any] = (
        {"summary": report.summary}
        if args.summary_only
        else report.to_dict()
    )
    if args.output:
        _atomic_write_json(args.output.resolve(), payload)
        print(f"report: {args.output.resolve()}")
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    return 0 if report.summary["invalid_or_errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
