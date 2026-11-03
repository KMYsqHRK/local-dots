"""python -m localdots <command>"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import dotenv
from .backend import BackendError
from .config import ConfigError, load_app, load_dots
from .inbox import Inbox
from .scheduler import due_dots, is_due, run_dots
from .state import DotState, now_local

DOT_TEMPLATE = """\
description = "{name} の説明を一行で"
enabled = true

[schedule]
every = "1h"
# at = "02:00"
# window = ["00:00", "06:00"]
# manual = true

[agent]
max_steps = 10
instructions_file = "instructions.md"
"""

INSTRUCTIONS_TEMPLATE = """\
ここに担当の仕事内容を書く。毎回の実行で何を確認し、何をして、いつ finish するか。
"""

TOOLS_TEMPLATE = '''\
"""この dot 専用のツール。@tool を付けた関数が LLM から呼べるようになる。"""

from localdots.tools import tool


@tool
def example(ctx, text: str) -> dict:
    """例: 受け取った文字列の長さを返す。"""
    return {"length": len(text)}
'''


def _find_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / "localdots.toml").exists():
            return p
    return start


def cmd_list(app, dots, args) -> int:
    if not dots:
        print(f"dot がありません({app.dots_dir})")
    inbox = Inbox(app.state_dir)
    now = now_local()
    for d in dots.values():
        status = DotState(app.state_dir, d.name).load_status()
        due, why = is_due(d, status, inbox, now)
        mark = "●" if due else "・"
        print(f"{mark} {d.name:<24} {status.get('status', 'idle'):<8} {why}")
        if d.description:
            print(f"    {d.description}")
    return 0


def cmd_status(app, dots, args) -> int:
    d = _get(dots, args.name)
    status = DotState(app.state_dir, d.name).load_status()
    for k, v in status.items():
        print(f"{k}: {v}")
    notes = DotState(app.state_dir, d.name).notes()[:5]
    if notes:
        print("notes:")
        for n in notes:
            print(f"  - {n['text']}")
    return 0


def _logger(app, echo: bool = True):
    """画面と state/localdots.log の両方に書く(タスクスケジューラーから動かすと画面は無いので)。"""
    path = app.state_dir / "localdots.log"
    path.parent.mkdir(parents=True, exist_ok=True)

    def log(line: str) -> None:
        if echo:
            print(line)
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{now_local():%Y-%m-%d %H:%M:%S} {line}\n")

    return log


def cmd_run(app, dots, args) -> int:
    targets = [_get(dots, n) for n in args.names]
    results = run_dots(app, targets, manage=False if args.no_manage else None, log=_logger(app))
    return 0 if all(r.outcome != "error" for r in results) else 1


def cmd_tick(app, dots, args) -> int:
    targets = due_dots(app, dots.values())
    if not targets:
        print("期限の来た dot はありません")
        return 0
    results = run_dots(app, targets, manage=False if args.no_manage else None, log=_logger(app))
    return 0 if all(r.outcome != "error" for r in results) else 1


def cmd_watch(app, dots, args) -> int:
    print(f"{args.interval} 秒ごとに tick します(Ctrl+C で終了)")
    while True:
        try:
            cmd_tick(app, load_dots(app), args)
        except (BackendError, RuntimeError) as e:
            print(f"[watch] {e}", file=sys.stderr)
        time.sleep(args.interval)


def cmd_inbox(app, dots, args) -> int:
    items = Inbox(app.state_dir).list(pending_only=not args.all)
    if not items:
        print("質問はありません")
    for q in items:
        print(f"[{q['id']}] {q['dot']}  ({q['asked_at']})")
        print(f"  Q: {q['question']}")
        if q["options"]:
            print(f"  選択肢: {' / '.join(q['options'])}")
        if q["answer"] is not None:
            print(f"  A: {q['answer']}")
    return 0


def cmd_answer(app, dots, args) -> int:
    try:
        q = Inbox(app.state_dir).answer(args.id, args.text)
    except KeyError:
        print(f"質問 {args.id} が見つかりません", file=sys.stderr)
        return 1
    print(f"{q['dot']} に回答しました。次の tick で続きから動きます")
    return 0


def cmd_new(app, dots, args) -> int:
    path = app.dots_dir / args.name
    if path.exists():
        print(f"{path} はもうあります", file=sys.stderr)
        return 1
    path.mkdir(parents=True)
    (path / "dot.toml").write_text(DOT_TEMPLATE.format(name=args.name), encoding="utf-8")
    (path / "instructions.md").write_text(INSTRUCTIONS_TEMPLATE, encoding="utf-8")
    (path / "tools.py").write_text(TOOLS_TEMPLATE, encoding="utf-8")
    print(f"{path} を作りました。dot.toml と instructions.md を書き換えてください")
    return 0


def _get(dots, name):
    if name not in dots:
        raise ConfigError(f"dot {name!r} がありません。あるのは: {', '.join(dots) or 'なし'}")
    return dots[name]


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows のコンソールでも日本語を崩さない
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    p = argparse.ArgumentParser(prog="localdots", description="ローカル LLM で動く、担当を持ったエージェント")
    p.add_argument("--root", type=Path, help="localdots.toml のあるフォルダー(省略時はカレントから探す)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="dot の一覧と、次に動く時刻")
    s = sub.add_parser("status", help="dot の状態と最近の記憶")
    s.add_argument("name")
    s = sub.add_parser("run", help="スケジュールを無視して今すぐ動かす")
    s.add_argument("names", nargs="+")
    s.add_argument("--no-manage", action="store_true", help="LLM サーバーを起動・停止しない")
    s = sub.add_parser("tick", help="期限の来た dot だけを動かす(タスクスケジューラーから呼ぶ)")
    s.add_argument("--no-manage", action="store_true", help="LLM サーバーを起動・停止しない")
    s = sub.add_parser("watch", help="tick を一定間隔で繰り返す(前面で動かしたいとき)")
    s.add_argument("--interval", type=int, default=300)
    s.add_argument("--no-manage", action="store_true")
    s = sub.add_parser("inbox", help="人への質問の一覧")
    s.add_argument("--all", action="store_true", help="回答済みも表示する")
    s = sub.add_parser("answer", help="質問に答える")
    s.add_argument("id")
    s.add_argument("text")
    s = sub.add_parser("new", help="新しい dot のひな形を作る")
    s.add_argument("name")

    args = p.parse_args(argv)
    commands = {
        "list": cmd_list, "status": cmd_status, "run": cmd_run, "tick": cmd_tick, "watch": cmd_watch,
        "inbox": cmd_inbox, "answer": cmd_answer, "new": cmd_new,
    }
    try:
        app = load_app(args.root or _find_root(Path.cwd()))
        dotenv.load(app.root / ".env")  # メールのパスワードや API キー(git に入れない)
        dots = load_dots(app)
        return commands[args.command](app, dots, args)
    except (ConfigError, BackendError, RuntimeError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        if "app" in locals():
            _logger(app, echo=False)(f"エラー: {e}")
        return 1
    except KeyboardInterrupt:
        return 130
