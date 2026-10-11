"""The remote-control page: one HTML file, its script and its style.

Kept as strings so the packaged app needs no data files. File names and
error messages come from the internet, so the script only ever puts them in
the page as text (`textContent`), never as HTML.
"""

HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Boltdown</title>
<link rel="stylesheet" href="app.css">
</head>
<body>
<header>
  <h1>Boltdown</h1>
  <div id="speed" class="speed"></div>
</header>
<main>
  <p id="notice" class="notice" hidden></p>
  <form id="add">
    <input id="url" type="url" inputmode="url" autocomplete="off" required
           placeholder="https://... / ftp://... / magnet:?...">
    <button id="add-button" type="submit">+</button>
  </form>
  <div class="bar">
    <button id="pause-all" type="button"></button>
    <button id="resume-all" type="button"></button>
  </div>
  <ul id="items"></ul>
  <p id="empty" class="empty" hidden></p>
</main>
<script src="app.js"></script>
</body>
</html>
"""

STYLE = """
:root {
  --bg: #f6f7f9; --card: #ffffff; --text: #16181d; --muted: #646b78;
  --line: #e2e5ea; --accent: #2563eb; --ok: #15803d; --bad: #b91c1c;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #111318; --card: #1a1d24; --text: #e8eaee; --muted: #9aa1ad;
    --line: #2a2e37; --accent: #60a5fa; --ok: #4ade80; --bad: #f87171;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 15px/1.4 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
header {
  position: sticky; top: 0; z-index: 1; display: flex; align-items: baseline;
  justify-content: space-between; gap: 12px; padding: 12px 16px;
  background: var(--card); border-bottom: 1px solid var(--line);
}
h1 { margin: 0; font-size: 18px; }
.speed { color: var(--muted); font-variant-numeric: tabular-nums; }
main { max-width: 720px; margin: 0 auto; padding: 12px 16px 32px; }
form { display: flex; gap: 8px; }
input {
  flex: 1; min-width: 0; padding: 10px 12px; font: inherit; color: var(--text);
  background: var(--card); border: 1px solid var(--line); border-radius: 8px;
}
button {
  padding: 8px 14px; font: inherit; color: var(--text); cursor: pointer;
  background: var(--card); border: 1px solid var(--line); border-radius: 8px;
}
button:active { transform: translateY(1px); }
#add-button { background: var(--accent); border-color: var(--accent); color: #fff; font-weight: 600; }
.bar { display: flex; gap: 8px; margin: 12px 0; }
ul { list-style: none; margin: 0; padding: 0; display: grid; gap: 8px; }
li {
  padding: 10px 12px; background: var(--card);
  border: 1px solid var(--line); border-radius: 10px;
}
.name { font-weight: 600; overflow-wrap: anywhere; }
.meta { display: flex; flex-wrap: wrap; gap: 4px 12px; color: var(--muted);
  font-size: 13px; font-variant-numeric: tabular-nums; margin-top: 2px; }
.progress { height: 6px; margin: 8px 0; background: var(--line); border-radius: 3px; overflow: hidden; }
.progress > div { height: 100%; background: var(--accent); width: 0; transition: width .4s; }
li.completed .progress > div { background: var(--ok); }
li.error .progress > div { background: var(--bad); }
.error-text { color: var(--bad); font-size: 13px; overflow-wrap: anywhere; }
.actions { display: flex; gap: 6px; justify-content: flex-end; }
.actions button { padding: 4px 10px; font-size: 13px; }
.notice { padding: 10px 12px; border-radius: 8px; background: var(--card);
  border: 1px solid var(--bad); color: var(--bad); }
.empty { color: var(--muted); text-align: center; margin-top: 32px; }
"""

SCRIPT = r"""
"use strict";
(function () {
  var KEY = "boltdown-token";
  var token = "";
  try {
    var hash = new URLSearchParams(location.hash.slice(1)).get("t");
    if (hash) {
      localStorage.setItem(KEY, hash);
      // The token leaves the address bar: no one reads it over a shoulder.
      history.replaceState(null, "", location.pathname);
    }
    token = hash || localStorage.getItem(KEY) || "";
  } catch (e) {
    token = new URLSearchParams(location.hash.slice(1)).get("t") || "";
  }

  var words = {};
  function t(key, fallback) { return words[key] || fallback; }

  var $ = function (id) { return document.getElementById(id); };

  function call(method, path, body) {
    var options = { method: method, headers: { "X-Boltdown-Token": token } };
    if (body !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    return fetch(path, options).then(function (response) {
      return response.json().then(function (data) {
        if (!response.ok) { throw new Error(data.error || response.status); }
        return data;
      });
    });
  }

  function size(bytes) {
    if (bytes == null) { return "?"; }
    var units = ["B", "KB", "MB", "GB", "TB"], i = 0;
    while (bytes >= 1024 && i < units.length - 1) { bytes /= 1024; i++; }
    return (i ? bytes.toFixed(1) : bytes) + " " + units[i];
  }
  function eta(seconds) {
    if (seconds == null) { return ""; }
    seconds = Math.round(seconds);
    var h = Math.floor(seconds / 3600), m = Math.floor(seconds % 3600 / 60), s = seconds % 60;
    return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(s).padStart(2, "0");
  }

  function show(message) {
    var box = $("notice");
    box.textContent = message || "";
    box.hidden = !message;
  }

  function button(label, action, id) {
    var b = document.createElement("button");
    b.type = "button";
    b.textContent = label;
    b.addEventListener("click", function () {
      call("POST", "/api/items/" + id + "/" + action).then(refresh, function (e) { show(e.message); });
    });
    return b;
  }

  function render(items) {
    var list = $("items");
    list.textContent = "";
    $("empty").hidden = items.length > 0;
    items.forEach(function (item) {
      var li = document.createElement("li");
      li.className = item.state;
      var name = document.createElement("div");
      name.className = "name";
      name.textContent = item.filename;
      li.appendChild(name);

      var bar = document.createElement("div");
      bar.className = "progress";
      var fill = document.createElement("div");
      fill.style.width = (item.percent || 0).toFixed(1) + "%";
      bar.appendChild(fill);
      li.appendChild(bar);

      var meta = document.createElement("div");
      meta.className = "meta";
      [
        t("state_" + item.state, item.state),
        size(item.downloaded) + " / " + size(item.size),
        item.state === "downloading" ? size(item.speed) + "/s" : "",
        item.state === "downloading" ? eta(item.eta) : ""
      ].forEach(function (text) {
        if (!text) { return; }
        var span = document.createElement("span");
        span.textContent = text;
        meta.appendChild(span);
      });
      li.appendChild(meta);

      if (item.error) {
        var error = document.createElement("div");
        error.className = "error-text";
        error.textContent = item.error;
        li.appendChild(error);
      }

      var actions = document.createElement("div");
      actions.className = "actions";
      if (item.state === "downloading" || item.state === "probing" || item.state === "queued") {
        actions.appendChild(button(t("pause", "Pause"), "pause", item.id));
      } else if (item.state !== "completed") {
        actions.appendChild(button(t("resume", "Resume"), "resume", item.id));
      }
      actions.appendChild(button(t("remove", "Remove"), "remove", item.id));
      li.appendChild(actions);
      list.appendChild(li);
    });
  }

  function refresh() {
    return Promise.all([call("GET", "/api/status"), call("GET", "/api/items")])
      .then(function (both) {
        var status = both[0];
        words = status.words || words;
        document.documentElement.lang = status.lang || "en";
        $("speed").textContent = status.active
          ? size(status.speed) + "/s · " + status.active
          : "";
        $("pause-all").textContent = t("pause_all", "Pause all");
        $("resume-all").textContent = t("resume_all", "Resume all");
        $("empty").textContent = t("empty", "No downloads");
        show("");
        render(both[1].items);
      })
      .catch(function (e) { show(token ? e.message : t("no_token", "Open the link shown in Boltdown's settings.")); });
  }

  $("add").addEventListener("submit", function (event) {
    event.preventDefault();
    var url = $("url").value.trim();
    if (!url) { return; }
    call("POST", "/api/add", { url: url }).then(function () {
      $("url").value = "";
      refresh();
    }, function (e) { show(e.message); });
  });
  $("pause-all").addEventListener("click", function () {
    call("POST", "/api/all/pause").then(refresh, function (e) { show(e.message); });
  });
  $("resume-all").addEventListener("click", function () {
    call("POST", "/api/all/resume").then(refresh, function (e) { show(e.message); });
  });

  refresh();
  setInterval(function () { if (!document.hidden) { refresh(); } }, 1500);
})();
"""
