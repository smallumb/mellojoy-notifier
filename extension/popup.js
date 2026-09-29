import { DEFAULT_SETTINGS, formatJst } from "./lib/schedule.js";

const form = document.getElementById("settings");
const statusLabelEl = document.getElementById("status-label");
const autoEl = document.getElementById("auto");
const mainEl = document.getElementById("status-main");
const lastEl = document.getElementById("last");
const errorEl = document.getElementById("error");
const logEl = document.getElementById("log");

const RESULT_TEXT = { found: "開きました", notfound: "見つからず", error: "失敗" };

function formatDay(ms) {
  const d = new Date(ms + 9 * 60 * 60 * 1000);
  return `${d.getUTCMonth() + 1}/${d.getUTCDate()}`;
}

function formatDate(ms) {
  return `${formatDay(ms)} ${formatJst(ms).slice(0, 5)}`;
}

function span(className, text) {
  const el = document.createElement("span");
  el.className = className;
  el.textContent = text;
  return el;
}

async function render() {
  const { settings, nextRunAt, lastResult, log = [] } =
    await chrome.storage.local.get(["settings", "nextRunAt", "lastResult", "log"]);
  const { run } = await chrome.storage.session.get("run");
  const s = { ...DEFAULT_SETTINGS, ...settings };

  autoEl.textContent = s.enabled ? "自動 ON" : "自動 OFF";
  autoEl.classList.toggle("off", !s.enabled);

  mainEl.classList.remove("small");
  if (run && run.status !== "done") {
    statusLabelEl.textContent = run.demo ? "テスト中" : "監視中";
    mainEl.replaceChildren(span("time", formatJst(run.end)), " ", span("suffix", "まで"));
  } else if (nextRunAt) {
    statusLabelEl.textContent = "次回の監視";
    mainEl.replaceChildren(`${formatDay(nextRunAt)} `, span("time", formatJst(nextRunAt).slice(0, 5)));
  } else {
    statusLabelEl.textContent = "次回の監視";
    mainEl.textContent = "自動の監視はオフです";
    mainEl.classList.add("small");
  }

  lastEl.hidden = !lastResult;
  if (lastResult) {
    lastEl.className = lastResult.status;
    lastEl.textContent = `前回（${lastResult.demo ? "テスト・" : ""}${formatDate(lastResult.at)}）：` +
      `${RESULT_TEXT[lastResult.status] ?? lastResult.status}　${lastResult.message}`;
  }

  logEl.textContent = log.join("\n") || "（まだありません）";
  logEl.scrollTop = logEl.scrollHeight;

  if (!form.dataset.loaded) {
    form.enabled.checked = s.enabled;
    form.at.value = s.at;
    form.interval.value = s.interval;
    form.durationSec.value = s.durationSec;
    form.maxTabs.value = s.maxTabs;
    form.dataset.loaded = "1";
  }
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  await chrome.storage.local.set({
    settings: {
      enabled: form.enabled.checked,
      at: form.at.value,
      interval: Math.max(Number(form.interval.value), 0.5),
      durationSec: Number(form.durationSec.value),
      maxTabs: Number(form.maxTabs.value),
    },
  });
});

document.getElementById("test").addEventListener("click", async () => {
  const res = await chrome.runtime.sendMessage({ type: "test" });
  errorEl.hidden = !!res?.ok;
  errorEl.textContent = res?.ok ? "" : res?.error ?? "テストを始められませんでした";
});

chrome.storage.onChanged.addListener(() => render());
render();
