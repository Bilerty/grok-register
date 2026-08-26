# -*- coding: utf-8 -*-
"""在代理后的浏览器里识别出口 IP，并避开已风控的出口。"""
from __future__ import annotations

import ipaddress
import re
import threading
import time
from typing import Any, Callable, Optional

from backend.automation.session import active_page, restart_browser

MAX_ROTATE_ATTEMPTS = 3
DETECT_TIMEOUT_MS = 12_000
FETCH_TIMEOUT_MS = 8_000

# 必须走浏览器内请求，才能得到代理后的真实出口。
# Cloudflare trace 比第三方查 IP 站更不容易被 uBlock 拦截。
BROWSER_FETCH_URLS = (
    "https://1.1.1.1/cdn-cgi/trace",
    "https://cloudflare.com/cdn-cgi/trace",
    "https://api.ipify.org?format=text",
    "https://ipv4.icanhazip.com",
)
BROWSER_NAV_URLS = (
    "https://1.1.1.1/cdn-cgi/trace",
    "https://api.ipify.org?format=text",
)

_IPV4_RE = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d{1,2})\.){3}(?:25[0-5]|2[0-4]\d|1?\d{1,2})\b"
)
_tls = threading.local()

_DETECT_FETCH_JS = """
async () => {
  const urls = %s;
  const timeoutMs = %s;
  for (const url of urls) {
    try {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), timeoutMs);
      const response = await fetch(url, {
        signal: ctrl.signal,
        cache: "no-store",
        credentials: "omit",
      });
      clearTimeout(timer);
      if (!response.ok) continue;
      const text = (await response.text() || "").trim();
      if (text) return text;
    } catch (e) {}
  }
  return "";
}
""" % (
    repr(list(BROWSER_FETCH_URLS)),
    FETCH_TIMEOUT_MS,
)


def normalize_exit_ip(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return ipaddress.ip_address(text).compressed
    except ValueError:
        return ""


def parse_exit_ip(text: Any) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("ip="):
            candidate = normalize_exit_ip(stripped.split("=", 1)[1])
            if candidate:
                return candidate
    direct = normalize_exit_ip(raw.split()[0] if raw.split() else raw)
    if direct:
        return direct
    match = _IPV4_RE.search(raw)
    if match:
        return normalize_exit_ip(match.group(0))
    return ""


def current_exit_ip() -> str:
    return str(getattr(_tls, "ip", "") or "").strip()


def current_start_exit_ip() -> str:
    return str(getattr(_tls, "start_ip", "") or "").strip()


def set_current_exit_ip(ip: Any, *, as_start: bool = False) -> str:
    normalized = normalize_exit_ip(ip) or str(ip or "").strip()
    _tls.ip = normalized
    if not normalized:
        _tls.start_ip = ""
        return ""
    if as_start or not current_start_exit_ip():
        _tls.start_ip = normalized
    return normalized


def _log(log_callback: Optional[Callable[[str], None]], message: str) -> None:
    if log_callback:
        log_callback(f"[出口IP] {message}")


def _page_evaluate(page: Any, script: str) -> Any:
    raw_page = getattr(page, "raw_page", None)
    if raw_page is not None and hasattr(raw_page, "evaluate"):
        return raw_page.evaluate(script)
    if hasattr(page, "run_js"):
        return page.run_js(f"return ({script})();")
    return ""


def _detect_via_fetch(page: Any) -> str:
    try:
        raw = _page_evaluate(page, _DETECT_FETCH_JS)
    except Exception:
        return ""
    return parse_exit_ip(raw)


def _detect_via_navigation(page: Any) -> str:
    context = getattr(page, "raw_context", None)
    if context is None or not hasattr(context, "new_page"):
        return ""
    tab = None
    try:
        tab = context.new_page()
        for url in BROWSER_NAV_URLS:
            try:
                tab.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=DETECT_TIMEOUT_MS,
                )
                try:
                    text = str(tab.inner_text("body") or "")
                except Exception:
                    text = str(tab.content() or "")
                ip = parse_exit_ip(text)
                if ip:
                    return ip
            except Exception:
                continue
    finally:
        if tab is not None:
            try:
                tab.close()
            except Exception:
                pass
    return ""


def detect_browser_exit_ip(page: Any = None) -> str:
    """在当前浏览器上下文中识别代理后的出口 IP。"""
    target = page if page is not None else active_page()
    if target is None:
        return ""
    ip = _detect_via_fetch(target)
    if ip:
        return ip
    return _detect_via_navigation(target)


def refresh_browser_exit_ip(
    *,
    log_callback: Optional[Callable[[str], None]] = None,
    reason: str = "",
    detect: Optional[Callable[[], str]] = None,
) -> str:
    """在当前仍打开的浏览器里再测一次出口 IP。

    动态代理池可能在注册过程中换出口；风控记录必须用此刻浏览器看到的 IP，
    不能沿用打开注册页前缓存的值。
    """
    previous = current_exit_ip()
    prefix = f"{reason}，" if reason else ""
    detector = detect or detect_browser_exit_ip
    try:
        raw = detector()
    except Exception as exc:
        _log(log_callback, f"{prefix}再次识别出口 IP 失败: {exc}")
        return previous
    ip = parse_exit_ip(raw) or normalize_exit_ip(raw)
    if not ip:
        if previous:
            _log(log_callback, f"{prefix}未能再次识别出口 IP，沿用 {previous}")
        else:
            _log(log_callback, f"{prefix}未能再次识别出口 IP")
        return previous
    set_current_exit_ip(ip)
    if previous and ip != previous:
        _log(log_callback, f"{prefix}出口 IP 已变化: {previous} -> {ip}")
    elif previous:
        _log(log_callback, f"{prefix}出口 IP 仍为 {ip}")
    else:
        _log(log_callback, f"{prefix}浏览器识别到出口 IP {ip}")
    return ip


def ensure_unflagged_exit_ip(
    *,
    store: Any,
    proxy_enabled: bool,
    log_callback: Optional[Callable[[str], None]] = None,
    max_attempts: int = MAX_ROTATE_ATTEMPTS,
    restart: Optional[Callable[[], Any]] = None,
    detect: Optional[Callable[[], str]] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """注册前检查浏览器出口 IP；命中风控名单时重启浏览器换出口。"""
    detector = detect or detect_browser_exit_ip
    _tls.start_ip = ""

    def read_ip() -> str:
        try:
            raw = detector()
        except Exception as exc:
            _log(log_callback, f"浏览器识别出口 IP 失败: {exc}")
            raw = ""
        ip = parse_exit_ip(raw) or normalize_exit_ip(raw)
        set_current_exit_ip(ip)
        return ip

    try:
        ip = read_ip()
        if not ip:
            _log(log_callback, "未能在浏览器中识别出口 IP，跳过风控 IP 检查")
            return ""
        if not proxy_enabled:
            _log(log_callback, f"未配置代理，当前浏览器出口 IP {ip}，无法更换出口")
            return set_current_exit_ip(ip, as_start=True)
        if not store or not store.is_flagged_exit_ip(ip):
            _log(log_callback, f"浏览器识别到出口 IP {ip}，不在风控名单")
            return set_current_exit_ip(ip, as_start=True)

        attempts = max(1, int(max_attempts or MAX_ROTATE_ATTEMPTS))
        restarter = restart or (lambda: restart_browser(log_callback=log_callback))
        original_ip = ip
        last_ip = ip
        seen = {ip}
        _log(log_callback, f"{ip} 在风控名单中，准备重启浏览器更换出口")
        for attempt in range(1, attempts):
            _log(
                log_callback,
                f"重启浏览器更换出口 ({attempt}/{attempts - 1})，当前风控 IP {last_ip}",
            )
            try:
                restarter()
            except Exception as exc:
                _log(log_callback, f"重启浏览器失败，继续使用当前出口 IP {last_ip}: {exc}")
                return set_current_exit_ip(last_ip, as_start=True)
            sleep(1)
            new_ip = read_ip()
            if not new_ip:
                _log(log_callback, "重启后未能识别出口 IP，继续使用当前浏览器注册")
                return set_current_exit_ip(last_ip, as_start=True)
            if new_ip == last_ip:
                _log(log_callback, f"重启后出口 IP 仍为 {new_ip}")
            elif new_ip in seen:
                _log(log_callback, f"重启后出口 IP 变回已见过的 {new_ip}")
            else:
                _log(log_callback, f"重启后出口 IP 变为 {new_ip}")
            last_ip = new_ip
            seen.add(new_ip)
            if not store.is_flagged_exit_ip(new_ip):
                _log(log_callback, f"出口 IP {new_ip} 不在风控名单，继续注册")
                return set_current_exit_ip(new_ip, as_start=True)

        if last_ip == original_ip and len(seen) == 1:
            _log(
                log_callback,
                f"已尝试 {attempts} 次，出口 IP 一直是 {last_ip}，继续使用该 IP 注册",
            )
        else:
            _log(
                log_callback,
                f"已尝试 {attempts} 次，当前出口 IP {last_ip} 仍在风控名单，继续注册",
            )
        return set_current_exit_ip(last_ip, as_start=True)
    except Exception as exc:
        _log(log_callback, f"检查出口 IP 异常，继续注册: {exc}")
        return current_exit_ip()
