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
  // "Hide" lasts until the next page, and a navigation inside a single-page
  // app is a new page as far as the user is concerned.
  let hiddenFor = null;

  function t(key) {
    try {
      return chrome.i18n.getMessage(key) || key;
    } catch (error) {
      return key;
    }
  }

  function hidden() {
    return hiddenFor !== null && hiddenFor === location.href;
  }

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
    .fab:focus-visible, .go:focus-visible, .hide:focus-visible {
      outline: 2px solid #ffb300; outline-offset: 2px;
    }
    .hide {
      margin-left: 2px; border: none; background: transparent; color: inherit;
      cursor: pointer; font: inherit; opacity: .75; padding: 0 2px;
    }
    .hide:hover { opacity: 1; }
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
    @media (prefers-color-scheme: dark) {
      .panel { background: #263238; color: #eceff1; }
      .panel h4 { background: #37474f; }
      .row { border-top-color: #37474f; }
      .empty { color: #b0bec5; }
    }
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

  function removeButton() {
    closePanel();
    const fab = root && root.querySelector(".fab");
    if (fab) fab.remove();
  }

  function ensureButton(count) {
    if (hidden()) return;
    ensureHost();
    let fab = root.querySelector(".fab");
    if (!fab) {
      fab = document.createElement("div");
      fab.className = "fab";
      fab.setAttribute("role", "button");
      fab.tabIndex = 0;
      const label = document.createElement("span");
      label.textContent = t("fabLabel");
      const badge = document.createElement("span");
      badge.className = "badge";
      const hide = document.createElement("button");
      hide.className = "hide";
      hide.textContent = "\u00d7";
      hide.title = t("fabHide");
      hide.setAttribute("aria-label", t("fabHide"));
      hide.addEventListener("click", (event) => {
        event.stopPropagation();
        hiddenFor = location.href;
        removeButton();
      });
      fab.append(label, badge, hide);
      fab.addEventListener("click", togglePanel);
      fab.addEventListener("keydown", (event) => {
        if (event.target === fab && (event.key === "Enter" || event.key === " ")) {
          event.preventDefault();
          togglePanel();
        }
      });
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
    let reply = null;
    try {
      reply = await chrome.runtime.sendMessage({ type: "get-media" });
    } catch (error) {
      // The extension was reloaded under this page; nothing to show.
      removeButton();
      return;
    }
    if (panel) return; // a second click while we were waiting
    const items = (reply && reply.items) || [];

    panel = document.createElement("div");
    panel.className = "panel";
    const title = document.createElement("h4");
    title.textContent = "Boltdown";
    panel.appendChild(title);

    if (!items.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = t("noMediaOnPage");
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
      button.textContent = t("download");
      button.addEventListener("click", async () => {
        button.disabled = true;
        button.textContent = "...";
        let response = null;
        try {
          response = await chrome.runtime.sendMessage({
            type: "send-media",
            url: item.url
          });
        } catch (error) {
          response = null;
        }
        const ok = Boolean(response && response.ok);
        button.textContent = ok ? "\u2713" : "!";
        if (!ok && response && response.error) button.title = response.error;
      });

      row.appendChild(label);
      row.appendChild(button);
      panel.appendChild(row);
    });

    root.appendChild(panel);
  }

  chrome.runtime.onMessage.addListener((message, sender) => {
    if (sender && sender.id !== chrome.runtime.id) return;
    if (message && message.type === "media-count" && message.count > 0) {
      ensureButton(message.count);
    }
  });

  // On a site yt-dlp handles, the button must appear even when the page never
  // issues a request our sniffer recognises.
  chrome.runtime
    .sendMessage({ type: "get-media" })
    .then((reply) => {
      const items = (reply && reply.items) || [];
      if (items.length && reply.showButton !== false) ensureButton(items.length);
    })
    .catch(() => {});

  // pagehide rather than beforeunload: a beforeunload listener keeps the page
  // out of the back/forward cache in some browsers.
  window.addEventListener("pagehide", closePanel);
})();
