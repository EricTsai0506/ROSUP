"""序號授權：機器碼、序號驗證、到期檢查。

序號 = 內容（版本、機器碼雜湊、開通日、到期日）+ 作者私鑰的 Ed25519 簽章。
程式裡只有公鑰，只能驗證、不能產生序號。

線上驗證（LICENSE_URL 有設定時）：開程式時和之後每 6 小時問一次賣家的驗證伺服器
（Cloudflare Worker），伺服器可以停用序號、改到期日，並用伺服器的日期判斷到期。
伺服器的回應有簽章（SERVER_PUBLIC_KEY_HEX 驗證），無法偽造。
連不上伺服器時，最後一次驗證成功後 3 天內照常使用。

存檔（license.state）裡放的是伺服器簽過名的整份回應，每次讀都重新驗簽章、
並確認是給這組序號的；手動改檔案內容不會被採信。
"""
import base64
import datetime as dt
import hashlib
import json
import os
import struct
import sys
import threading
import time
import urllib.request

PUBLIC_KEY_HEX = "29b2500b132fb7ccf147ac3d464981c57e28a1ce2cec313fe53ce6c9ee4ea839"
EPOCH = dt.date(2020, 1, 1)
FMT = ">B8sHH"            # 版本、機器碼雜湊 8 bytes、開通日、到期日（距 2020-01-01 的天數）
VERSION = 1

# 線上驗證伺服器（空字串 = 只用離線驗證）
LICENSE_URL = "https://rosup-license-rice-0162.yowlin0506.workers.dev"
SERVER_PUBLIC_KEY_HEX = "0849bf22a86622dec45c4e6a537b613a21cc9b4f9eca5b3d5ba61493b7072d17"
ONLINE_EVERY = 6 * 3600          # 驗證成功後幾秒內不再問伺服器
GRACE_DAYS = 3                   # 連不上伺服器時，最後一次成功後還能用幾天
GRACE_SEC = GRACE_DAYS * 86400
NET_TIMEOUT = 6
_LOCK = threading.RLock()
_REG_PATH = r"Software\HangjiHelper"
APP_VERSION = ""                 # gui 啟動時填入程式版本（給管理頁看）

FROZEN = getattr(sys, "frozen", False)
BASE_DIR = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))
LICENSE_FILE = os.path.join(BASE_DIR, "license.key")
STATE_FILE = os.path.join(BASE_DIR, "license.state")


# ---------------- 機器碼 ----------------
def _machine_id():
    if sys.platform == "win32":
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                               winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0))
            v, _ = winreg.QueryValueEx(k, "MachineGuid")
            return "win:" + str(v)
        except OSError:
            pass
    for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(p) as f:
                return "linux:" + f.read().strip()
        except OSError:
            pass
    import uuid
    return "mac:" + hex(uuid.getnode())


def machine_hash():
    return hashlib.sha256(("hangji-helper|" + _machine_id()).encode()).digest()[:8]


def machine_code():
    """給買家看、傳給賣家的機器碼，例如 3F2A-91C0-7B44-E0D1"""
    h = machine_hash().hex().upper()
    return "-".join(h[i:i + 4] for i in range(0, 16, 4))


def parse_machine_code(code):
    h = "".join(ch for ch in code.upper() if ch in "0123456789ABCDEF")
    if len(h) != 16:
        raise ValueError("機器碼格式不對（應該是 16 個英數字，例如 3F2A-91C0-7B44-E0D1）")
    return bytes.fromhex(h)


# ---------------- 序號編碼 ----------------
def _b32(b):
    return base64.b32encode(b).decode().rstrip("=")


def _unb32(s):
    s = "".join(ch for ch in s.upper() if ch.isalnum())
    return base64.b32decode(s + "=" * (-len(s) % 8))


def pretty(key):
    return "-".join(key[i:i + 5] for i in range(0, len(key), 5))


def days(d):
    return (d - EPOCH).days


def from_days(n):
    return EPOCH + dt.timedelta(days=n)


def make_key(private_key, mhash, valid_days, start=None):
    """（賣家用）產生序號；private_key 是 cryptography 的 Ed25519PrivateKey"""
    start = start or dt.date.today()
    end = start + dt.timedelta(days=int(valid_days))
    payload = struct.pack(FMT, VERSION, mhash, days(start), days(end))
    sig = private_key.sign(payload)
    return pretty(_b32(payload + sig)), end


def decode_key(key):
    """驗證簽章並解出內容；失敗丟出 ValueError"""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature
    try:
        raw = _unb32(key)
    except Exception:
        raise ValueError("序號格式不對，請確認有完整複製") from None
    n = struct.calcsize(FMT)
    if len(raw) != n + 64:
        raise ValueError("序號長度不對，請確認有完整複製")
    payload, sig = raw[:n], raw[n:]
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(PUBLIC_KEY_HEX)).verify(sig, payload)
    except InvalidSignature:
        raise ValueError("序號無效") from None
    ver, mh, s, e = struct.unpack(FMT, payload)
    if ver != VERSION:
        raise ValueError("序號版本不符，請更新程式")
    return {"machine": mh, "start": from_days(s), "end": from_days(e)}


# ---------------- 時間（防止把電腦時間調回去）----------------
def _load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
        return st if isinstance(st, dict) else {}
    except Exception:
        return {}


def _save_state(st):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f)
    except OSError:
        pass


def _reg_get(name):
    """另外存一份在 Windows 登錄檔（刪掉 license.state 也不會歸零）；讀不到回傳 None"""
    if sys.platform != "win32":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_PATH) as k:
            v, _ = winreg.QueryValueEx(k, name)
            return float(v)
    except (OSError, ValueError, TypeError):
        return None


def _reg_set(name, val):
    if sys.platform != "win32":
        return
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _REG_PATH) as k:
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, repr(float(val)))
    except OSError:
        pass


def trusted_today():
    """今天的日期；電腦時間比上次看到的還早超過 1 天就不採信"""
    now = time.time()
    with _LOCK:
        st = _load_state()
        try:
            last = float(st.get("seen", 0))
        except (TypeError, ValueError):
            last = 0.0
        last = max(last, _reg_get("seen") or 0.0)
        if now < last - 86400:
            raise ValueError("電腦的日期時間不正確（比上次使用的時間還早），請校正後再開啟")
        if now > last + 60:          # 不用每次都寫檔
            st["seen"] = now
            _save_state(st)
            _reg_set("seen", now)
    return dt.date.fromtimestamp(now)


# ---------------- 線上驗證 ----------------
def _key_id(key):
    return hashlib.sha256(_unb32(key)).hexdigest()[:16]


_CTX = None


def _ssl_ctx():
    """Windows 憑證 + 程式內附的 certifi 根憑證（有些電腦的 Windows 根憑證不完整，
    只用 Windows 的會出現 CERTIFICATE_VERIFY_FAILED，一直連不上驗證伺服器）"""
    global _CTX
    if _CTX is None:
        import ssl
        ctx = ssl.create_default_context()
        try:
            import certifi
            ctx.load_verify_locations(cafile=certifi.where())
        except Exception:
            pass
        _CTX = ctx
    return _CTX


def _bind(r, kid):
    return hashlib.sha256(f"{r}|{kid}|{machine_hash().hex()}".encode()).hexdigest()[:32]


def _make_nonce(kid):
    """每次詢問用的隨機碼；後半段綁定這組序號和這台電腦，別人的回應拿來也對不上"""
    r = os.urandom(8).hex()
    return r + _bind(r, kid)


def _verify_signed(p, s_b64, kid, nonce=None):
    """驗證伺服器簽過名的回應是真的、而且是給這組序號的；不對就丟出例外"""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(SERVER_PUBLIC_KEY_HEX)).verify(
        base64.b64decode(s_b64), p.encode())
    res = json.loads(p)
    n = str(res.get("nonce", ""))
    if len(n) != 48 or n[16:] != _bind(n[:16], kid) or (nonce is not None and n != nonce):
        raise ValueError("驗證伺服器回應不符")
    dt.date.fromisoformat(res["today"])
    return res


def _query_server(key, kid):
    """問驗證伺服器；回傳 (結果, 原文, 簽章)，連不上或回應不對就丟出例外"""
    nonce = _make_nonce(kid)
    body = json.dumps({"key": key, "machine": machine_hash().hex().upper(), "nonce": nonce,
                       "app_ver": APP_VERSION}).encode()
    req = urllib.request.Request(LICENSE_URL.rstrip("/") + "/v1/check", data=body, method="POST",
                                 headers={"content-type": "application/json",
                                          "user-agent": "hangji-helper"})
    with urllib.request.urlopen(req, timeout=NET_TIMEOUT, context=_ssl_ctx()) as r:
        resp = json.loads(r.read().decode())
    p, sig = resp["p"], resp["s"]
    return _verify_signed(p, sig, kid, nonce), p, sig


def _stored(on, kid):
    """存檔裡那份伺服器回應；沒有、被改過、不是這組序號的 → None"""
    try:
        return _verify_signed(on["p"], on["s"], kid)
    except Exception:
        return None


def online_check(key, force=False):
    """回傳 None（沒設定伺服器）或 {"status": ok/revoked/expired/bad/offline, "msg", "end"}"""
    if not LICENSE_URL:
        return None
    with _LOCK:
        now = time.time()
        today = dt.date.fromtimestamp(now)
        st = _load_state()
        kid = _key_id(key)
        on = st.get("online") if isinstance(st.get("online"), dict) else {}
        if on.get("kid") != kid:
            on = {"kid": kid}
        # 第一次用這組序號的時間（還沒連上過伺服器時的寬限期從這裡算）：檔案和登錄檔取最早的
        firsts = [v for v in (on.get("first"), _reg_get("f_" + kid)) if isinstance(v, (int, float)) and v > 0]
        first = min(firsts + [now])
        if on.get("first") != first or not firsts:
            on["first"] = first
            _reg_set("f_" + kid, first)
        sg = _stored(on, kid)
        age = (today - dt.date.fromisoformat(sg["today"])).days if sg else None
        in_grace = sg is not None and -1 <= age <= GRACE_DAYS
        try:
            ok_t = float(on.get("ok_t", 0))
        except (TypeError, ValueError):
            ok_t = 0.0
        if not force and sg and sg.get("status") == "ok" and in_grace and 0 <= now - ok_t < ONLINE_EVERY:
            return {"status": "ok", "msg": "", "end": sg.get("end")}
    try:
        res, p, sig = _query_server(key, kid)
    except Exception as e:
        with _LOCK:
            st = _load_state()
            st["online"] = on
            _save_state(st)
        if sg is not None and sg.get("status") != "ok":      # 上次伺服器就說不行
            return {"status": sg.get("status"), "msg": sg.get("msg", ""), "end": sg.get("end")}
        if sg is not None and in_grace:
            return {"status": "ok", "msg": "", "end": sg.get("end"), "offline": True}
        if sg is None and 0 <= now - first < GRACE_SEC:      # 還沒連上過：從第一次使用起算
            return {"status": "ok", "msg": "", "end": None, "offline": True}
        return {"status": "offline", "end": None,
                "msg": f"連不上序號驗證伺服器超過 {GRACE_DAYS} 天（{type(e).__name__}），請確認網路後重開程式"}
    with _LOCK:
        st = _load_state()
        on.update(p=p, s=sig)
        for k_ in ("status", "msg", "end"):      # 舊版沒簽章的欄位不再使用
            on.pop(k_, None)
        if res.get("status") == "ok":
            on["ok_t"] = time.time()
        st["online"] = on
        _save_state(st)
    return {"status": res.get("status"), "msg": res.get("msg", ""), "end": res.get("end")}


# 掛機中的定期檢查：在背景做，不卡住打怪
_BG = {"thread": None, "res": None}


def check_in_background():
    """開一條背景執行緒做 check()；回傳上一次背景檢查的結果（還沒有就 None）"""
    t = _BG["thread"]
    if t is None or not t.is_alive():
        def work():
            try:
                _BG["res"] = check()
            except Exception as e:      # 檢查本身出錯不該讓掛機停掉
                _BG["res"] = {"ok": True, "msg": f"授權檢查出錯：{e}", "end": None, "days_left": 0}
        t = threading.Thread(target=work, daemon=True)
        _BG["thread"] = t
        t.start()
    return _BG["res"]


def background_result():
    return _BG["res"]


# ---------------- 對外 ----------------
def check(key=None, force_online=False):
    """回傳 {"ok", "msg", "end", "days_left"}"""
    if key is None:
        try:
            with open(LICENSE_FILE, encoding="utf-8") as f:
                key = f.read().strip()
        except OSError:
            return {"ok": False, "msg": "尚未啟用，請輸入序號", "end": None, "days_left": 0}
    try:
        info = decode_key(key)
        if info["machine"] != machine_hash():
            return {"ok": False, "msg": "這組序號不是這台電腦的（機器碼不符）",
                    "end": info["end"], "days_left": 0}
        today = trusted_today()
    except ValueError as e:
        return {"ok": False, "msg": str(e), "end": None, "days_left": 0}
    end = info["end"]
    on = online_check(key, force_online)
    if on is not None:
        if on["status"] == "ok":
            if on.get("end"):            # 伺服器可以延長或縮短到期日
                try:
                    end = dt.date.fromisoformat(on["end"])
                except ValueError:
                    pass
        else:
            return {"ok": False, "msg": on.get("msg") or "序號驗證失敗", "end": end, "days_left": 0}
    info = dict(info, end=end)
    left = (info["end"] - today).days
    if left <= 0:
        return {"ok": False, "msg": f"授權已於 {info['end']} 到期，請購買新的月卡",
                "end": info["end"], "days_left": 0}
    return {"ok": True, "msg": f"授權到 {info['end']}（剩 {left} 天）", "end": info["end"],
            "days_left": left}


def activate(key):
    r = check(key.strip(), force_online=True)
    if r["ok"]:
        with open(LICENSE_FILE, "w", encoding="utf-8") as f:
            f.write(key.strip())
    return r
