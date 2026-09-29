import { test } from "node:test";
import assert from "node:assert/strict";

import { checkoutTargets, formatJst, lateBaseline, nextStart } from "../lib/schedule.js";

const MIN = 60 * 1000;
const DAY = 24 * 60 * MIN;
const jst = (s) => Date.parse(`${s}+09:00`);

test("nextStart: 開始前なら今日", () => {
  assert.equal(nextStart(jst("2026-09-29T11:00:00"), "12:00", 5 * MIN), jst("2026-09-29T12:00:00"));
});

test("nextStart: 監視時間の途中なら今日", () => {
  assert.equal(nextStart(jst("2026-09-29T12:03:00"), "12:00", 5 * MIN), jst("2026-09-29T12:00:00"));
});

test("nextStart: 監視時間を過ぎたら明日", () => {
  assert.equal(nextStart(jst("2026-09-29T12:05:00"), "12:00", 5 * MIN), jst("2026-09-29T12:00:00") + DAY);
});

test("nextStart: UTC では前日にあたる時刻でも、日本時間の日付で計算する", () => {
  // 日本時間 2026-10-01 08:00 は UTC では 9/30 23:00
  assert.equal(nextStart(jst("2026-10-01T08:00:00"), "12:00", 5 * MIN), jst("2026-10-01T12:00:00"));
  // 月末の日付またぎ
  assert.equal(nextStart(jst("2026-09-30T23:00:00"), "00:30", 5 * MIN), jst("2026-10-01T00:30:00"));
});

test("checkoutTargets: 在庫のあるバリエーションだけ、テスト時は在庫なしも含める", () => {
  const product = { variants: [{ id: 1, available: true }, { id: 2, available: false }, { id: 3, available: true }] };
  const events = [["new", product, new Set([1, 2])]];
  assert.deepEqual(checkoutTargets(events).map(([, , v]) => v.id), [1]);
  assert.deepEqual(checkoutTargets(events, { includeSoldOut: true }).map(([, , v]) => v.id), [1, 2]);
});

test("formatJst", () => {
  assert.equal(formatJst(jst("2026-09-29T12:00:05")), "12:00:05");
});

test("lateBaseline: 開始前なら、いま取得した一覧を使う", () => {
  assert.equal(lateBaseline(jst("2026-09-29T11:59:00"), jst("2026-09-29T12:00:00"), null), undefined);
});

test("lateBaseline: 開始後なら、開始前に保存した一覧を使う", () => {
  const snapshot = { at: jst("2026-09-28T12:00:09"), products: {} };
  assert.equal(lateBaseline(jst("2026-09-29T12:00:03"), jst("2026-09-29T12:00:00"), snapshot), snapshot);
});

test("lateBaseline: 開始後で、開始後に取った一覧しかなければ null", () => {
  const snapshot = { at: jst("2026-09-29T12:00:01"), products: {} };
  assert.equal(lateBaseline(jst("2026-09-29T12:00:03"), jst("2026-09-29T12:00:00"), snapshot), null);
  assert.equal(lateBaseline(jst("2026-09-29T12:00:03"), jst("2026-09-29T12:00:00"), undefined), null);
});
