/* The link picker: every link on a page, pick what to download. */

const KINDS = [
  ["video", /\.(mp4|m4v|mkv|webm|mov|avi|flv|wmv|3gp|mpg|mpeg|m3u8|mpd)$/i],
  ["audio", /\.(mp3|m4a|aac|flac|ogg|oga|opus|wav|wma)$/i],
  ["archive", /\.(zip|rar|7z|tar|gz|tgz|bz2|xz|zst|iso|img|cab)$/i],
  ["document", /\.(pdf|docx?|xlsx?|pptx?|odt|ods|odp|rtf|txt|csv|epub|mobi|djvu)$/i],
  ["image", /\.(jpe?g|png|gif|webp|bmp|tiff?|heic|avif|svg|psd)$/i],
  ["program", /\.(exe|msi|msix|apk|aab|dmg|pkg|deb|rpm|appimage|jar|torrent)$/i]
];
const OTHER = "other";

const key = decodeURIComponent(location.hash.slice(1));
const rowsEl = document.getElementById("rows");
const filterEl = document.getElementById("filter");
const kindsEl = document.getElementById("kinds");
const sendEl = document.getElementById("send");
const summaryEl = document.getElementById("summary");
const errorEl = document.getElementById("error");
const emptyEl = document.getElementById("empty");

let links = [];
const selected = new Set();

function t(name, subs) {
  try {
    return chrome.i18n.getMessage(name, subs) || name;
  } catch (error) {
    return name;
  }
}

function fileName(url) {
  try {
    const u = new URL(url);
    const last = u.pathname.split("/").filter(Boolean).pop() || u.hostname;
    try {
      return decodeURIComponent(last);
    } catch (error) {
      return last;
    }
  } catch (error) {
    return url;
  }
}

function kindOf(url) {
  let path = url;
  try {
    path = new URL(url).pathname;
  } catch (error) {
    // keep the raw string
  }
  for (const [kind, pattern] of KINDS) {
    if (pattern.test(path)) return kind;
  }
  return OTHER;
}

function hostOf(url) {
  try {
    return new URL(url).hostname;
  } catch (error) {
    return "";
  }
}

function visible() {
  const words = filterEl.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
  return links.filter((link) => {
    if (!words.length) return true;
    const haystack = `${link.url} ${link.text}`.toLowerCase();
    return words.every((word) => haystack.includes(word));
  });
}

function updateSummary() {
  sendEl.textContent = t("pickerDownload", [String(selected.size)]);
  sendEl.disabled = selected.size === 0;
  summaryEl.textContent = t("pickerSummary", [String(selected.size), String(links.length)]);
}

function render() {
  const shown = visible();
  const fragment = document.createDocumentFragment();
  for (const link of shown) {
    const row = document.createElement("tr");

    const checkCell = document.createElement("td");
    checkCell.className = "check";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = selected.has(link.url);
    box.setAttribute("aria-label", link.name);
    box.addEventListener("change", () => {
      if (box.checked) selected.add(link.url);
      else selected.delete(link.url);
      updateSummary();
    });
    checkCell.appendChild(box);

    const nameCell = document.createElement("td");
    nameCell.textContent = link.name;
    nameCell.title = link.url;
    if (link.text && link.text !== link.name) {
      const text = document.createElement("span");
      text.className = "text";
      text.textContent = link.text;
      nameCell.appendChild(text);
    }
    nameCell.addEventListener("click", () => box.click());

    const kindCell = document.createElement("td");
    kindCell.className = "kind";
    kindCell.textContent = t(`kind_${link.kind}`);

    const hostCell = document.createElement("td");
    hostCell.className = "host";
    hostCell.textContent = link.host;

    row.append(checkCell, nameCell, kindCell, hostCell);
    fragment.appendChild(row);
  }
  rowsEl.replaceChildren(fragment);
  emptyEl.hidden = shown.length > 0;
  updateSummary();
  syncKinds();
}

/** A kind's chip is ticked when every link of that kind is. */
function syncKinds() {
  for (const box of kindsEl.querySelectorAll("input")) {
    const ofKind = links.filter((link) => link.kind === box.dataset.kind);
    const picked = ofKind.filter((link) => selected.has(link.url)).length;
    box.checked = picked === ofKind.length;
    box.indeterminate = picked > 0 && picked < ofKind.length;
    box.parentElement.classList.toggle("off", picked === 0);
  }
}

function renderKinds() {
  const counts = new Map();
  for (const link of links) counts.set(link.kind, (counts.get(link.kind) || 0) + 1);
  kindsEl.replaceChildren();
  for (const kind of [...KINDS.map(([k]) => k), OTHER]) {
    if (!counts.get(kind)) continue;
    const label = document.createElement("label");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.dataset.kind = kind;
    // A chip selects or clears every link of its kind; the list below always
    // shows them all, so nothing is ever chosen out of sight.
    box.addEventListener("change", () => {
      for (const link of links) {
        if (link.kind !== kind) continue;
        if (box.checked) selected.add(link.url);
        else selected.delete(link.url);
      }
      render();
    });
    const text = document.createElement("span");
    text.textContent = `${t(`kind_${kind}`)} (${counts.get(kind)})`;
    label.append(box, text);
    kindsEl.appendChild(label);
  }
}

async function load() {
  document.documentElement.lang = t("@@ui_locale").replace("_", "-");
  for (const el of document.querySelectorAll("[data-i18n]")) {
    el.textContent = t(el.dataset.i18n);
  }
  filterEl.placeholder = t("pickerFilter");
  document.title = `Boltdown - ${t("pickerTitle")}`;

  const reply = await chrome.runtime.sendMessage({ type: "picker-data", key });
  if (!reply || !reply.ok) {
    errorEl.textContent = t("pickerExpired");
    sendEl.disabled = true;
    return;
  }
  document.getElementById("source").textContent = reply.pageUrl || "";
  links = reply.links.map((link) => ({
    url: link.url,
    text: link.text || "",
    name: fileName(link.url),
    kind: kindOf(link.url),
    host: hostOf(link.url)
  }));
  // Files are what people came for; navigation links start unticked - and
  // if a page has nothing but, show them all rather than an empty list.
  const files = links.filter((link) => link.kind !== OTHER);
  for (const link of files.length ? files : links) selected.add(link.url);
  renderKinds();
  render();
  filterEl.focus();
}

document.getElementById("all").addEventListener("click", () => {
  for (const link of visible()) selected.add(link.url);
  render();
});

document.getElementById("none").addEventListener("click", () => {
  for (const link of visible()) selected.delete(link.url);
  render();
});

document.getElementById("cancel").addEventListener("click", () => window.close());
filterEl.addEventListener("input", render);

async function send() {
  if (!selected.size) return;
  sendEl.disabled = true;
  errorEl.textContent = "";
  // In page order, which is the order people expect them in the list.
  const urls = links.filter((link) => selected.has(link.url)).map((link) => link.url);
  const reply = await chrome.runtime.sendMessage({ type: "picker-send", key, urls });
  if (reply && reply.ok) {
    window.close();
    return;
  }
  errorEl.textContent = (reply && reply.error) || t("unknownError");
  sendEl.disabled = false;
}

sendEl.addEventListener("click", send);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") window.close();
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) send();
});

load();
