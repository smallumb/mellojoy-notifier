import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { cartUrl, cartUrlFor, diff, fetchAllProducts, HttpError, summarize, variantName } from "../lib/shop.js";

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

function page(n) {
  return { status: 200, json: async () => ({ products: Array.from({ length: n }, (_, i) => ({ id: i })) }) };
}

test("fetchAllProducts は250件ちょうどなら次のページも取る", async () => {
  const urls = [];
  const pages = [page(250), page(3)];
  const items = await fetchAllProducts(async (url) => (urls.push(url), pages.shift()));
  assert.equal(items.length, 253);
  assert.match(urls[1], /page=2$/);
});

test("fetchAllProducts は 200 以外で HttpError を投げる", async () => {
  await assert.rejects(fetchAllProducts(async () => ({ status: 429 })), (e) => e instanceof HttpError && e.status === 429);
});

test("cartUrlFor: 1件なら cartUrl と同じ、複数ならカンマでつなぐ", () => {
  assert.equal(cartUrlFor([{ id: 11 }]), cartUrl({ id: 11 }));
  assert.equal(cartUrlFor([{ id: 11 }, { id: 22 }, { id: 33 }]),
    "https://www.mellojoyjapan.com/cart/11:1,22:1,33:1");
});
