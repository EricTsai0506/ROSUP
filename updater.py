"""線上更新：到 GitHub Releases 檢查新版、下載、換檔後重開。

只會連到 api.github.com / github.com（放程式的地方），不會連遊戲伺服器。
更新時保留：bot_config.json、town_route.json、edge_profile、logs。
"""
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

FROZEN = getattr(sys, "frozen", False)
BASE_DIR = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))
ASSET_EXE = "RoBot-win.zip"      # exe 版（GitHub 自動打包好的）
ASSET_SRC = "RoBot-src.zip"      # 原始碼版（用 run.bat 執行的人）
KEEP_FILES = ["bot_config.json", "town_route.json"]
KEEP_DIRS = ["edge_profile", "logs"]


def parse_ver(v):
    v = (v or "").strip().lstrip("vV")
    out = []
    for p in v.split("."):
        num = "".join(ch for ch in p if ch.isdigit())
        out.append(int(num) if num else 0)
    return tuple(out + [0] * (3 - len(out)))


def _get(url, token="", timeout=15):
    req = urllib.request.Request(url, headers={
        "User-Agent": "RoBot-updater",
        "Accept": "application/vnd.github+json",
    })
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    return urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context())


def check(repo, current, token=""):
    """回傳 None（沒有新版）或 {"version", "notes", "url", "size"}"""
    repo = (repo or "").strip().strip("/")
    if repo.startswith("https://github.com/"):
        repo = repo[len("https://github.com/"):]
    if repo.count("/") != 1:
        raise ValueError("GitHub 儲存庫格式要是「帳號/名稱」，例如 lin05061/robot")
    with _get(f"https://api.github.com/repos/{repo}/releases/latest", token) as r:
        rel = json.load(r)
    latest = rel.get("tag_name", "")
    if parse_ver(latest) <= parse_ver(current):
        return None
    want = ASSET_EXE if FROZEN else ASSET_SRC
    asset = next((a for a in rel.get("assets", []) if a.get("name") == want), None)
    if asset is None:
        raise RuntimeError(f"新版 {latest} 裡找不到 {want}（可能還在打包，等幾分鐘再試）")
    # 私人儲存庫要用 API 網址＋token 下載；公開的用一般下載網址
    url = asset["url"] if token else asset["browser_download_url"]
    return {"version": latest.lstrip("vV"), "notes": rel.get("body") or "",
            "url": url, "size": asset.get("size", 0)}


def download(info, token="", progress=None):
    """下載並解壓到暫存資料夾，回傳新版檔案所在的資料夾"""
    tmp = tempfile.mkdtemp(prefix="robot_update_")
    zpath = os.path.join(tmp, "update.zip")
    req = urllib.request.Request(info["url"], headers={"User-Agent": "RoBot-updater"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/octet-stream")
    with urllib.request.urlopen(req, timeout=60, context=ssl.create_default_context()) as r, \
            open(zpath, "wb") as f:
        total = int(r.headers.get("Content-Length") or info.get("size") or 0)
        done = 0
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if progress and total:
                progress(done, total)
    out = os.path.join(tmp, "new")
    with zipfile.ZipFile(zpath) as z:
        z.extractall(out)
    # 找到真正放程式的那層（有 RoBot.exe 或 gui.py 的資料夾）
    marker = "RoBot.exe" if FROZEN else "gui.py"
    for root, _dirs, files in os.walk(out):
        if marker in files:
            return tmp, root
    raise RuntimeError(f"下載的檔案裡找不到 {marker}")


def apply_and_restart(tmp, src):
    """寫一個小批次檔：等這個程式關掉 → 覆蓋新檔（保留設定）→ 重開。呼叫後要馬上結束程式。"""
    for name in KEEP_FILES:                      # 新版不要蓋掉自己的設定
        p = os.path.join(src, name)
        if os.path.exists(p) and os.path.exists(os.path.join(BASE_DIR, name)):
            os.remove(p)
    for d in KEEP_DIRS:
        p = os.path.join(src, d)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
    if FROZEN:
        relaunch = f'start "" "{os.path.join(BASE_DIR, "RoBot.exe")}"'
    else:
        pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if not os.path.exists(pyw):
            pyw = sys.executable
        relaunch = f'start "" "{pyw}" "{os.path.join(BASE_DIR, "gui.py")}"'
    pid = os.getpid()
    bat = os.path.join(tmp, "apply_update.bat")
    with open(bat, "w", encoding="utf-8") as f:
        f.write(f"""@echo off
chcp 65001 >nul
:wait
tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul && (timeout /t 1 /nobreak >nul & goto wait)
robocopy "{src}" "{BASE_DIR}" /E /R:5 /W:1 /NFL /NDL /NJH /NJS >nul
cd /d "{BASE_DIR}"
{relaunch}
(goto) 2>nul & rmdir /s /q "{tmp}"
""")
    flags = 0x08000000 if sys.platform == "win32" else 0   # CREATE_NO_WINDOW
    subprocess.Popen(["cmd", "/c", bat], creationflags=flags, close_fds=True)
