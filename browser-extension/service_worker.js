const ENDPOINT = "http://127.0.0.1:8765/active-tab";
const HEARTBEAT_MS = 2000;

async function currentFocusedTab() {
  const windows = await chrome.windows.getAll({ populate: true, windowTypes: ["normal"] });
  const focusedWindow = windows.find((item) => item.focused);
  if (!focusedWindow) {
    return null;
  }

  const activeTab = (focusedWindow.tabs || []).find((tab) => tab.active);
  if (!activeTab) {
    return null;
  }

  return {
    browser: navigator.userAgent.includes("Edg/") ? "Edge" : "Chrome",
    title: activeTab.title || "",
    url: activeTab.url || "",
    windowFocused: true,
    timestamp: new Date().toISOString()
  };
}

async function publishActiveTab() {
  try {
    const payload = await currentFocusedTab();
    if (!payload || !payload.url) {
      return;
    }

    await fetch(ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
  } catch (error) {
    // The desktop app may be closed. Keep the extension quiet and retry on the next event.
  }
}

chrome.tabs.onActivated.addListener(() => {
  publishActiveTab();
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (tab.active && (changeInfo.url || changeInfo.title || changeInfo.status === "complete")) {
    publishActiveTab();
  }
});

chrome.windows.onFocusChanged.addListener((windowId) => {
  if (windowId !== chrome.windows.WINDOW_ID_NONE) {
    publishActiveTab();
  }
});

chrome.runtime.onInstalled.addListener(() => {
  publishActiveTab();
});

setInterval(() => {
  publishActiveTab();
}, HEARTBEAT_MS);
