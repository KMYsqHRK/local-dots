"""1つの dot の1回分の実行。LLM に JSON で行動を1つずつ選ばせ、コードがツールを実行する。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from .builtins import BUILTIN_TOOLS
from .config import AppConfig, DotConfig
from .inbox import Inbox
from .llm import ChatModel, LLMError
from .state import DotState, now_local, to_iso
from .tools import Control, Tool, ToolArgError, ToolContext, load_dot_tools

MAX_BAD_REPLIES = 3
CHARS_PER_TOKEN = 1.5  # 日本語で実測 1.8 字/トークン。少し安全側に見積もる

SYSTEM_TEMPLATE = """\
あなたは「{name}」という担当を任された自律エージェントです。定期的に起こされ、毎回少しずつ仕事を進めます。

# 担当
{description}

# 仕事の内容
{instructions}

# 覚えていること(新しい順)
{notes}

# 使えるツール
{tools}

# 応答のしかた
毎回、次の形の JSON オブジェクトを1つだけ返してください。それ以外の文章は書かないでください。
{{"thought": "短い考え", "tool": "ツール名", "args": {{"引数名": 値}}}}

- ツールの結果は OBSERVATION として返ってきます。それを見て次の行動を決めてください。
- 今回の仕事が終わったら finish を呼んでください。summary は次回のあなたが読みます。
- 自分で決められないことがあるときだけ ask_human を使ってください。
- 1回の実行で使える行動は最大 {max_steps} 回です。
"""


@dataclass
class RunResult:
    dot: str
    outcome: str  # done / blocked / snoozed / step_limit / skipped / error
    summary: str
    steps: int = 0


def parse_action(text: str) -> dict[str, Any] | None:
    """応答から "tool" キーを持つ最初の JSON オブジェクトを取り出す。前後の文章やコードフェンスは無視する。"""
    decoder = json.JSONDecoder()
    i = text.find("{")
    while i != -1:
        try:
            obj, _ = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict) and isinstance(obj.get("tool"), str):
            obj.setdefault("args", {})
            return obj
        i = text.find("{", i + 1)
    return None


def _estimate_tokens(messages: list[dict[str, str]]) -> int:
    return int(sum(len(m["content"]) for m in messages) / CHARS_PER_TOKEN) + 8 * len(messages)


def trim_messages(messages: list[dict[str, str]], budget_tokens: int, window: int | None = None) -> list[dict[str, str]]:
    """system と最初の指示は残し、収まるまで古いやりとり(assistant と OBSERVATION の組)から省く。

    window を指定すると、直近 window 組だけを残す。プロンプトが短いほど読み込みが速い
    (Strata は毎回プロンプトを最初から読み直すので、7,000 トークンで約1分かかる)。
    """
    head, rest = messages[:2], messages[2:]
    dropped = False
    if window is not None and len(rest) > 2 * window:
        rest = rest[len(rest) - 2 * window:]
        dropped = True
    while rest and _estimate_tokens(head + rest) > budget_tokens:
        rest = rest[2:]
        dropped = True
    if dropped:
        head = head + [{"role": "user", "content": "(古いやりとりは文字数の都合で省きました)"}]
    return head + rest


def _render_tools(tools: dict[str, Tool]) -> str:
    return "\n".join(f"- {t.signature()}: {t.description}" for t in tools.values())


def _render_notes(state: DotState) -> str:
    notes = state.notes()[:15]
    return "\n".join(f"- {n['text']}" for n in notes) if notes else "(まだありません)"


def _observation(value: Any, steps_left: int, max_chars: int) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) > max_chars:
        text = text[:max_chars] + "…(省略)"
    # 小さいモデルは指示の中の回数を数えないので、残りを毎回はっきり伝える
    if steps_left <= 1:
        hint = "次が最後の行動です。必ず finish を呼んで、今回やったことと次回の方針を summary に書いてください。"
    elif steps_left <= 3:
        hint = f"残りの行動は {steps_left} 回です。そろそろ finish の準備をしてください。"
    else:
        hint = f"残りの行動は {steps_left} 回です。"
    return f"OBSERVATION: {text}\n({hint})"


WRAP_UP_PROMPT = "行動の上限に達しました。finish だけを呼んで、今回やったことと次回の方針を summary に書いてください。"


def run_dot(
    dot: DotConfig,
    app: AppConfig,
    model: ChatModel,
    inbox: Inbox,
    *,
    now: datetime | None = None,
    log: Callable[[str], None] = print,
) -> RunResult:
    now = now or now_local()
    state = DotState(app.state_dir, dot.name)
    status = state.load_status()

    answer_text = None
    if status.get("pending_question"):
        q = inbox.get(status["pending_question"])
        if q is None or q["answer"] is None:
            return RunResult(dot.name, "skipped", "人の回答待ち")
        answer_text = f"前回の質問「{q['question']}」への人の回答: {q['answer']}"

    tools = {**BUILTIN_TOOLS, **load_dot_tools(dot.path)}
    ctx = ToolContext(dot=dot, state=state, inbox=inbox, now=now, app=app)
    run_log = state.open_run_log(now)

    system = SYSTEM_TEMPLATE.format(
        name=dot.name,
        description=dot.description or "(説明なし)",
        instructions=dot.instructions or "(指示なし)",
        notes=_render_notes(state),
        tools=_render_tools(tools),
        max_steps=dot.max_steps,
    )
    kickoff = [f"現在時刻: {to_iso(now)}", f"前回の要約: {status.get('last_summary') or '(初回の実行です)'}"]
    if answer_text:
        kickoff.append(answer_text)
    kickoff.append("今回の仕事を始めてください。")
    messages = [{"role": "system", "content": system}, {"role": "user", "content": "\n".join(kickoff)}]
    budget = app.backend.context_tokens - app.backend.max_tokens - 256
    run_log.write("start", system=system, kickoff=messages[1]["content"])

    result = RunResult(dot.name, "step_limit", f"{dot.max_steps} 回の行動で終わりませんでした")
    control: Control | None = None
    bad_replies = 0
    try:
        for step in range(1, dot.max_steps + 1):
            result.steps = step
            reply = model.chat(trim_messages(messages, budget, dot.history_window), temperature=dot.temperature)
            run_log.write("reply", step=step, text=reply)
            messages.append({"role": "assistant", "content": reply})

            action = parse_action(reply)
            if action is None:
                bad_replies += 1
                if bad_replies >= MAX_BAD_REPLIES:
                    raise LLMError(f"JSON の行動が {MAX_BAD_REPLIES} 回続けて読み取れませんでした: {reply[:200]!r}")
                messages.append({"role": "user", "content": 'OBSERVATION: {"error": "JSON の行動が読み取れません。{\\"thought\\": ..., \\"tool\\": ..., \\"args\\": {...}} の形で1つだけ返してください"}'})
                continue

            name, args = action["tool"], action.get("args") or {}
            log(f"[{dot.name}] {step}: {name} {json.dumps(args, ensure_ascii=False)[:120]}")
            if name not in tools:
                bad_replies += 1
                obs: Any = {"error": f"ツール {name} はありません。使えるのは: {', '.join(tools)}"}
            else:
                try:
                    value = tools[name].call(ctx, args)
                    bad_replies = 0
                except ToolArgError as e:
                    bad_replies += 1
                    value = {"error": str(e)}
                except Exception as e:  # ツールの中の失敗は LLM に伝えて判断させる
                    value = {"error": f"{type(e).__name__}: {e}"}
                if isinstance(value, Control):
                    control = value
                    run_log.write("control", kind=value.kind, summary=value.summary)
                    break
                obs = value
            run_log.write("observation", step=step, value=obs)
            messages.append({"role": "user", "content": _observation(obs, dot.max_steps - step, dot.max_observation_chars)})
            if bad_replies >= MAX_BAD_REPLIES:
                raise LLMError(f"ツールの呼び方の誤りが {MAX_BAD_REPLIES} 回続きました")
        else:
            # 上限まで finish しなかった: 要約だけはもらって次回に引き継ぐ
            messages.append({"role": "user", "content": WRAP_UP_PROMPT})
            reply = model.chat(trim_messages(messages, budget, dot.history_window), temperature=dot.temperature)
            run_log.write("reply", step="wrap_up", text=reply)
            action = parse_action(reply)
            if action and action["tool"] == "finish" and isinstance(action["args"].get("summary"), str):
                result.summary = action["args"]["summary"]
    except LLMError as e:
        result.outcome, result.summary = "error", str(e)
        run_log.write("error", message=str(e))

    if control:
        result.outcome, result.summary = control.kind, control.summary

    state.save_data()
    new_status: dict[str, Any] = {
        "status": {"blocked": "blocked", "error": "error"}.get(result.outcome, "idle"),
        "last_run": to_iso(now),
        "last_outcome": result.outcome,
        "last_summary": result.summary if result.outcome != "error" else status.get("last_summary"),
        "last_error": result.summary if result.outcome == "error" else None,
        "next_due": to_iso(control.next_due) if control and control.next_due else None,
        "pending_question": control.question_id if control and control.question_id else None,
    }
    state.save_status(new_status)
    run_log.write("end", outcome=result.outcome, summary=result.summary, steps=result.steps)
    return result
