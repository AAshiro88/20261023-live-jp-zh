#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""獨立的即時字幕小視窗（Python / tkinter），永遠在最上層。

取代 open_caption.bat 開的 Chrome app 視窗（僅取代「字幕顯示」，不取代來源分頁的 Chrome + 擴充）。
版面對齊 live_translate.py 的 `?mini=1`：黑底、無邊框、日語小字（.ja 灰小）+ 中文大字（.zh 白粗）、
partial 淡化 + （辨識中…）、置底、保留最近 N 句、視窗置頂。
定稿與 partial 放在同一個 Text 依序排列，避免兩個 widget 互相擠壓而重疊。
需先啟動 run_live.bat；字幕 WS：ws://127.0.0.1:8850/events。
不需額外安裝套件：tkinter（標準庫）+ aiohttp（專案已依賴）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import queue
import sys
import threading
import tkinter as tk

import aiohttp

DEFAULT_WS_URL = "ws://127.0.0.1:8850/events"
DEFAULT_FONT = "Microsoft JhengHei"  # Windows 繁中字型；其他平台 tkinter 會退回預設字型

BG = "#0e0e12"            # mini 黑底
JA_FG = "#9aa0a6"         # .ja 灰
ZH_FG = "#ffffff"         # .zh 白
PARTIAL_JA_FG = "#9aa0a6"  # partial .ja
PARTIAL_ZH_FG = "#bcbcc4"  # partial .zh（原 .partial opacity .55 的近似）
PARTIAL_PLACEHOLDER = "（辨識中…）"  # 原 pzh 無 zh 時文字

# 寬對齊 open_caption.bat：主螢幕 5/12；高縮小為主螢幕 1/4（原 1/3）；位置 30,30；偵測失敗退回 1920x1080
FALLBACK_SCREEN = (1920, 1080)
WINDOW_POSITION = 30
WIDTH_FRACTION_NUM = 5
WIDTH_FRACTION_DEN = 12
HEIGHT_FRACTION_DEN = 4

# asyncio 執行緒 -> tkinter 主執行緒的執行緒安全佇列
event_q: "queue.Queue[dict]" = queue.Queue()


async def listen(ws_url: str, q: "queue.Queue[dict]") -> None:
    """連線字幕伺服器 /events，將 JSON 事件放入佇列；斷線每 1 秒重連。"""
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.ws_connect(ws_url, heartbeat=30) as ws:
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                q.put(json.loads(msg.data))
                            except json.JSONDecodeError:
                                pass
            except asyncio.CancelledError:
                raise
            except Exception:
                # 伺服器未啟動、連線中斷等：稍後重連
                pass
            await asyncio.sleep(1.0)


def ws_thread_main(ws_url: str, q: "queue.Queue[dict]") -> None:
    """在獨立執行緒跑 asyncio 事件迴圈。"""
    try:
        asyncio.run(listen(ws_url, q))
    except asyncio.CancelledError:
        pass


def screen_size(root: tk.Tk) -> tuple[int, int]:
    """主螢幕像素尺寸；偵測失敗退回 FALLBACK_SCREEN。"""
    try:
        sw = int(root.winfo_screenwidth())
        sh = int(root.winfo_screenheight())
        if sw > 0 and sh > 0:
            return sw, sh
    except (tk.TclError, ValueError):
        pass
    return FALLBACK_SCREEN


def main() -> int:
    # Python 輸出 UTF-8；Windows 主控台編碼不符時仍盡量可讀（比照專案其他腳本；pythonw 下 sys.stdout 可能為 None）
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="live-jp-zh 獨立置頂字幕視窗（日語小字 + 中文大字）")
    ap.add_argument("--url", default=DEFAULT_WS_URL, help="字幕 WebSocket 網址")
    ap.add_argument("--max-lines", type=int, default=6, help="保留最近幾句定稿（原 MAX）")
    ap.add_argument("--width", type=int, default=None, help="視窗寬（預設主螢幕 5/12；對應 open_caption.bat width）")
    ap.add_argument("--height", type=int, default=None, help="視窗高（預設主螢幕 1/4；原 open_caption.bat 1/3；可覆寫）")
    ap.add_argument("--font-size", type=int, default=15, help="中文 .zh 字級（原 mini .zh 15；日文 .ja 10）")
    ap.add_argument("--font", default=DEFAULT_FONT)
    args = ap.parse_args()

    ja_size = max(args.font_size - 5, 9)  # 原 mini .ja 10 vs .zh 15
    zh_size = args.font_size

    root = tk.Tk()
    root.title("live-jp-zh")
    root.configure(bg=BG)

    # 寬對齊 open_caption.bat 5/12；高 1/4（縮小；原 1/3）；明確 --width/--height 覆寫
    sw, sh = screen_size(root)
    width = args.width if args.width is not None else sw * WIDTH_FRACTION_NUM // WIDTH_FRACTION_DEN
    height = args.height if args.height is not None else sh // HEIGHT_FRACTION_DEN
    root.geometry(f"{width}x{height}+{WINDOW_POSITION}+{WINDOW_POSITION}")
    root.minsize(120, 60)

    # 永遠在最上層
    root.attributes("-topmost", True)
    root.bind("<Escape>", lambda _e: root.destroy())
    root.bind("<q>", lambda _e: root.destroy())
    root.bind("<t>", lambda _e: root.attributes("-topmost", not root.attributes("-topmost")))

    # 定稿 + partial 都用同一個 Text，照順序排列；partial 永遠在最後（原 #partial order:999）
    text = tk.Text(
        root, wrap="word", bg=BG, fg=ZH_FG,
        font=(args.font, zh_size),
        # 關鍵：tk.Text 預設要求約 24 行；小視窗會把換行內容擠出可視區。
        # 設 1 行後由 expand 分配剩餘空間，最新句子才能留在底部。
        height=1,
        borderwidth=0, padx=10, pady=6,
        state="disabled", cursor="arrow", highlightthickness=0,
        insertbackground=ZH_FG, spacing1=1, spacing3=2,
    )
    text.pack(fill="both", expand=True)
    text.tag_config("ja", foreground=JA_FG, font=(args.font, ja_size))
    text.tag_config("zh", foreground=ZH_FG, font=(args.font, zh_size, "bold"))
    text.tag_config("pja", foreground=PARTIAL_JA_FG, font=(args.font, ja_size))
    text.tag_config("pzh", foreground=PARTIAL_ZH_FG, font=(args.font, zh_size, "bold"))

    finals: list[tuple[str, str]] = []  # (ja, zh)
    partial: tuple[str, str] | None = None  # (ja, zh)；zh 可能為 （辨識中…）

    def append_item(ja: str, zh: str, ja_tag: str, zh_tag: str, with_gap: bool) -> None:
        """寫入一則 entry；with_gap 時先留一行空白，避免蓋住上一句。"""
        if with_gap:
            text.insert("end", "\n\n")
        if ja:
            text.insert("end", ja + "\n", ja_tag)
        text.insert("end", zh, zh_tag)

    def redraw() -> None:
        text.config(state="normal")
        text.delete("1.0", "end")
        for i, (ja, zh) in enumerate(finals):
            append_item(ja, zh, "ja", "zh", with_gap=(i > 0))
        if partial is not None:
            pja, pzh = partial
            append_item(pja, pzh, "pja", "pzh", with_gap=bool(finals))
        text.config(state="disabled")
        text.see("end")  # 原 flex-end + overflow:hidden：永遠顯示最新底部

    def clear_partial() -> None:
        nonlocal partial
        partial = None
        redraw()

    def show_partial(ja: str, zh: str) -> None:
        nonlocal partial
        partial = (ja, zh)
        redraw()

    def add_final(ja: str, zh: str) -> None:
        nonlocal partial
        if not ja and not zh:
            return
        finals.append((ja, zh))
        del finals[:-args.max_lines]  # 只保留最近 max_lines 句
        partial = None
        redraw()

    def pump() -> None:
        try:
            while True:
                e = event_q.get_nowait()
                stage = e.get("stage")
                if stage == "final_zh":
                    add_final(str(e.get("ja") or ""), str(e.get("zh") or ""))
                elif stage == "partial":
                    # 原 pja=e.ja；pzh = e.zh ? e.zh : （辨識中…）
                    zh = e.get("zh")
                    show_partial(str(e.get("ja") or ""), str(zh) if zh else PARTIAL_PLACEHOLDER)
        except queue.Empty:
            pass
        except tk.TclError:
            pass
        finally:
            root.after(50, pump)  # 確保 pump 不會因單次錯誤停止

    threading.Thread(target=ws_thread_main, args=(args.url, event_q), daemon=True).start()
    root.after(50, pump)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
