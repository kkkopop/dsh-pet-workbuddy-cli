from __future__ import annotations
import threading
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)
from .models import ChatSettings, ProviderConfig, SecretStore
from .providers import test_connection
from .themes import CHAT_UI_STYLE_LABELS, CHAT_UI_VIEW_ASPECT, theme_names
from .utils import _safe_emit

# 本对话框只读写经典风格的 chat_background 键（肥鱼版 DeepSeek 走主设置窗的
# modern_chat_background），所以裁切入口按单一风格标注名称；名字取自 themes 的
# 单一来源，不在本模块重复字面量。
_CLASSIC_STYLE_LABEL = CHAT_UI_STYLE_LABELS['classic']


class ChatSettingsDialog(QDialog):
    """AI 对话设置对话框。

    支持多 Provider / API 列表：可以在一个对话框里维护多套模型服务配置，
    通过下拉框快速切换当前要使用/编辑的 API；保存后 active_provider 会切到
    下拉框当前选中的那一项。

    测试连接在 Python daemon 线程中执行真实请求，通过本对象的 Signal
    排队回主线程更新界面——不用 QThread，避免线程对象被提前销毁导致的
    "Destroyed while thread is still running" 崩溃（多次连续测试时偶发）。
    """
    _test_done = Signal(bool, str)
    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.settings = config.chat_settings()
        p = self.settings.active_config
        self._test_thread = None
        self._provider_drafts: dict[str, dict] = {}
        self._deleted_provider_ids: set[str] = set()
        self._loading_provider = False
        self._test_done.connect(self._on_test_done)
        self.setWindowTitle('AI 对话设置')

        # API 列表：管理多套 Provider，快速切换当前 API
        self.provider_combo = QComboBox()
        self.provider_combo.setMinimumWidth(220)
        for pid, provider in self.settings.providers.items():
            self.provider_combo.addItem(self._provider_label(provider), pid)
        idx = self.provider_combo.findData(self.settings.active_provider)
        if idx >= 0:
            self.provider_combo.setCurrentIndex(idx)
        self.add_provider_btn = QPushButton('添加')
        self.add_provider_btn.setToolTip('复制当前 API 配置为一份新配置，便于填不同的地址/模型/Key')
        self.delete_provider_btn = QPushButton('删除')
        self.delete_provider_btn.setToolTip('删除当前选中的 API 配置；至少保留一个')
        provider_row = QWidget()
        provider_lay = QHBoxLayout(provider_row)
        provider_lay.setContentsMargins(0, 0, 0, 0)
        provider_lay.setSpacing(6)
        provider_lay.addWidget(self.provider_combo, 1)
        provider_lay.addWidget(self.add_provider_btn)
        provider_lay.addWidget(self.delete_provider_btn)

        form = QFormLayout()
        self.form = form
        self.name = QLineEdit(p.name)
        self.url = QLineEdit(p.base_url)
        self.model = QLineEdit(p.model)
        # 连接方式：http=OpenAI 兼容端点；cli=本地 codebuddy CLI（无需 API Key）
        self.kind = QComboBox()
        self.kind.addItem('OpenAI 兼容（HTTP）', 'http')
        self.kind.addItem('本地 CLI（无需 API Key）', 'cli')
        self.kind.setCurrentIndex(0 if getattr(p, 'kind', 'http') == 'http' else 1)
        self.cli_path = QLineEdit(getattr(p, 'cli_path', '') or '')
        self.cli_path.setPlaceholderText('留空自动检测 WorkBuddy 自带 codebuddy')
        self.cli_model = QLineEdit(getattr(p, 'cli_model', '') or '')
        self.cli_model.setPlaceholderText('如 hy4-preview / deepseek-v4.1-flash')
        # 已保存的 Key **始终**回显：密码框把每个字符画成圆点，"有 Key"看上去就是一串
        # 圆点（与 CodeBuddy 等模型服务一致），重新选中一份 API 也不会变回空白。框里放的
        # 是真值而不是"••••"哨兵——哨兵漏过一处判断就会被当新 Key 存下去，真凭据当场被
        # 圆点覆盖，这种"只是显示"的写法会毁数据。
        self.key = QLineEdit(self._stored_key(p.api_key_ref, p.api_key))
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText('尚未保存 API Key')
        self.key_hint = QLabel(self._key_status(p))
        self.key_hint.setObjectName('key-hint')
        self.key_hint.setWordWrap(True)
        self.prompt = QPlainTextEdit(self.settings.default_system_prompt)
        self.prompt.setMinimumHeight(120)
        self.timeout = QSpinBox()
        self.timeout.setRange(1, 600)
        self.timeout.setValue(int(p.timeout))
        self.temp = QDoubleSpinBox()
        self.temp.setRange(0, 2)
        self.temp.setSingleStep(.1)
        self.temp.setValue(p.temperature)
        self.tokens = QSpinBox()
        self.tokens.setRange(1, 32768)
        self.tokens.setValue(p.max_tokens)
        # 视觉模型（看看屏幕）：默认同聊天模型推导；取消勾选可手填（如免费 glm-4.6v-flash）
        self.vmodel = QLineEdit(p.vision_model)
        self.vmodel.setPlaceholderText('留空自动推导；免费视觉可用智谱 glm-4.6v-flash')
        self.vsame = QCheckBox('视觉模型同聊天模型（ds 文本模型自动换 vision-exp；GLM/Kimi 等多模态直接复用）')
        self.vsame.setChecked(p.vision_same_as_chat)
        self.vurl = QLineEdit(p.vision_base_url)
        self.vurl.setPlaceholderText('视觉 API 地址（留空复用聊天地址；GLM 填 https://open.bigmodel.cn/api/paas/v4）')
        self.vkey = QLineEdit(self._stored_key(p.vision_api_key_ref, p.vision_api_key))
        self.vkey.setEchoMode(QLineEdit.EchoMode.Password)
        self.vkey.setPlaceholderText('视觉 API Key（留空复用聊天 Key）')
        _vextra = [self.vmodel, self.vurl, self.vkey]
        def _vtoggle(c):
            for w in _vextra:
                w.setEnabled(not c)
        _vtoggle(p.vision_same_as_chat)
        self.vsame.toggled.connect(_vtoggle)
        # 聊天背景：纯色 / 内置主题 / 自定义图片 + 裁切取景
        self._bg_keys = [k for k, _ in theme_names()]
        self.bg_mode = QComboBox()
        self.bg_mode.addItems(['纯色（奶油）'] + [n for _, n in theme_names()] + ['自定义图片…'])
        cur = str(config.get('chat_background', '') or '')
        self.bg = QLineEdit(cur if cur and not cur.startswith('builtin:') else '')
        self.bg.setPlaceholderText('自定义图片路径，或点浏览选图')
        self.bg_btn = QPushButton('浏览…')
        self.bg_btn.clicked.connect(self._pick_bg)
        bg_row = QWidget()
        bg_lay = QHBoxLayout(bg_row)
        bg_lay.setContentsMargins(0, 0, 0, 0)
        bg_lay.addWidget(self.bg)
        bg_lay.addWidget(self.bg_btn)
        # 显式父对象：无父时 setVisible(True) 会瞬时建顶层原生窗口，慢机/远程桌面
        # 可能闪一下（下面 addWidget 之后才真正入布局）。
        self.crop_btn = QPushButton(f'裁切取景（{_CLASSIC_STYLE_LABEL}）…', self)
        self.crop_btn.clicked.connect(self._crop_bg)
        # contain/stretch 下取景框被渲染路径整体忽略（见 themes.py 的 fill 语义）；
        # 本对话框没有填充方式控件，直接按配置里的经典风格填充值决定入口是否可见。
        self.crop_btn.setVisible(str(config.get('chat_background_fill', 'cover') or 'cover') == 'cover')
        bgmode_row = QWidget()
        bgm_lay = QHBoxLayout(bgmode_row)
        bgm_lay.setContentsMargins(0, 0, 0, 0)
        bgm_lay.addWidget(self.bg_mode)
        bgm_lay.addWidget(self.crop_btn)
        theme_idx = self._bg_keys.index(cur[8:]) + 1 if cur.startswith('builtin:') and cur[8:] in self._bg_keys else None
        self.bg_mode.setCurrentIndex(0 if not cur else (theme_idx if theme_idx is not None else len(self._bg_keys) + 1))
        bg_row.setVisible(bool(cur) and theme_idx is None)
        self.bg_mode.currentIndexChanged.connect(lambda i: bg_row.setVisible(i == len(self._bg_keys) + 1))
        self.skip_ssl = QCheckBox('跳过 SSL 证书验证（开着代理/梯子、本地网关或自签名证书时勾选）')
        self.skip_ssl.setChecked(not p.verify_ssl)
        self.system_notify_check = QCheckBox('对话完成 / 生成失败 / 需要授权时，在桌面右下角弹系统通知')
        self.system_notify_check.setChecked(bool(config.get("system_notifications_enabled", True)))

        form.insertRow(0, 'API 列表', provider_row)
        form.addRow('连接方式', self.kind)
        for label, w in [('Provider 名称', self.name), ('API 地址', self.url),
                         ('模型', self.model), ('', self.vsame), ('视觉模型', self.vmodel), ('视觉 API 地址', self.vurl), ('视觉 API Key', self.vkey),
                         ('API Key', self.key), ('', self.key_hint),
                         ('系统通知', self.system_notify_check),
                         ('System Prompt', self.prompt),
                         ('聊天背景', bgmode_row), ('', bg_row),
                         ('超时（秒）', self.timeout), ('Temperature', self.temp),
                         ('Max Tokens', self.tokens)]:
            form.addRow(label, w)
        self._cli_path_label = QLabel('CLI 路径')
        self._cli_model_label = QLabel('CLI 模型')
        form.addRow(self._cli_path_label, self.cli_path)
        form.addRow(self._cli_model_label, self.cli_model)
        form.addRow(self.skip_ssl)
        self.result = QLabel('')
        self.result.setWordWrap(True)
        self.test = QPushButton('测试连接')
        self.test.clicked.connect(self._run_test)
        save = QPushButton('保存')
        save.clicked.connect(self.save)
        buttons = QHBoxLayout()
        buttons.addWidget(self.test)
        buttons.addStretch(1)
        buttons.addWidget(save)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.result)
        layout.addLayout(buttons)

        self.provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        self.add_provider_btn.clicked.connect(self._add_provider)
        self.delete_provider_btn.clicked.connect(self._delete_provider)
        self.kind.currentIndexChanged.connect(self._on_kind_changed)
        self._update_provider_buttons()
        self._update_kind_visibility()

    # ---------------------------------------------------------------- provider 列表
    @staticmethod
    def _provider_label(p) -> str:
        name = str(p.name or p.provider_id)
        model = str(p.model or '').strip()
        return f'{name} · {model}' if model else name

    def _capture_current_draft(self) -> None:
        """把当前表单的非提交态保存到草稿，切换 API 时不会丢编辑。"""
        pid = self.settings.active_provider
        if not pid or pid not in self.settings.providers:
            return
        existing = self._provider_drafts.get(pid, {})
        key_text = self.key.text()
        vkey_text = self.vkey.text()
        self._provider_drafts[pid] = {
            'name': self.name.text().strip(),
            'base_url': self.url.text().strip(),
            'model': self.model.text().strip(),
            # 输入框为空表示“不修改/不覆盖”，保留草稿里已录入但尚未保存的 Key；
            # 否则 _load_provider_ui() 清空输入框后会把草稿 Key 覆盖成空。
            'key': key_text if key_text else existing.get('key', ''),
            'timeout': float(self.timeout.value()),
            'temperature': float(self.temp.value()),
            'max_tokens': int(self.tokens.value()),
            'vision_model': self.vmodel.text().strip(),
            'vision_same_as_chat': self.vsame.isChecked(),
            'vision_base_url': self.vurl.text().strip(),
            'vision_key': vkey_text if vkey_text else existing.get('vision_key', ''),
            'verify_ssl': not self.skip_ssl.isChecked(),
            'kind': str(self.kind.currentData() or 'http'),
            'cli_path': self.cli_path.text().strip(),
            'cli_model': self.cli_model.text().strip(),
        }

    def _load_provider_ui(self, provider_id: str) -> None:
        p = self.settings.providers.get(provider_id)
        if p is None:
            return
        draft = self._provider_drafts.get(provider_id, {})
        self.settings.active_provider = provider_id
        self.name.setText(draft.get('name') if draft.get('name') is not None else p.name)
        self.url.setText(draft.get('base_url') if draft.get('base_url') is not None else p.base_url)
        self.model.setText(draft.get('model') if draft.get('model') is not None else p.model)
        # 重新选中一份 API 时 Key 不再是空白：草稿里录入过的值优先，其次已保存的凭据
        # （钥匙串 / 内存明文）。密码框把它画成圆点，"有 Key" 一眼可见；"留空" 仍表示不改。
        self.key.setText(str(draft.get('key') or '') or self._stored_key(p.api_key_ref, p.api_key))
        self.key_hint.setText(self._key_status(p))
        self.timeout.setValue(int(draft.get('timeout', p.timeout)))
        self.temp.setValue(float(draft.get('temperature', p.temperature)))
        self.tokens.setValue(int(draft.get('max_tokens', p.max_tokens)))
        self.vmodel.setText(draft.get('vision_model', p.vision_model))
        self.vsame.setChecked(bool(draft.get('vision_same_as_chat', p.vision_same_as_chat)))
        self.vurl.setText(draft.get('vision_base_url', p.vision_base_url))
        self.vkey.setText(
            str(draft.get('vision_key') or '')
            or self._stored_key(p.vision_api_key_ref, p.vision_api_key))
        self.skip_ssl.setChecked(not bool(draft.get('verify_ssl', p.verify_ssl)))
        self.kind.setCurrentIndex(0 if str(draft.get('kind', getattr(p, 'kind', 'http')) or 'http') == 'http' else 1)
        self.cli_path.setText(draft.get('cli_path', getattr(p, 'cli_path', '')))
        self.cli_model.setText(draft.get('cli_model', getattr(p, 'cli_model', '')))
        self._update_provider_buttons()
        self._update_kind_visibility()

    def _on_provider_changed(self, _index: int) -> None:
        if self._loading_provider:
            return
        self._capture_current_draft()
        pid = self.provider_combo.currentData()
        if pid and pid in self.settings.providers:
            self._load_provider_ui(pid)

    def _new_provider_id(self) -> str:
        # 避免复用已删除的 provider_id：否则保存合并时新建项会被删除集合过滤掉。
        used = set(self.settings.providers) | set(self._deleted_provider_ids)
        i = len(self.settings.providers) + 1
        while f'api-{i}' in used:
            i += 1
        return f'api-{i}'

    def _add_provider(self) -> None:
        self._capture_current_draft()
        base = self.settings.active_config
        new_id = self._new_provider_id()
        new = ProviderConfig(
            new_id,
            name=f'{base.name} 副本',
            base_url=base.base_url,
            chat_path=base.chat_path,
            model=base.model,
            api_key_ref=f'provider/{new_id}',
            timeout=base.timeout,
            temperature=base.temperature,
            max_tokens=base.max_tokens,
            vision_model=base.vision_model,
            vision_same_as_chat=base.vision_same_as_chat,
            vision_base_url=base.vision_base_url,
            vision_api_key_ref=f'provider/{new_id}/vision',
            verify_ssl=base.verify_ssl,
            kind=getattr(base, 'kind', 'http'),
            cli_path=getattr(base, 'cli_path', ''),
            cli_model=getattr(base, 'cli_model', ''),
        )
        self.settings.providers[new_id] = new
        # 复制当前草稿（不含 Key），方便基于同一份参数快速修改
        self._provider_drafts[new_id] = {
            'name': new.name,
            'base_url': new.base_url,
            'model': new.model,
            'key': '',
            'timeout': new.timeout,
            'temperature': new.temperature,
            'max_tokens': new.max_tokens,
            'vision_model': new.vision_model,
            'vision_same_as_chat': new.vision_same_as_chat,
            'vision_base_url': new.vision_base_url,
            'vision_key': '',
            'verify_ssl': new.verify_ssl,
            'kind': getattr(new, 'kind', 'http'),
            'cli_path': getattr(new, 'cli_path', ''),
            'cli_model': getattr(new, 'cli_model', ''),
        }
        self.provider_combo.addItem(self._provider_label(new), new_id)
        self.provider_combo.setCurrentIndex(self.provider_combo.count() - 1)
        # setCurrentIndex 会触发 _on_provider_changed，但 active_provider 已在
        # _capture_current_draft 后仍为旧值；这里显式切换并加载新配置。
        self._capture_current_draft()
        self.settings.active_provider = new_id
        self._load_provider_ui(new_id)
        self._update_provider_buttons()

    def _delete_provider(self) -> None:
        if len(self.settings.providers) <= 1:
            return
        pid = self.provider_combo.currentData()
        if not pid or pid not in self.settings.providers:
            return
        self.settings.providers.pop(pid, None)
        self._provider_drafts.pop(pid, None)
        self._deleted_provider_ids.add(pid)
        index = self.provider_combo.currentIndex()
        self._loading_provider = True
        try:
            self.provider_combo.removeItem(index)
            new_pid = next(iter(self.settings.providers))
            new_index = self.provider_combo.findData(new_pid)
            if new_index >= 0:
                self.provider_combo.setCurrentIndex(new_index)
        finally:
            self._loading_provider = False
        self.settings.active_provider = next(iter(self.settings.providers))
        self._load_provider_ui(self.settings.active_provider)

    def _update_provider_buttons(self) -> None:
        self.delete_provider_btn.setEnabled(len(self.settings.providers) > 1)

    def _on_kind_changed(self, _index: int) -> None:
        if self._loading_provider:
            return
        self._capture_current_draft()
        self._update_kind_visibility()

    def _set_row_visible(self, widget, visible: bool) -> None:
        widget.setVisible(visible)
        label = self.form.labelForField(widget)
        if label is not None:
            label.setVisible(visible)

    def _update_kind_visibility(self) -> None:
        cli = str(self.kind.currentData() or 'http') == 'cli'
        self._set_row_visible(self.url, not cli)
        self._set_row_visible(self.key, not cli)
        self.key_hint.setVisible(not cli)
        self._cli_path_label.setVisible(cli)
        self.cli_path.setVisible(cli)
        self._cli_model_label.setVisible(cli)
        self.cli_model.setVisible(cli)
        self.test.setText('检测 CLI' if cli else '测试连接')

    # ---------------------------------------------------------------- 保存与测试
    def _pick_bg(self):
        path, _ = QFileDialog.getOpenFileName(self, '选择聊天背景图', '', '图片文件 (*.png *.jpg *.jpeg *.webp *.bmp)')
        if path:
            self.bg.setText(path)

    def _crop_bg(self):
        from .crop_dialog import CropDialog
        from .themes import get_theme
        from .widgets import resolve_bg_pixmap
        i = self.bg_mode.currentIndex()
        value = '' if i == 0 else ('builtin:' + self._bg_keys[i - 1] if i <= len(self._bg_keys) else self.bg.text().strip())
        pix = resolve_bg_pixmap(value)
        if pix is None:
            self.crop_btn.setText(f'无可裁背景（{_CLASSIC_STYLE_LABEL}）')
            return
        crops = dict(self.config.get('chat_bg_crops', {}) or {})
        initial = crops.get(value)
        if initial is None and value.startswith('builtin:'):
            t = get_theme(value[8:])
            initial = tuple(t['focus']) if t else None
        dlg = CropDialog(pix, initial, self, _CLASSIC_STYLE_LABEL, CHAT_UI_VIEW_ASPECT['classic'])
        accepted = dlg.exec()
        reset, box = dlg.result_box() if accepted else (False, None)
        dlg.deleteLater()  # exec 后即释放：对话框持有整张背景 QPixmap，不随使用次数累积
        if accepted:
            if reset:
                crops.pop(value, None)
            else:
                crops[value] = [round(float(v), 4) for v in box]
            self.config.set('chat_bg_crops', crops)
            self.config.save()

    @staticmethod
    def _stored_key(ref: str, inline: str = '') -> str:
        """已保存凭据的**当前值**：系统钥匙串优先，回落内存明文。

        与 `Config.resolve_api_key` 同一优先序——输入框里那串圆点必须是运行时真正会用的
        那一份，否则会出现"显示已保存、实际发空串"。
        """
        return SecretStore().get(ref) or str(inline or '')

    @staticmethod
    def _key_status(p) -> str:
        """当前是否已保存 API Key（提示用户圆点即已保存，留空表示不修改）。"""
        saved = p.api_key or SecretStore().get(p.api_key_ref)
        if saved:
            return '已保存 API Key（输入框里的圆点就是它；要更换就全选重填，留空保持不变）'
        return '尚未设置 API Key（填入后保存即生效）'

    def _provisional_config(self) -> ProviderConfig:
        """用表单当前值构造一份临时配置（不保存），供测试连接使用。"""
        p = self.settings.active_config
        return ProviderConfig(
            p.provider_id,
            self.name.text().strip() or p.name,
            self.url.text().strip(),
            p.chat_path,
            self.model.text().strip(),
            p.api_key_ref,
            # 表单未填时回退钥匙串：凭据默认存系统钥匙串，直接读 api_key 为空
            self.key.text() or p.api_key or SecretStore().get(p.api_key_ref),
            float(self.timeout.value()),
            float(self.temp.value()),
            int(self.tokens.value()),
            verify_ssl=not self.skip_ssl.isChecked(),
            kind=str(self.kind.currentData() or 'http'),
            cli_path=self.cli_path.text().strip(),
            cli_model=self.cli_model.text().strip(),
        )

    def _run_test(self):
        if self._test_thread is not None and self._test_thread.is_alive():
            return
        self.test.setEnabled(False)
        self.test.setText('测试中…')
        self.result.setText('')
        self.result.setStyleSheet('color: #666666;')
        self._test_thread = threading.Thread(
            target=self._run_test_worker,
            args=(self._provisional_config(),),
            daemon=True,
            name='pet-chat-connection-test',
        )
        self._test_thread.start()

    def _run_test_worker(self, provider_config: ProviderConfig):
        if getattr(provider_config, 'kind', 'http') == 'cli':
            ok, message = self._probe_cli(provider_config)
        else:
            ok, message = test_connection(provider_config, timeout=10.0)
        # 对话框可能在测试在飞时被关闭销毁（WA_DeleteOnClose）：直接
        # self._test_done.emit 会对已删 C++ 对象 RuntimeError。
        _safe_emit(self, "_test_done", ok, message)

    @staticmethod
    def _probe_cli(provider_config):
        from .cli import discover_cli
        from .cli.bridge import CliBridge

        path = (getattr(provider_config, 'cli_path', '') or '').strip() or discover_cli()
        if not path:
            return False, '未检测到 CodeBuddy CLI，请填写 CLI 路径或安装 WorkBuddy'
        model = (getattr(provider_config, 'cli_model', '') or '').strip() or 'hy4-preview'
        try:
            return CliBridge(path, model=model).probe()
        except Exception as exc:  # noqa: BLE001
            return False, f'CLI 探测失败：{exc}'

    def _on_test_done(self, ok: bool, message: str):
        self.test.setEnabled(True)
        self._update_kind_visibility()
        self.result.setText(message)
        self.result.setStyleSheet('color: #16a34a;' if ok else 'color: #dc2626;')
        self._test_thread = None

    def _apply_draft_to_provider(self, provider_id: str) -> None:
        p = self.settings.providers.get(provider_id)
        draft = self._provider_drafts.get(provider_id)
        if p is None or not draft:
            return
        if draft.get('name'):
            p.name = draft['name']
        p.base_url = draft.get('base_url') or p.base_url
        p.model = draft.get('model') or p.model
        p.timeout = float(draft.get('timeout', p.timeout))
        p.temperature = float(draft.get('temperature', p.temperature))
        p.max_tokens = int(draft.get('max_tokens', p.max_tokens))
        p.vision_model = draft.get('vision_model', p.vision_model)
        p.vision_same_as_chat = bool(draft.get('vision_same_as_chat', p.vision_same_as_chat))
        p.vision_base_url = draft.get('vision_base_url', p.vision_base_url)
        p.verify_ssl = bool(draft.get('verify_ssl', p.verify_ssl))
        p.kind = str(draft.get('kind', getattr(p, 'kind', 'http')) or 'http')
        p.cli_path = str(draft.get('cli_path') or '')
        p.cli_model = str(draft.get('cli_model') or '')
        key = str(draft.get('key') or '')
        # 只写**真的变了**的值：输入框现在始终回显已保存的 Key，无条件写回就是每次保存
        # 都往钥匙串写一遍同一个值；钥匙串不可用的机器上还会每次弹一次"仅本次运行保留"
        # （而那个 Key 本来就是内存明文，一个字节都没变）。
        if key and key != self._stored_key(p.api_key_ref, p.api_key):
            p.api_key_ref = p.api_key_ref or f'provider/{provider_id}'
            if not SecretStore().set(p.api_key_ref, key):
                p.api_key = key
                QMessageBox.warning(self, '安全存储不可用', '无法使用系统安全存储，Key 仅本次运行保留，重启需重输。')
        vkey = str(draft.get('vision_key') or '')
        if vkey and vkey != self._stored_key(p.vision_api_key_ref, p.vision_api_key):
            p.vision_api_key_ref = p.vision_api_key_ref or f'provider/{provider_id}/vision'
            if not SecretStore().set(p.vision_api_key_ref, vkey):
                p.vision_api_key = vkey
                QMessageBox.warning(self, '安全存储不可用', '无法使用系统安全存储，Key 仅本次运行保留，重启需重输。')

    def save(self):
        # 保存前基于磁盘最新配置重取快照：另一个设置窗口可能在本窗口打开期间
        # 写入结构性改动。先保留本窗口的 provider 草稿/新增/删除，再与磁盘快照合并。
        old_settings = self.settings
        self.config.reload()
        self._capture_current_draft()
        self.settings = self.config.chat_settings()
        # 合并 provider 结构：保留磁盘中其它窗口新增的项，应用本窗口删除/新增的项。
        for pid in list(self.settings.providers):
            if pid in self._deleted_provider_ids:
                self.settings.providers.pop(pid, None)
        for pid, provider in old_settings.providers.items():
            if pid not in self.settings.providers and pid not in self._deleted_provider_ids:
                self.settings.providers[pid] = provider
        active_pid = self.provider_combo.currentData()
        if active_pid in self.settings.providers:
            self.settings.active_provider = active_pid
        elif self.settings.providers:
            self.settings.active_provider = next(iter(self.settings.providers))
        for pid in list(self.settings.providers):
            self._apply_draft_to_provider(pid)
        self.settings.default_system_prompt = self.prompt.toPlainText().strip()
        i = self.bg_mode.currentIndex()
        bg_val = '' if i == 0 else ('builtin:' + self._bg_keys[i - 1] if i <= len(self._bg_keys) else self.bg.text().strip())
        self.config.set('chat_background', bg_val)
        self.config.set('system_notifications_enabled', self.system_notify_check.isChecked())
        self.config.set_chat_settings(self.settings)
        if not self.config.save():
            QMessageBox.warning(self, '保存失败', '配置未能写入磁盘，改动可能在重启后丢失。\n\n配置路径：' + str(self.config.path))
        self.accept()
