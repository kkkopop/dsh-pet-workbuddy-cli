# -*- coding: utf-8 -*-
"""本地 CodeBuddy CLI 子进程桥：长驻进程 + stream-json 事件。

不含任何 Qt 依赖，可在 ``ChatService`` 的 worker 线程安全调用。协议（实测 CLI 2.161.4）：

- 输入（stdin，JSONL）：``{"type":"user","message":{"role":"user","content":[{"type":"text","text":...}]}}``
- 输出（stdout，JSONL）：``system/init``、``stream_event``（``text_delta``/``thinking_delta``）、``result`` 等。

长驻模式：一个进程可多轮，取消即终止进程、下一轮重建（Windows 管道无 select，不能阻塞读超时）。
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Iterator

DEFAULT_MODEL = "hy4-preview"
IDLE_TIMEOUT = 600.0
PROBE_TIMEOUT = 20.0

# 桌宠只把 CLI 当聊天模型：禁掉一切可能动文件 / 执行命令 / 生成媒体 / 联网 / 团队 / 技能的工具。
DISALLOWED_TOOLS = (
    "Bash", "PowerShell", "Write", "Edit", "NotebookEdit", "ComputerUse",
    "ImageGen", "VideoGen", "AudioTranscribe", "Artifact", "ArtifactControl",
    "LibraryUpload", "Agent", "Workflow", "Monitor", "REPL", "LSP",
    "CronCreate", "CronDelete", "CronList",
    "WebFetch", "WebSearch",
    "Skill", "SkillManage",
    "TeamCreate", "TeamDelete", "CreateColleague", "CreateChannel",
    "SendMessage", "SendUserMessage", "MessageColleague", "SpeakInChannel", "ChannelTasks",
    "WeChatReply", "WeComReply", "PushNotification",
    "TaskCreate", "TaskUpdate", "TaskList", "TaskGet", "TaskStop", "TaskOutput",
    "StructuredOutput", "A2AGetAgentCard", "A2ASendMessage", "A2AGetTask", "A2ACancelTask",
    "EnterWorktree", "LeaveWorktree", "EnterPlanMode", "ExitPlanMode",
    "AskUserQuestion", "AskUserForStructuredInput", "ReportFindings",
    "ListMcpResources", "ReadMcpResource", "WaitForMcpServers",
    "ToolSearch", "DeferExecuteTool",
)


class CliBridgeError(RuntimeError):
    """CLI 桥的错误（启动失败、写失败、result.is_error、进程意外退出）。"""


def build_argv(executable, model=DEFAULT_MODEL, system_prompt="",
               disallowed_tools=DISALLOWED_TOOLS, max_turns=1) -> list[str]:
    """拼出 ``codebuddy`` 子进程的完整命令行。

    ``executable`` 可为路径字符串，或 ``[解释器, 脚本路径]``（测试桩用）。
    """
    if isinstance(executable, (list, tuple)):
        argv = [str(x) for x in executable]
    else:
        argv = [str(executable)]
    argv += [
        "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--max-turns", str(max_turns),
        "--permission-mode", "dontAsk",
        "--model", str(model),
        "--system-prompt", str(system_prompt or ""),
        "--disallowedTools", " ".join(str(t) for t in disallowed_tools),
    ]
    return argv


def map_event(obj):
    """把一条 JSONL 事件映射为 ``(kind, text)``；无关事件返回 ``None``。

    kind ∈ ``text`` / ``reasoning`` / ``status`` / ``result``。
    """
    if not isinstance(obj, dict):
        return None
    t = obj.get("type")
    if t == "stream_event":
        ev = obj.get("event") or {}
        delta = ev.get("delta") if isinstance(ev, dict) else None
        if not isinstance(delta, dict):
            return None
        if delta.get("type") == "text_delta":
            text = delta.get("text")
            return ("text", text) if isinstance(text, str) else None
        if delta.get("type") == "thinking_delta":
            thinking = delta.get("thinking")
            return ("reasoning", thinking) if isinstance(thinking, str) else None
        return None
    if t == "system" and obj.get("subtype") == "status":
        status = obj.get("status")
        return ("status", status) if isinstance(status, str) and status else None
    if t == "result":
        return ("result", None)
    return None


def _result_error_message(obj: dict) -> str:
    msg = obj.get("result")
    if isinstance(msg, str) and msg.strip():
        return msg
    subtype = obj.get("subtype")
    if isinstance(subtype, str) and subtype.strip():
        return subtype
    return "CLI 返回错误"


_VERSION_RE = re.compile(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?")


def _version_key(name: str):
    m = _VERSION_RE.match(name)
    if not m:
        return (-1, -1, -1)
    return tuple(int(g) if g else 0 for g in m.groups())


def discover_cli(explicit=None, home=None) -> str | None:
    """按优先级找 CLI：显式路径 → ``~/.workbuddy`` 自带版本目录（新版本优先）→ PATH。"""
    if explicit:
        return explicit if Path(explicit).is_file() else None
    base = Path(home) if home is not None else Path.home()
    versions_dir = base / ".workbuddy" / "binaries" / "node" / "versions"
    if versions_dir.is_dir():
        candidates = []
        for entry in versions_dir.iterdir():
            if not entry.is_dir():
                continue
            for name in ("codebuddy.cmd", "codebuddy"):
                p = entry / name
                if p.is_file():
                    candidates.append((_version_key(entry.name), str(p)))
        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            return candidates[0][1]
    for name in ("codebuddy", "cbc"):
        found = shutil.which(name)
        if found:
            return found
    return None


class CliBridge:
    """长驻 ``codebuddy`` 子进程 + stream-json 事件桥；单轮在飞，取消即终止、下一轮重建。"""

    def __init__(self, executable, model=DEFAULT_MODEL, disallowed_tools=DISALLOWED_TOOLS,
                 max_turns=1, cwd=None, env=None, idle_timeout=IDLE_TIMEOUT):
        self._executable = list(executable) if isinstance(executable, (list, tuple)) else str(executable)
        self._model = model
        self._disallowed_tools = tuple(disallowed_tools)
        self._max_turns = max_turns
        self._cwd = cwd or tempfile.gettempdir()
        self._env = dict(env or {})
        self._idle_timeout = idle_timeout
        self._lock = threading.Lock()
        self._proc = None
        self._reader = None
        self._system_text = None
        self._queue = None
        self._eof = False
        self._last_use = time.monotonic()

    # ------------------------------------------------------------ lifecycle
    def _spawn(self, system_text: str) -> None:
        argv = build_argv(self._executable, self._model, system_text,
                          self._disallowed_tools, self._max_turns)
        env = dict(os.environ)
        env["CODEBUDDY_CODE_DISABLE_BACKGROUND_TASKS"] = "1"
        env.update(self._env)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        self._proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            cwd=self._cwd, env=env, creationflags=flags,
        )
        self._system_text = system_text
        self._eof = False
        self._queue = queue.Queue()
        self._reader = threading.Thread(target=self._read_loop, args=(self._proc,), daemon=True)
        self._reader.start()

    def _read_loop(self, proc) -> None:
        try:
            for line in proc.stdout:
                self._queue.put(line)
        finally:
            self._eof = True

    def _terminate(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        for closer in (proc.stdin.close, proc.terminate):
            try:
                closer()
            except Exception:
                pass
        try:
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _ensure(self, system_text: str):
        with self._lock:
            if self._proc is None or self._proc.poll() is not None or system_text != self._system_text:
                self._terminate()
                self._spawn(system_text)
            self._last_use = time.monotonic()
            return self._proc

    def close(self) -> None:
        with self._lock:
            self._terminate()

    # ------------------------------------------------------------ send
    def send(self, user_text: str, system_text: str, cancel: threading.Event) -> Iterator[tuple[str, str]]:
        """发送一轮并产出 ``(kind, text)``；kind ∈ text/reasoning/status。出错抛 ``CliBridgeError``。"""
        proc = self._ensure(system_text)
        payload = {"type": "user", "message": {"role": "user",
                  "content": [{"type": "text", "text": user_text}]}}
        try:
            proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            proc.stdin.flush()
        except OSError as exc:
            self._terminate()
            raise CliBridgeError(f"无法写入 CLI 进程：{exc}") from exc

        saw_result = False
        while True:
            if cancel.is_set():
                self._terminate()
                return
            try:
                line = self._queue.get(timeout=0.05)
            except queue.Empty:
                if self._eof and self._queue.empty():
                    break
                continue
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            mapped = map_event(obj)
            if mapped is None:
                continue
            kind, text = mapped
            if kind == "result":
                saw_result = True
                if obj.get("is_error"):
                    raise CliBridgeError(_result_error_message(obj))
                return
            yield (kind, text)
        if not saw_result and not cancel.is_set():
            raise CliBridgeError("CLI 进程意外退出")

    # ------------------------------------------------------------ probe
    def probe(self) -> tuple[bool, str]:
        """只读检测：可执行性 + 版本（不发起会消耗额度的推理）。"""
        exe = self._executable[0] if isinstance(self._executable, list) else self._executable
        try:
            proc = subprocess.run([exe, "--version"], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  timeout=PROBE_TIMEOUT, cwd=self._cwd)
        except FileNotFoundError:
            return False, "未检测到 CodeBuddy CLI（路径不存在或无执行权限）"
        except subprocess.TimeoutExpired:
            return False, "CodeBuddy CLI 无响应（--version 超时）"
        except OSError as exc:
            return False, f"无法启动 CodeBuddy CLI：{exc}"
        out = (proc.stdout or "").strip() or (proc.stderr or "").strip()
        if proc.returncode == 0:
            return True, out or "CodeBuddy CLI 可用"
        return False, out or f"CodeBuddy CLI 退出码 {proc.returncode}"
