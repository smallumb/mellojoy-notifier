// 商品一覧の取得・要約・差分検出。
// 取得だけは Storefront API（GraphQL）で行う（main.py は products.json）。products.json は Shopify の
// ページキャッシュから返り、新商品が画面より数十秒遅れて出てくることがあるため。
// 取得結果は products.json と同じ形に直して返すので、summarize 以降は
// src/main.py の summarize / diff / cart_url / variant_name と同じ振る舞いに揃える
// （片方を直したら、もう片方も直す）。

export const STORE_URL = "https://www.mellojoyjapan.com";

const STOREFRONT_API_VERSION = "2026-07";
const STOREFRONT_URL = `${STORE_URL}/api/${STOREFRONT_API_VERSION}/graphql.json`;
const PAGE_LIMIT = 25;           // 1ページあたりの商品数（重さをトークンなしの上限 1,000 より十分小さく保つ。実測で1商品あたり約23）
const VARIANT_LIMIT = 100;       // 1商品あたりに取るバリエーション数
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

export class GraphqlError extends Error {}

const PRODUCTS_QUERY = `query ($cursor: String) {
  products(first: ${PAGE_LIMIT}, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id title handle
      featuredImage { url }
      variants(first: ${VARIANT_LIMIT}) {
        pageInfo { hasNextPage }
        nodes { id title availableForSale price { amount } }
      }
    }
  }
}`;

/** "gid://shopify/Product/123" → 123（products.json と同じ数値の ID にする） */
function numericId(gid) {
  return Number(gid.slice(gid.lastIndexOf("/") + 1));
}

/** Storefront API の商品を、products.json の商品と同じ形に直す。 */
export function toRawProduct(node) {
  if (node.variants.pageInfo.hasNextPage) {
    throw new GraphqlError(`バリエーションが ${VARIANT_LIMIT} 件を超える商品があります: ${node.title}`);
  }
  return {
    id: numericId(node.id),
    title: node.title,
    handle: node.handle,
    images: node.featuredImage ? [{ src: node.featuredImage.url }] : [],
    variants: node.variants.nodes.map((v) => ({
      id: numericId(v.id),
      title: v.title,
      available: v.availableForSale,
      price: v.price?.amount ?? "",
    })),
  };
}

export async function fetchAllProducts(fetchImpl = fetch) {
  const items = [];
  let cursor = null;
  for (let page = 1; page <= MAX_PAGES; page++) {
    // ブラウザの Cookie と User-Agent のまま送る（ボット対策に止められにくくするため）
    const resp = await fetchImpl(STOREFRONT_URL, {
      method: "POST",
      credentials: "include",
      cache: "no-store",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ query: PRODUCTS_QUERY, variables: { cursor } }),
      signal: AbortSignal.timeout(HTTP_TIMEOUT_MS),
    });
    if (resp.status !== 200) throw new HttpError(resp.status, STOREFRONT_URL);

    const body = await resp.json();
    if (body.errors?.length) {
      const [e] = body.errors;
      throw new GraphqlError(`GraphQL: ${e.message}${e.extensions?.code ? ` (${e.extensions.code})` : ""}`);
    }
    const products = body.data?.products;
    if (!products) throw new GraphqlError("GraphQL: 商品一覧が返ってきませんでした");

    items.push(...products.nodes.map(toRawProduct));
    if (!products.pageInfo.hasNextPage) break;
    cursor = products.pageInfo.endCursor;
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

/** 複数のバリエーションを1個ずつ入れた1つのチェックアウトの URL（拡張だけで使う。main.py にはない）。 */
export function cartUrlFor(variants) {
  return `${STORE_URL}/cart/${variants.map((v) => `${v.id}:1`).join(",")}`;
}

export function variantName(variant) {
  return PLAIN_VARIANT_TITLES.has(variant.title) ? "" : variant.title;
}
