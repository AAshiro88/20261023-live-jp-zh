#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""獨立的即時字幕小視窗（Python / tkinter），永遠在最上層。

取代 open_caption.bat 開的 Chrome app 視窗（僅取代「字幕顯示」，不取代來源分頁的 Chrome + 擴充）。
版面對齊 live_translate.py 的 `?mini=1`：黑底、無邊框、日語小字（.ja 灰小）+ 中文大字（.zh 白粗）、
partial 淡化（opacity .55 → 以較暗前景近似）+ （辨識中…）、保留最近 N 句、置底 partial、視窗置頂。
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
    ap.add_argument("--max-lines", type=int, default=6, help="保留最近幾句定稿（原 MAX；mini 無 header 時可對應 output.max_lines_on_page）")
    ap.add_argument("--width", type=int, default=None, help="視窗寬（預設主螢幕 5/12；對應 open_caption.bat width）")
    ap.add_argument("--height", type=int, default=None, help="視窗高（預設主螢幕 1/4；原 open_caption.bat 1/3；可覆寫）")
    ap.add_argument("--font-size", type=int, default=15, help="中文 .zh 字級（原 mini .zh 15；日文 .ja 取 font-size-5，最小 9；原 .ja 10）")
    ap.add_argument("--font", default=DEFAULT_FONT)
    args = ap.parse_args()

    ja_size = max(args.font_size - 5, 9)  # 原 mini .ja 10 vs .zh 15；此處依比例縮
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

    wrap_w = max(width - 20, 20)

    # 定稿區：.ja（日語小字灰）+ .zh（中文大字白粗），置底顯示
    text = tk.Text(
        root, wrap="word", bg=BG, fg=ZH_FG,
        font=(args.font, zh_size),
        # 關鍵：tk.Text 預設要求約 24 行；小視窗會把底下 partial 擠到看不到。
        # 設 1 行後由 expand 分配剩餘空間，partial 才能固定在底部。
        height=1,
        borderwidth=0, padx=10, pady=6,
        state="disabled", cursor="arrow", highlightthickness=0,
        insertbackground=ZH_FG, spacing1=1, spacing3=2,
    )
    text.pack(fill="both", expand=True, side="top")
    text.tag_config("ja", foreground=JA_FG, font=(args.font, ja_size))
    text.tag_config("zh", foreground=ZH_FG, font=(args.font, zh_size, "bold"))

    # partial 區：order:999 置底、淡化；.ja + .zh 兩行；無 zh 時（辨識中…）；無 partial 時隱藏（原 pb display:none）
    partial_frame = tk.Frame(root, bg=BG)
    partial_ja = tk.Label(
        partial_frame, text="", bg=BG, fg=PARTIAL_JA_FG,
        font=(args.font, ja_size), anchor="w", justify="left", padx=10, pady=0, wraplength=wrap_w,
    )
    partial_ja.pack(fill="x")
    partial_zh = tk.Label(
        partial_frame, text="", bg=BG, fg=PARTIAL_ZH_FG,
        font=(args.font, zh_size, "bold"), anchor="w", justify="left", padx=10, pady=4, wraplength=wrap_w,
    )
    partial_zh.pack(fill="x")
    # 預設隱藏；收到 partial 才 pack（原 pb.style.display='none' -> ''）

    finals: list[tuple[str, str]] = []  # (ja, zh)

    def redraw_finals() -> None:
        text.config(state="normal")
        text.delete("1.0", "end")
        for i, (ja, zh) in enumerate(finals):
            if i:
                text.insert("end", "\n\n")  # .entry 間距（gap/padding 近似）
            if ja:
                text.insert("end", ja + "\n", "ja")  # 日語小字
            text.insert("end", zh, "zh")             # 中文大字
        text.config(state="disabled")
        text.see("end")  # justify-content:flex-end + overflow:hidden → 顯示最新

    def clear_partial() -> None:
        partial_ja.config(text="")
        partial_zh.config(text="")
        partial_frame.pack_forget()  # 原 pb.style.display='none'；clearPartial

    def show_partial(ja: str, zh: str) -> None:
        partial_ja.config(text=ja)
        partial_zh.config(text=zh)
        if not partial_frame.winfo_ismapped():
            partial_frame.pack(fill="x", side="bottom")  # order:999 置底；原 pb.style.display=''；已 pack 不重複

    def add_final(ja: str, zh: str) -> None:
        if not ja and not zh:
            return
        finals.append((ja, zh))
        del finals[:-args.max_lines]  # 只保留最近 max_lines 句
        redraw_finals()
        clear_partial()

    def pump() -> None:
        try:
            while True:
                e = event_q.get_nowait()
                stage = e.get("stage")
                if stage == "final_zh":
                    add_final(str(e.get("ja") or ""), str(e.get("zh") or ""))  # final_zh 含 ja + zh
                elif stage == "partial":
                    # 原 pja=e.ja；pzh = e.zh ? e.zh : （辨識中…）；ASR partial 可能只有 ja、translator partial 含 zh
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
