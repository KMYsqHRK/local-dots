"""robotics-trends / robotics-digest を、ネットワーク・LLM・メールを偽物にして通すテスト。"""

import importlib.util
import io
import json
import os
import shutil
import tempfile
import tomllib
import unittest
import urllib.parse
import zlib
from datetime import datetime
from pathlib import Path
from unittest import mock

from localdots.agent import run_dot
from localdots.config import AppConfig, BackendConfig, load_dot
from localdots.inbox import Inbox
from localdots.mail import markdown_to_html
from localdots.state import DotState, write_json
from localdots.tools import ToolContext, load_dot_tools

from test_core import JST, ScriptedModel, act

REPO = Path(__file__).resolve().parents[1]
MAIL_ENV = {"LOCALDOTS_SMTP_USER": "me@example.com", "LOCALDOTS_SMTP_PASSWORD": "x"}


def s2_paper(tag, i, year, cites, arxiv=None):
    return {
        "paperId": f"{tag}{i}", "title": f"Paper {tag}{i}", "year": year, "venue": "ICRA", "citationCount": cites,
        "abstract": f"We present method {tag}{i} for robots.", "url": f"https://s2/{tag}{i}",
        "externalIds": {"ArXiv": arxiv} if arxiv else {},
    }


def fake_urlopen(req, timeout=None):
    url = req.full_url
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    if "semanticscholar" in url:
        h = zlib.crc32(q["query"][0].encode()) % 1000
        tag = f"q{h}-"  # step ごとに別の論文が返る
        if q["year"][0] != f"{datetime.now().year - 1}-":
            data = [s2_paper(tag, 1, 2017, 500), s2_paper(tag, 2, 2012, 900, f"1201.{h:05d}"), {"paperId": "x", "title": "no abstract"}]
        else:
            data = [s2_paper(tag, 1, 2017, 500), s2_paper(tag, 3, 2026, 50)]
        return io.BytesIO(json.dumps({"total": len(data), "data": data}).encode())
    n = zlib.crc32(q["search_query"][0].encode()) % 100000
    return io.BytesIO(f"""<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><id>http://arxiv.org/abs/2611.{n:05d}v1</id><title>New  Robot
  Paper</title><published>2026-11-01T00:00:00Z</published><summary>A new robot paper.</summary></entry>
</feed>""".encode())


def rec(imp=4):
    return act("record_paper", summary="ロボットの方法を提案した。", novelty="新しい。", importance=imp)


class RoboticsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for name in ("robotics-trends", "robotics-digest"):
            shutil.copytree(REPO / "dots" / name, self.tmp / "dots" / name)
        self.app = AppConfig(root=self.tmp, backend=BackendConfig(), dots_dir=self.tmp / "dots", state_dir=self.tmp / "state")
        self.inbox = Inbox(self.app.state_dir)
        self.trends = load_dot(self.tmp / "dots" / "robotics-trends")
        self.digest = load_dot(self.tmp / "dots" / "robotics-digest")
        self.digest.settings["report_dir"] = str(self.tmp / "out")  # 本物の保管庫に書かない
        self.sent = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def read_today(self, when):
        model = ScriptedModel([
            act("next_paper"),
            rec(imp=5),                          # 2012 Paper 2(年の順)
            rec(imp=3),                          # 2017 Paper 1
            act("skip_paper", reason="関係ない"),  # 2026 Paper 3(最近の注目)
            rec(imp=2),                          # arXiv 新着 → 結果は done_for_today
            act("finish", summary="3 本"),
        ])
        r = run_dot(self.trends, self.app, model, self.inbox, now=when)
        self.assertEqual(r.outcome, "done")
        return model

    def write_digest(self, when):
        smtp = mock.MagicMock()
        smtp.return_value.__enter__.return_value.send_message.side_effect = self.sent.append
        model = ScriptedModel([
            act("publish"),  # 解説より先 → エラー
            act("tonight"),
            act("write_overview", text="古い定番から始まった。\n\n今は学習に向かっている。"),
            act("publish"),
            act("finish", summary="書き出した"),
        ])
        with mock.patch.dict(os.environ, MAIL_ENV), mock.patch("smtplib.SMTP_SSL", smtp):
            self.assertEqual(run_dot(self.digest, self.app, model, self.inbox, now=when).outcome, "done")
        return model

    @mock.patch("urllib.request.urlopen", side_effect=fake_urlopen)
    def test_weekday_track_and_next_week_continues(self, _):
        tue = datetime(2026, 11, 10, 3, 0, tzinfo=JST)
        model = self.read_today(tue)
        self.assertIn("Paper q", model.seen[1][-1]["content"])
        self.assertIn("2012", model.seen[1][-1]["content"])  # 被引用の多い順ではなく年の順
        self.assertIn("記録 3 本、スキップ 1 本", model.seen[-1][-1]["content"])
        self.assertIn("認識(リコグニション) / 深層学習による物体検出の基礎", model.seen[1][-1]["content"])  # 論文と一緒に今日の分野を渡す
        self.assertIn("分野全体の新着", model.seen[4][-1]["content"])  # 新着は段階でなく分野で判断させる
        d = DotState(self.app.state_dir, "robotics-trends").data
        self.assertEqual(d["sessions"][-1] | {}, {
            "label": "2026-11-10", "track": "recognition", "step": "deep-detection", "step_number": 1, "round": 1, "finished": True,
        })
        self.assertEqual(d["progress"], {"recognition": 1})

        model = self.write_digest(tue.replace(hour=5))
        tonight_obs = json.loads(model.seen[2][-1]["content"].split("OBSERVATION: ", 1)[1].split("\n")[0])
        self.assertEqual([p["year"] for p in tonight_obs["papers"]], [2012, 2017])  # 新着とスキップは除く
        self.assertEqual(tonight_obs["step"], "深層学習による物体検出の基礎")
        out = self.tmp / "out"
        rec_dir = out / "2_認識"
        text = (rec_dir / "2026-11-10 深層学習による物体検出の基礎.md").read_text(encoding="utf-8")
        self.assertIn("# 認識(リコグニション) 1/7: 深層学習による物体検出の基礎", text)
        self.assertIn("は今日から始まります", text)
        self.assertIn("- 来週: セグメンテーション", text)
        self.assertIn("1 本は飛ばしました", text)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["Subject"], "[ロボティクス] 11/10(火) 認識(リコグニション) 1/7: 深層学習による物体検出の基礎")
        self.assertEqual(self.sent[0]["To"], "me@example.com")
        roadmap = (out / "ロードマップ.md").read_text(encoding="utf-8")
        self.assertIn("## 火曜: 認識(リコグニション)(1/7)", roadmap)
        self.assertIn("## お休み中: 2020年代の潮流(0/10)", roadmap)
        self.assertIn("✅ 1. 深層学習による物体検出の基礎(11/10(火))", roadmap)
        self.assertIn("▶ 2. セグメンテーション", roadmap)

        # 曜日ごとに別の分野。木曜はハードとセンサーを交互に読む
        for day, track, step in [(11, "slam", "probabilistic-filtering"), (12, "sensors-hardware", "joints-actuators"),
                                 (13, "simulation", "physics-engines")]:
            self.read_today(datetime(2026, 11, day, 3, 0, tzinfo=JST))
            d = DotState(self.app.state_dir, "robotics-trends").data
            self.assertEqual((d["sessions"][-1]["track"], d["sessions"][-1]["step"]), (track, step), day)
        # 木曜は2つのテーマ(part)から等分に読む
        thu = [p for p in d["papers"].values() if p["session"] == "2026-11-12" and p["source"] == "history"]
        self.assertEqual(len({p["title"].split("-")[0] for p in thu}), 2)  # 題名 "Paper q<クエリ>-<番号>"

        # 翌週の火曜は、先週の続きの段階
        next_tue = datetime(2026, 11, 17, 3, 0, tzinfo=JST)
        self.read_today(next_tue)
        d = DotState(self.app.state_dir, "robotics-trends").data
        self.assertEqual((d["sessions"][-1]["step"], d["progress"]["recognition"]), ("segmentation", 2))
        self.write_digest(next_tue.replace(hour=5))
        text = (rec_dir / "2026-11-17 セグメンテーション.md").read_text(encoding="utf-8")
        self.assertIn("前回(11/10(火))は「深層学習による物体検出の基礎」を読みました。", text)
        self.assertIn("> 今は学習に向かっている。", text)
        self.assertIn("今回はその次の段階、「セグメンテーション」です。", text)
        self.assertIn("## 深層学習による物体検出の基礎", (rec_dir / "年表.md").read_text(encoding="utf-8"))

    @mock.patch("urllib.request.urlopen", side_effect=fake_urlopen)
    def test_unfinished_session_continues_same_day_and_is_dropped_next_session(self, _):
        self.trends.max_steps = 1
        tue = datetime(2026, 11, 10, 3, 0, tzinfo=JST)
        run_dot(self.trends, self.app, ScriptedModel([act("next_paper"), act("finish", summary="途中")]), self.inbox, now=tue)
        model = ScriptedModel([act("next_paper"), act("finish", summary="x")])
        run_dot(self.trends, self.app, model, self.inbox, now=tue.replace(hour=4))
        self.assertIn("2012", model.seen[1][-2]["content"])  # 読みかけの論文がもう一度来る([-1] は締めの指示)
        d = DotState(self.app.state_dir, "robotics-trends").data
        left = set(d["queue"]) | {d["current"]}
        self.assertEqual(d["progress"], {})  # 読み終えていないので進まない

        run_dot(self.trends, self.app, ScriptedModel([act("next_paper"), act("finish", summary="x")]), self.inbox,
                now=datetime(2026, 11, 11, 3, 0, tzinfo=JST))
        d = DotState(self.app.state_dir, "robotics-trends").data
        self.assertFalse(left & set(d["papers"]))  # 火曜の読み残しは「まだ読んでいない」に戻る

    def test_migrates_v1_data(self):
        write_json(self.app.state_dir / "robotics-trends" / "data.json", {
            "papers": {"arxiv:1": {"id": "arxiv:1", "title": "Old", "source": "history", "topic_hint": "llm-planning",
                                   "night": "2026-11-02", "record": {"summary": "s", "topic": "vla", "novelty": "n", "importance": 5}}},
            "queue": [], "topic_index": 0, "night": "2026-11-02", "night_topic": "llm-planning",
        })
        spec = importlib.util.spec_from_file_location("trends_tools", self.tmp / "dots" / "robotics-trends" / "tools.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        state = DotState(self.app.state_dir, "robotics-trends")
        ctx = ToolContext(dot=self.trends, state=state, inbox=self.inbox, now=datetime(2026, 11, 10, 3, 0, tzinfo=JST), app=self.app)
        d = mod._data(ctx)
        self.assertEqual(d["progress"], {"trends": 1})  # llm-planning は読み終えている
        self.assertEqual(d["papers"]["arxiv:1"]["session"], "2026-11-02")
        self.assertEqual(d["papers"]["arxiv:1"]["step"], "llm-planning")
        self.assertNotIn("topic", d["papers"]["arxiv:1"]["record"])
        self.assertEqual(d["sessions"][0]["track"], "trends")

    def test_tracks_are_well_formed(self):
        tracks = tomllib.loads((REPO / "dots" / "robotics-trends" / "tracks.toml").read_text(encoding="utf-8"))["track"]
        # 平日の各曜日・奇数週/偶数週に、分野がちょうど1つずつ(weekday の無い分野はお休み中)
        for wd in ["mon", "tue", "wed", "thu", "fri"]:
            for parity in ["odd", "even"]:
                hits = [t["key"] for t in tracks if t.get("weekday") == wd and t.get("week", parity) == parity]
                self.assertEqual(len(hits), 1, (wd, parity, hits))
        self.assertEqual([t["key"] for t in tracks if not t.get("weekday")], ["trends"])
        keys = [(t["key"], s["key"]) for t in tracks for s in t["step"]]
        self.assertEqual(len(keys), len(set(keys)))
        for t in tracks:
            self.assertTrue(t["arxiv"])
            for s in t["step"]:
                self.assertTrue(s["label"] and s["aim"], s["key"])
                for p in s.get("part") or [s]:
                    self.assertTrue(p["query"] and p["year"].endswith("-"), s["key"])
        # 分野ごとの段階の数がそろっている(同じころに読み終わる)
        self.assertLessEqual(max(len(t["step"]) for t in tracks if t.get("weekday")), 8)
        self.assertEqual(set(load_dot_tools(REPO / "dots" / "robotics-trends")), {"next_paper", "record_paper", "skip_paper"})

    def test_markdown_to_html(self):
        h = markdown_to_html("# 見出し\n\n本文 **太字** [リンク](https://a.b/c?d=1&e=2)\n\n- 1つ目\n- 2つ目\n\n---\n<script>")
        self.assertIn("<h1>見出し</h1>", h)
        self.assertIn("<b>太字</b>", h)
        self.assertIn('<a href="https://a.b/c?d=1&amp;e=2">リンク</a>', h)
        self.assertIn("<ul>\n<li>1つ目</li>\n<li>2つ目</li>\n</ul>", h)
        self.assertIn("&lt;script&gt;", h)


if __name__ == "__main__":
    unittest.main()
