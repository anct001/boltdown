/**
 * "Download this video": a button on the video itself while the pointer is
 * over it - the way IDM does it - plus a floating button for pages where the
 * media has no visible player.
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
  // How many things the tab has to offer, and whether to show buttons at all.
  let mediaCount = 0;
  let allowed = true;

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
    .meta { font-size: 11px; color: #607d8b; }
    .on-video {
      position: fixed; z-index: 2147483601; display: none;
      align-items: center; gap: 6px; padding: 6px 10px;
      border: none; border-radius: 6px; cursor: pointer;
      background: rgba(21,101,192,.92); color: #fff;
      font: 600 12px/1.2 system-ui, "Segoe UI", sans-serif;
      box-shadow: 0 2px 10px rgba(0,0,0,.4);
    }
    .on-video.shown { display: flex; }
    .on-video:hover { background: #0d47a1; }
    .on-video:focus-visible { outline: 2px solid #ffb300; outline-offset: 2px; }
    @media (prefers-color-scheme: dark) {
      .panel { background: #263238; color: #eceff1; }
      .panel h4 { background: #37474f; }
      .row { border-top-color: #37474f; }
      .empty, .meta { color: #b0bec5; }
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
    hideVideoButton();
  }

  function formatSize(bytes) {
    if (!bytes) return "";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let value = bytes;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024;
      unit += 1;
    }
    return `${value.toFixed(value < 10 && unit > 0 ? 1 : 0)} ${units[unit]}`;
  }

  /** A player big enough to carry its own button, anywhere on the page. */
  function hasPlayer() {
    return Array.from(document.querySelectorAll("video")).some((video) => {
      const box = video.getBoundingClientRect();
      return box.width >= 200 && box.height >= 110;
    });
  }

  function ensureButton(count) {
    mediaCount = Math.max(mediaCount, count);
    if (hidden() || !allowed) return;
    // Where there is a player, the button on it is the one to use; a second
    // one in the corner is only clutter. The corner is for audio, streams
    // without a visible player, and yt-dlp pages.
    if (hasPlayer() && !(root && root.querySelector(".fab"))) return;
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
      fab.addEventListener("click", () => togglePanel());
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

  async function togglePanel(anchor) {
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
      } else if (item.size || item.mime) {
        const meta = document.createElement("div");
        meta.className = "meta";
        meta.textContent = [formatSize(item.size), item.mime].filter(Boolean).join(" \u00b7 ");
        label.appendChild(meta);
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

    if (anchor) {
      // Under the button on the video, kept inside the window.
      const box = anchor.getBoundingClientRect();
      const width = 340;
      panel.style.right = "auto";
      panel.style.bottom = "auto";
      panel.style.left = `${Math.max(8, Math.min(box.right - width, innerWidth - width - 8))}px`;
      panel.style.top = `${Math.min(box.bottom + 6, innerHeight - 200)}px`;
    }
    root.appendChild(panel);
  }

  // ------------------------------------------------------ button on the video

  let videoButton = null;
  let currentVideo = null;
  let hideTimer = null;
  let lastMove = 0;

  function bigEnough(video) {
    const box = video.getBoundingClientRect();
    return box.width >= 200 && box.height >= 110 && box.bottom > 0 && box.top < innerHeight;
  }

  /**
   * The video under the pointer. Players lay their controls over the video,
   * so the element under the pointer is usually not the <video> at all -
   * hence geometry, not event targets.
   */
  function videoAt(x, y) {
    for (const video of document.querySelectorAll("video")) {
      if (!bigEnough(video)) continue;
      const box = video.getBoundingClientRect();
      if (x >= box.left && x <= box.right && y >= box.top && y <= box.bottom) return video;
    }
    return null;
  }

  function placeVideoButton() {
    if (!videoButton || !currentVideo) return;
    const box = currentVideo.getBoundingClientRect();
    const width = videoButton.offsetWidth || 150;
    videoButton.style.top = `${Math.max(4, box.top + 10)}px`;
    videoButton.style.left = `${Math.max(4, box.right - width - 10)}px`;
  }

  function showVideoButton(video) {
    if (hidden() || !allowed || mediaCount === 0) return;
    ensureHost();
    if (!videoButton) {
      videoButton = document.createElement("button");
      videoButton.className = "on-video";
      videoButton.textContent = `\u2B07 ${t("fabLabel")}`;
      videoButton.addEventListener("click", (event) => {
        event.stopPropagation();
        event.preventDefault();
        togglePanel(videoButton);
      });
      videoButton.addEventListener("pointerenter", () => clearTimeout(hideTimer));
      root.appendChild(videoButton);
    }
    clearTimeout(hideTimer);
    currentVideo = video;
    videoButton.classList.add("shown");
    host.dataset.videoButton = "shown";
    placeVideoButton();
  }

  function hideVideoButton() {
    if (!videoButton) return;
    videoButton.classList.remove("shown");
    currentVideo = null;
    if (host) delete host.dataset.videoButton;
  }

  function onPointerMove(event) {
    const now = Date.now();
    if (now - lastMove < 80) return;
    lastMove = now;
    if (!mediaCount || !allowed) return;
    const video = videoAt(event.clientX, event.clientY);
    if (video) {
      showVideoButton(video);
    } else if (currentVideo && !panel) {
      clearTimeout(hideTimer);
      hideTimer = setTimeout(hideVideoButton, 1200);
    }
  }

  document.addEventListener("pointermove", onPointerMove, { passive: true, capture: true });
  window.addEventListener("scroll", placeVideoButton, { passive: true, capture: true });
  window.addEventListener("resize", placeVideoButton, { passive: true });

  chrome.runtime.onMessage.addListener((message, sender) => {
    if (sender && sender.id !== chrome.runtime.id) return;
    if (message && message.type === "media-count" && message.count > 0) {
      ensureButton(message.count);
    }
    if (message && message.type === "open-panel") {
      // The keyboard shortcut: the player's own button if there is a video,
      // the floating list otherwise.
      const video = Array.from(document.querySelectorAll("video")).find(bigEnough);
      if (video && mediaCount) {
        showVideoButton(video);
        togglePanel(videoButton);
      } else {
        togglePanel();
      }
    }
  });

  // On a site yt-dlp handles, the button must appear even when the page never
  // issues a request our sniffer recognises.
  chrome.runtime
    .sendMessage({ type: "get-media" })
    .then((reply) => {
      const items = (reply && reply.items) || [];
      allowed = reply.showButton !== false;
      magnets = reply.captureMagnets === true;
      if (items.length) ensureButton(items.length);
    })
    .catch(() => {});

  // Magnet links: a click the user made goes to Boltdown. The default action
  // is stopped before the answer is known (it cannot wait), so when the app
  // does not take the link the browser is asked to open it after all.
  let magnets = false;
  let passThrough = null;
  document.addEventListener(
    "click",
    (event) => {
      if (!magnets || !event.isTrusted || event.defaultPrevented) return;
      if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      const link = event.target instanceof Element ? event.target.closest("a[href]") : null;
      if (!link || !/^magnet:\?/i.test(link.href) || link.href === passThrough) return;
      event.preventDefault();
      const url = link.href;
      chrome.runtime
        .sendMessage({ type: "send-magnet", url })
        .then((reply) => {
          if (!reply || !reply.handled || !reply.ok) open(url);
        })
        .catch(() => open(url));
    },
    true
  );
  function open(url) {
    passThrough = url;
    window.location.assign(url);
  }

  // pagehide rather than beforeunload: a beforeunload listener keeps the page
  // out of the back/forward cache in some browsers.
  window.addEventListener("pagehide", closePanel);
})();
