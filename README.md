# dsh-pet × WorkBuddy CLI —— 让桌宠用账号额度聊天的可移植补丁包

> 给 [dsh-pet](https://github.com/MerZlin/dsh-pet-indesktop)（PySide6 桌面宠物）加一种新的**模型服务连接方式**：
> 不再依赖用户自备的 API Key，而是调用本机已登录的 **WorkBuddy / CodeBuddy 官方 CLI**，
> 直接消耗该账号的订阅额度（积分）。
>
> 本仓库是**可移植补丁包**，不是项目 fork：把它覆盖到你的 dsh-pet 源码树即可使用。

---

## 一、背景：为什么要做这个

dsh-pet 的 AI 对话走标准 OpenAI Chat Completions 协议，用户需要在设置里填 `API 地址 + API Key`。

而腾讯的 WorkBuddy / CodeBuddy 有一套**积分（额度）**体系：

- 所有对话请求都消耗账号积分，不同模型单价不同；
- 但**个人 API Key 只对标准版/个人版账号开放**（`https://copilot.tencent.com/profile/` 里的 Key 页面）；
- 非标准版账号拿不到这把 Key —— 于是"直接填 URL + Key 指向 `copilot.tencent.com`"这条路走不通。

同时我们又实测确认了另一条路：

| 探测 | 结果 |
|---|---|
| `POST https://copilot.tencent.com/v2/chat/completions`（不带凭据） | `401 Authorization Required`（APISIX 网关）→ 端点真实存在 |
| 本机 WorkBuddy 客户端 | 自带 `codebuddy.cmd`（实测 **2.161.4**）与 `node.exe`（**22.22.2**），**不需要另装 Node** |
| `codebuddy -p "…"`（不带任何 Key） | 正常返回，init 事件里 `"apiKeySource":"copilot.tencent.com"` → 走的是**账号登录态** |

于是有了这个方案：**桌宠不碰密钥，只把 CLI 当"模型"来驱动**。

---

## 二、这个功能是做什么的

给 dsh-pet 的 Provider 增加一个字段 `kind`，取值为：

- `http`（默认，保持原行为）：走 OpenAI 兼容端点 + API Key；
- `cli`（本补丁新增）：走本机 `codebuddy` 子进程，**不需要 API Key**，消耗账号积分。

选 `cli` 之后在设置页里：

- 「API 地址」「API Key」两行自动隐藏；
- 换成「CLI 路径」（留空自动发现）、「CLI 模型」（如 `deepseek-v4.1-flash`）；
- 「连接测试」变成「检测 CLI」——只跑 `--version`，**不消耗积分**。

对话体验与 HTTP 模式一致：逐字流式、思考过程可折叠、可中途停止、失败有可读报错。

```
ChatWindow / QuickChat / FileInterpret
        │
        ▼
   ChatService.send(messages, config)
        │  build_provider(config)  ← 按 config.kind 分派
        ├── kind=http ──► OpenAICompatibleProvider（原样不动）
        └── kind=cli  ──► CliProvider ──► CliBridge ──► codebuddy 子进程（长驻，stream-json）
                                        └─ 账号登录态 → copilot.tencent.com → 扣积分
```

### 设计要点

| 点 | 做法 | 原因 |
|---|---|---|
| 进程形态 | **长驻子进程**，`--input-format stream-json --output-format stream-json --include-partial-messages` | 单次 `-p` 实测冷启动 ≈8.5s，每条消息都起进程会慢到不可用 |
| 事件映射 | `stream_event.delta.text_delta`→正文；`thinking_delta`→思考动画；`result.is_error`→可读错误 | 复用项目既有的 `stream()` 迭代器契约（yield 文本 / `StreamEvent`） |
| 安全红线 | 强制 `--max-turns 1` + `--permission-mode dontAsk` + `--disallowedTools`（Bash/PowerShell/Write/Edit/ComputerUse/ImageGen/WebFetch/WebSearch/Skill/Agent…），**永不传 `-y`** | 桌宠只当聊天模型，不能让一句话就动本地文件 |
| 隔离 | 子进程 cwd 固定为临时目录、`CODEBUDDY_CODE_DISABLE_BACKGROUND_TASKS=1` | 不加载用户项目上下文、不跑后台任务 |
| 会话真源 | 历史/记忆仍以桌宠 `SessionStore`/`ChatMemory` 为准；CLI 只当模型 | 避免"两边各记一份"的双份真相 |
| HTTP 旁路 | 视觉识屏 / 截图翻译 / 记忆提炼自动回退到列表里第一个 HTTP provider（`ChatSettings.http_fallback`） | CLI 不提供 HTTP 端点，这些能力必须有兜底 |
| 联网检索 | CLI 模式下禁用并给出提示 | 项目的联网检索走 provider 的 HTTP 工具链，CLI 不支持 |

---

## 三、如何放进你自己的项目

### 适用前提

- 一份 **dsh-pet v4.4.0** 源码树（`pet/__init__.py` 的 `__version__ == '4.4.0'`）；
- Windows（本包在 Windows 上实测；CLI 桥本身只用标准库，但 `codebuddy` 的路径发现按 Windows 写成）；
- 本机装了 **WorkBuddy 客户端并已登录**（或任何带 `codebuddy` 的安装）。

### 方式 A：一键安装（推荐）

```bash
git clone <this-repo> workbuddy-cli-kit
cd workbuddy-cli-kit

# 先看看会改哪些文件
python install.py --target "D:/path/to/dsh-pet" --dry-run

# 确认后真正写入（会先把被覆盖的文件备份到目标下的 .workbuddy-cli-backup-<时间戳>/）
python install.py --target "D:/path/to/dsh-pet"
```

脚本会：

1. 校验目标是 dsh-pet 源码树；
2. 版本不是 `4.4.0` 时默认拒绝（确认风险可加 `--force`）；
3. 逐文件 SHA256 比对，已相同的跳过（可重复执行）；
4. 覆盖前备份。

### 方式 B：手工合并

`files/` 里是**改好的完整文件**，直接覆盖同名路径即可：

```text
files/pet/chat/cli/{__init__,bridge,provider}.py   ← 新增（核心实现）
files/pet/chat/models.py                            ← ProviderConfig.kind/cli_path/cli_model + ChatSettings.http_fallback
files/pet/chat/providers.py                         ← build_provider(config) 工厂
files/pet/chat/service.py                           ← send() 按 kind 分派
files/pet/chat/ai_settings_page.py                  ← 现代设置页：连接方式/CLI 两行 + 互斥显隐 + 检测
files/pet/chat/settings_dialog.py                   ← 托盘老窗：同上
files/pet/chat/memory_extract.py                    ← 记忆提炼回退 HTTP
files/pet/modern_settings_dialog.py                 ← 【关键】域装配 claim 表登记三行
files/pet/config.py                                 ← 默认 provider 增加 kind 默认值
files/pet/screen_capture/vision_bridge.py           ← 视觉/翻译回退
files/pet/window.py                                 ← 主动识屏回退
files/tests/{fake_cli,test_chat_cli_provider,test_chat_cli_config,test_api_provider_list}.py
```

> ⚠️ **最容易漏的一处**：现代设置窗不是直接渲染 `ai_settings_page` 里的 `SettingsSection`，
> 而是 `modern_settings_dialog._rebuild_domain_navigation()` **按行 key 认领后重新装配**。
> 只在 `ai_settings_page` 里建行、不在 `claim(...)` 里登记 → 行不会显示（我们踩过这个坑）。
> 详见 [`PORTING.md`](PORTING.md)。

### 方式 C：只拿核心实现

如果你不是 dsh-pet，只是想抄这套"把 CLI 当模型"的做法：`files/pet/chat/cli/` 三个文件是**零 Qt 依赖**的，
只依赖标准库（`subprocess`/`threading`/`queue`/`json`），可以单独拿走用。

---

## 四、配置与使用

### 配置键（都在 `config.json` 的 `chat.providers.<id>` 下）

| 键 | 默认 | 说明 |
|---|---|---|
| `kind` | `"http"` | `http` / `cli`；老配置缺这个键时按 `http` 处理 |
| `cli_path` | `""` | 留空自动发现：`~/.workbuddy/binaries/node/versions/*/codebuddy.cmd`（新版本优先）→ PATH |
| `cli_model` | `""` | 留空用 `hy4-preview`；常用 `deepseek-v4.1-flash` / `glm-5.3` / `kimi-k3.1-pro` |
| `chat.aux_provider` | `""` | 可选：指定 HTTP 旁路回退用哪个 provider |

### 界面操作

1. 右键桌宠 →「桌宠设置」→ 左侧「AI 与对话」（或托盘 →「AI 设置」）；
2. 「API 列表」→「添加」（**保留原来的 HTTP 那条**，视觉/翻译要靠它兜底）；
3. 「模型与连接」→「连接方式」选「本地 CLI（无需 API Key）」；
4. 「CLI 模型」填 `deepseek-v4.1-flash`，「CLI 路径」留空；
5. 点「检测 CLI」→ 显示版本号（不消耗积分）；
6. 「保存」→ 打开 AI 对话发一句话；
7. 想切回：把「连接方式」改回「OpenAI 兼容（HTTP）」，或在「API 列表」选回原来的那条。

### 排错

| 现象 | 原因 / 处理 |
|---|---|
| 「未检测到 CodeBuddy CLI」 | 自动发现失败：手填 `codebuddy.cmd` 绝对路径，或确认 WorkBuddy 已安装 |
| 「本地模型未返回任何内容」 | 模型 id 不对 / 额度不足：换 `deepseek-v4.1-flash` 重试 |
| 气泡里出现 `HTTP 401` 或 Key 相关报错 | 当前生效的不是 CLI 那条：回「API 列表」确认选中并保存 |
| 设置页看不到「连接方式」 | ①旧进程没重启（Python 不热更新）；②漏了 `modern_settings_dialog` 的 claim 登记 |
| 每条回复都很慢 | 见下面「已知限制」 |

---

## 五、实测数据（Windows 11 / PySide6 6.12 / codebuddy 2.161.4）

一次 `codebuddy -p "用一句话介绍你自己" --output-format stream-json --include-partial-messages`：

| 指标 | 数值 |
|---|---|
| 墙钟总时长 | **13.78 s**（首跑 16.86 s），其中 `result.duration_ms` 5.26 s → **≈8.5 s 是 Node 冷启动** |
| 事件结构 | 89 条 JSONL：83 `stream_event` + 2 `system/init` + 2 `assistant` + 1 `system/status` + 1 `result/success` |
| 文本增量 | 9 个 `text_delta`（可逐字）；64 个 `thinking_delta` |
| token | input 28 227（cache_creation 26 883 + cache_read 1 344）/ output 179 |

测试（在 dsh-pet v4.4.0 源码树上）：

```bash
python -m pytest tests/test_chat_cli_provider.py tests/test_chat_cli_config.py tests/test_api_provider_list.py -q
# 35 passed
python -m pytest tests/test_chat_service.py tests/test_settings_page_guide.py \
                 tests/test_settings_search_aliases.py tests/test_screen_capture_translate.py \
                 tests/test_vision.py tests/test_feature_modules_gating.py tests/test_architecture.py -q
# 相邻域全绿
python -m ruff check pet tests
# All checks passed!
```

---

## 六、已知限制（请先读再用）

1. **每条消息都会冷启动 CLI 进程**（≈8–15 s/条）。两个原因：
   - `ChatService.send()` 每次按 `kind` 新建 provider，桥也跟着重建；
   - CLI 进程的 `--system-prompt` 含"当前时间"，每分钟变化会触发重启。
   修法（尚未包含在本包里）：在 `ChatService` 缓存 provider 实例 + 进程只在人设变化时重启、
   把时间/记忆这类动态上下文并入本轮用户消息。改完预期：首条 8–13 s，之后 ~5 s。
2. **CLI 模式不支持内置联网检索**（会提示），需要联网就切回 HTTP provider。
3. **视觉/翻译/记忆提炼只能用 HTTP provider**，所以列表里要保留至少一条 `kind=http` 且填了 `base_url` 的。
4. CLI 的 JSONL 事件 schema 是官方客户端的**内部协议**（未对外承诺），CLI 升级可能漂移。
5. 本包按 **v4.4.0** 生成；其它版本请用 `--force` 前先自己 diff（`PORTING.md` 有逐文件改动说明）。

---

## 七、目录结构

```
workbuddy-cli-kit/
├── README.md
├── PORTING.md              # 逐文件「改了什么」，手工合并用
├── install.py              # 一键安装（版本守卫 / 哈希比对 / 备份 / --dry-run）
├── LICENSE                 # MIT（上游 Merzlin）
└── files/                  # 要覆盖进目标项目的完整文件
    ├── pet/chat/cli/       # 新增：CliBridge（子进程+协议）+ CliProvider（适配）
    ├── pet/…               # 修改：models/providers/service/config/两套设置页/旁路
    └── tests/              # 新增用例 + 装配回归用例
```

---

## 八、许可与致谢

- 本补丁包衍生自 [MerZlin/dsh-pet-indesktop](https://github.com/MerZlin/dsh-pet-indesktop)（**MIT**），
  `files/` 里的既有文件版权归原作者；新增的 `pet/chat/cli/*` 亦以 MIT 发布。
- 协议细节（`stream-json` 事件、`apiKeySource`）来自对 **CodeBuddy Code CLI 2.161.4** 的实测，
  不是官方 API 承诺；只应使用**本人或组织已授权**的账号与额度。
