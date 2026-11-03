"""LLM を使わずに枠組みを確かめるテスト。python -m unittest discover -s tests"""

import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from localdots.agent import parse_action, run_dot, trim_messages
from localdots.config import BackendConfig, AppConfig, ConfigError, Schedule, load_dot, parse_duration
from localdots.inbox import Inbox
from localdots.scheduler import in_window, is_due
from localdots.state import DotState, to_iso
from localdots.tools import ToolArgError, tool, tool_from_func

REPO = Path(__file__).resolve().parents[1]
JST = datetime.now().astimezone().tzinfo


class ScriptedModel:
    """台本の応答を順に返す偽の LLM。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []

    def chat(self, messages, *, temperature=None):
        self.seen.append(messages)
        return self.replies.pop(0)


def act(tool_name, **args):
    return json.dumps({"thought": "t", "tool": tool_name, "args": args}, ensure_ascii=False)


class TempApp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        shutil.copytree(REPO / "dots" / "param-sweep-demo", self.tmp / "dots" / "param-sweep-demo")
        self.app = AppConfig(root=self.tmp, backend=BackendConfig(), dots_dir=self.tmp / "dots", state_dir=self.tmp / "state")
        self.dot = load_dot(self.tmp / "dots" / "param-sweep-demo")
        self.dot.enabled = True  # リポジトリでは定期実行を切ってあるサンプル
        self.inbox = Inbox(self.app.state_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class ParseActionTest(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(parse_action('{"tool": "finish", "args": {"summary": "ok"}}')["tool"], "finish")

    def test_fenced_with_prose(self):
        text = '考えます。\n```json\n{"thought": "x", "tool": "note", "args": {"text": "a{b}"}}\n```'
        self.assertEqual(parse_action(text)["args"]["text"], "a{b}")

    def test_skips_objects_without_tool(self):
        self.assertEqual(parse_action('{"a": 1} then {"tool": "finish"}')["args"], {})

    def test_none(self):
        self.assertIsNone(parse_action("JSON がありません"))


class TrimTest(unittest.TestCase):
    def test_keeps_head_and_drops_oldest(self):
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "k"}]
        for i in range(20):
            msgs += [{"role": "assistant", "content": f"a{i}" * 50}, {"role": "user", "content": f"o{i}" * 50}]
        out = trim_messages(msgs, 400)
        self.assertEqual(out[:2], msgs[:2])
        self.assertIn("省きました", out[2]["content"])
        self.assertEqual(out[-1], msgs[-1])
        self.assertLess(len(out), len(msgs))

    def test_window_keeps_only_recent_pairs(self):
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "k"}]
        for i in range(5):
            msgs += [{"role": "assistant", "content": f"a{i}"}, {"role": "user", "content": f"o{i}"}]
        out = trim_messages(msgs, 100_000, window=2)
        self.assertEqual([m["content"] for m in out[3:]], ["a3", "o3", "a4", "o4"])
        self.assertEqual(len(trim_messages(msgs[:4], 100_000, window=2)), 4)  # 窓に収まっていれば何もしない


class ToolTest(unittest.TestCase):
    def test_signature_and_conversion(self):
        @tool
        def f(ctx, x: float, n: int = 2, flag: bool = False) -> dict:
            """説明の1行目。

            ここは説明に入らない。"""
            return {"x": x, "n": n, "flag": flag}

        t = tool_from_func(f)
        self.assertEqual(t.signature(), "f(x: float, n: int = 2, flag: bool = False)")
        self.assertEqual(t.description, "説明の1行目。")
        self.assertEqual(t.call(None, {"x": "1.5", "flag": "true"}), {"x": 1.5, "n": 2, "flag": True})
        with self.assertRaises(ToolArgError):
            t.call(None, {})
        with self.assertRaises(ToolArgError):
            t.call(None, {"x": 1, "z": 3})


class ScheduleTest(TempApp):
    def test_duration(self):
        self.assertEqual(parse_duration("30m"), 1800)
        with self.assertRaises(ConfigError):
            parse_duration("30 minutes")

    def test_window_across_midnight(self):
        self.assertTrue(in_window(("22:00", "06:00"), datetime(2026, 1, 1, 23, 0)))
        self.assertTrue(in_window(("22:00", "06:00"), datetime(2026, 1, 1, 5, 0)))
        self.assertFalse(in_window(("22:00", "06:00"), datetime(2026, 1, 1, 12, 0)))

    def test_every(self):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=JST)
        self.assertTrue(is_due(self.dot, {}, self.inbox, now)[0])
        status = {"last_run": to_iso(now - timedelta(minutes=10))}
        self.assertFalse(is_due(self.dot, status, self.inbox, now)[0])
        status = {"last_run": to_iso(now - timedelta(minutes=31))}
        self.assertTrue(is_due(self.dot, status, self.inbox, now)[0])

    def test_daily_at(self):
        self.dot.schedule = Schedule(at="02:00")
        now = datetime(2026, 1, 2, 3, 0, tzinfo=JST)
        self.assertTrue(is_due(self.dot, {"last_run": to_iso(now - timedelta(days=1))}, self.inbox, now)[0])
        self.assertFalse(is_due(self.dot, {"last_run": to_iso(now - timedelta(minutes=30))}, self.inbox, now)[0])
        self.assertFalse(is_due(self.dot, {}, self.inbox, now.replace(hour=1))[0])

    def test_snooze_overrides_every(self):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=JST)
        status = {"last_run": to_iso(now - timedelta(hours=5)), "next_due": to_iso(now + timedelta(minutes=5))}
        self.assertFalse(is_due(self.dot, status, self.inbox, now)[0])
        self.assertTrue(is_due(self.dot, status, self.inbox, now + timedelta(minutes=6))[0])


class RunDotTest(TempApp):
    def test_param_sweep_run(self):
        model = ScriptedModel([
            act("history"),
            "すみません、こう返します: " + act("evaluate", x=0, y=0),
            act("evaluate", x=1.3, y=-0.7),
            act("note", text="(1.3,-0.7) 付近が良い"),
            act("finish", summary="2点評価した。次は (1.3,-0.7) の周り"),
        ])
        r = run_dot(self.dot, self.app, model, self.inbox)
        self.assertEqual((r.outcome, r.steps), ("done", 5))
        state = DotState(self.app.state_dir, self.dot.name)
        self.assertEqual(len(state.data["evals"]), 2)
        self.assertLess(state.data["evals"][1]["loss"], state.data["evals"][0]["loss"])
        self.assertEqual(state.notes()[0]["text"], "(1.3,-0.7) 付近が良い")
        status = state.load_status()
        self.assertEqual(status["last_summary"], "2点評価した。次は (1.3,-0.7) の周り")
        # 次の実行には前回の要約と記憶が渡る
        model2 = ScriptedModel([act("finish", summary="何もしない")])
        run_dot(self.dot, self.app, model2, self.inbox)
        self.assertIn("(1.3,-0.7) 付近が良い", model2.seen[0][0]["content"])
        self.assertIn("2点評価した", model2.seen[0][1]["content"])

    def test_bad_replies_then_recovery(self):
        model = ScriptedModel(["JSON じゃない", act("nope"), act("evaluate", x="abc", y=0), act("finish", summary="ok")])
        r = run_dot(self.dot, self.app, model, self.inbox)
        self.assertEqual(r.outcome, "error")  # 3回続けて誤り

        model = ScriptedModel(["JSON じゃない", act("finish", summary="ok")])
        self.assertEqual(run_dot(self.dot, self.app, model, self.inbox).outcome, "done")

    def test_ask_human_blocks_until_answered(self):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=JST)
        model = ScriptedModel([act("ask_human", question="範囲を広げますか?", options=["はい", "いいえ"])])
        r = run_dot(self.dot, self.app, model, self.inbox, now=now)
        self.assertEqual(r.outcome, "blocked")
        state = DotState(self.app.state_dir, self.dot.name)
        status = state.load_status()
        self.assertEqual(is_due(self.dot, status, self.inbox, now + timedelta(hours=3))[1], "人の回答待ち")
        self.assertEqual(run_dot(self.dot, self.app, ScriptedModel([]), self.inbox).outcome, "skipped")

        self.inbox.answer(status["pending_question"], "いいえ、そのままで")
        self.assertTrue(is_due(self.dot, state.load_status(), self.inbox, now + timedelta(minutes=1))[0])
        model = ScriptedModel([act("finish", summary="範囲はそのまま")])
        self.assertEqual(run_dot(self.dot, self.app, model, self.inbox).outcome, "done")
        self.assertIn("いいえ、そのままで", model.seen[0][1]["content"])
        self.assertIsNone(state.load_status()["pending_question"])

    def test_snooze_sets_next_due(self):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=JST)
        run_dot(self.dot, self.app, ScriptedModel([act("snooze", minutes=90, reason="待つ")]), self.inbox, now=now)
        status = DotState(self.app.state_dir, self.dot.name).load_status()
        self.assertEqual(status["next_due"], to_iso(now + timedelta(minutes=90)))

    def test_step_limit_still_gets_summary(self):
        self.dot.max_steps = 2
        model = ScriptedModel([act("history"), act("history"), act("finish", summary="続きは次回")])
        r = run_dot(self.dot, self.app, model, self.inbox)
        self.assertEqual((r.outcome, r.steps, r.summary), ("step_limit", 2, "続きは次回"))
        self.assertIn("最後の行動", model.seen[1][-1]["content"])
        self.assertEqual(DotState(self.app.state_dir, self.dot.name).load_status()["last_summary"], "続きは次回")

    def test_per_run_limit_is_enforced_by_tool(self):
        model = ScriptedModel([act("evaluate", x=i, y=0) for i in range(5)] + [act("finish", summary="ok")])
        run_dot(self.dot, self.app, model, self.inbox)
        state = DotState(self.app.state_dir, self.dot.name)
        self.assertEqual(len(state.data["evals"]), 4)
        self.assertIn("4 回まで", model.seen[5][-1]["content"])


if __name__ == "__main__":
    unittest.main()
