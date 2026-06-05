"""学在城院作业附件下载器 —— 图形界面 (Tkinter)。

运行： python gui.py

界面分四个页签：
  · 下载：填账号/密码/课程并设置下载选项
  · 作业：表格展示作业列表、作业正文、我的提交详情，并可提交作业
  · 课件：表格展示课程资料，可一键下载（不可下载的课件自动转 PDF 兜底）
  · 下载任务：任务列表 + 暂停/继续 + 并行数控制

课程、作业、课件列表均带本地缓存，离线可看；超过 3 天会提示刷新。
"""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from dotenv import load_dotenv

import cache
from core import (
    authenticate,
    coursewares_with_cache,
    courses_with_cache,
    homeworks_with_cache,
    prepare_courseware_download,
    prepare_download,
    submit_homework_files,
)
from download_manager import DownloadManager, DownloadTask


def _fmt_bytes(n: int) -> str:
    if not n:
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("学在城院 作业附件下载器")
        self.geometry("860x640")
        self.minsize(760, 560)

        load_dotenv()
        self._log_queue: "queue.Queue[str]" = queue.Queue()
        self._ui_queue: "queue.Queue" = queue.Queue()   # 跨线程 UI 回调
        self._worker: threading.Thread | None = None

        # 共享状态
        self.var_user = tk.StringVar(value=os.getenv("HZCU_USERNAME", ""))
        self.var_pass = tk.StringVar(value=os.getenv("HZCU_PASSWORD", ""))
        self.var_course = tk.StringVar(value=os.getenv("COURSE_ID", ""))
        self.var_output = tk.StringVar(value=os.getenv("OUTPUT_DIR", "downloads"))
        self.var_submissions = tk.BooleanVar(value=True)
        self.var_list_only = tk.BooleanVar(value=False)
        self.var_parallel = tk.IntVar(value=4)
        self.var_hw = tk.StringVar(value="")
        self.var_comment = tk.StringVar(value="")
        self.var_show = tk.BooleanVar(value=False)

        self._courses: list[dict] = []
        self._records: list[dict] = []          # 当前作业概览
        self._cw_records: list[dict] = []        # 当前课件概览
        self._submit_files: list[str] = []
        self._manager: DownloadManager | None = None
        self._task_rows: dict[int, str] = {}     # task.id -> treeview item id
        self._tasks: list[DownloadTask] = []
        self._last_output: str | None = None

        self._build_widgets()
        self._poll_log_queue()
        self._poll_ui_queue()

    # ---------- UI 构建 ----------
    def _build_widgets(self) -> None:
        self._build_shared_top()
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=8, pady=4)
        self._build_tab_download()
        self._build_tab_homeworks()
        self._build_tab_coursewares()
        self._build_tab_tasks()
        self._build_log()

    def _build_shared_top(self) -> None:
        pad = {"padx": 8, "pady": 4}
        form = ttk.Frame(self)
        form.pack(fill="x", **pad)
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="学号/账号").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(form, textvariable=self.var_user).grid(
            row=0, column=1, columnspan=2, sticky="ew", **pad
        )

        ttk.Label(form, text="密码").grid(row=1, column=0, sticky="w", **pad)
        self.entry_pass = ttk.Entry(form, textvariable=self.var_pass, show="•")
        self.entry_pass.grid(row=1, column=1, sticky="ew", **pad)
        ttk.Checkbutton(form, text="显示", variable=self.var_show,
                        command=self._toggle_pw).grid(row=1, column=2, sticky="w", **pad)

        ttk.Label(form, text="课程").grid(row=2, column=0, sticky="w", **pad)
        self.combo_course = ttk.Combobox(form, textvariable=self.var_course, state="normal")
        self.combo_course.grid(row=2, column=1, sticky="ew", **pad)
        self.combo_course.bind("<<ComboboxSelected>>", self._on_course_pick)
        ttk.Button(form, text="刷新课程", command=self._on_list_courses).grid(
            row=2, column=2, sticky="w", **pad
        )
        self.lbl_course_cache = ttk.Label(form, text="", foreground="gray")
        self.lbl_course_cache.grid(row=3, column=1, columnspan=2, sticky="w", padx=8)
        self._load_cached_courses()

    # ---------- 页签：下载 ----------
    def _build_tab_download(self) -> None:
        pad = {"padx": 8, "pady": 4}
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="下载")
        tab.columnconfigure(1, weight=1)

        ttk.Label(tab, text="保存目录").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(tab, textvariable=self.var_output).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(tab, text="浏览…", command=self._choose_dir).grid(row=0, column=2, sticky="w", **pad)

        opts = ttk.Frame(tab)
        opts.grid(row=1, column=0, columnspan=3, sticky="w", **pad)
        ttk.Checkbutton(opts, text="同时下载我的提交", variable=self.var_submissions).pack(side="left", padx=8)
        ttk.Checkbutton(opts, text="仅列出不下载", variable=self.var_list_only).pack(side="left", padx=8)
        ttk.Label(opts, text="并行数").pack(side="left", padx=(16, 2))
        ttk.Spinbox(opts, from_=1, to=8, width=4, textvariable=self.var_parallel).pack(side="left")

        bar = ttk.Frame(tab)
        bar.grid(row=2, column=0, columnspan=3, sticky="ew", **pad)
        self.btn_start = ttk.Button(bar, text="开始下载", command=self._on_start)
        self.btn_start.pack(side="left", padx=8)
        self.btn_open = ttk.Button(bar, text="打开下载目录", command=self._open_output, state="disabled")
        self.btn_open.pack(side="left", padx=8)
        self.progress = ttk.Progressbar(bar, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=8)

        ttk.Label(tab, text="说明：开始下载后，可在「下载任务」页签查看进度并暂停/继续。",
                  foreground="gray").grid(row=3, column=0, columnspan=3, sticky="w", **pad)

    # ---------- 页签：作业 ----------
    def _build_tab_homeworks(self) -> None:
        pad = {"padx": 6, "pady": 3}
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="作业")
        tab.rowconfigure(2, weight=1)
        tab.columnconfigure(0, weight=1)

        top = ttk.Frame(tab)
        top.grid(row=0, column=0, sticky="ew", **pad)
        ttk.Button(top, text="刷新作业", command=self._on_refresh_homeworks).pack(side="left", padx=4)
        self.lbl_hw_cache = ttk.Label(top, text="", foreground="gray")
        self.lbl_hw_cache.pack(side="left", padx=8)

        cols = ("title", "submitted", "deadline", "score", "n_assign", "n_submit")
        heads = {"title": "标题", "submitted": "已提交", "deadline": "截止",
                 "score": "分数", "n_assign": "题目附件", "n_submit": "提交数"}
        widths = {"title": 280, "submitted": 60, "deadline": 130,
                  "score": 60, "n_assign": 70, "n_submit": 60}
        self.tree_hw = ttk.Treeview(tab, columns=cols, show="headings", height=8)
        for c in cols:
            self.tree_hw.heading(c, text=heads[c])
            self.tree_hw.column(c, width=widths[c], anchor="w")
        self.tree_hw.grid(row=1, column=0, sticky="nsew", **pad)
        self.tree_hw.bind("<<TreeviewSelect>>", self._on_hw_select)

        detail = ttk.Frame(tab)
        detail.grid(row=2, column=0, sticky="nsew", **pad)
        detail.columnconfigure(0, weight=2)
        detail.columnconfigure(1, weight=1)
        detail.rowconfigure(1, weight=1)

        ttk.Label(detail, text="作业正文").grid(row=0, column=0, sticky="w")
        self.txt_desc = tk.Text(detail, wrap="word", height=8, state="disabled")
        self.txt_desc.grid(row=1, column=0, sticky="nsew", padx=(0, 6))
        ttk.Label(detail, text="我的提交").grid(row=0, column=1, sticky="w")
        self.txt_sub = tk.Text(detail, wrap="word", height=8, state="disabled")
        self.txt_sub.grid(row=1, column=1, sticky="nsew")

        # 提交作业面板
        sub = ttk.LabelFrame(tab, text="提交作业（写操作，会真实改变服务器状态）")
        sub.grid(row=3, column=0, sticky="ew", **pad)
        sub.columnconfigure(1, weight=1)
        ttk.Label(sub, text="作业 ID").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(sub, textvariable=self.var_hw, width=14).grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(sub, text="说明").grid(row=0, column=2, sticky="w", **pad)
        ttk.Entry(sub, textvariable=self.var_comment).grid(row=0, column=3, sticky="ew", **pad)
        sub.columnconfigure(3, weight=1)
        ttk.Button(sub, text="选择文件…", command=self._choose_submit_files).grid(row=1, column=0, sticky="w", **pad)
        self.lbl_files = ttk.Label(sub, text="未选择文件", foreground="gray")
        self.lbl_files.grid(row=1, column=1, columnspan=2, sticky="w", **pad)
        self.btn_submit = ttk.Button(sub, text="提交", command=self._on_submit)
        self.btn_submit.grid(row=1, column=3, sticky="e", **pad)

    # ---------- 页签：课件 ----------
    def _build_tab_coursewares(self) -> None:
        pad = {"padx": 6, "pady": 3}
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="课件")
        tab.rowconfigure(1, weight=1)
        tab.columnconfigure(0, weight=1)

        top = ttk.Frame(tab)
        top.grid(row=0, column=0, sticky="ew", **pad)
        ttk.Button(top, text="刷新课件", command=self._on_refresh_coursewares).pack(side="left", padx=4)
        ttk.Button(top, text="下载全部课件", command=self._on_download_coursewares).pack(side="left", padx=4)
        ttk.Button(top, text="下载选中课件", command=self._on_download_selected_coursewares).pack(side="left", padx=4)
        ttk.Button(top, text="全选", command=self._select_all_coursewares).pack(side="left", padx=4)
        ttk.Button(top, text="清空选择", command=self._clear_courseware_selection).pack(side="left", padx=4)
        self.lbl_cw_cache = ttk.Label(top, text="", foreground="gray")
        self.lbl_cw_cache.pack(side="left", padx=8)

        cols = ("title", "n_files", "downloadable")
        heads = {"title": "课件", "n_files": "附件数", "downloadable": "可下载"}
        widths = {"title": 420, "n_files": 80, "downloadable": 100}
        self.tree_cw = ttk.Treeview(tab, columns=cols, show="headings", height=10, selectmode="extended")
        for c in cols:
            self.tree_cw.heading(c, text=heads[c])
            self.tree_cw.column(c, width=widths[c], anchor="w")
        self.tree_cw.grid(row=1, column=0, sticky="nsew", **pad)
        cs = ttk.Scrollbar(tab, orient="vertical", command=self.tree_cw.yview)
        self.tree_cw.configure(yscrollcommand=cs.set)
        cs.grid(row=1, column=1, sticky="ns")

        ttk.Label(
            tab,
            text="说明：「不可下载」的课件会自动以 PDF 兜底方式获取（走转码接口）。",
            foreground="gray",
        ).grid(row=2, column=0, sticky="w", **pad)

    # ---------- 页签：下载任务 ----------
    def _build_tab_tasks(self) -> None:
        pad = {"padx": 6, "pady": 3}
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="下载任务")
        tab.rowconfigure(1, weight=1)
        tab.columnconfigure(0, weight=1)

        bar = ttk.Frame(tab)
        bar.grid(row=0, column=0, sticky="ew", **pad)
        self.btn_pause = ttk.Button(bar, text="暂停", command=self._on_pause, state="disabled")
        self.btn_pause.pack(side="left", padx=4)
        self.btn_resume = ttk.Button(bar, text="继续", command=self._on_resume, state="disabled")
        self.btn_resume.pack(side="left", padx=4)
        ttk.Label(bar, text="并行数").pack(side="left", padx=(16, 2))
        ttk.Spinbox(bar, from_=1, to=8, width=4, textvariable=self.var_parallel).pack(side="left")
        self.lbl_task_summary = ttk.Label(bar, text="", foreground="gray")
        self.lbl_task_summary.pack(side="left", padx=12)

        cols = ("hw", "kind", "name", "status", "progress")
        heads = {"hw": "作业", "kind": "类型", "name": "文件名",
                 "status": "状态", "progress": "进度"}
        widths = {"hw": 200, "kind": 70, "name": 240, "status": 80, "progress": 140}
        self.tree_task = ttk.Treeview(tab, columns=cols, show="headings")
        for c in cols:
            self.tree_task.heading(c, text=heads[c])
            self.tree_task.column(c, width=widths[c], anchor="w")
        self.tree_task.grid(row=1, column=0, sticky="nsew", **pad)
        ts = ttk.Scrollbar(tab, orient="vertical", command=self.tree_task.yview)
        self.tree_task.configure(yscrollcommand=ts.set)
        ts.grid(row=1, column=1, sticky="ns")

    def _build_log(self) -> None:
        self.log = tk.Text(self, height=8, wrap="word", state="disabled")
        self.log.pack(fill="both", expand=False, padx=8, pady=(0, 8))
        scroll = ttk.Scrollbar(self.log, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")

    # ---------- 通用交互 ----------
    def _toggle_pw(self) -> None:
        self.entry_pass.configure(show="" if self.var_show.get() else "•")

    def _choose_dir(self) -> None:
        d = filedialog.askdirectory(initialdir=self.var_output.get() or ".")
        if d:
            self.var_output.set(d)

    def _busy(self) -> bool:
        return bool(self._worker and self._worker.is_alive())

    def _start_worker(self, target, *args) -> None:
        if self._busy():
            messagebox.showinfo("请稍候", "已有任务正在运行")
            return
        self._worker = threading.Thread(target=target, args=args, daemon=True)
        self._worker.start()

    def _creds(self, need_course: bool = False) -> tuple[str, str, str] | None:
        user = self.var_user.get().strip()
        pw = self.var_pass.get()
        course = self.var_course.get().strip()
        if not user or not pw:
            messagebox.showerror("缺少信息", "请填写账号和密码")
            return None
        if need_course and not course:
            messagebox.showerror("缺少信息", "请填写或选择课程")
            return None
        return user, pw, course

    # ---------- 课程 ----------
    def _on_course_pick(self, _event=None) -> None:
        idx = self.combo_course.current()
        if 0 <= idx < len(self._courses):
            self.var_course.set(str(self._courses[idx]["id"]))
        self._try_load_cached_homeworks()
        self._try_load_cached_coursewares()

    def _set_courses(self, courses: list[dict], cached_at, stale: bool) -> None:
        self._courses = courses
        self.combo_course["values"] = [f"{c['id']}  {c['name']}" for c in courses]
        self.lbl_course_cache.configure(**_cache_label(cached_at, stale))

    def _load_cached_courses(self) -> None:
        user = self.var_user.get().strip()
        if not user:
            return
        data, cached_at = cache.load("courses", user)
        if data:
            self._set_courses(data, cached_at, cache.is_stale(cached_at))

    def _on_list_courses(self) -> None:
        c = self._creds()
        if not c:
            return
        user, pw, _ = c
        self._start_worker(self._job_list_courses, user, pw)

    def _job_list_courses(self, user: str, pw: str) -> None:
        try:
            courses, cached_at, stale = courses_with_cache(
                user, pw, refresh=True, log=self._log
            )
            self._ui(lambda: self._set_courses(courses, cached_at, stale))
            self._log(f"✓ 已载入 {len(courses)} 门课程")
        except Exception as exc:
            err = str(exc)
            self._log(f"\n✗ 获取课程失败：{err}")
            self._ui(lambda err=err: messagebox.showerror("获取课程失败", err))

    # ---------- 作业概览 ----------
    def _on_refresh_homeworks(self) -> None:
        c = self._creds(need_course=True)
        if not c:
            return
        user, pw, course = c
        self._start_worker(self._job_homeworks, user, pw, course, True)

    def _try_load_cached_homeworks(self) -> None:
        course = self.var_course.get().strip()
        if not course:
            return
        data, cached_at = cache.load("homeworks", course)
        if data:
            self._ui(lambda: self._fill_homeworks(data, cached_at, cache.is_stale(cached_at)))

    def _job_homeworks(self, user, pw, course, refresh) -> None:
        try:
            records, cached_at, stale = homeworks_with_cache(
                user, pw, course, refresh=refresh,
                download_submissions=self.var_submissions.get(), log=self._log,
            )
            self._ui(lambda: self._fill_homeworks(records, cached_at, stale))
            self._log(f"✓ 作业列表已更新（{len(records)} 个）")
        except Exception as exc:
            err = str(exc)
            self._log(f"\n✗ 获取作业失败：{err}")
            self._ui(lambda err=err: messagebox.showerror("获取作业失败", err))

    def _fill_homeworks(self, records, cached_at, stale) -> None:
        self._records = records
        self.tree_hw.delete(*self.tree_hw.get_children())
        for i, r in enumerate(records):
            st = r.get("status") or {}
            info = r.get("submission_info") or {}
            self.tree_hw.insert("", "end", iid=str(i), values=(
                r["title"],
                "是" if st.get("submitted") else "否",
                st.get("deadline") or "-",
                info.get("score") or st.get("score") or "-",
                len(r.get("assignment") or []),
                len(r.get("submission") or []),
            ))
        self.lbl_hw_cache.configure(**_cache_label(cached_at, stale))

    def _on_hw_select(self, _event=None) -> None:
        sel = self.tree_hw.selection()
        if not sel:
            return
        idx = int(sel[0])
        if not (0 <= idx < len(self._records)):
            return
        r = self._records[idx]
        self.var_hw.set(str(r["id"]))
        self._set_text(self.txt_desc, r.get("description") or "（无正文）")

        info = r.get("submission_info")
        if not info:
            self._set_text(self.txt_sub, "（尚未提交或无提交信息）")
            return
        lines = []
        if info.get("submitted_at"):
            lines.append(f"提交时间：{info['submitted_at']}")
        if info.get("score") not in (None, ""):
            lines.append(f"分数：{info['score']}")
        if info.get("score_status"):
            lines.append(f"批阅状态：{info['score_status']}")
        if info.get("comment"):
            lines.append(f"说明：{info['comment']}")
        if info.get("files"):
            lines.append("提交文件：")
            lines += [f"  • {n}" for n in info["files"]]
        self._set_text(self.txt_sub, "\n".join(lines) or "（无提交详情）")

    def _set_text(self, widget: tk.Text, content: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", content)
        widget.configure(state="disabled")

    # ---------- 课件 ----------
    def _on_refresh_coursewares(self) -> None:
        c = self._creds(need_course=True)
        if not c:
            return
        user, pw, course = c
        self._start_worker(self._job_coursewares, user, pw, course, True)

    def _try_load_cached_coursewares(self) -> None:
        course = self.var_course.get().strip()
        if not course:
            return
        data, cached_at = cache.load("coursewares", course)
        if data:
            self._ui(lambda: self._fill_coursewares(
                data, cached_at, cache.is_stale(cached_at)))

    def _job_coursewares(self, user, pw, course, refresh) -> None:
        try:
            records, cached_at, stale = coursewares_with_cache(
                user, pw, course, refresh=refresh, log=self._log,
            )
            self._ui(lambda: self._fill_coursewares(records, cached_at, stale))
            self._log(f"✓ 课件列表已更新（{len(records)} 个）")
        except Exception as exc:
            err = str(exc)
            self._log(f"\n✗ 获取课件失败：{err}")
            self._ui(lambda err=err: messagebox.showerror("获取课件失败", err))

    def _fill_coursewares(self, records, cached_at, stale) -> None:
        self._cw_records = records
        self.tree_cw.delete(*self.tree_cw.get_children())
        for i, r in enumerate(records):
            mats = r.get("materials") or []
            n_dl = sum(1 for m in mats if m.get("allow_download") is not False)
            n_pdf = sum(1 for m in mats if m.get("pdf_fallback"))
            if n_pdf:
                tag = f"{n_dl}/{len(mats)}（{n_pdf} 个转PDF）"
            else:
                tag = "全部" if n_dl == len(mats) else f"{n_dl}/{len(mats)}"
            self.tree_cw.insert("", "end", iid=str(i), values=(
                r["title"], len(mats), tag,
            ))
        self.lbl_cw_cache.configure(**_cache_label(cached_at, stale))

    def _selected_courseware_ids(self) -> list[str]:
        ids: list[str] = []
        for iid in self.tree_cw.selection():
            try:
                idx = int(iid)
            except ValueError:
                continue
            if 0 <= idx < len(self._cw_records):
                ids.append(str(self._cw_records[idx].get("id")))
        return ids

    def _select_all_coursewares(self) -> None:
        self.tree_cw.selection_set(self.tree_cw.get_children())

    def _clear_courseware_selection(self) -> None:
        self.tree_cw.selection_remove(self.tree_cw.selection())

    def _on_download_selected_coursewares(self) -> None:
        selected_ids = self._selected_courseware_ids()
        if not selected_ids:
            messagebox.showinfo("请选择课件", "请先在课件列表中选择要下载的课件")
            return
        self._on_download_coursewares(selected_ids=selected_ids)

    def _on_download_coursewares(self, selected_ids: list[str] | None = None) -> None:
        if self._manager and not self._manager.is_done():
            messagebox.showinfo("请稍候", "已有下载任务在进行")
            return
        c = self._creds(need_course=True)
        if not c:
            return
        user, pw, course = c
        output = self.var_output.get().strip() or "downloads"
        self._start_worker(
            self._job_prepare_coursewares, user, pw, course, output, selected_ids
        )

    def _job_prepare_coursewares(self, user, pw, course, output, selected_ids=None) -> None:
        try:
            records = self._cw_records or None
            prep = prepare_courseware_download(
                user, pw, course, output, records=records,
                selected_ids=selected_ids, log=self._log,
            )
            self._last_output = str(prep["output_root"])
            if not selected_ids:
                self._ui(lambda: self._fill_coursewares(
                    prep["records"], None, False))
            self._ui(lambda: self._launch_manager(
                prep["session"], prep["tasks"]))
        except Exception as exc:
            err = str(exc)
            self._log(f"\n✗ 出错：{err}")
            self._ui(lambda err=err: messagebox.showerror("下载失败", err))

    # ---------- 提交作业 ----------
    def _choose_submit_files(self) -> None:
        files = filedialog.askopenfilenames(title="选择要提交的文件")
        if files:
            self._submit_files = list(files)
            names = ", ".join(os.path.basename(f) for f in self._submit_files)
            self.lbl_files.configure(
                text=f"{len(self._submit_files)} 个文件：{names}", foreground="black"
            )

    def _on_submit(self) -> None:
        c = self._creds()
        if not c:
            return
        user, pw, _ = c
        hw = self.var_hw.get().strip()
        if not hw:
            messagebox.showerror("缺少信息", "请填写或在列表中选择要提交的作业")
            return
        if not self._submit_files:
            messagebox.showerror("缺少信息", "请先选择要提交的文件")
            return
        names = "\n".join(f"  • {os.path.basename(f)}" for f in self._submit_files)
        ok = messagebox.askyesno(
            "确认提交",
            f"即将向作业 {hw} 提交以下文件：\n{names}\n\n"
            "此操作会真实改变服务器上的提交状态，不可自动撤销。确认提交？",
            icon="warning",
        )
        if not ok:
            self._log("已取消提交。")
            return
        self._start_worker(self._job_submit, user, pw, hw,
                           list(self._submit_files), self.var_comment.get())

    def _job_submit(self, user, pw, hw, files, comment) -> None:
        try:
            result = submit_homework_files(user, pw, hw, files, comment=comment, log=self._log)
            n = len(result["uploads"])
            self._log(f"\n✓ 已提交 {n} 个附件到作业 {hw}")
            self._ui(lambda: messagebox.showinfo("提交成功", f"已提交 {n} 个附件"))
        except Exception as exc:
            err = str(exc)
            self._log(f"\n✗ 提交失败：{err}")
            self._ui(lambda err=err: messagebox.showerror("提交失败", err))

    # ---------- 下载 ----------
    def _open_output(self) -> None:
        path = self._last_output or self.var_output.get()
        p = Path(path)
        if p.exists():
            os.startfile(str(p))   # Windows
        else:
            messagebox.showwarning("提示", "下载目录还不存在")

    def _on_start(self) -> None:
        if self._manager and not self._manager.is_done():
            messagebox.showinfo("请稍候", "已有下载任务在进行")
            return
        c = self._creds(need_course=True)
        if not c:
            return
        user, pw, course = c
        self.btn_start.configure(text="准备中…", state="disabled")
        self.btn_open.configure(state="disabled")
        self.progress.configure(value=0)
        self._start_worker(self._job_prepare, user, pw, course,
                           self.var_output.get().strip() or "downloads")

    def _job_prepare(self, user, pw, course, output) -> None:
        try:
            if self.var_list_only.get():
                records, cached_at, stale = homeworks_with_cache(
                    user, pw, course, refresh=True,
                    download_submissions=self.var_submissions.get(), log=self._log,
                )
                self._ui(lambda: self._fill_homeworks(records, cached_at, stale))
                self._log("✓ 仅列出完成，详见「作业」页签")
                self._ui(lambda: self.btn_start.configure(text="开始下载", state="normal"))
                return
            prep = prepare_download(
                user, pw, course, output,
                download_submissions=self.var_submissions.get(), log=self._log,
            )
            self._last_output = str(prep["output_root"])
            self._ui(lambda: self._launch_manager(prep["session"], prep["tasks"]))
        except Exception as exc:
            err = str(exc)
            self._log(f"\n✗ 出错：{err}")
            self._ui(lambda err=err: messagebox.showerror("下载失败", err))
            self._ui(lambda: self.btn_start.configure(text="开始下载", state="normal"))

    def _launch_manager(self, session, tasks: list[DownloadTask]) -> None:
        self._tasks = tasks
        self.tree_task.delete(*self.tree_task.get_children())
        self._task_rows.clear()
        for t in tasks:
            iid = self.tree_task.insert("", "end", values=(
                t.hw_title,
                {"assignment": "题目", "submission": "提交",
                 "material": "课件"}.get(t.kind, t.kind),
                t.name, t.status, _fmt_bytes(t.total_bytes) if t.total_bytes else "-",
            ))
            self._task_rows[t.id] = iid

        if not tasks:
            self._log("没有可下载的附件。")
            self.btn_start.configure(text="开始下载", state="normal")
            return

        self._manager = DownloadManager(
            session, tasks, parallel=self.var_parallel.get(),
            on_update=lambda t: self._ui(lambda: self._update_task_row(t)),
        )
        self._manager.start()
        self.btn_start.configure(text="下载中…", state="disabled")
        self.btn_pause.configure(state="normal")
        self.btn_resume.configure(state="disabled")
        self.progress.configure(maximum=max(len(tasks), 1), value=0)
        self.nb.select(3)
        self._log(f"开始下载 {len(tasks)} 个附件（并行 {self.var_parallel.get()}）")

    def _update_task_row(self, t: DownloadTask) -> None:
        iid = self._task_rows.get(t.id)
        if not iid:
            return
        if t.total_bytes:
            prog = f"{_fmt_bytes(t.done_bytes)}/{_fmt_bytes(t.total_bytes)}"
        else:
            prog = _fmt_bytes(t.done_bytes)
        label = {"pending": "等待", "downloading": "下载中", "paused": "暂停",
                 "done": "完成", "error": "失败", "skipped": "跳过"}.get(t.status, t.status)
        kind_label = {"assignment": "题目", "submission": "提交", "material": "课件"}.get(
            t.kind, t.kind
        )
        self.tree_task.item(iid, values=(
            t.hw_title, kind_label, t.name, label, prog,
        ))
        self._refresh_task_summary()

    def _refresh_task_summary(self) -> None:
        if not self._tasks:
            return
        done = sum(1 for t in self._tasks if t.status in ("done", "error", "skipped"))
        total = len(self._tasks)
        self.lbl_task_summary.configure(text=f"{done}/{total} 完成")
        self.progress.configure(value=done)
        if done >= total and self._manager:
            self.btn_start.configure(text="开始下载", state="normal")
            self.btn_pause.configure(state="disabled")
            self.btn_resume.configure(state="disabled")
            self.btn_open.configure(state="normal")
            ok = sum(1 for t in self._tasks if t.status == "done")
            self._log(f"\n✓ 下载完成：{ok}/{total} 成功")

    def _on_pause(self) -> None:
        if self._manager:
            self._manager.pause()
            self.btn_pause.configure(state="disabled")
            self.btn_resume.configure(state="normal")
            self._log("已暂停（进行中的文件停在原地，继续后断点续传）")

    def _on_resume(self) -> None:
        if self._manager:
            self._manager.resume()
            self.btn_pause.configure(state="normal")
            self.btn_resume.configure(state="disabled")
            self._log("已继续")

    # ---------- 队列/线程桥接 ----------
    def _log(self, msg: str) -> None:
        self._log_queue.put(msg)

    def _ui(self, fn) -> None:
        self._ui_queue.put(fn)

    def _poll_log_queue(self) -> None:
        try:
            while True:
                msg = self._log_queue.get_nowait()
                self.log.configure(state="normal")
                self.log.insert("end", msg + "\n")
                self.log.see("end")
                self.log.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self._poll_log_queue)

    def _poll_ui_queue(self) -> None:
        try:
            while True:
                fn = self._ui_queue.get_nowait()
                try:
                    fn()
                except Exception as exc:   # noqa: BLE001
                    self._log(f"[UI] {exc}")
        except queue.Empty:
            pass
        self.after(60, self._poll_ui_queue)


def _cache_label(cached_at, stale: bool) -> dict:
    if cached_at is None:
        return {"text": "（联网获取）", "foreground": "gray"}
    age = cache.age_days(cached_at)
    when = cached_at.strftime("%Y-%m-%d %H:%M")
    if stale:
        d = f"{age:.0f}" if age is not None else "?"
        return {"text": f"缓存于 {when}（{d} 天前，数据可能已更新，建议刷新）",
                "foreground": "#c00"}
    return {"text": f"缓存于 {when}", "foreground": "gray"}


if __name__ == "__main__":
    App().mainloop()
