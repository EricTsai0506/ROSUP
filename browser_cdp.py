"""
browser_cdp.py — 透過 Chrome DevTools Protocol（CDP）在背景控制 Edge

程式直接向瀏覽器要畫面、把按鍵和點擊送進網頁，不經過真實的鍵盤滑鼠，
所以 Edge 被其他視窗蓋住也能照常運作，你可以同時使用其他程式。
（不要把 Edge 縮到最小，縮小後瀏覽器會停止繪製畫面）
"""
import base64
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

import cv2
import numpy as np

EDGE_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
]

# 讓瀏覽器被蓋住、在背景時也照常繪製與執行遊戲
BACKGROUND_FLAGS = [
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
    "--disable-features=CalculateNativeWinOcclusion",
    "--no-first-run",
    "--no-default-browser-check",
]

_SPECIAL = {
    "space": (" ", "Space", 32), "enter": ("Enter", "Enter", 13),
    "esc": ("Escape", "Escape", 27), "escape": ("Escape", "Escape", 27),
    "tab": ("Tab", "Tab", 9), "up": ("ArrowUp", "ArrowUp", 38),
    "down": ("ArrowDown", "ArrowDown", 40), "left": ("ArrowLeft", "ArrowLeft", 37),
    "right": ("ArrowRight", "ArrowRight", 39), "shift": ("Shift", "ShiftLeft", 16),
}


def key_info(k):
    k = k.lower()
    if k in _SPECIAL:
        return _SPECIAL[k]
    if len(k) == 1 and k.isalpha():
        return (k, "Key" + k.upper(), ord(k.upper()))
    if len(k) == 1 and k.isdigit():
        return (k, "Digit" + k, ord(k))
    if k.startswith("f") and k[1:].isdigit():
        n = int(k[1:])
        return (k.upper(), k.upper(), 111 + n)
    return (k, k, 0)


def find_edge(custom=""):
    for p in ([custom] if custom else []) + EDGE_PATHS:
        if p and os.path.exists(p):
            return p
    return None


def _http_json(port, path, timeout=2.0):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def port_alive(port):
    try:
        _http_json(port, "/json/version", 1.0)
        return True
    except Exception:
        return False


def launch_browser(exe, url, port, profile_dir, size=(1280, 960), log=print):
    """開一個專門給遊戲用的 Edge 視窗（獨立設定檔、沒有網址列）"""
    if port_alive(port):
        log("遊戲用的 Edge 已經開著，直接連線")
        return True
    if not exe:
        log("找不到 Edge（msedge.exe），請在設定裡填入 Edge 的路徑")
        return False
    os.makedirs(profile_dir, exist_ok=True)
    args = [exe, f"--remote-debugging-port={port}", f"--user-data-dir={profile_dir}",
            f"--window-size={size[0]},{size[1]}", f"--app={url}"] + BACKGROUND_FLAGS
    kw = {}
    if sys.platform == "win32":
        kw["creationflags"] = 0x00000008  # DETACHED_PROCESS：關掉程式不會連帶關掉 Edge
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
    log("開啟遊戲用的 Edge…")
    for _ in range(60):
        if port_alive(port):
            return True
        time.sleep(0.5)
    log("Edge 沒有回應遠端控制，請關掉這個 Edge 視窗後再試一次")
    return False


def open_settings(exe, profile_dir, page="edge://settings/system"):
    """在遊戲專用的 Edge 開設定頁（關掉效率模式用）"""
    if not exe:
        return False
    kw = {"creationflags": 0x00000008} if sys.platform == "win32" else {}
    subprocess.Popen([exe, f"--user-data-dir={profile_dir}", page],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
    return True


class CdpPage:
    """連到一個分頁：截圖、送按鍵、送滑鼠"""

    def __init__(self, port, url_hint="", log=print):
        import websocket  # websocket-client
        self.log = log
        self.port = port
        pages = [t for t in _http_json(port, "/json/list") if t.get("type") == "page"]
        if not pages:
            raise RuntimeError("遊戲用的 Edge 裡沒有任何分頁")
        hint = (url_hint or "").lower()
        page = next((p for p in pages if hint and hint in p.get("url", "").lower()), pages[0])
        self.title = page.get("title", "")
        self.ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=10,
                                              suppress_origin=True)
        self.ws.settimeout(None)
        self._id = 0
        self._lock = threading.Lock()
        self._pending = {}
        self._closed = False
        threading.Thread(target=self._reader, daemon=True).start()
        self.dpr = 1.0          # 畫面像素 ÷ 網頁座標；第一張畫面進來時計算，視窗大小改變就重算
        self._scale_w = 0
        self.frame = None
        self.frame_t = 0.0
        self.capture_paused = False   # 暫停／休息時不截圖，省資源
        self.frame_interval = 0.1     # 每秒約 10 張
        self._fstop = False
        self._frame_lock = threading.Lock()
        threading.Thread(target=self._frame_loop, daemon=True).start()
        self.mouse = (0.0, 0.0)

    # ---------------- 連線 ----------------
    def _reader(self):
        while not self._closed:
            try:
                msg = json.loads(self.ws.recv())
            except Exception:
                self._closed = True
                for ev in list(self._pending.values()):
                    ev["done"].set()
                break
            mid = msg.get("id")
            if mid is not None and mid in self._pending:
                slot = self._pending[mid]
                slot["result"] = msg
                slot["done"].set()

    def send(self, method, params=None, timeout=10.0):
        if self._closed:
            raise RuntimeError("和 Edge 的連線中斷了（遊戲用的 Edge 被關掉？）")
        with self._lock:
            self._id += 1
            mid = self._id
            slot = {"done": threading.Event(), "result": None}
            self._pending[mid] = slot
            self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        ok = slot["done"].wait(timeout)
        self._pending.pop(mid, None)
        if not ok or slot["result"] is None:
            raise RuntimeError(f"Edge 沒有回應（{method}）")
        if "error" in slot["result"]:
            raise RuntimeError(f"{method}：{slot['result']['error'].get('message')}")
        return slot["result"].get("result", {})

    def _css_width(self):
        """網頁可視寬度（網頁座標）。只問瀏覽器的版面資訊，不在網頁裡執行程式碼"""
        m = self.send("Page.getLayoutMetrics")
        for key in ("cssVisualViewport", "cssLayoutViewport", "layoutViewport"):
            v = m.get(key) or {}
            if v.get("clientWidth"):
                return float(v["clientWidth"])
        return None

    def _update_scale(self, img_w):
        try:
            cw = self._css_width()
        except Exception:
            cw = None
        if cw:
            new = round(img_w / cw, 4)
            if abs(new - self.dpr) > 0.01 or not self._scale_w:
                self.log(f"畫面縮放比例 {new:.2f}（畫面 {img_w} 像素寬）")
            self.dpr = new
        self._scale_w = img_w

    def send_nowait(self, method, params=None):
        """送出指令但不等回應（滑鼠移動軌跡用，比較快）"""
        if self._closed:
            raise RuntimeError("和 Edge 的連線中斷了（遊戲用的 Edge 被關掉？）")
        with self._lock:
            self._id += 1
            self.ws.send(json.dumps({"id": self._id, "method": method, "params": params or {}}))

    def ensure_window(self, width, height):
        """把遊戲視窗固定成指定大小；被縮到最小／最大化時還原。回傳做了什麼（沒動作回傳空字串）"""
        r = self.send("Browser.getWindowForTarget")
        wid, bnd = r["windowId"], r.get("bounds", {})
        state = bnd.get("windowState", "normal")
        did = ""
        if state != "normal":
            self.send("Browser.setWindowBounds", {"windowId": wid,
                                                  "bounds": {"windowState": "normal"}})
            did = {"minimized": "從最小化還原", "maximized": "取消最大化",
                   "fullscreen": "取消全螢幕"}.get(state, "還原視窗")
            time.sleep(0.3)
            bnd = self.send("Browser.getWindowForTarget").get("bounds", {})
        if abs(bnd.get("width", 0) - width) > 2 or abs(bnd.get("height", 0) - height) > 2:
            self.send("Browser.setWindowBounds", {"windowId": wid,
                                                  "bounds": {"width": int(width),
                                                             "height": int(height)}})
            did = (did + "，" if did else "") + f"調整成 {width}×{height}"
        return did

    def evaluate(self, expr):
        """（只給測試用；正式執行時不會在遊戲網頁裡執行任何程式碼）"""
        r = self.send("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        return r.get("result", {}).get("value")

    def close(self):
        self._fstop = True
        self._closed = True
        try:
            self.ws.close()
        except Exception:
            pass

    @property
    def alive(self):
        return not self._closed

    # ---------------- 畫面 ----------------
    def _frame_loop(self):
        while not self._fstop and not self._closed:
            t0 = time.time()
            if self.capture_paused:
                time.sleep(0.3)
                continue
            try:
                r = self.send("Page.captureScreenshot",
                              {"format": "jpeg", "quality": 85, "fromSurface": True}, timeout=5)
                buf = np.frombuffer(base64.b64decode(r["data"]), np.uint8)
                img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
                if img is not None:
                    if img.shape[1] != self._scale_w:   # 第一張或視窗大小改變 → 重算比例
                        self._update_scale(img.shape[1])
                    with self._frame_lock:
                        self.frame, self.frame_t = img, time.time()
            except Exception:
                time.sleep(0.5)
            dt = time.time() - t0
            if dt < self.frame_interval:
                time.sleep(self.frame_interval - dt)

    def latest(self, wait=3.0):
        end = time.time() + wait
        while True:
            with self._frame_lock:
                f = self.frame
            if f is not None or time.time() > end:
                return f
            time.sleep(0.05)

    # ---------------- 輸入（座標為畫面像素） ----------------
    def key_down(self, k):
        key, code, vk = key_info(k)
        p = {"type": "keyDown", "key": key, "code": code,
             "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk}
        if len(key) == 1:
            p["text"] = key
            p["unmodifiedText"] = key
        self.send("Input.dispatchKeyEvent", p)

    def key_up(self, k):
        key, code, vk = key_info(k)
        self.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": key, "code": code,
                                             "windowsVirtualKeyCode": vk,
                                             "nativeVirtualKeyCode": vk})

    def _css(self, x, y):
        return x / self.dpr, y / self.dpr

    def mouse_move(self, x, y, buttons=0):
        cx, cy = self._css(x, y)
        self.mouse = (x, y)
        self.send_nowait("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": cx, "y": cy,
                                                      "buttons": buttons})

    def mouse_down(self, x=None, y=None):
        if x is None:
            x, y = self.mouse
        cx, cy = self._css(x, y)
        self.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": cx, "y": cy,
                                               "button": "left", "buttons": 1, "clickCount": 1})

    def mouse_up(self, x=None, y=None):
        if x is None:
            x, y = self.mouse
        cx, cy = self._css(x, y)
        self.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": cx, "y": cy,
                                               "button": "left", "buttons": 0, "clickCount": 1})
