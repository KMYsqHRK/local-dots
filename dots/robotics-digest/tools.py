"""robotics-trends がその朝に記録した論文から、ダイジェスト・分野別の年表・ロードマップを書き出し、メールで送るツール。

ファイルの組み立て(論文の一覧・リンク・年表・前回の続き・予定)はコードがやる。
LLM が書くのは、今日の段階(step)の流れの解説だけ。
"""

import tomllib
from datetime import date, timedelta
from pathlib import Path

from localdots.mail import send_markdown
from localdots.state import now_local, read_json, to_iso
from localdots.tools import tool

TRENDS_DOT = "robotics-trends"
DEFAULT_REPORT_DIR = Path("reports") / "robotics"  # dot.toml の [settings] report_dir が無いとき(local-dots からの相対パス)
UNSAFE_CHARS = str.maketrans({c: "_" for c in '\\/:*?"<>|'})
SUMMARY_CHARS_FOR_LLM = 110
MAX_PAPERS_FOR_LLM = 25
WEEKDAY_JA = "月火水木金土日"
WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _session_label(now) -> str:
    return (now + timedelta(hours=6)).date().isoformat()  # robotics-trends と同じ決め方


def _tracks(ctx) -> list[dict]:
    with (ctx.app.dots_dir / TRENDS_DOT / "tracks.toml").open("rb") as f:
        return tomllib.load(f)["track"]


def _trends(ctx) -> dict:
    return read_json(ctx.app.state_dir / TRENDS_DOT / "data.json", {})


def _today(ctx):
    """(label, trends のデータ, 今日の回, 今日の分野, 今日の step, 今日読んだ論文)"""
    label = _session_label(ctx.now)
    t = _trends(ctx)
    session = next((s for s in reversed(t.get("sessions", [])) if s["label"] == label), None)
    if session is None:
        return label, t, None, None, None, []
    track = next(x for x in _tracks(ctx) if x["key"] == session["track"])
    step = next(x for x in track["step"] if x["key"] == session["step"])
    papers = [p for p in t.get("papers", {}).values() if p.get("session") == label]
    return label, t, session, track, step, papers


def report_dir(ctx) -> Path:
    """レポートの置き場所。分野ごとに folder(tracks.toml)のサブフォルダーを作る。"""
    configured = ctx.dot.settings.get("report_dir")
    path = Path(configured) if configured else DEFAULT_REPORT_DIR
    return path if path.is_absolute() else ctx.app.root / path


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


def _day(label: str) -> str:
    d = date.fromisoformat(label)
    return f"{d.month}/{d.day}({WEEKDAY_JA[d.weekday()]})"


def _theme_papers(papers: list[dict]) -> list[dict]:
    ps = [p for p in papers if p.get("record") and p["source"] != "new"]
    return sorted(ps, key=lambda p: (p.get("year") or 0, -p.get("citations", 0)))


@tool
def tonight(ctx) -> dict:
    """今日 robotics-trends が読んだ論文の一覧(今日の段階の論文だけ、年順・短い要約付き)と、今日の分野・段階・ねらいを返す。"""
    label, _, session, track, step, papers = _today(ctx)
    theme = _theme_papers(papers)
    if session is None or not theme:
        return {"empty": True, "message": f"{label} に記録された論文がありません。finish してください"}
    return {
        "track": track["label"],
        "step": step["label"],
        "aim": step["aim"],
        "papers": [
            {
                "year": p.get("year"),
                "title": p["title"],
                "importance": p["record"]["importance"],
                "summary": p["record"]["summary"][:SUMMARY_CHARS_FOR_LLM],
            }
            for p in theme[:MAX_PAPERS_FOR_LLM]
        ],
    }


@tool
def write_overview(ctx, text: str) -> dict:
    """今日の段階の流れの解説を保存する(日本語 400〜800 字)。publish の前に1回呼ぶ。"""
    label, _, session, _, _, _ = _today(ctx)
    if session is None:
        return {"error": "今日の回がありません。finish してください"}
    ctx.state.data.setdefault("overviews", {})[label] = {
        "track": session["track"], "step": session["step"], "text": text.strip(),
    }
    ctx.state.save_data()
    return {"ok": True, "chars": len(text.strip())}


@tool
def publish(ctx) -> dict:
    """今日のダイジェスト、分野別の年表、ロードマップを Markdown で書き出し、ダイジェストをメールで送る。"""
    label, t, session, track, step, papers = _today(ctx)
    overview = ctx.state.data.get("overviews", {}).get(label)
    if session is None:
        return {"error": "今日の回がありません。finish してください"}
    if overview is None:
        return {"error": "先に write_overview で解説を保存してください"}
    out_dir = report_dir(ctx)
    track_dir = out_dir / track.get("folder", track["key"])
    track_dir.mkdir(parents=True, exist_ok=True)
    digest = _render_digest(ctx, label, t, session, track, step, papers, overview["text"])
    files = {
        "digest": track_dir / f"{label} {step['label']}.md".translate(UNSAFE_CHARS),
        "map": track_dir / "年表.md",
        "roadmap": out_dir / "ロードマップ.md",
    }
    files["digest"].write_text(digest, encoding="utf-8")
    files["map"].write_text(_render_map(ctx, t, track), encoding="utf-8")
    files["roadmap"].write_text(_render_roadmap(ctx, t, label), encoding="utf-8")

    subject = f"[ロボティクス] {_day(label)} {track['label']} {session['step_number']}/{len(track['step'])}: {step['label']}"
    try:
        mail = send_markdown(subject, digest)
    except Exception as e:  # メールが送れなくてもファイルは残っている
        mail = f"送信に失敗しました: {type(e).__name__}: {e}"
    ctx.state.data.setdefault("mailed", {})[label] = mail
    ctx.state.save_data()
    return {"ok": True, "mail": mail, **{k: str(v) for k, v in files.items()}}


def _paper_line(p: dict) -> str:
    r = p["record"]
    return f"- **{p.get('year', '?')}** [{p['title']}]({p['url']}) {_stars(r['importance'])} — {_first_sentence(r['summary'])}"


def _previous(ctx, t: dict, track: dict, label: str) -> tuple[dict, dict | None] | None:
    """同じ分野の、前回の回とその解説。"""
    prev = [s for s in t.get("sessions", []) if s["track"] == track["key"] and s["label"] < label]
    if not prev:
        return None
    s = prev[-1]
    return s, ctx.state.data.get("overviews", {}).get(s["label"])


def _render_digest(ctx, label, t, session, track, step, papers, overview) -> str:
    steps = track["step"]
    recorded = [p for p in papers if p.get("record")]
    theme = _theme_papers(papers)
    new = [p for p in recorded if p["source"] == "new"]
    top3 = sorted(theme or recorded, key=lambda p: (-p["record"]["importance"], -p.get("citations", 0)))[:3]
    skipped = sum(1 for p in papers if p.get("skipped"))
    n = session["step_number"]
    round_note = f"(2周目以降: まだ読んでいない論文だけ)" if session.get("round", 1) > 1 else ""

    lines = [
        f"# {track['label']} {n}/{len(steps)}: {step['label']}",
        "",
        f"{_day(label)} の分 / 読んだ論文 {len(recorded)} 本{round_note}",
        "",
        f"**今日のねらい**: {step['aim']}",
        "",
        "## 前回の続き",
        "",
    ]
    prev = _previous(ctx, t, track, label)
    if prev:
        s, ov = prev
        prev_step = next((x for x in steps if x["key"] == s["step"]), {"label": s["step"]})
        lines.append(f"前回({_day(s['label'])})は「{prev_step['label']}」を読みました。")
        if ov:
            para = ov["text"].split("\n\n")[-1].strip()  # 解説の最後の段落(「今どこに向かっているか」)
            lines += ["", f"> {para[:300]}{'…' if len(para) > 300 else ''}"]
        lines += ["", f"今回はその次の段階、「{step['label']}」です。", ""]
    else:
        lines += [f"「{track['label']}」は今日から始まります。全 {len(steps)} 段階の予定です。", ""]
        lines += [f"{i}. {x['label']}" for i, x in enumerate(steps, 1)] + [""]

    lines += ["## 今日の3本", ""]
    for p in top3:
        r = p["record"]
        lines += [
            f"### [{p['title']}]({p['url']})",
            f"{_cite(p)} / 重要度 {_stars(r['importance'])}",
            "",
            r["summary"],
            "",
            f"**新しさ**: {r['novelty']}",
            "",
        ]
    lines += ["## この段階の流れ", "", overview, ""]
    if theme:
        lines += ["## 今日読んだ論文(年順)", ""] + [_paper_line(p) for p in theme] + [""]
    if new:
        lines += [f"## {track['label']}の arXiv 新着", ""] + [_paper_line(p) for p in new] + [""]
    if skipped:
        lines += [f"(関係なさそうな論文 {skipped} 本は飛ばしました)", ""]
    upcoming = [steps[(n + i) % len(steps)]["label"] for i in range(min(2, len(steps) - 1))]
    if upcoming:
        lines += ["## この先の予定", "", f"- 来週: {upcoming[0]}"] + ([f"- 再来週: {upcoming[1]}"] if len(upcoming) > 1 else []) + [""]
    lines += [
        "---",
        f"robotics-trends / robotics-digest が {to_iso(now_local())} に作成。要約はアブストラクトだけをもとにしています。",
        "",
    ]
    return "\n".join(lines)


def _render_map(ctx, t: dict, track: dict) -> str:
    overviews = ctx.state.data.get("overviews", {})
    by_step: dict[str, list[dict]] = {}
    for p in t.get("papers", {}).values():
        if p.get("record") and p.get("track") == track["key"]:
            by_step.setdefault(p["step"], []).append(p)
    total = sum(len(v) for v in by_step.values())
    lines = [f"# {track['label']} の年表", "", f"これまでに読んだ論文: {total} 本", ""]
    for step in [*track["step"], {"key": "new", "label": "arXiv の新着", "aim": ""}]:
        ps = by_step.get(step["key"])
        if not ps:
            continue
        lines += [f"## {step['label']}", ""]
        if step["aim"]:
            lines += [f"*{step['aim']}*", ""]
        latest = [
            v["text"] for _, v in sorted(overviews.items())
            # 曜日ごとの分野にする前の解説は {"topic": step} の形で、2020年代の潮流のもの
            if v.get("step", v.get("topic")) == step["key"] and v.get("track", "trends") == track["key"]
        ]
        if latest:
            lines += [latest[-1], ""]
        ps.sort(key=lambda p: (p.get("year") or 0, -p["record"]["importance"], -p.get("citations", 0)))
        lines += [_paper_line(p) for p in ps] + [""]
    return "\n".join(lines)


def _render_roadmap(ctx, t: dict, label: str) -> str:
    progress = t.get("progress", {})
    done_on = {}
    for s in t.get("sessions", []):
        if s.get("finished"):
            done_on[(s["track"], s["step"])] = s["label"]
    lines = ["# ロボティクス学習ロードマップ", "", f"{_day(label)} 時点。平日の午前3時に、曜日の分野を1段階ずつ読み進めます。", ""]
    for track in _tracks(ctx):
        steps = track["step"]
        done = min(progress.get(track["key"], 0), len(steps))
        if track.get("weekday"):
            when = f"{WEEKDAY_JA[WEEKDAY_KEYS.index(track['weekday'])]}曜"
            when += {"odd": "(奇数週)", "even": "(偶数週)"}.get(track.get("week"), "")
        else:
            when = "お休み中"
        lines += [f"## {when}: {track['label']}({done}/{len(steps)})", ""]
        for i, step in enumerate(steps):
            when = done_on.get((track["key"], step["key"]))
            mark = "✅" if when else ("▶" if i == done else "・")
            suffix = f"({_day(when)})" if when else ""
            lines.append(f"- {mark} {i + 1}. {step['label']}{suffix} — {step['aim']}")
        lines.append("")
    return "\n".join(lines)
