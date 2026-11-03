"""robotics-trends / robotics-digest を、ネットワークと LLM を偽物にして一晩分通すテスト。"""

import io
import json
import shutil
import tempfile
import unittest
import urllib.parse
from datetime import datetime
from pathlib import Path
from unittest import mock

from localdots.agent import run_dot
from localdots.config import AppConfig, BackendConfig, load_dot
from localdots.inbox import Inbox
from localdots.state import DotState

from test_core import JST, ScriptedModel, act

REPO = Path(__file__).resolve().parents[1]


def s2_paper(i, year, cites, arxiv=None):
    return {
        "paperId": f"p{i}", "title": f"Paper {i}", "year": year, "venue": "CoRL", "citationCount": cites,
        "abstract": f"We present method {i} for robot manipulation.", "url": f"https://s2/p{i}",
        "externalIds": {"ArXiv": arxiv} if arxiv else {},
    }


ARXIV_FEED = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><id>http://arxiv.org/abs/2611.00001v1</id><title>New  Robot
  Paper</title><published>2026-11-01T00:00:00Z</published><summary>A new robot paper.</summary></entry>
</feed>"""


def fake_urlopen(req, timeout=None):
    url = req.full_url if hasattr(req, "full_url") else req
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    if "semanticscholar" in url:
        if q["year"] == ["2020-"]:
            data = [s2_paper(1, 2023, 500, "2301.00001"), s2_paper(2, 2021, 900), {"paperId": "x", "title": "no abstract"}]
        else:
            data = [s2_paper(1, 2023, 500, "2301.00001"), s2_paper(3, 2026, 50)]
        body = json.dumps({"total": len(data), "data": data}).encode()
    else:
        body = ARXIV_FEED.encode()
    return io.BytesIO(body)


class RoboticsNightTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for name in ("robotics-trends", "robotics-digest"):
            shutil.copytree(REPO / "dots" / name, self.tmp / "dots" / name)
        self.app = AppConfig(root=self.tmp, backend=BackendConfig(), dots_dir=self.tmp / "dots", state_dir=self.tmp / "state")
        self.inbox = Inbox(self.app.state_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def rec(self, topic="vla", imp=4):
        return act("record_paper", summary="ロボットの方策を提案した。", topic=topic, novelty="新しい。", importance=imp)

    @mock.patch("urllib.request.urlopen", side_effect=fake_urlopen)
    def test_one_night(self, _):
        trends = load_dot(self.tmp / "dots" / "robotics-trends")
        night = datetime(2026, 11, 4, 1, 0, tzinfo=JST)
        model = ScriptedModel([
            self.rec(),                       # 読む前に記録 → エラー
            act("next_paper"), act("next_paper"),  # 2回続けて → エラー(1本目はそのまま)
            self.rec(imp=3),                  # 2021 Paper 2(年の順)。結果に次の論文が入っている
            self.rec(topic="nope"), self.rec(imp=5),  # 2023 Paper 1、topic の誤り → 出し直し
            act("skip_paper", reason="関係ない"),     # 2026 Paper 3(最近の注目)
            self.rec(topic="other", imp=2),           # arXiv 新着 → 結果は done_for_tonight
            act("finish", summary="vla を 3 本"),
        ])
        r = run_dot(trends, self.app, model, self.inbox, now=night)
        self.assertEqual(r.outcome, "done")
        d = DotState(self.app.state_dir, "robotics-trends").data
        self.assertEqual((d["night"], d["night_topic"], d["queue"]), ("2026-11-03", "llm-planning", []))
        self.assertEqual(d["papers"]["arxiv:2301.00001"]["record"]["importance"], 5)
        self.assertEqual(d["papers"]["s2:p3"]["skipped"], "関係ない")
        self.assertEqual(d["papers"]["arxiv:2611.00001"]["title"], "New Robot Paper")
        first_obs = model.seen[2][-1]["content"]
        self.assertIn("Paper 2", first_obs)  # 被引用の多い順ではなく年の順
        self.assertIn("done_for_tonight", model.seen[-1][-1]["content"])
        self.assertIn("記録 3 本、スキップ 1 本", model.seen[-1][-1]["content"])

        digest = load_dot(self.tmp / "dots" / "robotics-digest")
        morning = datetime(2026, 11, 4, 6, 0, tzinfo=JST)
        model = ScriptedModel([
            act("publish"),  # 解説より先 → エラー
            act("tonight"),
            act("write_overview", text="2021年の Paper 2 から 2023年の Paper 1 へ。"),
            act("publish"),
            act("finish", summary="書き出した"),
        ])
        self.assertEqual(run_dot(digest, self.app, model, self.inbox, now=morning).outcome, "done")
        self.assertGreaterEqual(digest.max_observation_chars, 6000)  # 一覧が途中で切れないこと
        tonight_obs = json.loads(model.seen[2][-1]["content"].split("OBSERVATION: ", 1)[1].split("\n")[0])
        self.assertEqual([p["title"] for p in tonight_obs["papers"]], ["Paper 2", "Paper 1"])  # 新着とスキップは除く

        text = (self.tmp / "reports" / "robotics" / "digest-2026-11-04.md").read_text(encoding="utf-8")
        self.assertIn("LLMによるタスク計画", text)
        self.assertIn("[Paper 1](https://arxiv.org/abs/2301.00001)", text)
        self.assertIn("## cs.RO の新着", text)
        self.assertIn("2021年の Paper 2", text)
        self.assertIn("1 本は飛ばしました", text)
        trend_map = (self.tmp / "reports" / "robotics" / "trend-map.md").read_text(encoding="utf-8")
        self.assertIn("## VLA・ロボット基盤モデル", trend_map)
        self.assertIn("## その他", trend_map)
        self.assertLess(trend_map.index("Paper 2"), trend_map.index("Paper 1"))

    @mock.patch("urllib.request.urlopen", side_effect=fake_urlopen)
    def test_unfinished_paper_is_served_again_next_run(self, _):
        trends = load_dot(self.tmp / "dots" / "robotics-trends")
        trends.max_steps = 1
        night = datetime(2026, 11, 4, 1, 0, tzinfo=JST)
        run_dot(trends, self.app, ScriptedModel([act("next_paper"), act("finish", summary="途中")]), self.inbox, now=night)
        model = ScriptedModel([act("next_paper"), act("finish", summary="x")])
        run_dot(trends, self.app, model, self.inbox, now=night.replace(hour=2))
        self.assertIn("Paper 2", model.seen[1][-2]["content"])  # [-1] は上限に達したときの締めの指示

    def test_instructions_list_every_topic(self):
        from localdots.tools import load_dot_tools  # noqa: F401  (tools.py が読み込めることも確かめる)
        import tomllib

        topics = tomllib.loads((REPO / "dots" / "robotics-trends" / "topics.toml").read_text(encoding="utf-8"))["topic"]
        instructions = (REPO / "dots" / "robotics-trends" / "instructions.md").read_text(encoding="utf-8")
        for t in topics:
            self.assertIn(f"- {t['key']}: {t['label']}", instructions)
        self.assertEqual(set(load_dot_tools(REPO / "dots" / "robotics-trends")), {"next_paper", "record_paper", "skip_paper"})


if __name__ == "__main__":
    unittest.main()
