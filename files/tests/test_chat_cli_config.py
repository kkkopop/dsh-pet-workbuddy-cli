# -*- coding: utf-8 -*-
"""ProviderConfig.kind / cli 字段、http_fallback、build_provider 分派 的测试。"""
from __future__ import annotations

import time

import pytest

from pet.chat.models import ChatSettings, ProviderConfig
from pet.chat.providers import OpenAICompatibleProvider, build_provider


# ---------------------------------------------------------------- ProviderConfig
def test_provider_config_kind_defaults_http():
    p = ProviderConfig.from_dict("p", {})
    assert p.kind == "http"
    assert p.cli_path == ""
    assert p.cli_model == ""


def test_provider_config_cli_roundtrip():
    p = ProviderConfig.from_dict("p", {
        "kind": "cli", "cli_path": "C:/cb.cmd", "cli_model": "deepseek-v4.1-flash",
    })
    assert p.kind == "cli"
    assert p.cli_path == "C:/cb.cmd"
    assert p.cli_model == "deepseek-v4.1-flash"
    d = p.to_dict()
    assert d["kind"] == "cli"
    assert d["cli_path"] == "C:/cb.cmd"
    assert d["cli_model"] == "deepseek-v4.1-flash"
    again = ProviderConfig.from_dict("p", d)
    assert again.kind == "cli" and again.cli_model == "deepseek-v4.1-flash"


# ---------------------------------------------------------------- http_fallback
def _providers():
    return {
        "http": ProviderConfig("http", name="HTTP", base_url="https://api.example.com", kind="http"),
        "empty": ProviderConfig("empty", name="Empty", base_url="", kind="http"),
        "cli": ProviderConfig("cli", name="CLI", kind="cli", cli_path="C:/cb.cmd"),
    }


def test_http_fallback_prefers_http_with_url():
    s = ChatSettings(providers=_providers())
    assert s.http_fallback is not None and s.http_fallback.provider_id == "http"


def test_http_fallback_skips_empty_url():
    s = ChatSettings(providers={"empty": ProviderConfig("empty", base_url="", kind="http"),
                                "cli": ProviderConfig("cli", kind="cli", cli_path="C:/cb.cmd")})
    assert s.http_fallback is None


def test_http_fallback_none_when_only_cli():
    s = ChatSettings(providers={"cli": ProviderConfig("cli", kind="cli", cli_path="C:/cb.cmd")})
    assert s.http_fallback is None


def test_http_fallback_aux_provider_override():
    s = ChatSettings(aux_provider="other",
                     providers={**_providers(),
                                "other": ProviderConfig("other", base_url="https://x2", kind="http")})
    assert s.http_fallback.provider_id == "other"


# ---------------------------------------------------------------- build_provider
def test_build_provider_dispatches_on_kind():
    http_cfg = ProviderConfig("p", base_url="https://x", kind="http")
    cli_cfg = ProviderConfig("p", kind="cli", cli_path="C:/cb.cmd")
    legacy_cfg = ProviderConfig("p", base_url="https://x")  # 无 kind
    assert isinstance(build_provider(http_cfg), OpenAICompatibleProvider)
    assert isinstance(build_provider(legacy_cfg), OpenAICompatibleProvider)
    from pet.chat.cli.provider import CliProvider
    assert isinstance(build_provider(cli_cfg), CliProvider)


# ---------------------------------------------------------------- HTTP 旁路回退
class _Cfg:
    def __init__(self, settings):
        self._s = settings

    def chat_settings(self):
        return self._s

    def resolve_api_key(self, p):
        return p.api_key or ""


def test_resolve_chat_context_falls_back_for_cli():
    from pet.screen_capture.vision_bridge import resolve_chat_context

    s = ChatSettings(providers=_providers())
    s.active_provider = "cli"
    provider, _sys = resolve_chat_context(_Cfg(s))
    assert provider.provider_id == "http"


def test_resolve_chat_context_raises_without_http():
    from pet.screen_capture.vision_bridge import resolve_chat_context

    s = ChatSettings(providers={"cli": ProviderConfig("cli", kind="cli", cli_path="C:/cb.cmd")})
    s.active_provider = "cli"
    with pytest.raises(RuntimeError):
        resolve_chat_context(_Cfg(s))


def test_resolve_chat_context_keeps_http_active():
    from pet.screen_capture.vision_bridge import resolve_chat_context

    s = ChatSettings(providers=_providers())
    s.active_provider = "http"
    provider, _sys = resolve_chat_context(_Cfg(s))
    assert provider.provider_id == "http"


# ---------------------------------------------------------------- ChatService 分派
def test_chat_service_send_resolves_provider_via_build_provider(monkeypatch):
    from PySide6.QtWidgets import QApplication
    from pet.chat.service import ChatService

    app = QApplication.instance() or QApplication([])

    calls = []

    class Stub:
        def stream(self, messages, config, cancel):
            calls.append(config.provider_id)
            yield "hi"

    import pet.chat.service as svc
    monkeypatch.setattr(svc, "build_provider", lambda config: Stub())

    service = ChatService(provider=None)
    finished = []
    service.finished.connect(lambda _rid, text: finished.append(text))
    service.send([{"role": "user", "content": "hi"}],
                 ProviderConfig("cli-pid", kind="cli", cli_path="C:/cb.cmd"))
    deadline = time.time() + 3
    while not finished and time.time() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert finished == ["hi"]
    assert calls == ["cli-pid"]
