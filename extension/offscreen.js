// Offscreen document: captures tab audio and streams PCM to the local server.
// Uses the official chrome.tabCapture flow: the service worker obtains a
// streamId, this document redeems it via getUserMedia and keeps working
// even after the service worker is suspended.

let media = null;      // MediaStream from the tab
let ctx = null;        // AudioContext (48 kHz)
let src = null;        // MediaStreamAudioSourceNode
let node = null;       // ScriptProcessorNode (capture tap)
let zero = null;       // zero-gain to keep the tap alive silently
let ws = null;         // WebSocket to the local server
let reconnectTimer = null;
let running = false;
let wsUrl = null;
let pending = [];      // int16 samples buffered while reconnecting

const SR = 48000;              // force the capture context to 48 kHz
const CHUNK_SEND = 8192;       // samples per binary frame (~170 ms)
const PENDING_MAX = 480000;    // ~10 s of 48 kHz audio buffered while offline

function toInt16(inputBuffer) {
  const chans = [];
  for (let i = 0; i < inputBuffer.numberOfChannels; i++) {
    chans.push(inputBuffer.getChannelData(i));
  }
  const n = inputBuffer.length;
  const out = new Int16Array(n);
  for (let i = 0; i < n; i++) {
    let s = 0;
    for (let c = 0; c < chans.length; c++) s += chans[c][i];
    s /= chans.length;
    s = Math.max(-1, Math.min(1, s));
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
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

function startCapture(streamId, url, sendResponse) {
  if (running) { sendResponse({ ok: false, error: 'already running' }); return; }
  wsUrl = url;
  navigator.mediaDevices
    .getUserMedia({
      audio: {
        mandatory: {
          chromeMediaSource: 'tab',
          chromeMediaSourceId: streamId,
        },
      },
      video: false,
    })
    .then((stream) => {
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

      // Silent capture tap (input/output channels must match the stream).
      const ch = Math.max(1, src.channelCount);
      node = ctx.createScriptProcessor(4096, ch, ch);
      let acc = new Int16Array(0);
      let accLen = 0;
      node.onaudioprocess = (e) => {
        if (!running) return;
        const ints = toInt16(e.inputBuffer);
        if (accLen + ints.length >= CHUNK_SEND) {
          const buf = new Int16Array(accLen + ints.length);
          buf.set(acc, 0);
          buf.set(ints, accLen);
          acc = new Int16Array(0);
          accLen = 0;
          if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(buf.buffer);
          } else {
            pending.push(buf);
            const tot = pending.reduce((a, b) => a + b.length, 0);
            if (tot > PENDING_MAX) pending = [joinPending().slice(-PENDING_MAX)];
          }
        } else {
          const nb = new Int16Array(accLen + ints.length);
          nb.set(acc, 0);
          nb.set(ints, accLen);
          acc = nb;
          accLen = nb.length;
        }
      };
      zero = ctx.createGain();
      zero.gain.value = 0;
      src.connect(node);
      node.connect(zero);
      zero.connect(ctx.destination);

      connectWs();
      sendResponse({ ok: true, sampleRate: SR });
    })
    .catch((err) => {
      console.error('getUserMedia failed:', err);
      sendResponse({ ok: false, error: String(err && err.message || err) });
    });
}

function stopAll() {
  running = false;
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  try { if (ws) { ws.onclose = null; ws.close(); } } catch (e) {}
  try { if (node) node.disconnect(); } catch (e) {}
  try { if (zero) zero.disconnect(); } catch (e) {}
  try { if (src) src.disconnect(); } catch (e) {}
  try { if (media) media.getTracks().forEach((t) => t.stop()); } catch (e) {}
  try { if (ctx) ctx.close(); } catch (e) {}
  ws = null; node = null; zero = null; src = null; media = null; ctx = null;
  pending = [];
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
