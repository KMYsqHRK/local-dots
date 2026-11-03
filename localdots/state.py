"""dot ごとの状態・記憶・データ・実行記録。どれも人が読める JSON / JSONL で置く。"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

MAX_NOTES = 50


def now_local() -> datetime:
    return datetime.now().astimezone()


def to_iso(t: datetime | None) -> str | None:
    return t.isoformat(timespec="seconds") if t else None


def from_iso(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class DotState:
    def __init__(self, state_dir: Path, name: str):
        self.dir = state_dir / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self._status_path = self.dir / "status.json"
        self._memory_path = self.dir / "memory.json"
        self._data_path = self.dir / "data.json"
        self.data: dict[str, Any] = read_json(self._data_path, {})

    # --- status: idle / blocked / error、前回と次回の時刻、前回の要約
    def load_status(self) -> dict[str, Any]:
        return read_json(self._status_path, {"status": "idle"})

    def save_status(self, status: dict[str, Any]) -> None:
        write_json(self._status_path, status)

    # --- memory: note ツールで残す気づき(新しい順)
    def notes(self) -> list[dict[str, str]]:
        return read_json(self._memory_path, {"notes": []})["notes"]

    def add_note(self, text: str) -> None:
        notes = [{"at": to_iso(now_local()), "text": text}] + self.notes()
        write_json(self._memory_path, {"notes": notes[:MAX_NOTES]})

    # --- data: 専用ツールが自由に使う永続データ
    def save_data(self) -> None:
        write_json(self._data_path, self.data)

    # --- runs: 1回の実行の全記録
    def open_run_log(self, started: datetime) -> RunLog:
        return RunLog(self.dir / "runs" / f"{started:%Y%m%d-%H%M%S}.jsonl")


class RunLog:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path

    def write(self, event: str, **fields: Any) -> None:
        line = json.dumps({"at": to_iso(now_local()), "event": event, **fields}, ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
