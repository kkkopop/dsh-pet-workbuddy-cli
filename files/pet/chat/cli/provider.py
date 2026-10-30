# -*- coding: utf-8 -*-
"""CliProvider：把本地 codebuddy CLI 桥适配成 ``OpenAICompatibleProvider`` 同款流式契约。

与 HTTP provider 的差异：
- 不需要 API Key，走账号登录态；
- 人设/记忆以 ``messages`` 里的 system 文本为准，经 ``--system-prompt`` 注入；
- 历史以文本转录形式每轮重发（stateless，桌宠仍是唯一真源）；
- 内置联网检索在 cli 下禁用（给出 status 提示），其余 HTTP 旁路由 ``ChatSettings.http_fallback`` 兜底。
"""
from __future__ import annotations

from typing import Any, Iterator

from ..providers import ProviderError, StreamEvent
from .bridge import CliBridge, DEFAULT_MODEL, discover_cli


def _system_text(messages) -> str:
    parts = []
    for m in messages or []:
        if isinstance(m, dict) and m.get("role") == "system":
            c = m.get("content")
            if isinstance(c, str) and c.strip():
                parts.append(c.strip())
    return "\n".join(parts)


def _as_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return " ".join(b.get("text") for b in content
                        if isinstance(b, dict) and isinstance(b.get("text"), str)).strip()
    return ""


def _transcript(messages) -> str:
    lines = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "system":
            continue
        text = _as_text(m.get("content"))
        if not text:
            continue
        if role == "user":
            label = "用户"
        elif role == "assistant":
            label = "助手"
        else:
            label = str(role)
        lines.append(f"{label}：{text}")
    return "\n".join(lines)


class CliProvider:
    """本地 CLI 聊天 provider；实例持有长驻子进程桥，随 ChatService 生命周期复用。"""

    def __init__(self, bridge: CliBridge | None = None):
        # bridge 可注入假实现（测试）；为 None 时按 config 惰性建真实子进程桥。
        self._bridge = bridge

    def _get_bridge(self, config) -> CliBridge:
        if self._bridge is None:
            path = (getattr(config, "cli_path", "") or "").strip() or discover_cli()
            if not path:
                raise ProviderError(
                    "未检测到 CodeBuddy CLI。请在 AI 设置里填写 CLI 路径，或安装 WorkBuddy 客户端。")
            model = (getattr(config, "cli_model", "") or "").strip() or DEFAULT_MODEL
            self._bridge = CliBridge(path, model=model)
        return self._bridge

    def stream(self, messages: list[dict[str, Any]], config, cancel_event,
               response_holder: list | None = None) -> Iterator[str | StreamEvent]:
        web = getattr(config, "web_search", None)
        if web is not None and getattr(web, "enabled", False):
            yield StreamEvent("status", "当前连接方式不支持内置联网检索，直接回答")
        system_text = _system_text(messages)
        transcript = _transcript(messages)
        bridge = self._get_bridge(config)
        got_content = False
        for kind, text in bridge.send(transcript, system_text, cancel_event):
            if cancel_event.is_set():
                return
            if kind == "text":
                got_content = True
                yield text
            elif kind == "reasoning":
                yield StreamEvent("reasoning", text)
            elif kind == "status":
                yield StreamEvent("status", text)
        if not got_content and not cancel_event.is_set():
            raise ProviderError("本地模型未返回任何内容，请稍后重试")

    def close(self) -> None:
        if self._bridge is not None:
            self._bridge.close()
            self._bridge = None
