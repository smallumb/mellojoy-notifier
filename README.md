# mellojoy-notifier

メロジョイ公式通販の「新製品」「新バリエーション」「在庫復活」を検知して、
LINE（と、設定されていれば Slack）に通知する。Google Cloud の Cloud Run functions で動かす。

- 商品の通知は、LINE 公式アカウントの**友だち全員**に一斉配信（Broadcast）する
- 監視開始・取得失敗・復旧のお知らせは、`LINE_USER_ID`（管理者）だけに送る（Push）

- サイトに対しては読み取り（`products.json` の GET）だけを行う
- カート投入・購入などの操作は一切しない
- 通知には、人間がタップして使う商品ページとカートパーマリンクを載せる
- 手元の Chrome では、拡張機能で 12:00 に追加された商品のチェックアウトを開ける（後述）

## 構成

| 役割 | 使うサービス |
| --- | --- |
| 実行 | Cloud Run functions（Python 3.12、`asia-northeast1`、未認証アクセス不可） |
| 定期起動 | Cloud Scheduler（2ジョブ。時刻は日本時間） |
| 状態の保存 | Firestore の `state/snapshot` ドキュメント |
| 秘密情報 | Secret Manager（関数には環境変数として渡す） |
| デプロイ | GitHub Actions（main へのマージで自動デプロイ。Workload Identity 連携で鍵ファイルなし） |

以前は Cloudflare Workers で動かしていたが、Worker からの取得がショップ前段（Cloudflare）の
ボット対策に断続的にチャレンジされる（`cf-mitigated: challenge`）ため、Google Cloud に移した。

## 仕組み

1. Cloud Scheduler が関数を呼ぶ（通常は10分ごと。日本時間 12:00〜12:10 だけ毎分）
2. `products.json` をページングで全件取得する
3. 前回のスナップショットとバリエーション単位で差分を取る
4. 差分があれば LINE の友だち全員 / Slack に通知する
5. 最新のスナップショットを Firestore に保存する

初回実行は記録のみで、通知は「監視を始めたよ」の1通だけ。
全商品が「新製品」として通知されるのを防いでいる。

取得に失敗したときは指数バックオフ（2分→最大30分）で待機し、
3回連続で失敗した時点で管理者（`LINE_USER_ID`）宛てに警告を送る。復旧したら復旧通知を送る。

失敗したときのレスポンス（`Retry-After`・`server`・`cf-ray`・`cf-mitigated` などのヘッダーと本文の先頭）は、
直近10件を状態の `error_log` に残している（復旧後も消えない）。
Firestore のコンソールで `state/snapshot` の `value`（JSON 文字列）を開くと見られる。

## セットアップ（初回だけ）

1. Google Cloud のプロジェクトを作り、請求先アカウントを紐づける
2. Cloud Shell でこのリポジトリを取得し、初期設定スクリプトを実行する
   （API の有効化、Firestore・サービスアカウント・Workload Identity 連携の作成、シークレットの登録）

   ```sh
   git clone https://github.com/smallumb/mellojoy-notifier.git && cd mellojoy-notifier
   GCP_PROJECT_ID=<プロジェクトID> bash deploy/bootstrap_gcp.sh
   ```

   シークレット（`LINE_CHANNEL_ACCESS_TOKEN`・`LINE_USER_ID`・任意で `SLACK_WEBHOOK_URL`）は
   実行中に聞かれるので入力する（画面には表示されない）。
3. 最後に表示される3つの値を、GitHub の Settings → Secrets and variables → Actions → **Variables** に登録する
   - `GCP_PROJECT_ID`
   - `GCP_WIF_PROVIDER`
   - `GCP_DEPLOY_SA`
4. GitHub の Actions タブから「Deploy to Google Cloud」を手動実行する（以降は main へのマージで自動デプロイ）
5. 初回の動作を確認する

   ```sh
   gcloud scheduler jobs run mellojoy-every-10min --location=asia-northeast1
   ```

   管理者の LINE に「サイトの監視を始めたよ」が届き、Firestore に `state/snapshot` ができていれば完了。
6. 予算アラート（例：月100円）を「お支払い → 予算とアラート」で作っておく

シークレットの値を変えるときは、Secret Manager に新しいバージョンを追加してから、もう一度デプロイする。

## ローカルで試す

自分の PC で、本物の LINE に届くかを確かめる手順。
`LINE_ADMIN_ONLY=1` によって、商品通知も友だち全員ではなく自分だけに届く。
`STATE_FILE` を指定すると、状態は Firestore ではなくローカルの JSON ファイルに保存される。

1. `.env.example` を `.env` にコピーし、本物のトークンと自分の `LINE_USER_ID` を入れる
   （`.env` は Git に含めない。`LINE_ADMIN_ONLY=1` と `STATE_FILE` は消さない）。
2. 依存パッケージを入れて起動する。

   ```sh
   python3 -m venv .venv && . .venv/bin/activate
   pip install -r src/requirements.txt
   set -a && . ./.env && set +a
   functions-framework --target=run --source=src/main.py
   ```

3. 別のターミナルから関数を呼ぶ。1回目は記録だけで、「サイトの監視を始めたよ」が自分に届く。

   ```sh
   curl -X POST http://localhost:8080
   ```

4. 差分を作るため、状態ファイルから在庫のある商品を1つ消す
   （「見たことのある商品」には残るため、次の実行で「在庫復活」として通知される）。

   ```sh
   python3 - <<'PY'
   import json
   s = json.load(open("state.json"))
   pid = next(k for k, v in s["products"].items() if v["available"])
   del s["products"][pid]
   json.dump(s, open("state.json", "w"), ensure_ascii=False)
   PY
   ```

5. もう一度 3 の curl を叩くと、「🔁 在庫復活」のカルーセルが自分にだけ届く。

本番の Secret Manager には `LINE_ADMIN_ONLY` と `STATE_FILE` を登録しない。
友だち全員に届くかは、デプロイ後に友達に確認してもらう。

## 12:00 に追加された商品のチェックアウトを開く（Chrome 拡張）

自分の Chrome に入れて使う拡張機能（`extension/`）。Cloud Run にはデプロイしない。
12:00 に追加された商品を見つけ、そのカートパーマリンク（開くとチェックアウトへ進む URL）を
Chrome のタブで開く。ログイン・住所・支払い方法が保存されたブラウザで開くので、
あとは人間が確認して購入を進めるだけになる。購入の確定などの操作は拡張ではしない。

1. 11:59 に `products.json` を1回取得し、その時点の商品を基準として覚える
2. 12:00:00〜12:00:10 のあいだ、0.5秒おきに取得して基準と比べる
   （間隔は取得を始めた時刻から数える。失敗したら間隔を倍々に空ける。上限5秒）
3. 新製品・新バリエーション・在庫復活のうち**在庫のあるもの**が見つかったら、
   バリエーションごとにタブで開いて終わる（最大5タブ。残りの URL はログに出す）

在庫なしで先に掲載され、あとから在庫ありになった商品も「在庫復活」として拾う。
取得は Chrome 自身の通信（Cookie・User-Agent もそのまま）で行うので、ボット対策に止められにくい。
商品の要約と差分検出は `src/main.py` と同じ振る舞いにしてある（`extension/lib/shop.js`）。

### 入れ方

1. Chrome で `chrome://extensions` を開き、右上の「デベロッパー モード」をオンにする
2. 「パッケージ化されていない拡張機能を読み込む」で、このリポジトリの `extension` フォルダを選ぶ
3. ツールバーのパズルのアイコンから「Mellojoy Checkout Opener」をピン留めしておく
4. 同じ Chrome でメロジョイ通販にログインしておく（チェックアウトの前にログインを求められるため）

コードを更新したら、`chrome://extensions` で拡張の更新ボタン（↻）を押す。

### 使い方

アイコンを押すと、次回の予定・前回の結果・ログが見られる。設定もここで変える（時刻は日本時間）。

| 設定 | 既定 | 説明 |
| --- | --- | --- |
| 毎日自動で監視する | オン | オフにすると何もしない |
| 開始時刻 | 12:00 | 基準はこの1分前に取る |
| 取得の間隔 | 0.5秒 | 0.5秒未満は0.5秒にする |
| 監視を続ける時間 | 10秒 | |
| 一度に開くタブの上限 | 5 | |

「今すぐテスト」を押すと、すぐに始め、商品を1つ「追加された」とみなしてタブで開く。
カート（またはログイン）画面が開けば OK（在庫がない商品なら在庫切れの表示になる。開いたカートの中身は手で消す）。
監視中はアイコンに「ON」と表示される。

- Chrome が起動していること（ウィンドウは閉じていてもよいが、Chrome 自体は終了しない）
- 11:59 に Mac がスリープしていないこと。スリープしていると、起きたときに監視時間内なら途中から始める。
  このとき基準には、開始時刻より前に保存しておいた商品一覧（前回の監視で取ったもの）を使う。
  保存した一覧がなければ「見分けられなかった」と記録して終わる。監視時間を過ぎていれば翌日に回る。
  スリープから自動で起こすには次を実行する

  ```sh
  sudo pmset repeat wakeorpoweron MTWRFSU 11:55:00
  ```

### テスト

判定と時刻計算の関数は Node.js（22 以降）で確かめられる。

```sh
cd extension && npm test
```

`extension/test/fixtures/expected_from_python.json` は、同じ入力を `src/main.py` の
`summarize` / `diff` に通した結果。Python 側の判定を変えたら作り直す。

## 環境変数

| 名前 | 必須 | 説明 |
| --- | --- | --- |
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE に送るなら | LINE Messaging API のチャネルアクセストークン |
| `LINE_USER_ID` | 任意 | 監視開始・失敗・復旧のお知らせを受け取る管理者のユーザーID（`U` から始まる）。商品通知の宛先ではない |
| `SLACK_WEBHOOK_URL` | 任意 | 設定されている場合だけ Slack にも送る |
| `LINE_ADMIN_ONLY` | ローカルのみ | `1` にすると、商品通知も `LINE_USER_ID` だけに送る。本番には登録しない |
| `STATE_FILE` | ローカルのみ | 状態を Firestore ではなくこの JSON ファイルに保存する。本番には登録しない |

本番では、上の3つは Secret Manager から環境変数として渡す（`deploy/deploy.sh` の `--set-secrets`）。

商品通知は、公式アカウントを友だち追加している人全員に届く。
友達に受け取ってもらうには、公式アカウントを友だち追加してもらう。

LINE の無料プランは月200通まで。通数は**受け取った人数分**で数えられる
（1回の配信を友達5人が受け取れば5通。この場合は月40回ほどで上限になる）。
通数を抑えるため、複数商品はカルーセルにまとめて1回で送っている。

## 費用

月4,600回ほどの実行で、いずれも Google Cloud の無料枠に収まる見込み。

- Cloud Run functions：月200万回・40万 GB秒まで無料
- Cloud Scheduler：請求先アカウントごとに3ジョブまで無料（2ジョブ使う）
- Firestore：1日あたり読み取り5万回・書き込み2万回まで無料
- Secret Manager：環境変数として渡すので、読み取りは起動時だけ
- Artifact Registry：古いイメージは自動で消す（新しい2つだけ残す）
