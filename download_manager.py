"""多线程下载管理器：任务列表 + 暂停/继续 + 并行数控制。

把每个待下载附件包成 DownloadTask，交给若干 worker 线程并行下载。
暂停采用「文件内断点续传」语义：pause() 清除 _running 事件后，
- 尚未开始的任务在 worker 取任务前的 _running.wait() 处阻塞；
- 正在下载的任务把同一个 _running 事件当作 pause_event 传给
  download_file_resumable，使其在 chunk 之间阻塞，**停在原地、进度不丢**。
resume() 重新 set 该事件即可全部继续。

requests.Session 在多线程下各自发独立请求是安全的（底层连接池），
因此所有 worker 复用同一个已登录 session。
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import requests

from downloader import download_file_resumable

MAX_PARALLEL = 8

# 状态机：pending -> downloading -> done | error | skipped
#                          ^ (暂停时仍显示 downloading，停在原地)


@dataclass
class DownloadTask:
    id: int
    name: str
    url: str
    dest_dir: Path
    hw_title: str
    kind: str                       # "assignment" / "submission"
    status: str = "pending"
    done_bytes: int = 0
    total_bytes: int = 0
    error: str | None = None
    saved_path: Path | None = None


# 任务状态变化回调：(task) -> None。可能从 worker 线程触发，GUI 需自行切回主线程。
UpdateCb = Callable[[DownloadTask], None]


class DownloadManager:
    def __init__(
        self,
        session: requests.Session,
        tasks: list[DownloadTask],
        *,
        parallel: int = 4,
        on_update: UpdateCb | None = None,
    ) -> None:
        self.session = session
        self.tasks = tasks
        self._parallel = max(1, min(MAX_PARALLEL, parallel))
        self._on_update = on_update

        self._queue: "queue.Queue[DownloadTask]" = queue.Queue()
        for t in tasks:
            self._queue.put(t)
        self._running = threading.Event()
        self._running.set()             # 默认运行；pause() 清除
        self._cancel = threading.Event()
        self._workers: list[threading.Thread] = []
        self._started = False

    # ---------- 配置 ----------
    def set_parallel(self, n: int) -> None:
        """设置并行数；仅在 start() 之前生效。"""
        if self._started:
            return
        self._parallel = max(1, min(MAX_PARALLEL, n))

    # ---------- 生命周期 ----------
    def start(self) -> None:
        if self._started:
            return
        self._started = True
        for i in range(self._parallel):
            w = threading.Thread(target=self._worker, name=f"dl-{i}", daemon=True)
            w.start()
            self._workers.append(w)

    def pause(self) -> None:
        self._running.clear()

    def resume(self) -> None:
        self._running.set()

    def stop(self) -> None:
        """取消全部：置 cancel 并解除暂停，让 worker 尽快退出。"""
        self._cancel.set()
        self._running.set()

    def is_paused(self) -> bool:
        return not self._running.is_set()

    def is_done(self) -> bool:
        return all(
            t.status in ("done", "error", "skipped") for t in self.tasks
        )

    def join(self, timeout: float | None = None) -> None:
        for w in self._workers:
            w.join(timeout)

    # ---------- worker ----------
    def _emit(self, task: DownloadTask) -> None:
        if self._on_update:
            self._on_update(task)

    def _worker(self) -> None:
        while True:
            try:
                task = self._queue.get_nowait()
            except queue.Empty:
                return
            if self._cancel.is_set():
                task.status = "skipped"
                self._emit(task)
                self._queue.task_done()
                continue

            # 取任务前若处于暂停，先等待恢复
            self._running.wait()

            task.status = "downloading"
            self._emit(task)

            def progress(done: int, total: int, _t=task) -> None:
                _t.done_bytes = done
                if total:
                    _t.total_bytes = total
                self._emit(_t)

            try:
                saved = download_file_resumable(
                    self.session, task.url, task.dest_dir, task.name,
                    pause_event=self._running,
                    cancel_event=self._cancel,
                    on_progress=progress,
                )
            except Exception as exc:    # noqa: BLE001 单个任务失败不拖垮整体
                task.status = "error"
                task.error = str(exc)
                self._emit(task)
                self._queue.task_done()
                continue

            if saved is None:
                # cancel 触发：若是真停止则标记 skipped，否则（理论上不会到这）回 pending
                task.status = "skipped" if self._cancel.is_set() else "pending"
            else:
                task.status = "done"
                task.saved_path = saved
                if task.total_bytes:
                    task.done_bytes = task.total_bytes
            self._emit(task)
            self._queue.task_done()
