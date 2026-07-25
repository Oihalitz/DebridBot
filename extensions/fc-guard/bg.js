/* Cierra pestañas/popups que no son de filecrypt ni de su captcha.
   La pestaña principal de filecrypt se deja en paz. */

const KEEP = [
  "filecrypt.cc",
  "filecrypt.co",
  "filecrypt.to",
  "cutcaptcha.net",
  "captcha.filecrypt.cc",
  "pow.filecrypt.cc",
  "static.filecrypt.to",
  "chrome-extension://",
  "about:blank",
  "chrome://",
];

function isKept(url) {
  if (!url) return true;
  const u = String(url).toLowerCase();
  return KEEP.some((k) => u.includes(k));
}

// Popups abiertos desde filecrypt → cerrar si salen a publicidad
chrome.tabs.onCreated.addListener((tab) => {
  // dar un instante a que pongan URL real
  setTimeout(() => {
    chrome.tabs.get(tab.id, (t) => {
      if (chrome.runtime.lastError || !t) return;
      if (t.openerTabId == null) return; // no es popup
      if (!isKept(t.url || t.pendingUrl || "")) {
        chrome.tabs.remove(t.id).catch(() => {});
      }
    });
  }, 400);
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (!changeInfo.url) return;
  if (isKept(changeInfo.url)) return;
  // Solo cerrar si nació como popup (tiene opener)
  if (tab.openerTabId != null) {
    chrome.tabs.remove(tabId).catch(() => {});
  }
});
