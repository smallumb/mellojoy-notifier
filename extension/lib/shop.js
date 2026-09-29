// 商品一覧の取得・要約・差分検出。
// src/main.py の fetch_all_products / summarize / diff / cart_url / variant_name と同じ振る舞いに揃える
// （片方を直したら、もう片方も直す）。

export const STORE_URL = "https://www.mellojoyjapan.com";

const PAGE_LIMIT = 250;          // products.json の1ページあたり件数（Shopifyの上限）
const MAX_PAGES = 10;            // 念のための取得ページ上限
const HTTP_TIMEOUT_MS = 20000;   // 1回の取得のタイムアウト

export const LABELS = {
  new: "🆕 新製品",
  new_variant: "🆕 新バリエーション追加",
  restock: "🔁 在庫復活",
};

// 1種類しかない商品のバリエーション名（表示しても意味がないもの）
const PLAIN_VARIANT_TITLES = new Set(["", "Default Title", "1 * ボックス"]);

export class HttpError extends Error {
  constructor(status, url) {
    super(`HTTP ${status}: ${url}`);
    this.status = status;
  }
}

export async function fetchAllProducts(fetchImpl = fetch) {
  const items = [];
  for (let page = 1; page <= MAX_PAGES; page++) {
    const url = `${STORE_URL}/products.json?limit=${PAGE_LIMIT}&page=${page}`;
    // ブラウザの Cookie と User-Agent のまま送る（ボット対策に止められにくくするため）
    const resp = await fetchImpl(url, {
      credentials: "include",
      cache: "no-store",
      headers: { Accept: "application/json" },
      signal: AbortSignal.timeout(HTTP_TIMEOUT_MS),
    });
    if (resp.status !== 200) throw new HttpError(resp.status, url);

    const batch = (await resp.json()).products ?? [];
    items.push(...batch);
    if (batch.length < PAGE_LIMIT) break;
  }
  return items;
}

/** 比較と表示に必要な項目だけを取り出す。 */
export function summarize(rawProducts) {
  const result = {};
  for (const p of rawProducts) {
    const variants = (p.variants ?? []).map((v) => ({
      id: v.id,
      title: v.title ?? "",
      available: Boolean(v.available),
      price: v.price ?? "",
    }));
    const images = p.images ?? [];
    result[String(p.id)] = {
      title: p.title ?? "",
      handle: p.handle ?? "",
      image: images.length ? images[0].src ?? "" : "",
      available: variants.some((v) => v.available),
      variants,
    };
  }
  return result;
}

/**
 * バリエーション単位で判定し、[種類, 商品, 強調するバリエーションIDの Set] の配列を返す。
 * new         : 一度も見たことのない商品が現れた
 * new_variant : 既存の商品に新しいバリエーションが追加された
 * restock     : バリエーションが 在庫なし→在庫あり になった
 *               （一度消えた商品が在庫ありで再掲載された場合も含む）
 */
export function diff(previous, current, seen) {
  const events = [];
  for (const [pid, product] of Object.entries(current)) {
    const before = previous[pid];

    if (before === undefined) {
      if (!seen.has(pid)) {
        events.push(["new", product, new Set(product.variants.map((v) => v.id))]);
      } else {
        const ids = new Set(product.variants.filter((v) => v.available).map((v) => v.id));
        if (ids.size) events.push(["restock", product, ids]);
      }
      continue;
    }

    const beforeVariants = new Map(before.variants.map((v) => [v.id, v]));
    const added = new Set(product.variants.filter((v) => !beforeVariants.has(v.id)).map((v) => v.id));
    const restocked = new Set(
      product.variants
        .filter((v) => v.available && beforeVariants.has(v.id) && !beforeVariants.get(v.id).available)
        .map((v) => v.id),
    );
    if (added.size) events.push(["new_variant", product, added]);
    if (restocked.size) events.push(["restock", product, restocked]);
  }
  return events;
}

export function cartUrl(variant) {
  return `${STORE_URL}/cart/${variant.id}:1`;
}

export function variantName(variant) {
  return PLAIN_VARIANT_TITLES.has(variant.title) ? "" : variant.title;
}
