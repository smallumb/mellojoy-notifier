"""
メロジョイ公式通販の「新製品」「在庫復活」を検知して LINE の友だち全員（と、設定されていれば Slack）に通知する
Google Cloud Run functions（Cloud Scheduler から定期的に呼び出す）

方針:
- サイトに対しては読み取り（products.json の GET）だけを行う。
- カート投入・購入などの操作は一切しない。
- 通知には、人間がタップして使う商品ページとカートパーマリンクを載せる。
"""

import json
import os
import time
from datetime import datetime, timezone
from urllib.parse import quote

import functions_framework
import requests

# ---- 設定 -------------------------------------------------------------------

STORE_URL = "https://www.mellojoyjapan.com"
USER_AGENT = "mellojoy-restock-notifier/1.0 (personal use, read-only)"

PAGE_LIMIT = 250          # products.json の1ページあたり件数（Shopifyの上限）
MAX_PAGES = 10            # 念のための取得ページ上限
HTTP_TIMEOUT_SEC = 20     # 外部への HTTP リクエストのタイムアウト

# 状態の保存先（Firestore の state/snapshot ドキュメント）
STATE_COLLECTION = "state"
STATE_DOCUMENT = "snapshot"

FAILURE_ALERT_THRESHOLD = 3   # 連続失敗がこの回数に達したら自分宛てに警告
BACKOFF_MIN_SEC = 120         # 失敗時の待機（2分から倍々）
BACKOFF_MAX_SEC = 1800        # 待機の上限（30分）

ERROR_LOG_SIZE = 10           # 状態に残す失敗の詳細の件数（新しい順）
ERROR_BODY_SNIPPET = 200      # 失敗時に残すレスポンス本文の文字数
# 429 などの原因を切り分けるために残すレスポンスヘッダー
ERROR_HEADERS = ["retry-after", "server", "cf-ray", "cf-mitigated", "x-request-id", "content-type"]

MAX_VARIANTS_PER_PRODUCT = 10 # 通知に載せるバリエーション数の上限

# LINE（Messaging API）
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"             # 管理者1人宛て（お知らせ用）
LINE_BROADCAST_URL = "https://api.line.me/v2/bot/message/broadcast"   # 友だち全員宛て（商品通知用）
LINE_BUBBLES_PER_CAROUSEL = 12    # カルーセル1つに入るカードの上限
LINE_MESSAGES_PER_REQUEST = 5     # 1回のプッシュで送れるメッセージの上限
LINE_MAX_CART_BUTTONS = 4         # 1カードに付けるカートボタンの上限
LINE_IMAGE_WIDTH = 1024           # 画像はShopify CDNで縮小してから表示する

# Slack（任意）
SLACK_PRODUCTS_PER_MESSAGE = 20   # Slackのブロック上限50に収める
SLACK_SECTION_TEXT_LIMIT = 3000


# ---- エントリーポイント -------------------------------------------------------

@functions_framework.http
def run(request):
    """Cloud Scheduler から呼ばれる。取得の失敗は状態とログに残して 200 を返す。
    想定外の例外は 500 になり、Cloud Logging に残る（Scheduler は再試行しない）。"""
    check()
    return "ok"


def check():
    now = time.time()
    state = load_state()

    if now < state.get("backoff_until", 0):
        print("前回の失敗により待機中のため、今回はスキップします")
        return

    try:
        raw_products = fetch_all_products()
    except Exception as e:  # noqa: BLE001
        handle_failure(state, now, e)
        return

    current = summarize(raw_products)
    previous = state.get("products")
    seen = set(state.get("seen_ids", []))

    # 初回は記録のみ（全商品が「新製品」扱いになるのを防ぐ）
    if previous is None:
        save_state(build_state(current, seen, state.get("error_log")))
        send_text(f"サイトの監視を始めたよ 👀（{len(current)}件の商品を記録）")
        return

    events = diff(previous, current, seen)
    if events:
        notify(events)

    if state.get("failures", 0) >= FAILURE_ALERT_THRESHOLD:
        send_text("✅ 取得が復旧しました")

    save_state(build_state(current, seen, state.get("error_log")))


# ---- 取得 -------------------------------------------------------------------

class HttpError(Exception):
    def __init__(self, status, url, detail=None):
        super().__init__(f"HTTP {status}: {url}")
        self.status = status
        self.detail = detail or {}


def describe_response(resp):
    """失敗したレスポンスの、原因の切り分けに使うヘッダーと本文の先頭を取り出す。"""
    headers = {name: resp.headers[name] for name in ERROR_HEADERS if name in resp.headers}
    try:
        body = " ".join(resp.text.split())[:ERROR_BODY_SNIPPET]
    except Exception as e:  # noqa: BLE001
        body = f"（本文を読めませんでした: {e}）"
    return {"headers": headers, "body": body}


def fetch_all_products():
    items = []
    for page in range(1, MAX_PAGES + 1):
        url = f"{STORE_URL}/products.json?limit={PAGE_LIMIT}&page={page}"
        resp = requests.get(url, timeout=HTTP_TIMEOUT_SEC, headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        })
        if resp.status_code != 200:
            raise HttpError(resp.status_code, url, describe_response(resp))

        data = resp.json()
        batch = data.get("products", [])
        items.extend(batch)
        if len(batch) < PAGE_LIMIT:
            break
    return items


def summarize(raw_products):
    """比較と通知に必要な項目だけを取り出す。"""
    result = {}
    for p in raw_products:
        variants = [
            {
                "id": v["id"],
                "title": v.get("title", ""),
                "available": bool(v.get("available")),
                "price": v.get("price", ""),
            }
            for v in p.get("variants", [])
        ]
        images = p.get("images") or []
        result[str(p["id"])] = {
            "title": p.get("title", ""),
            "handle": p.get("handle", ""),
            "image": images[0].get("src", "") if images else "",
            "available": any(v["available"] for v in variants),
            "variants": variants,
        }
    return result


# ---- 差分検出 ---------------------------------------------------------------

def diff(previous, current, seen):
    """
    バリエーション単位で判定し、(種類, 商品, 強調するバリエーションID集合) を返す。
    new         : 一度も見たことのない商品が現れた
    new_variant : 既存の商品に新しいバリエーションが追加された
    restock     : バリエーションが 在庫なし→在庫あり になった
                  （一度消えた商品が在庫ありで再掲載された場合も含む）
    """
    events = []
    for pid, product in current.items():
        before = previous.get(pid)

        if before is None:
            if pid not in seen:
                ids = {v["id"] for v in product["variants"]}
                events.append(("new", product, ids))
            else:
                ids = {v["id"] for v in product["variants"] if v["available"]}
                if ids:
                    events.append(("restock", product, ids))
            continue

        before_variants = {v["id"]: v for v in before["variants"]}
        added = {v["id"] for v in product["variants"] if v["id"] not in before_variants}
        restocked = {
            v["id"] for v in product["variants"]
            if v["available"]
            and v["id"] in before_variants
            and not before_variants[v["id"]]["available"]
        }
        if added:
            events.append(("new_variant", product, added))
        if restocked:
            events.append(("restock", product, restocked))
    return events


# ---- 通知（共通） -------------------------------------------------------------

LABELS = {
    "new": "🆕 新製品",
    "new_variant": "🆕 新バリエーション追加",
    "restock": "🔁 在庫復活",
}

# 1種類しかない商品のバリエーション名（表示しても意味がないもの）
PLAIN_VARIANT_TITLES = {"", "Default Title", "1 * ボックス"}


def format_price(price):
    try:
        return f"¥{int(float(price)):,}"
    except (TypeError, ValueError):
        return str(price)


def product_url(product):
    # ハンドルに日本語が含まれるため、URLエンコードしてからリンクにする
    return f"{STORE_URL}/products/{quote(product['handle'])}"


def cart_url(variant):
    return f"{STORE_URL}/cart/{variant['id']}:1"


def variant_name(variant):
    return "" if variant["title"] in PLAIN_VARIANT_TITLES else variant["title"]


def target_variants(product, highlight_ids):
    return [v for v in product["variants"] if v["id"] in highlight_ids]


def summary_text(events):
    counts = {}
    for kind, _, _ in events:
        counts[kind] = counts.get(kind, 0) + 1
    return "　".join(f"{LABELS[k]} {n}件" for k, n in counts.items())


def _secret(name):
    """環境変数（本番では Secret Manager から渡される）を読む。未設定・空なら None。"""
    return os.environ.get(name) or None


def notify(events):
    """設定されている通知先すべてに送る。"""
    if _secret("LINE_CHANNEL_ACCESS_TOKEN"):
        notify_line(events)
    if _secret("SLACK_WEBHOOK_URL"):
        notify_slack(events)


def send_text(text):
    """
    監視開始・失敗・復旧などのお知らせを、設定されている通知先すべてに送る。
    LINE は友だち全員ではなく、LINE_USER_ID（管理者）だけに送る。
    """
    if _secret("LINE_CHANNEL_ACCESS_TOKEN") and _secret("LINE_USER_ID"):
        push_line([{"type": "text", "text": text[:5000]}])
    if _secret("SLACK_WEBHOOK_URL"):
        post_slack({"text": text})


# ---- 通知（LINE） -------------------------------------------------------------

def line_image_url(src):
    """Shopify CDN の画像を縮小したURLにする（元画像は4000px超で重いため）。"""
    if not src:
        return ""
    sep = "&" if "?" in src else "?"
    return f"{src}{sep}width={LINE_IMAGE_WIDTH}"


def build_line_bubble(kind, product, highlight_ids):
    targets = target_variants(product, highlight_ids)

    body = [
        {"type": "text", "text": LABELS[kind], "weight": "bold", "size": "sm",
         "color": "#1DB446" if kind != "restock" else "#2F6FDB"},
        {"type": "text", "text": product["title"], "weight": "bold", "size": "md",
         "wrap": True, "maxLines": 4, "margin": "sm"},
        {"type": "separator", "margin": "md"},
    ]
    for v in targets[:MAX_VARIANTS_PER_PRODUCT]:
        name = variant_name(v)
        status = "在庫あり" if v["available"] else "在庫なし"
        line = f"{name}　{format_price(v['price'])}　{status}" if name \
            else f"{format_price(v['price'])}　{status}"
        body.append({"type": "text", "text": line, "size": "sm", "wrap": True,
                     "margin": "sm", "color": "#333333" if v["available"] else "#999999"})
    if len(targets) > MAX_VARIANTS_PER_PRODUCT:
        body.append({"type": "text", "size": "xs", "color": "#999999", "margin": "sm",
                     "text": f"ほか {len(targets) - MAX_VARIANTS_PER_PRODUCT} 件"})

    footer = []
    for v in [v for v in targets if v["available"]][:LINE_MAX_CART_BUTTONS]:
        label = f"🛒 {variant_name(v) or 'カートに入れて開く'}"
        footer.append({"type": "button", "style": "primary", "height": "sm",
                       "action": {"type": "uri", "label": label[:40], "uri": cart_url(v)}})
    footer.append({"type": "button", "style": "link", "height": "sm",
                   "action": {"type": "uri", "label": "商品ページを開く", "uri": product_url(product)}})

    bubble = {
        "type": "bubble",
        "body": {"type": "box", "layout": "vertical", "contents": body},
        "footer": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": footer},
    }
    image = line_image_url(product["image"])
    if image:
        bubble["hero"] = {"type": "image", "url": image, "size": "full",
                          "aspectRatio": "1:1", "aspectMode": "cover",
                          "action": {"type": "uri", "uri": product_url(product)}}
    return bubble


def notify_line(events):
    """
    すべての商品をカルーセルにまとめ、公式アカウントの友だち全員に一斉配信する。
    （無料プランは月200通まで。1回の配信は「受け取った友だちの人数分」の通数として数えられる）
    """
    summary = summary_text(events)
    bubbles = [build_line_bubble(k, p, ids) for k, p, ids in events]
    carousels = [
        {
            "type": "flex",
            "altText": summary,  # スマホのプッシュ通知に表示される文
            "contents": {"type": "carousel",
                         "contents": bubbles[i:i + LINE_BUBBLES_PER_CAROUSEL]},
        }
        for i in range(0, len(bubbles), LINE_BUBBLES_PER_CAROUSEL)
    ]
    for i in range(0, len(carousels), LINE_MESSAGES_PER_REQUEST):
        batch = carousels[i:i + LINE_MESSAGES_PER_REQUEST]
        if _secret("LINE_ADMIN_ONLY") == "1":
            # ローカルで試すときは、友だち全員ではなく自分（LINE_USER_ID）だけに送る
            if _secret("LINE_USER_ID"):
                push_line(batch)
            else:
                print("LINE_ADMIN_ONLY=1 ですが LINE_USER_ID がないため、LINE には送りません")
        else:
            broadcast_line(batch)


def push_line(messages):
    """LINE_USER_ID（管理者）1人に送る。"""
    _post_line(LINE_PUSH_URL, {"to": _secret("LINE_USER_ID"), "messages": messages})


def broadcast_line(messages):
    """公式アカウントの友だち全員に送る。"""
    _post_line(LINE_BROADCAST_URL, {"messages": messages})


def _post_line(url, payload):
    resp = requests.post(
        url,
        json=payload,
        timeout=HTTP_TIMEOUT_SEC,
        headers={"Authorization": f"Bearer {_secret('LINE_CHANNEL_ACCESS_TOKEN')}"},
    )
    if resp.status_code >= 300:
        # 429 は月の上限到達の可能性が高い
        print(f"LINE への送信に失敗（{url}）: HTTP {resp.status_code} {resp.text}")


# ---- 通知（Slack・任意） -------------------------------------------------------

def escape_mrkdwn(text):
    """Slack の mrkdwn で特別な意味を持つ文字をエスケープする。"""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_slack_blocks(kind, product, highlight_ids):
    targets = target_variants(product, highlight_ids)
    lines = [f"*{LABELS[kind]}*", f"<{product_url(product)}|{escape_mrkdwn(product['title'])}>"]
    for v in targets[:MAX_VARIANTS_PER_PRODUCT]:
        name = escape_mrkdwn(variant_name(v))
        name = f"{name}　" if name else ""
        price = format_price(v["price"])
        if v["available"]:
            lines.append(f"・{name}{price}　<{cart_url(v)}|🛒 カートに入れて開く>")
        else:
            lines.append(f"・{name}{price}　（在庫なし）")
    if len(targets) > MAX_VARIANTS_PER_PRODUCT:
        lines.append(f"ほか {len(targets) - MAX_VARIANTS_PER_PRODUCT} 件")

    section = {"type": "section",
               "text": {"type": "mrkdwn", "text": "\n".join(lines)[:SLACK_SECTION_TEXT_LIMIT]}}
    if product["image"]:
        section["accessory"] = {"type": "image", "image_url": product["image"],
                                "alt_text": product["title"][:200]}
    return [section, {"type": "divider"}]


def notify_slack(events):
    summary = summary_text(events)
    for i in range(0, len(events), SLACK_PRODUCTS_PER_MESSAGE):
        blocks = []
        if i == 0:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*{summary}*"}})
        for kind, product, ids in events[i:i + SLACK_PRODUCTS_PER_MESSAGE]:
            blocks.extend(build_slack_blocks(kind, product, ids))
        post_slack({"text": summary, "blocks": blocks})


def post_slack(payload):
    resp = requests.post(_secret("SLACK_WEBHOOK_URL"), json=payload, timeout=HTTP_TIMEOUT_SEC)
    if resp.status_code >= 300:
        print(f"Slack への送信に失敗: HTTP {resp.status_code} {resp.text}")


# ---- 失敗時の処理 -------------------------------------------------------------

def handle_failure(state, now, error):
    failures = state.get("failures", 0) + 1
    wait = min(BACKOFF_MAX_SEC, BACKOFF_MIN_SEC * 2 ** (failures - 1))
    state["failures"] = failures
    state["backoff_until"] = now + wait
    state["last_error"] = str(error)
    detail = getattr(error, "detail", {})
    state["error_log"] = ([{
        "at": datetime.now(timezone.utc).isoformat(),
        "error": str(error),
        **detail,
    }] + state.get("error_log", []))[:ERROR_LOG_SIZE]
    print(f"取得失敗（{failures}回目）: {error} → {wait}秒待機")
    if detail:
        print(f"失敗の詳細: {json.dumps(detail, ensure_ascii=False)}")

    if failures == FAILURE_ALERT_THRESHOLD:
        send_text(
            f"⚠️ 監視が止まっています：連続 {failures} 回取得に失敗しました。\n```{str(error)[:500]}```",
        )
    save_state(state)


# ---- 状態の保存（Firestore） ------------------------------------------------

def build_state(current, seen, error_log=None):
    seen = set(seen) | set(current.keys())
    return {
        "products": current,
        "seen_ids": sorted(seen),
        "failures": 0,
        "backoff_until": 0,
        # 復旧後も、直近の失敗の詳細は後から調べられるように残す
        "error_log": error_log or [],
    }


_firestore_client = None


def _state_doc():
    """Firestore の state/snapshot ドキュメント。クライアントは起動中に使い回す。"""
    global _firestore_client
    if _firestore_client is None:
        from google.cloud import firestore  # ローカルで STATE_FILE を使うときは読み込まない
        _firestore_client = firestore.Client()
    return _firestore_client.collection(STATE_COLLECTION).document(STATE_DOCUMENT)


def load_state():
    # ローカルで試すときは、Firestore の代わりに JSON ファイルを使う
    path = os.environ.get("STATE_FILE")
    if path:
        if not os.path.exists(path):
            return {}
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    snapshot = _state_doc().get()
    # 初回はまだドキュメントがない
    if not snapshot.exists:
        return {}
    return json.loads(snapshot.get("value"))


def save_state(state):
    value = json.dumps(state, ensure_ascii=False)
    path = os.environ.get("STATE_FILE")
    if path:
        with open(path, "w", encoding="utf-8") as f:
            f.write(value)
        return

    _state_doc().set({
        "value": value,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
