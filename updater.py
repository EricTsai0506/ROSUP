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
import urllib.error
import urllib.request
import zipfile

FROZEN = getattr(sys, "frozen", False)
BASE_DIR = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))
ASSET_EXE = "RoBot-win.zip"      # exe 版（GitHub 自動打包好的）
ASSET_SRC = "RoBot-src.zip"      # 原始碼版（用 run.bat 執行的人）
KEEP_FILES = ["bot_config.json", "town_route.json", "town_route_expedition.json", "license.key", "license.state"]
KEEP_DIRS = ["edge_profile", "logs", "minimap"]


def parse_ver(v):
    v = (v or "").strip().lstrip("vV")
    out = []
    for p in v.split("."):
        num = "".join(ch for ch in p if ch.isdigit())
        out.append(int(num) if num else 0)
    return tuple(out + [0] * (3 - len(out)))


def _short(t, width):
    t = " ".join((t or "").split())
    return t if len(t) <= width else t[:width - 1].rstrip("、，。；： ") + "…"


def brief_notes(notes, current, max_versions=5, max_items=4, width=38):
    """更新視窗用的精簡更新內容（幾行字，不是整份更新紀錄）。
    notes 是 GitHub 版本說明（每一版一段，第一行是「v1.2.3 標題」，下面是「- 細項」）。
    - 只比現在新一版 → 把那一版的標題用「、」拆成幾個重點
    - 一次跳好幾版 → 每一版一行「v1.2.3 標題」，最多 max_versions 行
    回傳文字的 list；讀不出來回傳 []"""
    import re
    cur = parse_ver(current)
    secs = []
    for line in (notes or "").splitlines():
        m = re.match(r"^\s*#*\s*[vV](\d+(?:\.\d+){1,3})\s*[:：\-－]?\s*(.*)$", line)
        if m:
            secs.append([m.group(1), m.group(2).strip(), []])
        elif secs and re.match(r"^\s*[-・*]\s+\S", line):
            secs[-1][2].append(re.sub(r"^\s*[-・*]\s+", "", line).strip())
    new = [s for s in secs if parse_ver(s[0]) > cur]
    if not new:
        return []
    if len(new) == 1:
        ver, title, items = new[0]
        parts = [p.strip() for p in re.split(r"[、；;]", title) if p.strip()]
        if not parts:                       # 沒有標題 → 用前幾個細項
            parts = items
        out = [_short(p, width) for p in parts[:max_items]]
        if len(parts) > max_items:
            out.append(f"…還有 {len(parts) - max_items} 項")
        return out
    out = [_short(f"v{ver} {title or (items[0] if items else '')}", width + 8) for ver, title, items in new[:max_versions]]
    if len(new) > max_versions:
        out.append(f"…以及更早的 {len(new) - max_versions} 個版本")
    return out


_CTX = None


def _ssl_ctx():
    """Windows 憑證 + 程式內附的 certifi 根憑證。
    有些電腦的 Windows 根憑證不完整（沒自動更新），只用 Windows 的會出現
    CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate"""
    global _CTX
    if _CTX is None:
        ctx = ssl.create_default_context()
        try:
            import certifi
            ctx.load_verify_locations(cafile=certifi.where())
        except Exception:
            pass
        _CTX = ctx
    return _CTX


def _get(url, token="", timeout=15):
    req = urllib.request.Request(url, headers={
        "User-Agent": "RoBot-updater",
        "Accept": "application/vnd.github+json",
    })
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    return urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx())


def check(repo, current, token=""):
    """回傳 None（沒有新版）或 {"version", "notes", "url", "size"}"""
    repo = (repo or "").strip().strip("/")
    if repo.startswith("https://github.com/"):
        repo = repo[len("https://github.com/"):]
    if repo.count("/") != 1:
        raise ValueError("GitHub 儲存庫格式要是「帳號/名稱」，例如 lin05061/robot")
    try:
        with _get(f"https://api.github.com/repos/{repo}/releases/latest", token) as r:
            rel = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RuntimeError("找不到這個儲存庫或還沒有任何版本。如果儲存庫是私人的，"
                               "請在「存取金鑰」填 GitHub 的唯讀金鑰") from None
        if e.code in (401, 403):
            raise RuntimeError("存取金鑰無效或已過期，或 GitHub 暫時限制查詢次數") from None
        raise
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
    with urllib.request.urlopen(req, timeout=60, context=_ssl_ctx()) as r, \
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


PENDING = os.path.join(BASE_DIR, "update_pending.json")


def can_write():
    """程式資料夾能不能寫入（放在 Program Files、唯讀資料夾就不行）"""
    try:
        t = os.path.join(BASE_DIR, ".write_test")
        with open(t, "w") as f:
            f.write("ok")
        os.remove(t)
        return True
    except OSError:
        return False


def mark_pending(version, brief=None):
    try:
        with open(PENDING, "w", encoding="utf-8") as f:
            json.dump({"to": version, "brief": list(brief or [])}, f, ensure_ascii=False)
    except OSError:
        pass


def take_pending():
    """重開後呼叫：回傳 (上次要更新到的版本, 精簡更新內容)；沒有就 (None, [])，並刪掉記錄"""
    try:
        with open(PENDING, encoding="utf-8") as f:
            d = json.load(f)
        os.remove(PENDING)
        brief = d.get("brief") or []
        return d.get("to"), [str(b) for b in brief if b][:8]
    except (OSError, ValueError, AttributeError):
        return None, []


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
    logdir = os.path.join(BASE_DIR, "logs")
    os.makedirs(logdir, exist_ok=True)
    ulog = os.path.join(logdir, "update.log")
    # 批次檔用系統的編碼（中文 Windows 是 Big5），資料夾名稱有中文才不會壞掉；不用 chcp 65001
    enc = "mbcs" if sys.platform == "win32" else "utf-8"
    with open(bat, "w", encoding=enc, errors="replace") as f:
        f.write(f"""@echo off
:wait
tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul && (timeout /t 1 /nobreak >nul & goto wait)
echo ==== %date% %time% 更新到 {os.path.basename(src)} >> "{ulog}"
robocopy "{src}" "{BASE_DIR}" /E /R:10 /W:1 /NFL /NDL /NP >> "{ulog}" 2>&1
echo robocopy 結果代碼 %ERRORLEVEL%（0~7 = 成功，8 以上 = 失敗） >> "{ulog}"
cd /d "{BASE_DIR}"
{relaunch}
(goto) 2>nul & rmdir /s /q "{tmp}"
""")
    flags = 0x08000000 if sys.platform == "win32" else 0   # CREATE_NO_WINDOW
    subprocess.Popen(["cmd", "/c", bat], creationflags=flags, close_fds=True)
