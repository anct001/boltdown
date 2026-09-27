/**
 * Boltdown browser integration - background script.
 *
 * Runs as an MV3 service worker on Chromium and as a background script on
 * Firefox (scripts/build_extension.py shapes the manifest for each).
 *
 * Two jobs:
 *  1. Take over ordinary downloads: cancel the browser's transfer and hand the
 *     URL (plus cookies and referer, or the app would get a login page instead
 *     of the file) to the native host.
 *  2. Watch requests for media URLs so the page can offer a "download this
 *     video" button.
 *
 * The service worker is evicted when idle, so nothing lives in module scope
 * that we cannot rebuild - per-tab media lists go to chrome.storage.session.
 */

const HOST = "com.boltdown.host";

const DEFAULTS = {
  enabled: true,
  captureMedia: true,
  showButton: true,
  // A private window is a request not to leave traces; handing its
  // downloads to an application with a history list would ignore that.
  captureIncognito: false,
  minSize: 0, // bytes; 0 = capture everything
  // Sites whose downloads the browser keeps: a host, or a parent domain.
  excludedSites: [],
  skipExtensions: [
    "html", "htm", "php", "asp", "aspx", "jsp", "css", "js", "mjs", "json",
    "xml", "svg", "ico", "woff", "woff2", "ttf", "map"
  ]
};

// `.ts` is deliberately missing: on the web it is almost always one segment
// of an HLS stream, and a page playing one fetches hundreds of them.
const MEDIA_PATTERN =
  /\.(m3u8|mpd|mp4|m4v|webm|mkv|mov|avi|flv|mp3|m4a|aac|flac|ogg|opus|wav)(\?|#|$)/i;
const STREAM_PATTERN = /\.(m3u8|mpd)(\?|#|$)/i;
const MEDIA_TYPE = /^(video|audio)\/|^application\/(vnd\.apple\.mpegurl|x-mpegurl|dash\+xml)/i;
const STREAM_TYPE = /mpegurl|dash\+xml/i;
/** Pieces of a stream, never something a person wants on its own. */
const SEGMENT_PATTERN = /\.(ts|m4s|m4f|cmfv|cmfa|aac\.part)(\?|#|$)|[/_-](seg|segment|chunk|frag|fragment)[-_]?\d+/i;
const SEGMENT_TYPE = /^video\/(mp2t|iso\.segment)|^audio\/mp2t/i;
/** Smaller than this, a "video" is a preview, an ad beacon or a probe. */
const MIN_MEDIA_BYTES = 48 * 1024;
const MAX_MEDIA_PER_TAB = 25;
const HTTP_URL = /^https?:\/\//i;

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

/** A localised string; the key itself if the locale files are missing one. */
function t(key, substitutions) {
  try {
    return chrome.i18n.getMessage(key, substitutions) || key;
  } catch (error) {
    return key;
  }
}

function isHttp(url) {
  return typeof url === "string" && HTTP_URL.test(url);
}

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
  return { url, name: t("pageVideoName"), page: true, streaming: true };
}

/** A file name out of a URL; a malformed %-escape must not throw. */
function nameFromUrl(url) {
  const last = url.split("#")[0].split("?")[0].split("/").pop() || "media";
  let name = last;
  try {
    name = decodeURIComponent(last);
  } catch (error) {
    // keep it encoded
  }
  return name.slice(0, 80);
}

/** Download ids we already took over, so the two events cannot double-fire. */
const handled = new Set();
const HANDLED_LIMIT = 200;

function markHandled(id) {
  handled.add(id);
  // Bound the set, oldest first: the worker may live for hours, and clearing
  // it wholesale could let an in-flight download be taken over twice.
  while (handled.size > HANDLED_LIMIT) {
    handled.delete(handled.values().next().value);
  }
}

// --------------------------------------------------------------------- settings

let settingsCache = null;

async function getSettings() {
  if (!settingsCache) {
    const stored = await chrome.storage.local.get("settings");
    settingsCache = Object.assign({}, DEFAULTS, stored.settings || {});
  }
  return Object.assign({}, settingsCache);
}

/** Only known keys, only of the type the default has. */
function cleanPatch(patch) {
  const clean = {};
  if (!patch || typeof patch !== "object") return clean;
  for (const [key, value] of Object.entries(patch)) {
    if (!(key in DEFAULTS)) continue;
    const expected = DEFAULTS[key];
    if (Array.isArray(expected)) {
      if (Array.isArray(value) && value.every((v) => typeof v === "string")) {
        clean[key] = value.map((v) => v.toLowerCase());
      }
    } else if (typeof value === typeof expected) {
      clean[key] = value;
    }
  }
  return clean;
}

async function setSettings(patch) {
  const current = await getSettings();
  const next = Object.assign({}, current, cleanPatch(patch));
  settingsCache = next;
  await chrome.storage.local.set({ settings: next });
  return Object.assign({}, next);
}

if (chrome.storage.onChanged) {
  // Another window of the popup, or a sync, changed them under us.
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "local" && changes.settings) settingsCache = null;
  });
}

// ------------------------------------------------------------------ native host

/**
 * One host process for the whole session, reached through a port.
 *
 * `sendNativeMessage` starts a fresh host for every message: an interpreter
 * and its imports each time, a quarter of a second before the app even hears
 * about the link - while the browser keeps downloading the file it is about
 * to give up. A port started once answers in a few milliseconds, and an open
 * native port also keeps the service worker from being evicted.
 *
 * Replies carry back the `seq` of their request. A host from before that
 * convention answers without it, but it answers in order, so the oldest
 * outstanding request is the one being answered.
 */
const NATIVE_TIMEOUT = 30000; // the host may be starting the application
let nativePort = null;
let nativeSeq = 0;
const waiting = new Map(); // seq -> { resolve, timer, port }

function settle(seq, reply) {
  const entry = waiting.get(seq);
  if (!entry) return;
  waiting.delete(seq);
  clearTimeout(entry.timer);
  entry.resolve(reply);
}

function openPort() {
  if (nativePort) return nativePort;
  if (typeof chrome.runtime.connectNative !== "function") return null;
  let port;
  try {
    port = chrome.runtime.connectNative(HOST);
  } catch (error) {
    return null;
  }
  nativePort = port;
  port.onMessage.addListener((message) => {
    let seq = message && message.seq;
    if (!waiting.has(seq)) {
      // An older host: the first request still waiting on this port.
      seq = Array.from(waiting.keys()).find((key) => waiting.get(key).port === port);
    }
    if (seq === undefined) return;
    const reply = Object.assign({}, message);
    delete reply.seq;
    settle(seq, reply);
  });
  port.onDisconnect.addListener(() => {
    const reason =
      (port.error && port.error.message) ||
      (chrome.runtime.lastError && chrome.runtime.lastError.message) ||
      "the native host went away";
    if (nativePort === port) nativePort = null;
    for (const [seq, entry] of Array.from(waiting)) {
      if (entry.port === port) settle(seq, { ok: false, error: reason });
    }
  });
  return port;
}

function sendOnce(payload) {
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

function sendNative(payload) {
  const port = openPort();
  if (!port) return sendOnce(payload);
  return new Promise((resolve) => {
    const seq = ++nativeSeq;
    const timer = setTimeout(
      () => settle(seq, { ok: false, error: "the native host did not answer" }),
      NATIVE_TIMEOUT
    );
    waiting.set(seq, { resolve, timer, port });
    try {
      port.postMessage(Object.assign({}, payload, { seq }));
    } catch (error) {
      if (nativePort === port) nativePort = null;
      settle(seq, { ok: false, error: String(error) });
    }
  });
}

function notify(title, message) {
  Promise.resolve()
    .then(() =>
      chrome.notifications.create({
        type: "basic",
        iconUrl: chrome.runtime.getURL("icons/icon48.png"),
        title,
        message
      })
    )
    .catch(() => {});
}

// ----------------------------------------------------------------------- cookies

/** The cookie store ids a private window uses, per browser family. */
const PRIVATE_STORES = ["1", "firefox-private"];

/**
 * Which cookie jar a request belongs to.
 *
 * Without a store id, `cookies.getAll` reads the normal profile - so a
 * download from a private window, or from a Firefox container, would go out
 * with the cookies of a different identity. Returns undefined for "the
 * default store", or null for "no cookies at all" when the right store
 * cannot be found.
 */
async function cookieStoreFor({ tabId, incognito, cookieStoreId }) {
  if (cookieStoreId) return cookieStoreId;
  const hasTab = typeof tabId === "number" && tabId >= 0;
  if (!hasTab && !incognito) return undefined;
  try {
    const stores = await chrome.cookies.getAllCookieStores();
    if (hasTab) {
      const own = stores.find((store) => (store.tabIds || []).includes(tabId));
      if (own) return own.id;
    }
    if (incognito) {
      const priv = stores.find((store) => PRIVATE_STORES.includes(store.id));
      if (priv) return priv.id;
    }
  } catch (error) {
    // fall through
  }
  return incognito ? null : undefined;
}

async function cookieHeader(url, where) {
  if (!isHttp(url)) return undefined;
  try {
    const storeId = await cookieStoreFor(where || {});
    if (storeId === null) return undefined;
    const query = storeId === undefined ? { url } : { url, storeId };
    const cookies = await chrome.cookies.getAll(query);
    if (!cookies.length) return undefined;
    return cookies.map((c) => `${c.name}=${c.value}`).join("; ");
  } catch (error) {
    return undefined;
  }
}

function whereTab(tab) {
  if (!tab) return {};
  return { tabId: tab.id, incognito: Boolean(tab.incognito), cookieStoreId: tab.cookieStoreId };
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

function hostOf(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, "").toLowerCase();
  } catch (error) {
    return "";
  }
}

/** Is `url` - or the page it came from - on a site the user excluded? */
function isExcluded(settings, ...urls) {
  const sites = settings.excludedSites || [];
  if (!sites.length) return false;
  return urls.some((url) => {
    const host = hostOf(url || "");
    return host && sites.some((site) => host === site || host.endsWith(`.${site}`));
  });
}

function shouldSkip(item, settings) {
  const url = item.finalUrl || item.url || "";
  if (!isHttp(url)) return true;
  if (isExcluded(settings, url, item.referrer)) return true;
  if (item.state && item.state !== "in_progress") return true;
  if (item.byExtensionId && item.byExtensionId === chrome.runtime.id) return true;
  if (item.incognito && !settings.captureIncognito) return true;
  if (settings.minSize && item.fileSize > 0 && item.fileSize < settings.minSize) {
    return true;
  }
  const ext = extensionOf(url, item.filename);
  return ext !== "" && settings.skipExtensions.includes(ext);
}

/** scripts/bench_browser.py sets `__boltdownTiming` to see where time goes. */
function timing(stage, url) {
  const log = globalThis.__boltdownTiming;
  if (Array.isArray(log) && log.length < 1000) log.push({ stage, url, at: Date.now() });
}

async function takeOver(item, suggestedName, via) {
  timing(via || "takeover", item.finalUrl || item.url);
  if (handled.has(item.id)) return;
  markHandled(item.id);

  const settings = await getSettings();
  if (!settings.enabled || shouldSkip(item, settings)) {
    handled.delete(item.id);
    return;
  }

  // Hold the browser's transfer while the app is asked. Letting it run means
  // megabytes downloaded only to be thrown away; cancelling it first means
  // losing the download if the app does not answer. Paused is neither.
  let paused = false;
  try {
    await chrome.downloads.pause(item.id);
    paused = true;
  } catch (error) {
    // not pausable yet (or already finished) - carry on as before
  }

  const url = item.finalUrl || item.url;
  const payload = {
    type: "download",
    url,
    filename: baseName(suggestedName || item.filename),
    referer: isHttp(item.referrer) ? item.referrer : undefined,
    cookie: await cookieHeader(url, {
      incognito: Boolean(item.incognito),
      cookieStoreId: item.cookieStoreId
    }),
    user_agent: navigator.userAgent,
    size: item.fileSize > 0 ? item.fileSize : undefined,
    mime: item.mime || undefined
  };

  // Ask *before* cancelling. The other order loses the download outright when
  // the app cannot be reached - which is what happens after the application is
  // reinstalled, since the uninstaller removes the native-messaging
  // registration. The browser writing a few hundred kilobytes we then throw
  // away is a far smaller price than a download that simply does not happen.
  timing(paused ? "paused-sent" : "sent", url);
  const response = await sendNative(payload);
  timing("answered", url);
  if (!response.ok) {
    handled.delete(item.id);
    if (paused) chrome.downloads.resume(item.id).catch(() => {});
    notify("Boltdown", t("handoverFailed", [String(response.error)]));
    return;
  }

  try {
    await chrome.downloads.cancel(item.id);
    await chrome.downloads.erase({ id: item.id });
  } catch (error) {
    // Small files can finish before we get here. The app has the URL and will
    // download it too; the browser's copy is the duplicate, and the app's
    // duplicate check is what deals with that.
  }
}

// onDeterminingFilename is a Chromium extension to the API; Firefox has no
// such event, and touching `.addListener` on undefined would kill this whole
// script - taking every other listener below with it. onCreated exists
// everywhere and is enough on its own, so this one is a bonus: it fires
// earlier, before the browser has opened its own file.
if (chrome.downloads.onDeterminingFilename) {
  chrome.downloads.onDeterminingFilename.addListener((item, suggest) => {
    takeOver(item, item.filename, "named");
    // Chrome falls back to the default name; we cancel the transfer anyway.
    suggest();
  });
}

chrome.downloads.onCreated.addListener((item) => {
  if (!chrome.downloads.onDeterminingFilename) {
    // Firefox: this is the only event, so there is nothing to wait for.
    takeOver(item, item.filename);
    return;
  }
  // Chromium: a fallback for downloads that never reach the naming stage;
  // the pause gives onDeterminingFilename, which knows the name, first go.
  setTimeout(() => takeOver(item, item.filename, "created+150"), 150);
});

// ------------------------------------------------------------------ media sniff

// storage.session is where the per-tab media list belongs: it is rebuilt by
// browsing and should not outlive the browser. Firefox only grew it in 115,
// so fall back to local storage rather than throwing on every sniffed URL.
const sessionStore = chrome.storage.session || chrome.storage.local;

function mediaKey(tabId) {
  return `media:${tabId}`;
}

async function mediaFor(tabId) {
  if (typeof tabId !== "number" || tabId < 0) return [];
  const key = mediaKey(tabId);
  const store = await sessionStore.get(key);
  return store[key] || [];
}

/**
 * One read-modify-write at a time per tab. A page that starts twenty
 * segment requests at once would otherwise have them all read the same
 * list and each write back its own, keeping only the last one's entry.
 */
const tabChains = new Map();

function serialised(tabId, job) {
  const previous = tabChains.get(tabId) || Promise.resolve();
  const next = previous.catch(() => {}).then(job);
  tabChains.set(tabId, next);
  next.finally(() => {
    if (tabChains.get(tabId) === next) tabChains.delete(tabId);
  }).catch(() => {});
  return next;
}

/** Same file, different byte range: players ask for one mp4 in pieces. */
function mediaIdentity(entry) {
  if (entry.streaming) return entry.url;
  try {
    const u = new URL(entry.url);
    return u.origin + u.pathname;
  } catch (error) {
    return entry.url;
  }
}

function rememberMedia(tabId, entry, settings) {
  if (tabId < 0) return Promise.resolve();
  return serialised(tabId, async () => {
    const list = await mediaFor(tabId);
    const identity = mediaIdentity(entry);
    const known = list.find((m) => mediaIdentity(m) === identity);
    if (known) {
      if (!known.size && entry.size) {
        known.size = entry.size;
        await sessionStore.set({ [mediaKey(tabId)]: list });
      }
      return;
    }
    list.push(entry);
    // Over the limit, the oldest plain file goes first. A playlist is the
    // one entry that downloads the whole stream; it must never be pushed out
    // by what the player fetched after it.
    while (list.length > MAX_MEDIA_PER_TAB) {
      const plain = list.findIndex((m) => !m.streaming);
      list.splice(plain >= 0 ? plain : 0, 1);
    }
    await sessionStore.set({ [mediaKey(tabId)]: list });

    chrome.action.setBadgeBackgroundColor({ color: "#1565c0" }).catch(() => {});
    chrome.action.setBadgeText({ tabId, text: String(list.length) }).catch(() => {});
    if (settings.showButton) {
      chrome.tabs
        .sendMessage(tabId, { type: "media-count", count: list.length })
        .catch(() => {});
    }
  });
}

function header(headers, name) {
  const found = (headers || []).find((h) => h.name.toLowerCase() === name);
  return found ? String(found.value || "") : "";
}

/** The whole file's size: a range reply says it after the slash. */
function totalSize(headers) {
  const range = /\/(\d+)\s*$/.exec(header(headers, "content-range"));
  if (range) return Number(range[1]);
  const length = Number(header(headers, "content-length"));
  return Number.isFinite(length) && length > 0 ? length : 0;
}

/**
 * What a response is, as far as the media list cares - or null.
 *
 * Decided on the reply, not the request: a Content-Type gives away a video
 * served from `/stream?id=42`, and the size tells a film from a thumbnail
 * preview. Stream segments are dropped however they are named.
 */
function classifyMedia(details) {
  if (details.statusCode >= 400) return null;
  const type = header(details.responseHeaders, "content-type").split(";")[0].trim();
  const url = details.url;
  if (SEGMENT_TYPE.test(type) || SEGMENT_PATTERN.test(url)) return null;
  const streaming = STREAM_PATTERN.test(url) || STREAM_TYPE.test(type);
  if (!streaming && !MEDIA_PATTERN.test(url) && !MEDIA_TYPE.test(type)) return null;
  const size = totalSize(details.responseHeaders);
  if (!streaming && size && size < MIN_MEDIA_BYTES) return null;
  return {
    url,
    streaming,
    name: nameFromUrl(url),
    size: size || undefined,
    mime: type || undefined
  };
}

chrome.webRequest.onHeadersReceived.addListener(
  (details) => {
    if (details.tabId < 0) return;
    // On YouTube and friends the sniffed pieces are signed fragments; the
    // page entry is what works there, so the rest is only noise.
    if (isSitePage(details.documentUrl || details.initiator || "")) return;
    const entry = classifyMedia(details);
    if (!entry) return;
    getSettings()
      .then((settings) => {
        if (!settings.captureMedia) return undefined;
        if (details.incognito && !settings.captureIncognito) return undefined;
        return rememberMedia(details.tabId, entry, settings);
      })
      .catch(() => {});
  },
  { urls: ["http://*/*", "https://*/*"], types: ["media", "xmlhttprequest", "object", "other"] },
  ["responseHeaders"]
);

chrome.tabs.onRemoved.addListener((tabId) => {
  sessionStore.remove(mediaKey(tabId)).catch(() => {});
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (changeInfo.status !== "loading" || !changeInfo.url) return;
  serialised(tabId, () => sessionStore.remove(mediaKey(tabId))).catch(() => {});
  chrome.action.setBadgeText({ tabId, text: "" }).catch(() => {});
});

/**
 * The entry for `url` among what this tab is known to offer, or null.
 *
 * `send-media` reads the cookies for its URL and hands them to the
 * application. Accepting any URL from a content script would let a page
 * that compromised its renderer pick whose cookies are read; only what the
 * browser itself saw this tab load, or the tab's own page, may be sent.
 */
async function knownMedia(tabId, pageUrl, url) {
  const page = pageEntry(pageUrl);
  if (page && page.url === url) return page;
  const items = await mediaFor(tabId);
  return items.find((m) => m.url === url) || null;
}

// --------------------------------------------------------------- message router

const EXTENSION_ORIGIN = chrome.runtime.getURL("");

/**
 * The popup, the options page or the link picker - a page of this extension,
 * not a web page. (The picker is a window of its own, so it has a tab; a
 * content script's URL is the web page's, never this origin.)
 */
function fromExtensionPage(sender) {
  return (
    Boolean(sender) &&
    sender.id === chrome.runtime.id &&
    typeof sender.url === "string" &&
    sender.url.startsWith(EXTENSION_ORIGIN)
  );
}

/** Our content script, running in a tab. */
function fromContentScript(sender) {
  return (
    Boolean(sender) &&
    sender.id === chrome.runtime.id &&
    Boolean(sender.tab) &&
    typeof sender.tab.id === "number" &&
    sender.tab.id >= 0
  );
}

async function sendMedia(entry, tab, referer) {
  const response = await sendNative({
    type: "media",
    url: entry.url,
    referer: isHttp(referer) ? referer : undefined,
    cookie: await cookieHeader(entry.url, whereTab(tab)),
    user_agent: navigator.userAgent,
    streaming: Boolean(entry.streaming),
    page: Boolean(entry.page)
  });
  if (!response.ok) notify("Boltdown", response.error || t("unknownError"));
  return response;
}

async function tabById(tabId) {
  try {
    return await chrome.tabs.get(tabId);
  } catch (error) {
    return undefined;
  }
}

/** Messages a content script may send - about its own tab only. */
async function routeContent(message, sender) {
  const tab = sender.tab;
  switch (message.type) {
    case "get-media": {
      const settings = await getSettings();
      const items = await mediaFor(tab.id);
      const page = pageEntry(tab.url);
      // The page entry goes first: on YouTube it is the only one that works.
      return {
        items: page ? [page, ...items] : items,
        showButton: Boolean(settings.showButton) && !isExcluded(settings, tab.url)
      };
    }

    case "send-media": {
      const entry = await knownMedia(tab.id, tab.url, message.url);
      if (!entry) return { ok: false, error: "not a media URL of this tab" };
      return sendMedia(entry, tab, tab.url);
    }

    default:
      return { ok: false, error: `not allowed from a page: ${message.type}` };
  }
}

/** Messages from the popup. */
async function routeExtension(message) {
  switch (message.type) {
    case "get-settings":
      return getSettings();

    case "set-settings":
      return setSettings(message.patch || {});

    case "ping-host":
      return sendNative({ type: "ping" });

    case "get-media": {
      const tab = await tabById(message.tabId);
      if (!tab) return { items: [] };
      const items = await mediaFor(tab.id);
      const page = pageEntry(tab.url);
      return { items: page ? [page, ...items] : items };
    }

    case "send-media": {
      const tab = await tabById(message.tabId);
      if (!tab) return { ok: false, error: "no such tab" };
      const entry = await knownMedia(tab.id, tab.url, message.url);
      if (!entry) return { ok: false, error: "not a media URL of this tab" };
      return sendMedia(entry, tab, tab.url);
    }

    case "exclude-site": {
      const host = hostOf(message.url || "");
      if (!host) return { ok: false, error: "no site" };
      const settings = await getSettings();
      const sites = settings.excludedSites.filter((site) => site !== host);
      if (message.excluded) sites.push(host);
      return setSettings({ excludedSites: sites });
    }

    case "open-picker": {
      const tab = await tabById(message.tabId);
      if (!tab) return { ok: false, error: "no such tab" };
      return openPicker(tab, Boolean(message.selectionOnly));
    }

    case "picker-data": {
      const data = await pickerData(message.key);
      return data ? { ok: true, links: data.links, pageUrl: data.referer } : { ok: false };
    }

    case "picker-send": {
      const data = await pickerData(message.key);
      if (!data) return { ok: false, error: "the list has expired" };
      // Only links that really were on the page: the picker cannot add any.
      const offered = new Set(data.links.map((link) => link.url));
      const urls = (Array.isArray(message.urls) ? message.urls : []).filter(
        (url) => offered.has(url)
      );
      if (!urls.length) return { ok: false, error: "nothing selected" };
      const tab = await tabById(data.tabId);
      const response = await sendLinks(urls, data.referer, tab || { incognito: data.incognito });
      if (response.ok) await sessionStore.remove(pickerKey(message.key)).catch(() => {});
      return response;
    }

    case "send-url": {
      if (!isHttp(message.url)) return { ok: false, error: "only http(s) URLs" };
      const tab = await tabById(message.tabId);
      const response = await sendNative({
        type: "download",
        url: message.url,
        referer: tab && isHttp(tab.url) ? tab.url : undefined,
        cookie: await cookieHeader(message.url, whereTab(tab)),
        user_agent: navigator.userAgent
      });
      if (!response.ok) notify("Boltdown", response.error || t("unknownError"));
      return response;
    }

    default:
      return { ok: false, error: `unknown message: ${message.type}` };
  }
}

async function route(message, sender) {
  if (!message || typeof message.type !== "string") {
    return { ok: false, error: "malformed message" };
  }
  if (fromExtensionPage(sender)) return routeExtension(message);
  if (fromContentScript(sender)) return routeContent(message, sender);
  return { ok: false, error: "unknown sender" };
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
function menus() {
  return [
    { id: "boltdown-link", title: t("menuLink"), contexts: ["link"] },
    { id: "boltdown-media", title: t("menuMedia"), contexts: ["image", "video", "audio"] },
    { id: "boltdown-page", title: t("menuPage"), contexts: ["page"] },
    { id: "boltdown-selection", title: t("menuSelection"), contexts: ["selection"] }
  ];
}

function buildMenus() {
  chrome.contextMenus.removeAll(() => {
    for (const item of menus()) {
      chrome.contextMenus.create(item, () => void chrome.runtime.lastError);
    }
  });
}

chrome.runtime.onInstalled.addListener(buildMenus);
chrome.runtime.onStartup.addListener(buildMenus);

/**
 * Collect every link on the page (or inside the selection), with its text.
 * Runs inside the page, so it must stand alone.
 */
function collectLinks(selectionOnly) {
  const selection = selectionOnly ? window.getSelection() : null;
  const inside = (el) =>
    !selectionOnly ||
    (selection && selection.rangeCount > 0 && selection.containsNode(el, true));
  const found = new Map();
  const add = (url, text) => {
    if (!url || !/^https?:/i.test(url) || found.has(url)) return;
    found.set(url, { url, text: (text || "").replace(/\s+/g, " ").trim().slice(0, 120) });
  };
  for (const a of document.querySelectorAll("a[href]")) {
    if (inside(a)) add(a.href, a.textContent || a.title);
  }
  for (const el of document.querySelectorAll("video[src], audio[src], source[src]")) {
    if (inside(el)) add(el.src, "");
  }
  return Array.from(found.values()).slice(0, 5000);
}

async function linksOnPage(tabId, selectionOnly) {
  const results = await chrome.scripting.executeScript({
    target: { tabId },
    func: collectLinks,
    args: [Boolean(selectionOnly)]
  });
  const found = (results && results[0] && results[0].result) || [];
  return Array.isArray(found)
    ? found.filter((link) => link && isHttp(link.url))
    : [];
}

/** One link, the way a clicked download goes: the app may ask first. */
async function sendOne(url, referer, tab) {
  if (!isHttp(url)) return { ok: false, error: "only http(s) URLs" };
  const response = await sendNative({
    type: "download",
    url,
    referer: isHttp(referer) ? referer : undefined,
    cookie: await cookieHeader(url, whereTab(tab)),
    user_agent: navigator.userAgent
  });
  if (!response.ok) notify("Boltdown", response.error || t("unknownError"));
  return response;
}

const BATCH_SIZE = 500;

/**
 * Many links in as few messages as possible. They used to go one message
 * each - fifty links, fifty round trips, three seconds - and each one could
 * open its own confirmation in the app. The picker was the confirmation.
 */
async function sendLinks(urls, referer, tab) {
  const where = whereTab(tab);
  const items = [];
  for (const url of urls) {
    if (!isHttp(url)) continue;
    const cookie = await cookieHeader(url, where);
    items.push(cookie ? { url, cookie } : { url });
  }
  let sent = 0;
  for (let i = 0; i < items.length; i += BATCH_SIZE) {
    const response = await sendNative({
      type: "batch",
      referer: isHttp(referer) ? referer : undefined,
      user_agent: navigator.userAgent,
      items: items.slice(i, i + BATCH_SIZE)
    });
    if (!response.ok) {
      notify("Boltdown", response.error || t("unknownError"));
      return { ok: false, error: response.error, sent };
    }
    sent += Math.min(BATCH_SIZE, items.length - i);
  }
  notify("Boltdown", t("sentLinks", [String(sent), String(items.length)]));
  return { ok: true, sent };
}

// ------------------------------------------------------------- link picker

function pickerKey(key) {
  return `picker:${String(key).replace(/[^A-Za-z0-9]/g, "")}`;
}

async function pickerData(key) {
  if (!key) return null;
  const store = await sessionStore.get(pickerKey(key));
  return store[pickerKey(key)] || null;
}

/**
 * IDM's "download all links": a list to pick from, never the whole page
 * blindly - a page's links are mostly navigation.
 */
async function openPicker(tab, selectionOnly) {
  let links;
  try {
    links = await linksOnPage(tab.id, selectionOnly);
  } catch (error) {
    notify("Boltdown", t("pageUnreadable", [String(error)]));
    return { ok: false, error: String(error) };
  }
  if (!links.length) {
    notify("Boltdown", t("noLinks"));
    return { ok: false, error: "no links" };
  }
  const key = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
  await sessionStore.set({
    [pickerKey(key)]: { links, referer: tab.url, tabId: tab.id, incognito: Boolean(tab.incognito) }
  });
  const url = chrome.runtime.getURL(`picker/picker.html#${key}`);
  try {
    await chrome.windows.create({ url, type: "popup", width: 760, height: 620, incognito: Boolean(tab.incognito) });
  } catch (error) {
    await chrome.tabs.create({ url });
  }
  return { ok: true, key, count: links.length };
}

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const settings = await getSettings();
  if (!settings.enabled) {
    notify("Boltdown", t("extensionOff"));
    return;
  }
  const referer = (tab && tab.url) || info.pageUrl;

  if (info.menuItemId === "boltdown-link" && info.linkUrl) {
    await sendOne(info.linkUrl, referer, tab);
    return;
  }
  if (info.menuItemId === "boltdown-media" && (info.srcUrl || info.linkUrl)) {
    await sendOne(info.srcUrl || info.linkUrl, referer, tab);
    return;
  }
  if (!tab || tab.id === undefined) return;
  await openPicker(tab, info.menuItemId === "boltdown-selection");
});

// ------------------------------------------------------------- shortcuts

if (chrome.commands && chrome.commands.onCommand) {
  chrome.commands.onCommand.addListener(async (command, commandTab) => {
    const tab = commandTab || (await chrome.tabs.query({ active: true, currentWindow: true }))[0];
    if (!tab || !isHttp(tab.url)) return;
    if (command === "download-links") {
      await openPicker(tab, false);
      return;
    }
    if (command === "download-video") {
      const page = pageEntry(tab.url);
      if (page) {
        await sendMedia(page, tab, tab.url);
        return;
      }
      chrome.tabs.sendMessage(tab.id, { type: "open-panel" }).catch(() => {});
    }
  });
}

// Start the host now, so the first click of the session does not pay for it.
getSettings()
  .then((settings) => {
    if (settings.enabled) openPort();
  })
  .catch(() => {});
