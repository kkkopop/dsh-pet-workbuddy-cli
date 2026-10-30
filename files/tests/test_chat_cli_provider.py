# -*- coding: utf-8 -*-
"""pet/chat/cli 子进程桥 + CLI provider 的测试（用假 CLI 桩，不联网不耗额度）。"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

from pet.chat.cli import CliBridge, CliBridgeError, build_argv, discover_cli, map_event
from pet.chat.cli.bridge import DEFAULT_MODEL, DISALLOWED_TOOLS
from pet.chat.models import ProviderConfig

FAKE_CLI = Path(__file__).with_name("fake_cli.py")


def _bridge(tmp_path, **kw):
    kw.setdefault("cwd", str(tmp_path))
    return CliBridge([sys.executable, str(FAKE_CLI)], **kw)


# ---------------------------------------------------------------- map_event
def test_map_event_text_delta():
    assert map_event({"type": "stream_event", "event": {"type": "content_block_delta",
                      "index": 0, "delta": {"type": "text_delta", "text": "你好"}}}) == ("text", "你好")


def test_map_event_thinking_delta():
    assert map_event({"type": "stream_event", "event": {"type": "content_block_delta",
                      "index": 0, "delta": {"type": "thinking_delta", "thinking": "想想"}}}) == ("reasoning", "想想")


def test_map_event_status():
    assert map_event({"type": "system", "subtype": "status", "status": "正在…"}) == ("status", "正在…")


def test_map_event_result_and_ignore():
    assert map_event({"type": "result", "subtype": "success"}) == ("result", None)
    assert map_event({"type": "system", "subtype": "init"}) is None


# ---------------------------------------------------------------- build_argv
def test_build_argv_flags_and_no_skip_permissions():
    argv = build_argv("codebuddy.cmd", "hy4-preview", "你是桌宠")
    assert argv[0] == "codebuddy.cmd"
    assert "-p" in argv
    assert argv[argv.index("--input-format") + 1] == "stream-json"
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--include-partial-messages" in argv
    assert argv[argv.index("--max-turns") + 1] == "1"
    assert argv[argv.index("--model") + 1] == "hy4-preview"
    assert argv[argv.index("--system-prompt") + 1] == "你是桌宠"
    assert "-y" not in argv and "--dangerously-skip-permissions" not in argv


def test_build_argv_disallows_write_tools():
    argv = build_argv("codebuddy.cmd", "hy4-preview", "")
    i = argv.index("--disallowedTools")
    tools = argv[i + 1]
    for name in ("Bash", "PowerShell", "Write", "Edit", "ComputerUse", "ImageGen"):
        assert name in tools


def test_build_argv_list_executable():
    argv = build_argv([sys.executable, str(FAKE_CLI)], "fake", "")
    assert argv[0] == sys.executable
    assert argv[1] == str(FAKE_CLI)
    assert "--max-turns" in argv


# ---------------------------------------------------------------- discover_cli
def test_discover_cli_explicit_preferred(tmp_path):
    p = tmp_path / "cb.cmd"
    p.write_text("", encoding="utf-8")
    assert discover_cli(explicit=str(p), home=tmp_path) == str(p)


def test_discover_cli_explicit_missing_returns_none(tmp_path):
    assert discover_cli(explicit=str(tmp_path / "nope.cmd"), home=tmp_path) is None


def test_discover_cli_prefers_highest_version(tmp_path):
    base = tmp_path / ".workbuddy" / "binaries" / "node" / "versions"
    for v in ("22.22.2-3", "22.30.1-1"):
        d = base / v
        d.mkdir(parents=True)
        (d / "codebuddy.cmd").write_text("", encoding="utf-8")
    found = discover_cli(home=tmp_path)
    assert found == str(base / "22.30.1-1" / "codebuddy.cmd")


def test_discover_cli_path_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr("pet.chat.cli.bridge.shutil.which",
                        lambda name: "C:/tools/codebuddy.cmd" if name == "codebuddy" else None)
    assert discover_cli(home=tmp_path) == "C:/tools/codebuddy.cmd"


# ---------------------------------------------------------------- CliBridge
def test_bridge_yields_reasoning_and_text(tmp_path):
    b = _bridge(tmp_path)
    cancel = threading.Event()
    items = list(b.send("你好", "你是桌宠", cancel))
    b.close()
    assert items == [("reasoning", "让我想想"), ("text", "收到")]


def test_bridge_error_result_raises(tmp_path):
    b = _bridge(tmp_path, env={"FAKE_CLI_ERROR": "1"})
    cancel = threading.Event()
    with pytest.raises(CliBridgeError):
        list(b.send("hi", "", cancel))
    b.close()


def test_bridge_cancel_terminates_and_can_restart(tmp_path):
    b = _bridge(tmp_path, env={"FAKE_CLI_HANG": "1"})
    cancel = threading.Event()
    it = b.send("hi", "", cancel)
    first = next(it)
    assert first == ("reasoning", "思考中…")
    cancel.set()
    with pytest.raises(StopIteration):
        next(it)
    # 取消后进程已终止，下一轮能重新拉起
    cancel2 = threading.Event()
    b2 = _bridge(tmp_path)
    assert list(b2.send("hi2", "", cancel2)) == [("reasoning", "让我想想"), ("text", "收到")]
    b.close()
    b2.close()


# ---------------------------------------------------------------- CliProvider
class _FakeBridge:
    def __init__(self, script=()):
        self.script = list(script)
        self.calls = []

    def send(self, user_text, system_text, cancel):
        self.calls.append((user_text, system_text))
        for item in self.script:
            yield item

    def close(self):
        pass


def _cli_config():
    return ProviderConfig("cli", kind="cli", cli_path="C:/cb.cmd", cli_model="deepseek-v4.1-flash")


def test_cli_provider_maps_reasoning_and_text():
    from pet.chat.cli import CliProvider
    from pet.chat.providers import StreamEvent

    p = CliProvider(bridge=_FakeBridge([("reasoning", "想"), ("text", "你好")]))
    out = list(p.stream([{"role": "system", "content": "你是桌宠"},
                         {"role": "user", "content": "在吗"}],
                        _cli_config(), threading.Event()))
    assert out == [StreamEvent("reasoning", "想"), "你好"]


def test_cli_provider_web_search_disabled_status():
    from pet.chat.cli import CliProvider
    from pet.chat.providers import StreamEvent

    cfg = _cli_config()
    cfg.web_search = type("W", (), {"enabled": True})()
    p = CliProvider(bridge=_FakeBridge([("text", "好")]))
    out = list(p.stream([{"role": "user", "content": "hi"}], cfg, threading.Event()))
    assert isinstance(out[0], StreamEvent) and out[0].kind == "status"
    assert "联网" in out[0].text


def test_cli_provider_empty_raises():
    import pytest

    from pet.chat.cli import CliProvider
    from pet.chat.providers import ProviderError

    p = CliProvider(bridge=_FakeBridge([]))
    with pytest.raises(ProviderError):
        list(p.stream([{"role": "user", "content": "hi"}], _cli_config(), threading.Event()))


def test_cli_provider_cancel_returns_without_raising():
    from pet.chat.cli import CliProvider

    p = CliProvider(bridge=_FakeBridge([]))
    cancel = threading.Event()
    cancel.set()
    assert list(p.stream([{"role": "user", "content": "hi"}], _cli_config(), cancel)) == []


def test_cli_provider_transcript_and_system_passed_to_bridge():
    from pet.chat.cli import CliProvider

    bridge = _FakeBridge([("text", "好")])
    p = CliProvider(bridge=bridge)
    list(p.stream([{"role": "system", "content": "你是桌宠"},
                   {"role": "user", "content": "第一句"},
                   {"role": "assistant", "content": "回答"},
                   {"role": "user", "content": "再问"}],
                  _cli_config(), threading.Event()))
    user_text, system_text = bridge.calls[0]
    assert system_text == "你是桌宠"
    assert "用户：第一句" in user_text and "助手：回答" in user_text and "用户：再问" in user_text


# ---------------------------------------------------------------- defaults
def test_defaults_sane():
    assert DEFAULT_MODEL
    assert "Bash" in DISALLOWED_TOOLS and "Write" in DISALLOWED_TOOLS
