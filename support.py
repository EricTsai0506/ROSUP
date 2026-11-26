# -*- coding: utf-8 -*-
"""回報問題：把文字紀錄檔打包成一個 zip，傳到賣家自己的伺服器（跟序號驗證同一台，不是遊戲伺服器）。

只有使用者自己按「回報問題」（或更新失敗時選擇回報）才會傳，程式不會自己偷傳。
只傳文字：執行紀錄、更新紀錄、報表、設定檔（密碼／權杖會先拿掉）和電腦的基本資訊；不傳截圖。
傳不出去的時候會把 zip 存在 logs 資料夾，使用者可以自己傳給賣家。
"""
import io
import json
import os
import platform
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

import licensing

MAX_BYTES = 900_000            # 伺服器最多收 1.9MB；文字壓縮後 900KB 已經是十幾 MB 的紀錄
MAX_BYTES_NOKEY = 380_000      # 還沒啟用序號的人只能傳小的
LOG_TAIL = 1_200_000           # 每個執行紀錄最多取最後這麼多 bytes
SEND_TIMEOUT = 40
SECRET_WORDS = ("token", "password", "passwd", "secret", "密碼", "權杖")
WHAT = ("會傳送（只有文字）：\n"
        "・最近幾次的執行紀錄、更新紀錄、今天和昨天的報表\n"
        "・設定檔（不含密碼）\n"
        "・電腦資訊：Windows 版本、螢幕大小、程式放在哪個資料夾、機器碼\n\n"
        "不會傳送：截圖、遊戲的帳號密碼。\n"
        "資料只會傳到賣家的伺服器，用來找問題。")


def _tail(path, limit):
    """檔案最後 limit bytes（從完整的一行開始）"""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if size <= limit:
            return f.read()
        f.seek(size - limit)
        data = f.read()
    i = data.find(b"\n")
    head = f"……（前面省略 {size - limit} bytes）\n".encode("utf-8")
    return head + (data[i + 1:] if 0 <= i < 4000 else data)


def _redact(obj):
    """設定檔裡名字像密碼／權杖的欄位，內容換成 ***"""
    if isinstance(obj, dict):
        return {k: ("***" if (v and any(w in str(k).lower() for w in SECRET_WORDS)) else _redact(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


def _config_text(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.dumps(_redact(json.load(f)), ensure_ascii=False, indent=2).encode("utf-8")
    except Exception as e:
        return f"設定檔讀不出來：{e!r}".encode("utf-8")


def _system_info(base_dir, version, note, kind):
    L = [f"掛機小幫手 v{version}", f"回報時間：{time.strftime('%Y-%m-%d %H:%M:%S')}",
         f"類型：{kind}", f"系統：{platform.platform()}", f"Python：{sys.version.split()[0]}"
         f"（{'打包版' if getattr(sys, 'frozen', False) else '原始碼'}）",
         f"程式資料夾：{base_dir}"]
    try:
        L.append(f"機器碼：{licensing.machine_code()}")
    except Exception as e:
        L.append(f"機器碼讀不到：{e!r}")
    # 這個資料夾能不能寫入（更新失敗最常見的原因）
    try:
        t = os.path.join(base_dir, f"_write_test_{os.getpid()}.tmp")
        with open(t, "w") as f:
            f.write("x")
        os.remove(t)
        L.append("程式資料夾可以寫入：是")
    except Exception as e:
        L.append(f"程式資料夾可以寫入：否（{e!r}）")
    try:
        import shutil
        du = shutil.disk_usage(base_dir)
        L.append(f"磁碟剩餘空間：{du.free / 1e9:.1f} GB")
    except Exception:
        pass
    try:
        import ctypes
        u = ctypes.windll.user32
        L.append(f"螢幕：{u.GetSystemMetrics(0)}x{u.GetSystemMetrics(1)}")
    except Exception:
        pass
    L += ["", "買家寫的說明：", note or "（沒有寫）", "", "程式資料夾裡的東西："]
    try:
        for name in sorted(os.listdir(base_dir))[:80]:
            p = os.path.join(base_dir, name)
            try:
                st = os.stat(p)
                kind_ = "資料夾" if os.path.isdir(p) else f"{st.st_size:>11,d}"
                L.append(f"  {time.strftime('%Y-%m-%d %H:%M', time.localtime(st.st_mtime))}  {kind_:>11}  {name}")
            except OSError:
                L.append(f"  （讀不到）  {name}")
    except OSError as e:
        L.append(f"  列不出來：{e!r}")
    return "\n".join(L).encode("utf-8")


def collect(base_dir, log_dir, version, note="", kind="manual", config_path=None, max_bytes=MAX_BYTES):
    """打包要回報的文字檔。回傳 (zip 的 bytes, [檔名…])。太大就少放一點舊的紀錄"""
    files = [("電腦資訊.txt", _system_info(base_dir, version, note, kind))]
    cfg = config_path or os.path.join(base_dir, "bot_config.json")
    if os.path.exists(cfg):
        files.append(("bot_config.json", _config_text(cfg)))
    names = []
    try:
        names = os.listdir(log_dir)
    except OSError:
        pass
    for name, limit in (("update.log", 300_000), ("crash.txt", 200_000)):
        p = os.path.join(log_dir, name)
        if name in names and os.path.getsize(p) > 0:
            try:
                files.append((name, _tail(p, limit)))
            except OSError:
                pass
    for name in sorted((n for n in names if n.startswith("report_") and n.endswith(".txt")), reverse=True)[:2]:
        try:
            files.append((name, _tail(os.path.join(log_dir, name), 100_000)))
        except OSError:
            pass
    logs = sorted((n for n in names if n.startswith("bot_") and n.endswith(".log")), reverse=True)
    n_logs, tail = 3, LOG_TAIL
    while True:
        out = list(files)
        for name in logs[:n_logs]:
            try:
                out.append((name, _tail(os.path.join(log_dir, name), tail)))
            except OSError:
                pass
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            for name, data in out:
                z.writestr(name, data)
        data = buf.getvalue()
        if len(data) <= max_bytes or (n_logs <= 1 and tail <= 20_000):
            return data, [n for n, _ in out]
        if n_logs > 1:
            n_logs -= 1                      # 先少放一個舊的紀錄
        else:
            tail //= 2                       # 還是太大 → 最新的紀錄只留後半


def _key():
    try:
        with open(licensing.LICENSE_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def has_key():
    k = _key()
    if not k:
        return False
    try:
        licensing.decode_key(k)
        return True
    except Exception:
        return False


def send(data, version, note="", kind="manual"):
    """把 zip 傳到伺服器。回傳 (成功?, 訊息, 回報編號或 None)"""
    key = _key()
    headers = {"content-type": "application/zip", "user-agent": "hangji-helper",
               "x-machine": licensing.machine_hash().hex().upper(), "x-ver": str(version)[:20],
               "x-kind": kind, "x-note": urllib.parse.quote((note or "")[:500])}
    if key:
        headers["x-key"] = "".join(c for c in key if c.isalnum() or c == "-")[:200]
    req = urllib.request.Request(licensing.LICENSE_URL.rstrip("/") + "/v1/report", data=data, method="POST",
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=SEND_TIMEOUT, context=licensing._ssl_ctx()) as r:
            res = json.loads(r.read().decode("utf-8"))
        if res.get("ok"):
            return True, "已送出", res.get("id")
        return False, str(res.get("error") or "伺服器沒有收下"), None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False, "伺服器還沒開放回報功能", None
        try:
            msg = json.loads(e.read().decode("utf-8")).get("error")
        except Exception:
            msg = None
        return False, msg or f"伺服器回應 {e.code}", None
    except Exception as e:
        return False, f"連不上伺服器（{type(e).__name__}）", None


def save_local(data, log_dir):
    """傳不出去時把 zip 存在 logs，讓使用者自己傳給賣家。回傳路徑（存不了回傳 None）"""
    try:
        os.makedirs(log_dir, exist_ok=True)
        path = os.path.join(log_dir, f"回報_{time.strftime('%Y%m%d_%H%M%S')}.zip")
        with open(path, "wb") as f:
            f.write(data)
        return path
    except OSError:
        return None


def report(base_dir, log_dir, version, note="", kind="manual", config_path=None):
    """打包＋傳送（給背景執行緒用）。回傳 dict：ok, msg, id, path（失敗時存的 zip）, files, size"""
    data, names = collect(base_dir, log_dir, version, note, kind, config_path,
                          MAX_BYTES if has_key() else MAX_BYTES_NOKEY)
    ok, msg, rid = send(data, version, note, kind)
    path = None if ok else save_local(data, log_dir)
    return {"ok": ok, "msg": msg, "id": rid, "path": path, "files": names, "size": len(data)}
