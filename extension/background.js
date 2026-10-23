// Service worker: toggles tab audio capture via offscreen document.
// Toolbar state: idle(灰) -> REC(綠) -> ERR(紅徽章)。狀態存 storage.session，
// SW 被掛起或重啟後會自動還原圖示與提示文字。
const OFFSCREEN_URL = 'offscreen.html';
const WS_URL = 'ws://127.0.0.1:8850/ingest';
const ICON_IDLE = { 16: 'icons/idle_16.png', 32: 'icons/idle_32.png', 48: 'icons/idle_48.png' };
const ICON_ACTIVE = { 16: 'icons/active_16.png', 32: 'icons/active_32.png', 48: 'icons/active_48.png' };
const TITLE_IDLE = 'Live JP-ZH Captions：點擊擷取此分頁音訊';

async function getState() {
  const s = await chrome.storage.session.get(['capturingTabId', 'capturingTitle', 'lastErr']);
  return {
    tabId: s.capturingTabId ?? null,
    title: s.capturingTitle || '',
    lastErr: s.lastErr || '',
  };
}

async function setState(tabId, title, lastErr) {
  await chrome.storage.session.set({
    capturingTabId: tabId,
    capturingTitle: title || '',
    lastErr: lastErr || '',
  });
  await applyAction(await getState());
}

async function applyAction(state) {
  if (state.tabId != null) {
    await chrome.action.setIcon({ path: ICON_ACTIVE });
    await chrome.action.setBadgeText({ text: 'REC' });
    await chrome.action.setBadgeBackgroundColor({ color: '#c0392b' });
    await chrome.action.setTitle({
      title: `● 擷取中：${state.title || '(分頁)'} —\u200E 點擊停止`,
    });
  } else if (state.lastErr) {
    await chrome.action.setIcon({ path: ICON_IDLE });
    await chrome.action.setBadgeText({ text: 'ERR' });
    await chrome.action.setBadgeBackgroundColor({ color: '#c0392b' });
    await chrome.action.setTitle({ title: `擷取失敗：${state.lastErr} — 點擊重試` });
  } else {
    await chrome.action.setIcon({ path: ICON_IDLE });
    await chrome.action.setBadgeText({ text: '' });
    await chrome.action.setTitle({ title: TITLE_IDLE });
  }
}

async function hasOffscreen() {
  const contexts = await chrome.runtime.getContexts({
    contextTypes: ['OFFSCREEN_DOCUMENT'],
  });
  return contexts.length > 0;
}

async function ensureOffscreen() {
  if (await hasOffscreen()) return;
  await chrome.offscreen.createDocument({
    url: OFFSCREEN_URL,
    reasons: ['USER_MEDIA'],
    justification: 'Capture tab audio for the local live caption server.',
  });
}

async function setBadge(text, color) {
  const opts = { text };
  if (color) opts.backgroundColor = color;
  await chrome.action.setBadgeText(opts);
  if (color) await chrome.action.setBadgeBackgroundColor({ color });
}

chrome.action.onClicked.addListener(async (tab) => {
  const state = await getState();
  if (state.tabId != null) {
    // Already capturing -> stop
    try {
      await chrome.runtime.sendMessage({ target: 'offscreen', type: 'stop' });
    } catch (e) { /* offscreen may already be gone */ }
    await setState(null, '', '');
    return;
  }
  await setState(null, '', ''); // 清除上一輪的 ERR
  try {
    if (!tab || tab.id == null) throw new Error('找不到分頁');
    await ensureOffscreen();
    const streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tab.id });
    const r = await chrome.runtime.sendMessage({
      target: 'offscreen',
      type: 'start',
      streamId,
      wsUrl: WS_URL,
    });
    if (!r || !r.ok) throw new Error(r && r.error || '無回應');
    await setState(tab.id, tab.title || '', '');
  } catch (e) {
    console.error('start failed:', e);
    await setState(null, '', String(e && e.message || e));
  }
});

// offscreen 通知擷取音訊結束（分頁關閉 / 擷取被瀏覽器停止）
chrome.runtime.onMessage.addListener((msg) => {
  if (msg && msg.target === 'bg' && msg.type === 'captureEnded') {
    (async () => {
      const state = await getState();
      if (state.tabId != null) {
        await setState(null, '', msg.reason || '音訊串流已結束');
      }
    })();
  }
  return false;
});

// SW 重啟後還原工具列狀態
applyAction(await getState());
