/* Options: everything the popup has, plus the lists that need room. */

const TOGGLES = ["enabled", "captureMedia", "captureMagnets", "showButton", "captureIncognito"];
const MB = 1024 * 1024;
const savedEl = document.getElementById("saved");
let savedTimer = null;

function t(key) {
  try {
    return chrome.i18n.getMessage(key) || key;
  } catch (error) {
    return key;
  }
}

function words(text, pattern) {
  return Array.from(
    new Set(
      text
        .split(/[\s,;]+/)
        .map((word) => word.trim().toLowerCase())
        .map((word) => word.replace(pattern, ""))
        .filter(Boolean)
    )
  );
}

/** "https://www.example.com/path" and "example.com" both mean example.com. */
function siteOf(value) {
  const bare = value.replace(/^[a-z]+:\/\//, "").split(/[/?#]/)[0].replace(/^www\./, "");
  return /^[a-z0-9.-]+(:\d+)?$/.test(bare) ? bare.replace(/:\d+$/, "") : "";
}

async function save(patch) {
  await chrome.runtime.sendMessage({ type: "set-settings", patch });
  savedEl.textContent = t("optionsSaved");
  clearTimeout(savedTimer);
  savedTimer = setTimeout(() => (savedEl.textContent = ""), 1500);
}

async function load() {
  document.documentElement.lang = t("@@ui_locale").replace("_", "-");
  for (const el of document.querySelectorAll("[data-i18n]")) el.textContent = t(el.dataset.i18n);

  const settings = await chrome.runtime.sendMessage({ type: "get-settings" });
  for (const id of TOGGLES) {
    const el = document.getElementById(id);
    el.checked = Boolean(settings[id]);
    el.addEventListener("change", () => save({ [id]: el.checked }));
  }

  const minSize = document.getElementById("minSize");
  minSize.value = settings.minSize ? String(Math.round((settings.minSize / MB) * 10) / 10) : "0";
  minSize.addEventListener("change", () => {
    const mb = Math.max(0, Number(minSize.value) || 0);
    save({ minSize: Math.round(mb * MB) });
  });

  const skip = document.getElementById("skipExtensions");
  skip.value = settings.skipExtensions.join(", ");
  skip.addEventListener("change", () => {
    const list = words(skip.value, /^\./).filter((ext) => /^[a-z0-9]{1,8}$/.test(ext));
    skip.value = list.join(", ");
    save({ skipExtensions: list });
  });

  const sites = document.getElementById("excludedSites");
  sites.value = settings.excludedSites.join("\n");
  sites.addEventListener("change", () => {
    const list = Array.from(new Set(words(sites.value, /$^/).map(siteOf).filter(Boolean)));
    sites.value = list.join("\n");
    save({ excludedSites: list });
  });

  if (chrome.commands && chrome.commands.getAll) {
    const commands = await chrome.commands.getAll();
    document.getElementById("shortcuts").textContent = commands
      .filter((c) => c.shortcut)
      .map((c) => `${c.shortcut} - ${c.description}`)
      .join(" | ");
  }
}

load();
