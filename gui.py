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

from core import run_download


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

        ttk.Label(form, text="学号/账号").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(form, textvariable=self.var_user).grid(row=0, column=1, columnspan=2, sticky="ew", **pad)

        ttk.Label(form, text="密码").grid(row=1, column=0, sticky="w", **pad)
        self.entry_pass = ttk.Entry(form, textvariable=self.var_pass, show="•")
        self.entry_pass.grid(row=1, column=1, sticky="ew", **pad)
        self.var_show = tk.BooleanVar(value=False)
        ttk.Checkbutton(form, text="显示", variable=self.var_show,
                        command=self._toggle_pw).grid(row=1, column=2, sticky="w", **pad)

        ttk.Label(form, text="课程 ID").grid(row=2, column=0, sticky="w", **pad)
        ttk.Entry(form, textvariable=self.var_course).grid(row=2, column=1, columnspan=2, sticky="ew", **pad)

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
        self.btn_open = ttk.Button(bar, text="打开下载目录", command=self._open_output, state="disabled")
        self.btn_open.pack(side="left", padx=8)
        self.progress = ttk.Progressbar(bar, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=8)

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
        if self._worker and self._worker.is_alive():
            return
        user = self.var_user.get().strip()
        pw = self.var_pass.get()
        course = self.var_course.get().strip()
        if not user or not pw or not course:
            messagebox.showerror("缺少信息", "请填写账号、密码和课程 ID")
            return

        self.btn_start.configure(state="disabled", text="下载中…")
        self.btn_open.configure(state="disabled")
        self.progress.configure(value=0)

        self._worker = threading.Thread(
            target=self._run_job,
            args=(user, pw, course, self.var_output.get().strip() or "downloads"),
            daemon=True,
        )
        self._worker.start()

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
            self.after(0, lambda: self.btn_start.configure(state="normal", text="开始下载"))


if __name__ == "__main__":
    App().mainloop()
