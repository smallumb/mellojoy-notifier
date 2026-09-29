// 指定の時刻（既定は日本時間 12:00）に追加された商品を見つけて、
// そのカートパーマリンク（開くとチェックアウトへ進む URL）をタブで開く。
//
// 方針:
// - サイトに対しては読み取り（products.json の GET）だけを行う。
// - タブで開くところまで。購入の確定などの操作は人間がする。
// - 取得の間隔と時間に上限を設け、サイトに負担をかけない。
//
// 流れ:
// 1. 開始時刻の1分前に「baseline」アラームで起き、その時点の商品を基準として覚える
// 2. 開始時刻から監視時間のあいだ、数秒おきに取得して基準と比べる
// 3. 在庫のある新製品・新バリエーション・在庫復活が見つかったら、カートパーマリンクを開いて終わる
//
// サービスワーカーは30秒ほど何もしないと止められるため、待つあいだも拡張の API を呼び続ける。
// それでも止められたときは、30秒ごとの「watchdog」アラームで起きて、保存した状態から続きを行う。

import { LABELS, cartUrl, diff, fetchAllProducts, summarize, variantName } from "./lib/shop.js";
import { DEFAULT_SETTINGS, checkoutTargets, formatJst, nextStart } from "./lib/schedule.js";

const MIN_INTERVAL_SEC = 3;          // 取得間隔の下限（サイトへの負担を抑える）
const ERROR_INTERVAL_MAX_SEC = 30;   // 取得に失敗したときの待機の上限
const BASELINE_LEAD_MS = 60 * 1000;  // 開始時刻の何ミリ秒前に基準を取るか
const BASELINE_ATTEMPTS = 3;
const BASELINE_RETRY_MS = 10 * 1000;
const KEEPALIVE_SLICE_MS = 10 * 1000; // 待つあいだ、この間隔で拡張の API を呼んで停止を防ぐ
const LOG_SIZE = 20;

// ---- 設定・状態 -------------------------------------------------------------

async function getSettings() {
  const { settings } = await chrome.storage.local.get("settings");
  const s = { ...DEFAULT_SETTINGS, ...settings };
  s.interval = Math.max(Number(s.interval) || DEFAULT_SETTINGS.interval, MIN_INTERVAL_SEC);
  return s;
}

// 実行中の状態はブラウザを閉じると消える storage.session に置く
async function getRun() {
  return (await chrome.storage.session.get("run")).run ?? null;
}

async function saveRun(run) {
  await chrome.storage.session.set({ run });
}

let logQueue = Promise.resolve();

function log(message) {
  const line = `[${formatJst(Date.now())}] ${message}`;
  console.log(line);
  // 書き込みが前後しないよう、順番に処理する
  logQueue = logQueue.then(async () => {
    const { log = [] } = await chrome.storage.local.get("log");
    await chrome.storage.local.set({ log: [...log, line].slice(-LOG_SIZE) });
  });
  return logQueue;
}

async function sleep(ms) {
  const until = Date.now() + ms;
  for (;;) {
    const remaining = until - Date.now();
    if (remaining <= 0) return;
    await new Promise((r) => setTimeout(r, Math.min(remaining, KEEPALIVE_SLICE_MS)));
    await chrome.runtime.getPlatformInfo();   // 拡張の API を呼び、アイドル停止のタイマーを戻す
  }
}

// ---- 予約 -------------------------------------------------------------------

async function schedule() {
  await chrome.alarms.clear("baseline");
  const settings = await getSettings();
  if (!settings.enabled) {
    await chrome.storage.local.set({ nextRunAt: null });
    return;
  }

  const now = Date.now();
  const durationMs = settings.duration * 60 * 1000;
  let start = nextStart(now, settings.at, durationMs);
  const { doneStart } = await chrome.storage.local.get("doneStart");
  if (start === doneStart) start += 24 * 60 * 60 * 1000;   // 今日の分はもう終えた

  await chrome.storage.local.set({ nextRunAt: start });
  if (now >= start - BASELINE_LEAD_MS) {
    // すでに基準を取る時刻を過ぎている（監視時間の途中でブラウザを起動したときなど）
    const run = await getRun();
    if (!run || run.start !== start) await startRun(start, settings);
    return;
  }
  chrome.alarms.create("baseline", { when: start - BASELINE_LEAD_MS });
}

// ---- 監視 -------------------------------------------------------------------

async function fetchBaseline() {
  for (let attempt = 1; attempt <= BASELINE_ATTEMPTS; attempt++) {
    try {
      return summarize(await fetchAllProducts());
    } catch (e) {
      await log(`基準の取得に失敗しました（${attempt}/${BASELINE_ATTEMPTS}）: ${e.message}`);
      if (attempt < BASELINE_ATTEMPTS) await sleep(BASELINE_RETRY_MS);
    }
  }
  return null;
}

async function startRun(start, settings, { demo = false } = {}) {
  const end = start + settings.duration * 60 * 1000;
  // 基準を取るあいだに watchdog が起きても、前回の状態と取り違えないよう先に保存する
  await saveRun({ start, end, demo, status: "preparing" });
  await chrome.alarms.create("watchdog", { periodInMinutes: 0.5 });
  await chrome.action.setBadgeText({ text: "ON" });
  await log(`${formatJst(start)} から ${formatJst(end)} まで、${settings.interval} 秒おきに確認します`);

  await log("基準となる商品一覧を取得します");
  const baseline = await fetchBaseline();
  if (!baseline) {
    await finish({ start, end, demo }, "error", "基準を取得できなかったため終了しました");
    return;
  }
  if (demo) {
    // 商品を1つ基準から消し、次の取得で「新製品」として見つかるようにする（在庫のあるものを優先）
    const ids = Object.keys(baseline);
    const pid = ids.find((k) => baseline[k].available) ?? ids[0];
    if (!pid) {
      await finish({ start, end, demo }, "error", "商品が1つもないため、テストを行えません");
      return;
    }
    const product = baseline[pid];
    delete baseline[pid];
    const note = product.available ? "" : "（在庫がないため、開いた先は在庫切れの表示になります）";
    await log(`テスト: 「${product.title}」を追加されたものとみなします${note}`);
  }
  await log(`${Object.keys(baseline).length} 件の商品を記録しました`);

  await saveRun({
    start, end, demo,
    interval: settings.interval,
    maxTabs: settings.maxTabs,
    baseline,
    seen: Object.keys(baseline),
    status: "watching",
  });
  await watch();
}

let watching = false;   // 同じサービスワーカーの中で監視ループが2本走らないようにする

async function watch() {
  if (watching) return;
  watching = true;
  try {
    let run = await getRun();
    if (!run || run.status !== "watching") return;

    await sleep(run.start - Date.now());
    let interval = run.interval;
    let polls = run.polls ?? 0;
    if (!polls) await log("監視を始めました");
    while (Date.now() < run.end) {
      let current;
      try {
        polls += 1;
        current = summarize(await fetchAllProducts());
      } catch (e) {
        interval = Math.min(interval * 2, ERROR_INTERVAL_MAX_SEC);
        await log(`取得に失敗しました（${interval} 秒後に再試行）: ${e.message}`);
        await sleep(interval * 1000);
        continue;
      }
      interval = run.interval;

      const seen = new Set(run.seen);
      const targets = checkoutTargets(diff(run.baseline, current, seen), { includeSoldOut: run.demo });
      if (targets.length) {
        const opened = await openTargets(targets, run.maxTabs);
        await finish(run, "found", `${opened.length} 件を開きました`, opened);
        return;
      }

      // 在庫なしで追加された商品が後から在庫ありになるのも拾えるよう、基準を更新する
      for (const pid of Object.keys(current)) seen.add(pid);
      run = { ...run, baseline: current, seen: [...seen], polls };
      await saveRun(run);
      await sleep(interval * 1000);
    }
    await finish(run, "notfound", notFoundMessage(polls));
  } finally {
    watching = false;
  }
}

async function openTargets(targets, maxTabs) {
  const opened = targets.map(([kind, product, v]) => {
    const name = variantName(v);
    return { label: `${LABELS[kind]}　${product.title}${name ? `（${name}）` : ""}`, url: cartUrl(v) };
  });
  const urls = opened.slice(0, maxTabs).map((o) => o.url);

  let win = null;
  try {
    win = await chrome.windows.getLastFocused({ windowTypes: ["normal"] });
  } catch {
    // ウィンドウが1つも開いていない
  }
  if (win) {
    const tabs = await Promise.all(
      urls.map((url, i) => chrome.tabs.create({ windowId: win.id, url, active: i === 0 })),
    );
    await chrome.windows.update(tabs[0].windowId, { focused: true });
  } else {
    await chrome.windows.create({ url: urls, focused: true });
  }

  for (const [i, o] of opened.entries()) {
    await log(`${i < maxTabs ? "開きました" : "（タブ上限のため開かない）"}: ${o.label} ${o.url}`);
  }
  return opened;
}

function notFoundMessage(polls) {
  return `${polls} 回確認しましたが、在庫のある追加商品は見つかりませんでした`;
}

async function finish(run, status, message, opened = []) {
  // 監視ループと watchdog の両方から呼ばれても、終了の処理は1回だけにする
  const saved = await getRun();
  if (saved?.start === run.start && saved.status === "done") return;
  await log(message);
  await chrome.storage.local.set({
    lastResult: { at: Date.now(), status, message, opened, demo: run.demo },
    // テストの実行は、その日の本番の実行済みとしては扱わない
    ...(run.demo ? {} : { doneStart: run.start }),
  });
  await saveRun({ ...run, baseline: null, status: "done" });
  await chrome.alarms.clear("watchdog");
  await chrome.action.setBadgeText({ text: "" });
  await schedule();
}

// ---- イベント -----------------------------------------------------------------

chrome.runtime.onInstalled.addListener(() => schedule());
chrome.runtime.onStartup.addListener(() => schedule());

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.settings) schedule();
});

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === "baseline") {
    const settings = await getSettings();
    const { nextRunAt } = await chrome.storage.local.get("nextRunAt");
    await startRun(nextRunAt, settings);
  } else if (alarm.name === "watchdog") {
    // サービスワーカーが止められていたら、保存した状態から監視を続ける
    const run = await getRun();
    if (!run || run.status === "done") {
      await chrome.alarms.clear("watchdog");
    } else if (watching) {
      // 監視ループは動いている（ループの側で終える）
    } else if (Date.now() >= run.end) {
      await finish(run, "notfound", notFoundMessage(run.polls ?? 0));
    } else if (run.status === "watching") {
      await watch();
    }
    // "preparing"（基準を取得中）のときは、取得が終わるのを待つ
  }
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "test") {
    (async () => {
      const run = await getRun();
      if (run && run.status !== "done") {
        sendResponse({ ok: false, error: "監視中のため、テストは行えません" });
        return;
      }
      sendResponse({ ok: true });
      await startRun(Date.now(), await getSettings(), { demo: true });
    })();
    return true;   // sendResponse を非同期で呼ぶ
  }
  return false;
});
