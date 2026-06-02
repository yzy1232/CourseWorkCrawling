"""学在城院作业附件下载器 —— 图形界面 (Tkinter)。

运行： python gui.py
界面填写账号/密码/课程 ID，点击开始即可在后台线程下载，
日志实时回显，进度条显示作业处理进度。
"""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from dotenv import load_dotenv

from core import (
    get_courses,
    list_unsubmitted,
    run_download,
    submit_homework_files,
)


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("学在城院 作业附件下载器")
        self.geometry("720x560")
        self.minsize(640, 480)

        load_dotenv()
        self._log_queue: "queue.Queue[str]" = queue.Queue()
        self._worker: threading.Thread | None = None

        self._build_widgets()
        self._poll_log_queue()

    # ---------- UI 构建 ----------
    def _build_widgets(self) -> None:
        pad = {"padx": 8, "pady": 4}
        form = ttk.Frame(self)
        form.pack(fill="x", **pad)
        form.columnconfigure(1, weight=1)

        self.var_user = tk.StringVar(value=os.getenv("HZCU_USERNAME", ""))
        self.var_pass = tk.StringVar(value=os.getenv("HZCU_PASSWORD", ""))
        self.var_course = tk.StringVar(value=os.getenv("COURSE_ID", ""))
        self.var_output = tk.StringVar(value=os.getenv("OUTPUT_DIR", "downloads"))
        self.var_submissions = tk.BooleanVar(value=True)
        self.var_list_only = tk.BooleanVar(value=False)
        self.var_hw = tk.StringVar(value="")
        self.var_comment = tk.StringVar(value="")
        self._courses: list[dict] = []     # [{id,name,raw}]
        self._submit_files: list[str] = []

        ttk.Label(form, text="学号/账号").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(form, textvariable=self.var_user).grid(row=0, column=1, columnspan=2, sticky="ew", **pad)

        ttk.Label(form, text="密码").grid(row=1, column=0, sticky="w", **pad)
        self.entry_pass = ttk.Entry(form, textvariable=self.var_pass, show="•")
        self.entry_pass.grid(row=1, column=1, sticky="ew", **pad)
        self.var_show = tk.BooleanVar(value=False)
        ttk.Checkbutton(form, text="显示", variable=self.var_show,
                        command=self._toggle_pw).grid(row=1, column=2, sticky="w", **pad)

        ttk.Label(form, text="课程").grid(row=2, column=0, sticky="w", **pad)
        self.combo_course = ttk.Combobox(
            form, textvariable=self.var_course, state="normal"
        )
        self.combo_course.grid(row=2, column=1, sticky="ew", **pad)
        self.combo_course.bind("<<ComboboxSelected>>", self._on_course_pick)
        ttk.Button(form, text="刷新课程", command=self._on_list_courses).grid(
            row=2, column=2, sticky="w", **pad
        )

        ttk.Label(form, text="保存目录").grid(row=3, column=0, sticky="w", **pad)
        ttk.Entry(form, textvariable=self.var_output).grid(row=3, column=1, sticky="ew", **pad)
        ttk.Button(form, text="浏览…", command=self._choose_dir).grid(row=3, column=2, sticky="w", **pad)

        opts = ttk.Frame(self)
        opts.pack(fill="x", **pad)
        ttk.Checkbutton(opts, text="同时下载我的提交", variable=self.var_submissions).pack(side="left", padx=8)
        ttk.Checkbutton(opts, text="仅列出不下载", variable=self.var_list_only).pack(side="left", padx=8)

        bar = ttk.Frame(self)
        bar.pack(fill="x", **pad)
        self.btn_start = ttk.Button(bar, text="开始下载", command=self._on_start)
        self.btn_start.pack(side="left", padx=8)
        self.btn_unsub = ttk.Button(bar, text="未提交作业", command=self._on_unsubmitted)
        self.btn_unsub.pack(side="left", padx=8)
        self.btn_open = ttk.Button(bar, text="打开下载目录", command=self._open_output, state="disabled")
        self.btn_open.pack(side="left", padx=8)
        self.progress = ttk.Progressbar(bar, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=8)

        # ---------- 提交作业面板 ----------
        sub = ttk.LabelFrame(self, text="提交作业（写操作，会真实改变服务器状态）")
        sub.pack(fill="x", padx=8, pady=4)
        sub.columnconfigure(1, weight=1)
        ttk.Label(sub, text="作业 ID").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(sub, textvariable=self.var_hw, width=14).grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(sub, text="说明").grid(row=0, column=2, sticky="w", **pad)
        ttk.Entry(sub, textvariable=self.var_comment).grid(row=0, column=3, sticky="ew", **pad)
        sub.columnconfigure(3, weight=1)
        ttk.Button(sub, text="选择文件…", command=self._choose_submit_files).grid(
            row=1, column=0, sticky="w", **pad
        )
        self.lbl_files = ttk.Label(sub, text="未选择文件", foreground="gray")
        self.lbl_files.grid(row=1, column=1, columnspan=2, sticky="w", **pad)
        self.btn_submit = ttk.Button(sub, text="提交", command=self._on_submit)
        self.btn_submit.grid(row=1, column=3, sticky="e", **pad)

        ttk.Label(self, text="日志").pack(anchor="w", padx=8)
        self.log = tk.Text(self, height=16, wrap="word", state="disabled")
        self.log.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        scroll = ttk.Scrollbar(self.log, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")

    # ---------- 交互 ----------
    def _toggle_pw(self) -> None:
        self.entry_pass.configure(show="" if self.var_show.get() else "•")

    def _choose_dir(self) -> None:
        d = filedialog.askdirectory(initialdir=self.var_output.get() or ".")
        if d:
            self.var_output.set(d)

    # ---------- 课程列表 ----------
    def _busy(self) -> bool:
        return bool(self._worker and self._worker.is_alive())

    def _set_buttons(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for b in (self.btn_start, self.btn_unsub, self.btn_submit):
            self.after(0, lambda b=b, s=state: b.configure(state=s))

    def _start_worker(self, target, *args) -> None:
        if self._busy():
            messagebox.showinfo("请稍候", "已有任务正在运行")
            return
        self._set_buttons(False)
        self._worker = threading.Thread(target=target, args=args, daemon=True)
        self._worker.start()

    def _on_course_pick(self, _event=None) -> None:
        """从下拉选中“ID  课程名”，把课程 ID 回填到 var_course。"""
        idx = self.combo_course.current()
        if 0 <= idx < len(self._courses):
            self.var_course.set(str(self._courses[idx]["id"]))

    def _on_list_courses(self) -> None:
        user = self.var_user.get().strip()
        pw = self.var_pass.get()
        if not user or not pw:
            messagebox.showerror("缺少信息", "请先填写账号和密码")
            return
        self._start_worker(self._job_list_courses, user, pw)

    def _job_list_courses(self, user: str, pw: str) -> None:
        try:
            courses = get_courses(user, pw, log=self._log)
            self._courses = courses
            values = [f"{c['id']}  {c['name']}" for c in courses]

            def upd():
                self.combo_course["values"] = values
                self._log(f"✓ 已载入 {len(courses)} 门课程，可在下拉框选择")
            self.after(0, upd)
        except Exception as exc:
            self._log(f"\n✗ 获取课程失败：{exc}")
            self.after(0, lambda: messagebox.showerror("获取课程失败", str(exc)))
        finally:
            self._set_buttons(True)

    # ---------- 未提交作业 ----------
    def _on_unsubmitted(self) -> None:
        user = self.var_user.get().strip()
        pw = self.var_pass.get()
        course = self.var_course.get().strip()
        if not user or not pw or not course:
            messagebox.showerror("缺少信息", "请填写账号、密码和课程 ID")
            return
        self._start_worker(self._job_unsubmitted, user, pw, course)

    def _job_unsubmitted(self, user: str, pw: str, course: str) -> None:
        try:
            items = list_unsubmitted(user, pw, course, log=self._log)
            if not items:
                self._log("\n✓ 该课程没有未提交的作业")
            else:
                self._log(f"\n尚未提交的作业（{len(items)} 个）：")
                for it in items:
                    dl = it.get("deadline") or "无截止时间"
                    self._log(f"  [{it['id']}] {it['title']}  截止: {dl}")
        except Exception as exc:
            self._log(f"\n✗ 查询失败：{exc}")
            self.after(0, lambda: messagebox.showerror("查询失败", str(exc)))
        finally:
            self._set_buttons(True)

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
        user = self.var_user.get().strip()
        pw = self.var_pass.get()
        hw = self.var_hw.get().strip()
        if not user or not pw:
            messagebox.showerror("缺少信息", "请填写账号和密码")
            return
        if not hw:
            messagebox.showerror("缺少信息", "请填写要提交的作业 ID")
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
        self._start_worker(
            self._job_submit, user, pw, hw, list(self._submit_files),
            self.var_comment.get(),
        )

    def _job_submit(self, user, pw, hw, files, comment) -> None:
        try:
            result = submit_homework_files(
                user, pw, hw, files, comment=comment, log=self._log
            )
            n = len(result["uploads"])
            self._log(f"\n✓ 已提交 {n} 个附件到作业 {hw}")
            self.after(0, lambda: messagebox.showinfo("提交成功", f"已提交 {n} 个附件"))
        except Exception as exc:
            self._log(f"\n✗ 提交失败：{exc}")
            self.after(0, lambda: messagebox.showerror("提交失败", str(exc)))
        finally:
            self._set_buttons(True)

    def _open_output(self) -> None:
        path = getattr(self, "_last_output", None) or self.var_output.get()
        p = Path(path)
        if p.exists():
            os.startfile(str(p))  # Windows
        else:
            messagebox.showwarning("提示", "下载目录还不存在")

    def _log(self, msg: str) -> None:
        self._log_queue.put(msg)

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

    def _set_progress(self, done: int, total: int) -> None:
        # 在主线程更新控件
        def upd():
            self.progress.configure(maximum=max(total, 1), value=done)
        self.after(0, upd)

    def _on_start(self) -> None:
        if self._busy():
            return
        user = self.var_user.get().strip()
        pw = self.var_pass.get()
        course = self.var_course.get().strip()
        if not user or not pw or not course:
            messagebox.showerror("缺少信息", "请填写账号、密码和课程 ID")
            return

        self.btn_start.configure(text="下载中…")
        self.btn_open.configure(state="disabled")
        self.progress.configure(value=0)
        self._start_worker(
            self._run_job, user, pw, course,
            self.var_output.get().strip() or "downloads",
        )

    def _run_job(self, user: str, pw: str, course: str, output: str) -> None:
        try:
            result = run_download(
                user, pw, course, output,
                download_submissions=self.var_submissions.get(),
                list_only=self.var_list_only.get(),
                log=self._log,
                progress=self._set_progress,
            )
            self._last_output = result.get("output_root") or output
            hw = result["homeworks"]
            files = sum(len(r["assignment"]) + len(r["submission"]) for r in hw)
            self._log(f"\n✓ 完成：{len(hw)} 个作业，{files} 个附件")
            self.after(0, lambda: self.btn_open.configure(state="normal"))
        except Exception as exc:
            self._log(f"\n✗ 出错：{exc}")
            self.after(0, lambda: messagebox.showerror("下载失败", str(exc)))
        finally:
            self._set_buttons(True)
            self.after(0, lambda: self.btn_start.configure(text="开始下载"))


if __name__ == "__main__":
    App().mainloop()
