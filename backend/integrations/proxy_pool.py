"""代理池 / 粘性代理框架（含节点健康管理与冷却）。

统一管理配置中的代理来源（单代理 / 代理池 / 粘性模板），并在任务线程内提供
贯穿账号全生命周期的稳定出口：

- ``bind_task(scope_key, email)``：任务开始前绑定，浏览器与所有 HTTP 调用同出口
- ``current_proxy_url()``：返回当前任务绑定的具体代理 URL
- ``release_task()``：任务结束释放（池代理的粘性租约随之释放）

健康管理：
- 节点状态：healthy / unreachable / cooldown（冷却剩余时间按秒计）
- 连通性探测（probe 回调注入）：失败 → unreachable；成功记录出口 IP 与延迟
- 业务失败（如账号被风控）通过 ``report_failure`` 进入冷却（时长可配）
- 选择器只从 healthy 节点中选择；无可用节点抛 ``ProxyPoolExhausted``
- 状态持久化到 JSON 文件（owner-only），重启不丢
"""

from __future__ import annotations

import itertools
import json
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from backend.integrations.proxy import (
    build_proxy_url_with_credentials,
    validate_http_proxy_url,
)

MODE_STATIC = "static"
MODE_POOL = "pool"
MODE_STICKY_TEMPLATE = "sticky_template"

SELECTION_ROUND_ROBIN = "round_robin"
SELECTION_RANDOM = "random"
SELECTION_LEAST_USED = "least_used"

STICKY_SCOPE_NONE = "none"
STICKY_SCOPE_TASK = "task"
STICKY_SCOPE_ACCOUNT = "account"

PLACEHOLDER_ACCOUNT = "{account}"
PLACEHOLDER_EMAIL = "{email}"

STATUS_HEALTHY = "healthy"
STATUS_UNREACHABLE = "unreachable"
STATUS_COOLDOWN = "cooldown"

VALID_MODES = frozenset({MODE_STATIC, MODE_POOL, MODE_STICKY_TEMPLATE})
VALID_SELECTIONS = frozenset(
    {SELECTION_ROUND_ROBIN, SELECTION_RANDOM, SELECTION_LEAST_USED}
)
VALID_STICKY_SCOPES = frozenset(
    {STICKY_SCOPE_NONE, STICKY_SCOPE_TASK, STICKY_SCOPE_ACCOUNT}
)
VALID_STATUSES = frozenset({STATUS_HEALTHY, STATUS_UNREACHABLE, STATUS_COOLDOWN})


class ProxyConfigError(Exception):
    """代理池配置错误。"""


class ProxyPoolExhausted(Exception):
    """代理池没有可用（健康）节点。"""


class NodeState:
    __slots__ = (
        "status", "cooldown_until", "last_used_at", "egress_ip",
        "latency_ms", "last_error", "probe_at",
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
        state.status = data.get("status") if data.get("status") in VALID_STATUSES else STATUS_HEALTHY
        state.cooldown_until = float(data.get("cooldown_until") or 0)
        state.last_used_at = float(data.get("last_used_at") or 0)
        state.egress_ip = str(data.get("egress_ip") or "")
        latency = data.get("latency_ms")
        state.latency_ms = int(latency) if latency is not None else None
        state.last_error = str(data.get("last_error") or "")
        state.probe_at = float(data.get("probe_at") or 0)
        return state


class ProxyPool:
    """解析并持有代理来源，提供选择、粘性租约与健康管理。"""

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
    ):
        self.mode = mode
        self.urls = urls
        self.selection = selection
        self.sticky_scope = sticky_scope
        self.username = username
        self.password = password
        self.cooldown_seconds = max(int(cooldown_seconds or 0), 0)
        self.state_file = state_file
        self.probe = probe
        self._lock = threading.Lock()
        self._rr = itertools.count()
        self._usage: dict[str, int] = {}
        self._leases: dict[str, str] = {}
        self._nodes: dict[str, NodeState] = {}
        self._dirty = False
        self._load_state()

    # ------------------------------------------------------------------
    # 状态持久化
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
            payload = {url: self._nodes[url].to_dict() for url in self._nodes}
            self._dirty = False
        directory = os.path.dirname(self.state_file)
        if directory:
            os.makedirs(directory, exist_ok=True)
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

    def _note_state(self, url: str, state: NodeState) -> None:
        with self._lock:
            self._nodes[url] = state
            self._dirty = True

    # ------------------------------------------------------------------
    # 基础行为
    # ------------------------------------------------------------------
    def empty(self) -> bool:
        return not self.urls

    def resolve(self, scope_key: str = "", email: str = "") -> str:
        """返回具体代理 URL（原始形式，未并入 proxy_username/password）。"""
        with self._lock:
            return self._resolve_locked(scope_key, email)

    def render(self, url: str) -> str:
        """把原始 URL 渲染为实际使用的 URL（并入原始凭据）。"""
        if not url:
            return ""
        return build_proxy_url_with_credentials(url, self.username, self.password)

    def _probe_url(self, url: str) -> dict:
        """探测节点（使用渲染后的 URL）；probe 未注入时视为通过。"""
        if self.probe is None:
            return {"ok": True, "egress_ip": "", "latency_ms": None, "error": ""}
        rendered = self.render(url)
        try:
            result = self.probe(rendered) or {}
        except Exception as exc:
            return {"ok": False, "egress_ip": "", "latency_ms": None, "error": str(exc)[:200]}
        if not result.get("ok"):
            return {
                "ok": False,
                "egress_ip": "",
                "latency_ms": None,
                "error": str(result.get("error") or "probe 无返回")[:200],
            }
        return result

    def _status_of(self, url: str, now: float) -> str:
        state = self._nodes.get(url)
        if state is None:
            return STATUS_HEALTHY
        if state.status == STATUS_COOLDOWN:
            if now >= state.cooldown_until:
                return STATUS_HEALTHY
            return STATUS_COOLDOWN
        return state.status

    def _healthy_urls_locked(self, now: float) -> list[str]:
        return [url for url in self.urls if self._status_of(url, now) == STATUS_HEALTHY]

    def _resolve_locked(self, scope_key: str, email: str) -> str:
        if not self.urls:
            return ""
        if self.mode == MODE_STICKY_TEMPLATE:
            template = self.urls[0]
            if PLACEHOLDER_ACCOUNT in template:
                return template.replace(PLACEHOLDER_ACCOUNT, scope_key or "")
            local = "".join(
                char for char in str(email or "").split("@", 1)[0] if char.isalnum()
            ).lower()
            return template.replace(PLACEHOLDER_EMAIL, local)
        if self.mode == MODE_POOL:
            if self.sticky_scope != STICKY_SCOPE_NONE and scope_key:
                if scope_key in self._leases:
                    return self._leases[scope_key]
                picked = self._pick_locked()
                self._leases[scope_key] = picked
                return picked
            return self._pick_locked()
        return self.urls[0]

    def _pick_locked(self) -> str:
        now = time.time()
        available = self._healthy_urls_locked(now)
        if not available:
            raise ProxyPoolExhausted("代理池没有健康节点可用")

        order: list[str]
        if self.selection == SELECTION_RANDOM:
            order = list(available)
            random.shuffle(order)
        elif self.selection == SELECTION_LEAST_USED:
            order = sorted(available, key=lambda url: self._usage.get(url, 0))
        else:
            order = available

        start = 0
        if self.selection == SELECTION_ROUND_ROBIN:
            start = next(self._rr) % len(order)

        last_error = ""
        for offset in range(len(order)):
            url = order[(start + offset) % len(order)]
            if self.probe is not None:
                result = self._probe_url(url)
                state = self._nodes.get(url) or NodeState()
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
                    last_error = state.last_error
                    self._nodes[url] = state
                    self._dirty = True
                    continue
                self._nodes[url] = state
            self._usage[url] = self._usage.get(url, 0) + 1
            state = self._nodes.get(url)
            if state is None:
                state = NodeState()
                self._nodes[url] = state
            state.last_used_at = now
            self._dirty = True
            self._persist_state()
            return url

        raise ProxyPoolExhausted(f"代理池节点探测全部失败: {last_error}")

    def release(self, scope_key: str = "") -> None:
        if not scope_key:
            return
        with self._lock:
            self._leases.pop(scope_key, None)

    def url_list(self) -> list[str]:
        return list(self.urls)

    # ------------------------------------------------------------------
    # 健康管理
    # ------------------------------------------------------------------
    def probe_node(self, url: str) -> dict:
        result = self._probe_url(url)
        now = time.time()
        state = self._nodes.get(url) or NodeState()
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
        self._note_state(url, state)
        self._persist_state()
        return result

    def probe_all(self, max_workers: int = 16) -> dict:
        """并发探测全部节点，返回统计。"""
        urls = list(self.urls)
        if not urls:
            return {"total": 0, "healthy": 0, "unreachable": 0}
        results: dict[str, dict] = {}

        def worker(url: str):
            results[url] = self._probe_url(url)

        with ThreadPoolExecutor(max_workers=min(max_workers, len(urls))) as executor:
            list(executor.map(worker, urls))

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

    def clear_cooldown(self, url: str) -> None:
        with self._lock:
            state = self._nodes.get(url)
            if state is None:
                return
            if state.status == STATUS_COOLDOWN:
                state.status = STATUS_HEALTHY
                state.cooldown_until = 0.0
                state.last_error = ""
                self._dirty = True
        self._persist_state()

    def healthy_count(self) -> int:
        now = time.time()
        with self._lock:
            return len(self._healthy_urls_locked(now))

    def node_list(self) -> list[dict]:
        """面板用节点快照（未脱敏，由 API 层脱敏）。"""
        now = time.time()
        with self._lock:
            return [
                {
                    "url": url,
                    "status": self._status_of(url, now),
                    "cooldown_remaining": max(
                        int(self._nodes[url].cooldown_until - now), 0
                    ) if self._nodes.get(url) else 0,
                    "last_used_at": self._nodes[url].last_used_at if self._nodes.get(url) else 0.0,
                    "egress_ip": self._nodes[url].egress_ip if self._nodes.get(url) else "",
                    "latency_ms": self._nodes[url].latency_ms if self._nodes.get(url) else None,
                    "last_error": self._nodes[url].last_error if self._nodes.get(url) else "",
                    "probe_at": self._nodes[url].probe_at if self._nodes.get(url) else 0.0,
                }
                for url in self.urls
            ]

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
    return (str(raw or "").strip().lower()) or MODE_STATIC


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


def build_pool_from_config(
    config_get: Callable[[str, object], object],
    file_read: Optional[Callable[[str], str]] = None,
    state_file: str = "",
    probe: Optional[Callable[[str], dict]] = None,
) -> ProxyPool:
    """按配置构建 ProxyPool。config_get(key, default) 读取配置项。"""
    proxy_value = str(config_get("proxy", "") or "").strip()
    raw_mode = str(config_get("proxy_mode", "") or "").strip().lower()
    if raw_mode and raw_mode not in VALID_MODES:
        raise ProxyConfigError(f"proxy_mode 无效: {raw_mode}")
    mode = _detect_mode(proxy_value, raw_mode)

    selection = str(config_get("proxy_selection", SELECTION_ROUND_ROBIN) or "").strip().lower()
    if selection not in VALID_SELECTIONS:
        raise ProxyConfigError(f"proxy_selection 无效: {selection}")

    sticky_scope = str(config_get("proxy_sticky_scope", STICKY_SCOPE_TASK) or "").strip().lower()
    if sticky_scope not in VALID_STICKY_SCOPES:
        raise ProxyConfigError(f"proxy_sticky_scope 无效: {sticky_scope}")

    username = str(config_get("proxy_username", "") or "").strip()
    password = str(config_get("proxy_password", "") or "").strip()

    try:
        cooldown_seconds = int(config_get("proxy_cooldown_seconds", 600) or 600)
    except (TypeError, ValueError):
        cooldown_seconds = 600
    cooldown_seconds = max(cooldown_seconds, 0)

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
            urls.append(candidate)
        if not urls:
            raise ProxyConfigError("proxy_mode=pool 但代理池为空")
    else:
        if not proxy_value:
            return ProxyPool(
                MODE_STATIC, [], selection, sticky_scope, username, password,
                cooldown_seconds=cooldown_seconds, state_file=state_file, probe=probe,
            )
        try:
            validate_http_proxy_url(proxy_value)
        except ValueError as exc:
            raise ProxyConfigError(f"代理配置无效: {exc}") from exc
        urls = [proxy_value]

    return ProxyPool(
        mode, urls, selection, sticky_scope, username, password,
        cooldown_seconds=cooldown_seconds, state_file=state_file, probe=probe,
    )


# ---------------------------------------------------------------------------
# 任务作用域（thread-local）
# ---------------------------------------------------------------------------

_tls = threading.local()
_pool_lock = threading.Lock()
_pool: Optional[ProxyPool] = None
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
    global _pool, _pool_signature
    if _build_pool is None:
        return
    with _pool_lock:
        _pool = _build_pool()
        _pool_signature = _config_signature() if _config_signature else None


def _config_changed() -> bool:
    if _config_signature is None:
        return False
    try:
        return _config_signature() != _pool_signature
    except Exception:
        return False


def _current_pool() -> ProxyPool:
    global _pool
    if _pool is None or _config_changed():
        with _pool_lock:
            if _pool is None or _config_changed():
                if _build_pool is not None:
                    _pool = _build_pool()
                    _pool_signature = _config_signature() if _config_signature else None
                else:
                    _pool = ProxyPool(MODE_STATIC, [])
    return _pool or ProxyPool(MODE_STATIC, [])


def bind_task(scope_key: str = "", email: str = "") -> str:
    """绑定当前线程的任务作用域，返回本次任务使用的代理 URL（渲染后）。

    浏览器启动与所有 HTTP 调用都应使用该返回值（或 current_proxy_url()）。
    池模式无健康节点、或候选节点探测全部失败时抛出 ProxyPoolExhausted。
    """
    pool = _current_pool()
    raw = pool.resolve(scope_key, email)
    rendered = pool.render(raw)
    _tls.raw_url = raw
    _tls.rendered_url = rendered
    _tls.scope_key = scope_key
    return rendered


def current_proxy_url() -> str:
    """当前任务的代理 URL（渲染后）；未绑定时返回默认解析。"""
    url = getattr(_tls, "rendered_url", "")
    if url:
        return url
    pool = _current_pool()
    return pool.render(pool.resolve())


def current_raw_url() -> str:
    """当前任务绑定的原始代理 URL（状态记录/上报用）。"""
    return getattr(_tls, "raw_url", "") or ""


def current_node_exit_ip() -> str:
    """当前任务绑定节点的探测出口 IP（无则空串）。"""
    raw = current_raw_url()
    if not raw:
        return ""
    pool = _current_pool()
    for node in pool.node_list():
        if node.get("url") == raw:
            return str(node.get("egress_ip") or "")
    return ""


def current_scope_key() -> str:
    return getattr(_tls, "scope_key", "") or ""


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


def describe_pool() -> dict:
    """调试/UI 用：描述当前池状态（脱敏）。"""
    pool = _current_pool()
    return {
        "mode": pool.mode,
        "selection": pool.selection,
        "sticky_scope": pool.sticky_scope,
        "cooldown_seconds": pool.cooldown_seconds,
        "count": len(pool.urls),
        "healthy": pool.healthy_count() if pool.mode == MODE_POOL else None,
    }


def get_pool() -> ProxyPool:
    """返回当前池实例（面板/引擎运维操作使用）。"""
    return _current_pool()
