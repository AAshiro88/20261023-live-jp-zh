// Service worker: toggles tab audio capture via offscreen document.
const OFFSCREEN_URL = 'offscreen.html';
const WS_URL = 'ws://127.0.0.1:8850/ingest';

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
  const { capturingTabId } = await chrome.storage.session.get('capturingTabId');
  if (capturingTabId != null) {
    // Already capturing -> stop
    await chrome.runtime.sendMessage({ target: 'offscreen', type: 'stop' }).catch(() => {});
    await chrome.storage.session.set({ capturingTabId: null });
    setBadge('');
    return;
  }
  try {
    await ensureOffscreen();
    const streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tab.id });
    const r = await chrome.runtime.sendMessage({
      target: 'offscreen',
      type: 'start',
      streamId,
      wsUrl: WS_URL,
    });
    if (r && r.ok) {
      await chrome.storage.session.set({ capturingTabId: tab.id });
      await setBadge('REC', '#c0392b');
    } else {
      console.error('start failed:', r && r.error);
      await setBadge('ERR', '#7f0000');
    }
  } catch (e) {
    console.error(e);
    await setBadge('ERR', '#7f0000');
  }
});
