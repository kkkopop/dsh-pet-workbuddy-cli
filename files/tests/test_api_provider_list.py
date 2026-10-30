# -*- coding: utf-8 -*-
"""API/Provider 列表：设置页可添加、切换、删除多套 API 配置。"""
from __future__ import annotations

from PySide6.QtWidgets import QApplication

from pet.config import Config


def _app():
    return QApplication.instance() or QApplication([])


def test_chat_settings_provider_list_add_switch_delete(tmp_path):
    """AI 设置对话框：API 列表添加后能切换并保留编辑，删除后回到默认项。"""
    from pet.chat.settings_dialog import ChatSettingsDialog

    _app()
    cfg = Config(tmp_path)
    dlg = ChatSettingsDialog(cfg)
    try:
        assert dlg.provider_combo.count() == 1
        assert dlg.settings.active_provider == "openai-main"

        dlg._add_provider()
        new_pid = dlg.settings.active_provider
        assert dlg.provider_combo.count() == 2
        assert new_pid != "openai-main"
        assert new_pid in dlg.settings.providers

        # 修改新 API 后切到默认项再切回，草稿应保留
        dlg.name.setText("Second API")
        dlg.provider_combo.setCurrentIndex(dlg.provider_combo.findData("openai-main"))
        assert dlg.settings.active_provider == "openai-main"
        dlg.provider_combo.setCurrentIndex(dlg.provider_combo.findData(new_pid))
        assert dlg.name.text() == "Second API"

        # 删除新 API 后回到唯一默认项
        dlg._delete_provider()
        assert dlg.provider_combo.count() == 1
        assert dlg.settings.active_provider == "openai-main"
        assert dlg.settings.providers.keys() == {"openai-main"}
    finally:
        dlg.close()
        _app().processEvents()


def test_chat_settings_provider_id_not_reused_after_delete(tmp_path):
    """删除后再次添加不能复用已删除 provider_id，避免保存合并误过滤。"""
    from pet.chat.settings_dialog import ChatSettingsDialog

    _app()
    cfg = Config(tmp_path)
    dlg = ChatSettingsDialog(cfg)
    try:
        dlg._add_provider()
        first_pid = dlg.settings.active_provider
        dlg._delete_provider()
        dlg._add_provider()
        second_pid = dlg.settings.active_provider
        assert second_pid != first_pid
        assert first_pid not in dlg.settings.providers
        assert second_pid in dlg.settings.providers
    finally:
        dlg.close()
        _app().processEvents()


class _FakeStore:
    """系统钥匙串的进程内替身：``shared`` 是"钥匙串里的值"，``ok`` 控制写入成败。"""

    shared: dict = {}
    ok: bool = True

    def __init__(self, service_name="dsh-pet-standalone"):
        self.service_name = service_name

    def get(self, ref):
        return self.shared.get(ref, "")

    def set(self, ref, value):
        if not self.ok:
            return False
        self.shared[ref] = value
        return True

    @property
    def available(self):
        return self.ok


def _seed_key_ref(cfg, *, key="", vision_key="") -> None:
    """把默认 provider 补成"真实存过 Key"的形状（有 ref 才有可回显的凭据）。"""
    settings = cfg.chat_settings()
    provider = settings.providers["openai-main"]
    provider.api_key_ref = "provider/openai-main"
    provider.api_key = key
    provider.vision_api_key_ref = "provider/openai-main/vision"
    provider.vision_api_key = vision_key
    cfg.set_chat_settings(settings)


def test_chat_settings_provider_switch_keeps_key_draft(tmp_path, monkeypatch):
    """切走再切回后，已录入但未保存的 API Key 仍以**圆点**回显（不再被清成空白）。"""
    from PySide6.QtWidgets import QLineEdit

    import pet.chat.models as chat_models
    import pet.chat.settings_dialog as sd

    _FakeStore.shared = {}
    _FakeStore.ok = True
    monkeypatch.setattr(chat_models, "SecretStore", _FakeStore)
    monkeypatch.setattr(sd, "SecretStore", _FakeStore)
    _app()
    cfg = Config(tmp_path)
    dlg = sd.ChatSettingsDialog(cfg)
    try:
        dlg._add_provider()
        new_pid = dlg.settings.active_provider
        dlg.key.setText("sk-draft")

        # 切到默认项再切回：Key 输入框必须回显草稿里的值，而不是一片空白
        dlg.provider_combo.setCurrentIndex(dlg.provider_combo.findData("openai-main"))
        assert dlg.settings.active_provider == "openai-main"
        dlg.provider_combo.setCurrentIndex(dlg.provider_combo.findData(new_pid))
        assert dlg.key.text() == "sk-draft", "草稿里的 Key 被清空了（用户会以为丢了吧）"
        assert dlg.key.echoMode() == QLineEdit.EchoMode.Password, "回显必须是圆点（密码框）"
        assert dlg._provider_drafts[new_pid]["key"] == "sk-draft"
    finally:
        dlg.close()
        _app().processEvents()


def test_modern_ai_page_shows_a_saved_key_as_dots_after_reselecting(tmp_path, monkeypatch):
    """已保存的 Key 重新选中 API 后仍以圆点回显（钥匙串里那一份，不是构造期快照）。"""
    from PySide6.QtWidgets import QLineEdit

    import pet.chat.models as chat_models
    from pet.chat.ai_settings_page import _AiSettingsPage

    _FakeStore.shared = {"provider/openai-main": "sk-saved-123456",
                         "websearch/api": "sk-web-789"}
    _FakeStore.ok = True
    monkeypatch.setattr(chat_models, "SecretStore", _FakeStore)
    _app()
    cfg = Config(tmp_path)
    _seed_key_ref(cfg)
    page = _AiSettingsPage(cfg)
    try:
        assert page.key.text() == "sk-saved-123456", "构造时就要回显已保存的 Key"
        assert page.key.echoMode() == QLineEdit.EchoMode.Password, "echo 必须是圆点"
        assert page.web_search_key.text() == "sk-web-789", "搜索 Key 同一口径"

        page._add_provider()  # 新 API 没有 Key：空白才是对的
        assert page.key.text() == ""
        page.provider_combo.setCurrentData("openai-main")
        assert page.key.text() == "sk-saved-123456", (
            "重新选中 API 后 Key 又变空了——回显必须来自已保存的凭据")
    finally:
        page.deleteLater()
        _app().processEvents()


def test_saving_without_touching_the_key_keeps_the_saved_value(tmp_path, monkeypatch):
    """不改 Key 直接保存：落盘的仍是原值（回显的是真值，不是"••••"哨兵）。"""
    import pet.chat.models as chat_models
    from pet.chat.ai_settings_page import _AiSettingsPage

    _FakeStore.shared = {"provider/openai-main": "sk-saved-123456"}
    _FakeStore.ok = True
    monkeypatch.setattr(chat_models, "SecretStore", _FakeStore)
    _app()
    cfg = Config(tmp_path)
    _seed_key_ref(cfg)
    page = _AiSettingsPage(cfg)
    try:
        page.model.setText("deepseek-test")
        page.save()
        assert _FakeStore.shared["provider/openai-main"] == "sk-saved-123456"
        assert "•" not in _FakeStore.shared["provider/openai-main"], (
            "圆点被当成新 Key 存进去了：显示层不许用哨兵值冒充凭据")
        assert cfg.chat_settings().active_config.api_key_ref == "provider/openai-main"
    finally:
        page.deleteLater()
        _app().processEvents()


def test_unchanged_key_does_not_warn_when_the_keychain_is_unavailable(tmp_path, monkeypatch):
    """钥匙串不可用时，圆点回显的是内存明文；保存不该再弹一次「安全存储不可用」。

    回归点：输入框一旦始终回显，旧写法（"非空就写钥匙串"）会在每次保存都尝试写一个
    存不进去的同一个值，于是每点一次保存弹一次警告——而那个值一个字节都没变。
    """
    from PySide6.QtWidgets import QMessageBox

    import pet.chat.models as chat_models
    from pet.chat.ai_settings_page import _AiSettingsPage

    _FakeStore.shared = {}
    _FakeStore.ok = False  # 模拟没有可用后端
    monkeypatch.setattr(chat_models, "SecretStore", _FakeStore)
    warnings: list = []
    monkeypatch.setattr(QMessageBox, "warning",
                        staticmethod(lambda *a, **k: warnings.append(a)))
    _app()
    cfg = Config(tmp_path)
    _seed_key_ref(cfg, key="sk-mem-only")
    page = _AiSettingsPage(cfg)
    try:
        assert page.key.text() == "sk-mem-only", "钥匙串不可用时要回退到内存明文"
        page.save()
        assert warnings == [], f"没变的 Key 不该再触发保存警告（实得 {warnings}）"
        assert cfg.chat_settings().active_config.api_key == "sk-mem-only", (
            "内存明文必须仍然有效（下次启动前就靠它）")
    finally:
        page.deleteLater()
        _app().processEvents()


def test_chat_settings_provider_list_save_persists_active_provider(tmp_path):
    """保存后 active_provider 与新增 Provider 应写入配置。"""
    from pet.chat.settings_dialog import ChatSettingsDialog

    _app()
    cfg = Config(tmp_path)
    dlg = ChatSettingsDialog(cfg)
    try:
        dlg._add_provider()
        new_pid = dlg.settings.active_provider
        dlg.name.setText("Second API")
        dlg.save()

        reloaded = cfg.chat_settings()
        assert reloaded.active_provider == new_pid
        assert new_pid in reloaded.providers
        assert reloaded.providers[new_pid].name == "Second API"
    finally:
        dlg.close()
        _app().processEvents()


def test_modern_ai_page_provider_list_add_switch_delete(tmp_path, monkeypatch):
    """现代设置 AI 页：API 列表同样支持添加/切换/删除。"""
    import pet.modern_settings_dialog as settings_mod

    monkeypatch.setattr(settings_mod.autostart_mod, "is_enabled", lambda: False)
    _app()
    cfg = Config(tmp_path)
    dlg = settings_mod.ModernSettingsDialog(cfg, include_ai=True)
    try:
        page = dlg.ai_page
        assert page.provider_combo.count() == 1
        assert page.settings.active_provider == "openai-main"

        page._add_provider()
        new_pid = page.settings.active_provider
        assert page.provider_combo.count() == 2
        assert new_pid != "openai-main"

        page.name.setText("Second API")
        page.provider_combo.setCurrentData("openai-main")
        assert page.settings.active_provider == "openai-main"
        page.provider_combo.setCurrentData(new_pid)
        assert page.name.text() == "Second API"

        page._delete_provider()
        assert page.provider_combo.count() == 1
        assert page.settings.providers.keys() == {"openai-main"}
    finally:
        dlg.close()
        _app().processEvents()


def test_modern_settings_mounts_connection_kind_rows(tmp_path, monkeypatch):
    """「连接方式 / CLI 路径 / CLI 模型」必须被「模型与连接」认领并真实挂载。

    回归点：现代设置窗是**按行 key 认领后再重新装配**的（`claim("connection_kind", …)`），
    只在 `chat/ai_settings_page` 里建行、不在装配处认领 = 行不显示（只剩「待分类」）。
    """
    import pet.modern_settings_dialog as settings_mod
    from pet.modern_settings_dialog import SettingRow, SettingsSection
    from pet import settings_page_guide

    _app()
    monkeypatch.setattr(settings_mod.autostart_mod, "is_enabled", lambda: False)
    dlg = settings_mod.ModernSettingsDialog(Config(tmp_path), include_ai=True)
    try:
        index = next(i for i in range(dlg.sidebar.count())
                     if dlg.sidebar.item(i).text() == "AI 与对话")
        domain_page = dlg.pages.widget(index)
        for key in ("connection_kind", "cli_path", "cli_model"):
            row = dlg.findChild(SettingRow, f"settingRow_{key}")
            assert row is not None, f"缺少设置行 {key}"
            parent = row.parentWidget()
            while parent is not None and not isinstance(parent, SettingsSection):
                parent = parent.parentWidget()
            assert parent is not None, f"{key} 没有被任何分区认领（掉进待分类）"
            assert settings_page_guide.section_title(parent) == "模型与连接", (
                f"{key} 应挂在「模型与连接」组，实际在 "
                f"{settings_page_guide.section_title(parent)}"
            )
        # 认领 = 真的搬进「AI 与对话」域页；只建行不认领时它留在被 hide() 的
        # 旧 ai_page 里，对域页不可见（这就是"设置页上没有这一行"的成因）。
        assert dlg.findChild(SettingRow, "settingRow_connection_kind").isVisibleTo(domain_page)
        # CLI 两行在 HTTP 连接方式下隐藏（互斥显隐），切到 CLI 后必须出现。
        assert not dlg.findChild(SettingRow, "settingRow_cli_path").isVisibleTo(domain_page)
        dlg.ai_page.kind.setCurrentData("cli")
        _app().processEvents()
        assert dlg.findChild(SettingRow, "settingRow_cli_path").isVisibleTo(domain_page)
        assert dlg.findChild(SettingRow, "settingRow_cli_model").isVisibleTo(domain_page)
        assert not dlg.findChild(SettingRow, "settingRow_api_url").isVisibleTo(domain_page)
    finally:
        dlg.close()
        _app().processEvents()
