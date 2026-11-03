"""すべての dot が使える組み込みツール。"""

from __future__ import annotations

from datetime import timedelta

from .tools import Control, ToolContext, collect_tools, tool


@tool
def note(ctx: ToolContext, text: str) -> dict:
    """次回以降の実行でも覚えておきたい気づきを1つ記録する。短く、具体的に。"""
    ctx.state.add_note(text)
    return {"ok": True}


@tool
def ask_human(ctx: ToolContext, question: str, options: list = []) -> Control:
    """人に質問して、この実行を終える。回答が来たら次の実行で受け取れる。自分で決められないことにだけ使う。"""
    qid = ctx.inbox.ask(ctx.dot.name, question, [str(o) for o in options])
    return Control("blocked", f"人の回答待ち: {question}", question_id=qid)


@tool
def snooze(ctx: ToolContext, minutes: float, reason: str) -> Control:
    """今は何もせず、minutes 分後にもう一度起こしてもらう。条件が揃うのを待つときに使う。"""
    minutes = max(1.0, minutes)
    return Control("snoozed", reason, next_due=ctx.now + timedelta(minutes=minutes))


@tool
def finish(ctx: ToolContext, summary: str) -> Control:
    """今回の仕事を終える。summary には、やったことと次回やるべきことを書く(次回の実行で読まれる)。"""
    return Control("done", summary)


BUILTIN_TOOLS = collect_tools(globals())
