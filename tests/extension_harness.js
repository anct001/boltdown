// Runs the extension's background script against a fake browser, so the
// download hand-over can be tested without Chrome.
//
//   node tests/extension_harness.js <"ok" | "fail" | "incognito" | "router">
//
// Prints a JSON trace of what the script did to the fake browser: which
// native messages it sent, and whether it cancelled the browser's own
// download. The question that matters is what happens when the native host
// does not answer - the browser download must survive.

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const mode = process.argv[2] || "ok";
const trace = {
  native: [], cancelled: [], erased: [], paused: [], resumed: [], notified: [],
  cookieQueries: [], replies: {}, ports: 0, media: null
};

// chrome.i18n backed by the real English catalogue, placeholders and all, so
// a key missing from it shows up as the bare key in the trace.
const catalogue = JSON.parse(fs.readFileSync(
  path.join(__dirname, "..", "extension", "_locales", "en", "messages.json"), "utf8"
));
function getMessage(key, subs) {
  const entry = catalogue[key];
  if (!entry) return "";
  const args = subs === undefined ? [] : [].concat(subs);
  return entry.message.replace(/\$([A-Za-z0-9_]+)\$/g, (whole, name) => {
    const ph = (entry.placeholders || {})[name.toLowerCase()];
    if (!ph) return whole;
    return ph.content.replace(/\$(\d)/g, (_m, n) => args[Number(n) - 1] ?? "");
  });
}

const TABS = {
  3: { id: 3, url: "https://www.youtube.com/watch?v=abc", incognito: false },
  4: { id: 4, url: "https://example.com/page", incognito: true }
};
const session = {
  "media:3": [{ url: "https://cdn.example.com/v.m3u8", name: "v.m3u8", streaming: true }]
};

function listenerSlot() {
  const slot = { fn: null };
  slot.addListener = (fn) => { slot.fn = fn; };
  return slot;
}

const chrome = {
  runtime: {
    id: "boltdown-test",
    lastError: null,
    getURL: (p) => `chrome-extension://test/${p}`,
    onInstalled: listenerSlot(),
    onStartup: listenerSlot(),
    onMessage: listenerSlot(),
    // "port" and "oldport" give the script a long-lived connection; the old
    // host answers without echoing `seq`, the way hosts before it did.
    connectNative: (mode === "port" || mode === "oldport") ? (host) => {
      trace.ports += 1;
      const onMessage = listenerSlot();
      const onDisconnect = listenerSlot();
      return {
        onMessage,
        onDisconnect,
        postMessage(payload) {
          trace.native.push(payload);
          const reply = { ok: true, accepted: payload.url };
          if (mode === "port") reply.seq = payload.seq;
          setTimeout(() => onMessage.fn(reply), 5);
        }
      };
    } : undefined,
    sendNativeMessage(host, payload, callback) {
      trace.native.push(payload);
      if (mode === "fail") {
        chrome.runtime.lastError = { message: "host not found" };
        callback(undefined);
        chrome.runtime.lastError = null;
        return;
      }
      callback({ ok: true, accepted: payload.url });
    }
  },
  downloads: {
    onCreated: listenerSlot(),
    onDeterminingFilename: undefined,   // as on Firefox
    async cancel(id) { trace.cancelled.push(id); },
    async pause(id) { trace.paused.push(id); },
    async resume(id) { trace.resumed.push(id); },
    async erase(query) { trace.erased.push(query.id); }
  },
  storage: {
    local: {
      async get() { return {}; },
      async set() {}
    },
    session: {
      async get(key) { return key in session ? { [key]: session[key] } : {}; },
      async set(items) { Object.assign(session, items); },
      async remove(key) { delete session[key]; }
    },
    onChanged: listenerSlot()
  },
  i18n: { getMessage },
  cookies: {
    async getAll(query) {
      trace.cookieQueries.push(query);
      return [{ name: "sid", value: query.storeId ? `abc-${query.storeId}` : "abc" }];
    },
    async getAllCookieStores() {
      return [{ id: "0", tabIds: [3] }, { id: "1", tabIds: [4] }];
    }
  },
  notifications: { async create(options) { trace.notified.push(options.message); } },
  contextMenus: { create() {}, removeAll() {}, onClicked: listenerSlot() },
  action: { async setBadgeBackgroundColor() {}, async setBadgeText() {} },
  tabs: {
    onRemoved: listenerSlot(),
    onUpdated: listenerSlot(),
    async get(id) {
      if (!TABS[id]) throw new Error("no tab");
      return TABS[id];
    },
    async sendMessage() {}
  },
  webRequest: { onBeforeRequest: listenerSlot(), onHeadersReceived: listenerSlot() },
  scripting: { async executeScript() { return []; } }
};

const context = vm.createContext({
  chrome,
  navigator: { userAgent: "TestBrowser/1.0" },
  console,
  setTimeout,
  clearTimeout,
  URL,
  fetch: async () => { throw new Error("no network in the harness"); }
});

// A third argument lets a test point the harness at a modified copy, which
// is how the fix for the lost-download bug is shown to be load-bearing.
const script = process.argv[3] ||
  path.join(__dirname, "..", "extension", "background.js");
const source = fs.readFileSync(script, "utf8");
vm.runInContext(source, context);

const item = {
  id: 7,
  url: "https://example.com/big.iso",
  finalUrl: "https://example.com/big.iso",
  filename: "big.iso",
  fileSize: 1024 * 1024,
  mime: "application/octet-stream",
  referrer: "https://example.com/",
  incognito: mode === "incognito"
};

/** Deliver one runtime message as a browser would, and keep the reply. */
function ask(label, message, sender) {
  chrome.runtime.onMessage.fn(message, sender, (reply) => {
    trace.replies[label] = reply;
  });
}

const POPUP = { id: "boltdown-test", url: "chrome-extension://test/popup/popup.html" };
const PAGE = (tabId) => ({ id: "boltdown-test", url: TABS[tabId].url, tab: TABS[tabId] });

if (mode === "sniff") {
  const H = (type, length) => [
    { name: "Content-Type", value: type },
    ...(length ? [{ name: "Content-Length", value: String(length) }] : [])
  ];
  const seen = (url, type, length) => chrome.webRequest.onHeadersReceived.fn({
    tabId: 9, url, statusCode: 200, responseHeaders: H(type, length),
    initiator: "https://news.example"
  });
  seen("https://cdn.example/live/master.m3u8", "application/vnd.apple.mpegurl");
  for (let i = 0; i < 40; i++) seen(`https://cdn.example/live/seg${i}.ts`, "video/mp2t", 900000);
  for (let i = 0; i < 40; i++) seen(`https://cdn.example/clip${i}.mp4`, "video/mp4", 5000000);
  seen("https://cdn.example/stream?id=42", "video/webm", 7000000);
  seen("https://cdn.example/preview.mp4", "video/mp4", 2000);
  seen("https://cdn.example/big.mp4?range=0-1000", "video/mp4", 9000000);
  seen("https://cdn.example/big.mp4?range=1001-2000", "video/mp4", 9000000);
  chrome.webRequest.onHeadersReceived.fn({
    tabId: 10, url: "https://rr1.googlevideo.com/videoplayback?itag=22", statusCode: 200,
    responseHeaders: H("video/mp4", 9000000), initiator: "https://www.youtube.com"
  });
  setTimeout(() => {
    trace.media = { 9: session["media:9"] || [], 10: session["media:10"] || [] };
  }, 300);
} else if (mode === "port" || mode === "oldport") {
  const run = async () => {
    const replies = await Promise.all([1, 2, 3].map((n) =>
      new Promise((resolve) => chrome.runtime.onMessage.fn(
        { type: "send-url", tabId: 3, url: `https://example.com/f${n}.zip` },
        { id: "boltdown-test", url: "chrome-extension://test/popup/popup.html" },
        resolve
      ))
    ));
    trace.replies.port = replies;
  };
  setTimeout(run, 50);
} else if (mode === "router") {
  // A content script may only send what its own tab loaded, or the page.
  ask("contentForeign", { type: "send-media", url: "https://bank.example/statement.pdf" }, PAGE(3));
  ask("contentKnown", { type: "send-media", url: "https://cdn.example.com/v.m3u8" }, PAGE(3));
  ask("contentPage", { type: "send-media", url: TABS[3].url }, PAGE(3));
  ask("contentSettings", { type: "set-settings", patch: { enabled: false } }, PAGE(3));
  ask("contentUrl", { type: "send-url", url: "https://example.com/x.zip" }, PAGE(3));
  ask("contentOtherTab", { type: "get-media", tabId: 4 }, PAGE(3));
  ask("stranger", { type: "get-settings" }, { id: "someone-else", url: "https://evil.example/" });
  ask("popupSettings", { type: "set-settings", patch: { enabled: "yes", bogus: 1, minSize: 5 } }, POPUP);
  ask("popupPrivateUrl", { type: "send-url", tabId: 4, url: "https://example.com/private.zip" }, POPUP);
  ask("popupJs", { type: "send-url", tabId: 3, url: "javascript:alert(1)" }, POPUP);
} else {
  // This is how a browser announces a download.
  chrome.downloads.onCreated.fn(item);
}

// Let the promise chain inside the script settle, then report.
setTimeout(() => {
  console.log(JSON.stringify(trace));
}, 400);
