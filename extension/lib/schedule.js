// 監視の時刻計算と、開く対象の選び方。chrome.* に依存しない（node --test で確かめる）。

// 設定の既定値（時刻は日本時間、間隔と監視時間は秒）
export const DEFAULT_SETTINGS = { enabled: true, at: "12:00", interval: 0.5, durationSec: 15, maxTabs: 5 };

const JST_OFFSET_MS = 9 * 60 * 60 * 1000;   // 日本時間は夏時間がないので固定

/**
 * 次の監視開始時刻（epoch ms）を返す。at は日本時間の "HH:MM"。
 * 今日の監視時間（開始〜開始 + durationMs）がまだ終わっていなければ今日、終わっていれば明日。
 */
export function nextStart(nowMs, at, durationMs) {
  const [hour, minute] = at.split(":").map(Number);
  const jst = new Date(nowMs + JST_OFFSET_MS);
  let start = Date.UTC(jst.getUTCFullYear(), jst.getUTCMonth(), jst.getUTCDate(), hour, minute) - JST_OFFSET_MS;
  if (nowMs >= start + durationMs) start += 24 * 60 * 60 * 1000;
  return start;
}

/**
 * 開く対象（[種類, 商品, バリエーション]）を、在庫のあるものだけ並べる。
 * 在庫なしで追加されて後から在庫ありになった商品は「在庫復活」として届くため、在庫復活も対象にする。
 */
export function checkoutTargets(events, { includeSoldOut = false } = {}) {
  const targets = [];
  for (const [kind, product, ids] of events) {
    for (const v of product.variants) {
      if (ids.has(v.id) && (v.available || includeSoldOut)) targets.push([kind, product, v]);
    }
  }
  return targets;
}

/** 表示用の時刻（日本時間の HH:MM:SS）。 */
export function formatJst(ms) {
  return new Date(ms + JST_OFFSET_MS).toISOString().slice(11, 19);
}
