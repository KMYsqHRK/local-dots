"""パラメータスタディのサンプル用ツール。

本番では evaluate の中身を、シミュレーションの実行(subprocess.run など)と結果ファイルの読み取りに置き換える。
評価の記録は ctx.state.data に置くので、何回起こされても続きから進められる。
"""

import math

from localdots.tools import tool

BUDGET = 30
PER_RUN = 4  # 1回の実行で評価できる数。指示に書くだけでは守られないので、ここで強制する
LOW, HIGH = -5.0, 5.0


def _objective(x: float, y: float) -> float:
    # 最小値は (1.3, -0.7) 付近。sin の項で浅い谷がいくつかできる
    return (x - 1.3) ** 2 + 2.0 * (y + 0.7) ** 2 + 0.3 * math.sin(3.0 * x) * math.cos(2.0 * y) + 0.3


def _evals(ctx) -> list[dict]:
    return ctx.state.data.setdefault("evals", [])


@tool
def evaluate(ctx, x: float, y: float) -> dict:
    """パラメータ (x, y) で目的関数を1回評価して loss を返す(小さいほど良い)。x, y は -5〜5。"""
    evals = _evals(ctx)
    if len(evals) >= BUDGET:
        return {"error": f"評価回数の上限({BUDGET} 回)に達しています。最良の点を summary に書いて finish してください"}
    this_run = ctx.run.get("evals", 0)
    if this_run >= PER_RUN:
        return {"error": f"今回の実行で評価できるのは {PER_RUN} 回までです。note と finish で締めてください"}
    if not (LOW <= x <= HIGH and LOW <= y <= HIGH):
        return {"error": f"x, y は {LOW}〜{HIGH} の範囲で指定してください"}
    loss = round(_objective(x, y), 5)
    evals.append({"x": x, "y": y, "loss": loss})
    ctx.run["evals"] = this_run + 1
    ctx.state.save_data()
    return {
        "loss": loss,
        "used": len(evals),
        "remaining": BUDGET - len(evals),
        "remaining_this_run": PER_RUN - this_run - 1,
    }


@tool
def history(ctx, top: int = 8) -> dict:
    """これまでの評価結果を loss の良い順に top 件と、直近の3件、残りの評価回数を返す。"""
    evals = _evals(ctx)
    return {
        "best": sorted(evals, key=lambda e: e["loss"])[:top],
        "recent": evals[-3:],
        "used": len(evals),
        "remaining": BUDGET - len(evals),
    }
