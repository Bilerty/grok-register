# -*- coding: utf-8 -*-
"""代理池与任务级出口绑定（适配 v1.0.9 网络栈）。

v1.0.9 中浏览器（automation.session）与全部 HTTP 客户端统一经
``engine.get_proxies()`` 取出口，因此池的核心是"任务线程绑定"：

- ``bind_task(scope_key, email)``：任务/worker 开始时绑定，返回渲染后的代理 URL；
  此后线程内 ``engine.get_proxies()`` 一律返回该出口，浏览器与 HTTP 同源
- ``current_proxy_url()`` / ``current_raw_url()``：线程内当前出口（渲染后 / 池条目原文）
- ``release_task()``：任务结束释放粘性租约
- ``rebind_if_unhealthy()``：绑定节点在任务中进入冷却/被隔离时自动换节点，
  引擎据此重启浏览器使新出口生效
- ``fallback_proxy_url()``：仅 static / sticky_template 模式返回配置代理；pool 模式
  返回空串，绝不把多行池文本当单代理误用

健康管理：
- healthy / unreachable / cooldown / flagged（出口 IP 命中上游风控出口名单时隔离）
- 探测回调与风控名单回调由引擎注入（network_checks.check_proxy / 注册库风控 IP），
  本模块不做网络请求与存储假设
- 探测在锁外执行：选节点时只在锁内维护状态，绝不持锁做网络 IO
- 状态持久化 JSON：临时文件原子替换，锁外写入，重启不丢
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional
from urllib.parse import quote, urlsplit, urlunsplit

from backend.integrations.proxy import validate_http_proxy_url

MODE_STATIC = "static"
MODE_POOL = "pool"
MODE_STICKY_TEMPLATE = "sticky_template"

SELECTION_ROUND_ROBIN = "round_robin"
SELECTION_RANDOM = "random"
SELECTION_LEAST_USED = "least_used"

STICKY_SCOPE_NONE = "none"
STICKY_SCOPE_TASK = "task"

PLACEHOLDER_ACCOUNT = "{account}"
PLACEHOLDER_EMAIL = "{email}"

STATUS_HEALTHY = "healthy"
STATUS_UNREACHABLE = "unreachable"
STATUS_COOLDOWN = "cooldown"
STATUS_FLAGGED = "flagged"

VALID_MODES = frozenset({MODE_STATIC, MODE_POOL, MODE_STICKY_TEMPLATE})
VALID_SELECTIONS = frozenset(
    {SELECTION_ROUND_ROBIN, SELECTION_RANDOM, SELECTION_LEAST_USED}
)
VALID_STICKY_SCOPES = frozenset({STICKY_SCOPE_NONE, STICKY_SCOPE_TASK})
VALID_STATUSES = frozenset(
    {STATUS_HEALTHY, STATUS_UNREACHABLE, STATUS_COOLDOWN, STATUS_FLAGGED}
)


class ProxyConfigError(Exception):
    """代理池配置错误。"""


class ProxyPoolExhausted(Exception):
    """代理池没有可用（健康）节点。"""


def node_key(url: str) -> str:
    """节点的稳定短键（不暴露原文，供 API 往返定位节点）。"""
    return hashlib.sha1(str(url or "").encode("utf-8")).hexdigest()[:12]


def build_proxy_credentials_url(url: str, username: str = "", password: str = "") -> str:
    """把池级凭据并入代理 URL；URL 自带认证时以 URL 自身为准。

    凭据按 RFC 3986 百分号编码后写入 userinfo，供 HTTP 客户端与
    Camoufox/Playwright（proxy.parse_http_proxy_url）解析。
    """
    value = str(url or "").strip()
    if not value or "@" in value:
        return value
    user = str(username or "")
    password = str(password or "")
    if not user and not password:
        return value
    has_scheme = "://" in value
    try:
        parsed = urlsplit(value if has_scheme else f"http://{value}")
        if not parsed.hostname:
            return value
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        userinfo = quote(user, safe="")
        if password:
            userinfo += f":{quote(password, safe='')}"
        rebuilt = urlunsplit(
            (parsed.scheme or "http", f"{userinfo}@{host}{port}", "", "", "")
        )
        return rebuilt if has_scheme else rebuilt.split("://", 1)[1]
    except ValueError:
        return value


class NodeState:
    __slots__ = (
        "status",
        "cooldown_until",
        "last_used_at",
        "egress_ip",
        "latency_ms",
        "last_error",
        "probe_at",
    )

    def __init__(self):
        self.status = STATUS_HEALTHY
        self.cooldown_until = 0.0
        self.last_used_at = 0.0
        self.egress_ip = ""
        self.latency_ms = None
        self.last_error = ""
        self.probe_at = 0.0

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "cooldown_until": self.cooldown_until,
            "last_used_at": self.last_used_at,
            "egress_ip": self.egress_ip,
            "latency_ms": self.latency_ms,
            "last_error": self.last_error,
            "probe_at": self.probe_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "NodeState":
        state = cls()
        if not isinstance(data, dict):
            return state
        if data.get("status") in VALID_STATUSES:
            state.status = data.get("status")
        state.cooldown_until = float(data.get("cooldown_until") or 0)
        state.last_used_at = float(data.get("last_used_at") or 0)
        state.egress_ip = str(data.get("egress_ip") or "")
        latency = data.get("latency_ms")
        state.latency_ms = int(latency) if latency is not None else None
        state.last_error = str(data.get("last_error") or "")
        state.probe_at = float(data.get("probe_at") or 0)
        return state


class ProxyPool:
    """解析并持有代理来源，提供选择、粘性租约与健康管理。

    ``probe(proxy_url)`` 返回 ``{"ok": bool, "egress_ip": str,
    "latency_ms": int|None, "error": str}``；``ip_flagged(ip)`` 查询上游
    风控出口名单。两者均可为空，此时节点默认可用。
    """

    def __init__(
        self,
        mode: str,
        urls: list[str],
        selection: str = SELECTION_ROUND_ROBIN,
        sticky_scope: str = STICKY_SCOPE_TASK,
        username: str = "",
        password: str = "",
        cooldown_seconds: int = 600,
        state_file: str = "",
        probe: Optional[Callable[[str], dict]] = None,
        ip_flagged: Optional[Callable[[str], bool]] = None,
        probe_once_per_batch: bool = False,
    ):
        self.mode = mode
        self.urls = list(urls)
        self.selection = selection
        self.sticky_scope = sticky_scope
        self.username = username
        self.password = password
        self.cooldown_seconds = max(int(cooldown_seconds or 0), 0)
        self.state_file = state_file
        self.probe = probe
        self.ip_flagged = ip_flagged
        self.probe_once_per_batch = bool(probe_once_per_batch)
        # 批次启动前统一探测后置位：此后任务绑定不再逐节点探测
        self._batch_probed = False
        self._lock = threading.Lock()
        self._rr = itertools.count()
        self._usage: dict[str, int] = {}
        self._leases: dict[str, str] = {}
        self._nodes: dict[str, NodeState] = {}
        self._dirty = False
        self._load_state()

    # ------------------------------------------------------------------
    # 状态持久化（锁外原子写）
    # ------------------------------------------------------------------
    def _load_state(self) -> None:
        if not self.state_file or not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for url, raw in data.items():
                    if url in self.urls:
                        self._nodes[url] = NodeState.from_dict(raw)
        except (OSError, ValueError, TypeError):
            pass

    def _persist_state(self) -> None:
        if not self.state_file:
            return
        with self._lock:
            if not self._dirty:
                return
            payload = {url: state.to_dict() for url, state in self._nodes.items()}
            self._dirty = False
        directory = os.path.dirname(self.state_file)
        if directory:
            try:
                os.makedirs(directory, exist_ok=True)
            except OSError:
                pass
        temporary = f"{self.state_file}.{os.getpid()}.tmp"
        try:
            with open(temporary, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.state_file)
        except OSError:
            try:
                if os.path.exists(temporary):
                    os.remove(temporary)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # 基础行为
    # ------------------------------------------------------------------
    def empty(self) -> bool:
        return not self.urls

    def resolve(self, scope_key: str = "", email: str = "") -> str:
        """返回本任务使用的代理 URL（含 {account}/{email} 展开，未并入池凭据）。

        pool 模式下按需探测：锁内只维护选择与状态，网络探测在锁外进行；
        持久化同样在锁外执行。
        """
        try:
            if self.mode == MODE_POOL:
                return self._resolve_pool(scope_key, email)
            with self._lock:
                return self._resolve_locked(scope_key, email)
        finally:
            self._persist_state()

    def _resolve_pool(self, scope_key: str, email: str) -> str:
        if self.sticky_scope != STICKY_SCOPE_NONE and scope_key:
            with self._lock:
                leased = self._leases.get(scope_key)
                if leased and self._status_of(leased, time.time()) == STATUS_HEALTHY:
                    return self._expand_template(leased, scope_key, email)
        template = self._pick_with_probe(scope_key, email)
        if self.sticky_scope != STICKY_SCOPE_NONE and scope_key:
            with self._lock:
                self._leases[scope_key] = template
        return self._expand_template(template, scope_key, email)

    def resolve_template(self, scope_key: str = "", email: str = "") -> str:
        """本任务命中的池条目原文（状态记录 / 上报键）。"""
        with self._lock:
            if self.mode == MODE_POOL and self.sticky_scope != STICKY_SCOPE_NONE and scope_key:
                return self._leases.get(scope_key, "")
            return self.urls[0] if self.urls else ""

    @staticmethod
    def _expand_template(url: str, scope_key: str, email: str) -> str:
        if PLACEHOLDER_ACCOUNT in url:
            return url.replace(PLACEHOLDER_ACCOUNT, scope_key or "")
        if PLACEHOLDER_EMAIL in url:
            local = "".join(
                char for char in str(email or "").split("@", 1)[0] if char.isalnum()
            ).lower()
            return url.replace(PLACEHOLDER_EMAIL, local)
        return url

    def render(self, url: str) -> str:
        """把池条目渲染为实际使用的 URL（并入池级凭据）。"""
        if not url:
            return ""
        return build_proxy_credentials_url(url, self.username, self.password)

    def _status_of(self, url: str, now: float) -> str:
        state = self._nodes.get(url)
        if state is None:
            return STATUS_HEALTHY
        if state.status == STATUS_COOLDOWN and now >= state.cooldown_until:
            return STATUS_HEALTHY
        return state.status

    def _healthy_urls_locked(self, now: float) -> list[str]:
        return [url for url in self.urls if self._status_of(url, now) == STATUS_HEALTHY]

    def _resolve_locked(self, scope_key: str, email: str) -> str:
        """static / sticky_template 的解析（pool 模式走 ``_resolve_pool``）。"""
        if not self.urls:
            return ""
        if self.mode == MODE_STICKY_TEMPLATE:
            return self._expand_template(self.urls[0], scope_key, email)
        return self.urls[0]

    def _pick_locked(self, scope_key: str, email: str) -> str:
        """锁内选定节点：按健康状态与策略排序，跳过出口 IP 命中风控名单的节点。

        返回池条目原文；网络探测由调用方（``_pick_with_probe``）在锁外完成。
        """
        now = time.time()
        available = self._healthy_urls_locked(now)
        if not available:
            raise ProxyPoolExhausted("代理池没有健康节点可用")

        if self.selection == SELECTION_RANDOM:
            order = list(available)
            random.shuffle(order)
        elif self.selection == SELECTION_LEAST_USED:
            order = sorted(available, key=lambda url: self._usage.get(url, 0))
        else:
            start = next(self._rr) % len(available)
            order = available[start:] + available[:start]

        for template in order:
            state = self._nodes.get(template)
            if state is not None and state.egress_ip and self.ip_flagged is not None:
                try:
                    flagged = bool(self.ip_flagged(state.egress_ip))
                except Exception:
                    flagged = False
                if flagged:
                    state.status = STATUS_FLAGGED
                    state.last_error = "出口 IP 命中风控名单"
                    self._dirty = True
                    continue
            if state is None:
                state = NodeState()
                self._nodes[template] = state
            self._usage[template] = self._usage.get(template, 0) + 1
            state.last_used_at = now
            self._dirty = True
            return template
        raise ProxyPoolExhausted("代理池节点出口 IP 均命中风控名单")

    def _pick_with_probe(self, scope_key: str, email: str) -> str:
        """选定节点并按需探测；探测在锁外进行，失败节点标记后尝试下一个。"""
        last_error = ""
        for _ in range(len(self.urls) + 1):
            try:
                with self._lock:
                    template = self._pick_locked(scope_key, email)
            except ProxyPoolExhausted as exc:
                raise ProxyPoolExhausted(
                    f"{exc}{('：' + last_error) if last_error else ''}"
                ) from exc
            if self._needs_probe(template):
                probe_url = self.render(self._expand_template(template, scope_key, email))
                result = self._probe_url(probe_url)
                self._apply_probe_result(template, result)
                if not result.get("ok"):
                    last_error = str(result.get("error") or "探测失败")
                    continue
            return template
        raise ProxyPoolExhausted(
            f"代理池节点探测全部失败{('：' + last_error) if last_error else ''}"
        )

    def _needs_probe(self, template: str) -> bool:
        if self.probe is None or self._batch_probed:
            return False
        with self._lock:
            state = self._nodes.get(template)
            return state is None or state.probe_at <= 0

    def _probe_url(self, url: str) -> dict:
        if self.probe is None:
            return {"ok": True, "egress_ip": "", "latency_ms": None, "error": ""}
        try:
            result = self.probe(url)
        except Exception as exc:
            return {"ok": False, "egress_ip": "", "latency_ms": None, "error": str(exc)[:200]}
        if not isinstance(result, dict):
            return {"ok": False, "egress_ip": "", "latency_ms": None, "error": "探测回调返回异常"}
        return {
            "ok": bool(result.get("ok")),
            "egress_ip": str(result.get("egress_ip") or ""),
            "latency_ms": result.get("latency_ms"),
            "error": str(result.get("error") or "")[:200],
        }

    def _apply_probe_result(self, template: str, result: dict) -> None:
        now = time.time()
        with self._lock:
            state = self._nodes.get(template) or NodeState()
            state.probe_at = now
            if result.get("ok"):
                state.status = STATUS_HEALTHY
                state.egress_ip = str(result.get("egress_ip") or "")[:120]
                latency = result.get("latency_ms")
                state.latency_ms = int(latency) if latency is not None else None
                state.last_error = ""
                state.cooldown_until = 0.0
            else:
                state.status = STATUS_UNREACHABLE
                state.last_error = str(result.get("error") or "")[:200]
            self._nodes[template] = state
            self._dirty = True
        self._persist_state()

    # ------------------------------------------------------------------
    # 任务绑定
    # ------------------------------------------------------------------
    def rebind_if_unhealthy(self, scope_key: str, email: str = "") -> tuple[bool, str, str]:
        """绑定节点不再健康时换节点（保持同一 scope 的租约语义）。

        返回 ``(changed, previous_template, current_template)``。
        """
        previous = self.resolve_template(scope_key, email)
        if previous and self._status_of(previous, time.time()) == STATUS_HEALTHY:
            return False, previous, previous
        with self._lock:
            self._leases.pop(scope_key, None)
        expanded = self.resolve(scope_key, email)
        current = self.resolve_template(scope_key, email) or expanded
        if current == previous:
            return False, previous, previous
        return True, previous, current

    def release(self, scope_key: str = "") -> None:
        if not scope_key:
            return
        with self._lock:
            self._leases.pop(scope_key, None)

    def is_healthy(self, url: str) -> bool:
        return bool(url) and self._status_of(url, time.time()) == STATUS_HEALTHY

    def url_list(self) -> list[str]:
        return list(self.urls)

    # ------------------------------------------------------------------
    # 健康管理
    # ------------------------------------------------------------------
    def probe_node(self, url: str) -> dict:
        """手动探测单个节点（渲染后的 URL 交给探测回调）。"""
        result = self._probe_url(self.render(url))
        self._apply_probe_result(url, result)
        return result

    def probe_all(self, max_workers: int = 4) -> dict:
        """并发探测全部节点（低并发避免压垮远端网关），返回统计。"""
        urls = list(self.urls)
        if not urls:
            return {"total": 0, "healthy": 0, "unreachable": 0}

        def worker(url: str):
            return url, self._probe_url(self.render(self._expand_template(url, "probe", "")))

        workers = max(1, min(int(max_workers or 1), len(urls)))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = dict(executor.map(worker, urls))

        now = time.time()
        healthy = 0
        unreachable = 0
        with self._lock:
            for url in urls:
                result = results.get(url) or {}
                state = self._nodes.get(url) or NodeState()
                state.probe_at = now
                if result.get("ok"):
                    state.status = STATUS_HEALTHY
                    state.egress_ip = str(result.get("egress_ip") or "")[:120]
                    latency = result.get("latency_ms")
                    state.latency_ms = int(latency) if latency is not None else None
                    state.last_error = ""
                    state.cooldown_until = 0.0
                    healthy += 1
                else:
                    state.status = STATUS_UNREACHABLE
                    state.last_error = str(result.get("error") or "")[:200]
                    unreachable += 1
                self._nodes[url] = state
                self._dirty = True
        self._persist_state()
        return {"total": len(urls), "healthy": healthy, "unreachable": unreachable}

    def mark_batch_probed(self) -> None:
        """标记本批次已完成统一探测；此后任务绑定不再逐节点探测。"""
        self._batch_probed = True

    def report_failure(self, url: str, reason: str = "") -> None:
        """业务失败（如风控）→ 节点进入冷却。"""
        if not url or self.cooldown_seconds <= 0:
            return
        now = time.time()
        with self._lock:
            state = self._nodes.get(url) or NodeState()
            state.status = STATUS_COOLDOWN
            state.cooldown_until = now + self.cooldown_seconds
            if reason:
                state.last_error = str(reason)[:200]
            self._nodes[url] = state
            self._dirty = True
        self._persist_state()

    def report_success(self, url: str, egress_ip: str = "", latency_ms: Optional[int] = None) -> None:
        """任务成功 → 节点保持/恢复健康并记录出口信息。"""
        if not url:
            return
        now = time.time()
        with self._lock:
            state = self._nodes.get(url) or NodeState()
            state.status = STATUS_HEALTHY
            state.cooldown_until = 0.0
            state.last_used_at = now
            if egress_ip:
                state.egress_ip = str(egress_ip)[:120]
            if latency_ms is not None:
                state.latency_ms = int(latency_ms)
            state.last_error = ""
            self._nodes[url] = state
            self._dirty = True
        self._persist_state()

    def clear_cooldown(self, url: str) -> bool:
        """复位节点：冷却 / 隔离 → 健康（探测成功同样会复位）。"""
        changed = False
        with self._lock:
            state = self._nodes.get(url)
            if state is not None and state.status in (STATUS_COOLDOWN, STATUS_FLAGGED):
                state.status = STATUS_HEALTHY
                state.cooldown_until = 0.0
                state.last_error = ""
                self._dirty = True
                changed = True
        if changed:
            self._persist_state()
        return changed

    def healthy_count(self) -> int:
        now = time.time()
        with self._lock:
            return len(self._healthy_urls_locked(now))

    def node_list(self) -> list[dict]:
        """面板用节点快照；``key`` 供 API 往返定位（原文不出网）。"""
        now = time.time()
        with self._lock:
            return [
                {
                    "key": node_key(url),
                    "url": url,
                    "status": self._status_of(url, now),
                    "cooldown_remaining": (
                        max(int(self._nodes[url].cooldown_until - now), 0)
                        if self._nodes.get(url)
                        else 0
                    ),
                    "last_used_at": self._nodes[url].last_used_at if self._nodes.get(url) else 0.0,
                    "egress_ip": self._nodes[url].egress_ip if self._nodes.get(url) else "",
                    "latency_ms": self._nodes[url].latency_ms if self._nodes.get(url) else None,
                    "last_error": self._nodes[url].last_error if self._nodes.get(url) else "",
                    "probe_at": self._nodes[url].probe_at if self._nodes.get(url) else 0.0,
                }
                for url in self.urls
            ]

    def find_url_by_key(self, key: str) -> str:
        wanted = str(key or "").strip()
        for url in self.urls:
            if node_key(url) == wanted:
                return url
        return ""

    def add_urls(self, lines: list[str]) -> dict:
        """批量导入：校验 + 去重 + 追加。"""
        added: list[str] = []
        invalid: list[str] = []
        with self._lock:
            existing = set(self.urls)
            for line in lines:
                value = str(line or "").strip()
                if not value:
                    continue
                try:
                    validate_http_proxy_url(value)
                except ValueError:
                    invalid.append(value)
                    continue
                if value in existing:
                    continue
                existing.add(value)
                self.urls.append(value)
                added.append(value)
                self._dirty = True
        self._persist_state()
        return {"added": added, "invalid": invalid}

    def remove_url(self, url: str) -> bool:
        with self._lock:
            if url not in self.urls:
                return False
            self.urls.remove(url)
            self._nodes.pop(url, None)
            self._leases = {
                key: leased for key, leased in self._leases.items() if leased != url
            }
            self._dirty = True
        self._persist_state()
        return True

    def clear(self) -> int:
        """清空全部节点与状态，返回移除数量。"""
        with self._lock:
            removed = len(self.urls)
            self.urls = []
            self._nodes.clear()
            self._leases.clear()
            self._usage.clear()
            self._dirty = True
        self._persist_state()
        return removed


# ---------------------------------------------------------------------------
# 配置构建
# ---------------------------------------------------------------------------

def _normalize_mode(raw: str) -> str:
    value = (str(raw or "").strip().lower()) or MODE_STATIC
    return value if value in VALID_MODES else MODE_STATIC


def _detect_mode(proxy_value: str, raw_mode: str) -> str:
    """未显式指定 proxy_mode 时按内容自动推断。"""
    if raw_mode:
        return raw_mode
    lines = [line.strip() for line in proxy_value.splitlines() if line.strip()]
    if len(lines) > 1:
        return MODE_POOL
    if PLACEHOLDER_ACCOUNT in proxy_value or PLACEHOLDER_EMAIL in proxy_value:
        return MODE_STICKY_TEMPLATE
    return MODE_STATIC


def detect_mode(proxy_value: str, raw_mode: str = "") -> str:
    """按配置推断代理模式（显式模式优先，供管理端导入/清空前判断）。"""
    value = str(raw_mode or "").strip().lower()
    if value in VALID_MODES:
        return value
    return _detect_mode(proxy_value, value)


def build_pool_from_config(
    config_get: Callable[[str, object], object],
    file_read: Optional[Callable[[str], str]] = None,
    state_file: str = "",
    probe: Optional[Callable[[str], dict]] = None,
    ip_flagged: Optional[Callable[[str], bool]] = None,
) -> ProxyPool:
    """按配置构建 ProxyPool；config_get(key, default) 读取配置项。"""
    proxy_value = str(config_get("proxy", "") or "").strip()
    raw_mode = str(config_get("proxy_mode", "") or "").strip().lower()
    if raw_mode and raw_mode not in VALID_MODES:
        raise ProxyConfigError(f"proxy_mode 无效: {raw_mode}")
    mode = _detect_mode(proxy_value, raw_mode)

    selection = str(config_get("proxy_selection", SELECTION_ROUND_ROBIN) or "").strip().lower()
    if selection and selection not in VALID_SELECTIONS:
        raise ProxyConfigError(f"proxy_selection 无效: {selection}")
    selection = selection or SELECTION_ROUND_ROBIN

    sticky_scope = str(config_get("proxy_sticky_scope", STICKY_SCOPE_TASK) or "").strip().lower()
    if sticky_scope and sticky_scope not in VALID_STICKY_SCOPES:
        raise ProxyConfigError(f"proxy_sticky_scope 无效: {sticky_scope}")
    sticky_scope = sticky_scope or STICKY_SCOPE_TASK

    username = str(config_get("proxy_username", "") or "").strip()
    password = str(config_get("proxy_password", "") or "").strip()

    try:
        cooldown_seconds = int(config_get("proxy_cooldown_seconds", 600) or 600)
    except (TypeError, ValueError):
        cooldown_seconds = 600
    cooldown_seconds = max(cooldown_seconds, 0)

    probe_once = bool(config_get("proxy_probe_once_per_batch", True))

    urls: list[str] = []
    if mode == MODE_POOL:
        candidates = [line.strip() for line in proxy_value.splitlines() if line.strip()]
        proxy_file = str(config_get("proxy_file", "") or "").strip()
        if proxy_file and file_read is not None:
            try:
                content = file_read(proxy_file)
            except OSError:
                content = ""
            candidates.extend(
                [line.strip() for line in content.splitlines() if line.strip()]
            )
        for candidate in candidates:
            try:
                validate_http_proxy_url(candidate)
            except ValueError as exc:
                raise ProxyConfigError(f"代理池条目无效: {exc}") from exc
            if candidate not in urls:
                urls.append(candidate)
        if not urls:
            raise ProxyConfigError("proxy_mode=pool 但代理池为空")
    else:
        if not proxy_value:
            return ProxyPool(
                MODE_STATIC,
                [],
                selection,
                sticky_scope,
                username,
                password,
                cooldown_seconds=cooldown_seconds,
                state_file=state_file,
                probe=probe,
                ip_flagged=ip_flagged,
                probe_once_per_batch=probe_once,
            )
        try:
            validate_http_proxy_url(proxy_value)
        except ValueError as exc:
            raise ProxyConfigError(f"代理配置无效: {exc}") from exc
        urls = [proxy_value]

    return ProxyPool(
        mode,
        urls,
        selection,
        sticky_scope,
        username,
        password,
        cooldown_seconds=cooldown_seconds,
        state_file=state_file,
        probe=probe,
        ip_flagged=ip_flagged,
        probe_once_per_batch=probe_once,
    )


# ---------------------------------------------------------------------------
# 模块级单例与线程作用域
# ---------------------------------------------------------------------------

_tls = threading.local()
_pool_lock = threading.Lock()
_pool: Optional[ProxyPool] = None
_pool_error = ""
_build_pool: Optional[Callable[[], ProxyPool]] = None
_config_signature: Optional[Callable[[], tuple]] = None
_pool_signature: Optional[tuple] = None


def configure_proxy_pool(
    build_pool: Callable[[], ProxyPool],
    config_signature: Optional[Callable[[], tuple]] = None,
) -> None:
    """注入池构建函数与配置签名（engine 启动时调用），并立即重建。"""
    global _build_pool, _config_signature
    _build_pool = build_pool
    _config_signature = config_signature
    reload_proxy_pool()


def reload_proxy_pool() -> None:
    """配置变更后重建池（Web 保存配置、启动加载配置时调用）。"""
    global _pool, _pool_signature, _pool_error
    if _build_pool is None:
        return
    with _pool_lock:
        try:
            _pool = _build_pool()
            _pool_error = ""
        except ProxyConfigError as exc:
            _pool = ProxyPool(MODE_STATIC, [])
            _pool_error = str(exc)
        _pool_signature = _config_signature() if _config_signature else None


def pool_error() -> str:
    return _pool_error


def _config_changed() -> bool:
    if _config_signature is None:
        return False
    try:
        return _config_signature() != _pool_signature
    except Exception:
        return False


def _current_pool() -> ProxyPool:
    global _pool, _pool_error
    if _pool is None or _config_changed():
        with _pool_lock:
            if _pool is None or _config_changed():
                if _build_pool is not None:
                    try:
                        _pool = _build_pool()
                        _pool_error = ""
                    except ProxyConfigError as exc:
                        _pool = ProxyPool(MODE_STATIC, [])
                        _pool_error = str(exc)
                    _pool_signature = _config_signature() if _config_signature else None
                else:
                    _pool = ProxyPool(MODE_STATIC, [])
                    _pool_error = ""
    return _pool or ProxyPool(MODE_STATIC, [])


def get_pool() -> ProxyPool:
    """当前池实例（管理面板 / 引擎运维操作使用）。"""
    return _current_pool()


def bind_task(scope_key: str = "", email: str = "") -> str:
    """绑定当前线程的任务作用域，返回本次任务使用的代理 URL（渲染后）。

    浏览器启动与所有 HTTP 调用都会经由 engine.get_proxies() 使用该出口。
    池模式无健康节点或探测全部失败时抛出 ProxyPoolExhausted。
    """
    pool = _current_pool()
    use_url = pool.resolve(scope_key, email)
    template = pool.resolve_template(scope_key, email) or use_url
    rendered = pool.render(use_url)
    _tls.raw_url = template
    _tls.rendered_url = rendered
    _tls.scope_key = scope_key
    return rendered


def current_proxy_url() -> str:
    """当前任务绑定的代理 URL（渲染后）；未绑定时返回空串。"""
    return getattr(_tls, "rendered_url", "") or ""


def current_raw_url() -> str:
    """当前任务绑定的池条目原文（状态记录 / 上报用）。"""
    return getattr(_tls, "raw_url", "") or ""


def current_scope_key() -> str:
    return getattr(_tls, "scope_key", "") or ""


def current_node_status() -> str:
    """当前绑定节点的健康状态；未绑定时返回空串。"""
    raw = current_raw_url()
    if not raw:
        return ""
    return _current_pool()._status_of(raw, time.time())


def rebind_if_unhealthy(email: str = "") -> bool:
    """绑定节点不再健康时自动换节点；返回是否已切换（引擎据此重启浏览器）。"""
    raw = current_raw_url()
    scope = current_scope_key()
    if not raw or not scope:
        return False
    pool = _current_pool()
    changed, _previous, current = pool.rebind_if_unhealthy(scope, email)
    if changed:
        use_url = pool.resolve(scope, email)
        _tls.raw_url = current
        _tls.rendered_url = pool.render(use_url)
    return changed


def release_task() -> None:
    """任务结束释放作用域与池粘性租约。"""
    scope_key = getattr(_tls, "scope_key", "")
    if scope_key:
        try:
            _current_pool().release(scope_key)
        except Exception:
            pass
    _tls.raw_url = ""
    _tls.rendered_url = ""
    _tls.scope_key = ""


def fallback_proxy_url() -> str:
    """未绑定线程的兜底出口：仅 static / sticky_template 模式返回渲染后的配置代理。

    pool 模式返回空串——调用方不应回落到多行池文本，出口应由任务绑定提供。
    """
    pool = _current_pool()
    if pool.mode == MODE_POOL:
        return ""
    return pool.render(pool.urls[0]) if pool.urls else ""


def describe_pool() -> dict:
    """调试/UI 用：描述当前池状态（节点原文由 API 层决定是否脱敏）。"""
    pool = _current_pool()
    return {
        "mode": pool.mode,
        "selection": pool.selection,
        "sticky_scope": pool.sticky_scope,
        "cooldown_seconds": pool.cooldown_seconds,
        "probe_once_per_batch": pool.probe_once_per_batch,
        "count": len(pool.urls),
        "healthy": pool.healthy_count(),
        "error": _pool_error,
        "batch_probed": pool._batch_probed,
        "nodes": pool.node_list(),
    }
