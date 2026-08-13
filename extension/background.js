/**
 * IDMClone browser integration - MV3 service worker.
 *
 * Two jobs:
 *  1. Take over ordinary downloads: cancel Chrome's transfer and hand the URL
 *     (plus cookies and referer, or the app would get a login page instead of
 *     the file) to the native host.
 *  2. Watch requests for media URLs so the page can offer a "download this
 *     video" button.
 *
 * The service worker is evicted when idle, so nothing lives in module scope
 * that we cannot rebuild - per-tab media lists go to chrome.storage.session.
 */

const HOST = "com.idmclone.host";

const DEFAULTS = {
  enabled: true,
  captureMedia: true,
  minSize: 0, // bytes; 0 = capture everything
  skipExtensions: [
    "html", "htm", "php", "asp", "aspx", "jsp", "css", "js", "mjs", "json",
    "xml", "svg", "ico", "woff", "woff2", "ttf", "map"
  ]
};

const MEDIA_PATTERN =
  /\.(m3u8|mpd|mp4|m4v|webm|mkv|mov|avi|flv|ts|mp3|m4a|aac|flac|ogg|opus|wav)(\?|#|$)/i;
const STREAM_PATTERN = /\.(m3u8|mpd)(\?|#|$)/i;
const MAX_MEDIA_PER_TAB = 25;

/**
 * Sites whose media URLs are signed, short-lived fragments: sniffing them is
 * useless, so we hand the *page* URL over and let the app ask yt-dlp. Keep in
 * sync with SITE_HOSTS in app/media/detect.py, which decides the same thing.
 */
const SITE_HOSTS = [
  "youtube.com", "youtu.be", "vimeo.com", "dailymotion.com", "twitch.tv",
  "tiktok.com", "facebook.com", "fb.watch", "instagram.com", "twitter.com",
  "x.com", "bilibili.com", "soundcloud.com", "reddit.com", "nicovideo.jp",
  "ok.ru", "vk.com", "rumble.com", "odysee.com"
];

function isSitePage(url) {
  try {
    const host = new URL(url).hostname.replace(/^www\./, "").toLowerCase();
    return SITE_HOSTS.some((site) => host === site || host.endsWith(`.${site}`));
  } catch (error) {
    return false;
  }
}

/** The synthetic "download the video on this page" entry, if it applies. */
function pageEntry(url) {
  if (!url || !isSitePage(url)) return null;
  return { url, name: "Video của trang này (yt-dlp)", page: true, streaming: true };
}

/** Download ids we already took over, so the two events cannot double-fire. */
const handled = new Set();

// --------------------------------------------------------------------- settings

async function getSettings() {
  const stored = await chrome.storage.local.get("settings");
  return Object.assign({}, DEFAULTS, stored.settings || {});
}

async function setSettings(patch) {
  const current = await getSettings();
  const next = Object.assign({}, current, patch);
  await chrome.storage.local.set({ settings: next });
  return next;
}

// ------------------------------------------------------------------ native host

function sendNative(payload) {
  return new Promise((resolve) => {
    try {
      chrome.runtime.sendNativeMessage(HOST, payload, (response) => {
        if (chrome.runtime.lastError) {
          resolve({ ok: false, error: chrome.runtime.lastError.message });
          return;
        }
        resolve(response || { ok: false, error: "empty response" });
      });
    } catch (error) {
      resolve({ ok: false, error: String(error) });
    }
  });
}

function notify(title, message) {
  chrome.notifications
    .create({
      type: "basic",
      iconUrl: chrome.runtime.getURL("icons/icon48.png"),
      title,
      message
    })
    .catch(() => {});
}

async function cookieHeader(url) {
  try {
    const cookies = await chrome.cookies.getAll({ url });
    if (!cookies.length) return undefined;
    return cookies.map((c) => `${c.name}=${c.value}`).join("; ");
  } catch (error) {
    return undefined;
  }
}

// ---------------------------------------------------------------- download hook

function extensionOf(url, filename) {
  const source = filename || url.split("?")[0].split("#")[0];
  const match = /\.([A-Za-z0-9]{1,8})$/.exec(source);
  return match ? match[1].toLowerCase() : "";
}

function baseName(path) {
  if (!path) return undefined;
  const parts = path.split(/[\\/]/);
  return parts[parts.length - 1] || undefined;
}

function shouldSkip(item, settings) {
  const url = item.finalUrl || item.url || "";
  if (!/^https?:/i.test(url)) return true;
  if (settings.minSize && item.fileSize > 0 && item.fileSize < settings.minSize) {
    return true;
  }
  const ext = extensionOf(url, item.filename);
  return ext !== "" && settings.skipExtensions.includes(ext);
}

async function takeOver(item, suggestedName) {
  if (handled.has(item.id)) return;
  handled.add(item.id);
  // Bound the set: the worker may live for hours.
  if (handled.size > 200) handled.clear();

  const settings = await getSettings();
  if (!settings.enabled || shouldSkip(item, settings)) {
    handled.delete(item.id);
    return;
  }

  const url = item.finalUrl || item.url;
  try {
    await chrome.downloads.cancel(item.id);
    await chrome.downloads.erase({ id: item.id });
  } catch (error) {
    // Small files can finish before we get here; nothing to take over then.
    return;
  }

  const payload = {
    type: "download",
    url,
    filename: baseName(suggestedName || item.filename),
    referer: item.referrer || undefined,
    cookie: await cookieHeader(url),
    user_agent: navigator.userAgent,
    size: item.fileSize > 0 ? item.fileSize : undefined,
    mime: item.mime || undefined
  };

  const response = await sendNative(payload);
  if (!response.ok) {
    notify("IDMClone", `Could not hand over the download: ${response.error}`);
  }
}

chrome.downloads.onDeterminingFilename.addListener((item, suggest) => {
  takeOver(item, item.filename);
  // Chrome falls back to the default name; we cancel the transfer anyway.
  suggest();
});

chrome.downloads.onCreated.addListener((item) => {
  // Fallback for downloads that never reach the naming stage.
  setTimeout(() => takeOver(item, item.filename), 150);
});

// ------------------------------------------------------------------ media sniff

function mediaKey(tabId) {
  return `media:${tabId}`;
}

async function rememberMedia(tabId, entry) {
  if (tabId < 0) return;
  const key = mediaKey(tabId);
  const store = await chrome.storage.session.get(key);
  const list = store[key] || [];
  if (list.some((m) => m.url === entry.url)) return;
  list.push(entry);
  while (list.length > MAX_MEDIA_PER_TAB) list.shift();
  await chrome.storage.session.set({ [key]: list });

  chrome.action.setBadgeBackgroundColor({ color: "#1565c0" }).catch(() => {});
  chrome.action.setBadgeText({ tabId, text: String(list.length) }).catch(() => {});
  chrome.tabs
    .sendMessage(tabId, { type: "media-count", count: list.length })
    .catch(() => {});
}

chrome.webRequest.onBeforeRequest.addListener(
  (details) => {
    if (details.tabId < 0) return;
    if (!MEDIA_PATTERN.test(details.url)) return;
    getSettings().then((settings) => {
      if (!settings.captureMedia) return;
      rememberMedia(details.tabId, {
        url: details.url,
        streaming: STREAM_PATTERN.test(details.url),
        name: decodeURIComponent(
          (details.url.split("?")[0].split("/").pop() || "media").slice(0, 80)
        )
      });
    });
  },
  { urls: ["http://*/*", "https://*/*"], types: ["media", "xmlhttprequest", "object", "other"] }
);

chrome.tabs.onRemoved.addListener((tabId) => {
  chrome.storage.session.remove(mediaKey(tabId)).catch(() => {});
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (changeInfo.status !== "loading" || !changeInfo.url) return;
  chrome.storage.session.remove(mediaKey(tabId)).catch(() => {});
  chrome.action.setBadgeText({ tabId, text: "" }).catch(() => {});
});

// --------------------------------------------------------------- message router

async function route(message, sender) {
  switch (message.type) {
    case "get-settings":
      return getSettings();

    case "set-settings":
      return setSettings(message.patch || {});

    case "ping-host":
      return sendNative({ type: "ping" });

    case "get-media": {
      const tabId = message.tabId ?? sender.tab?.id ?? -1;
      const store = await chrome.storage.session.get(mediaKey(tabId));
      const items = store[mediaKey(tabId)] || [];
      const pageUrl = message.pageUrl || sender.tab?.url;
      const page = pageEntry(pageUrl);
      // The page entry goes first: on YouTube it is the only one that works.
      return { items: page ? [page, ...items] : items };
    }

    case "send-media": {
      const response = await sendNative({
        type: "media",
        url: message.url,
        referer: message.referer || sender.tab?.url,
        cookie: await cookieHeader(message.url),
        user_agent: navigator.userAgent,
        streaming: Boolean(message.streaming),
        page: Boolean(message.page)
      });
      if (!response.ok) notify("IDMClone", response.error || "unknown error");
      return response;
    }

    case "send-url": {
      const response = await sendNative({
        type: "download",
        url: message.url,
        referer: message.referer,
        cookie: await cookieHeader(message.url),
        user_agent: navigator.userAgent
      });
      if (!response.ok) notify("IDMClone", response.error || "unknown error");
      return response;
    }

    default:
      return { ok: false, error: `unknown message: ${message.type}` };
  }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  route(message, sender).then(sendResponse, (error) =>
    sendResponse({ ok: false, error: String(error) })
  );
  return true; // keep the channel open for the async reply
});

// ------------------------------------------------------------ context menus

/**
 * Right-click entries. IDM's most-used feature after link capture: you see a
 * link, you send it without navigating to it first.
 */
const MENUS = [
  { id: "idmclone-link", title: "Tải link này bằng IDMClone", contexts: ["link"] },
  { id: "idmclone-media", title: "Tải media này bằng IDMClone",
    contexts: ["image", "video", "audio"] },
  { id: "idmclone-page", title: "Tải mọi link trên trang này", contexts: ["page"] },
  { id: "idmclone-selection", title: "Tải các link vừa bôi đen",
    contexts: ["selection"] }
];

function buildMenus() {
  chrome.contextMenus.removeAll(() => {
    for (const item of MENUS) chrome.contextMenus.create(item);
  });
}

chrome.runtime.onInstalled.addListener(buildMenus);
chrome.runtime.onStartup.addListener(buildMenus);

/** Collect every href on the page (or inside the selection). */
function collectLinks(selectionOnly) {
  const anchors = Array.from(document.querySelectorAll("a[href]"));
  const wanted = anchors.filter((a) => {
    if (!/^https?:/i.test(a.href)) return false;
    if (!selectionOnly) return true;
    const selection = window.getSelection();
    return selection && selection.rangeCount > 0 && selection.containsNode(a, true);
  });
  return Array.from(new Set(wanted.map((a) => a.href)));
}

async function linksOnPage(tabId, selectionOnly) {
  const results = await chrome.scripting.executeScript({
    target: { tabId },
    func: collectLinks,
    args: [Boolean(selectionOnly)]
  });
  return (results && results[0] && results[0].result) || [];
}

async function sendMany(urls, referer) {
  let sent = 0;
  for (const url of urls) {
    const response = await sendNative({
      type: "download",
      url,
      referer,
      cookie: await cookieHeader(url),
      user_agent: navigator.userAgent
    });
    if (response.ok) sent += 1;
    else {
      notify("IDMClone", response.error || "unknown error");
      break;
    }
  }
  return sent;
}

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const settings = await getSettings();
  if (!settings.enabled) {
    notify("IDMClone", "Tiện ích đang tắt trong popup.");
    return;
  }
  const referer = (tab && tab.url) || info.pageUrl;

  if (info.menuItemId === "idmclone-link" && info.linkUrl) {
    await sendMany([info.linkUrl], referer);
    return;
  }
  if (info.menuItemId === "idmclone-media" && (info.srcUrl || info.linkUrl)) {
    await sendMany([info.srcUrl || info.linkUrl], referer);
    return;
  }
  if (!tab || tab.id === undefined) return;

  // Whole page or just the selection: read the links out of the DOM, hand
  // them over one at a time so a failure stops at the first one.
  const selectionOnly = info.menuItemId === "idmclone-selection";
  let urls = [];
  try {
    urls = await linksOnPage(tab.id, selectionOnly);
  } catch (error) {
    notify("IDMClone", `Không đọc được trang: ${error}`);
    return;
  }
  if (!urls.length) {
    notify("IDMClone", "Không thấy link nào.");
    return;
  }
  const sent = await sendMany(urls, referer);
  notify("IDMClone", `Đã gửi ${sent}/${urls.length} link sang IDMClone.`);
});
