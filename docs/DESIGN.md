# local-dots 設計

ChatGPT の dots(担当を割り当てると会話をまたいで動き続け、判断が必要なときだけ人に確認するエージェント)を、
ローカル LLM(Strata + Qwen3.6-35B など)で小さく再現するための枠組み。

## 目標と前提

- **常駐させない**。必要なときだけ LLM サーバーを起動し、仕事が終わったら止める。普段の作業を邪魔しない。
- **小さいコンテキストで回る**。8K トークンでも動くように、ループ・記録・数値計算はコードが持ち、LLM には「次に何をするか」の判断だけをさせる。
- **担当(dot)はファイルで定義する**。設計や担当の文面は Claude などの強いモデルと作り、実行はローカルモデルに任せる。
- **人への確認は非同期**。エージェントは質問を受信箱に置いて止まり、人が答えたら次の起動で続きから動く。
- Python 3.11 標準ライブラリのみ。OpenAI 互換 API(`/v1/chat/completions`)を話すサーバーなら Strata 以外でも動く。

## 全体の流れ

```
 タスクスケジューラー(15分ごとなど)
        │  python -m localdots tick
        ▼
 ┌──────────────── scheduler ────────────────┐
 │ 1. dots/*/dot.toml を読み、期限の来た dot を選ぶ │
 │ 2. 1つもなければ何もせず終了(LLM は起動しない) │
 │ 3. backend.ensure()  … Strata が止まっていれば起動 │
 │ 4. 各 dot について agent.run_dot()             │
 │ 5. backend.release() … 自分で起動した場合だけ停止 │
 └────────────────────────────────────────────┘
        │
        ▼
 ┌──────────────── agent(1回の実行) ────────────┐
 │ system: 担当の説明・指示・記憶・使えるツール       │
 │ user:   現在時刻・前回の要約・人からの回答         │
 │  ┌─► LLM が JSON で行動を1つ返す                 │
 │  │   {"thought": "...", "tool": "...", "args": {...}} │
 │  │   コードがツールを実行し、結果を返す            │
 │  └── finish / ask_human / snooze / 手数上限まで繰り返す │
 └────────────────────────────────────────────┘
```

## 構成要素

| モジュール | 役割 |
|---|---|
| `config.py` | `localdots.toml`(全体設定)と `dots/<name>/dot.toml`(担当)を読む |
| `backend.py` | LLM サーバーの生存確認、起動・停止。自分で起動したときだけ止める |
| `llm.py` | OpenAI 互換の chat クライアント(urllib のみ)。Qwen の思考モードを切り替え、`<think>` を取り除く |
| `agent.py` | 1回の実行ループ。JSON 行動の解析・再要求、コンテキストの切り詰め、状態の更新 |
| `tools.py` | `@tool` デコレーター、関数シグネチャからのツール定義、dot ごとの `tools.py` の読み込み |
| `builtins.py` | すべての dot が使える組み込みツール(`note` / `ask_human` / `snooze` / `finish`) |
| `state.py` | dot ごとの状態・記憶・データ・実行記録(JSON / JSONL) |
| `inbox.py` | 人への質問と回答の受信箱 |
| `scheduler.py` | 期限判定と `tick`(多重起動防止のロック付き) |
| `cli.py` | `list` / `status` / `run` / `tick` / `watch` / `inbox` / `answer` / `new` |
| `mail.py` | 自分宛てのメール(SMTP over SSL)。設定は環境変数。Markdown を簡単な HTML にもして送る |

## dot の定義

```
dots/<name>/
  dot.toml          必須。説明・スケジュール・手数上限など
  instructions.md   担当の仕事内容(LLM への指示)
  tools.py          任意。この dot 専用のツール(@tool を付けた関数)
```

```toml
description = "この担当が何をするかの一行説明"
enabled = true

[schedule]
every = "1h"                 # 間隔(s / m / h / d)。at と併用しない
# at = "02:00"               # 毎日この時刻以降に1回
window = ["00:00", "06:00"]  # この時間帯だけ動く(任意。日付をまたいでもよい)
# days = "weekdays"          # 動かす曜日(任意)。["mon", "wed"] のようにも書ける
# manual = true              # 自動では動かさない(run でのみ実行)

[agent]
max_steps = 12               # 1回の実行で LLM に行動を選ばせる最大回数
instructions_file = "instructions.md"
# temperature = 0.2          # 全体設定を上書き
# history_window = 2         # 直近この回数分のやりとりだけを渡す(1件ずつ独立した仕事向け)
# max_observation_chars = 2000  # ツールの結果をこの文字数で切る

[settings]                   # その dot のツールが ctx.dot.settings で読む自由な設定(出力先など)
# report_dir = 'C:\path\to\folder'
```

### 速さについて(Strata + RTX 3050 での実測)

Strata は、毎回プロンプトを最初から読み直します(前回分の再利用は0トークンでした)。読み込みは約 110 トークン/秒なので、7,000 トークンのプロンプトでは1回あたり約1分かかります。

- 論文を1本ずつ読むような、1件ずつ独立した仕事では `history_window` を小さくして、プロンプトを短く保つ。robotics-trends では、1本あたり約2分だったのが約40秒になった。
- 「結果を記録する」ツールが「次の1件」も一緒に返すようにすると、LLM の呼び出しが1件あたり1回で済む。
- 逆に、一覧を丸ごと読ませる仕事(robotics-digest)では `max_observation_chars` を広げる。初期値の 2,000 字のままだと一覧の後半が切れて、LLM が新しい年の論文を見落とす。

### 小さいモデルに任せるときの原則

- **守らせたい数の制約は、指示文ではなくツールに書く**。「1回で4点まで」と指示しても守られなかったので、ツールが5回目を断るようにした。
- **数える・並べる・リンクを張る、はコードがやる**。今夜の本数や年表は、LLM に数えさせず、コードが組み立てる。

### 専用ツール

```python
from localdots.tools import tool

@tool
def evaluate(ctx, x: float, y: float) -> dict:
    """パラメータ (x, y) で1回評価して loss を返す。"""
    ...
```

- 第1引数は必ず `ctx`(`ToolContext`)。`ctx.state.data` に dot 専用の永続データを読み書きできる。
- 残りの引数の型注釈(`float` / `int` / `str` / `bool`)とデフォルト値がそのまま LLM への説明になり、受け取った値もその型に変換される。
- docstring の1段落目がツールの説明になる。
- 戻り値は JSON にできる値。長すぎる結果は切り詰めて LLM に渡す。

## 行動の形式(LLM とのやりとり)

ネイティブの tool calling には頼らず、**1回の応答で JSON オブジェクトを1つ返させる**方式にした。
ローカルモデルやサーバーによって tool calling の対応がまちまちなのと、8K のコンテキストではツール定義の
スキーマを長々と載せられないため。

- 応答の中から `"tool"` キーを持つ最初の JSON オブジェクトを取り出す(前後の文章やコードフェンスは無視)。
- 取り出せない・存在しないツール・引数の誤りは、エラー内容を返して出し直させる。連続して失敗したら実行を打ち切る。
- ツールの結果は `OBSERVATION: {...}` として user 発言で返す。

## 状態と記憶

```
state/
  inbox/<id>.json            人への質問(回答もここに書かれる)
  <dot>/status.json          状態(idle / blocked / error)、前回実行時刻、次回予定、前回の要約
  <dot>/memory.json          note ツールで残した気づき(新しい順に最大50件。system に載せる)
  <dot>/data.json            専用ツールが使う永続データ(評価履歴など)
  <dot>/runs/<時刻>.jsonl    1回の実行の全記録(LLM の応答・ツール結果)
  backend.log                サーバー起動コマンドの出力
```

- **会話履歴は実行ごとに捨てる**。次の実行に引き継ぐのは「前回の要約」(`finish` の summary)と `memory.json` と `data.json` だけ。こうすることで、何十回動いてもコンテキストが膨らまない。
- コンテキストが足りなくなりそうなときは、system と最初の指示を残して古いやりとりから省く(文字数から大まかに見積もる)。

## 人への確認(受信箱)

1. LLM が `ask_human(question, options)` を呼ぶと、`state/inbox/<id>.json` に質問を書き、dot を `blocked` にして実行を終える。
2. `tick` は回答待ちの dot を飛ばす。
3. 人が `python -m localdots answer <id> "回答"` で答える。
4. 次の `tick` で dot がすぐ期限扱いになり、回答を最初の OBSERVATION として受け取って続きから動く。

質問の通知は今は無い。`inbox` コマンドで確認する。成果物をメールで届けたい dot は、ツールの中から `localdots.mail.send_markdown` を呼ぶ(robotics-digest がその例)。

## 実行タイミング

- `tick` は「期限の来た dot だけを動かす」コマンド。タスクスケジューラーなどから定期的に呼ぶ想定(`scripts/register-task.ps1`)。
- 期限が来た dot が無ければ LLM サーバーには触れないので、15分ごとに呼んでも負担は無い。
- LLM は `snooze(minutes, reason)` で次回の起動時刻を自分で決められる。「今は条件が揃っていないので2時間後にもう一度見る」といった、**タイミングを選ぶエージェント**はこれで作る。
- `window` を指定すると、その時間帯以外は動かない(夜間だけパラスタを回す、など)。

## LLM サーバーの起動・停止

- `backend.manage = true` のとき、サーバーが応答しなければ `start_cmd` を実行し、`/v1/models` が応答するまで待つ。
- 起動したのが自分なら、`tick` / `run` の最後に `stop_cmd` を実行する。もともと動いていたサーバーは止めない。
- `--no-manage` を付けると起動・停止をせず、動いているサーバーだけを使う。

### Windows の注意

Strata は `%LOCALAPPDATA%` にファイルを書く。Claude デスクトップアプリの中から起動すると、Windows がその書き込みを
アプリ専用の領域に振り分けてしまう。なので、`tick` は**普通の PowerShell かタスクスケジューラー**から実行する。

## 今後の拡張の候補

- **バックエンドのプロファイル**: 難しい判断だけクラウドのモデル(Claude など)に上げる `escalate` ツール。
- **質問の通知**: 質問が来たときに Windows のトーストやメールで知らせる(メールの部品はもうある)。
- **コマンド実行ツール**: 許可リスト付きの `run_command`(シミュレーションの実行など)。今は dot ごとの `tools.py` に書く。
- **dot 同士の連携**: 共有メモ(`state/shared.json`)や、ある dot が別の dot を起こすトリガー。
