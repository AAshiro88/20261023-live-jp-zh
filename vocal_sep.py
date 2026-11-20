#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""vocal_sep: MDX-Net（UVR）ONNX 人聲分離。

用途：唱歌/背景音樂場景，把 VAD 切出的短音訊分離成人聲再餵 ASR 定稿。
實作對齊 UVR / python-audio-separator 的 MDX 推論流程（STFT 4 通道排列、
chunk 重疊加窗、前 3 個頻段歸零、compensate 抵消），只用 numpy + onnxruntime，
不引入 torch。

模型：UVR-MDX-NET-Inst_HQ_3（預測「伴奏」stem，人聲 = 原音 - 伴奏 × compensate）。
"""
from __future__ import annotations

import hashlib
import urllib.request
from pathlib import Path

import numpy as np

MODEL_SR = 44100      # MDX-Net 訓練取樣率
N_FFT = 6144
HOP = 1024
DIM_F = 3072          # 由 ONNX input shape (batch, 4, 3072, 256) 確認
DIM_T = 256
TRIM = N_FFT // 2
CHUNK = HOP * (DIM_T - 1)   # 261120 samples ≈ 5.92s @ 44.1kHz
PRIMARY_STEM = "Instrumental"


class VocalSeparator:
    """載入 MDX-Net ONNX，把 16kHz mono 音訊分離出人聲。"""

    def __init__(self, model_path: str, device: str = "cpu", overlap: float = 0.25,
                 compensate: float = 1.022, console=None):
        import onnxruntime as ort

        self.console = console
        self.overlap = float(np.clip(overlap, 0.0, 0.9))
        self.compensate = float(compensate)
        self.model_path = Path(model_path)

        providers = self._pick_providers(ort, device)
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        opts.inter_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(self.model_path), providers=providers, sess_options=opts)
        # 檢查 input shape 與預期一致（4, 3072, 256），不一致就報錯提示
        shape = self.session.get_inputs()[0].shape
        if len(shape) != 4 or shape[2] != DIM_F or shape[3] != DIM_T:
            raise ValueError(
                f"模型 input shape {shape} 與預期 (b, 4, {DIM_F}, {DIM_T}) 不符，"
                "請確認模型是否為 UVR MDX-Net 系列")
        self.window = np.hanning(N_FFT + 1)[:-1].astype(np.float32)  # periodic hann

    def _pick_providers(self, ort, device: str) -> list:
        avail = ort.get_available_providers()
        if device == "cuda" or device == "auto":
            if "CUDAExecutionProvider" in avail:
                return ["CUDAExecutionProvider", "CPUExecutionProvider"]
            if device == "cuda":
                self._log("[SEP] 此 onnxruntime 不支援 CUDA，退回 CPU")
        return ["CPUExecutionProvider"]

    def _log(self, msg: str):
        if self.console is not None:
            self.console.write(msg, to_file=False)
        else:
            print(msg, flush=True)

    @staticmethod
    def ensure_model(path: str, url: str, sha256: str, console=None) -> str:
        """模型檔不存在時下載並驗證 SHA256。"""
        p = Path(path)
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            msg = f"[SEP] 下載分離模型 {p.name} …"
            (console.write(msg, to_file=False) if console else print(msg, flush=True))
            tmp = p.with_suffix(".part")
            urllib.request.urlretrieve(url, tmp)
            digest = hashlib.sha256(tmp.read_bytes()).hexdigest()
            if digest != sha256:
                tmp.unlink(missing_ok=True)
                raise RuntimeError(f"分離模型 SHA256 驗證失敗: {digest}")
            tmp.replace(p)
        return str(p)

    # ------------------------------------------------------------------ #
    # STFT / ISTFT（numpy 版，對齊 torch.stft center=True + periodic hann）
    # ------------------------------------------------------------------ #

    def _stft4(self, x2: np.ndarray) -> np.ndarray:
        """(2, N) -> (4, DIM_F, T')，通道 [L.re, L.im, R.re, R.im]。"""
        n = x2.shape[1]
        pad = np.pad(x2, ((0, 0), (TRIM, TRIM)))
        frames = 1 + n // HOP
        idx = np.arange(N_FFT)[None, :] + HOP * np.arange(frames)[:, None]
        seg = pad[:, idx] * self.window  # (2, T', N_FFT)
        spec = np.fft.rfft(seg, axis=-1)  # (2, T', n_bins)
        arr = np.stack([spec[0].real, spec[0].imag,
                        spec[1].real, spec[1].imag]).astype(np.float32)
        return arr.transpose(0, 2, 1)[:, :DIM_F, :]  # (4, DIM_F, T')

    def _istft(self, spec4: np.ndarray, length: int) -> np.ndarray:
        """(4, DIM_F, T') -> (2, length)；重疊相加並以窗平方和歸一。"""
        bins = N_FFT // 2 + 1
        spec = np.concatenate(
            [spec4, np.zeros((4, bins - DIM_F, spec4.shape[-1]), np.float32)], axis=1)
        comp = (spec[0::2] + 1j * spec[1::2]).astype(np.complex64)  # (2, bins, T')
        frames = np.fft.irfft(comp, n=N_FFT, axis=-2).transpose(0, 2, 1)  # (2, T', N_FFT)
        w = self.window
        t, n = frames.shape[1], length
        out = np.zeros((2, n + 2 * TRIM), np.float32)
        norm = np.zeros(n + 2 * TRIM, np.float32)
        w2 = (w * w).astype(np.float32)
        for i in range(t):
            s = i * HOP
            out[:, s:s + N_FFT] += frames[:, i, :] * w
            norm[s:s + N_FFT] += w2
        norm = np.maximum(norm, 1e-8)
        out /= norm
        # center=True：去掉頭尾 padding
        out = out[:, TRIM:TRIM + (t - 1) * HOP]
        return out[:, :length]

    # ------------------------------------------------------------------ #
    # 分離主流程
    # ------------------------------------------------------------------ #

    def _demix(self, mix: np.ndarray) -> np.ndarray:
        """(2, N) 44.1kHz -> 主 stem（伴奏）(2, N)。"""
        from scipy import signal as sig

        step = int((1 - self.overlap) * CHUNK)
        pad = CHUNK - 2 * TRIM - (mix.shape[1] % (CHUNK - 2 * TRIM))
        mixture = np.concatenate(
            (np.zeros((2, TRIM), np.float32), mix,
             np.zeros((2, max(pad, 0)), np.float32)), 1)

        result = np.zeros((2, mixture.shape[1]), np.float32)
        divider = np.zeros((2, mixture.shape[1]), np.float32)
        for start in range(0, mixture.shape[1], step):
            end = min(start + CHUNK, mixture.shape[1])
            part = mixture[:, start:end]
            if part.shape[1] < CHUNK:
                part = np.concatenate(
                    (part, np.zeros((2, CHUNK - part.shape[1]), np.float32)), 1)
            spek = self._stft4(part)
            spek[:, :3, :] = 0  # UVR：前 3 個頻段歸零
            out = self.session.run(None, {"input": spek[None, :]})[0][0]
            tar = self._istft(out, CHUNK)  # (2, CHUNK)
            win = np.hanning(end - start).astype(np.float32)[None, :]
            result[:, start:end] += tar[:, : end - start] * win
            divider[:, start:end] += win
        result /= np.maximum(divider, 1e-8)
        return result[:, TRIM:TRIM + mix.shape[1]]

    def separate(self, pcm: np.ndarray) -> np.ndarray:
        """16kHz mono float32（混合音）-> 16kHz mono float32（人聲）。"""
        from scipy import signal as sig

        if pcm.size == 0:
            return pcm
        x44 = sig.resample_poly(pcm, 441, 160).astype(np.float32)
        mix = np.stack([x44, x44])
        peak = float(np.abs(mix).max())
        if peak > 1e-6:
            mix = mix / peak
        inst = self._demix(mix)
        voc = mix - inst * self.compensate
        voc = voc[0] * peak  # 兩聲道相同，取其一
        y16 = sig.resample_poly(voc, 160, 441).astype(np.float32)
        return np.clip(y16, -1.0, 1.0)
