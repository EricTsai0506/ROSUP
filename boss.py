"""頭目戰：看地上的紅色預警範圍（直線雷射、粉紅圓圈、一圈一圈的波紋），角色站在裡面就往最近的安全處走。

全部只看畫面顏色，在本機計算，不碰遊戲資料。
"""
import math

import cv2
import numpy as np

DS = 4                 # 縮小倍數（1264x921 → 316x230，夠快）
CHAR_R = 34            # 角色腳下要保持安全的半徑（原始像素）
MIN_BLOB = 1000        # 小於這個面積（原始像素）的紅色塊當成雜訊（技能特效、數字）
DIRS = 24
LANTERN_R = 16         # 燈籠周圍（縮小後格數）不算


def danger_mask(frame, extra=None):
    """回傳縮小後的危險區（uint8 0/1）。畫面上方的血條、左上頭像、右上小地圖、下方技能欄不算。"""
    small = cv2.resize(frame, (frame.shape[1] // DS, frame.shape[0] // DS), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    b, g, r = [small[:, :, i].astype(np.int16) for i in range(3)]
    rg = (r - np.maximum(g, b)) >= 12
    # 預警範圍是半透明的紅／粉紅（色相 158~179）；蓋在藍色水面上會偏成橘紅（色相 0~8）
    m1 = (h >= 158) & (s >= 45) & (s <= 150) & (v >= 60) & rg
    m2 = (h <= 8) & (s >= 45) & (s <= 140) & (v >= 60) & rg
    # 紅燈籠（很飽和的深紅）和它照在地上的橘紅光 → 色相 0~8 那部分在燈籠附近不算
    lan = (((h >= 172) | (h <= 10)) & (s >= 150) & (v >= 35)).astype(np.uint8)
    lan = cv2.morphologyEx(lan, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    if lan.any():
        lan = cv2.dilate(lan, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * LANTERN_R + 1,) * 2))
        m2 &= lan == 0
    # 色相 0~8 的「靜態」判斷會把紅地板、燈籠光也算進去 → 只有沒有鏡頭資料時才用；
    # 有鏡頭資料時改用 extra（跟幾秒前同一塊地面比，新變紅的地方）
    m = (m1 | (m2 if extra is None else extra.astype(bool))).astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    H, W = m.shape
    # 介面區塊
    m[:int(H * 0.13), :int(W * 0.36)] = 0           # 頭像、HP
    m[:int(H * 0.24), int(W * 0.25):int(W * 0.76)] = 0  # 計時、頭目血條
    m[:int(H * 0.31), int(W * 0.80):] = 0           # 小地圖
    m[int(H * 0.82):, int(W * 0.33):int(W * 0.67)] = 0  # 技能欄
    n, lab, st, _ = cv2.connectedComponentsWithStats(m, 8)
    keep = np.zeros(n, np.uint8)
    keep[1:] = st[1:, cv2.CC_STAT_AREA] * DS * DS >= MIN_BLOB
    return keep[lab]


def _disk_hit(mask, x, y, r, r_in=0):
    """(x, y) 是縮小後座標；回傳半徑 r 內（r_in 以外）危險的比例"""
    H, W = mask.shape
    x0, x1 = max(0, int(x - r)), min(W, int(x + r) + 1)
    y0, y1 = max(0, int(y - r)), min(H, int(y + r) + 1)
    if x0 >= x1 or y0 >= y1:
        return 1.0      # 畫面外當成不知道 → 不去
    sub = mask[y0:y1, x0:x1]
    yy, xx = np.ogrid[y0:y1, x0:x1]
    d2 = (xx - x) ** 2 + (yy - y) ** 2
    disk = (d2 <= r * r) & (d2 >= r_in * r_in)
    tot = disk.sum()
    return float(sub[disk].sum()) / tot if tot else 1.0


def lantern_core(hsv):
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    return (((h <= 10) | (h >= 170)) & (s >= 150) & (v >= 60)).astype(np.uint8)


def lantern_mask(hsv, past=None, center=None):
    """紅燈籠：飽和的深紅色、圓圓一團（預警範圍的紅框是細長的線，不會算進來）。回傳燈籠和它周圍的光暈。
    past = 幾秒前同一塊地面的深紅團；燈籠一直都在，剛出現的（例如預警交會的中心）不算燈籠"""
    core = lantern_core(hsv)
    n, lab, st, cen = cv2.connectedComponentsWithStats(core, 8)
    keep = np.zeros(n, np.uint8)
    for k in range(1, n):
        x, y, w, hh, a = st[k]
        if 6 <= a <= 400 and a / float(w * hh) >= 0.45 and max(w, hh) <= 2.2 * min(w, hh):
            # 角色腳下剛出現的一團紅 → 多半是預警交會的中心，不當燈籠
            if center is not None and math.hypot(cen[k][0] - center[0], cen[k][1] - center[1]) < 22 \
                    and (past is None or past[lab == k].mean() < 0.3):
                continue
            keep[k] = 1
    m = keep[lab]
    if m.any():
        m = cv2.dilate(m, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * GLOW_R + 1,) * 2))
    return m


def _redness(frame):
    """縮小後每格的「偏紅程度」R-(G+B)/2，和色相、飽和度"""
    small = cv2.resize(frame, (frame.shape[1] // DS, frame.shape[0] // DS), interpolation=cv2.INTER_AREA)
    b, g, r = [small[:, :, i].astype(np.int16) for i in range(3)]
    red = r - (g + b) // 2
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    return red, hsv


GLOW_R = 34           # 燈籠光暈半徑（縮小後格數）
NEW_RED = 18          # 跟 0.4~3 秒前同一塊地面比，偏紅多這麼多 → 新出現的紅色預警
HIST_SEC = 3.0


class BossDodge:
    def __init__(self):
        self.hist = []        # (時間, 偏紅程度, 世界座標)
        self.mask = None
        self.mask_t = 0
        self.dir = None
        self.until = 0
        self.stats = {"dodge": 0}
        self.active_since = None

    def _new_red(self, frame, now, world, lc=None):
        """跟前幾秒「同一塊地面」比較，新變紅的地方（紅色預警剛畫出來）。
        燈籠、紅地板一直都是紅的，不會算進去。world = 角色在地圖上的位置（像素，鏡頭位移累計）"""
        red, hsv = _redness(frame)
        core = lantern_core(hsv)
        H, W = red.shape
        base = None
        pcore = None
        if world is not None:
            for t, r0, w0, c0 in self.hist:
                if not (0.35 <= now - t <= HIST_SEC):
                    continue
                # 同一塊地面：現在畫面 p ↔ 過去畫面 p + (world_now - world_then)
                sx = int(round((world[0] - w0[0]) / DS))
                sy = int(round((world[1] - w0[1]) / DS))
                if abs(sx) >= W // 2 or abs(sy) >= H // 2:
                    continue
                al = np.full_like(r0, 999)
                ys0, ys1 = max(0, -sy), min(H, H - sy)
                xs0, xs1 = max(0, -sx), min(W, W - sx)
                al[ys0:ys1, xs0:xs1] = r0[ys0 + sy:ys1 + sy, xs0 + sx:xs1 + sx]
                base = al if base is None else np.minimum(base, al)
                ac = np.zeros_like(c0)
                ac[ys0:ys1, xs0:xs1] = c0[ys0 + sy:ys1 + sy, xs0 + sx:xs1 + sx]
                pcore = ac if pcore is None else np.maximum(pcore, ac)
            if not self.hist or now - self.hist[-1][0] >= 0.15:
                self.hist.append((now, red, world, core))
            self.hist = [h for h in self.hist if now - h[0] <= HIST_SEC + 0.5]
        if base is None:
            return np.zeros_like(red, dtype=np.uint8)
        h, sat = hsv[:, :, 0], hsv[:, :, 1]
        hue_ok = (h <= 7) | (h >= 150)
        valid = base < 999
        diff = red - base
        # 整個畫面一起變亮變暗（技能閃光、天色）→ 扣掉整體的變化
        if valid.any():
            diff = diff - int(np.median(diff[valid]))
        m = (diff >= NEW_RED) & hue_ok & (sat >= 35) & valid
        if pcore is not None:
            pcore = cv2.dilate(pcore, np.ones((3, 3), np.uint8))
        cen = (lc[0] / DS, lc[1] / DS) if lc is not None else (W / 2, H / 2)
        m &= lantern_mask(hsv, pcore, cen) == 0     # 燈籠的光會忽明忽暗，不算
        return m.astype(np.uint8)

    def update(self, frame, now, world=None, lc=None):
        m = danger_mask(frame, extra=self._new_red(frame, now, world, lc))
        self.mask = m
        self.mask_t = now
        return self.mask

    def danger_frac(self):
        return float(self.mask.mean()) if self.mask is not None else 0.0

    def in_danger(self, lc):
        if self.mask is None:
            return False
        # 角色自己的圖、腳下的白圈不會是紅色 → 看角色周圍一圈
        return _disk_hit(self.mask, lc[0] / DS, lc[1] / DS, CHAR_R * 1.5 / DS, CHAR_R * 0.6 / DS) > 0.2

    def escape(self, lc, now, wall_ok=None, prefer=None):
        """站在預警範圍裡 → 找最近的安全點，回傳 (方向, 距離像素)；不在裡面回傳 None。
        wall_ok(dir, dist_px) → False 表示小地圖說那邊是牆。prefer = 原本想走的方向（同距離時優先）"""
        if self.mask is None or not self.in_danger(lc):
            self.dir = None
            return None
        # 剛決定的方向持續一下，避免左右抖
        if self.dir is not None and now < self.until:
            return self.dir
        cx, cy, r = lc[0] / DS, lc[1] / DS, CHAR_R / DS
        best = None
        for k in range(DIRS):
            a = 2 * math.pi * k / DIRS
            dx, dy = math.cos(a), math.sin(a)
            for step in range(2, 80, 2):        # 縮小後 2 格 = 8 像素
                fx, fy = cx + dx * step, cy + dy * step
                frac = _disk_hit(self.mask, fx, fy, r)
                if frac >= 1.0 and not (0 <= fx < self.mask.shape[1] and 0 <= fy < self.mask.shape[0]):
                    break
                if frac <= 0.03:
                    dist = step * DS
                    score = dist
                    if prefer is not None:
                        score -= 25 * (dx * prefer[0] + dy * prefer[1])
                    if wall_ok is not None and not wall_ok((dx, dy), dist):
                        score += 400
                    if best is None or score < best[0]:
                        best = (score, (dx, dy), dist)
                    break
        if best is None:
            return None
        self.dir = (best[1], best[2])
        self.until = now + 0.35
        self.stats["dodge"] += 1
        return self.dir

    def veto(self, d, lc, ahead=90):
        """原本要走的方向 d：前方 ahead 像素內會踩進預警範圍 → 轉到最接近、不會踩進去的方向"""
        if self.mask is None or d is None:
            return d
        cx, cy, r = lc[0] / DS, lc[1] / DS, CHAR_R / DS * 0.8

        def bad(v):
            for s in (ahead * 0.4, ahead * 0.7, ahead):
                if _disk_hit(self.mask, cx + v[0] * s / DS, cy + v[1] * s / DS, r) > 0.15:
                    return True
            return False
        if not bad(d):
            return d
        base = math.atan2(d[1], d[0])
        for off in (20, -20, 40, -40, 60, -60, 90, -90, 120, -120, 150, -150, 180):
            a = base + math.radians(off)
            v = (math.cos(a), math.sin(a))
            if not bad(v):
                return v
        return d

    def _ahead_bad(self, v, lc, now, ahead=110):
        if self.mask is None or now - self.mask_t > 0.6:
            return 0.0
        cx, cy, r = lc[0] / DS, lc[1] / DS, CHAR_R / DS * 0.8
        return max(_disk_hit(self.mask, cx + v[0] * s_ / DS, cy + v[1] * s_ / DS, r)
                   for s_ in (ahead * 0.4, ahead * 0.7, ahead))

    def kite(self, lc, now, cur, world, free=None):
        """頭目戰走位：一直移動（站著不動最容易被打中），往空曠的地方繞圈；
        前面快撞牆、會踩進預警、或卡住了就換方向。free(v) → 0~1，小地圖上那個方向有多空曠"""
        st = self.__dict__.setdefault("_k", {"dir": None, "next": 0, "pos": []})
        st["pos"].append((now, world))
        st["pos"] = [p for p in st["pos"] if now - p[0] <= 1.3]
        stuck = len(st["pos"]) >= 4 and now - st["pos"][0][0] > 1.0 and \
            math.hypot(world[0] - st["pos"][0][1][0], world[1] - st["pos"][0][1][1]) < 12
        kd = st["dir"]
        need = kd is None or now >= st["next"] or stuck or self._ahead_bad(kd, lc, now) > 0.15 \
            or (free is not None and free(kd) < 0.45)
        if not need:
            return kd
        best = None
        base = kd or cur
        for k in range(16):
            a = 2 * math.pi * k / 16
            v = (math.cos(a), math.sin(a))
            sc = 0.0
            if free is not None:
                sc += 2.0 * free(v)
            sc -= 3.0 * self._ahead_bad(v, lc, now)
            if base is not None:
                dot = v[0] * base[0] + v[1] * base[1]
                # 繞圈：偏好轉 60~120 度的方向；卡住了就偏好往回走
                sc += (-0.6 * dot) if stuck else (0.5 * (1 - abs(dot - 0.2)))
            sc += np.random.uniform(0, 0.3)
            if best is None or sc > best[0]:
                best = (sc, v)
        st["dir"] = best[1]
        st["next"] = now + np.random.uniform(1.2, 2.5)
        if stuck:
            st["pos"] = []
        return st["dir"]

    def reset_kite(self):
        self.__dict__.pop("_k", None)

    def debug_image(self, frame, lc, d=None):
        img = frame.copy()
        if self.mask is not None:
            big = cv2.resize(self.mask, (frame.shape[1], frame.shape[0]), interpolation=cv2.INTER_NEAREST)
            ov = img.copy()
            ov[big > 0] = (0, 255, 0)
            img = cv2.addWeighted(img, 0.6, ov, 0.4, 0)
        cv2.circle(img, (int(lc[0]), int(lc[1])), CHAR_R, (0, 255, 255), 2)
        if d is not None:
            cv2.arrowedLine(img, (int(lc[0]), int(lc[1])),
                            (int(lc[0] + d[0] * 80), int(lc[1] + d[1] * 80)), (255, 0, 255), 3)
        return img


def boss_bar(frame):
    """畫面上方中間有沒有頭目／MVP 的血條。
    血條一定從固定位置（畫面寬 26.7% 左右）開始往右填滿，扣血從右邊減少；
    所以看「從那個位置開始的一段紅／橘色」，地圖上的紅色東西不會剛好在那裡"""
    H, W = frame.shape[:2]
    y0, x0 = int(H * 0.17), int(W * 0.25)
    band = frame[y0:int(H * 0.25), x0:int(W * 0.75)].astype(np.int16)
    b, g, r = band[:, :, 0], band[:, :, 1], band[:, :, 2]
    red = ((r > 120) & (r - g > 60) & (r - b > 50)).astype(np.int8)
    lo, hi = int(W * 0.255) - x0, int(W * 0.29) - x0
    need = max(12, int(W * 0.015))
    for row in red:
        d = np.diff(np.concatenate(([0], row, [0])))
        st, en = np.where(d == 1)[0], np.where(d == -1)[0]
        for a_, e_ in zip(st, en):
            if lo <= a_ <= hi and e_ - a_ >= need:
                return True
    return False
