#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""live-jp-zh: 即時日文轉繁體中文字幕。

資料流:
  Chrome 擴充 (tabCapture, offscreen) -> WS /ingest (48kHz 16-bit mono PCM)
  -> StreamingResampler -> StreamVAD -> faster-whisper (ja)
  -> LLM 翻譯 (llama.cpp, Qwen3-4B-Instruct) -> OpenCC s2twp
  -> 主控台 / 字幕網頁 (WS /events) / transcript 記錄檔
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import queue
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import numpy as np
from aiohttp import WSMsgType, web

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = BASE_DIR / "config.json"

INGEST_SAMPLE_RATE = 48000  # 擴充端強制 48kHz
TARGET_SR = 16000

SYSTEM_PROMPT = (
    "你是專業的即時字幕譯者，把日文翻譯成繁體中文（台灣用語）。規則：\n"
    "- 只輸出譯文，全部用中文字，禁止插入英文、假名或任何說明。\n"
    "- 不要加引號或任何符號的框。\n"
    "- 口語、簡短、適合字幕。\n"
    "- 數字與人名要正確保留；作品名可用慣用中文譯名。\n"
    "- 內容不完整時依現有資訊直譯，不要補充或改寫。"
)

PARTIAL_EVERY_SEC = 1.5  # 說話期間每隔幾秒解一次 provisional


# --------------------------------------------------------------------------- #
# 組態與工具
# --------------------------------------------------------------------------- #

def load_config(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def get(d: dict, *keys, default=None):
    for k in keys:
        d = d[k]
    return d


def now_ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


class Console:
    """主控台與記錄檔共用的輸出。"""

    def __init__(self, transcript_path: Path | None):
        self.file = None
        self.lock = threading.Lock()
        if transcript_path is not None:
            transcript_path.parent.mkdir(parents=True, exist_ok=True)
            self.file = open(transcript_path, "a", encoding="utf-8")
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    def write(self, line: str, to_file: bool = True):
        with self.lock:
            print(line, flush=True)
            if self.file and to_file:
                self.file.write(line + "\n")
                self.file.flush()

    def close(self):
        with self.lock:
            if self.file:
                self.file.write(f"[{now_ts()}] session end\n")
                self.file.close()
                self.file = None


# --------------------------------------------------------------------------- #
# 重取樣：48k -> 16k（整數倍 FIR 抽取；非整數倍用 resample_poly 區塊退化）
# --------------------------------------------------------------------------- #

class StreamingResampler:
    def __init__(self, in_sr: int, out_sr: int = TARGET_SR):
        self.in_sr = int(in_sr)
        self.out_sr = int(out_sr)
        self.exact = (self.in_sr % self.out_sr == 0)
        if self.exact:
            from scipy import signal as sig
            self._lfilter = sig.lfilter
            self.d = self.in_sr // self.out_sr
            self.taps = sig.firwin(65, 0.9 / self.d)
            self.zi = sig.lfilter_zi(self.taps, 1.0) * 0.0
            self.counter = 0
        else:
            from scipy import signal as _sig
            self._sig = _sig
            g = math.gcd(self.in_sr, self.out_sr)
            self.up, self.down = self.out_sr // g, self.in_sr // g
            self.buf = np.zeros(0, dtype=np.float32)
            self.carry = np.zeros(0, dtype=np.float32)

    def push(self, x_int16: np.ndarray) -> np.ndarray:
        x = x_int16.astype(np.float32) / 32768.0
        if self.exact:
            y, self.zi = self._lfilter(self.taps, 1.0, x, zi=self.zi)
            first = (self.d - self.counter % self.d) % self.d
            idx = np.arange(first, len(y), self.d)
            self.counter += len(x)
            return y[idx].astype(np.float32)
        # 非整數倍：區塊 resample_poly（44.1k 等罕見情況）
        self.buf = np.concatenate([self.carry, self.buf, x])
        if len(self.buf) < self.in_sr:
            return np.zeros(0, dtype=np.float32)
        y = self._sig.resample_poly(self.buf, self.up, self.down)
        # 保留一段重疊避免區塊邊界的濾波突刺
        self.carry = np.concatenate([np.zeros(0), self.buf])[-2 * self.down:]
        self.buf = np.zeros(0, dtype=np.float32)
        return y.astype(np.float32)


# --------------------------------------------------------------------------- #
# Silero VAD 串流切句（faster-whisper 內附 ONNX，不需 torch）
# --------------------------------------------------------------------------- #

import onnxruntime  # noqa: E402


class StreamVAD:
    def __init__(self, cfg: dict, on_utterance, on_partial_tick):
        from faster_whisper.utils import get_assets_path
        self.on_utterance = on_utterance
        self.on_partial_tick = on_partial_tick
        path = os.path.join(get_assets_path(), "silero_vad_v6.onnx")
        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.enable_cpu_mem_arena = False
        opts.log_severity_level = 4
        self.session = onnxruntime.InferenceSession(
            path, providers=["CPUExecutionProvider"], sess_options=opts)
        self.n_inputs = len(self.session.get_inputs())

        self.h = np.zeros((1, 1, 128), dtype="float32")
        self.c = np.zeros((1, 1, 128), dtype="float32")
        self.context = np.zeros((64,), dtype="float32")

        self.window = 512  # 32 ms @ 16k
        self.threshold = float(cfg.get("threshold", 0.5))
        self.neg = max(self.threshold - 0.15, 0.01)
        self.min_sil_ms = int(cfg.get("min_silence_duration_ms", 500))
        self.min_speech_ms = int(cfg.get("min_speech_duration_ms", 300))
        self.max_samples = int(float(cfg.get("max_utterance_seconds", 12.0)) * TARGET_SR)
        self.preroll_n = int(float(cfg.get("preroll_seconds", 0.3)) * TARGET_SR) // self.window

        self.preroll: deque = deque(maxlen=max(self.preroll_n, 1))
        self.buf = np.zeros(0, dtype=np.float32)
        self.triggered = False
        self.speech: list = []
        self.speech_samples = 0
        self.silence_ms = 0
        self.last_partial_tick = 0.0

    def _prob(self, window: np.ndarray) -> float:
        inp = np.concatenate([self.context[-64:], window])[None, :]
        if self.n_inputs == 3:
            out, self.h, self.c = self.session.run(
                None, {"input": inp, "h": self.h, "c": self.c})
        else:
            out = self.session.run(None, {"input": inp})[0]
        self.context = window.copy()
        return float(np.asarray(out).ravel()[0])

    def feed(self, pcm: np.ndarray):
        self.buf = np.concatenate([self.buf, pcm])
        step = self.window
        while len(self.buf) >= step:
            w = self.buf[:step].copy()
            self.buf = self.buf[step:]
            self._process_window(w)

    def _process_window(self, w: np.ndarray):
        p = self._prob(w)
        t = time.time()
        if not self.triggered:
            self.preroll.append(w)
            if p >= self.threshold:
                self.triggered = True
                self.speech = list(self.preroll)
                self.preroll.clear()
                self.speech_samples = len(self.speech) * self.window
                self.silence_ms = 0
                self.last_partial_tick = t
        else:
            self.speech.append(w)
            self.speech_samples += self.window
            if p < self.neg:
                self.silence_ms += 32
                if self.silence_ms >= self.min_sil_ms:
                    self.finalize(True)
                    return
            else:
                self.silence_ms = 0
            if self.speech_samples >= self.max_samples:
                self.finalize(False)
                return
            if (t - self.last_partial_tick) >= PARTIAL_EVERY_SEC:
                self.last_partial_tick = t
                self.on_partial_tick(np.concatenate(self.speech))

    def finalize(self, trim_tail: bool):
        n_keep = self.speech_samples
        audio = np.concatenate(self.speech).copy()
        dur = len(audio) / TARGET_SR
        self.triggered = False
        self.speech = []
        self.speech_samples = 0
        self.silence_ms = 0
        if trim_tail and n_keep < len(audio):
            audio = audio[:n_keep]
            dur = n_keep / TARGET_SR
        if dur < self.min_speech_ms / 1000.0:
            return  # 太短丟棄
        self.on_utterance(audio, dur)


# --------------------------------------------------------------------------- #
# ASR 執行緒
# --------------------------------------------------------------------------- #

class AsrWorker(threading.Thread):
    def __init__(self, cfg: dict, console: Console, job_q: "queue.Queue", publish):
        super().__init__(daemon=True, name="asr")
        self.cfg = cfg
        self.console = console
        self.q = job_q
        self.publish = publish
        self.model = None
        self.last_final = ""

    def stop(self):
        self.q.put(None)

    def _load(self):
        from faster_whisper import WhisperModel
        w = self.cfg
        path = (w.get("whisper_model_path") or "").strip()
        models_dir = w.get("models_dir", "models")
        root = str(BASE_DIR / models_dir / "faster-whisper")
        if path:
            self.console.write(f"[ASR] 載入模型路徑: {path}")
            self.model = WhisperModel(path, device=w.get("device", "cuda"),
                                      compute_type=w.get("compute_type", "auto"))
        else:
            self.console.write(f"[ASR] 載入 {w['model']}（快取 {root}）")
            self.model = WhisperModel(w["model"], download_root=root,
                                      device=w.get("device", "cuda"),
                                      compute_type=w.get("compute_type", "auto"))

    def _kw_final(self):
        w = self.cfg
        kw = dict(
            language=w.get("language", "ja"),
            beam_size=int(w.get("beam_size", 5)),
            temperature=list(w.get("temperature", [0.0])),
            condition_on_previous_text=bool(w.get("condition_on_previous_text", False)),
            no_speech_threshold=float(w.get("no_speech_threshold", 0.6)),
            log_prob_threshold=float(w.get("log_prob_threshold", -1.0)),
            compression_ratio_threshold=float(w.get("compression_ratio_threshold", 2.4)),
            vad_filter=False,
        )
        hw = w.get("hotwords") or []
        if hw:
            kw["hotwords"] = " ".join(hw)
        return kw

    def run(self):
        try:
            self._load()
        except Exception as e:
            self.publish("fatal", {"stage": "asr_load", "error": str(e)})
            self.console.write(f"[ASR] 模型載入失敗: {e}")
            return
        self.console.write("[ASR] 模型就緒")
        while True:
            job = self.q.get()
            if job is None:
                return
            kind, seq, audio, dur = job
            try:
                t0 = time.time()
                if kind == "partial":
                    segs, _ = self.model.transcribe(
                        audio, language=self.cfg.get("language", "ja"),
                        beam_size=1, best_of=1, temperature=(0.0,),
                        condition_on_previous_text=False, vad_filter=False)
                    text = "".join(s.text for s in segs).strip()
                    dt = time.time() - t0
                    if text:
                        self.publish("partial", {"seq": seq, "ja": text,
                                                 "t": round(dt, 2)})
                else:
                    segs, _ = self.model.transcribe(audio, **self._kw_final())
                    text = "".join(s.text for s in segs).strip()
                    dt = time.time() - t0
                    rtf = dt / max(dur, 1e-6)  # 實時比（越小越快）
                    if self.cfg.get("drop_repeated_segments", True) \
                            and text and text == self.last_final:
                        self.console.write(f"[ASR] 丟棄重複輸出: {text[:24]}")
                        self.publish("drop", {"seq": seq})
                        continue
                    self.last_final = text
                    self.console.write(
                        f"[{now_ts()}] ASR #{seq} 語音{dur:.1f}s 解碼{dt:.1f}s "
                        f"(rtf {rtf:.2f}) JA {text}")
                    self.publish("final_ja", {"seq": seq, "ja": text,
                                              "dur": round(dur, 2),
                                              "decode": round(dt, 2)})
            except Exception as e:
                self.console.write(f"[ASR] 轉錄失敗: {e}")


# --------------------------------------------------------------------------- #
# 翻譯執行緒（llama.cpp + OpenCC）
# --------------------------------------------------------------------------- #

class Translator(threading.Thread):
    def __init__(self, cfg: dict, out_cfg: dict, console: Console, job_q: "queue.Queue",
                 publish):
        super().__init__(daemon=True, name="llm")
        self.cfg = cfg
        self.out_cfg = out_cfg
        self.console = console
        self.q = job_q
        self.publish = publish
        self.llm = None
        self.hist: deque = deque(maxlen=int(cfg.get("history_turns", 3)))
        self.converter = None

    def stop(self):
        self.q.put(None)

    def _load(self) -> bool:
        import llama_cpp
        from opencc import OpenCC
        self.converter = OpenCC(self.out_cfg.get("opencc_config", "s2twp"))
        path = Path(self.cfg["gguf_path"])
        if not path.is_absolute():
            path = BASE_DIR / path
        path = path.resolve()
        if not path.exists():
            self.console.write(f"[LLM] 找不到模型檔: {path}（請先執行 setup_env.bat）")
            self.publish("fatal", {"stage": "llm_load", "error": f"missing {path}"})
            return False
        n_threads = int(self.cfg.get("n_threads", -1))
        if n_threads <= 0:
            n_threads = os.cpu_count() or 4
        t0 = time.time()
        self.console.write(f"[LLM] 載入 {path.name}（CPU 執行緒 {n_threads}）…")
        self.llm = llama_cpp.Llama(
            model_path=str(path),
            n_ctx=int(self.cfg.get("n_ctx", 4096)),
            n_threads=n_threads,
            n_gpu_layers=int(self.cfg.get("n_gpu_layers", 0)),
            verbose=False,
        )
        self.console.write(f"[LLM] 模型就緒（{time.time() - t0:.1f}s）")
        return True

    def _sanitize(self, text: str) -> str:
        text = text.strip()
        text = text.strip("` \t\r\n")
        text = text.strip("「」『』\"'")
        # 常見的說明性前綴丟掉
        for pre in ("譯文：", "译文：", "訳:", "翻譯:", "翻译:"):
            if text.startswith(pre):
                text = text[len(pre):].strip()
        return self.converter.convert(text).strip()

    def _translate(self, text: str) -> str:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        for ja, zh in self.hist:
            messages.append({"role": "user", "content": ja})
            messages.append({"role": "assistant", "content": zh})
        messages.append({"role": "user", "content": text})
        resp = self.llm.create_chat_completion(
            messages,
            max_tokens=int(self.cfg.get("max_tokens", 200)),
            temperature=float(self.cfg.get("temperature", 0.3)),
            top_p=0.9,
            repeat_penalty=1.05,
        )
        return self._sanitize(resp["choices"][0]["message"]["content"] or "")

    def run(self):
        loaded = False
        while True:
            job = self.q.get()
            if job is None:
                return
            kind, seq, text = job
            try:
                if not loaded:
                    if not self._load():
                        return
                    loaded = True
                    if kind == "warmup":
                        continue
                t0 = time.time()
                zh = self._translate(text)
                dt = time.time() - t0
                if kind == "final_ja":
                    self.hist.append((text, zh))
                out_stage = "final_zh" if kind == "final_ja" else "partial"
                self.publish(out_stage, {"seq": seq, "ja": text, "zh": zh,
                                         "t": round(dt, 2)})
            except Exception as e:
                self.console.write(f"[LLM] 翻譯失敗: {e}")


# --------------------------------------------------------------------------- #
# 事件廣播（worker 執行緒 -> asyncio WS clients）
# --------------------------------------------------------------------------- #

class Broadcaster:
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.clients: dict = {}
        self.captures: set = set()
        self.segments = 0

    async def register(self, ws) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self.clients[ws] = q
        return q

    async def unregister(self, ws):
        self.clients.pop(ws, None)

    def publish(self, stage: str, data: dict):
        event = {"stage": stage,
                 "ts": datetime.now().strftime("%H:%M:%S"), **data}
        if stage == "final_zh":
            self.segments += 1
        if not self.clients:
            return
        try:
            self.loop.call_soon_threadsafe(self._push, event)
        except RuntimeError:
            pass

    def _push(self, event: dict):
        for q in list(self.clients.values()):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass


# --------------------------------------------------------------------------- #
# 音訊檔模擬輸入
# --------------------------------------------------------------------------- #

def wav_source(path: Path, sr_in: int, sink, realtime: bool):
    """解碼音訊檔 -> s16 mono @sr_in -> 逐塊交給 sink。"""
    import av

    container = av.open(str(path))
    stream = container.streams.audio[0]
    to_sr = av.AudioResampler(format="s16", layout="mono", rate=sr_in)
    for packet in container.demux(stream):
        for frame in packet.decode():
            for rf in to_sr.resample(frame):
                chunk = rf.to_ndarray().reshape(-1)
                sink(chunk.astype(np.int16))
                if realtime:
                    time.sleep(len(chunk) / sr_in)
    container.close()


# --------------------------------------------------------------------------- #
# 主程式
# --------------------------------------------------------------------------- #

HTML = """<!DOCTYPE html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<title>即時日→中字幕</title>
<style>
html,body {{ margin:0; height:100%; overflow:hidden; {bg}; font-family:"Microsoft JhengHei",sans-serif; }}
.wrap {{ display:flex; flex-direction:column; justify-content:flex-end; height:100%; box-sizing:border-box; padding:10px 14px; gap:10px; }}
#partial {{ order:999; }}
.entry {{ {border}; padding:4px 6px; {shadow} }}
.ja {{ color:#9aa0a6; font-size:14px; line-height:1.35; }}
.zh {{ color:#fff; font-size:22px; line-height:1.5; font-weight:600; }}
.partial {{ opacity:.55; }}
.head {{ position:fixed; top:6px; left:16px; color:#7c828a; font-size:12px; }}
a {{ color:#8ab4f8; }}
{extra_style}
</style></head>
<body>
{header}
<div class="wrap" id="wrap">
<div class="entry partial" id="partial" style="display:none">
  <div class="ja" id="pja"></div><div class="zh" id="pzh"></div>
</div>
</div>
<script>
const wrap = document.getElementById('wrap');
const pb = document.getElementById('partial');
const pja = document.getElementById('pja');
const pzh = document.getElementById('pzh');
const MAX = {max_lines};
function clearPartial() {{ pb.style.display = 'none'; }}
function pushEntry(e) {{
  const d = document.createElement('div');
  d.className = 'entry';
  d.innerHTML = '<div class="ja"></div><div class="zh"></div>';
  d.children[0].textContent = e.ja;
  d.children[1].textContent = e.zh;
  wrap.appendChild(d);
  while (wrap.querySelectorAll('.entry:not(.partial)').length > MAX)
    wrap.querySelector('.entry:not(.partial)').remove();
}}
const ws = new WebSocket('ws://' + location.host + '/events');
ws.onmessage = (ev) => {{
  const e = JSON.parse(ev.data);
  if (e.stage === 'final_zh') {{
    pushEntry(e);
    clearPartial();
  }} else if (e.stage === 'partial') {{
    pb.style.display = '';
    pja.textContent = e.ja || '';
    pzh.textContent = e.zh ? e.zh : '（辨識中…）';
  }}
}};
</script></body></html>"""


def push_int16(state, x_int16: np.ndarray):
    """重取樣 -> 16k 交給 VAD 佇列（ingest WS 與 WAV 模擬共用）。"""
    block = state["rs"].push(x_int16)
    if len(block):
        state["pcm_q"].put(block)


def render_page(style: str, max_lines: int) -> str:
    if style == "obs":
        bg = "background:transparent"
        header = ""
        border = "border:none"
        shadow = "text-shadow:0 0 4px rgba(0,0,0,.95),0 1px 2px rgba(0,0,0,.95)"
        extra_style = ""
    elif style == "mini":
        # 迷你視窗：黑底、省空間的字級、無頁首
        bg = "background:#0e0e12"
        header = ""
        border = "border:none"
        shadow = ""
        extra_style = (".wrap{{padding:6px 10px;gap:5px}}"
                       ".zh{{font-size:15px;line-height:1.45}}"
                       ".ja{{font-size:10px}}")
    else:
        bg = "background:#141418"
        header = ('<div class="head">live-jp-zh 即時日→中字幕　'
                  '<a href="/?mini=1" target="_blank">迷你視窗</a>　'
                  '<a href="/?obs=1" target="_blank">OBS 透明版</a></div>')
        border = "border-top:1px solid #26262c"
        shadow = ""
        extra_style = ""
    return HTML.format(bg=bg, header=header, border=border, shadow=shadow,
                       max_lines=max_lines, extra_style=extra_style)


async def ingest_handler(request, state) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(max_msg_size=4 * 1024 * 1024)
    await ws.prepare(request)
    if state["ingest_ws"] is not None:
        await ws.send_json({"stage": "error", "text": "已有音訊來源連線中"})
        await ws.close()
        return ws
    state["ingest_ws"] = ws
    state["console"].write(f"[S] 音訊來源已連線 {request.remote}")
    hello = None
    resampler = None
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    hello = json.loads(msg.data)
                except Exception:
                    hello = None
                if not hello or hello.get("format") != "pcm_s16le" \
                        or int(hello.get("channels", 0)) != 1:
                    await ws.send_json({"stage": "error",
                                        "text": "hello 需 format=pcm_s16le, channels=1"})
                    await ws.close()
                    return ws
                sr = int(hello.get("sr", 0))
                if sr not in (16000, 24000, 32000, 44100, 48000, 96000):
                    await ws.send_json({"stage": "error", "text": f"不支援取樣率 {sr}"})
                    await ws.close()
                    return ws
                resampler = StreamingResampler(sr, TARGET_SR)
                state["rs"] = resampler
                state["console"].write(
                    f"[S] 音訊輸入 {sr} Hz -> {TARGET_SR} Hz"
                    + ("（整數抽取）" if resampler.exact else "（區塊 resample_poly）"))
            elif msg.type == WSMsgType.BINARY:
                if hello is None or state["rs"] is None:
                    continue
                x = np.frombuffer(msg.data, dtype=np.int16)
                if 0 < len(x) <= INGEST_SAMPLE_RATE * 2:
                    push_int16(state, x)
            elif msg.type == WSMsgType.ERROR:
                break
    finally:
        state["ingest_ws"] = None
        state["console"].write("[S] 音訊來源已離線")
    return ws


async def run(args) -> int:
    cfg = load_config(Path(args.config).resolve())
    host = str(get(cfg, "server", "host", default="127.0.0.1"))
    port = args.port if args.port else int(get(cfg, "server", "port", default=8850))
    if args.host:
        host = args.host
    if host not in ("127.0.0.1", "localhost"):
        print("server.host 僅允許 127.0.0.1 / localhost（本機工具不對外開放）")
        return 1
    loop = asyncio.get_running_loop()
    broadcaster = Broadcaster(loop)

    session_name = datetime.now().strftime("%Y%m%d_%H%M%S")
    transcript_dir = BASE_DIR / str(get(cfg, "output", "transcript_dir",
                                        default="transcript"))
    console = Console(transcript_dir / f"{session_name}.log")
    console.write(f"[S] 即時日→中字幕 HTTP/WS 於 http://{host}:{port}/")
    console.write(f"[S] 記錄檔 {transcript_dir / (session_name + '.log')}")

    max_lines = int(get(cfg, "output", "max_lines_on_page", default=12))

    state = {"console": console, "pcm_q": queue.Queue(), "ingest_ws": None,
             "rs": None}
    asr_q: "queue.Queue" = queue.Queue()
    trans_q: "queue.Queue" = queue.Queue()
    counters = {"seq": 0}

    enable_translate = bool(cfg["translate"].get("enabled", True))
    skip_threshold = int(cfg["translate"].get("skip_if_pending", 3))

    def on_utterance(audio: np.ndarray, dur: float):
        counters["seq"] += 1
        asr_q.put(("final", counters["seq"], audio, dur))

    def on_partial_tick(audio: np.ndarray):
        asr_q.put(("partial", counters["seq"] + 1, audio, 0.0))

    def asr_publish(stage: str, data: dict):
        # ASR 事件 -> 翻譯佇列轉發（final_ja / partial）
        if stage == "final_ja" and enable_translate:
            trans_q.put(("final_ja", data["seq"], data["ja"]))
        elif stage == "partial" and enable_translate:
            if trans_q.qsize() < skip_threshold:  # 積壓過多時只出日文 partial
                trans_q.put(("partial", data["seq"], data["ja"]))
        if stage == "partial":
            console.write(f"[{now_ts()}] PR  {data.get('ja', '')}  ({data.get('t')}s)",
                          to_file=False)
        broadcaster.publish(stage, data)

    def llm_publish(stage_out: str, data: dict):
        # LLM 事件（final_zh / 含 zh 的 partial）只輸出，不再回饋佇列
        if stage_out == "final_zh":
            console.write(f"[{now_ts()}] ZH  {data['zh']}  (LLM {data.get('t')}s)")
        else:
            console.write(f"[{now_ts()}] PRZ {data.get('zh', '')}",
                          to_file=False)
        broadcaster.publish(stage_out, data)

    asr_worker = AsrWorker(cfg["asr"], console, asr_q, asr_publish)
    translator = Translator(cfg["translate"], cfg["output"], console, trans_q,
                            llm_publish)

    vad = StreamVAD(cfg["vad"], on_utterance, on_partial_tick)

    def vad_feeder():
        while True:
            pcm = state["pcm_q"].get()
            if pcm is None:
                break
            try:
                vad.feed(pcm)
            except Exception as e:
                console.write(f"[VAD] 失敗: {e}")

    # 應用路由
    def pick_style(r):
        # obs=1 透明字幕（OBS overlay）、mini=1 迷你黑底視窗
        if r.query.get("obs") == "1":
            return "obs"
        if r.query.get("mini") == "1":
            return "mini"
        return "normal"

    async def events_handler(request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        q = await broadcaster.register(ws)
        try:
            while True:
                event = await q.get()
                if event is None:
                    break
                await ws.send_json(event)
        finally:
            await broadcaster.unregister(ws)
        return ws

    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(
        text=render_page(pick_style(r), max_lines),
        content_type="text/html", charset="utf-8"))
    app.router.add_get("/events", events_handler)
    app.router.add_get("/ingest", lambda r: ingest_handler(r, state))
    app.router.add_get("/health", lambda r: web.json_response(
        {"status": "ok", "segments": broadcaster.segments,
         "capture": 1 if state["ingest_ws"] else 0}))

    asr_worker.start()
    translator.start()
    if enable_translate:
        trans_q.put(("warmup", 0, ""))
    threading.Thread(target=vad_feeder, daemon=True, name="vad-feed").start()

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    console.write("[S] 待命中：瀏覽器開 http://%s:%d/ 並在目標分頁點擴充功能圖示"
                  % (host, port))
    wav_done = {"flag": False}

    def wav_wrapper():
        try:
            wav_source(src_path, int(args.wav_sr),
                       lambda b: push_int16(state, b), not args.no_realtime)
        except Exception as e:
            console.write(f"[S] WAV 餵音失敗: {e}")
        wav_done["flag"] = True

    try:
        if args.wav:
            src_path = Path(args.wav).resolve()
            if not src_path.exists():
                console.write(f"[S] 找不到音訊檔 {src_path}")
                return 1
            state["rs"] = StreamingResampler(int(args.wav_sr), TARGET_SR)
            console.write(f"[S] WAV 模擬輸入: {src_path.name}（{args.wav_sr} Hz -> 16k）"
                          + ("" if args.no_realtime else "（實時節奏）"))
            threading.Thread(target=wav_wrapper, daemon=True, name="wav-src").start()
            # 等音檔放完，並讓 ASR / 翻譯把殘留工作清空
            while not wav_done["flag"]:
                await asyncio.sleep(0.5)
            settled = 0
            while settled < 2:
                if asr_q.qsize() == 0 and trans_q.qsize() == 0:
                    settled += 1
                else:
                    settled = 0
                await asyncio.sleep(0.5)
            await asyncio.sleep(3.0)  # 留緩衝給最後一段
        else:
            try:
                while True:
                    await asyncio.sleep(3600)
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                pass
    finally:
        asr_worker.stop()
        if enable_translate:
            translator.stop()
        await runner.cleanup()
        console.write(f"[S] 結束，共輸出 {broadcaster.segments} 段字幕")
        console.close()
    return 0


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="即時日文轉繁體中文字幕")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--host", default=None)
    ap.add_argument("--wav", default=None, help="音訊檔模擬串流輸入（測試用）")
    ap.add_argument("--wav-sr", default=48000, help="--wav 檔案取樣率（預設 48000）")
    ap.add_argument("--no-realtime", action="store_true",
                    help="--wav 模式不按實時節奏餵音")
    return ap.parse_args(argv)


def main() -> int:
    if sys.platform == "win32":
        os.system("")  # 啟用主控台 ANSI
    args = parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n[S] 已結束")
        return 0


if __name__ == "__main__":
    sys.exit(main())
