# mellojoy-notifier

メロジョイ公式通販の「新製品」「新バリエーション」「在庫復活」を検知して、
LINE（と、設定されていれば Slack）に通知する Cloudflare Python Worker。

- 商品の通知は、LINE 公式アカウントの**友だち全員**に一斉配信（Broadcast）する
- 監視開始・取得失敗・復旧のお知らせは、`LINE_USER_ID`（管理者）だけに送る（Push）

- サイトに対しては読み取り（`products.json` の GET）だけを行う
- カート投入・購入などの操作は一切しない
- 通知には、人間がタップして使う商品ページとカートパーマリンクを載せる

## 仕組み

1. cron で起動する（通常は5分ごと。日本時間 11:55〜12:09 だけ毎分）
2. `products.json` をページングで全件取得する
3. 前回のスナップショットとバリエーション単位で差分を取る
4. 差分があれば LINE の友だち全員 / Slack に通知する
5. 最新のスナップショットを D1 に保存する

初回実行は記録のみで、通知は「監視を始めたよ」の1通だけ。
全商品が「新製品」として通知されるのを防いでいる。

取得に失敗したときは指数バックオフ（2分→最大30分）で待機し、
3回連続で失敗した時点で管理者（`LINE_USER_ID`）宛てに警告を送る。復旧したら復旧通知を送る。

## セットアップ

```sh
npm install -g wrangler   # または npx wrangler を使う

# D1 データベースを作る（作成後、表示された database_id を wrangler.toml に書く）
npx wrangler d1 create mellojoy-notifier

# テーブルを作る
npx wrangler d1 execute mellojoy-notifier --remote --file=schema.sql

# シークレットを登録する
npx wrangler secret put LINE_CHANNEL_ACCESS_TOKEN
npx wrangler secret put LINE_USER_ID        # お知らせを受け取る自分のID（任意）
npx wrangler secret put SLACK_WEBHOOK_URL   # Slack にも送る場合だけ

npx wrangler deploy
```

## ローカルで試す

自分の PC で、本物の LINE に届くかを確かめる手順。
`.dev.vars` の `LINE_ADMIN_ONLY=1` によって、商品通知も友だち全員ではなく自分だけに届く。

1. `.dev.vars.example` を `.dev.vars` にコピーし、本物のトークンと自分の `LINE_USER_ID` を入れる
   （`.dev.vars` は Git に含めない。`LINE_ADMIN_ONLY=1` は消さない）。
2. ローカルの D1 を作って起動する。

   ```sh
   npx wrangler d1 execute mellojoy-notifier --local --file=schema.sql
   npx wrangler dev --test-scheduled
   ```

3. 別のターミナルから cron を手で叩く。1回目は記録だけで、「サイトの監視を始めたよ」が自分に届く。

   ```sh
   curl "http://localhost:8787/__scheduled?cron=*/5+*+*+*+*"
   ```

4. 差分を作るため、ローカルのスナップショットから在庫のある商品を1つ消す
   （「見たことのある商品」には残るため、次の実行で「在庫復活」として通知される）。

   ```sh
   npx wrangler d1 execute mellojoy-notifier --local --command \
     "UPDATE state SET value = json_remove(value, '\$.products.\"' || (SELECT j.key FROM state s, json_each(s.value, '\$.products') j WHERE json_extract(j.value, '\$.available') = 1 LIMIT 1) || '\"') WHERE key = 'snapshot'"
   ```

5. もう一度 3 の curl を叩くと、「🔁 在庫復活」のカルーセルが自分にだけ届く。

本番（`wrangler secret put`）には `LINE_ADMIN_ONLY` を登録しない。
友だち全員に届くかは、デプロイ後に友達に確認してもらう。

## 環境変数

| 名前 | 必須 | 説明 |
| --- | --- | --- |
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE に送るなら | LINE Messaging API のチャネルアクセストークン |
| `LINE_USER_ID` | 任意 | 監視開始・失敗・復旧のお知らせを受け取る管理者のユーザーID（`U` から始まる）。商品通知の宛先ではない |
| `SLACK_WEBHOOK_URL` | 任意 | 設定されている場合だけ Slack にも送る |
| `LINE_ADMIN_ONLY` | ローカルのみ | `1` にすると、商品通知も `LINE_USER_ID` だけに送る。本番には登録しない |

商品通知は、公式アカウントを友だち追加している人全員に届く。
友達に受け取ってもらうには、公式アカウントを友だち追加してもらう。

LINE の無料プランは月200通まで。通数は**受け取った人数分**で数えられる
（1回の配信を友達5人が受け取れば5通。この場合は月40回ほどで上限になる）。
通数を抑えるため、複数商品はカルーセルにまとめて1回で送っている。
