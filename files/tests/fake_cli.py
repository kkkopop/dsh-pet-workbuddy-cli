# -*- coding: utf-8 -*-
"""假 CLI 桩：模拟 `codebuddy --input-format stream-json --output-format stream-json`。

只用于测试 `pet/chat/cli` 的子进程桥，不联网、不消耗任何额度。行为由环境变量控制：

- FAKE_CLI_HANG=1   收到 user 消息后先发一条 thinking 增量，然后长时间不结束（测取消）
- FAKE_CLI_ERROR=1  直接发 result 且 is_error=True（测错误映射）
- FAKE_CLI_EMPTY=1  只发 result 成功、不发任何 text 增量（测空回复）
- 默认              发 thinking 增量 + text 增量「收到」+ result 成功
"""
from __future__ import annotations

import json
import os
import sys
import time


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def stream_delta(delta_type: str, **extra) -> dict:
    delta = {"type": delta_type}
    delta.update(extra)
    return {
        "type": "stream_event",
        "event": {"type": "content_block_delta", "index": 0, "delta": delta},
        "session_id": "s-fake",
    }


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # 强制 UTF-8，避免 Windows 默认 GBK 污染 JSONL
    emit({"type": "system", "subtype": "init", "session_id": "s-fake",
          "apiKeySource": "copilot.tencent.com", "model": "fake"})
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        # 只关心 user 消息，其余行忽略
        if msg.get("type") != "user":
            continue
        if os.environ.get("FAKE_CLI_HANG") == "1":
            emit(stream_delta("thinking_delta", thinking="思考中…"))
            time.sleep(30)
            continue
        if os.environ.get("FAKE_CLI_ERROR") == "1":
            emit({"type": "result", "subtype": "error_during_execution",
                  "is_error": True, "result": "boom"})
            continue
        if os.environ.get("FAKE_CLI_EMPTY") == "1":
            emit({"type": "result", "subtype": "success", "is_error": False})
            continue
        emit(stream_delta("thinking_delta", thinking="让我想想"))
        emit(stream_delta("text_delta", text="收到"))
        emit({"type": "result", "subtype": "success", "is_error": False})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
