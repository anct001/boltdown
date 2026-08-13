// Runs the extension's background script against a fake browser, so the
// download hand-over can be tested without Chrome.
//
//   node tests/extension_harness.js <"ok" | "fail">
//
// Prints a JSON trace of what the script did to the fake browser: which
// native messages it sent, and whether it cancelled the browser's own
// download. The question that matters is what happens when the native host
// does not answer - the browser download must survive.

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const mode = process.argv[2] || "ok";
const trace = { native: [], cancelled: [], erased: [], notified: [] };

function listenerSlot() {
  const slot = { fn: null };
  slot.addListener = (fn) => { slot.fn = fn; };
  return slot;
}

const chrome = {
  runtime: {
    lastError: null,
    getURL: (p) => `chrome-extension://test/${p}`,
    onInstalled: listenerSlot(),
    onStartup: listenerSlot(),
    onMessage: listenerSlot(),
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
    async erase(query) { trace.erased.push(query.id); }
  },
  storage: {
    local: {
      async get() { return {}; },
      async set() {}
    },
    session: {
      async get() { return {}; },
      async set() {},
      async remove() {}
    }
  },
  cookies: { async getAll() { return [{ name: "sid", value: "abc" }]; } },
  notifications: { async create(options) { trace.notified.push(options.message); } },
  contextMenus: { create() {}, removeAll() {}, onClicked: listenerSlot() },
  action: { async setBadgeBackgroundColor() {}, async setBadgeText() {} },
  tabs: {
    onRemoved: listenerSlot(),
    onUpdated: listenerSlot(),
    async sendMessage() {}
  },
  webRequest: { onBeforeRequest: { addListener() {} } },
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
  referrer: "https://example.com/"
};

// This is how a browser announces a download.
chrome.downloads.onCreated.fn(item);

// Let the promise chain inside the script settle, then report.
setTimeout(() => {
  console.log(JSON.stringify(trace));
}, 400);
