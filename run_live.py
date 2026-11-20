#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""run_live.py: 即時日->中字幕  ASR 模型選擇（Python 處理中文互動，bat 僅呼叫此腳本）"""
import os
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_CHOICES = {
    "1": ("config.large-v3.json", "faster-whisper large-v3（通用模型，約 3GB）"),
    "2": ("config.json",          "KotobaWhisper v2.0-faster（日語優化，約 1.5GB）"),
    "3": ("config.all-gpu.json",  "KotobaWhisper + LLM 全掛 GPU（需 CUDA 版 llama wheel）"),
}
DEFAULT_CHOICE = "3"


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
    except Exception:
        pass

    print("=" * 34)
    print("  即時日->中字幕  ASR 模型選擇")
    print("=" * 34)
    for key, (_cfg, desc) in CONFIG_CHOICES.items():
        print(f"[{key}] {desc}")
    print("=" * 34)

    # 非互動環境（stdin 關閉或導向）時 input() 會丟 EOFError，視為直接採用預設值
    while True:
        try:
            choice = input(f"請輸入選項 (1-3，預設 {DEFAULT_CHOICE}): ").strip()
        except EOFError:
            choice = ""
        if not choice:
            choice = DEFAULT_CHOICE
            break
        if choice in CONFIG_CHOICES:
            break
        print(f"  無效選項：{choice}，請重新輸入")

    config_file, desc = CONFIG_CHOICES[choice]
    config_path = os.path.join(BASE_DIR, config_file)
    if not os.path.isfile(config_path):
        print(f"[ERROR] 找不到設定檔: {config_path}")
        return 1

    print(f"\n已選擇: {desc}")
    print(f"設定檔: {config_file}")
    print("啟動伺服器...\n")

    # 由 bat 呼叫時 sys.executable 就是 conda 環境的 python，不再寫死路徑；使用絕對路徑
    cmd = [sys.executable, os.path.join(BASE_DIR, "live_translate.py"), "--config", config_path]
    # 額外命令列參數傳遞給 live_translate.py（例如 --wav 檔案.wav）
    cmd.extend(sys.argv[1:])

    try:
        result = subprocess.run(cmd, cwd=BASE_DIR)
        rc = result.returncode
    except KeyboardInterrupt:
        print("\n[INFO] 已由使用者中斷")
        rc = 130
    except Exception as e:
        print(f"\n[ERROR] 啟動失敗: {e}")
        rc = 1

    if rc != 0:
        print(f"\n[ERROR] live_translate.py 結束碼 {rc}")
    else:
        # 雙擊啟動時 press Enter 後關閉視窗；stdin 非互動時忽略
        try:
            input("\n按 Enter 關閉視窗...")
        except EOFError:
            pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
