"""学在城院 (TronClass) 课程作业附件批量下载工具 —— 命令行入口。

用法：
    1. 复制 .env.example 为 .env，填入账号密码和课程 ID
    2. pip install -r requirements.txt
    3. python main.py
       或覆盖参数： python main.py --course 53472 --output downloads
       图形界面：   python gui.py

按作业分文件夹保存题目附件与我的提交，并生成 manifest.json 清单。
"""

from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv

from auth import LoginError
from core import run_download

# Windows 默认 GBK 控制台无法输出 ✓ 等字符，统一切到 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="学在城院课程作业附件批量下载")
    p.add_argument("--course", help="课程 ID（默认读取 .env 的 COURSE_ID）")
    p.add_argument("--output", help="下载根目录（默认读取 .env 的 OUTPUT_DIR）")
    p.add_argument(
        "--no-submissions", action="store_true", help="不下载我的提交，只下题目附件"
    )
    p.add_argument(
        "--list-only", action="store_true", help="只列出作业与附件，不实际下载"
    )
    return p.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()

    username = os.getenv("HZCU_USERNAME")
    password = os.getenv("HZCU_PASSWORD")
    course_id = args.course or os.getenv("COURSE_ID")
    output_dir = args.output or os.getenv("OUTPUT_DIR") or "downloads"

    if not username or not password:
        print("✗ 缺少账号密码，请在 .env 中设置 HZCU_USERNAME / HZCU_PASSWORD")
        return 2
    if not course_id:
        print("✗ 缺少课程 ID，请用 --course 或在 .env 中设置 COURSE_ID")
        return 2

    try:
        result = run_download(
            username,
            password,
            course_id,
            output_dir,
            download_submissions=not args.no_submissions,
            list_only=args.list_only,
            log=lambda m: print(_indent(m)),
        )
    except LoginError as exc:
        print(f"✗ {exc}")
        return 1
    except Exception as exc:
        print(f"✗ 失败: {exc}")
        return 1

    hw = result["homeworks"]
    total_files = sum(
        len(r["assignment"]) + len(r["submission"]) for r in hw
    )
    print(
        f"\n✓ 完成：{len(hw)} 个作业，{total_files} 个附件"
        f"{'（仅列表，未下载）' if args.list_only else ''}"
    )
    if result.get("manifest"):
        print(f"  清单: {result['manifest']}")
    if result.get("output_root") and not args.list_only:
        print(f"  文件: {result['output_root']}")
    return 0


def _indent(msg: str) -> str:
    """顶层步骤不缩进，带前导空格的子步骤保持原样。"""
    return msg if msg.startswith(" ") else f"→ {msg}"


if __name__ == "__main__":
    sys.exit(main())
