/* Popup: toggles, host status, and the media found on the active tab. */

const statusEl = document.getElementById("status");
const mediaEl = document.getElementById("media");
const sendPageEl = document.getElementById("sendPage");
const permissionEl = document.getElementById("permission");
const grantEl = document.getElementById("grant");
const incognitoRowEl = document.getElementById("incognitoRow");
const siteRowEl = document.getElementById("siteRow");
const siteOnEl = document.getElementById("siteOn");
const siteLabelEl = document.getElementById("siteLabel");

function t2(key, subs) {
  try {
    return chrome.i18n.getMessage(key, subs) || key;
  } catch (error) {
    return key;
  }
}

function siteOf(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, "").toLowerCase();
  } catch (error) {
    return "";
  }
}

/** Settings shown as checkboxes, by element id. */
const TOGGLES = ["enabled", "captureMedia", "showButton", "captureIncognito"];
const SITE_ORIGINS = ["http://*/*", "https://*/*"];

function t(key) {
  try {
    return chrome.i18n.getMessage(key) || key;
  } catch (error) {
    return key;
  }
}

function localise() {
  document.documentElement.lang = t("@@ui_locale").replace("_", "-");
  for (const el of document.querySelectorAll("[data-i18n]")) {
    el.textContent = t(el.dataset.i18n);
  }
}

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
  for (const id of TOGGLES) {
    document.getElementById(id).checked = Boolean(settings[id]);
  }
  // One click to leave a site alone - the thing people reach for when a
  // site's own downloader fights with the capture.
  const tab = await activeTab();
  const site = tab && /^https?:/i.test(tab.url || "") ? siteOf(tab.url) : "";
  if (site) {
    siteLabelEl.textContent = t2("popupCaptureOn", [site]);
    siteOnEl.checked = !(settings.excludedSites || []).some(
      (s) => site === s || site.endsWith(`.${s}`)
    );
    siteRowEl.hidden = false;
    siteOnEl.addEventListener("change", () =>
      chrome.runtime.sendMessage({ type: "exclude-site", url: tab.url, excluded: !siteOnEl.checked })
    );
  }
}

async function checkHost() {
  const reply = await chrome.runtime.sendMessage({ type: "ping-host" });
  if (reply && reply.ok) {
    setStatus(`v${reply.version || "?"}`, "ok");
  } else {
    setStatus(t("statusNotRunning"), "bad");
    if (reply && reply.error) statusEl.title = reply.error;
  }
}

/**
 * Firefox before 127 installs an MV3 add-on *without* its host permissions,
 * and without them there are no cookies, no sniffing and no floating button.
 * The user has to grant them, and only from a click.
 */
async function checkPermission() {
  if (!chrome.permissions || !chrome.permissions.contains) return;
  let granted = true;
  try {
    granted = await chrome.permissions.contains({ origins: SITE_ORIGINS });
  } catch (error) {
    granted = true;
  }
  permissionEl.hidden = granted;
}

grantEl.addEventListener("click", () => {
  chrome.permissions
    .request({ origins: SITE_ORIGINS })
    .then((granted) => {
      permissionEl.hidden = Boolean(granted);
    })
    .catch(() => {});
});

async function checkIncognito() {
  // Only worth offering when the user let the extension into private windows.
  try {
    incognitoRowEl.hidden = !(await chrome.extension.isAllowedIncognitoAccess());
  } catch (error) {
    incognitoRowEl.hidden = true;
  }
}

async function loadMedia() {
  const tab = await activeTab();
  const reply = await chrome.runtime.sendMessage({
    type: "get-media",
    tabId: tab ? tab.id : -1
  });
  const items = (reply && reply.items) || [];
  mediaEl.replaceChildren();

  if (!items.length) {
    const empty = document.createElement("li");
    empty.className = "empty";
    empty.textContent = t("popupNoMedia");
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
    button.textContent = t("download");
    button.addEventListener("click", async () => {
      button.disabled = true;
      const response = await chrome.runtime.sendMessage({
        type: "send-media",
        tabId: tab ? tab.id : -1,
        url: item.url
      });
      button.textContent = response && response.ok ? "✓" : "!";
      if (response && !response.ok && response.error) button.title = response.error;
    });

    row.appendChild(label);
    row.appendChild(button);
    mediaEl.appendChild(row);
  });
}

for (const id of TOGGLES) {
  const el = document.getElementById(id);
  el.addEventListener("change", () =>
    chrome.runtime.sendMessage({ type: "set-settings", patch: { [id]: el.checked } })
  );
}

sendPageEl.addEventListener("click", async () => {
  const tab = await activeTab();
  if (!tab || !/^https?:/i.test(tab.url || "")) return;
  sendPageEl.disabled = true;
  // On a site yt-dlp reads, "the page" means the video on it.
  const media = await chrome.runtime.sendMessage({ type: "get-media", tabId: tab.id });
  const page = ((media && media.items) || []).find((m) => m.page && m.url === tab.url);
  const response = page
    ? await chrome.runtime.sendMessage({ type: "send-media", tabId: tab.id, url: tab.url })
    : await chrome.runtime.sendMessage({ type: "send-url", tabId: tab.id, url: tab.url });
  sendPageEl.textContent = response && response.ok ? t("popupSent") : t("popupError");
  sendPageEl.disabled = false;
});

document.getElementById("pickLinks").addEventListener("click", async () => {
  const tab = await activeTab();
  if (!tab || !/^https?:/i.test(tab.url || "")) return;
  const reply = await chrome.runtime.sendMessage({ type: "open-picker", tabId: tab.id });
  if (reply && reply.ok) window.close();
});

document.getElementById("options").addEventListener("click", () => {
  chrome.runtime.openOptionsPage().catch(() => {});
  window.close();
});

localise();
loadSettings();
checkHost();
checkPermission();
checkIncognito();
loadMedia();
