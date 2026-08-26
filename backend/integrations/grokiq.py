# -*- coding: utf-8 -*-
"""Durable GrokIQ webhook delivery.

The registration flow writes an outbox row only after the grok_build document
has been accepted by Grok2API. A single background thread delivers
pending rows with at-least-once semantics; GrokIQ deduplicates by event_id.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Mapping
from urllib.parse import urlsplit

from curl_cffi import requests

logger = logging.getLogger(__name__)


class GrokIQDeliveryError(RuntimeError):
    def __init__(self, message: str, *, response_text: str = ""):
        super().__init__(message)
        self.response_text = response_text


def validate_grokiq_config(config: Mapping[str, Any]) -> Dict[str, Any]:
    enabled = bool(config.get("grokiq_webhook_enabled"))
    url = str(config.get("grokiq_webhook_url") or "").strip()
    token = str(config.get("grokiq_webhook_token") or "").strip()
    try:
        timeout = int(config.get("grokiq_webhook_timeout_seconds") or 10)
    except (TypeError, ValueError) as exc:
        raise ValueError("GrokIQ 联动超时必须是整数") from exc
    timeout = max(1, min(timeout, 60))
    if enabled:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("GrokIQ Webhook URL 必须是有效的 HTTP(S) 地址")
        if not token:
            raise ValueError("启用 GrokIQ 联动时必须填写联动 Token")
    return {
        "enabled": enabled,
        "url": url,
        "token": token,
        "timeout": timeout,
    }


def enqueue_imported_account(
    repository: Any,
    record: Mapping[str, Any],
    config: Mapping[str, Any],
) -> Dict[str, Any] | None:
    """Persist one event after a successful grok_build import."""

    if not bool(config.get("grokiq_webhook_enabled")):
        return None
    sso = str(record.get("sso") or "").strip()
    if not sso:
        account_file = str(record.get("account_file") or "").strip()
        if account_file:
            try:
                raw = Path(account_file).read_text(encoding="utf-8").strip()
                _, separator, token = raw.rpartition("----")
                sso = token.strip() if separator else ""
            except (OSError, UnicodeError):
                sso = ""
    event = repository.enqueue_grokiq_event(
        registration_id=int(record.get("id") or 0),
        email=str(record.get("email") or ""),
        bot_risk=bool(record.get("bot_risk")),
        bfs=record.get("bfs"),
        occurred_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        sso=sso,
    )
    grokiq_notifier.wake()
    return event


def grok_build_import_succeeded(result: Mapping[str, Any] | None) -> bool:
    if not isinstance(result, Mapping):
        return False
    formats = result.get("formats")
    return isinstance(formats, Mapping) and isinstance(
        formats.get("grok_build"), Mapping
    )


class GrokIQNotifier:
    def __init__(self) -> None:
        self._repository: Any = None
        self._config_loader: Callable[[], Mapping[str, Any]] | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.RLock()
        self._last_error = ""

    def start(
        self,
        repository: Any,
        config_loader: Callable[[], Mapping[str, Any]],
    ) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._repository = repository
            self._config_loader = config_loader
            self._stop.clear()
            recovered = repository.recover_grokiq_deliveries()
            if recovered:
                logger.info("recovered GrokIQ deliveries count=%s", recovered)
            self._thread = threading.Thread(
                target=self._run,
                name="grokiq-webhook",
                daemon=True,
            )
            self._thread.start()
        self._wake.set()

    def stop(self, timeout: float = 5.0) -> None:
        with self._lock:
            thread = self._thread
        if thread is None:
            return
        self._stop.set()
        self._wake.set()
        thread.join(timeout=max(float(timeout), 0.1))
        with self._lock:
            if self._thread is thread and not thread.is_alive():
                self._thread = None

    def wake(self) -> None:
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            config = self._load_config()
            try:
                normalized = validate_grokiq_config(config)
            except ValueError as exc:
                self._set_error(str(exc))
                self._wait(2.0)
                continue
            with self._lock:
                self._last_error = ""
            if not normalized["enabled"] or not normalized["url"] or not normalized["token"]:
                self._wait(2.0)
                continue
            event = self._repository.claim_grokiq_delivery()
            if event is None:
                self._wait(2.0)
                continue
            self._deliver(event, normalized)

    def _load_config(self) -> Mapping[str, Any]:
        loader = self._config_loader
        if loader is None:
            return {}
        try:
            value = loader()
        except Exception as exc:
            self._set_error(f"读取 GrokIQ 联动配置失败: {exc}")
            return {}
        return value if isinstance(value, Mapping) else {}

    def _deliver(self, event: Mapping[str, Any], config: Mapping[str, Any]) -> None:
        event_id = str(event.get("event_id") or "")
        attempts = max(int(event.get("attempts") or 1), 1)
        payload = {
            "event_id": event_id,
            "event_type": str(
                event.get("event_type") or "grok2api.account_imported"
            ),
            "registration_id": str(event.get("registration_id") or ""),
            "email": str(event.get("email") or ""),
            "bot_risk": bool(event.get("bot_risk")),
            "bfs": str(event.get("bfs") or ""),
            "sso": str(event.get("sso") or ""),
            "occurred_at": str(event.get("occurred_at") or ""),
        }
        response_text = ""
        try:
            with requests.Session(trust_env=False) as session:
                response = session.post(
                    str(config["url"]),
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "x-grokiq-token": str(config["token"]),
                    },
                    json=payload,
                    timeout=float(config["timeout"]),
                )
            status_code = int(getattr(response, "status_code", 0) or 0)
            if status_code < 200 or status_code >= 300:
                response_text = str(getattr(response, "text", "") or "")[:16000]
                raise GrokIQDeliveryError(
                    self._response_error(response, status_code),
                    response_text=response_text,
                )
            self._repository.complete_grokiq_delivery(
                event_id,
            )
            with self._lock:
                self._last_error = ""
            logger.info("GrokIQ webhook delivered event_id=%s", event_id)
        except Exception as exc:
            delay = min(300.0, float(2 ** min(attempts, 8)))
            body = (
                exc.response_text
                if isinstance(exc, GrokIQDeliveryError)
                else response_text
            )
            self._repository.retry_grokiq_delivery(
                event_id,
                error=str(exc),
                delay_seconds=delay,
                response_json=body,
            )
            logger.warning(
                "GrokIQ webhook retry event_id=%s attempts=%s delay=%s error=%s",
                event_id,
                attempts,
                delay,
                exc,
            )

    @staticmethod
    def _response_error(response: Any, status_code: int) -> str:
        try:
            payload = response.json()
        except Exception:
            payload = None
        if isinstance(payload, Mapping):
            detail = payload.get("detail") or payload.get("error")
            if isinstance(detail, Mapping):
                detail = detail.get("message") or detail.get("detail")
            if detail:
                return f"GrokIQ Webhook 返回 HTTP {status_code}: {detail}"
        text = str(getattr(response, "text", "") or "").strip()
        return f"GrokIQ Webhook 返回 HTTP {status_code}: {text or '请求失败'}"

    def _set_error(self, message: str) -> None:
        normalized = str(message or "")[:4000]
        with self._lock:
            if normalized == self._last_error:
                return
            self._last_error = normalized
        if normalized:
            logger.warning("GrokIQ webhook paused error=%s", normalized)

    def _wait(self, seconds: float) -> None:
        self._wake.wait(timeout=max(float(seconds), 0.1))
        self._wake.clear()


grokiq_notifier = GrokIQNotifier()
