"""期限の判定と tick(期限の来た dot だけを動かす)。"""

from __future__ import annotations

import os
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Callable, Iterable

from .agent import RunResult, run_dot
from .backend import Backend
from .config import AppConfig, DotConfig
from .inbox import Inbox
from .state import DotState, from_iso, now_local


def _hhmm(text: str) -> time:
    h, m = text.split(":")
    return time(int(h), int(m))


def in_window(window: tuple[str, str] | None, now: datetime) -> bool:
    if window is None:
        return True
    start, end, t = _hhmm(window[0]), _hhmm(window[1]), now.time()
    return start <= t <= end if start <= end else (t >= start or t <= end)  # 日付をまたぐ窓


def is_due(dot: DotConfig, status: dict, inbox: Inbox, now: datetime) -> tuple[bool, str]:
    if not dot.enabled:
        return False, "無効"
    if status.get("pending_question"):
        q = inbox.get(status["pending_question"])
        if q is None or q["answer"] is None:
            return False, "人の回答待ち"
        return True, "回答が届いた"
    s = dot.schedule
    if s.manual:
        return False, "手動のみ"
    if s.days is not None and now.weekday() not in s.days:
        return False, "今日は動かない曜日"
    if not in_window(s.window, now):
        return False, f"時間帯 {s.window[0]}-{s.window[1]} の外"
    next_due = from_iso(status.get("next_due"))
    if next_due:
        return (now >= next_due), f"snooze で {next_due:%m/%d %H:%M} まで待機"
    last = from_iso(status.get("last_run"))
    if s.every_s is not None:
        if last is None:
            return True, "初回"
        due_at = last + timedelta(seconds=s.every_s)
        return (now >= due_at), f"次は {due_at:%m/%d %H:%M}"
    if s.at is not None:
        today_at = now.replace(hour=_hhmm(s.at).hour, minute=_hhmm(s.at).minute, second=0, microsecond=0)
        if now < today_at:
            return False, f"次は今日 {s.at}"
        if last is not None and last >= today_at:
            return False, f"次は明日 {s.at}"
        return True, f"毎日 {s.at}"
    return False, "スケジュールなし"


class TickLock:
    """tick の多重起動を防ぐ。前の tick が落ちてロックが残っても、そのプロセスが居なければ取り直す。"""

    def __init__(self, path: Path):
        self.path = path

    def __enter__(self) -> TickLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if _pid_alive(self.path.read_text().strip()):
                raise RuntimeError(f"別の tick が実行中です({self.path})")
            self.path.unlink()
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return self

    def __exit__(self, *exc) -> None:
        self.path.unlink(missing_ok=True)


def _pid_alive(text: str) -> bool:
    try:
        pid = int(text)
    except ValueError:
        return False
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def run_dots(
    app: AppConfig,
    dots: Iterable[DotConfig],
    *,
    manage: bool | None = None,
    log: Callable[[str], None] = print,
) -> list[RunResult]:
    """渡された dot を順に動かす。LLM サーバーは最初に1回だけ用意し、最後に(自分で起動していれば)止める。"""
    dots = list(dots)
    if not dots:
        return []
    inbox = Inbox(app.state_dir)
    results = []
    with TickLock(app.state_dir / ".tick.lock"):
        with Backend(app.backend, app.state_dir / "backend.log", manage=manage, log=log) as backend:
            for dot in dots:
                r = run_dot(dot, app, backend.client, inbox, log=log)
                log(f"[{dot.name}] {r.outcome}: {r.summary}")
                results.append(r)
    return results


def due_dots(app: AppConfig, dots: Iterable[DotConfig], now: datetime | None = None) -> list[DotConfig]:
    now = now or now_local()
    inbox = Inbox(app.state_dir)
    return [d for d in dots if is_due(d, DotState(app.state_dir, d.name).load_status(), inbox, now)[0]]
