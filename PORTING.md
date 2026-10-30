# 逐文件改动说明（手工合并用）

本包按 **dsh-pet v4.4.0** 生成。若你的树是别的版本，或不想整文件覆盖，按下面逐条合并。
每一条都给了「改哪 / 加了什么 / 为什么」，锚点是**改动前的原文**，便于搜索定位。

---

## 1. 新增：`pet/chat/cli/`

| 文件 | 内容 |
|---|---|
| `__init__.py` | 导出 `CliBridge` / `CliBridgeError` / `CliProvider` / `build_argv` / `discover_cli` / `map_event` / `DEFAULT_MODEL` / `DISALLOWED_TOOLS` |
| `bridge.py` | 子进程桥。`discover_cli()` 路径发现；`build_argv()` 参数拼接（含工具禁用）；`map_event()` 事件映射；`CliBridge` 长驻进程 / 读线程+队列 / 取消即终止 / `probe()` 只读检测 |
| `provider.py` | `CliProvider.stream()`：把桥的 `(kind, text)` 映射成正文增量 / `StreamEvent('reasoning'/'status')` / `ProviderError`；system 文本经 `--system-prompt` 注入，历史以文本转录每轮重发 |

**零 Qt 依赖**，只 import 标准库；`provider.py` 依赖父包的 `ProviderError` / `StreamEvent`。

---

## 2. `pet/chat/models.py`

**2.1 `ProviderConfig` 增加三个字段**（放在 `reasoning_effort` 之后、`web_search` 之前）：

```python
    # 连接方式：'http'=OpenAI 兼容端点（默认），'cli'=本地 codebuddy CLI（无需 API Key）
    kind: str='http'; cli_path: str=''; cli_model: str=''
```

**2.2 `ProviderConfig.from_dict()` 末尾补三行**（在 `reasoning_effort` 赋值之后、`return c` 之前）：

```python
        c.kind=_choice(raw.get('kind','http'),('http','cli'),'http')
        c.cli_path=str(raw.get('cli_path',''))
        c.cli_model=str(raw.get('cli_model',''))
```

> `to_dict()` 用 `asdict(self)`，字段自动带出，不需要改。

**2.3 `ChatSettings` 增加 `aux_provider` 字段与 `http_fallback` 属性**：

- dataclass 末尾加 `aux_provider: str=''`；
- `from_dict()` 末尾参数补 `str(raw.get('aux_provider',''))`；
- `to_dict()` 增加 `'aux_provider': self.aux_provider`；
- 新增属性：

```python
    @property
    def http_fallback(self):
        """CLI 类型激活时，视觉/翻译/记忆提炼等 HTTP 旁路的回退 provider。"""
        if self.aux_provider and self.aux_provider in self.providers:
            p = self.providers[self.aux_provider]
            if getattr(p, 'kind', 'http') != 'cli' and p.base_url:
                return p
        for p in self.providers.values():
            if getattr(p, 'kind', 'http') != 'cli' and p.base_url:
                return p
        return None
```

---

## 3. `pet/chat/providers.py`

在 `_safe_error_detail()` **之前**加工厂：

```python
def build_provider(config):
    """按 ``ProviderConfig.kind`` 返回 provider 实例；未知/缺失一律回落 HTTP 实现。"""
    if getattr(config, 'kind', 'http') == 'cli':
        from .cli.provider import CliProvider
        return CliProvider()
    return OpenAICompatibleProvider()
```

> 惰性 import 是刻意的：`cli/provider.py` 反过来 import 本模块的 `ProviderError`/`StreamEvent`，
> 顶层互相 import 会成环。

---

## 4. `pet/chat/service.py`

1. 导入改为：`from .providers import StreamEvent, build_provider`（去掉 `OpenAICompatibleProvider`）；
2. `__init__`：`self.provider=provider`（**不再**默认 `OpenAICompatibleProvider()`），并加 `self._extract_provider=None`；
3. `send()`：解析 provider 并记住它（记忆提炼要用同一条）：

```python
        provider=self.provider or build_provider(config)
        worker=_Worker(provider,messages,config,cancel); self._request_id=rid; self._cancel=cancel; self._worker=worker
        self._extract_provider=provider
```

4. `_maybe_extract_memory()`：`hook(user_text,reply_text,self._extract_provider,config)`。

---

## 5. `pet/config.py`

`_default_chat_data()['providers']['openai-main']` 增加三键（新 provider 会继承 `kind="http"`）：

```python
                "kind": "http",
                "cli_path": "",
                "cli_model": "",
```

> `_merge_chat_data()` 不需要改：它以 openai-main 默认值为骨架再 `update(用户值)`，老配置自动得到 `kind=http`。

---

## 6. `pet/chat/ai_settings_page.py`（现代设置页）

| 位置 | 改动 |
|---|---|
| `__init__` 建控件处（`self.model = _line_edit(...)` 之后） | 新增 `self.kind`（`ModernSelect`：HTTP / 本地 CLI）、`self.cli_path`、`self.cli_model` 三个控件 |
| 「模型与连接」`SettingsSection` | 把内联 `SettingRow(...)` 提成属性（`self._kind_row` / `_api_url_row` / `_api_key_row` / `_cli_path_row` / `_cli_model_row`），并把它们插进列表（顺序：Provider 名称 → 连接方式 → API 地址 → 模型 → API Key → CLI 路径 → CLI 模型 → System Prompt → 连接测试） |
| 信号连接处 | `self.kind.currentIndexChanged.connect(self._on_kind_changed)`；末尾调一次 `self._update_kind_visibility()` |
| 新增方法 | `_on_kind_changed()`（先 `_capture_current_draft()` 再刷新可见性）、`_update_kind_visibility()`（cli 时隐藏 API 地址/Key、显示 CLI 两行、按钮文案改「检测 CLI」） |
| `_capture_current_draft()` | 草稿字典加 `"kind" / "cli_path" / "cli_model"` |
| `_load_provider_ui()` | 回填三个字段 + `_update_kind_visibility()` |
| `_add_provider()` | 新 Provider 与其草稿都带上三个字段 |
| `_apply_draft_to_provider()` | 写回 `p.kind / p.cli_path / p.cli_model` |
| `provisional_config()` | 三个字段进临时配置（供「检测」用） |
| `_run_test_worker()` | `kind=='cli'` 时改走 `_probe_cli()`；新增 `_probe_cli()`：`discover_cli()` 找不到就返回可读错误，否则 `CliBridge(path, model=...).probe()` |
| `_on_test_done()` | 不再写死「测试连接」文案，改为 `_update_kind_visibility()` 复原 |

---

## 7. `pet/chat/settings_dialog.py`（托盘老窗，同语义）

与第 6 节一一对应，差异只在控件类型（`QComboBox` / `QLineEdit`）与表单是 `QFormLayout`：

- 建控件处加 `self.kind` / `self.cli_path` / `self.cli_model`，并保存 `self.form = form`；
- 表单里 `form.addRow('连接方式', self.kind)`；CLI 两行用显式 `QLabel` 持有，便于显隐；
- 新增 `_on_kind_changed()` / `_set_row_visible()` / `_update_kind_visibility()`（`QFormLayout.labelForField` 拿行标签一起隐藏）；
- `_capture_current_draft` / `_load_provider_ui` / `_add_provider` / `_apply_draft_to_provider` / `_provisional_config` 同步三个字段；
- `_run_test_worker()` 分支到 `_probe_cli()`；`_on_test_done()` 用 `_update_kind_visibility()` 复原文案。

---

## 8. `pet/modern_settings_dialog.py` ★ **最容易漏**

`_rebuild_domain_navigation()` 里「模型与连接」的 `claim(...)` 必须补三行，否则行不会显示：

```python
                    (
                        "模型与连接",
                        claim(
                            "provider_name",
                            "connection_kind",   # ← 新增
                            "api_url",
                            "model",
                            "api_key",
                            "cli_path",          # ← 新增
                            "cli_model",         # ← 新增
                            "system_prompt",
                            "connection_test",
                        ),
                    ),
```

> 现代设置窗不是直接渲染 `ai_settings_page` 的 `SettingsSection`，而是按 `settingRow_<key>` **认领后重新装配**；
> 未认领的行留在被 `hide()` 的旧页里 = 用户看不到。回归用例：`tests/test_api_provider_list.py::test_modern_settings_mounts_connection_kind_rows`。

---

## 9. HTTP 旁路回退（3 处）

| 文件 | 位置 | 改动 |
|---|---|---|
| `pet/screen_capture/vision_bridge.py` | `resolve_chat_context()` | `active_config.kind=='cli'` 时改用 `settings.http_fallback`；没有可用 HTTP 时 `raise RuntimeError("当前对话用的是本地 CLI …请保留一个 HTTP 服务作为回退")` |
| `pet/window.py` | `_on_look_screen()` 取 provider 处 | 同上；无回退时 `self._look_busy=False` + `show_bubble(...)` 直接返回 |
| `pet/chat/memory_extract.py` | `schedule_extraction()` 护栏段 | `provider_config.kind=='cli'` 时换成 `http_fallback`（`copy` 后 `api_key = config.resolve_api_key(fallback)`）+ `OpenAICompatibleProvider()`；**没有 HTTP 就 `return False` 静默跳过** |

---

## 10. 测试与文档

**新增**：

- `tests/fake_cli.py` —— 假 CLI 桩（`sys.executable` 启动，按环境变量切换：正常 / 报错 / 崩溃 / 挂起），
  注意桩里 `sys.stdout.reconfigure(encoding="utf-8")`，否则 Windows 默认 GBK 会污染 JSONL；
- `tests/test_chat_cli_provider.py` —— 15 例：事件映射、argv 工具禁用、路径发现、子进程收发、取消终止后重建、崩溃报错、`CliProvider` 的文本/思考/status/空回复/取消/转录；
- `tests/test_chat_cli_config.py` —— 11 例：`kind` 序列化与默认值、`http_fallback` 选择、`build_provider` 分派、`ChatService.send` 按 kind 解析、`resolve_chat_context` 回退；
- `tests/test_api_provider_list.py` 新增 1 例：现代设置窗里三行被「模型与连接」认领且挂进「AI 与对话」域页。

**可选**：`docs/SETTINGS-CHANGE-GATES.md` 的「变更记录」里补一条新键登记（本包未包含该文档的改动，
因为它是项目自己的门禁文件，合并时按你的仓库口径写）。

---

## 11. 验证清单

```bash
python -m pytest tests/test_chat_cli_provider.py tests/test_chat_cli_config.py tests/test_api_provider_list.py -q
python -m ruff check pet tests
python -m pet          # 打开设置 → AI 与对话，应看到「连接方式」
```
