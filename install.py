# -*- coding: utf-8 -*-
"""把本移植包的文件装进一份 dsh-pet 源码树。

用法（在目标项目根目录执行，或显式给 --target）：

    python install.py --target "D:/path/to/dsh-pet"
    python install.py --target "D:/path/to/dsh-pet" --dry-run     # 只看会改哪些文件
    python install.py --target "D:/path/to/dsh-pet" --force       # 版本号不匹配也继续

行为：
- 逐文件比较 SHA256；已相同的文件跳过（可重复执行）。
- 覆盖前把原文件备份到 <目标>/.workbuddy-cli-backup-<时间戳>/（可用 --no-backup 关闭）。
- 目标版本不是 4.4.0 时默认拒绝（本包按 v4.4.0 生成），确认风险后再加 --force。
"""
from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import sys
import time
from pathlib import Path

PACK_ROOT = Path(__file__).resolve().parent
FILES_ROOT = PACK_ROOT / "files"
EXPECTED_VERSION = "4.4.0"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_version(target: Path) -> str:
    init = target / "pet" / "__init__.py"
    if not init.is_file():
        return ""
    m = re.search(r"__version__\s*=\s*['\"]([^'\"]+)['\"]", init.read_text(encoding="utf-8", errors="replace"))
    return m.group(1) if m else ""


def main() -> int:
    ap = argparse.ArgumentParser(description="安装 WorkBuddy CLI 聊天 provider 到 dsh-pet 源码树")
    ap.add_argument("--target", default=".", help="目标项目根目录（含 pet/ 的那一层）")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写盘")
    ap.add_argument("--force", action="store_true", help="目标版本不是 4.4.0 也继续")
    ap.add_argument("--no-backup", action="store_true", help="不备份被覆盖的文件")
    args = ap.parse_args()

    target = Path(args.target).expanduser().resolve()
    if not (target / "pet" / "__init__.py").is_file():
        print(f"[x] 目标不像 dsh-pet 源码树（缺 pet/__init__.py）：{target}")
        return 2

    version = read_version(target)
    if version != EXPECTED_VERSION and not args.force:
        print(f"[x] 目标版本是 {version or '未知'}，本包按 v{EXPECTED_VERSION} 生成。")
        print("    确认要覆盖请加 --force（建议先备份整份项目）。")
        return 3

    mapfiles = sorted(p for p in FILES_ROOT.rglob("*") if p.is_file())
    if not mapfiles:
        print("[x] 包内 files/ 为空，无法安装")
        return 2

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_root = target / f".workbuddy-cli-backup-{stamp}"
    plan: list[tuple[Path, Path, str]] = []  # (包内文件, 目标文件, 动作)
    for src in mapfiles:
        rel = src.relative_to(FILES_ROOT)
        dst = target / rel
        if dst.is_file() and sha256(dst) == sha256(src):
            plan.append((src, dst, "same"))
        elif dst.is_file():
            plan.append((src, dst, "overwrite"))
        else:
            plan.append((src, dst, "new"))

    print(f"目标: {target}   版本: {version or '未知'}")
    for _src, dst, action in plan:
        print(f"  {action:9s} {dst.relative_to(target)}")

    counts = {k: sum(1 for _s, _d, a in plan if a == k) for k in ("new", "overwrite", "same")}
    print(f"\n合计: 新增 {counts['new']} / 覆盖 {counts['overwrite']} / 已相同 {counts['same']}")
    if args.dry_run:
        print("（--dry-run：未写入任何文件）")
        return 0
    if counts["new"] == 0 and counts["overwrite"] == 0:
        print("已经是最新，无需改动。")
        return 0

    for src, dst, action in plan:
        if action == "same":
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if action == "overwrite" and not args.no_backup:
            bak = backup_root / dst.relative_to(target)
            bak.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, bak)
        shutil.copy2(src, dst)

    if counts["overwrite"] and not args.no_backup and backup_root.is_dir():
        print(f"\n完成。被覆盖的原文件已备份到: {backup_root}")
    else:
        print("\n完成。（本次没有覆盖任何既有文件）")
    print("下一步：python -m pytest tests/test_chat_cli_provider.py tests/test_chat_cli_config.py -q")
    return 0


if __name__ == "__main__":
    sys.exit(main())
