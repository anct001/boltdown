/* Popup: toggles, host status, and the media found on the active tab. */

const statusEl = document.getElementById("status");
const enabledEl = document.getElementById("enabled");
const captureEl = document.getElementById("captureMedia");
const mediaEl = document.getElementById("media");
const sendPageEl = document.getElementById("sendPage");

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

function setStatus(text, kind) {
  statusEl.textContent = text;
  statusEl.className = `status ${kind || ""}`.trim();
}

async function loadSettings() {
  const settings = await chrome.runtime.sendMessage({ type: "get-settings" });
  enabledEl.checked = Boolean(settings.enabled);
  captureEl.checked = Boolean(settings.captureMedia);
}

async function checkHost() {
  const reply = await chrome.runtime.sendMessage({ type: "ping-host" });
  if (reply && reply.ok) {
    setStatus(`v${reply.version || "?"}`, "ok");
  } else {
    setStatus("chưa chạy", "bad");
  }
}

async function loadMedia() {
  const tab = await activeTab();
  const reply = await chrome.runtime.sendMessage({
    type: "get-media",
    tabId: tab ? tab.id : -1,
    pageUrl: tab ? tab.url : undefined
  });
  const items = (reply && reply.items) || [];
  mediaEl.innerHTML = "";

  if (!items.length) {
    const empty = document.createElement("li");
    empty.className = "empty";
    empty.textContent = "Không có media nào.";
    mediaEl.appendChild(empty);
    return;
  }

  items.forEach((item) => {
    const row = document.createElement("li");

    const label = document.createElement("div");
    label.className = "name";
    label.textContent = item.name;
    label.title = item.url;
    if (item.page || item.streaming) {
      const tag = document.createElement("div");
      tag.className = "tag";
      tag.textContent = item.page ? "yt-dlp" : "HLS/DASH";
      label.appendChild(tag);
    }

    const button = document.createElement("button");
    button.textContent = "Tải";
    button.addEventListener("click", async () => {
      button.disabled = true;
      const response = await chrome.runtime.sendMessage({
        type: "send-media",
        url: item.url,
        streaming: item.streaming,
        page: item.page,
        referer: tab ? tab.url : undefined
      });
      button.textContent = response && response.ok ? "✓" : "!";
    });

    row.appendChild(label);
    row.appendChild(button);
    mediaEl.appendChild(row);
  });
}

enabledEl.addEventListener("change", () =>
  chrome.runtime.sendMessage({
    type: "set-settings",
    patch: { enabled: enabledEl.checked }
  })
);

captureEl.addEventListener("change", () =>
  chrome.runtime.sendMessage({
    type: "set-settings",
    patch: { captureMedia: captureEl.checked }
  })
);

sendPageEl.addEventListener("click", async () => {
  const tab = await activeTab();
  if (!tab || !/^https?:/i.test(tab.url || "")) return;
  const response = await chrome.runtime.sendMessage({
    type: "send-url",
    url: tab.url,
    referer: tab.url
  });
  sendPageEl.textContent = response && response.ok ? "Đã gửi ✓" : "Lỗi!";
});

loadSettings();
checkHost();
loadMedia();
