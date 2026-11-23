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


def danger_mask(frame):
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
    m = (m1 | m2).astype(np.uint8)
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


class BossDodge:
    def __init__(self):
        self.mask = None
        self.mask_t = 0
        self.dir = None
        self.until = 0
        self.stats = {"dodge": 0}
        self.active_since = None

    def update(self, frame, now):
        self.mask = danger_mask(frame)
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
    """畫面上方中間有沒有頭目的長條紅色血條（OCR 沒讀到「MVP」時的備援）"""
    H, W = frame.shape[:2]
    band = frame[int(H * 0.17):int(H * 0.25), int(W * 0.25):int(W * 0.75)].astype(np.int16)
    b, g, r = band[:, :, 0], band[:, :, 1], band[:, :, 2]
    red = ((r > 120) & (r - g > 60) & (r - b > 50)).astype(np.int8)
    need = max(40, int(W * 0.08))
    for row in red[::2]:
        # 最長連續紅色
        d = np.diff(np.concatenate(([0], row, [0])))
        st, en = np.where(d == 1)[0], np.where(d == -1)[0]
        if len(st) and (en - st).max() >= need:
            return True
    return False
