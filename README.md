# live-jp-zh

瀏覽器分頁音訊即時字幕：日文 ASR + 本機 LLM 翻譯成繁體中文（台灣用語）。

- 音訊來源：Chrome 擴充功能擷取**指定分頁**（非全系統音），WebSocket 送本機伺服器。
- 全程離線：faster-whisper（`large-v3` 或 KotobaWhisper v2.0-faster，GPU/CUDA）+ llama.cpp `Qwen3-4B-Instruct-2507` Q4（CPU）。
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
  ├─ faster-whisper（large-v3 或 KotobaWhisper v2.0-faster，language=ja，CUDA）
  ├─ llama.cpp Qwen3-4B Q4_K_M（CPU，日本語→繁體中文）
  ├─ OpenCC s2twp
  └─ 輸出：console、transcript log、
       http://127.0.0.1:8850/        字幕網頁
       http://127.0.0.1:8850/?mini=1 迷你視窗（黑底小字，適合 app 視窗）
       http://127.0.0.1:8850/?obs=1  OBS 透明 overlay 版
```

資料流：`final_ja` → 翻譯 → `final_zh`；部分轉錄（partial）先行顯示並送翻譯（積壓超過 `translate.skip_if_pending` 時跳過，只等最終版），避免排隊堆積。有定稿（final）在排隊時，尚未開始的 partial 會直接讓位，不佔用 ASR 與翻譯。

## 安裝

1. conda 環境（只需一次；會建 `live-jp-zh` 環境、下載模型與依賴）：

   ```
   setup_env.bat
   ```

   - Whisper 模型 `large-v3` 若本機已有快取會以 junction 連結，不重複下載。
   - KotobaWhisper v2.0-faster 位於 `models\faster-whisper\kotoba-whisper`。
   - GGUF 下載到 `models\Qwen3-4B-Instruct-2507-Q4_K_M.gguf`。

2. Chrome 擴充功能（只需一次）：

   - 開 `chrome://extensions` → 右上「開發人員模式」→「載入未封裝項目」→ 選 `D:\claudeFile\20261023-live-jp-zh\extension`。
   - 之後若改過 `extension\` 內的檔案，要在 `chrome://extensions` 該擴充功能按「重新載入」，並重新點圖示開始擷取。

## 使用

1. 雙擊 `run_live.bat`。實際流程：`run_live.bat` 定位 conda 環境後叫用 `run_live.py`，先顯示 ASR 模型選單：

   ```
   ================================
     即時日->中字幕  ASR 模型選擇
   ================================
   [1] faster-whisper large-v3（通用模型，約 3GB）
   [2] KotobaWhisper v2.0-faster（日語優化，約 1.5GB）
   [3] KotobaWhisper + LLM 全掛 GPU（需 CUDA 版 llama wheel）
   [4] 歌曲模式：KotobaWhisper + 人聲分離去 BGM
   ================================
   請輸入選項 (1-4，預設 3):
   ```

   - 輸入 `1`：使用 `config.large-v3.json`（large-v3，LLM 跑 CPU）
   - 輸入 `2`：使用 `config.json`（KotobaWhisper，LLM 跑 CPU）
   - 輸入 `3` 或直接按 Enter：使用 `config.all-gpu.json`（KotobaWhisper + LLM 全部上 GPU，需先換 CUDA 版 llama wheel，見〈全掛 GPU〉）
   - 輸入 `4`：使用 `config.song.json`（歌曲模式，見〈歌曲模式（唱歌/背景音樂）〉）
   - 之後等同 `python live_translate.py --config <所選設定>`，額外參數會一併傳遞
     （例如在命令列執行 `run_live.bat --wav 檔案.wav`）
   - 啟動失敗時視窗會保留，可看到錯誤訊息
   - 伺服器啟動在 `http://127.0.0.1:8850/`，ASR 與 LLM 載入約 3–10 秒。
2. 開字幕網頁 `http://127.0.0.1:8850/`。
   - 只想看字幕：雙擊 `open_caption.bat`，開一個獨立迷你視窗
     （預設：螢幕 5/12 寬 x 1/3 高；即 `http://127.0.0.1:8850/?mini=1`），
     可自行關掉大網頁；`open_caption.bat 600 400` 可自訂視窗大小。
3. 到要翻譯的日文分頁（例如 YouTube 直播），點 Chrome 工具列擴充圖示
   **「Live JP-ZH Captions」**開始擷取該分頁。狀態一眼可辨：
   **灰圈**＝待命中、**綠圈＋紅色 `REC` 徽章**＝擷取中（游標停上去顯示
   「擷取中：<分頁標題>」，再點一次即停止）；**紅色 `ERR` 徽章**＝失敗或中斷
   （游標停上去顯示原因）。分頁關閉或串流被瀏覽器停止時，會回灰圈並顯示 `ERR`。
4. 字幕同時寫進 `transcript\<日期時間>.log`（含日文原文與中文譯文）。

OBS：加「瀏覽器」來源，網址 `http://127.0.0.1:8850/?obs=1`，勾「重新整理時暫停 CSS」不必要，直接用即可（背景透明）。

## CLI 測試（不需要瀏覽器）

```
python live_translate.py --wav 檔案.wav            # 實時節奏模擬串流
python live_translate.py --wav 檔案.wav --no-realtime  # 全速餵（量測箱）
python live_translate.py --config config.large-v3.json --wav 檔案.wav  # 指定 ASR 設定
```

不帶 `--config` 時預設讀 `config.json`（KotobaWhisper）。

實測（20.8 秒日文合成語音、三句，RTX 3050 8GB + i5 CPU，large-v3）：

| 項目 | 數值 |
|---|---|
| ASR 定案延遲（發話結束→日文定稿） | 0.7–1.0s |
| ASR RTF（語音時長/解碼時長） | 0.16–0.35 |
| LLM 翻譯每句 | 0.5–1.3s |
| 發話結束→中文定稿總延遲 | 約 1.3–2.3s |
| 部分字幕出現延遲 | 約 0.7–1.5s（邊說邊出） |
| 記憶體（VRAM） | whisper large-v3 約 3.5GB |

## 設定（config.json / config.large-v3.json / config.all-gpu.json / config.song.json）

四份設定檔的差異只在 ASR 模型三項（`asr.model`、`asr.whisper_model_path`、`asr.temperature`）、`translate.n_gpu_layers`、歌曲模式的 `separation` 區塊與少數 ASR/VAD 值；其他鍵相同。
`config.all-gpu.json` 為預設與選單第 3 項（KotobaWhisper + `translate.n_gpu_layers: 99`＝全部層上 GPU）；`config.json` 為選單第 2 項（KotobaWhisper，LLM 跑 CPU）；`config.large-v3.json` 為選單第 1 項（large-v3，`temperature` 為 `[0.0, 0.4, 0.8]`）；`config.song.json` 為選單第 4 項（歌曲模式）。

| 鍵 | 說明 |
|---|---|
| `server.port` / `host` | HTTP/WS 埠（預設 8850），host 僅允許 127.0.0.1/localhost |
| `asr.whisper_model_path` | 非空＝直接載入此本地 faster-whisper 目錄（`config.json` 預設 `models/faster-whisper/kotoba-whisper`）；空＝改用 `asr.model` 走快取 |
| `asr.model` | `whisper_model_path` 為空時使用的模型名（`config.large-v3.json` 為 `large-v3`；可改 `large-v3-turbo`，更快、品質略低） |
| `asr.device` / `compute_type` | 預設 `cuda`/`auto`；CPU 環境改 `cpu`/`int8` |
| `asr.beam_size` | 預設 3（越高越準但越慢；想更準可改回 5） |
| `asr.without_timestamps` | 預設 `true`：字幕不需要時間戳，跳過時間戳解碼可省時間 |
| `vad.min_silence_duration_ms` | 切句靜音門檻（預設 400；越低定稿越快，但太低的停頓會被切成兩句） |
| `vad.min_speech_duration_ms` | 最短語音段（預設 300） |
| `vad.max_utterance_seconds` | 單句最長（預設 12.0；一直不換氣時的最長等待） |
| `vad.preroll_seconds` | 語音開始前保留的音訊（預設 0.3） |
| `vad.partial_every_seconds` | 說話期間每隔幾秒解一次 partial（預設 0.8；越小越快看到字，但吃更多 GPU） |
| `translate.gguf_path` | 翻譯 GGUF 路徑（目前 `models\Qwen3-4B-Instruct-2507-Q4_K_M.gguf`） |
| `translate.n_threads` | llama.cpp CPU 執行緒數（預設 `-1`＝全部核心） |
| `translate.n_gpu_layers` | 載入到 GPU 的層數（`0`＝純 CPU；`config.all-gpu.json` 為 `99`＝全部層上 GPU，需 CUDA 版 llama wheel） |
| `translate.temperature` | 預設 0.15 |
| `translate.max_tokens` | 定稿翻譯長度上限（預設 200） |
| `translate.partial_max_tokens` | partial 翻譯長度上限（預設 96；partial 會被定稿取代，不需太長） |
| `translate.history_turns` | 附帶最近 N 條 `final` 給 LLM 參考（預設 2；越多越慢） |
| `translate.skip_if_pending` | 積壓 partial 翻譯上限（預設 3） |
| `separation.enabled` | 歌曲模式：定稿前做人聲分離（預設關閉，`config.song.json` 開啟） |
| `separation.model_path` / `model_url` / `model_sha256` | 分離模型（UVR-MDX-NET-Inst_HQ_3）路徑、下載網址與驗證雜湊 |
| `separation.device` | `cpu`（預設；定稿多等約 1–3s）或 `cuda`（需 onnxruntime-gpu） |
| `separation.overlap` / `compensate` | 分離推論的視窗重疊（預設 0.25）與伴奏抵消量（預設 1.022） |

LLM 對話可帶最近 `translate.history_turns` 條 `final` 為參考，提升代名詞與接續一致性。

### 延遲調整

延遲主要來自四段：擴充功能送音訊的區塊（`extension\offscreen.js` 的 `CHUNK_SEND`，預設 2048 samples ≈ 43ms）、VAD 切句靜音門檻、ASR 解碼（`beam_size`）、LLM 翻譯（CPU）。

- 想讓「開始說話→出現第一行字」更快：調低 `vad.partial_every_seconds`。
- 想讓「說完→定稿」更快：調低 `vad.min_silence_duration_ms`（太低會把停頓切成兩句）或 `asr.beam_size`。
- 有 final 在排隊時，排隊中的 partial 會直接讓位，不會卡住定稿。

## 歌曲模式（唱歌/背景音樂）

Whisper 是用「說話」訓練的，遇到唱歌 + 背景音樂辨識會明顯變差。歌曲模式在**定稿前**先做一次人聲分離（MDX-Net ONNX），只把人聲餵給 ASR：

- partial（即時預覽）不分離，維持原本延遲；VAD 切句後才分離再定稿，所以「定稿」會比對話模式多等約 1–3 秒（CPU 上分離實測約 0.4× 即時速度，10 秒的句子約多 4 秒內）。
- `drop_repeated_segments` 在歌曲模式預設**關閉**：副歌重複的歌詞不會被當成幻覺丟掉。
- `beam_size` 回 5（歌曲較吃準度）、`max_utterance_seconds` 15、切句靜音門檻 500ms。
- 分離模型首次使用會自動下載到 `models\separation\`（約 67MB，SHA256 已驗證），之後離線可用。
- `separation.device` 可改 `cuda`（需 onnxruntime-gpu；目前的 onnxruntime 是 CPU 版，faster-whisper 依賴它，先不要動）。
- 想提高特定歌曲的準度，可在 `asr.hotwords` 填入歌名或已知歌詞詞組。
- CLI 測試：`python live_translate.py --config config.song.json --wav 歌曲.mp3`

已知限制：分離不是银弹，編曲很滿或人聲被伴奏蓋住時仍會漏字；對純說話內容沒有壞處但也不需要。

## 全掛 GPU（選用）

`setup_env.bat` 安裝的是 CPU 版 llama.cpp（`whl/cpu`），這種 build 會忽略 `translate.n_gpu_layers`。要讓 LLM 上 GPU，換成對應 CUDA 版本的 wheel（以本機 RTX 3050 / CUDA 12.4 build 為例）：

```
C:\Users\ayore\.conda\envs\live-jp-zh\Scripts\pip.exe install ^
  https://github.com/abetlen/llama-cpp-python/releases/download/v0.3.36-cu124/llama_cpp_python-0.3.36-py3-none-win_amd64.whl
```

其他 CUDA 版本可從 <https://abetlen.github.io/llama-cpp-python/whl/cu124/llama-cpp-python/> 之類的索引挑選。**重新執行 `setup_env.bat` 會把 wheel 換回 CPU 版**，需再執行上面指令。

1. 啟動時選 `3`（`config.all-gpu.json`），或手動指定：

   ```
   python live_translate.py --config config.all-gpu.json
   ```

2. VRAM：KotobaWhisper 約 1.5GB ＋ Qwen3-4B Q4 全部層約 3.5GB，實測含桌面佔用約 **5.8GB / 8GB**。large-v3 加上 LLM 約 **7.5GB**，8GB 很緊，容易 OOM。
3. 若出現 OOM：把 `config.all-gpu.json` 的 `translate.n_gpu_layers` 調低（例如 20），或改跑 `large-v3-turbo`。
4. 驗證：跑起來後看 `nvidia-smi`（VRAM 應明顯上升），以及 `transcript\<日期時間>.log` 的 `(LLM x.xs)` 是否變小。

## 已知限制

- 需支援 WebAudio tabCapture 的 Chromium 瀏覽器；受 DRM 保護的內容（如 Netflix）分頁音可能被瀏覽器擋住無法擷取。
- Whisper 偶爾在純音樂/無語音時產生幻覺文字；有 `no_speech`、`log_probs`、`compression_ratio` 過濾與重複文字丟棄機制，但非 100%。
- 只有單一 Clients 連線吃音（多分頁同時搶會踢掉前一個）。
- 選單預設第 3 項會把 LLM 放上 GPU，但需 CUDA 版 llama wheel；若仍是 CPU 版，llama.cpp 會忽略 `translate.n_gpu_layers` 退回 CPU（不會報錯）。
- 網頁介面僅本機可用（`127.0.0.1`/`localhost`），不會對外開放。

## 檔案

| 路徑 | 說明 |
|---|---|
| `live_translate.py` | 主程式（HTTP/WS 伺服器、重取樣、VAD、ASR、翻譯、輸出） |
| `run_live.bat` | 啟動捷徑（純英文）；定位 conda 環境後叫用 `run_live.py` |
| `run_live.py` | ASR 模型選單；依選擇帶 `--config` 啟動 `live_translate.py`（額外參數會傳遞） |
| `config.json` | KotobaWhisper 設定（選單第 2 項，LLM 跑 CPU） |
| `config.large-v3.json` | large-v3 設定（選單第 1 項） |
| `config.all-gpu.json` | KotobaWhisper + LLM 全掛 GPU（選單第 3 項） |
| `config.song.json` | 歌曲模式：人聲分離 + 副歌不丟棄（選單第 4 項） |
| `vocal_sep.py` | MDX-Net（UVR）ONNX 人聲分離模組（numpy + onnxruntime，不依賴 torch） |
| `models\separation\` | 分離模型（首次使用自動下載） |
| `setup_env.bat` | 環境建置 |
| `open_caption.bat` | 開獨立迷你字幕視窗 |
| `requirements.txt` / `requirements.lock.txt` | 依賴（寬鬆 / 鎖定版號） |
| `extension\` | Chrome MV3 擴充功能 |
| `models\` | faster-whisper 快取（junction）、`faster-whisper\kotoba-whisper`、GGUF 模型 |
| `transcript\` | 輸出記錄檔 |
