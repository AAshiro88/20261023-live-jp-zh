// Offscreen document: captures tab audio and streams PCM to the local server.
// Uses the official chrome.tabCapture flow: the service worker obtains a
// streamId, this document redeems it via getUserMedia and keeps working
// even after the service worker is suspended.
// Capture tap uses AudioWorklet (ScriptProcessorNode is deprecated).

let media = null;      // MediaStream from the tab
let ctx = null;        // AudioContext (48 kHz)
let src = null;        // MediaStreamAudioSourceNode
let node = null;       // AudioWorkletNode (capture tap)
let zero = null;       // zero-gain to keep the tap alive silently
let ws = null;         // WebSocket to the local server
let reconnectTimer = null;
let running = false;
let wsUrl = null;
let pending = [];      // int16 samples buffered while reconnecting
let accF32 = null;     // mono float accumulator between worklet callbacks
let accLen = 0;

const SR = 48000;              // force the capture context to 48 kHz
const CHUNK_SEND = 8192;       // samples per binary frame (~170 ms)
const PENDING_MAX = 480000;    // ~10 s of 48 kHz audio buffered while offline

function mixdown(chans) {
  // chans: Float32Array[] -> mono Float32Array (average over channels)
  const n = chans[0].length;
  const out = new Float32Array(n);
  const c = chans.length;
  for (let ch = 0; ch < c; ch++) {
    const d = chans[ch];
    for (let i = 0; i < n; i++) out[i] += d[i];
  }
  if (c > 1) {
    for (let i = 0; i < n; i++) out[i] /= c;
  }
  for (let i = 0; i < n; i++) out[i] = Math.max(-1, Math.min(1, out[i]));
  return out;
}

function toInt16(f32) {
  const out = new Int16Array(f32.length);
  for (let i = 0; i < f32.length; i++) {
    out[i] = f32[i] < 0 ? f32[i] * 0x8000 : f32[i] * 0x7fff;
  }
  return out;
}

function joinPending() {
  const total = pending.reduce((a, b) => a + b.length, 0);
  const buf = new Int16Array(total);
  let off = 0;
  for (const p of pending) { buf.set(p, off); off += p.length; }
  pending = [];
  return buf;
}

function connectWs() {
  try { if (ws) { ws.onclose = null; ws.close(); } } catch (e) {}
  ws = new WebSocket(wsUrl);
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => {
    const hello = JSON.stringify({ sr: SR, format: 'pcm_s16le', channels: 1 });
    ws.send(hello);
    if (pending.length) {
      const buf = joinPending();
      if (ws.readyState === WebSocket.OPEN && buf.length) ws.send(buf.buffer);
    }
  };
  ws.onclose = () => {
    if (!running) return;
    reconnectTimer = setTimeout(connectWs, 3000); // keep capture alive, retry
  };
  ws.onerror = () => { /* onclose follows */ };
}

// worklet 回呼：疊加累積 mono 樣本，達到 CHUNK_SEND 才送幀
function onWorkletMessage(e) {
  if (!running) return;
  const mono = mixdown(e.data.chans);
  if (!accF32) accF32 = new Float32Array(CHUNK_SEND + mono.length);
  if (accLen + mono.length >= CHUNK_SEND) {
    const total = accLen + mono.length;
    const buf = new Int16Array(total);
    buf.set(toInt16(accF32.subarray(0, accLen)), 0);
    buf.set(toInt16(mono), accLen);
    accF32 = null;
    accLen = 0;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(buf.buffer);
    } else {
      pending.push(buf);
      const tot = pending.reduce((a, b) => a + b.length, 0);
      if (tot > PENDING_MAX) pending = [joinPending().slice(-PENDING_MAX)];
    }
  } else {
    accF32.set(mono, accLen);
    accLen += mono.length;
  }
}

async function startCapture(streamId, url, sendResponse) {
  if (running) { sendResponse({ ok: false, error: 'already running' }); return; }
  wsUrl = url;
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        mandatory: {
          chromeMediaSource: 'tab',
          chromeMediaSourceId: streamId,
        },
      },
      video: false,
    });
  } catch (err) {
    console.error('getUserMedia failed:', err);
    sendResponse({ ok: false, error: String(err && err.message || err) });
    return;
  }

  try {
    running = true;
    media = stream;
    ctx = new AudioContext({ sampleRate: SR });
    src = ctx.createMediaStreamSource(stream);

    // 擷取串流結束（分頁關閉/被瀏覽器停止）時通知 background 顯示 ERR
    const track = stream.getAudioTracks()[0];
    if (track) {
      track.addEventListener('ended', () => {
        chrome.runtime.sendMessage({
          target: 'bg',
          type: 'captureEnded',
          reason: '音訊串流已結束（分頁關閉或被瀏覽器停止）',
        }).catch(() => {});
        stopAll();
      });
    }

    // Play-through so the user still hears the tab while capturing.
    src.connect(ctx.destination);

    const ch = Math.max(1, src.channelCount);
    await ctx.audioWorklet.addModule(
      chrome.runtime.getURL('capture-worklet.js'));
    node = new AudioWorkletNode(ctx, 'tap-processor', {
      numberOfInputs: 1,
      numberOfOutputs: 1,
      outputChannelCount: [ch],
      channelCount: ch,
    });
    node.port.onmessage = onWorkletMessage;

    // Silent tap: worklet output goes to a zero gain to stay rendered.
    zero = ctx.createGain();
    zero.gain.value = 0;
    src.connect(node);
    node.connect(zero);
    zero.connect(ctx.destination);

    connectWs();
    sendResponse({ ok: true, sampleRate: SR });
  } catch (err) {
    console.error('capture setup failed:', err);
    stopAll();
    sendResponse({ ok: false, error: String(err && err.message || err) });
  }
}

function stopAll() {
  running = false;
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  try { if (node) node.port.onmessage = null; } catch (e) {}
  try { if (node) node.disconnect(); } catch (e) {}
  try { if (zero) zero.disconnect(); } catch (e) {}
  try { if (src) src.disconnect(); } catch (e) {}
  try { if (media) media.getTracks().forEach((t) => t.stop()); } catch (e) {}
  try { if (ctx) ctx.close(); } catch (e) {}
  ws = null; node = null; zero = null; src = null; media = null; ctx = null;
  pending = [];
  accF32 = null;
  accLen = 0;
}

function stopCapture(sendResponse) {
  stopAll();
  sendResponse({ ok: true });
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.target !== 'offscreen') return false;
  if (msg.type === 'start') {
    startCapture(msg.streamId, msg.wsUrl, sendResponse);
    return true; // async response
  }
  if (msg.type === 'stop') {
    stopCapture(sendResponse);
    return false;
  }
  return false;
});
