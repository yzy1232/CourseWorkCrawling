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
from core import (
    get_courses,
    list_unsubmitted,
    run_download,
    submit_homework_files,
)

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
    p.add_argument(
        "--list-courses", action="store_true",
        help="列出我的全部课程及其 ID（id<=>课程名 映射），不下载",
    )
    p.add_argument(
        "--unsubmitted", action="store_true",
        help="只汇总该课程中尚未提交的作业，不下载",
    )
    p.add_argument(
        "--submit", metavar="HW_ID",
        help="向指定作业 ID 提交作业（配合 --files；这是写操作，需确认）",
    )
    p.add_argument(
        "--files", nargs="+", metavar="PATH",
        help="要提交的文件路径（可多个），配合 --submit 使用",
    )
    p.add_argument(
        "--comment", default="", help="提交作业时附带的说明文字（可选）",
    )
    p.add_argument(
        "--yes", action="store_true",
        help="提交作业时跳过交互确认（谨慎使用）",
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

    log = lambda m: print(_indent(m))

    # --- 列出课程：不需要 course_id ---
    if args.list_courses:
        try:
            courses = get_courses(username, password, log=log)
        except LoginError as exc:
            print(f"✗ {exc}")
            return 1
        except Exception as exc:
            print(f"✗ 失败: {exc}")
            return 1
        print(f"\n共 {len(courses)} 门课程：")
        for c in courses:
            print(f"  {c['id']}\t{c['name']}")
        return 0

    # --- 提交作业：写操作，需确认 ---
    if args.submit:
        if not args.files:
            print("✗ --submit 需要配合 --files 指定要提交的文件")
            return 2
        missing = [f for f in args.files if not os.path.isfile(f)]
        if missing:
            print(f"✗ 以下文件不存在：{', '.join(missing)}")
            return 2
        print("⚠ 即将向作业提交以下文件（此操作会真实改变服务器提交状态，不可自动撤销）：")
        print(f"  作业 ID: {args.submit}")
        for f in args.files:
            print(f"  - {f}")
        if not args.yes:
            try:
                ans = input("确认提交？输入 y 继续，其它键取消： ").strip().lower()
            except EOFError:
                ans = ""
            if ans != "y":
                print("已取消。")
                return 0
        try:
            result = submit_homework_files(
                username, password, args.submit, args.files,
                comment=args.comment, log=log,
            )
        except LoginError as exc:
            print(f"✗ {exc}")
            return 1
        except Exception as exc:
            print(f"✗ 提交失败: {exc}")
            return 1
        ups = result["uploads"]
        print(f"\n✓ 已提交 {len(ups)} 个附件到作业 {args.submit}")
        return 0

    if not course_id:
        print("✗ 缺少课程 ID，请用 --course 或在 .env 中设置 COURSE_ID")
        return 2

    # --- 未提交作业汇总 ---
    if args.unsubmitted:
        try:
            items = list_unsubmitted(username, password, course_id, log=log)
        except LoginError as exc:
            print(f"✗ {exc}")
            return 1
        except Exception as exc:
            print(f"✗ 失败: {exc}")
            return 1
        if not items:
            print("\n✓ 该课程没有未提交的作业")
            return 0
        print(f"\n尚未提交的作业（{len(items)} 个）：")
        for it in items:
            dl = it.get("deadline") or "无截止时间"
            print(f"  [{it['id']}] {it['title']}  截止: {dl}")
        return 0

    try:
        result = run_download(
            username,
            password,
            course_id,
            output_dir,
            download_submissions=not args.no_submissions,
            list_only=args.list_only,
            log=log,
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
