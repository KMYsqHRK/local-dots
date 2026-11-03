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

ロボティクスの論文を、平日の朝に1段階ずつ読み進める2つの担当です。曜日ごとに分野が決まっていて、翌週の同じ曜日はその続きから読みます。

| 曜日 | 分野 | 段階の例 |
|---|---|---|
| 月 | ナビゲーション | 経路計画の基礎 → 局所計画 → 軌道最適化 → 学習ベース → 意味的ナビ → 人のいる環境 → 基盤モデル(全7段階) |
| 火 | 認識 | 物体検出 → セグメンテーション → 点群 → 6D姿勢と把持 → 表現学習 → オープン語彙 → アフォーダンス(全7段階) |
| 水 | SLAM・自己位置推定・フィルタリング | カルマン/パーティクル → 古典SLAM → 視覚SLAM → VIO → LiDAR SLAM → 因子グラフ → 場所認識 → ニューラルSLAM(全8段階) |
| 木 | センサー・ハードウェア | ハードと、それを測るセンサーを1回にまとめて: 関節とアクチュエータ → 力覚・トルク・IMU → 脚とヒューマノイド → カメラ・深度・ステレオ → ハンドと触覚 → LiDARとレーダー → ソフトロボットと低コストなハード → ドローンとイベントカメラ(全8段階。1回の本数を2テーマで分け合う) |
| 金 | シミュレーション | 物理エンジン → 接触モデル → GPU並列 → sim-to-real → 微分可能 → 柔軟物 → 学習環境・ベンチマーク → Real2Sim(全8段階) |

「2020年代の潮流」(LLM計画 → VLA → 模倣学習 → … 全10段階)は今はお休み中です。`tracks.toml` で `weekday` を書けば、また読み進めます。

- **robotics-trends**(平日 03:00): 今日の分野の、次の段階を読みます。Semantic Scholar から、その段階の定番論文を被引用数の多い順に20本取ります(基礎の段階は1990年代から)。ここ1〜2年の注目論文を5本、その分野の arXiv 新着を5本足して、年の順に1本ずつアブストラクトを読ませ、要約・新しさ・重要度を記録します。30本で20分ほどです。
- **robotics-digest**(平日 05:00): 今日の段階の流れを解説させて、`diary-vault\papers\robotics_summary\` に次のファイルを書き出し、ダイジェストをメールで送ります。保存先は `dots/robotics-digest/dot.toml` の `report_dir` で変えられます。
  - `<分野>\YYYY-MM-DD <段階名>.md`: 前回(先週の同じ曜日)の続き、今日の3本、この段階の流れ、年順の一覧、新着、この先の予定
  - `<分野>\年表.md`: その分野のこれまでの全記録を、段階別・年順にまとめたもの
  - `ロードマップ.md`: 全分野の進み具合

  分野のフォルダーは `1_ナビゲーション` / `2_認識` / `3_SLAM・自己位置推定・フィルタリング` / `4_センサー・ハードウェア` / `5_シミュレーション` / `9_2020年代の潮流` です。

どの分野も7〜8段階なので、だいたい同じころ(始めてから約2か月)に読み終わります。最後まで読み終えた分野は1段階目に戻り、まだ読んでいない論文だけを読む2周目に入ります。

分野・段階・検索クエリは `dots/robotics-trends/tracks.toml` で変えられます。Semantic Scholar は、キーなしだと回数制限(429)によくかかります。待ってやり直す処理は入れてありますが、[API キー](https://www.semanticscholar.org/product/api#api-key-form)を環境変数 `S2_API_KEY` に入れておくと安定します。

### メールとAPIキーの設定(`.env`)

パスワードやキーは、ルートの `.env` に書きます(git には入りません)。コマンドを実行するたびに読み込むので、タスクスケジューラーから動かすときも、そのまま使われます。すでに同じ名前の環境変数があれば、そちらが優先されます。

Gmail の場合は、Google アカウントの2段階認証を有効にしてから「アプリ パスワード」を作り、`.env` を次のように作ります。

```ini
# 朝のダイジェストのメール
LOCALDOTS_SMTP_USER=you@gmail.com
LOCALDOTS_SMTP_PASSWORD=xxxxxxxxxxxxxxxx
# 宛先(省略すると LOCALDOTS_SMTP_USER 宛て)
# LOCALDOTS_MAIL_TO=you@example.com
# Gmail 以外のとき
# LOCALDOTS_SMTP_HOST=smtp.gmail.com
# LOCALDOTS_SMTP_PORT=465

# Semantic Scholar の API キー(無くても動くが、回数制限にかかりにくくなる)
# S2_API_KEY=
```

設定が無いときはファイルだけ書き出して、メールは送りません。

## 設定

設定はルートの2つのファイルにまとめてあります。

- **`localdots.toml`**: LLM サーバーの場所、起動・停止コマンド、コンテキスト長、担当ごとの上書き。初期値は RTX 3050 8GB / RAM 32GB で Qwen3.6-35B-A3B が動いた設定です。
  - `[dots.<名前>.schedule]` で時刻や曜日を、`[dots.<名前>.settings]` で保存先などを、`dots/<名前>/dot.toml` に手を入れずに変えられます。`enabled = false` で止めることもできます。
- **`.env`**: パスワードと API キー(上を参照)。

`python -m localdots list` で、上書きが反映された次の実行時刻を確認できます。

## テスト

```powershell
python -m unittest discover -s tests
```

LLM を使わず、台本どおりに応答する偽のモデルで、実行ループ・スケジュール・受信箱を確かめます。

## 状態ファイル

`state/` に、担当ごとの状態・記憶・データ・実行記録(`runs/*.jsonl`)と、受信箱が置かれます。git には入れません。
