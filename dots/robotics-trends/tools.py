"""2020年代のロボティクス論文を、一晩に1テーマずつ集めて読むためのツール。

論文の取得・順番・重複の管理はコードが持つ。LLM がするのは、1本ずつアブストラクトを読んで、
要約・テーマ・新しさ・重要度を record_paper で書き込むことだけ。
"""

import json
import os
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import timedelta
from pathlib import Path

from localdots.tools import tool

HERE = Path(__file__).parent
HISTORY_PER_NIGHT = 20  # テーマの 2020年以降の論文を、被引用数の多い順に
RECENT_PER_NIGHT = 5  # 同じテーマの、ここ1〜2年で注目されている論文
NEW_PER_NIGHT = 5  # cs.RO の新着(テーマを問わない)
MAX_ABSTRACT_CHARS = 1600

S2_URL = "https://api.semanticscholar.org/graph/v1/paper/search/bulk"
S2_FIELDS = "paperId,title,year,venue,citationCount,abstract,externalIds,url"
ARXIV_URL = "https://export.arxiv.org/api/query"
USER_AGENT = "local-dots robotics-trends (personal research digest)"
RETRY_WAITS = [5, 15, 30, 60, 90]  # Semantic Scholar はキーなしだと 429 がよく返る

SOURCE_LABELS = {"history": "歴史編(被引用の多い順)", "recent": "最近の注目", "new": "cs.RO の新着"}


def load_topics() -> list[dict]:
    with (HERE / "topics.toml").open("rb") as f:
        return tomllib.load(f)["topic"]


def night_label(now) -> str:
    # 01:00 の実行も 06:00 のレポートも「前の日の夜」にまとめる
    return (now - timedelta(hours=12)).date().isoformat()


def _get(url: str, headers: dict | None = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    for wait in [*RETRY_WAITS, None]:
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if wait is None or not (e.code == 429 or e.code >= 500):
                raise
        except (urllib.error.URLError, TimeoutError):
            if wait is None:
                raise
        time.sleep(wait)
    raise AssertionError("unreachable")


def s2_search(query: str, year: str, limit: int) -> list[dict]:
    params = {"query": query, "year": year, "fields": S2_FIELDS, "sort": "citationCount:desc"}
    headers = {"x-api-key": os.environ["S2_API_KEY"]} if os.environ.get("S2_API_KEY") else None
    data = json.loads(_get(f"{S2_URL}?{urllib.parse.urlencode(params)}", headers))
    papers = []
    for p in data.get("data") or []:
        if not p.get("abstract") or not p.get("title"):
            continue
        arxiv = (p.get("externalIds") or {}).get("ArXiv")
        papers.append({
            "id": f"arxiv:{arxiv}" if arxiv else f"s2:{p['paperId']}",
            "title": p["title"],
            "year": p.get("year"),
            "venue": p.get("venue") or "",
            "citations": p.get("citationCount") or 0,
            "url": f"https://arxiv.org/abs/{arxiv}" if arxiv else p.get("url") or "",
            "abstract": p["abstract"],
        })
        if len(papers) >= limit:
            break
    return papers


def arxiv_recent(limit: int) -> list[dict]:
    params = {"search_query": "cat:cs.RO", "sortBy": "submittedDate", "sortOrder": "descending", "max_results": limit}
    root = ET.fromstring(_get(f"{ARXIV_URL}?{urllib.parse.urlencode(params)}"))
    ns = {"a": "http://www.w3.org/2005/Atom"}
    papers = []
    for e in root.findall("a:entry", ns):
        abs_url = e.findtext("a:id", "", ns).strip()
        arxiv = abs_url.rsplit("/", 1)[-1].split("v")[0] if "/abs/" in abs_url else abs_url
        papers.append({
            "id": f"arxiv:{arxiv}",
            "title": " ".join(e.findtext("a:title", "", ns).split()),
            "year": int(e.findtext("a:published", "0000", ns)[:4]),
            "venue": "arXiv",
            "citations": 0,
            "url": f"https://arxiv.org/abs/{arxiv}",
            "abstract": " ".join(e.findtext("a:summary", "", ns).split()),
        })
    return papers


def _data(ctx) -> dict:
    d = ctx.state.data
    d.setdefault("papers", {})  # id -> 論文と記録
    d.setdefault("queue", [])  # 今夜これから読む id
    d.setdefault("topic_index", -1)
    return d


def _start_night(ctx, d: dict, label: str) -> None:
    # 取得をすべて済ませてから d を書き換える(途中で失敗しても、次に呼ばれたときにやり直せる)
    topics = load_topics()
    picked: list[dict] = []
    topic_index = d["topic_index"]
    if not d["queue"]:  # 前の夜の残りがあれば、同じテーマを続ける
        topic_index = (topic_index + 1) % len(topics)
        topic = topics[topic_index]
        history = s2_search(topic["query"], "2020-", HISTORY_PER_NIGHT * 2)
        recent = s2_search(topic["query"], f"{ctx.now.year - 1}-", RECENT_PER_NIGHT * 2)
        picked = _fresh(d, history, HISTORY_PER_NIGHT, "history", topic["key"], picked)
        picked.sort(key=lambda p: (p["year"] or 0, -p["citations"]))  # 年の順に読むと流れが分かる
        picked += _fresh(d, recent, RECENT_PER_NIGHT, "recent", topic["key"], picked)
    try:
        picked += _fresh(d, arxiv_recent(NEW_PER_NIGHT * 2), NEW_PER_NIGHT, "new", None, picked)
    except (urllib.error.URLError, TimeoutError, ET.ParseError):
        pass  # 新着が取れなくても今夜のテーマは進める
    for p in picked:
        d["papers"][p["id"]] = p
    d["queue"] += [p["id"] for p in picked]
    d["topic_index"] = topic_index
    d["night"] = label
    d["night_topic"] = topics[topic_index]["key"]
    ctx.state.save_data()


def _fresh(d: dict, papers: list[dict], limit: int, source: str, topic_key, taken: list[dict]) -> list[dict]:
    """まだ読んでいない論文を limit 本まで選ぶ(d は書き換えない)。"""
    seen = set(d["papers"]) | {p["id"] for p in taken}
    out = []
    for p in papers:
        if p["id"] in seen:
            continue
        seen.add(p["id"])
        out.append({**p, "source": source, "topic_hint": topic_key, "night": None, "record": None})
        if len(out) >= limit:
            break
    return out


@tool
def next_paper(ctx) -> dict:
    """今夜読む次の論文(題名・年・被引用数・アブストラクト)を1本返す。読み終えたら record_paper か skip_paper を呼ぶ。"""
    d = _data(ctx)
    label = night_label(ctx.now)
    if d.get("night") != label:
        _start_night(ctx, d, label)
    pid = d.get("current")
    if pid and ctx.run.get("served") == pid:
        return {"error": "先に、今の論文について record_paper か skip_paper を呼んでください", "title": d["papers"][pid]["title"]}
    return _serve(ctx, d)


def _serve(ctx, d: dict) -> dict:
    pid = d.get("current")
    if not pid:  # 前の実行が読みかけで終わっていたら、その論文をもう一度渡す
        if not d["queue"]:
            # LLM は直近のやりとりしか見ていないので、今夜の本数はここで数えて渡す
            tonight = [p for p in d["papers"].values() if p.get("night") == d.get("night")]
            recorded = sum(1 for p in tonight if p.get("record"))
            skipped = sum(1 for p in tonight if p.get("skipped"))
            return {
                "done_for_tonight": True,
                "message": f"今夜の分はすべて読み終えました(記録 {recorded} 本、スキップ {skipped} 本)。この本数を summary に書いて finish してください",
            }
        pid = d["current"] = d["queue"].pop(0)
        ctx.state.save_data()
    ctx.run["served"] = pid
    p = d["papers"][pid]
    abstract = p["abstract"] if len(p["abstract"]) <= MAX_ABSTRACT_CHARS else p["abstract"][:MAX_ABSTRACT_CHARS] + "…"
    return {
        "title": p["title"],
        "year": p["year"],
        "venue": p["venue"],
        "citations": p["citations"],
        "source": SOURCE_LABELS[p["source"]],
        "topic_hint": p["topic_hint"],
        "abstract": abstract,
        "remaining_tonight": len(d["queue"]),
    }


def _finish_current(ctx, record: dict | None, skipped: str | None) -> dict:
    d = _data(ctx)
    pid = d.get("current")
    if pid is None or ctx.run.get("served") != pid:
        return {"error": "読んでいる論文がありません。先に next_paper を呼んでください"}
    d["current"] = None
    ctx.run.pop("served", None)
    p = d["papers"][pid]
    p["night"] = d.get("night")
    p["record"] = record
    p["skipped"] = skipped
    ctx.state.save_data()
    # 次の論文もここで渡す(LLM の呼び出しが1本あたり1回で済む)
    return {"ok": True, "next_paper": _serve(ctx, d)}


@tool
def record_paper(ctx, summary: str, topic: str, novelty: str, importance: int) -> dict:
    """今読んだ論文を記録する。summary は日本語3文以内の要約、topic はテーマのキー、novelty はそれまでの研究と比べて何が新しいかを日本語1文、importance はこの分野の流れにとっての重要度 1〜5。"""
    keys = [t["key"] for t in load_topics()] + ["other"]
    if topic not in keys:
        return {"error": f"topic は次のどれかにしてください: {', '.join(keys)}"}
    if not 1 <= importance <= 5:
        return {"error": "importance は 1〜5 の整数にしてください"}
    return _finish_current(ctx, {"summary": summary.strip(), "topic": topic, "novelty": novelty.strip(), "importance": importance}, None)


@tool
def skip_paper(ctx, reason: str) -> dict:
    """今読んだ論文がロボティクスと関係ない、または内容が読み取れないときに飛ばす。"""
    return _finish_current(ctx, None, reason)
