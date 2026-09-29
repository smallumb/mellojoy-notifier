import { DEFAULT_SETTINGS, formatJst } from "./lib/schedule.js";

const form = document.getElementById("settings");
const statusEl = document.getElementById("status");
const logEl = document.getElementById("log");

const RESULT_TEXT = { found: "開きました", notfound: "見つからず", error: "失敗" };

function formatDate(ms) {
  const d = new Date(ms + 9 * 60 * 60 * 1000);
  return `${d.getUTCMonth() + 1}/${d.getUTCDate()} ${formatJst(ms).slice(0, 5)}`;
}

async function render() {
  const { settings, nextRunAt, lastResult, log = [] } =
    await chrome.storage.local.get(["settings", "nextRunAt", "lastResult", "log"]);
  const { run } = await chrome.storage.session.get("run");

  const lines = [];
  if (run && run.status !== "done") {
    lines.push(`🔍 ${run.demo ? "テスト" : "監視"}中（${formatJst(run.end).slice(0, 5)} まで）`);
  } else if (nextRunAt) {
    lines.push(`次回：${formatDate(nextRunAt)}`);
  } else {
    lines.push("自動の監視はオフです");
  }
  if (lastResult) {
    lines.push(`前回（${lastResult.demo ? "テスト・" : ""}${formatDate(lastResult.at)}）：` +
      `${RESULT_TEXT[lastResult.status] ?? lastResult.status}　${lastResult.message}`);
  }
  statusEl.textContent = lines.join("\n");
  statusEl.style.whiteSpace = "pre-line";
  logEl.textContent = log.join("\n") || "（まだありません）";
  logEl.scrollTop = logEl.scrollHeight;

  const s = { ...DEFAULT_SETTINGS, ...settings };
  if (!form.dataset.loaded) {
    form.enabled.checked = s.enabled;
    form.at.value = s.at;
    form.interval.value = s.interval;
    form.duration.value = s.duration;
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
      interval: Math.max(Number(form.interval.value), 3),
      duration: Number(form.duration.value),
      maxTabs: Number(form.maxTabs.value),
    },
  });
});

document.getElementById("test").addEventListener("click", async () => {
  const res = await chrome.runtime.sendMessage({ type: "test" });
  if (!res?.ok) statusEl.textContent = res?.error ?? "テストを始められませんでした";
});

chrome.storage.onChanged.addListener(() => render());
render();
