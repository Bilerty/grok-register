# -*- coding: utf-8 -*-
"""GrokIQ 回调未降智 → 推送生产号池 + 代理出口保护（fork 定制）。

流程：注册机收到 GrokIQ 检测回调且判定"未降智"（严格口径：degraded=false、
isolated=false、probe_outcome=passed）后，把该账号推送至生产 grok2api：

- 每个选定域（build/web/console，且与"Grok2API 目标"域取交集）独立执行：
  上传本地授权 JSON → 按 email 反查生产账号 id → 出口保护绑定
- 出口保护：取该账号在 staging（编排内 grok2api）绑定的出口节点名，解析
  末段 sid（``提供商-国家-ASN-...-<sid>``），在生产同 scope 出口节点中找
  同 sid 节点绑定；找不到则随机挑一个（enabled 且已配代理）
- 全部选定域推送成功后禁用 staging 侧同账号，避免同一 SSO 双池活跃

执行在持久 outbox（store.prod_push_outbox）+ 单 daemon 线程 worker 中进行，
失败按指数退避重试（上限 store.PROD_PUSH_MAX_ATTEMPTS），重启自动恢复。
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from backend.integrations import auth_exchange
from backend.integrations.grok2api_client import Grok2APIClient, Grok2APIImportError
from backend.integrations.proxy import redact_proxy_text
from backend.integrations.proxy_pool import MODE_POOL, get_pool, parse_proxy_entry

logger = logging.getLogger(__name__)

# 渠道 -> (生产推送开关键, staging 目标开关键)；生产推送域是两者的交集
DOMAIN_KEYS: Dict[str, Tuple[str, str]] = {
    "grok_build": ("prod_push_build", "grok2api_auto_import_build"),
    "grok_web": ("prod_push_web", "grok2api_auto_import_web"),
    "grok_console": ("prod_push_console", "grok2api_auto_import_console"),
}

PROD_CONFIG_KEYS = (
    "prod_grok2api_remote_url",
    "prod_grok2api_remote_username",
    "prod_grok2api_remote_password",
)


def extract_sid(node_name: str) -> str:
    """解析节点名称最后一个 ``-`` 之后的 sid；无分隔符返回原名。"""
    value = str(node_name or "").strip()
    if not value:
        return ""
    return value.rsplit("-", 1)[-1].strip()


def is_pushable_grokiq_result(payload: Dict[str, Any] | None) -> Tuple[bool, str]:
    """严格判定回调结果是否"证实未降智"可推送生产号池。

    口径：degraded=false 且 isolated=false 且 probe_outcome=passed。
    verdict 字段在部分回调里缺省，故不作为必要条件。
    """
    data = payload or {}
    if bool(data.get("degraded")):
        return False, "GrokIQ 判定降智"
    if bool(data.get("isolated")):
        return False, "GrokIQ 已隔离该账号"
    outcome = str(data.get("probe_outcome") or "").strip().lower()
    if outcome != "passed":
        return False, f"探针未通过（probe_outcome={outcome or '空'}）"
    return True, ""


def apply_risk_response(
    repository: Any,
    payload: Dict[str, Any] | None,
    registration_id: int,
    email: str,
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """GrokIQ 回调判定非"正常"时的风控后处理（fork 定制）。

    定位口径：**注册该账号时实际使用的出口**（判定所用的 staging 探针出口
    与注册出口是两个不同代理，不作匹配对象）。

    1. 从注册记录读取当时绑定的代理池节点（extra.pool_node.entry）→
       report_failure 冷却（后续注册不再选中该节点）
    2. 把注册时浏览器实测的出口 IP（extra.exit_ip）记入"出口 IP 风控名单"
       （之后注册遇同出口 IP 自动重启换出口）
    3. 结果由调用方写入 extra_json.risk_response 留痕
    全程不抛异常（失败记录到返回值的 error 字段）；历史账号无 pool_node
    记录时仅执行出口 IP 部分。
    """
    data = payload or {}
    verdict = str(data.get("verdict") or data.get("monitor_status") or "").strip()
    reasons = data.get("risk_reasons") or []
    reason_text = "；".join(str(r) for r in reasons)[:400] if isinstance(reasons, list) else str(reasons)[:400]

    result: Dict[str, Any] = {
        "status": "failed",
        "verdict": verdict,
        "pool_node": "",
        "cooldown_applied": False,
        "exit_ip": "",
        "exit_ip_recorded": False,
        "error": "",
    }

    try:
        records = repository.get_results_by_ids([int(registration_id)])
    except Exception as exc:
        result["error"] = f"读取注册记录失败: {str(exc)[:200]}"
        return result
    if not records:
        result["error"] = "未找到对应注册记录"
        return result
    record = records[0]
    extra = record.get("extra") or record.get("extra_json") or {}
    if isinstance(extra, str):
        import json as _json

        try:
            extra = _json.loads(extra or "{}")
        except ValueError:
            extra = {}
    if not isinstance(extra, dict):
        extra = {}

    # 1) 冷却注册时绑定的代理池节点（条目原文精确匹配，无需 SID）
    pool_node = extra.get("pool_node") or {}
    node_entry = str((pool_node or {}).get("entry") or "").strip()
    if node_entry:
        pool = get_pool()
        if pool.mode == MODE_POOL and node_entry in pool.url_list():
            pool.report_failure(
                node_entry,
                reason=f"GrokIQ 判定降智/风险: {verdict} {reason_text}"[:200],
            )
            result["cooldown_applied"] = True
            result["pool_node"] = redact_proxy_text(node_entry)
        else:
            result["error"] = "该节点已不在本机代理池中（未冷却）"
    else:
        result["error"] = "注册记录缺少所用节点信息（历史账号），仅执行出口 IP 记录"

    # 2) 注册时浏览器实测出口 IP 记入风控名单
    exit_ip = str(extra.get("exit_ip") or "").strip()
    result["exit_ip"] = exit_ip
    if exit_ip:
        recorded = repository.remember_flagged_exit_ip(
            exit_ip,
            email=email,
            bot_flag_source=f"grokiq:{verdict or 'risk'}",
            failure_reason=reason_text or f"GrokIQ 判定 {verdict}",
        )
        result["exit_ip_recorded"] = bool(recorded)
    else:
        result["error"] = (result["error"] + "；注册未记录出口 IP").lstrip("；")

    if result["cooldown_applied"] or result["exit_ip_recorded"]:
        result["status"] = "processed"
    elif result["status"] != "failed":
        result["status"] = "failed"
    return result


def selected_domains(config: Dict[str, Any]) -> Tuple[str, ...]:
    """生产推送域 = prod_push_<域> 开关 ∩ staging"Grok2API 目标"域。"""
    if not bool(config.get("grok2api_auto_import", False)):
        return ()
    selected: List[str] = []
    for domain, (prod_key, staging_key) in DOMAIN_KEYS.items():
        default = Grok2APIClient.AUTO_IMPORT_DEFAULTS[domain]
        if bool(config.get(prod_key, False)) and bool(config.get(staging_key, default)):
            selected.append(domain)
    return tuple(selected)


def prod_client_from_config(config: Dict[str, Any]) -> Optional[Grok2APIClient]:
    """按 prod_* 配置构造生产 grok2api 客户端；未配置返回 None。"""
    values = [str(config.get(key, "") or "").strip() for key in PROD_CONFIG_KEYS]
    if not all(values):
        return None
    return Grok2APIClient(values[0], values[1], values[2])


def staging_sid_for_account(
    staging_client: Grok2APIClient,
    provider: str,
    email: str,
    node_cache: Dict[str, Dict[str, str]],
) -> str:
    """查 staging 上该账号绑定的出口节点名并解析 sid；未绑定/查不到返回空串。"""
    account = staging_client.search_account(provider, email)
    if not account:
        return ""
    node_id = str(account.get("egress_node_id") or "").strip()
    if not node_id:
        return ""
    if node_id not in node_cache:
        node_cache[node_id] = ""
        for node in staging_client.list_egress_nodes(Grok2APIClient.PROVIDER_NODE_SCOPES[provider]):
            node_cache[node["id"]] = node["name"]
    return extract_sid(node_cache.get(node_id, ""))


def pick_prod_node(
    prod_client: Grok2APIClient,
    provider: str,
    sid: str,
    node_cache: Dict[str, Tuple[str, List[Dict[str, Any]]]],
) -> Dict[str, Any]:
    """出口保护选节点：优先 sid 精确匹配，否则随机一个可用节点。

    可用 = enabled 且已配置代理；scope 与 provider 兼容（console 用
    grok_console scope，其余一一对应）。返回 {"node", "matched"}。
    """
    scope = Grok2APIClient.PROVIDER_NODE_SCOPES[provider]
    if scope not in node_cache:
        node_cache[scope] = (time.time(), prod_client.list_egress_nodes(scope))
    _fetched_at, nodes = node_cache[scope]
    usable = [n for n in nodes if n["enabled"] and n["proxy_configured"]]
    if not usable:
        raise Grok2APIImportError(f"生产号池没有可用的 {scope} 出口节点")
    if sid:
        matched = [n for n in usable if extract_sid(n["name"]) == sid]
        if matched:
            return {"node": matched[0], "matched": True}
    return {"node": random.choice(usable), "matched": False}


def execute_prod_push(
    repository: Any,
    registration_id: int,
    email: str,
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """执行一次生产推送；返回写入 extra_json.prod_push 的结果对象。

    重试安全：先读取既有结果，已 pushed 的域跳过（import/绑定均幂等，
    但跳过可避免无谓请求与随机节点漂移）。
    """
    result: Dict[str, Any] = {
        "status": "failed",
        "domains": {},
        "guard_enabled": bool(config.get("prod_push_egress_guard", True)),
        "pushed_at": "",
        "error": "",
    }

    if not bool(config.get("prod_push_enabled", False)):
        result["status"] = "skipped"
        result["error"] = "正式号池推送开关未开启"
        return result
    if prod_client_from_config(config) is None:
        result["status"] = "skipped"
        result["error"] = "生产号池 API 地址/管理员账号/密码未配置完整"
        return result
    domains = selected_domains(config)
    if not domains:
        result["status"] = "skipped"
        result["error"] = "没有选定任何可推送域"
        return result

    staging_client: Optional[Grok2APIClient] = None
    if Grok2APIClient.is_configured(config):
        staging_client = Grok2APIClient.from_config(config)
    prod_client = prod_client_from_config(config)

    previous: Dict[str, Any] = {}
    # 邮箱大小写修正：webhook 入队时 email 被小写化，而授权 JSON 文件名
    # 保留注册时的原始大小写，文件查找必须用 DB 记录的原始 email。
    try:
        records = repository.get_results_by_ids([int(registration_id)])
    except Exception:
        records = []
    if records:
        original_email = str(records[0].get("email") or "").strip() or email
    else:
        original_email = email
        records = []
    if records:
        extra = records[0].get("extra") or records[0].get("extra_json") or {}
        if isinstance(extra, str):
            import json as _json

            extra = _json.loads(extra or "{}")
        previous = (extra.get("prod_push") or {}).get("domains") or {}

    auth_files = auth_exchange.find_grok2api_auth_files(
        original_email, config.get("grok2api_auth_dir", "")
    )

    staging_node_cache: Dict[str, Dict[str, str]] = {}
    prod_node_cache: Dict[str, Tuple[str, List[Dict[str, Any]]]] = {}
    pushed_any = False
    all_pushed = True

    for domain in domains:
        prior = previous.get(domain) or {}
        if prior.get("status") == "pushed":
            result["domains"][domain] = dict(prior)
            pushed_any = True
            continue
        entry: Dict[str, Any] = {"status": "failed", "matched": False, "sid": ""}
        try:
            file_path = auth_files.get(domain)
            if file_path is None:
                raise Grok2APIImportError(f"本地缺少 {domain} 授权 JSON")
            prod_client.import_auth_file(file_path, domain)
            account = prod_client.search_account(domain, email)
            if not account or not account.get("id"):
                raise Grok2APIImportError("推送后未能在生产号池反查到账号")
            entry["prod_account_id"] = account["id"]

            guard = bool(config.get("prod_push_egress_guard", True))
            sid = ""
            if guard and staging_client is not None:
                sid = staging_sid_for_account(staging_client, domain, email, staging_node_cache)
            entry["sid"] = sid
            if guard:
                picked = pick_prod_node(prod_client, domain, sid, prod_node_cache)
                node = picked["node"]
                entry["matched"] = bool(picked["matched"])
            else:
                picked = pick_prod_node(prod_client, domain, "", prod_node_cache)
                node = picked["node"]
                entry["matched"] = False
            prod_client.assign_account(domain, node["id"], account["id"], "manual")
            entry["status"] = "pushed"
            entry["node_name"] = node["name"]
            entry["node_id"] = node["id"]
            pushed_any = True
        except Exception as exc:
            all_pushed = False
            entry["error"] = str(exc)[:300]
            logger.warning(
                "[ProdPush] %s 域推送失败 registration=%s: %s",
                domain,
                registration_id,
                entry["error"],
            )
        result["domains"][domain] = entry

    domain_states = [entry.get("status") for entry in result["domains"].values()]
    if domain_states and all(state == "pushed" for state in domain_states):
        result["status"] = "pushed"
    elif pushed_any:
        result["status"] = "partial"
    else:
        result["status"] = "failed"
    failed_domains = [
        f"{domain}: {entry.get('error') or '未成功'}"
        for domain, entry in result["domains"].items()
        if entry.get("status") != "pushed"
    ]
    result["error"] = "；".join(failed_domains)[:600]

    # 全部选定域成功后禁用 staging 同账号（防同一 SSO 双池活跃）
    if result["status"] == "pushed" and staging_client is not None:
        disable_errors: List[str] = []
        for domain in domains:
            try:
                account = staging_client.search_account(domain, email)
                if account and account.get("id"):
                    staging_client.disable_accounts(domain, [account["id"]])
            except Exception as exc:
                disable_errors.append(f"{domain}: {str(exc)[:160]}")
        if disable_errors:
            result["staging_disable_error"] = "；".join(disable_errors)[:600]

    return result


class ProdPushWorker:
    """持久 outbox 驱动的单线程推送 worker（模式与 GrokIQNotifier 一致）。"""

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.RLock()
        self._repository: Any = None
        self._config_provider: Optional[Callable[[], Dict[str, Any]]] = None

    def start(self, repository: Any, config_provider: Callable[[], Dict[str, Any]]) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._repository = repository
            self._config_provider = config_provider
            self._stop.clear()
            try:
                repository.recover_prod_push()
            except Exception as exc:
                logger.warning("[ProdPush] 恢复队列失败: %s", exc)
            self._thread = threading.Thread(
                target=self._run, name="grok-prod-push", daemon=True
            )
            self._thread.start()
            logger.info("[ProdPush] worker 已启动")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        with self._lock:
            self._thread = None

    def wake(self) -> None:
        self._wake.set()

    def enqueue(self, registration_id: int | str, email: str) -> Dict[str, Any]:
        with self._lock:
            repository = self._repository
        if repository is None:
            raise RuntimeError("ProdPush worker 尚未启动")
        event = repository.enqueue_prod_push(registration_id, email)
        self.wake()
        logger.info(
            "[ProdPush] 已入队 registration=%s event=%s",
            registration_id,
            event.get("event_id"),
        )
        return event

    def _run(self) -> None:
        while not self._stop.is_set():
            claimed = None
            try:
                with self._lock:
                    repository = self._repository
                    config_provider = self._config_provider
                if repository is None or config_provider is None:
                    return
                claimed = repository.claim_prod_push()
                if claimed is None:
                    self._wake.wait(timeout=2.0)
                    self._wake.clear()
                    continue
                registration_id = int(claimed.get("registration_id") or 0)
                email = str(claimed.get("email") or "")
                logger.info(
                    "[ProdPush] 开始推送 registration=%s email=%s attempt=%s",
                    registration_id,
                    email,
                    claimed.get("attempts"),
                )
                result = execute_prod_push(
                    repository, registration_id, email, dict(config_provider())
                )
                repository.save_prod_push_result(registration_id, result)
                terminal = result.get("status") in ("pushed", "partial", "skipped")
                repository.finish_prod_push(
                    claimed.get("event_id"),
                    error=result.get("error", ""),
                    delivered=terminal,
                )
                logger.info(
                    "[ProdPush] 推送完成 registration=%s status=%s",
                    registration_id,
                    result.get("status"),
                )
            except Exception as exc:
                logger.exception("[ProdPush] 推送异常: %s", exc)
                if claimed is not None:
                    try:
                        with self._lock:
                            repository = self._repository
                        if repository is not None:
                            repository.finish_prod_push(
                                claimed.get("event_id"),
                                error=str(exc)[:4000],
                                delivered=False,
                            )
                    except Exception:
                        pass


prod_push_worker = ProdPushWorker()
