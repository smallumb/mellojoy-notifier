"""
メロジョイ公式通販の「新製品」「在庫復活」を検知して LINE の友だち全員（と、設定されていれば Slack）に通知する Cloudflare Python Worker

方針:
- サイトに対しては読み取り（products.json の GET）だけを行う。
- カート投入・購入などの操作は一切しない。
- 通知には、人間がタップして使う商品ページとカートパーマリンクを載せる。
"""

import json
import time
from datetime import datetime, timezone
from urllib.parse import quote

from workers import WorkerEntrypoint, fetch

# ---- 設定 -------------------------------------------------------------------

STORE_URL = "https://www.mellojoyjapan.com"
USER_AGENT = "mellojoy-restock-notifier/1.0 (personal use, read-only)"

PAGE_LIMIT = 250          # products.json の1ページあたり件数（Shopifyの上限）
MAX_PAGES = 10            # 念のための取得ページ上限
STATE_KEY = "snapshot"

FAILURE_ALERT_THRESHOLD = 3   # 連続失敗がこの回数に達したら自分宛てに警告
BACKOFF_MIN_SEC = 120         # 失敗時の待機（2分から倍々）
BACKOFF_MAX_SEC = 1800        # 待機の上限（30分）

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

class Default(WorkerEntrypoint):
    async def scheduled(self, controller, env, ctx):
        # ランタイムのバージョンによっては引数の env が None で渡されるため、
        # WorkerEntrypoint が保持している self.env を優先して使う
        env = getattr(self, "env", None) or env
        now = time.time()
        state = await load_state(env)

        if now < state.get("backoff_until", 0):
            print("前回の失敗により待機中のため、今回はスキップします")
            return

        try:
            raw_products = await fetch_all_products()
        except Exception as e:  # noqa: BLE001
            await handle_failure(env, state, now, e)
            return

        current = summarize(raw_products)
        previous = state.get("products")
        seen = set(state.get("seen_ids", []))

        # 初回は記録のみ（全商品が「新製品」扱いになるのを防ぐ）
        if previous is None:
            await save_state(env, build_state(current, seen))
            await send_text(env, f"サイトの監視を始めたよ 👀（{len(current)}件の商品を記録）")
            return

        events = diff(previous, current, seen)
        if events:
            await notify(env, events)

        if state.get("failures", 0) >= FAILURE_ALERT_THRESHOLD:
            await send_text(env, "✅ 取得が復旧しました")

        await save_state(env, build_state(current, seen))


# ---- 取得 -------------------------------------------------------------------

class HttpError(Exception):
    def __init__(self, status, url):
        super().__init__(f"HTTP {status}: {url}")
        self.status = status


async def fetch_all_products():
    items = []
    for page in range(1, MAX_PAGES + 1):
        url = f"{STORE_URL}/products.json?limit={PAGE_LIMIT}&page={page}"
        resp = await fetch(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        })
        if resp.status != 200:
            raise HttpError(resp.status, url)

        data = json.loads(await resp.text())
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


def _secret(env, name):
    value = getattr(env, name, None)
    return value if isinstance(value, str) and value else None


async def notify(env, events):
    """設定されている通知先すべてに送る。"""
    if _secret(env, "LINE_CHANNEL_ACCESS_TOKEN"):
        await notify_line(env, events)
    if _secret(env, "SLACK_WEBHOOK_URL"):
        await notify_slack(env, events)


async def send_text(env, text):
    """
    監視開始・失敗・復旧などのお知らせを、設定されている通知先すべてに送る。
    LINE は友だち全員ではなく、LINE_USER_ID（管理者）だけに送る。
    """
    if _secret(env, "LINE_CHANNEL_ACCESS_TOKEN") and _secret(env, "LINE_USER_ID"):
        await push_line(env, [{"type": "text", "text": text[:5000]}])
    if _secret(env, "SLACK_WEBHOOK_URL"):
        await post_slack(env, {"text": text})


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


async def notify_line(env, events):
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
        await broadcast_line(env, carousels[i:i + LINE_MESSAGES_PER_REQUEST])


async def push_line(env, messages):
    """LINE_USER_ID（管理者）1人に送る。"""
    await _post_line(env, LINE_PUSH_URL, {"to": env.LINE_USER_ID, "messages": messages})


async def broadcast_line(env, messages):
    """公式アカウントの友だち全員に送る。"""
    await _post_line(env, LINE_BROADCAST_URL, {"messages": messages})


async def _post_line(env, url, payload):
    resp = await fetch(
        url,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {env.LINE_CHANNEL_ACCESS_TOKEN}",
        },
        body=json.dumps(payload, ensure_ascii=False),
    )
    if resp.status >= 300:
        # 429 は月の上限到達の可能性が高い
        print(f"LINE への送信に失敗（{url}）: HTTP {resp.status} {await resp.text()}")


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


async def notify_slack(env, events):
    summary = summary_text(events)
    for i in range(0, len(events), SLACK_PRODUCTS_PER_MESSAGE):
        blocks = []
        if i == 0:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*{summary}*"}})
        for kind, product, ids in events[i:i + SLACK_PRODUCTS_PER_MESSAGE]:
            blocks.extend(build_slack_blocks(kind, product, ids))
        await post_slack(env, {"text": summary, "blocks": blocks})


async def post_slack(env, payload):
    resp = await fetch(
        env.SLACK_WEBHOOK_URL,
        method="POST",
        headers={"Content-Type": "application/json"},
        body=json.dumps(payload, ensure_ascii=False),
    )
    if resp.status >= 300:
        print(f"Slack への送信に失敗: HTTP {resp.status} {await resp.text()}")


# ---- 失敗時の処理 -------------------------------------------------------------

async def handle_failure(env, state, now, error):
    failures = state.get("failures", 0) + 1
    wait = min(BACKOFF_MAX_SEC, BACKOFF_MIN_SEC * 2 ** (failures - 1))
    state["failures"] = failures
    state["backoff_until"] = now + wait
    state["last_error"] = str(error)
    print(f"取得失敗（{failures}回目）: {error} → {wait}秒待機")

    if failures == FAILURE_ALERT_THRESHOLD:
        await send_text(
            env,
            f"⚠️ 監視が止まっています：連続 {failures} 回取得に失敗しました。\n```{str(error)[:500]}```",
        )
    await save_state(env, state)


# ---- 状態の保存（D1） --------------------------------------------------------

def build_state(current, seen):
    seen = set(seen) | set(current.keys())
    return {
        "products": current,
        "seen_ids": sorted(seen),
        "failures": 0,
        "backoff_until": 0,
    }


def _is_js_null(obj):
    """JavaScript の null / undefined を判定する。
    Pyodide では undefined は None に、null は JsNull に変換されるため両方を見る。"""
    return obj is None or type(obj).__name__ == "JsNull"


def _to_py(obj):
    return obj.to_py() if hasattr(obj, "to_py") else obj


async def load_state(env):
    row = await env.DB.prepare(
        "SELECT value FROM state WHERE key = ?"
    ).bind(STATE_KEY).first()
    # 初回はまだ行がないため、D1 は null を返す
    if _is_js_null(row):
        return {}
    row = _to_py(row)
    return json.loads(row["value"])


async def save_state(env, state):
    await env.DB.prepare(
        "INSERT INTO state (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at"
    ).bind(
        STATE_KEY,
        json.dumps(state, ensure_ascii=False),
        datetime.now(timezone.utc).isoformat(),
    ).run()
