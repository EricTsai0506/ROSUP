"""序號授權：機器碼、序號驗證、到期檢查。

序號 = 內容（版本、機器碼雜湊、開通日、到期日）+ 作者私鑰的 Ed25519 簽章。
程式裡只有公鑰，只能驗證、不能產生序號。

線上驗證（LICENSE_URL 有設定時）：開程式時和之後每 6 小時問一次賣家的驗證伺服器
（Cloudflare Worker），伺服器可以停用序號、改到期日，並用伺服器的日期判斷到期。
伺服器的回應有簽章（SERVER_PUBLIC_KEY_HEX 驗證），無法偽造。
連不上伺服器時，最後一次驗證成功後 3 天內照常使用。
"""
import base64
import datetime as dt
import hashlib
import json
import os
import struct
import sys
import time
import urllib.request
import uuid

PUBLIC_KEY_HEX = "29b2500b132fb7ccf147ac3d464981c57e28a1ce2cec313fe53ce6c9ee4ea839"
EPOCH = dt.date(2020, 1, 1)
FMT = ">B8sHH"            # 版本、機器碼雜湊 8 bytes、開通日、到期日（距 2020-01-01 的天數）
VERSION = 1

# 線上驗證伺服器（空字串 = 只用離線驗證）
LICENSE_URL = "https://rosup-license-rice-0162.yowlin0506.workers.dev"
SERVER_PUBLIC_KEY_HEX = "0849bf22a86622dec45c4e6a537b613a21cc9b4f9eca5b3d5ba61493b7072d17"
ONLINE_EVERY = 6 * 3600          # 驗證成功後幾秒內不再問伺服器
GRACE_SEC = 3 * 86400            # 連不上伺服器時，最後一次成功後還能用多久
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
            return json.load(f)
    except Exception:
        return {}


def _save_state(st):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f)
    except OSError:
        pass


def trusted_today():
    """今天的日期；電腦時間比上次看到的還早超過 1 天就不採信"""
    now = time.time()
    st = _load_state()
    last = float(st.get("seen", 0))
    if now < last - 86400:
        raise ValueError("電腦的日期時間不正確（比上次使用的時間還早），請校正後再開啟")
    if now > last:
        st["seen"] = now
        _save_state(st)
    return dt.date.fromtimestamp(now)


# ---------------- 線上驗證 ----------------
def _key_id(key):
    return hashlib.sha256(_unb32(key)).hexdigest()[:16]


def _query_server(key):
    """問驗證伺服器；回傳伺服器簽過名的結果 dict，連不上或回應不對就丟出例外"""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    nonce = uuid.uuid4().hex
    body = json.dumps({"key": key, "machine": machine_hash().hex().upper(), "nonce": nonce,
                       "app_ver": APP_VERSION}).encode()
    req = urllib.request.Request(LICENSE_URL.rstrip("/") + "/v1/check", data=body, method="POST",
                                 headers={"content-type": "application/json",
                                          "user-agent": "hangji-helper"})
    import ssl
    with urllib.request.urlopen(req, timeout=8, context=ssl.create_default_context()) as r:
        resp = json.loads(r.read().decode())
    p, sig = resp["p"], base64.b64decode(resp["s"])
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(SERVER_PUBLIC_KEY_HEX)).verify(sig, p.encode())
    res = json.loads(p)
    if res.get("nonce") != nonce:
        raise ValueError("驗證伺服器回應不符")
    return res


def online_check(key, force=False):
    """回傳 None（沒設定伺服器）或 {"status": ok/revoked/expired/bad/offline, "msg", "end"}"""
    if not LICENSE_URL:
        return None
    now = time.time()
    st = _load_state()
    kid = _key_id(key)
    on = st.get("online") if isinstance(st.get("online"), dict) else {}
    if on.get("kid") != kid:
        on = {"kid": kid, "first": now}
    ok_t = float(on.get("ok_t", 0))
    if not force and on.get("status") == "ok" and 0 <= now - ok_t < ONLINE_EVERY:
        return {"status": "ok", "msg": "", "end": on.get("end")}
    try:
        res = _query_server(key)
    except Exception as e:
        ref = ok_t or float(on.get("first", now))
        on.setdefault("first", now)
        st["online"] = on
        _save_state(st)
        if 0 <= now - ref < GRACE_SEC and on.get("status") in (None, "ok"):
            return {"status": "ok", "msg": "", "end": on.get("end"), "offline": True}
        if on.get("status") not in (None, "ok"):
            return {"status": on["status"], "msg": on.get("msg", ""), "end": on.get("end")}
        return {"status": "offline", "end": on.get("end"),
                "msg": f"連不上序號驗證伺服器超過 3 天（{type(e).__name__}），請確認網路後重開程式"}
    on.update(status=res.get("status"), msg=res.get("msg", ""), end=res.get("end"))
    if res.get("status") == "ok":
        on["ok_t"] = now
    st["online"] = on
    _save_state(st)
    return {"status": res.get("status"), "msg": res.get("msg", ""), "end": res.get("end")}


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
