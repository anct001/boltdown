/**
 * Floating "download this video" button.
 *
 * Everything lives in a shadow root so page CSS cannot restyle it and our CSS
 * cannot leak into the page.
 */

(() => {
  if (window.__boltdownInjected) return;
  window.__boltdownInjected = true;

  let host = null;
  let root = null;
  let panel = null;

  const STYLE = `
    :host { all: initial; }
    .fab {
      position: fixed; right: 18px; bottom: 18px; z-index: 2147483600;
      display: flex; align-items: center; gap: 8px;
      padding: 10px 14px; border-radius: 24px; border: none;
      background: #1565c0; color: #fff; cursor: pointer;
      font: 600 13px/1.2 system-ui, "Segoe UI", sans-serif;
      box-shadow: 0 4px 14px rgba(0,0,0,.35);
    }
    .fab:hover { background: #0d47a1; }
    .badge {
      background: rgba(255,255,255,.25); border-radius: 10px;
      padding: 1px 7px; font-size: 12px;
    }
    .panel {
      position: fixed; right: 18px; bottom: 70px; z-index: 2147483600;
      width: 340px; max-height: 320px; overflow: auto;
      background: #fff; color: #222; border-radius: 10px;
      box-shadow: 0 8px 28px rgba(0,0,0,.35);
      font: 13px/1.4 system-ui, "Segoe UI", sans-serif;
    }
    .panel h4 { margin: 0; padding: 10px 12px; background: #eceff1; font-size: 13px; }
    .row {
      display: flex; align-items: center; justify-content: space-between;
      gap: 8px; padding: 8px 12px; border-top: 1px solid #eceff1;
    }
    .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .tag { font-size: 11px; color: #ef6c00; }
    .go {
      border: none; background: #1565c0; color: #fff; cursor: pointer;
      border-radius: 6px; padding: 5px 10px; font-size: 12px;
    }
    .empty { padding: 12px; color: #607d8b; }
  `;

  function ensureHost() {
    if (host) return;
    host = document.createElement("div");
    host.id = "boltdown-root";
    root = host.attachShadow({ mode: "closed" });
    const style = document.createElement("style");
    style.textContent = STYLE;
    root.appendChild(style);
    document.documentElement.appendChild(host);
  }

  function ensureButton(count) {
    ensureHost();
    let fab = root.querySelector(".fab");
    if (!fab) {
      fab = document.createElement("button");
      fab.className = "fab";
      fab.innerHTML = '<span>Tải video này</span><span class="badge"></span>';
      fab.addEventListener("click", togglePanel);
      root.appendChild(fab);
    }
    fab.querySelector(".badge").textContent = String(count);
  }

  function closePanel() {
    if (panel) {
      panel.remove();
      panel = null;
    }
  }

  async function togglePanel() {
    if (panel) {
      closePanel();
      return;
    }
    const reply = await chrome.runtime.sendMessage({
      type: "get-media",
      pageUrl: location.href
    });
    const items = (reply && reply.items) || [];

    panel = document.createElement("div");
    panel.className = "panel";
    const title = document.createElement("h4");
    title.textContent = "Boltdown";
    panel.appendChild(title);

    if (!items.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "Chưa thấy media nào trên trang này.";
      panel.appendChild(empty);
    }

    items.forEach((item) => {
      const row = document.createElement("div");
      row.className = "row";

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
      button.className = "go";
      button.textContent = "Tải";
      button.addEventListener("click", async () => {
        button.disabled = true;
        button.textContent = "...";
        const response = await chrome.runtime.sendMessage({
          type: "send-media",
          url: item.url,
          streaming: item.streaming,
          page: item.page,
          referer: location.href
        });
        button.textContent = response && response.ok ? "✓" : "!";
      });

      row.appendChild(label);
      row.appendChild(button);
      panel.appendChild(row);
    });

    root.appendChild(panel);
  }

  chrome.runtime.onMessage.addListener((message) => {
    if (message && message.type === "media-count" && message.count > 0) {
      ensureButton(message.count);
    }
  });

  // On a site yt-dlp handles, the button must appear even when the page never
  // issues a request our sniffer recognises.
  chrome.runtime
    .sendMessage({ type: "get-media", pageUrl: location.href })
    .then((reply) => {
      const items = (reply && reply.items) || [];
      if (items.length) ensureButton(items.length);
    })
    .catch(() => {});

  window.addEventListener("beforeunload", closePanel);
})();
