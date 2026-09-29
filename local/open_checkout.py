"""
指定の時刻（既定は日本時間 12:00）に追加された商品を見つけて、
そのカートパーマリンク（開くとチェックアウトへ進む URL）を Mac の既定のブラウザで開く。
自分の Mac で動かす（Cloud Run にはデプロイしない）。

方針:
- サイトに対しては読み取り（products.json の GET）だけを行う。
- ブラウザで URL を開くところまで。カート投入の確定・支払いなどの操作は人間がする。
- 取得の間隔と時間に上限を設け、サイトに負担をかけない。

流れ:
1. 開始時刻の少し前に products.json を取得し、その時点の商品を「基準」として覚える
2. 開始時刻になったら、数秒おきに取得して基準と比べる
3. 新製品・新バリエーション・在庫復活のうち在庫のあるものが見つかったら、そのカートパーマリンクを開いて終わる
"""

import argparse
import sys
import time
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# 取得・差分の処理は本番の関数（src/main.py）と同じものを使う
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from main import LABELS, cart_url, diff, fetch_all_products, summarize, variant_name  # noqa: E402

JST = ZoneInfo("Asia/Tokyo")

BASELINE_LEAD_SEC = 60        # 開始時刻の何秒前に基準を取るか
BASELINE_ATTEMPTS = 3         # 基準の取得を試す回数
BASELINE_RETRY_SEC = 10
MIN_INTERVAL_SEC = 3          # 取得間隔の下限（サイトへの負担を抑える）
ERROR_INTERVAL_MAX_SEC = 30   # 取得に失敗したときの待機の上限


def log(message):
    print(f"[{datetime.now(JST):%H:%M:%S}] {message}", flush=True)


def parse_args():
    p = argparse.ArgumentParser(description="指定の時刻に追加された商品のチェックアウト URL をブラウザで開く")
    p.add_argument("--at", default="12:00", help="監視を始める時刻（日本時間、HH:MM。既定 12:00）")
    p.add_argument("--interval", type=float, default=5, help=f"取得の間隔（秒。既定 5、下限 {MIN_INTERVAL_SEC}）")
    p.add_argument("--duration", type=float, default=5, help="監視を続ける時間（分。既定 5）")
    p.add_argument("--max-tabs", type=int, default=5, help="一度に開くタブの上限（既定 5）")
    p.add_argument("--dry-run", action="store_true", help="ブラウザを開かず、URL を表示するだけ")
    p.add_argument("--demo", action="store_true",
                   help="動作確認用。すぐに始め、商品を1つ「追加された」とみなして開く（在庫がなくても開く）")
    args = p.parse_args()
    args.interval = max(args.interval, MIN_INTERVAL_SEC)
    return args


def start_time(at):
    """今日の指定時刻。すでに監視時間を過ぎていても、その時刻を返す（呼び出し側で判断する）。"""
    hour, minute = (int(x) for x in at.split(":"))
    return datetime.now(JST).replace(hour=hour, minute=minute, second=0, microsecond=0)


def sleep_until(target):
    """指定時刻まで待つ。近づくほど細かく起きて、誤差を小さくする。"""
    while True:
        remaining = (target - datetime.now(JST)).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(remaining / 2, 30) if remaining > 0.05 else remaining)


def fetch_baseline():
    for attempt in range(1, BASELINE_ATTEMPTS + 1):
        try:
            return summarize(fetch_all_products())
        except Exception as e:  # noqa: BLE001
            log(f"基準の取得に失敗しました（{attempt}/{BASELINE_ATTEMPTS}）: {e}")
            if attempt < BASELINE_ATTEMPTS:
                time.sleep(BASELINE_RETRY_SEC)
    return None


def checkout_targets(events, include_sold_out=False):
    """開く対象（種類, 商品, バリエーション）を、在庫のあるものだけ並べる。
    在庫なしで追加されて後から在庫ありになった商品は「在庫復活」として届くため、在庫復活も対象にする。"""
    targets = []
    for kind, product, ids in events:
        for v in product["variants"]:
            if v["id"] in ids and (v["available"] or include_sold_out):
                targets.append((kind, product, v))
    return targets


def open_targets(targets, max_tabs, dry_run):
    for i, (kind, product, v) in enumerate(targets):
        name = variant_name(v)
        label = f"{LABELS[kind]}　{product['title']}" + (f"（{name}）" if name else "")
        url = cart_url(v)
        if i < max_tabs and not dry_run:
            log(f"開きます: {label}\n    {url}")
            webbrowser.open(url)
        else:
            log(f"{'（表示のみ）' if dry_run else '（タブ上限のため開かない）'} {label}\n    {url}")


def main():
    args = parse_args()

    if args.demo:
        start = datetime.now(JST)
    else:
        start = start_time(args.at)
    end = start + timedelta(minutes=args.duration)
    if datetime.now(JST) >= end:
        log(f"今日の監視時間（{start:%H:%M}〜{end:%H:%M}）はもう過ぎています")
        return 1

    log(f"{start:%H:%M:%S} から {end:%H:%M:%S} まで、{args.interval:g} 秒おきに確認します")
    sleep_until(start - timedelta(seconds=BASELINE_LEAD_SEC))

    log("基準となる商品一覧を取得します")
    baseline = fetch_baseline()
    if baseline is None:
        log("基準を取得できなかったため終了します")
        return 1
    if args.demo:
        # 商品を1つ基準から消し、次の取得で「新製品」として見つかるようにする（在庫のあるものを優先）
        pid = next((k for k, v in baseline.items() if v["available"]), next(iter(baseline), None))
        if pid is None:
            log("商品が1つもないため、デモを行えません")
            return 1
        product = baseline.pop(pid)
        note = "" if product["available"] else "（在庫がないため、開いた先は在庫切れの表示になります）"
        log(f"デモ: 「{product['title']}」を追加されたものとみなします{note}")
    log(f"{len(baseline)} 件の商品を記録しました")

    sleep_until(start)
    seen = set(baseline.keys())
    interval = args.interval
    while datetime.now(JST) < end:
        try:
            current = summarize(fetch_all_products())
        except Exception as e:  # noqa: BLE001
            interval = min(interval * 2, ERROR_INTERVAL_MAX_SEC)
            log(f"取得に失敗しました（{interval:g} 秒後に再試行）: {e}")
            time.sleep(interval)
            continue
        interval = args.interval

        targets = checkout_targets(diff(baseline, current, seen), include_sold_out=args.demo)
        if targets:
            open_targets(targets, args.max_tabs, args.dry_run)
            return 0

        # 在庫なしで追加された商品が後から在庫ありになるのも拾えるよう、基準を更新する
        baseline = current
        seen |= set(current.keys())
        time.sleep(interval)

    log("監視時間内に、在庫のある追加商品は見つかりませんでした")
    return 0


if __name__ == "__main__":
    sys.exit(main())
