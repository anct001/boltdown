// Browser half of scripts/bench_browser.py - see there for what and why.
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const cfg = JSON.parse(process.argv[2]);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const crypto = require("crypto");

/** The id Chromium gives an unpacked extension: a hash of its path, in a-p. */
function unpackedId(dir) {
  const hex = crypto.createHash("sha256").update(path.resolve(dir)).digest("hex").slice(0, 32);
  return hex.replace(/./g, (c) => String.fromCharCode(97 + parseInt(c, 16)));
}

function registerHost(id) {
  fs.writeFileSync(path.join(cfg.hostDir, "com.boltdown.host.json"), JSON.stringify({
    name: "com.boltdown.host",
    description: "bench",
    path: cfg.launcher,
    type: "stdio",
    allowed_origins: [`chrome-extension://${id}/`]
  }));
}

(async () => {
  // Registered before the browser starts, the way an installed copy is.
  registerHost(unpackedId(cfg.extension));
  const { spawn } = require("child_process");
  const chromeArgs = (extra) => [
    "--headless=new",
    `--user-data-dir=${cfg.profile}`,
    "--no-first-run",
    "--no-default-browser-check",
    // Chromium refuses to start its sandbox as root (containers, CI).
    ...(process.getuid && process.getuid() === 0 ? ["--no-sandbox"] : []),
    `--disable-extensions-except=${cfg.extension}`,
    `--load-extension=${cfg.extension}`,
    ...extra
  ];

  // Phase 1 - downloads, in a browser nothing is attached to. Under
  // Playwright, even over CDP, Chromium's download handling is replaced and
  // onDeterminingFilename never fires, which is not what users run.
  const plain = spawn(cfg.chromium, chromeArgs([`${cfg.base}/auto?runs=${cfg.runs}`]), {
    stdio: ["ignore", "ignore", "pipe"]
  });
  let plainLog = "";
  plain.stderr.on("data", (chunk) => { plainLog = (plainLog + chunk).slice(-4000); });
  const plainExit = new Promise((resolve) => plain.once("exit", resolve));
  await sleep(2500 + cfg.runs * 3000 + 2500);
  if (plain.exitCode !== null) {
    throw new Error(`the browser quit during the download phase:\n${plainLog}`);
  }
  plain.kill();
  await plainExit;
  await sleep(500);

  // Phase 2 - everything that needs a hand on the browser.
  const portFile = path.join(cfg.profile, "DevToolsActivePort");
  try { fs.unlinkSync(portFile); } catch (error) { /* not there */ }
  const proc = spawn(cfg.chromium, chromeArgs(["--remote-debugging-port=0", "about:blank"]), {
    stdio: "ignore"
  });
  for (let i = 0; i < 100 && !fs.existsSync(portFile); i++) await sleep(100);
  const devtools = fs.readFileSync(portFile, "utf8").split("\n")[0];
  const browser = await chromium.connectOverCDP(`http://127.0.0.1:${devtools}`);
  const ctx = browser.contexts()[0];
  process.on("exit", () => proc.kill());
  let sw = ctx.serviceWorkers().find((w) => w.url().includes("background.js"));
  if (!sw) sw = await ctx.waitForEvent("serviceworker");
  const errors = [];
  sw.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  const id = sw.url().split("/")[2];

  registerHost(id); // in case the hash guess was wrong; read on each connect
  await sleep(1500); // a browser that has just started is not a fair test

  const page = await ctx.newPage();
  const out = { errors };

  // 2. every link on a page, the way the context menu does it
  await page.goto(`${cfg.base}/links`);
  const tabId = await sw.evaluate(async (url) => {
    const [tab] = await chrome.tabs.query({ url: url + "/links" });
    return tab.id;
  }, cfg.base);
  const batch = await sw.evaluate(async ({ tabId, base }) => {
    const tab = await chrome.tabs.get(tabId);
    const start = Date.now();
    const urls = (await linksOnPage(tabId, false)).map((l) => l.url || l);
    if (typeof sendLinks === "function") await sendLinks(urls, tab.url, tab);
    else await sendMany(urls, tab.url, tab);
    return { start, end: Date.now(), found: urls.length };
  }, { tabId, base: cfg.base });
  out.batch = batch;

  // 3. a stream that fetches its playlist and then many segments
  await page.goto(`${cfg.base}/hls`);
  await page.waitForFunction(() => document.title === "done", null, { timeout: 60000 });
  await sleep(500);
  out.hls = await sw.evaluate(async (tabId) => {
    const list = await mediaFor(tabId);
    return { count: list.length, playlist: list.some((m) => /\.m3u8/.test(m.url)) };
  }, tabId);

  // 4. media that only its Content-Type gives away
  await page.goto(`${cfg.base}/typed`);
  await page.waitForFunction(() => document.title === "done", null, { timeout: 30000 });
  await sleep(500);
  out.typed = await sw.evaluate(async (tabId) => {
    const list = await mediaFor(tabId);
    return list.some((m) => m.url.includes("/stream?id=42"));
  }, tabId);

  // 5. a button on the video itself
  await page.goto(`${cfg.base}/video`);
  await sleep(1500);
  await page.hover("#v").catch(() => {});
  await sleep(600);
  out.videoButton = await page.evaluate(() => {
    const host = document.getElementById("boltdown-root");
    return Boolean(host && host.dataset.videoButton === "shown");
  });

  await browser.close().catch(() => {});
  proc.kill();
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e); process.exit(1); });
