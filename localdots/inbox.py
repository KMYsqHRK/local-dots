"""人への質問と回答の受信箱。1問を1ファイル (state/inbox/<id>.json) にする。"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from .state import now_local, read_json, to_iso, write_json


class Inbox:
    def __init__(self, state_dir: Path):
        self.dir = state_dir / "inbox"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, qid: str) -> Path:
        return self.dir / f"{qid}.json"

    def ask(self, dot: str, question: str, options: list[str]) -> str:
        qid = f"{now_local():%Y%m%d}-{uuid.uuid4().hex[:6]}"
        write_json(self._path(qid), {
            "id": qid,
            "dot": dot,
            "question": question,
            "options": options,
            "asked_at": to_iso(now_local()),
            "answer": None,
            "answered_at": None,
        })
        return qid

    def get(self, qid: str) -> dict[str, Any] | None:
        return read_json(self._path(qid), None)

    def answer(self, qid: str, text: str) -> dict[str, Any]:
        q = self.get(qid)
        if q is None:
            raise KeyError(qid)
        q["answer"] = text
        q["answered_at"] = to_iso(now_local())
        write_json(self._path(qid), q)
        return q

    def list(self, pending_only: bool = True) -> list[dict[str, Any]]:
        items = [read_json(p, None) for p in sorted(self.dir.glob("*.json"))]
        return [q for q in items if q and (not pending_only or q["answer"] is None)]
