import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  cartUrl, cartUrlFor, diff, fetchAllProducts, GraphqlError, HttpError, summarize, toRawProduct, variantName,
} from "../lib/shop.js";

const fixture = JSON.parse(readFileSync(new URL("./fixtures/products.json", import.meta.url)));
// src/main.py の summarize / diff に同じ入力を与えた結果（Python 側を変えたら作り直す）
const expected = JSON.parse(readFileSync(new URL("./fixtures/expected_from_python.json", import.meta.url)));

test("summarize は Python 版と同じ結果になる", () => {
  assert.deepEqual(summarize(fixture.previous), expected.previous);
  assert.deepEqual(summarize(fixture.current), expected.current);
});

test("diff は Python 版と同じイベントを返す", () => {
  const events = diff(summarize(fixture.previous), summarize(fixture.current), new Set(fixture.seen));
  assert.deepEqual(
    events.map(([kind, p, ids]) => [kind, p.title, [...ids].sort((a, b) => a - b)]),
    expected.events,
  );
});

test("diff は見たことのない商品の全バリエーションを new にする（在庫なしも含む）", () => {
  const current = summarize([{ id: 9, variants: [{ id: 91, available: false }, { id: 92, available: true }] }]);
  const [[kind, , ids]] = diff({}, current, new Set());
  assert.equal(kind, "new");
  assert.deepEqual([...ids], [91, 92]);
});

test("cartUrl と variantName", () => {
  assert.equal(cartUrl({ id: 123 }), "https://www.mellojoyjapan.com/cart/123:1");
  assert.equal(variantName({ title: "Default Title" }), "");
  assert.equal(variantName({ title: "1 * ボックス" }), "");
  assert.equal(variantName({ title: "赤" }), "赤");
});

function node(id, { variants = [{ id: id * 10, available: true }], more = false, image = true } = {}) {
  return {
    id: `gid://shopify/Product/${id}`,
    title: `商品${id}`,
    handle: `p${id}`,
    featuredImage: image ? { url: `https://cdn/${id}.jpg` } : null,
    variants: {
      pageInfo: { hasNextPage: more },
      nodes: variants.map((v) => ({
        id: `gid://shopify/ProductVariant/${v.id}`,
        title: "Default Title",
        availableForSale: v.available,
        price: { amount: "1000.0" },
      })),
    },
  };
}

function page(nodes, endCursor = null) {
  const body = { data: { products: { pageInfo: { hasNextPage: endCursor !== null, endCursor }, nodes } } };
  return { status: 200, json: async () => body };
}

test("fetchAllProducts は Storefront API に POST し、hasNextPage ならカーソルで次のページも取る", async () => {
  const calls = [];
  const pages = [page([node(1)], "c1"), page([node(2)])];
  const items = await fetchAllProducts(async (url, init) => (calls.push([url, init]), pages.shift()));
  assert.deepEqual(items.map((p) => p.id), [1, 2]);
  assert.equal(calls.length, 2);
  assert.match(calls[0][0], /\/api\/\d{4}-\d{2}\/graphql\.json$/);
  assert.equal(calls[0][1].method, "POST");
  assert.equal(JSON.parse(calls[0][1].body).variables.cursor, null);
  assert.equal(JSON.parse(calls[1][1].body).variables.cursor, "c1");
});

test("fetchAllProducts は 200 以外で HttpError を投げる", async () => {
  await assert.rejects(fetchAllProducts(async () => ({ status: 429 })), (e) => e instanceof HttpError && e.status === 429);
});

test("fetchAllProducts は GraphQL のエラーで GraphqlError を投げる", async () => {
  const body = { errors: [{ message: "Throttled", extensions: { code: "THROTTLED" } }] };
  await assert.rejects(
    fetchAllProducts(async () => ({ status: 200, json: async () => body })),
    (e) => e instanceof GraphqlError && /THROTTLED/.test(e.message),
  );
  await assert.rejects(
    fetchAllProducts(async () => ({ status: 200, json: async () => ({ data: {} }) })),
    GraphqlError,
  );
});

test("toRawProduct はバリエーションを取りきれない商品で GraphqlError を投げる", () => {
  assert.throws(() => toRawProduct(node(1, { more: true })), GraphqlError);
});

test("toRawProduct の結果は products.json と同じように summarize される", () => {
  const nodes = [
    node(1, { variants: [{ id: 11, available: true }, { id: 12, available: false }] }),
    node(2, { variants: [{ id: 21, available: false }], image: false }),
  ];
  const fromJson = [
    {
      id: 1, title: "商品1", handle: "p1", images: [{ src: "https://cdn/1.jpg" }],
      variants: [
        { id: 11, title: "Default Title", available: true, price: "1000.0" },
        { id: 12, title: "Default Title", available: false, price: "1000.0" },
      ],
    },
    {
      id: 2, title: "商品2", handle: "p2", images: [],
      variants: [{ id: 21, title: "Default Title", available: false, price: "1000.0" }],
    },
  ];
  assert.deepEqual(summarize(nodes.map(toRawProduct)), summarize(fromJson));
  assert.equal(cartUrl(toRawProduct(nodes[0]).variants[0]), "https://www.mellojoyjapan.com/cart/11:1");
});

test("cartUrlFor: 1件なら cartUrl と同じ、複数ならカンマでつなぐ", () => {
  assert.equal(cartUrlFor([{ id: 11 }]), cartUrl({ id: 11 }));
  assert.equal(cartUrlFor([{ id: 11 }, { id: 22 }, { id: 33 }]),
    "https://www.mellojoyjapan.com/cart/11:1,22:1,33:1");
});
