"""代理池 / 粘性代理框架。

统一管理配置中的代理来源（单代理 / 代理池 / 粘性模板），并在任务线程内提供
贯穿账号全生命周期的稳定出口：

- ``bind_task(scope_key, email)``：任务开始前绑定，浏览器与所有 HTTP 调用同出口
- ``current_proxy_url()``：返回当前任务绑定的具体代理 URL
- ``release_task()``：任务结束释放（池代理的粘性租约随之释放）

设计原则：
- 无健康检测 / 冷却机制（注册用代理池质量整体较高，选择器保持简单）
- 池模式粘性由 scope_key 租约实现，同一账号/任务始终同一出口
- 模板模式（{account}/{email} 占位）直接渲染，天然粘性
- 原始凭据（proxy_username / proxy_password）由框架统一百分号编码
"""

from __future__ import annotations

import itertools
import random
import threading
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

VALID_MODES = frozenset({MODE_STATIC, MODE_POOL, MODE_STICKY_TEMPLATE})
VALID_SELECTIONS = frozenset(
    {SELECTION_ROUND_ROBIN, SELECTION_RANDOM, SELECTION_LEAST_USED}
)
VALID_STICKY_SCOPES = frozenset(
    {STICKY_SCOPE_NONE, STICKY_SCOPE_TASK, STICKY_SCOPE_ACCOUNT}
)


class ProxyConfigError(Exception):
    """代理池配置错误。"""


class ProxyPool:
    """解析并持有代理来源，提供选择与粘性租约。"""

    def __init__(
        self,
        mode: str,
        urls: list[str],
        selection: str = SELECTION_ROUND_ROBIN,
        sticky_scope: str = STICKY_SCOPE_TASK,
        username: str = "",
        password: str = "",
    ):
        self.mode = mode
        self.urls = urls
        self.selection = selection
        self.sticky_scope = sticky_scope
        self.username = username
        self.password = password
        self._lock = threading.Lock()
        self._rr = itertools.count()
        self._usage: dict[str, int] = {}
        self._leases: dict[str, str] = {}

    def empty(self) -> bool:
        return not self.urls

    def resolve(self, scope_key: str = "", email: str = "") -> str:
        """返回具体代理 URL（原始凭据已并入，未做 Docker 回环映射）。"""
        with self._lock:
            url = self._resolve_locked(scope_key, email)
        if not url:
            return ""
        return build_proxy_url_with_credentials(url, self.username, self.password)

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
                if scope_key not in self._leases:
                    self._leases[scope_key] = self._pick_locked()
                return self._leases[scope_key]
            return self._pick_locked()
        return self.urls[0]

    def _pick_locked(self) -> str:
        if self.selection == SELECTION_RANDOM:
            best = random.choice(self.urls)
        elif self.selection == SELECTION_LEAST_USED:
            best = min(self.urls, key=lambda url: self._usage.get(url, 0))
        else:
            best = self.urls[next(self._rr) % len(self.urls)]
        self._usage[best] = self._usage.get(best, 0) + 1
        return best

    def release(self, scope_key: str = "") -> None:
        if not scope_key:
            return
        with self._lock:
            self._leases.pop(scope_key, None)

    def url_list(self) -> list[str]:
        return list(self.urls)


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
            return ProxyPool(MODE_STATIC, [], selection, sticky_scope, username, password)
        try:
            validate_http_proxy_url(proxy_value)
        except ValueError as exc:
            raise ProxyConfigError(f"代理配置无效: {exc}") from exc
        urls = [proxy_value]

    return ProxyPool(mode, urls, selection, sticky_scope, username, password)


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
    """绑定当前线程的任务作用域，返回本次任务使用的代理 URL。

    浏览器启动与所有 HTTP 调用都应使用该返回值（或 current_proxy_url()）。
    """
    url = _current_pool().resolve(scope_key, email)
    _tls.proxy_url = url
    _tls.scope_key = scope_key
    return url


def current_proxy_url() -> str:
    """当前任务的代理 URL；未绑定时返回默认解析（static 或池轮选）。"""
    url = getattr(_tls, "proxy_url", "")
    if url:
        return url
    return _current_pool().resolve()


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
    _tls.proxy_url = ""
    _tls.scope_key = ""


def describe_pool() -> dict:
    """调试/UI 用：描述当前池状态（脱敏）。"""
    pool = _current_pool()
    return {
        "mode": pool.mode,
        "selection": pool.selection,
        "sticky_scope": pool.sticky_scope,
        "count": len(pool.urls),
    }
