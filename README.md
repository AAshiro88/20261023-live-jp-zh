# live-jp-zh

瀏覽器分頁音訊即時字幕：日文 ASR + 本機 LLM 翻譯成繁體中文（台灣用語）。

- 音訊來源：Chrome 擴充功能擷取**指定分頁**（非全系統音），WebSocket 送本機伺服器。
- 全程離線：faster-whisper `large-v3`（GPU/CUDA）+ llama.cpp `Qwen3-4B-Instruct-2507` Q4（CPU）。
- 字幕輸出：主控台、字幕網頁（可放 OBS 當 overlay）、transcript 記錄檔。

## 架構

```
Chrome 分頁
  └─ extension (MV3): chrome.tabCapture
        └─ offscreen.js: AudioWorklet 48kHz mono s16 PCM
              └─ WebSocket ws://127.0.0.1:8850/ingest
live_translate.py (aiohttp)
  ├─ 48k→16k FIR 重取樣（StreamingResampler，帶狀態）
  ├─ Silero VAD 串流切句（faster-whisper 內附 silero_vad_v6.onnx）
  ├─ faster-whisper large-v3（language=ja，CUDA）
  ├─ llama.cpp Qwen3-4B Q4_K_M（CPU，日本語→繁體中文）
  ├─ OpenCC s2twp
  └─ 輸出：console、transcript log、
       http://127.0.0.1:8850/        字幕網頁
       http://127.0.0.1:8850/?obs=1  OBS 透明 overlay 版
```

資料流：`final_ja` → 翻譯 → `final_zh`；部分轉錄（partial）先行顯示並送翻譯（積壓超過 `skip_if_pending` 時跳過，只等最終版），避免排隊堆積。

## 安裝

1. conda 環境（只需一次；會建 `live-jp-zh` 環境、下載模型與依賴）：

   ```
   setup_env.bat
   ```

   - Whisper 模型 `large-v3` 若本機已有快取會以 junction 連結，不重複下載。
   - GGUF 下載到 `models\Qwen3-4B-Instruct-2507-Q4_K_M.gguf`。

2. Chrome 擴充功能（只需一次）：

   - 開 `chrome://extensions` → 右上「開發人員模式」→「載入未封裝項目」→ 選 `D:\claudeFile\20261023-live-jp-zh\extension`。

## 使用

1. 雙擊 `run_live.bat`（等同：`live-jp-zh` 環境執行 `python live_translate.py`）。
   伺服器啟動在 `http://127.0.0.1:8850/`，ASR 與 LLM 載入約 3–10 秒。
2. 開字幕網頁 `http://127.0.0.1:8850/`。
3. 到要翻譯的日文分頁（例如 YouTube 直播），點 Chrome 擴充功能圖示中的
   **「即時日→中字幕」**；點下去該分頁會開始送音。
  要停止則再次點圖示（分頁重新載入也會停止）。
4. 字幕同時寫進 `transcript\<日期時間>.log`（含日文原文與中文譯文）。

OBS：加「瀏覽器」來源，網址 `http://127.0.0.1:8850/?obs=1`，勾「重新整理時暫停 CSS」不必要，直接用即可（背景透明）。

## CLI 測試（不需要瀏覽器）

```
python live_translate.py --wav 檔案.wav            # 實時節奏模擬串流
python live_translate.py --wav 檔案.wav --no-realtime  # 全速餵（量測箱）
```

實測（20.8 秒日文合成語音、三句，RTX 3050 8GB + i5 CPU）：

| 項目 | 數值 |
|---|---|
| ASR 定案延遲（發話結束→日文定稿） | 0.7–1.0s |
| ASR RTF（語音時長/解碼時長） | 0.16–0.35 |
| LLM 翻譯每句 | 0.5–1.3s |
| 發話結束→中文定稿總延遲 | 約 1.3–2.3s |
| 部分字幕出現延遲 | 約 0.7–1.5s（邊說邊出） |
| 記憶體（VRAM） | whisper large-v3 約 3.5GB |

## 設定（config.json）

| 鍵 | 說明 |
|---|---|
| `server.port` / `host` | HTTP/WS 埠（預設 8850），host 僅允許 127.0.0.1/localhost |
| `asr.model` | 預設 `large-v3`；可改 `large-v3-turbo`（更快、品質略低） |
| `asr.device` / `compute_type` | 預設 `cuda`/`auto`；CPU 環境改 `cpu`/`int8` |
| `asr.beam_size` | 預設 5 |
| `vad.min_silence_ms` | 切句靜音門檻（預設 500） |
| `vad.min_speech_ms` | 最短語音段（預設 250） |
| `translate.model_gguf` | 翻譯 GGUF 路徑 |
| `translate.threads` | llama.cpp CPU 執行緒數（預設 24） |
| `translate.temperature` | 預設 0.15 |
| `translate.skip_if_pending` | 積壓 partial 翻譯上限（預設 3） |
| `output.ts_offset` | 字幕網頁時間戳顯示的時差（小時） |

LLM 對話可帶最近 2 條 `final` 為參考（`max_history`），提升代名詞與接續一致性。

## 已知限制

- 需支援 WebAudio tabCapture 的 Chromium 瀏覽器；受 DRM 保護的內容（如 Netflix）分頁音可能被瀏覽器擋住無法擷取。
- `large-v3` 偶爾在純音樂/無語音時產生幻覺文字；有 `no_repeat`、`no_speech`、`log_probs` 過濾與重複文字丟棄機制，但非 100%。
- 只有單一 Clients 連線吃音（多分頁同時搶會踢掉前一個）。
- LLM 跑 CPU（3050 8GB 需保留給 Whisper）；若顯卡空間夠可改成 CUDA 版 llama wheel。
- 網頁介面僅本機可用（`127.0.0.1`/`localhost`），不會對外開放。

## 檔案

| 路徑 | 說明 |
|---|---|
| `live_translate.py` | 主程式（HTTP/WS 伺服器、重取樣、VAD、ASR、翻譯、輸出） |
| `config.json` | 各項設定 |
| `setup_env.bat` / `run_live.bat` | 環境建置 / 啟動 |
| `requirements.txt` / `requirements.lock.txt` | 依賴（寬鬆 / 鎖定版號） |
| `extension\` | Chrome MV3 擴充功能 |
| `models\` | faster-whisper 快取（junction）與 GGUF 模型 |
| `transcript\` | 輸出記錄檔 |
