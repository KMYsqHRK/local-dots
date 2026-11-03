"""robotics-trends が夜のうちに記録した論文から、朝のダイジェストとトレンドマップを書き出すツール。

ファイルの組み立て(論文の一覧・リンク・年表)はコードがやる。LLM が書くのは、今夜のテーマの流れの解説だけ。
"""

import tomllib
from datetime import date, timedelta
from pathlib import Path

from localdots.state import now_local, read_json, to_iso
from localdots.tools import tool

TRENDS_DOT = "robotics-trends"
REPORT_DIR = Path("reports") / "robotics"  # local-dots のフォルダーからの相対パス
SUMMARY_CHARS_FOR_LLM = 110
MAX_PAPERS_FOR_LLM = 25


def _night_label(now) -> str:
    return (now - timedelta(hours=12)).date().isoformat()


def _topics(ctx) -> list[dict]:
    with (ctx.app.dots_dir / TRENDS_DOT / "topics.toml").open("rb") as f:
        return tomllib.load(f)["topic"]


def _trends(ctx) -> dict:
    return read_json(ctx.app.state_dir / TRENDS_DOT / "data.json", {})


def _tonight(ctx) -> tuple[str, dict, list[dict]]:
    label = _night_label(ctx.now)
    t = _trends(ctx)
    papers = [p for p in t.get("papers", {}).values() if p.get("night") == label]
    return label, t, papers


def _stars(n: int) -> str:
    return "★" * n + "☆" * (5 - n)


def _first_sentence(text: str) -> str:
    for mark in ("。", ". "):
        if mark in text:
            return text.split(mark)[0] + mark.strip()
    return text


def _cite(p: dict) -> str:
    bits = [str(p["year"])] if p.get("year") else []
    if p.get("venue"):
        bits.append(p["venue"])
    if p.get("citations"):
        bits.append(f"被引用 {p['citations']}")
    return ", ".join(bits)


@tool
def tonight(ctx) -> dict:
    """今夜 robotics-trends が読んだ、今夜のテーマの論文の一覧(年順・短い要約付き)を返す。"""
    label, t, papers = _tonight(ctx)
    recorded = [p for p in papers if p.get("record") and p["source"] != "new"]
    if not recorded:
        return {"empty": True, "message": f"{label} の夜に記録された論文がありません。finish してください"}
    topic_key = t.get("night_topic")
    label_of = {x["key"]: x["label"] for x in _topics(ctx)}
    recorded.sort(key=lambda p: (p.get("year") or 0, -p.get("citations", 0)))
    return {
        "night": label,
        "topic": label_of.get(topic_key, topic_key),
        "papers": [
            {
                "year": p.get("year"),
                "title": p["title"],
                "importance": p["record"]["importance"],
                "summary": p["record"]["summary"][:SUMMARY_CHARS_FOR_LLM],
            }
            for p in recorded[:MAX_PAPERS_FOR_LLM]
        ],
    }


@tool
def write_overview(ctx, text: str) -> dict:
    """今夜のテーマの流れの解説を保存する(日本語 400〜800 字)。publish の前に1回呼ぶ。"""
    label, t, _ = _tonight(ctx)
    ctx.state.data.setdefault("overviews", {})[label] = {"topic": t.get("night_topic"), "text": text.strip()}
    ctx.state.save_data()
    return {"ok": True, "chars": len(text.strip())}


@tool
def publish(ctx) -> dict:
    """今朝のダイジェストと、これまでの全記録から作ったトレンドマップを Markdown で書き出す。"""
    label, t, papers = _tonight(ctx)
    overview = ctx.state.data.get("overviews", {}).get(label)
    if overview is None:
        return {"error": "先に write_overview で解説を保存してください"}
    out_dir = ctx.app.root / REPORT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    morning = date.fromisoformat(label) + timedelta(days=1)
    digest_path = out_dir / f"digest-{morning.isoformat()}.md"
    digest_path.write_text(_render_digest(ctx, label, t, papers, overview["text"]), encoding="utf-8")
    map_path = out_dir / "trend-map.md"
    map_path.write_text(_render_map(ctx, t), encoding="utf-8")
    return {"ok": True, "digest": str(digest_path), "trend_map": str(map_path)}


def _paper_line(p: dict) -> str:
    r = p["record"]
    return f"- **{p.get('year', '?')}** [{p['title']}]({p['url']}) {_stars(r['importance'])} — {_first_sentence(r['summary'])}"


def _render_digest(ctx, label: str, t: dict, papers: list[dict], overview: str) -> str:
    label_of = {x["key"]: x["label"] for x in _topics(ctx)}
    topic = label_of.get(t.get("night_topic"), t.get("night_topic"))
    recorded = [p for p in papers if p.get("record")]
    theme = sorted((p for p in recorded if p["source"] != "new"), key=lambda p: (p.get("year") or 0, -p.get("citations", 0)))
    new = [p for p in recorded if p["source"] == "new"]
    top3 = sorted(recorded, key=lambda p: (-p["record"]["importance"], -p.get("citations", 0)))[:3]
    skipped = sum(1 for p in papers if p.get("skipped"))

    lines = [f"# ロボティクス論文ダイジェスト({label} の夜)", "", f"今夜のテーマ: **{topic}**(読んだ論文 {len(recorded)} 本)", ""]
    lines += ["## 今日の3本", ""]
    for p in top3:
        r = p["record"]
        lines += [
            f"### [{p['title']}]({p['url']})",
            f"{_cite(p)} / 重要度 {_stars(r['importance'])} / {label_of.get(r['topic'], r['topic'])}",
            "",
            r["summary"],
            "",
            f"**新しさ**: {r['novelty']}",
            "",
        ]
    lines += ["## このテーマの流れ", "", overview, ""]
    if theme:
        lines += ["## 今夜読んだ論文(年順)", ""] + [_paper_line(p) for p in theme] + [""]
    if new:
        lines += ["## cs.RO の新着", ""] + [_paper_line(p) for p in new] + [""]
    if skipped:
        lines += [f"(関係なさそうな論文 {skipped} 本は飛ばしました)", ""]
    lines += ["---", f"robotics-trends / robotics-digest が {to_iso(now_local())} に作成。要約はアブストラクトだけをもとにしています。", ""]
    return "\n".join(lines)


def _render_map(ctx, t: dict) -> str:
    topics = _topics(ctx) + [{"key": "other", "label": "その他"}]
    overviews = ctx.state.data.get("overviews", {})
    by_topic: dict[str, list[dict]] = {}
    for p in t.get("papers", {}).values():
        if p.get("record"):
            by_topic.setdefault(p["record"]["topic"], []).append(p)
    total = sum(len(v) for v in by_topic.values())

    lines = ["# 2020年代ロボティクス トレンドマップ", "", f"これまでに読んだ論文: {total} 本(毎朝更新)", ""]
    for topic in topics:
        ps = by_topic.get(topic["key"])
        if not ps:
            continue
        lines += [f"## {topic['label']}", ""]
        latest = [v["text"] for k, v in sorted(overviews.items()) if v["topic"] == topic["key"]]
        if latest:
            lines += [latest[-1], ""]
        ps.sort(key=lambda p: (p.get("year") or 0, -p["record"]["importance"], -p.get("citations", 0)))
        lines += [_paper_line(p) for p in ps] + [""]
    return "\n".join(lines)
