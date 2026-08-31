# -*- coding: utf-8 -*-
"""面向对象的 Grok2API 管理端客户端。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping

from curl_cffi import CurlMime, requests


class Grok2APIImportError(RuntimeError):
    """远程 Grok2API 登录或导入失败。"""


class Grok2APIClient:
    """封装管理员登录、令牌复用、multipart 上传与 SSE 结果解析。"""

    LOGIN_PATH = "/api/admin/v1/auth/login"
    IMPORT_PATH = "/api/admin/v1/accounts/import"
    IMPORT_PATHS = {
        "grok_build": "/api/admin/v1/accounts/import",
        "grok_web": "/api/admin/v1/accounts/web/import",
        "grok_console": "/api/admin/v1/accounts/console/import",
    }
    AUTO_IMPORT_KEYS = {
        "grok_build": "grok2api_auto_import_build",
        "grok_web": "grok2api_auto_import_web",
        "grok_console": "grok2api_auto_import_console",
    }
    AUTO_IMPORT_DEFAULTS = {
        "grok_build": True,
        "grok_web": False,
        "grok_console": False,
    }
    CONFIG_KEYS = (
        "grok2api_remote_url",
        "grok2api_remote_username",
        "grok2api_remote_password",
    )
    PROVIDER_NODE_SCOPES = {
        "grok_build": "grok_build",
        "grok_web": "grok_web",
        # console 账号可使用 console 或 web 出口节点（见 grok2api scopeSupportsProvider）
        "grok_console": "grok_console",
    }
    ACCOUNT_PATH = "/api/admin/v1/accounts"
    EGRESS_NODES_PATH = "/api/admin/v1/egress-nodes"
    EGRESS_ASSIGN_PATH = "/api/admin/v1/egress-nodes/{node_id}/accounts"
    EGRESS_UNASSIGN_PATH = "/api/admin/v1/egress-nodes/accounts"
    ACCOUNTS_BATCH_PATH = "/api/admin/v1/accounts/batch"

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        session: Any = None,
        login_timeout: float = 20,
        import_timeout: float = 120,
    ) -> None:
        self.base_url = self._normalize_base_url(base_url)
        self.username = str(username or "").strip()
        self.password = str(password or "")
        if not self.username or not self.password:
            raise Grok2APIImportError("Grok2API 管理员账号或密码为空")
        self.login_timeout = float(login_timeout)
        self.import_timeout = float(import_timeout)
        self._owns_session = session is None
        # Grok2API 是独立管理服务，不继承项目代理或环境代理。
        self.session = session or requests.Session(trust_env=False)
        self._access_token = ""

    @classmethod
    def from_config(
        cls,
        config: Mapping[str, Any],
        *,
        session: Any = None,
        login_timeout: float = 20,
        import_timeout: float = 120,
    ) -> "Grok2APIClient":
        """从项目配置创建客户端，并统一校验必填字段。"""
        if not cls.is_configured(config):
            raise Grok2APIImportError(
                "请先完整配置 Grok2API API 地址、管理员账号和密码"
            )
        return cls(
            str(config.get("grok2api_remote_url") or ""),
            str(config.get("grok2api_remote_username") or ""),
            str(config.get("grok2api_remote_password") or ""),
            session=session,
            login_timeout=login_timeout,
            import_timeout=import_timeout,
        )

    @classmethod
    def is_configured(cls, config: Mapping[str, Any]) -> bool:
        return all(str(config.get(key, "") or "").strip() for key in cls.CONFIG_KEYS)

    @classmethod
    def auto_import_formats(cls, config: Mapping[str, Any] | None = None) -> tuple[str, ...]:
        """注册/重登成功后需要自动导入的 Grok2API 格式。"""
        data = config or {}
        if not bool(data.get("grok2api_auto_import", False)):
            return ()
        selected: list[str] = []
        for format_name in cls.IMPORT_PATHS:
            key = cls.AUTO_IMPORT_KEYS[format_name]
            default = cls.AUTO_IMPORT_DEFAULTS[format_name]
            if bool(data.get(key, default)):
                selected.append(format_name)
        return tuple(selected)

    @property
    def access_token(self) -> str:
        """只读暴露当前会话令牌，便于诊断是否已登录。"""
        return self._access_token

    @staticmethod
    def _normalize_base_url(value: str) -> str:
        base = str(value or "").strip().rstrip("/")
        if not base:
            raise Grok2APIImportError("Grok2API API 地址为空")
        if not base.startswith(("http://", "https://")):
            raise Grok2APIImportError(
                "Grok2API API 地址必须以 http:// 或 https:// 开头"
            )
        return base

    @staticmethod
    def _response_error(response: Any, fallback: str) -> str:
        try:
            payload = response.json()
        except Exception:
            payload = None
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict) and error.get("message"):
                return str(error["message"])
            if payload.get("message"):
                return str(payload["message"])
        status = int(getattr(response, "status_code", 0) or 0)
        return f"{fallback} (HTTP {status})" if status else fallback

    @staticmethod
    def _iter_sse_events(
        lines: Iterable[Any],
    ) -> Iterable[tuple[str, Dict[str, Any]]]:
        event = "message"
        data_lines: list[str] = []
        for raw in lines:
            line = (
                raw.decode("utf-8", errors="replace")
                if isinstance(raw, bytes)
                else str(raw or "")
            ).rstrip("\r\n")
            if not line:
                if data_lines:
                    yield event, Grok2APIClient._decode_sse_data(data_lines)
                event, data_lines = "message", []
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event = line[6:].strip() or "message"
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if data_lines:
            yield event, Grok2APIClient._decode_sse_data(data_lines)

    @staticmethod
    def _decode_sse_data(data_lines: Iterable[str]) -> Dict[str, Any]:
        raw_data = "\n".join(data_lines)
        try:
            payload = json.loads(raw_data)
        except json.JSONDecodeError:
            payload = {"message": raw_data}
        return payload if isinstance(payload, dict) else {"data": payload}

    @staticmethod
    def _load_auth_document(file_path: str | Path) -> tuple[Path, bytes]:
        path = Path(file_path).expanduser().resolve()
        if not path.is_file():
            raise Grok2APIImportError("Grok2API 授权 JSON 文件不存在")
        try:
            content = path.read_bytes()
            json.loads(content.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise Grok2APIImportError(f"Grok2API 授权 JSON 无效: {exc}") from exc
        return path, content

    def login(self, *, force: bool = False) -> str:
        """使用管理员账号密码登录；同一实例默认复用已取得的令牌。"""
        if self._access_token and not force:
            return self._access_token
        try:
            response = self.session.post(
                f"{self.base_url}{self.LOGIN_PATH}",
                json={"username": self.username, "password": self.password},
                headers={"Accept": "application/json"},
                timeout=self.login_timeout,
            )
        except Exception as exc:
            raise Grok2APIImportError(f"连接 Grok2API 登录接口失败: {exc}") from exc
        if int(getattr(response, "status_code", 0) or 0) != 200:
            raise Grok2APIImportError(self._response_error(response, "Grok2API 登录失败"))
        try:
            payload = response.json()
            token = str(payload["data"]["tokens"]["accessToken"] or "").strip()
        except (KeyError, TypeError, ValueError) as exc:
            raise Grok2APIImportError("Grok2API 登录响应缺少 accessToken") from exc
        if not token:
            raise Grok2APIImportError("Grok2API 登录响应缺少 accessToken")
        self._access_token = token
        return token

    def import_auth_file(
        self,
        file_path: str | Path,
        format_name: str = "grok_build",
    ) -> Dict[str, Any]:
        """自动登录并将一个 grok_build JSON 导入远程管理端。"""
        normalized_format = str(format_name or "").strip().lower() or "grok_build"
        import_path = self.IMPORT_PATHS.get(normalized_format)
        if not import_path:
            raise Grok2APIImportError(
                "Grok2API import format must be grok_build, grok_web, or grok_console"
            )
        path, content = self._load_auth_document(file_path)
        token = self.login()
        multipart = CurlMime()
        multipart.addpart(
            name="files",
            filename=path.name,
            content_type="application/json",
            data=content,
        )
        response = None
        try:
            request_kwargs = {
                "headers": {
                    "Accept": "text/event-stream",
                    "Authorization": f"Bearer {token}",
                    "Cache-Control": "no-cache",
                },
                "multipart": multipart,
                "timeout": self.import_timeout,
                "stream": True,
            }
            try:
                response = self.session.post(
                    f"{self.base_url}{import_path}", **request_kwargs
                )
            except TypeError as exc:
                if "multipart" not in str(exc):
                    raise
                request_kwargs.pop("multipart", None)
                request_kwargs["files"] = {
                    "files": (path.name, content, "application/json")
                }
                response = self.session.post(
                    f"{self.base_url}{import_path}", **request_kwargs
                )
        except Exception as exc:
            raise Grok2APIImportError(f"连接 Grok2API 导入接口失败: {exc}") from exc
        finally:
            if response is None:
                multipart.close()

        try:
            if int(getattr(response, "status_code", 0) or 0) != 200:
                raise Grok2APIImportError(
                    self._response_error(response, "Grok2API 导入失败")
                )
            completed: Dict[str, Any] | None = None
            for event, payload in self._iter_sse_events(response.iter_lines()):
                if event == "error":
                    raise Grok2APIImportError(
                        str(
                            payload.get("message")
                            or payload.get("code")
                            or "Grok2API 导入失败"
                        )
                    )
                if event == "complete":
                    completed = payload
            if completed is None:
                raise Grok2APIImportError(
                    "Grok2API 导入响应未返回 complete 事件"
                )
            return completed
        finally:
            try:
                response.close()
            except Exception:
                pass
            multipart.close()

    def _json_request(
        self,
        method: str,
        path: str,
        *,
        payload: Dict[str, Any] | None = None,
        params: Dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Dict[str, Any]:
        """管理员 JSON 请求：登录 + Bearer，返回 data 字段；失败抛错。"""
        token = self.login()
        try:
            response = self.session.request(
                method,
                f"{self.base_url}{path}",
                json=payload,
                params=params,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {token}",
                },
                timeout=timeout or self.login_timeout,
            )
        except Exception as exc:
            raise Grok2APIImportError(f"连接 Grok2API 接口失败: {exc}") from exc
        if int(getattr(response, "status_code", 0) or 0) != 200:
            raise Grok2APIImportError(
                self._response_error(response, f"Grok2API {method} {path} 失败")
            )
        try:
            body = response.json()
        except Exception as exc:
            raise Grok2APIImportError("Grok2API 响应不是有效 JSON") from exc
        data = body.get("data") if isinstance(body, dict) else None
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------
    # 账号与出口节点管理（正式号池推送 / 出口保护用）
    # ------------------------------------------------------------------
    def search_account(self, provider: str, email: str) -> Dict[str, Any] | None:
        """按 email 模糊反查某渠道账号，返回 {id, egress_node_id} 或 None。

        grok2api 的 search 对 name/email/user_id/team_id 做 LIKE 匹配；
        注册邮箱足够独特，取第一条即可。
        """
        data = self._json_request(
            "GET",
            self.ACCOUNT_PATH,
            params={"provider": provider, "search": str(email or "").strip(), "page": 1, "pageSize": 5},
        )
        items = data.get("items") or []
        for item in items:
            email_value = str(item.get("email") or "").strip().lower()
            if email_value == str(email or "").strip().lower():
                node_id = str(item.get("egressNodeId") or "").strip()
                return {"id": str(item.get("id") or ""), "egress_node_id": node_id}
        return None

    def list_egress_nodes(self, scope: str) -> list[Dict[str, Any]]:
        """列出某 scope 的全部出口节点（id/name/enabled/proxyConfigured）。"""
        nodes: list[Dict[str, Any]] = []
        page = 1
        while True:
            data = self._json_request(
                "GET",
                self.EGRESS_NODES_PATH,
                params={"scope": scope, "page": page, "pageSize": 100},
            )
            items = data.get("items") or []
            for item in items:
                nodes.append(
                    {
                        "id": str(item.get("id") or ""),
                        "name": str(item.get("name") or ""),
                        "enabled": bool(item.get("enabled")),
                        "proxy_configured": bool(item.get("proxyConfigured")),
                    }
                )
            total = int(data.get("total") or 0)
            if not items or len(nodes) >= total:
                return nodes
            page += 1

    def assign_account(
        self,
        provider: str,
        node_id: str,
        account_id: str,
        mode: str = "manual",
    ) -> None:
        """把账号（字符串 id）manual 绑定到指定出口节点。"""
        self._json_request(
            "POST",
            self.EGRESS_ASSIGN_PATH.format(node_id=str(node_id).strip()),
            payload={"provider": provider, "ids": [str(account_id)], "mode": mode},
        )

    def disable_accounts(self, provider: str, account_ids: Iterable[str]) -> int:
        """批量禁用账号，返回更新条数。"""
        ids = [str(value) for value in account_ids if str(value).strip()]
        if not ids:
            return 0
        data = self._json_request(
            "PATCH",
            self.ACCOUNTS_BATCH_PATH,
            payload={"ids": ids, "provider": provider, "enabled": False},
        )
        try:
            return int(data.get("updated") or 0)
        except (TypeError, ValueError):
            return 0

    def close(self) -> None:
        """释放客户端自行创建的 HTTP 会话。"""
        if not self._owns_session:
            return
        try:
            self.session.close()
        except Exception:
            pass

    def __enter__(self) -> "Grok2APIClient":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()
