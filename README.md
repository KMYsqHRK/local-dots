# local-dots

ローカル LLM で動く、**担当を持ったエージェント**の小さな枠組みです。
ChatGPT の dots のように、仕事を割り当てておくと定期的に起きて少しずつ進め、判断が必要なときだけ人に質問します。

- **常駐しない**: 期限の来た担当があるときだけ LLM サーバー(Strata など)を起動し、終わったら止めます。
- **8K コンテキストで回る**: ループと記録はコードが持ち、LLM には次の行動を選ばせるだけです。会話は実行ごとに捨て、要約と記憶だけを引き継ぎます。
- **担当はファイルで書く**: `dots/<name>/` に設定・指示・専用ツールを置きます。
- Python 3.11 の標準ライブラリだけで動きます。OpenAI 互換の API なら Strata 以外でも使えます。

設計の詳細は [docs/DESIGN.md](docs/DESIGN.md) にあります。

## 使い方

**普通の PowerShell から実行してください。** Claude デスクトップアプリの中から Strata を起動すると、Windows がファイルの書き込み先を振り分けてしまいます。

```powershell
cd C:\Users\kmysq\Documents\Kamiya_projects\local-dots

python -m localdots list                  # 担当の一覧と、次に動く時刻
python -m localdots run param-sweep-demo  # 今すぐ動かす(Strata を自動で起動・停止)
python -m localdots tick                  # 期限の来た担当だけを動かす
python -m localdots inbox                 # 担当からの質問
python -m localdots answer <id> "回答"    # 質問に答える(次の tick で続きから動く)
python -m localdots status param-sweep-demo
python -m localdots new my-dot            # 新しい担当のひな形
```

Strata をすでに手で起動している場合や、起動・停止させたくない場合は `--no-manage` を付けます。

### 定期実行

`tick` を15分ごとに呼ぶタスクを登録します(期限の来た担当が無ければ、LLM は起動しません)。

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register-task.ps1
```

外すときは `Unregister-ScheduledTask -TaskName local-dots-tick` です。

## 担当(dot)を作る

```
dots/<name>/
  dot.toml          説明・スケジュール・1回の行動数の上限
  instructions.md   仕事内容(LLM への指示)
  tools.py          この担当専用のツール(任意)
```

```toml
description = "夜間にパラメータスタディを進める"

[schedule]
every = "30m"               # または at = "02:00"(毎日1回)、manual = true(手動のみ)
window = ["01:00", "06:00"] # この時間帯だけ動く(任意)

[agent]
max_steps = 10
```

専用ツールは `@tool` を付けた関数です。第1引数は `ctx` で、`ctx.state.data` に担当ごとの永続データを置けます。
型注釈と docstring がそのまま LLM への説明になります。

```python
from localdots.tools import tool

@tool
def evaluate(ctx, x: float, y: float) -> dict:
    """パラメータ (x, y) で1回評価して loss を返す。"""
    ...
```

すべての担当で使える組み込みツール:

| ツール | 用途 |
|---|---|
| `note(text)` | 次回以降も覚えておく気づきを残す |
| `ask_human(question, options)` | 人に質問して止まる。回答が来たら続きから動く |
| `snooze(minutes, reason)` | 次に起きる時刻を自分で決める(タイミングを選ぶエージェント向け) |
| `finish(summary)` | 今回の仕事を終える。summary は次回に読まれる |

## サンプル: `param-sweep-demo`

2変数の目的関数を、30回の評価で最小化するパラメータスタディです。1回の実行で最大4点を評価し、結果と気づきを残して終わります。
本番で使うときは `tools.py` の `evaluate` を、シミュレーションの実行と結果の読み取りに置き換えます。

## 担当: `robotics-trends` と `robotics-digest`

2020年代のロボティクスの流れを、一晩に1テーマずつ読み進める2つの担当です。朝には `reports/robotics/` に読み物ができています。

- **robotics-trends**(01:00〜): `topics.toml` の順番で、今夜のテーマを1つ選びます。Semantic Scholar から2020年以降の論文を被引用数の多い順に20本、ここ1〜2年の注目論文を5本、arXiv の cs.RO 新着を5本取ってきて、1本ずつアブストラクトを読ませ、要約・テーマ・新しさ・重要度を記録します。30本で20分ほどです。
- **robotics-digest**(06:00〜): 今夜の記録から、テーマの流れの解説を書かせます。そのうえで次の2つを書き出します。
  - `digest-YYYY-MM-DD.md`: 今日の3本、テーマの流れ、年順の一覧、新着
  - `trend-map.md`: これまでの全記録を、テーマ別・年順にまとめたもの

テーマの順番と検索クエリは `dots/robotics-trends/topics.toml` で変えられます。Semantic Scholar は、キーなしだと回数制限(429)によくかかります。待ってやり直す処理は入れてありますが、[API キー](https://www.semanticscholar.org/product/api#api-key-form)を環境変数 `S2_API_KEY` に入れておくと安定します。

## 設定

`localdots.toml` でサーバーの場所、起動・停止コマンド、コンテキスト長などを設定します。
初期値は RTX 3050 8GB / RAM 32GB で Qwen3.6-35B-A3B が動いた設定です。

## テスト

```powershell
python -m unittest discover -s tests
```

LLM を使わず、台本どおりに応答する偽のモデルで、実行ループ・スケジュール・受信箱を確かめます。

## 状態ファイル

`state/` に、担当ごとの状態・記憶・データ・実行記録(`runs/*.jsonl`)と、受信箱が置かれます。git には入れません。
