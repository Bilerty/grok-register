# -*- coding: utf-8 -*-
"""注册任务协调器。

以单任务模型管理后台线程、停止信号、进度统计和有界日志队列。
最近一次任务的 batch_id 与进度摘要会写入 SQLite，服务重启后可恢复。
"""
from __future__ import annotations

import collections
import re
import threading
import time
from typing import Any, Deque, Dict, List, Optional


class RegistrationJobCoordinator:
    """Single-flight registration runner with ring-buffer logs."""

    def __init__(self, max_logs: int = 2000):
        self._lock = threading.RLock()
        self._logs: Deque[Dict[str, Any]] = collections.deque(maxlen=max(100, int(max_logs)))
        self._log_seq = 0
        self._running = False
        self._stop_controller = None
        self._stop_requested_before_controller = False
        self._thread: Optional[threading.Thread] = None
        self._started_at: Optional[float] = None
        self._finished_at: Optional[float] = None
        self._last_error = ""
        self._target_count = 0
        self._workers = 1
        self._source = "web"
        self._completed_count = 0
        self._success_count = 0
        self._failure_count = 0
        self._current_stage = "等待启动"
        self._current_email = ""
        self._batch_id = ""
        self._restored = False
        self._last_persist_at = 0.0
        self._last_persisted_completed = -1

    def _repository(self):
        try:
            from backend.registration import engine as gr

            return gr.get_registration_repository()
        except Exception:
            return None

    def _snapshot_payload(self) -> Dict[str, Any]:
        return {
            "batch_id": self._batch_id,
            "running": self._running,
            "started_at": self._started_at,
            "finished_at": self._finished_at,
            "target_count": self._target_count,
            "workers": self._workers,
            "source": self._source,
            "last_error": self._last_error,
            "completed_count": self._completed_count,
            "success_count": self._success_count,
            "failure_count": self._failure_count,
            "current_stage": self._current_stage,
            "current_email": self._current_email,
        }

    def _persist_snapshot(self, *, force: bool = False) -> None:
        """把当前任务摘要写入 SQLite；默认对进度变更做轻量节流。"""
        now = time.time()
        with self._lock:
            completed = self._completed_count
            if (
                not force
                and completed == self._last_persisted_completed
                and (now - self._last_persist_at) < 1.0
            ):
                return
            payload = self._snapshot_payload()
            self._last_persist_at = now
            self._last_persisted_completed = completed
        repo = self._repository()
        if repo is None:
            return
        try:
            repo.save_job_snapshot(payload)
        except Exception:
            # 持久化失败不影响注册主流程
            pass

    def restore_from_database(self) -> None:
        """进程启动后从 SQLite 恢复最近批次；不会恢复为 running。"""
        with self._lock:
            if self._restored:
                return
            self._restored = True
        repo = self._repository()
        if repo is None:
            return
        try:
            snap = repo.get_job_snapshot() or {}
        except Exception:
            snap = {}

        batch_id = str(snap.get("batch_id") or "").strip()
        if not batch_id:
            try:
                batch_id = str(repo.latest_web_batch_id() or "").strip()
            except Exception:
                batch_id = ""

        with self._lock:
            if self._running:
                return
            if batch_id:
                self._batch_id = batch_id
            if snap:
                self._started_at = snap.get("started_at")
                self._finished_at = snap.get("finished_at")
                self._target_count = int(snap.get("target_count") or 0)
                self._workers = max(1, int(snap.get("workers") or 1))
                self._source = str(snap.get("source") or "web")
                self._last_error = str(snap.get("last_error") or "")
                self._completed_count = int(snap.get("completed_count") or 0)
                self._success_count = int(snap.get("success_count") or 0)
                self._failure_count = int(snap.get("failure_count") or 0)
                self._current_email = str(snap.get("current_email") or "")
                was_running = bool(snap.get("running"))
                stage = str(snap.get("current_stage") or "").strip()
                if was_running:
                    self._running = False
                    if not self._finished_at:
                        self._finished_at = time.time()
                    self._current_stage = stage or "任务已中断（服务重启）"
                    if not self._last_error:
                        self._last_error = "服务重启，上次任务未正常收尾"
                else:
                    self._current_stage = stage or ("等待启动" if not batch_id else "最近任务已结束")
            elif batch_id:
                self._current_stage = "最近任务已结束"

        # 若快照标记仍在 running，写回为已中断，避免下次启动重复提示逻辑混乱
        if snap and snap.get("running"):
            self._persist_snapshot(force=True)

    def _update_progress_from_log(self, message: str) -> bool:
        """根据日志更新进度；返回 completed_count 是否变化。"""
        text = str(message or "").strip()
        if not text:
            return False

        stage_rules = (
            ("打开注册页", "打开注册页"),
            ("创建邮箱并提交", "创建并提交邮箱"),
            ("拉取验证码", "等待邮箱验证码"),
            ("填写资料", "填写账号资料"),
            ("等待 sso", "等待登录凭证"),
            ("开启 NSFW", "更新账号设置"),
            ("SSO→auth", "转换并写入授权"),
            ("[CPA]", "写入 CPA 授权"),
            ("下一个账号前等待", "等待下一账号"),
        )
        changed = False
        with self._lock:
            before = self._completed_count
            for marker, label in stage_rules:
                if marker in text:
                    self._current_stage = label
                    break

            email_match = re.search(r"(?:邮箱|注册成功):\s*([^\s]+@[^\s]+)", text)
            if email_match:
                self._current_email = email_match.group(1).strip()

            boot_failure = re.search(r"(\d+)\s*个任务均记为失败", text)
            success = bool(re.search(r"\[\+\]\s*注册成功", text))
            failure = any(
                marker in text
                for marker in (
                    "注册未计成功 [CPA失败]",
                    "[-] 域名拒绝:",
                    "[-] 邮箱域名被",
                    "[-] 卡住跳过:",
                    "达到最大重试次数，跳过",
                    "[-] 注册失败 [",
                    "] [-] 失败 [",
                )
            )

            remaining = max(self._target_count - self._completed_count, 0)
            if boot_failure:
                amount = min(int(boot_failure.group(1)), remaining)
                self._failure_count += amount
                self._completed_count += amount
            elif success and remaining:
                self._success_count += 1
                self._completed_count += 1
            elif failure and remaining:
                self._failure_count += 1
                self._completed_count += 1

            if self._completed_count:
                self._current_stage = (
                    "任务收尾中"
                    if self._completed_count >= self._target_count
                    else f"准备第 {self._completed_count + 1} 个账号"
                )
            changed = self._completed_count != before
        return changed

    def _append_log(self, message: str) -> None:
        text = str(message or "")
        progress_changed = self._update_progress_from_log(text)
        with self._lock:
            self._log_seq += 1
            self._logs.append(
                {
                    "id": self._log_seq,
                    "time": time.strftime("%H:%M:%S"),
                    "message": text,
                }
            )
        if progress_changed:
            self._persist_snapshot(force=True)

    def status(self) -> Dict[str, Any]:
        self.restore_from_database()
        with self._lock:
            return {
                "running": self._running,
                "started_at": self._started_at,
                "finished_at": self._finished_at,
                "target_count": self._target_count,
                "workers": self._workers,
                "source": self._source,
                "last_error": self._last_error,
                "log_count": len(self._logs),
                "latest_log_id": self._log_seq,
                "completed_count": self._completed_count,
                "success_count": self._success_count,
                "failure_count": self._failure_count,
                "progress_percent": round(
                    (self._completed_count / self._target_count * 100)
                    if self._target_count
                    else 0,
                    1,
                ),
                "current_stage": self._current_stage,
                "current_email": self._current_email,
                "batch_id": self._batch_id,
            }

    def get_logs(self, after_id: int = 0, limit: int = 500) -> List[Dict[str, Any]]:
        safe_limit = max(1, min(int(limit or 500), 2000))
        threshold = max(0, int(after_id or 0))
        with self._lock:
            items = [item for item in self._logs if int(item["id"]) > threshold]
        if len(items) > safe_limit:
            items = items[-safe_limit:]
        return items

    def clear_logs(self) -> None:
        with self._lock:
            self._logs.clear()

    def start(self, count: int = 1, workers: int = 1) -> Dict[str, Any]:
        from backend.registration import engine as gr

        self.restore_from_database()
        gr._bs.allow_browser_launches()
        count = max(1, min(int(count or 1), 1000))
        workers = max(1, min(int(workers or 1), 8, count))

        with self._lock:
            if self._running:
                raise RuntimeError("已有注册任务在运行")
            self._running = True
            self._started_at = time.time()
            self._finished_at = None
            self._last_error = ""
            self._target_count = count
            self._workers = workers
            self._stop_controller = None
            self._stop_requested_before_controller = False
            self._completed_count = 0
            self._success_count = 0
            self._failure_count = 0
            self._current_stage = "任务启动中"
            self._current_email = ""
            self._batch_id = ""
            self._append_log(f"[*] Web 任务启动：数量={count} 并发={workers}")

        self._persist_snapshot(force=True)

        manager = self

        def runner() -> None:
            original_registration_log = gr.registration_log
            original_controller_cls = gr.RegistrationStopController

            class WebStopController:
                """Compatible with RegistrationStopController; instance is kept by manager."""

                def __init__(self) -> None:
                    self.stop_requested = False
                    with manager._lock:
                        if manager._stop_requested_before_controller:
                            self.stop_requested = True
                        manager._stop_controller = self

                def should_stop(self) -> bool:
                    return self.stop_requested

                def stop(self) -> None:
                    self.stop_requested = True

            def web_registration_log(message: str) -> None:
                try:
                    original_registration_log(message)
                except Exception:
                    pass
                manager._append_log(str(message or ""))

            try:
                gr.load_config()
                gr._wire_runtime_modules()
                gr.config["register_count"] = count
                gr.config["register_workers"] = workers
                if gr.config.get("debug_mode"):
                    gr.config["register_count"] = 1
                    gr.config["register_workers"] = 1
                    manager._append_log("[*] 调试模式：强制单账号，结束后不关闭浏览器")
                    count_local = 1
                else:
                    count_local = count

                gr.registration_log = web_registration_log
                gr.RegistrationStopController = WebStopController

                original_new_batch_id = gr.new_registration_batch_id

                def capture_batch_id(source="web"):
                    batch_id = original_new_batch_id(source)
                    with manager._lock:
                        manager._batch_id = str(batch_id or "")
                    manager._append_log(f"[*] 任务批次: {batch_id}")
                    manager._persist_snapshot(force=True)
                    return batch_id

                gr.new_registration_batch_id = capture_batch_id
                try:
                    gr.run_registration(count_local)
                finally:
                    gr.new_registration_batch_id = original_new_batch_id
            except Exception as exc:
                with manager._lock:
                    manager._last_error = str(exc)
                manager._append_log(f"[!] Web 任务异常: {exc}")
                trace_text = gr.current_exception_traceback(gr.TRACEBACK_LOG_MAX_CHARS)
                manager._append_log(f"[异常堆栈]\n{trace_text}")
            finally:
                gr.registration_log = original_registration_log
                gr.RegistrationStopController = original_controller_cls
                with manager._lock:
                    manager._running = False
                    manager._finished_at = time.time()
                    manager._stop_controller = None
                    manager._stop_requested_before_controller = False
                    manager._current_stage = (
                        "任务已停止"
                        if manager._completed_count < manager._target_count
                        else "任务已完成"
                    )
                manager._append_log("[*] Web 任务已结束")
                manager._persist_snapshot(force=True)

        self._thread = threading.Thread(target=runner, name="web-registration", daemon=True)
        self._thread.start()
        return self.status()

    def request_stop(self) -> Dict[str, Any]:
        with self._lock:
            controller = self._stop_controller
            running = self._running
            if running and controller is None:
                self._stop_requested_before_controller = True
        if not running:
            return self.status()
        if controller is None:
            self._append_log("[!] 已预登记停止，等待注册流程响应")
            return self.status()
        try:
            controller.stop()
            self._append_log("[!] 已请求停止注册任务")
        except Exception as exc:
            self._append_log(f"[!] 停止失败: {exc}")
            raise
        try:
            from backend.registration import engine as gr
            gr._bs.interrupt_browser_work(log_callback=self._append_log)
        except Exception as exc:
            self._append_log(f"[!] 中断浏览器启动失败: {exc}")
        self._persist_snapshot(force=True)
        return self.status()

    def stop(self) -> Dict[str, Any]:
        status = self.request_stop()
        if not status.get("running"):
            return status
        with self._lock:
            controller = self._stop_controller
        if controller is None:
            deadline = time.time() + 8.0
            while time.time() < deadline:
                with self._lock:
                    controller = self._stop_controller
                    running = self._running
                if controller is not None or not running:
                    break
                time.sleep(0.05)
        if controller is None:
            self._append_log("[!] 停止控制器仍未就绪")
            return self.status()
        return self.status()


job_coordinator = RegistrationJobCoordinator()
