"""ロボティクスの論文を、曜日ごとの分野(track)で1晩に1 step ずつ読み進めるためのツール。

どの分野のどの step を読むか、論文の取得・順番・重複の管理はコードが持つ(tracks.toml)。
LLM がするのは、1本ずつアブストラクトを読んで、要約・新しさ・重要度を record_paper で書き込むことだけ。
"""

import json
import os
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path

from localdots.tools import tool

HERE = Path(__file__).parent
HISTORY_PER_NIGHT = 20  # その step の論文を、被引用数の多い順に(読むのは年の順)
RECENT_PER_NIGHT = 5  # 同じ step の、ここ1〜2年で注目されている論文
NEW_PER_NIGHT = 5  # その分野の arXiv 新着
MAX_ABSTRACT_CHARS = 1600

S2_URL = "https://api.semanticscholar.org/graph/v1/paper/search/bulk"
S2_FIELDS = "paperId,title,year,venue,citationCount,abstract,externalIds,url"
ARXIV_URL = "https://export.arxiv.org/api/query"
USER_AGENT = "local-dots robotics-trends (personal research digest)"
RETRY_WAITS = [5, 15, 30, 60, 90]  # Semantic Scholar はキーなしだと 429 がよく返る
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

SOURCE_LABELS = {"history": "定番(被引用の多い順)", "recent": "最近の注目", "new": "arXiv の新着"}


def load_tracks() -> list[dict]:
    with (HERE / "tracks.toml").open("rb") as f:
        return tomllib.load(f)["track"]


def session_label(now) -> str:
    """その回のレポートが届く朝の日付。03:00 の読み込みも 05:00 のレポートも同じ日になる。"""
    return (now + timedelta(hours=6)).date().isoformat()


def week_parity(d: date) -> str:
    return "odd" if d.isocalendar().week % 2 else "even"


def track_for(label: str, tracks: list[dict], progress: dict) -> dict:
    day = date.fromisoformat(label)
    wd = WEEKDAYS[day.weekday()]
    scheduled = [t for t in tracks if t.get("weekday")]  # weekday の無い分野はお休み中
    for t in scheduled:
        if t["weekday"] == wd and t.get("week", week_parity(day)) == week_parity(day):
            return t
    # 曜日の決まっていない日(週末に手で動かしたときなど)は、いちばん遅れている分野
    return min(scheduled, key=lambda t: progress.get(t["key"], 0) / len(t["step"]))


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


def arxiv_search(search_query: str, limit: int) -> list[dict]:
    params = {"search_query": search_query, "sortBy": "submittedDate", "sortOrder": "descending", "max_results": limit}
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
    if "topic_index" in d:  # 曜日ごとの分野にする前(2020年代の潮流だけを読んでいた頃)のデータ
        _migrate_v1(d)
    d.setdefault("papers", {})  # id -> 論文と記録
    d.setdefault("queue", [])  # この回にこれから読む id
    d.setdefault("progress", {})  # track -> 次に読む step の番号
    d.setdefault("sessions", [])  # これまでの回(新しいものが後ろ)
    return d


def _migrate_v1(d: dict) -> None:
    done = {p.get("topic_hint") for p in d["papers"].values() if p.get("record") and p.get("source") != "new"}
    steps = [s["key"] for s in next(t for t in load_tracks() if t["key"] == "trends")["step"]]
    for p in d["papers"].values():
        p["track"] = "trends"
        p["step"] = p.pop("topic_hint", None) or "new"
        p["session"] = p.pop("night", None)
        if p.get("record"):
            p["record"].pop("topic", None)
    d["progress"] = {"trends": sum(1 for s in steps if s in done)}
    if d.get("night"):
        d["sessions"] = [{"label": d["night"], "track": "trends", "step": d.get("night_topic"), "finished": True}]
    for k in ("topic_index", "night", "night_topic"):
        d.pop(k, None)


def _start_session(ctx, d: dict, label: str) -> None:
    # 取得をすべて済ませてから d を書き換える(途中で失敗しても、次に呼ばれたときにやり直せる)
    tracks = load_tracks()
    track = track_for(label, tracks, d["progress"])
    steps = track["step"]
    index = d["progress"].get(track["key"], 0)
    step = steps[index % len(steps)]  # 最後まで読んだら最初に戻る(まだ読んでいない論文だけが来る)
    # 1つの step に複数のテーマ(part)があるときは、本数を等分する
    parts = step.get("part") or [step]
    per_history = -(-HISTORY_PER_NIGHT // len(parts))
    per_recent = -(-RECENT_PER_NIGHT // len(parts))
    fetched = [
        (s2_search(p["query"], p.get("year", "2020-"), per_history * 2), s2_search(p["query"], f"{ctx.now.year - 1}-", per_recent * 2))
        for p in parts
    ]

    # 前の回の読み残しは、まだ読んでいないことにして戻す(別の曜日の分野なので、ここでは読まない)
    for pid in [*d["queue"], *([d["current"]] if d.get("current") else [])]:
        d["papers"].pop(pid, None)
    picked: list[dict] = []
    for history, _ in fetched:
        picked += _fresh(d, history, per_history, "history", track, step, picked)
    picked.sort(key=lambda p: (p["year"] or 0, -p["citations"]))  # 年の順に読むと流れが分かる
    for _, recent in fetched:
        picked += _fresh(d, recent, per_recent, "recent", track, step, picked)
    try:
        picked += _fresh(d, arxiv_search(track["arxiv"], NEW_PER_NIGHT * 2), NEW_PER_NIGHT, "new", track, None, picked)
    except (urllib.error.URLError, TimeoutError, ET.ParseError):
        pass  # 新着が取れなくても、今日の step は進める
    for p in picked:
        d["papers"][p["id"]] = p
    d["queue"] = [p["id"] for p in picked]
    d["current"] = None
    d["sessions"].append({
        "label": label,
        "track": track["key"],
        "step": step["key"],
        "step_number": index % len(steps) + 1,
        "round": index // len(steps) + 1,
        "finished": False,
    })
    ctx.state.save_data()


def _fresh(d: dict, papers: list[dict], limit: int, source: str, track: dict, step, taken: list[dict]) -> list[dict]:
    """まだ読んでいない論文を limit 本まで選ぶ(d は書き換えない)。"""
    seen = set(d["papers"]) | {p["id"] for p in taken}
    out = []
    for p in papers:
        if p["id"] in seen:
            continue
        seen.add(p["id"])
        out.append({
            **p,
            "source": source,
            "track": track["key"],
            "step": step["key"] if step else "new",
            "session": None,
            "record": None,
        })
        if len(out) >= limit:
            break
    return out


def _session(d: dict, label: str) -> dict | None:
    return next((s for s in reversed(d["sessions"]) if s["label"] == label), None)


@tool
def next_paper(ctx) -> dict:
    """今日読む次の論文(題名・年・被引用数・アブストラクト)を1本返す。最初に1回だけ呼ぶ。"""
    d = _data(ctx)
    label = session_label(ctx.now)
    if _session(d, label) is None:
        _start_session(ctx, d, label)
    pid = d.get("current")
    if pid and ctx.run.get("served") == pid:
        return {"error": "先に、今の論文について record_paper か skip_paper を呼んでください", "title": d["papers"][pid]["title"]}
    return _serve(ctx, d)


def _serve(ctx, d: dict) -> dict:
    pid = d.get("current")
    if not pid:  # 前の実行が読みかけで終わっていたら、その論文をもう一度渡す
        if not d["queue"]:
            return _done(ctx, d)
        pid = d["current"] = d["queue"].pop(0)
        ctx.state.save_data()
    ctx.run["served"] = pid
    p = d["papers"][pid]
    abstract = p["abstract"] if len(p["abstract"]) <= MAX_ABSTRACT_CHARS else p["abstract"][:MAX_ABSTRACT_CHARS] + "…"
    return {
        # 直近のやりとりしか見ていないので毎回渡す。新着は今日の段階ではなく、分野全体の論文として読む
        "today": _today_context(d, session_label(ctx.now), whole_track=p["source"] == "new"),
        "title": p["title"],
        "year": p["year"],
        "venue": p["venue"],
        "citations": p["citations"],
        "source": SOURCE_LABELS[p["source"]],
        "abstract": abstract,
        "remaining_today": len(d["queue"]),
    }


def _today_context(d: dict, label: str, whole_track: bool = False) -> str:
    s = _session(d, label)
    if s is None:
        return ""
    track = next(t for t in load_tracks() if t["key"] == s["track"])
    if whole_track:
        return f"{track['label']}(分野全体の新着。今日の段階に限らず、この分野の論文なら記録する)"
    step = next(x for x in track["step"] if x["key"] == s["step"])
    return f"{track['label']} / {step['label']}(ねらい: {step['aim']})"


def _done(ctx, d: dict) -> dict:
    label = session_label(ctx.now)
    s = _session(d, label)
    first_finish = s is not None and not s["finished"]
    if first_finish:
        s["finished"] = True
        d["progress"][s["track"]] = d["progress"].get(s["track"], 0) + 1  # 来週の同じ曜日は次の step から
        ctx.state.save_data()
    # LLM は直近のやりとりしか見ていないので、今日の本数はここで数えて渡す
    today = [p for p in d["papers"].values() if p.get("session") == label]
    recorded = sum(1 for p in today if p.get("record"))
    skipped = sum(1 for p in today if p.get("skipped"))
    return {
        "done_for_today": True,
        "message": f"今日の分はすべて読み終えました(記録 {recorded} 本、スキップ {skipped} 本)。この本数を summary に書いて finish してください",
    }


def _finish_current(ctx, record: dict | None, skipped: str | None) -> dict:
    d = _data(ctx)
    pid = d.get("current")
    if pid is None or ctx.run.get("served") != pid:
        return {"error": "読んでいる論文がありません。先に next_paper を呼んでください"}
    d["current"] = None
    ctx.run.pop("served", None)
    p = d["papers"][pid]
    p["session"] = session_label(ctx.now)
    p["record"] = record
    p["skipped"] = skipped
    ctx.state.save_data()
    # 次の論文もここで渡す(LLM の呼び出しが1本あたり1回で済む)
    return {"ok": True, "next_paper": _serve(ctx, d)}


@tool
def record_paper(ctx, summary: str, novelty: str, importance: int) -> dict:
    """今読んだ論文を記録する。summary は日本語3文以内の要約、novelty はそれまでの研究と比べて何が新しいかを日本語1文、importance はこの分野にとっての重要度 1〜5。結果に次の論文が入っている。"""
    if not 1 <= importance <= 5:
        return {"error": "importance は 1〜5 の整数にしてください"}
    return _finish_current(ctx, {"summary": summary.strip(), "novelty": novelty.strip(), "importance": importance}, None)


@tool
def skip_paper(ctx, reason: str) -> dict:
    """今読んだ論文がロボティクスと関係ない、または内容が読み取れないときに飛ばす。結果に次の論文が入っている。"""
    return _finish_current(ctx, None, reason)
