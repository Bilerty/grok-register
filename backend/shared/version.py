"""应用版本解析。

发布镜像优先使用构建时注入的环境变量；本地源码与普通容器则读取项目根目录
的 ``VERSION`` 文件。版本检测服务只接受语义化版本，读取失败时返回 ``dev``。
"""

from __future__ import annotations

import os
from pathlib import Path

from backend.shared.paths import PROJECT_ROOT


def _clean_version(value: object) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 128:
        return ""
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        return ""
    return text


def current_version() -> str:
    """返回当前运行实例版本。"""
    value = _clean_version(os.environ.get("GROK_REGISTER_VERSION", ""))
    if value:
        return value
    candidates = (
        PROJECT_ROOT / "VERSION",
        Path.cwd() / "VERSION",
    )
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            path = candidate.resolve()
        except OSError:
            path = candidate
        if path in seen:
            continue
        seen.add(path)
        try:
            value = _clean_version(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            continue
        if value:
            return value
    return "dev"

