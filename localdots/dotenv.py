"""プロジェクトのルートの .env を環境変数として読む(python-dotenv の、よく使う部分だけ)。

    # コメント
    KEY=value
    KEY2="空白を含む値"
    export KEY3='value'

すでに設定されている環境変数は上書きしない(PowerShell で一時的に変えた値が勝つ)。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_LINE = re.compile(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*")


def parse(text: str) -> dict[str, str]:
    values = {}
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE.fullmatch(line)
        if not m:
            raise ValueError(f".env の {n} 行目が読めません: {raw!r}(KEY=value の形で書いてください)")
        key, value = m.groups()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:  # 引用符で囲まない値の後ろのコメント
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def load(path: Path) -> list[str]:
    """path があれば読み込み、新しく設定したキーの一覧を返す。"""
    if not path.exists():
        return []
    added = []
    for key, value in parse(path.read_text(encoding="utf-8-sig")).items():
        if key not in os.environ:
            os.environ[key] = value
            added.append(key)
    return added
