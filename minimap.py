"""戰鬥右上角小地圖：找自己的位置（紅白箭頭）、邊走邊記住地形，走路時避開牆壁／水／懸崖。

做法（只看截圖，不讀遊戲資料、不連網路）：
  1. 用藍色標題列找到小地圖的位置
  2. 小地圖上「紅色外框、白色中心」的箭頭 = 自己（磁鐵也是紅白色，但白色在邊上）
  3. 自己走過的地方一定能走 → 記下那些地方的顏色；顏色差很多的（牆、水、樹、黑色空地）當作障礙
  4. 往某個方向走之前，先在小地圖上看前面有沒有障礙，有就換一個最接近、又走得通的方向
  5. 每張地圖學到的地形存在 minimap 資料夾，下一場一進來就知道
"""
import heapq
import math
import os
import random
import re
import time

import cv2
import numpy as np

LOOK = 7.0           # 往前檢查幾個小地圖像素（約 10 格地圖）
DEFAULT_SCALE = 20.0  # 還沒量到之前：畫面上幾個像素 ≈ 小地圖 1 個像素
MAGNET_NEAR = 30.0    # 磁鐵在這個距離內（小地圖像素，約 2～3 個畫面）就先去撿


# ---------------------------------------------------------------------
#  找小地圖、找箭頭
# ---------------------------------------------------------------------
def find_panel(frame):
    """右上角小地圖：標題列是很均勻的藍色（上淺下深）。
    回傳 ((地圖區 x, y, w, h), (標題列 x, y, w, h)) 或 None"""
    H, W = frame.shape[:2]
    x0, y1 = int(W * 0.66), int(H * 0.30)
    sub = frame[:y1, x0:]
    hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
    blue = (hsv[:, :, 0] >= 100) & (hsv[:, :, 0] <= 126) & (hsv[:, :, 1] >= 50) & (hsv[:, :, 2] >= 120)
    b0, b1 = int(W * 0.86) - x0, int(W * 0.96) - x0
    if b1 - b0 < 10:
        return None
    v = hsv[:, b0:b1, 2].astype(np.float32)
    bm = blue[:, b0:b1]
    frac = bm.mean(axis=1)
    std = np.array([v[y][bm[y]].std() if bm[y].sum() > 5 else 99.0 for y in range(len(v))])
    clean = np.nonzero((frac >= 0.9) & (std <= 2.5))[0]   # 沒有字、顏色均勻的那幾列
    if len(clean) < 4:
        return None
    grp = [int(clean[0])]
    for y in clean[1:]:
        if y - grp[0] <= 60:
            grp.append(int(y))
    ya, yb = grp[0], grp[-1]
    if yb - ya < 10 or ya > H * 0.2:
        return None
    cols = blue[grp[-3:]].all(axis=0)      # 標題列下面幾列（上面是圓角）
    mid = (b0 + b1) // 2
    if not cols[mid]:
        return None
    xa = mid
    while xa > 0 and cols[xa - 1]:
        xa -= 1
    xb = mid
    while xb + 1 < len(cols) and cols[xb + 1]:
        xb += 1
    w = xb - xa + 1
    if not (W * 0.10 <= w <= W * 0.28) or x0 + xb < W * 0.93:
        return None
    mx0, mx1 = x0 + xa + 3, x0 + xb - 3
    my0 = yb + 3
    my1 = min(H, my0 + (mx1 - mx0))
    if my1 - my0 < 40:
        return None
    return (int(mx0), int(my0), int(mx1 - mx0), int(my1 - my0)), (int(x0 + xa), int(ya), int(w), int(yb - ya + 1))


def _shift(m, dy, dx):
    """把布林圖往 (dy, dx) 平移，空出來的地方補 False"""
    out = np.zeros_like(m)
    H, W = m.shape
    if abs(dy) >= H or abs(dx) >= W:
        return out
    ys, yd = (slice(0, H - dy), slice(dy, H)) if dy >= 0 else (slice(-dy, H), slice(0, H + dy))
    xs, xd = (slice(0, W - dx), slice(dx, W)) if dx >= 0 else (slice(-dx, W), slice(0, W + dx))
    out[yd, xd] = m[ys, xs]
    return out


def _masks(mm):
    hsv = cv2.cvtColor(mm, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    red = ((h < 10) | (h > 170)) & (s > 110) & (v > 110)
    white = (s < 70) & (v > 205)
    gold = (h >= 14) & (h <= 34) & (s >= 150) & (v >= 170)
    return red, white, gold


def find_arrow(mm, red=None, white=None):
    """小地圖上自己的箭頭：紅色外框包住白色中心。回傳 [(x, y, 分數), ...]"""
    if red is None:
        red, white, _ = _masks(mm)
    sides = np.zeros(red.shape, np.int32)
    for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        hit = np.zeros_like(red)
        for k in (1, 2, 3):
            hit |= _shift(red, dy * k, dx * k)
        sides += hit
    enc = white & (sides >= 3)            # 白點四周至少三邊有紅色
    n, _lab, st, _ = cv2.connectedComponentsWithStats(red.astype(np.uint8))
    out = []
    for i in range(1, n):
        x, y, w, hh, a = st[i]
        if a < 20 or w > 34 or hh > 34 or w < 6 or hh < 6:
            continue
        x0, y0, x1, y1 = max(0, x - 3), max(0, y - 3), x + w + 3, y + hh + 3
        e = enc[y0:y1, x0:x1]
        ne = int(e.sum())
        if ne >= 5:
            ys, xs = np.nonzero(e)
            px, py = x0 + float(xs.mean()), y0 + float(ys.mean())
            dup = next((k for k, o in enumerate(out) if math.hypot(o[0] - px, o[1] - py) < 6), None)
            if dup is None:
                out.append((px, py, ne))
            elif ne > out[dup][2]:
                out[dup] = (px, py, ne)
    return out


def _bins(img):
    q = (img >> 4).astype(np.int32)
    return (q[..., 0] << 8) | (q[..., 1] << 4) | q[..., 2]


# 每個色階（16×16×16）的代表色
_BIN_BGR = (np.indices((16, 16, 16)).reshape(3, -1).T * 16 + 8).astype(np.uint8)
_BIN_HSV = cv2.cvtColor(_BIN_BGR.reshape(-1, 1, 3), cv2.COLOR_BGR2HSV).reshape(-1, 3).astype(np.int32)
# 一看就知道不能走的顏色：接近全黑（地圖外、懸崖）、藍色（水、藍綠色的牆）
_PRIOR = (_BIN_HSV[:, 2] < 38) | ((_BIN_HSV[:, 0] >= 88) & (_BIN_HSV[:, 0] <= 128)
                                  & (_BIN_HSV[:, 1] >= 70) & (_BIN_HSV[:, 2] >= 50))


def _maxf3(a):
    """16×16×16 色階表：每個色階取旁邊一格的最大值（材質有點雜，顏色差一點點也算同一種）"""
    out = a.reshape(16, 16, 16).copy()
    for ax in range(3):
        b = out.copy()
        sa, sb = [slice(None)] * 3, [slice(None)] * 3
        sa[ax], sb[ax] = slice(1, None), slice(None, -1)
        b[tuple(sa)] = np.maximum(b[tuple(sa)], out[tuple(sb)])
        b[tuple(sb)] = np.maximum(b[tuple(sb)], out[tuple(sa)])
        out = b
    return out.reshape(-1)


def astar(obst, start, goal, max_nodes=12000):
    """格子地圖上 8 方向的 A*；obst[y, x] = True 不能走。回傳 [(x, y), ...] 或 None"""
    H, W = obst.shape
    sx, sy = start
    gx, gy = goal
    if not (0 <= sx < W and 0 <= sy < H and 0 <= gx < W and 0 <= gy < H):
        return None
    openq = [(0.0, (sx, sy))]
    came = {(sx, sy): None}
    g = {(sx, sy): 0.0}
    n = 0
    while openq and n < max_nodes:
        n += 1
        _, cur = heapq.heappop(openq)
        if abs(cur[0] - gx) <= 1 and abs(cur[1] - gy) <= 1:
            path = []
            while cur:
                path.append(cur)
                cur = came[cur]
            return path[::-1]
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if not dx and not dy:
                    continue
                nx, ny = cur[0] + dx, cur[1] + dy
                if not (0 <= nx < W and 0 <= ny < H) or obst[ny, nx]:
                    continue
                if dx and dy and (obst[cur[1], nx] or obst[ny, cur[0]]):
                    continue           # 不要從斜角縫鑽過去
                ng = g[cur] + (1.414 if dx and dy else 1.0)
                if ng < g.get((nx, ny), 1e18):
                    g[(nx, ny)] = ng
                    came[(nx, ny)] = cur
                    heapq.heappush(openq, (ng + math.hypot(gx - nx, gy - ny), (nx, ny)))
    return None


_DIRS8 = [(math.cos(a), math.sin(a)) for a in [i * math.pi / 4 for i in range(8)]]
_DIRS16 = [(math.cos(a), math.sin(a)) for a in [i * math.pi / 8 for i in range(16)]]


def key_dir(d):
    """方向鍵實際按得出來的方向（跟 Mover 一樣：分量超過 0.38 才按）"""
    if d is None:
        return None
    x = (1 if d[0] > 0.38 else -1 if d[0] < -0.38 else 0)
    y = (1 if d[1] > 0.38 else -1 if d[1] < -0.38 else 0)
    if not x and not y:
        return None
    m = math.hypot(x, y)
    return x / m, y / m


def _norm(v):
    m = math.hypot(v[0], v[1])
    return (v[0] / m, v[1] / m) if m > 1e-6 else None


# ---------------------------------------------------------------------
#  小地圖
# ---------------------------------------------------------------------
class MiniMap:
    def __init__(self, base_dir, keys=True, log=print):
        self.dir = os.path.join(base_dir, "minimap")
        self.keys = keys
        self.log = log
        self.panel = None
        self.panel_ref = None     # 標題列平常的顏色（選單蓋住時會變暗 → 不更新）
        self.panel_next = 0
        self.panel_fail = 0
        self.name = None
        self.next_t = 0
        self.disabled = False
        self.err_logged = False
        self.ready_logged = False
        self._clear_model()
        self.new_game()

    # ---------- 狀態 ----------
    def _clear_model(self):
        self.bg = None
        self.seen = None
        self.wbins = np.zeros(4096, np.float32)   # 走得過去的顏色（走過的路、地上的掉落物）
        self.obins = np.zeros(4096, np.float32)   # 撞到過的顏色
        self.lut = None
        self.cover = None
        self.scale = None
        self.scale_obs = []
        self.loaded_name = None
        self.dirty = False
        self.visit = None
        self.block = None
        self.obst = None

    def new_game(self):
        """每一場戰鬥開始：先把上一場學到的存檔，等讀到這場的地圖名稱再載入那張地圖的地形
        （不同關卡的地形不會混在一起）"""
        self.disabled = False           # 上一場出錯關掉的，新的一場再試一次
        self.err_logged = False
        if self.name and self.dirty:
            self.save()
        self._clear_model()
        self.name = None
        self.next_t = 0
        self.pos, self.pos_t = None, 0.0
        self.trail = []
        self.hist = []
        self.visit = None
        self.block = None
        self.obst, self.obst_t = None, 0.0
        self.route_cache = None
        self.steer_last, self.steer_t = None, 0.0
        self.chest_pts, self.magnet_pts = [], []
        self.goal = None            # (x, y, 種類)
        self.goal_path, self.goal_path_t = None, 0.0
        self.goal_arrive_t = None
        self.goal_seen_t = 0.0
        self.goal_best, self.goal_best_t = None, 0.0
        self.goal_call_t = 0.0
        self.banned = []            # (x, y, 到什麼時候) 走不到／撿不到的圖示，先跳過
        self.goal_log_t = 0.0
        self.save_t = time.time() + 90
        self.stats = {"steer": 0, "reroute": 0, "unreach": 0, "goals": 0, "picked": 0}

    def _file(self, name):
        safe = re.sub(r"[^\w一-鿿]+", "_", name).strip("_") or "map"
        w, h = self.panel[0][2], self.panel[0][3]
        return os.path.join(self.dir, f"{safe}_{w}x{h}.npz")

    def set_name(self, name):
        """讀到地圖名稱：換地圖就存舊的、載入新的"""
        if not name or name == self.name or self.panel is None:
            return
        if self.name and self.dirty:
            self.save()
        old_bg, old_seen, old_w, old_o = self.bg, self.seen, self.wbins, self.obins
        old_scale = (self.scale, self.scale_obs)
        same_size = old_bg is not None and old_bg.shape[:2] == (self.panel[0][3], self.panel[0][2])
        keep = self.name is None and same_size      # 名稱還沒讀到前學的，算這張地圖的
        self._clear_model()
        self.name = name
        path = self._file(name)
        try:
            if os.path.exists(path):
                z = np.load(path)
                bg, seen = z["bg"], z["seen"]
                if bg.shape[:2] == (self.panel[0][3], self.panel[0][2]) and z["wbins"].shape == (4096,):
                    self.bg, self.seen = bg.copy(), seen.copy()
                    self.wbins = z["wbins"].astype(np.float32)
                    self.obins = z["obins"].astype(np.float32)
                    sc = float(z["scale"][0]) if "scale" in z.files else 0.0
                    if sc > 0:
                        self.scale, self.scale_obs = sc, [sc] * 5
                    self.loaded_name = name
                    self.log(f"小地圖：認得「{name}」的地形（之前學過），走路會避開牆壁")
        except Exception as e:
            self.log(f"小地圖：讀取「{name}」的地形失敗，重新學：{e}")
        if self.loaded_name is None:
            self.log(f"小地圖：第一次來「{name}」，邊走邊記地形（之後就會避開牆壁）")
        if keep:
            if self.bg is None:
                self.bg, self.seen = old_bg, old_seen
            else:
                self.bg[old_seen] = old_bg[old_seen]
                self.seen |= old_seen
            self.wbins += old_w
            self.obins += old_o
            self.scale, self.scale_obs = old_scale
        self.lut = None
        self.dirty = True

    def save(self):
        if not self.name or self.bg is None or not self.dirty or self.panel is None:
            return
        try:
            os.makedirs(self.dir, exist_ok=True)
            path = self._file(self.name)
            tmp = path + ".tmp.npz"
            np.savez_compressed(tmp, bg=self.bg, seen=self.seen, wbins=self.wbins, obins=self.obins,
                                scale=np.array([self.scale or 0.0], np.float32))
            os.replace(tmp, path)
            self.dirty = False
        except Exception as e:
            self.log(f"小地圖地形存檔失敗：{e}")

    # ---------- 每一張畫面 ----------
    def update(self, frame, now, world=None, name=None):
        if self.disabled:
            return
        try:
            self._update(frame, now, world, name)
        except Exception as e:
            if not self.err_logged:
                self.log(f"小地圖分析出錯（這場先不用小地圖）：{e}")
                self.err_logged = True
            self.disabled = True

    def _update(self, frame, now, world, name):
        if now < self.next_t:
            return
        self.next_t = now + 0.12
        if self.panel is None or now >= self.panel_next:
            p = find_panel(frame)
            self.panel_next = now + (5.0 if p else 1.5)
            if p:
                self.panel_fail = 0
                if self.panel is not None and all(abs(a - b) <= 8 for a, b in zip(p[0], self.panel[0])):
                    p = self.panel          # 只差幾個像素（量的誤差）→ 照舊，不重來
                if self.panel is None or p is not self.panel:
                    if self.panel is not None:
                        self.save()
                        self._clear_model()
                        self.name = None
                    self.panel = p
                tb = p[1]
                self.panel_ref = frame[tb[1] + tb[3] - 4:tb[1] + tb[3] - 1,
                                       tb[0] + tb[2] // 3:tb[0] + 2 * tb[2] // 3].reshape(-1, 3).mean(0)
            else:
                self.panel_fail += 1
                if self.panel_fail >= 4:
                    self.pos = None
                    return
        if self.panel is None:
            return
        (mx, my, mw, mh), tb = self.panel
        H, W = frame.shape[:2]
        if my + mh > H or mx + mw > W:
            self.panel = None
            return
        # 標題列顏色跟平常差很多 = 被選單蓋住、變暗了 → 這張不用
        cur = frame[tb[1] + tb[3] - 4:tb[1] + tb[3] - 1,
                    tb[0] + tb[2] // 3:tb[0] + 2 * tb[2] // 3].reshape(-1, 3).mean(0)
        if self.panel_ref is not None and np.abs(cur - self.panel_ref).max() > 18:
            return
        if name:
            self.set_name(name)
        mm = frame[my:my + mh, mx:mx + mw]
        red, white, gold = _masks(mm)
        self._find_icons(red, gold)

        # 自己的位置
        cands = find_arrow(mm, red, white)
        pos = None
        if cands:
            if self.pos is not None and now - self.pos_t < 1.5:
                c = min(cands, key=lambda o: math.hypot(o[0] - self.pos[0], o[1] - self.pos[1]))
                if math.hypot(c[0] - self.pos[0], c[1] - self.pos[1]) < 25:
                    pos = (c[0], c[1])
                elif len(cands) == 1 and now - self.pos_t > 0.6:
                    pos = (c[0], c[1])
            else:
                c = max(cands, key=lambda o: o[2])
                pos = (c[0], c[1])
        if pos is not None:
            self.pos, self.pos_t = pos, now

        # 圖示蓋住的地方（自己、寶箱、磁鐵、怪物點）不能拿來記地形
        cover = cv2.dilate((red | white).astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
        n, _lab, st, _ = cv2.connectedComponentsWithStats(gold.astype(np.uint8))
        for i in range(1, n):
            x, y, w, h, _a = st[i]
            cover[max(0, y - 4):y + h + 5, max(0, x - 4):x + w + 5] = True
        if self.pos is not None and now - self.pos_t < 1.0:
            cv2.circle(cover.view(np.uint8), (int(self.pos[0]), int(self.pos[1])), 12, 1, -1)

        self.cover = cover
        if self.bg is None or self.bg.shape != mm.shape:
            self.bg = mm.copy()
            self.seen = np.zeros(mm.shape[:2], bool)
        upd = ~cover
        # 跟記住的地形差很多（小地圖被放大縮小、換了地圖）→ 地形重新記
        both = upd & self.seen
        if both.sum() > 2000:
            diff = np.abs(mm.astype(np.int16) - self.bg.astype(np.int16)).sum(axis=2)
            if (diff[both] > 70).mean() > 0.3:
                self.seen[:] = False
                self.lut = None
        self.bg[upd] = mm[upd]
        newly = upd & ~self.seen
        self.seen |= upd
        if newly.any():
            self.dirty = True

        if self.pos is None or now - self.pos_t > 1.0:
            return
        px, py = self.pos
        # 走過的路
        if self.visit is None or self.visit.shape != mm.shape[:2] or self.block is None \
                or self.block.shape != mm.shape[:2]:
            self.visit = np.zeros(mm.shape[:2], np.uint8)
            self.block = np.zeros(mm.shape[:2], np.uint8)
        if not self.trail or math.hypot(px - self.trail[-1][1], py - self.trail[-1][2]) >= 1.5:
            self.trail.append([now, px, py, False])
            cv2.circle(self.visit, (int(px), int(py)), 3, 1, -1)
            if len(self.trail) > 600:
                del self.trail[:100]
        # 箭頭離開後，剛剛走過的地方露出來 → 那裡的顏色 = 可以走
        blur = None
        for t in self.trail:
            if t[3] or now - t[0] < 0.6:
                continue
            tx, ty = int(round(t[1])), int(round(t[2]))
            if math.hypot(tx - px, ty - py) < 12:
                continue
            if not (1 <= tx < mm.shape[1] - 1 and 1 <= ty < mm.shape[0] - 1):
                t[3] = True
                continue
            if cover[ty, tx] or not self.seen[ty, tx]:
                if now - t[0] > 20:
                    t[3] = True
                continue
            if blur is None:
                blur = cv2.blur(self.bg, (3, 3))
            self.wbins[_bins(blur[ty:ty + 1, tx:tx + 1])[0, 0]] += 1
            t[3] = True
            self.lut = None
            self.dirty = True
        # 畫面移動量 ÷ 小地圖移動量 = 比例（換算畫面上東西在小地圖的位置用）
        if world is not None:
            self.hist.append((now, px, py, world[0], world[1]))
            while self.hist and now - self.hist[0][0] > 6:
                self.hist.pop(0)
            old = next((h for h in self.hist if now - h[0] >= 1.5), None)
            if old is not None:
                dp = math.hypot(px - old[1], py - old[2])
                dw = math.hypot(world[0] - old[3], world[1] - old[4])
                if dp >= 5 and dw > 40:
                    self.scale_obs.append(dw / dp)
                    del self.scale_obs[:-25]
                    if len(self.scale_obs) >= 5:
                        self.scale = float(np.median(self.scale_obs))
                    self.hist = [h for h in self.hist if h[0] > old[0]]

    # ---------- 地形 ----------
    def _obstacles(self, now):
        if self.obst is not None and now - self.obst_t < 0.5 and self.lut is not None:
            return self.obst
        if self.lut is None:
            self.lut, self.wlut = self._classify()
        blur = cv2.blur(self.bg, (3, 3))
        b = _bins(blur)
        obst = self.lut[b] & self.seen
        # 比旁邊地板暗很多的小塊（坑洞、石頭、柱子）：顏色規則抓不到小東西，用「跟周圍比」再抓一次
        v = cv2.cvtColor(blur, cv2.COLOR_BGR2HSV)[..., 2].astype(np.float32)
        loc = cv2.blur(v, (15, 15))
        obst |= (v < 0.7 * loc) & self.seen & ~self.wlut[b]
        # 單獨一兩個點的雜訊不算障礙（細細的牆是一長條，不會被刪掉）
        n, lab, st, _ = cv2.connectedComponentsWithStats(obst.astype(np.uint8), connectivity=8)
        small = np.zeros(n, bool)
        small[1:] = st[1:, 4] <= 2
        obst = obst & ~small[lab]
        if self.block is not None:
            if self.block.shape == obst.shape:
                obst = obst | (self.block > 0)
            else:
                self.block = None
        self.obst, self.obst_t = obst, now
        return obst

    def _classify(self):
        """每個顏色：能走 / 不能走。沒把握的顏色當作能走（最多就是跟以前一樣撞到，不會被困住）"""
        Wd, Od = _maxf3(self.wbins), _maxf3(self.obins)
        tot = float(self.wbins.sum())
        walk = Wd >= max(2.0, tot * 0.002)
        obst = (Od >= 2) & ~(walk & (Wd >= Od))          # 撞到過好幾次的顏色
        bad = _PRIOR.copy()
        if tot >= 25:                                     # 比地板暗很多、又沒走過的顏色（樹、石頭、坑洞）
            v = _BIN_HSV[:, 2]
            order = np.argsort(v)
            cw = np.cumsum(self.wbins[order])
            v_floor = v[order][np.searchsorted(cw, cw[-1] / 2)]
            bad |= (v < 0.6 * v_floor) & (Wd <= 0)
        return obst | (bad & ~walk), walk

    def samples(self):
        return int(self.wbins.sum())

    def ready(self, now):
        """位置是新的、地形也學到一些了，才拿來導航"""
        if self.disabled or self.pos is None or now - self.pos_t > 0.8 or self.bg is None:
            return False
        obst = self._obstacles(now)
        px, py = int(self.pos[0]), int(self.pos[1])
        y0, y1 = max(0, py - 12), min(obst.shape[0], py + 13)
        x0, x1 = max(0, px - 12), min(obst.shape[1], px + 13)
        free = 1.0 - obst[y0:y1, x0:x1].mean()
        if free < 0.15:     # 附近幾乎都說不能走 → 大概判斷錯了，先不用
            return False
        if not self.ready_logged:
            self.ready_logged = True
            self.log("小地圖導航啟動：前面有牆會先轉彎")
        return True

    def free_dist(self, d, maxr, start=None):
        obst = self._obstacles(time.time())
        x, y = start if start is not None else self.pos
        H, W = obst.shape
        r = 1.0
        while r <= maxr:
            px, py = int(round(x + d[0] * r)), int(round(y + d[1] * r))
            if not (0 <= px < W and 0 <= py < H) or obst[py, px]:
                return r - 0.5
            r += 0.5
        return maxr

    def _cands(self):
        return _DIRS8 if self.keys else _DIRS16

    # ---------- 給移動用的 ----------
    def steer(self, d, need=None):
        """d：想走的方向。前面 need 個小地圖像素內有障礙 → 換成最接近、走得通的方向"""
        if d is None:
            return d
        need = min(LOOK, need if need is not None else LOOK)
        if need < 1.5:
            return d
        q = key_dir(d) if self.keys else _norm(d)
        if q is None:
            return d
        if self.free_dist(q, need) >= need - 0.01:
            self.steer_last = None
            return d
        cands = sorted(self._cands(), key=lambda c: -(c[0] * d[0] + c[1] * d[1]))
        # 剛換過的方向還走得通，就多走一下（避免上、下來回抖）
        if self.steer_last is not None and self.free_dist(self.steer_last, need) >= need - 0.01:
            dot = self.steer_last[0] * d[0] + self.steer_last[1] * d[1]
            if dot > 0 or (dot > -0.3 and time.time() - self.steer_t < 0.8):
                return self.steer_last
        best, best_f = None, self.free_dist(q, need)
        for c in cands:
            if c[0] * d[0] + c[1] * d[1] < -0.72:        # 不要往回走超過 135°
                break
            f = self.free_dist(c, need)
            if f >= need - 0.01:
                self.steer_last, self.steer_t = c, time.time()
                self.stats["steer"] += 1
                return c
            if f > best_f + 1:
                best, best_f = c, f
        if best is not None:
            self.steer_last, self.steer_t = best, time.time()
            self.stats["steer"] += 1
            return best
        return d

    def explore_dir(self, cur=None):
        """沒有東西可撿時要往哪走：前面空曠、又還沒走過的方向"""
        obst = self._obstacles(time.time())
        best, best_s = None, -1e9
        for c in self._cands():
            f = self.free_dist(c, 30)
            if f < 4:
                continue
            new = tot = 0
            r = 3.0
            while r <= f:
                x, y = int(self.pos[0] + c[0] * r), int(self.pos[1] + c[1] * r)
                if 0 <= y < obst.shape[0] and 0 <= x < obst.shape[1]:
                    tot += 1
                    new += self.visit is None or not self.visit[y, x]
                r += 1.0
            nov = new / tot if tot else 0
            s = f / 30 + 0.8 * nov + random.uniform(0, 0.15)
            if cur is not None:
                s += 0.35 * (c[0] * cur[0] + c[1] * cur[1])
            if s > best_s:
                best, best_s = c, s
        return best

    def escape_dir(self, d):
        """撞到東西：記住那裡，往最空曠、跟原本方向差 90° 以上的方向離開"""
        best, best_s = None, -1e9
        for c in self._cands():
            if d is not None and c[0] * d[0] + c[1] * d[1] > 0.3:
                continue
            f = self.free_dist(c, 15)
            s = f + random.uniform(0, 2.0)
            if s > best_s:
                best, best_s = c, s
        return best

    # ---------- 小地圖上的寶箱、磁鐵 ----------
    def _find_icons(self, red, gold):
        """寶箱 = 金色圖示；磁鐵 = 紅色 U 字（上面中間是空的，自己的箭頭是實心三角形）"""
        chests = []
        g = cv2.dilate(gold.astype(np.uint8), np.ones((3, 3), np.uint8))
        n, _lab, st, _ = cv2.connectedComponentsWithStats(g)
        for i in range(1, n):
            x, y, w, h, a = st[i]
            if 8 <= w <= 60 and 6 <= h <= 60 and a >= 30:
                chests.append((x + w / 2.0, y + h / 2.0))
        magnets = []
        n, lab, st, _ = cv2.connectedComponentsWithStats(red.astype(np.uint8))
        for i in range(1, n):
            x, y, w, h, a = st[i]
            if not (8 <= w <= 20 and 5 <= h <= 14 and 25 <= a <= 140):
                continue
            comp = lab[y:y + h, x:x + w] == i
            if comp[:max(1, h // 2), w // 3:w - w // 3].mean() > 0.35:
                continue
            cx, cy = x + w / 2.0, y + h / 2.0
            if self.pos is not None and math.hypot(cx - self.pos[0], cy - self.pos[1]) < 8:
                continue        # 自己的箭頭
            magnets.append((cx, cy))
        self.chest_pts, self.magnet_pts = chests, magnets

    def _is_banned(self, x, y, now):
        self.banned = [b for b in self.banned if b[2] > now]
        return any(math.hypot(x - bx, y - by) < 5 for bx, by, _ in self.banned)

    def magnet_near(self):
        if self.pos is None:
            return False
        g = self.goal          # 正在去撿的磁鐵（快到時會被自己的箭頭蓋住，看不到也算）
        if g is not None and g[2] == "magnet" and math.hypot(g[0] - self.pos[0], g[1] - self.pos[1]) <= MAGNET_NEAR:
            return True
        return any(math.hypot(x - self.pos[0], y - self.pos[1]) <= MAGNET_NEAR for x, y in self.magnet_pts)

    def goal_close(self):
        """快走到寶箱／磁鐵了（最後幾步直接走過去）"""
        g = self.goal
        return g is not None and self.pos is not None and math.hypot(g[0] - self.pos[0], g[1] - self.pos[1]) < 6

    def goal_dir(self, now, only_magnet=False):
        """畫面上沒東西撿時：往小地圖上最近的磁鐵／寶箱走（磁鐵優先）。回傳方向或 None"""
        if self.pos is None:
            return None
        px, py = self.pos
        if now - self.goal_call_t > 1.0:      # 中間去撿畫面上的東西了 → 重新計時
            self.goal_best, self.goal_best_t, self.goal_arrive_t = None, now, None
        self.goal_call_t = now
        cands = [(x, y, "magnet") for x, y in self.magnet_pts]
        if not only_magnet:
            cands += [(x, y, "chest") for x, y in self.chest_pts]
        cands = [c for c in cands if not self._is_banned(c[0], c[1], now)]
        if only_magnet:
            cands = [c for c in cands if math.hypot(c[0] - px, c[1] - py) <= MAGNET_NEAR]
        g = self.goal
        if g is not None and only_magnet and g[2] != "magnet":
            g = self.goal = None            # 只找磁鐵的時候，原本要去的寶箱先放著
            self.goal_path, self.goal_arrive_t = None, None
        if g is not None:
            same = [c for c in cands if c[2] == g[2] and math.hypot(c[0] - g[0], c[1] - g[1]) < 4]
            gd = math.hypot(g[0] - px, g[1] - py)
            if same:
                if math.hypot(same[0][0] - g[0], same[0][1] - g[1]) > 1.5:
                    self.goal_path_t = 0
                g = self.goal = same[0]
                self.goal_seen_t = now
            elif gd < 11 and now - self.goal_seen_t < 6 and gd >= 1.5:
                pass    # 靠近時圖示被自己的箭頭蓋住了 → 照記住的位置繼續走過去
            else:
                if gd < 11:
                    self.stats["picked"] += 1      # 走到了／圖示消失了 = 撿到了
                g = self.goal = None
                self.goal_path, self.goal_arrive_t = None, None
        if g is None:
            if not cands:
                return None
            val = {"magnet": 3.0, "chest": 1.0}
            g = self.goal = max(cands, key=lambda c: val[c[2]] / (math.hypot(c[0] - px, c[1] - py) + 8))
            self.goal_path, self.goal_path_t, self.goal_arrive_t = None, 0.0, None
            self.goal_seen_t = now
            self.goal_best, self.goal_best_t = None, now
            self.stats["goals"] += 1
            if now - self.goal_log_t > 10:
                self.goal_log_t = now
                self.log("小地圖：畫面上沒東西撿 → 往" + ("磁鐵" if g[2] == "magnet" else "寶箱") + "走"
                         if not only_magnet else "小地圖：附近有磁鐵 → 先去撿")
        gx, gy = g[0], g[1]
        dist = math.hypot(gx - px, gy - py)
        # 6 秒都沒有更接近 → 大概走不過去，先跳過這個
        if self.goal_best is None or dist < self.goal_best - 1.5:
            self.goal_best, self.goal_best_t = dist, now
        elif now - self.goal_best_t > 6:
            self.banned.append((gx, gy, now + 60))
            self.stats["unreach"] += 1
            self.goal, self.goal_path = None, None
            return None
        # 走到了圖示卻一直還在 → 撿不到，先跳過
        if dist < 2.5:
            if self.goal_arrive_t is None:
                self.goal_arrive_t = now
            elif now - self.goal_arrive_t > 4:
                self.banned.append((gx, gy, now + 60))
                self.goal = None
                return None
        if self.goal_path is None or now - self.goal_path_t > 1.2:
            obst = self._obstacles(now).copy()
            H, W = obst.shape
            sx, sy = int(min(max(px, 0), W - 1)), int(min(max(py, 0), H - 1))
            ex, ey = int(min(max(gx, 0), W - 1)), int(min(max(gy, 0), H - 1))
            obst[max(0, sy - 2):sy + 3, max(0, sx - 2):sx + 3] = False
            obst[max(0, ey - 3):ey + 4, max(0, ex - 3):ex + 4] = False   # 圖示蓋住的地方
            path = astar(obst, (sx, sy), (ex, ey), max_nodes=25000)
            self.goal_path_t = now
            if path is None:
                self.stats["unreach"] += 1
                self.banned.append((gx, gy, now + 30))     # 走不到 → 30 秒內先不去
                self.goal, self.goal_path = None, None
                return None
            self.goal_path = path
        if dist < 6:        # 最後幾步：直接走過去（圖示底下的地形看不到，不要被當成牆）
            return _norm((gx - px, gy - py))
        path = self.goal_path
        i0 = min(range(len(path)), key=lambda i: math.hypot(path[i][0] - px, path[i][1] - py))
        wp = next((p for p in path[i0:] if math.hypot(p[0] - px, p[1] - py) >= 4), path[-1])
        nd = _norm((wp[0] - px, wp[1] - py))
        if nd is None:
            nd = _norm((gx - px, gy - py))
        if nd is None:
            return None
        return self.steer(nd, need=min(LOOK, math.hypot(wp[0] - px, wp[1] - py)))

    def mark_block(self, d, now):
        """撞到東西的地方（自己前面一點）記成障礙，這場都會避開"""
        if self.pos is None or now - self.pos_t > 1.0 or d is None or self.block is None:
            return
        H, W = self.block.shape
        blur = cv2.blur(self.bg, (3, 3)) if self.bg is not None else None
        for k in (2.0, 3.0, 4.5, 6.0):
            x, y = int(round(self.pos[0] + d[0] * k)), int(round(self.pos[1] + d[1] * k))
            cv2.circle(self.block, (x, y), 1, 1, -1)
            # 記住撞到的顏色（同一張地圖的牆通常同一個顏色）
            if k <= 4.5 and blur is not None and 0 <= x < W and 0 <= y < H and self.seen[y, x] \
                    and (self.cover is None or not self.cover[y, x]):
                self.obins[_bins(blur[y:y + 1, x:x + 1])[0, 0]] += 1
                self.lut = None
                self.dirty = True
        self.obst = None
        self.goal_path_t = 0        # 重新規劃去寶箱／磁鐵的路

    def add_items(self, pts, center, now):
        """畫面上的金幣／礦石／寶箱都掉在走得到的地方 → 那裡的顏色算能走"""
        if self.pos is None or now - self.pos_t > 0.5 or self.bg is None or len(self.scale_obs) < 5 \
                or self.cover is None or now < getattr(self, "items_next", 0):
            return
        self.items_next = now + 0.5
        H, W = self.seen.shape
        blur = None
        for x, y in pts[:30]:
            dx, dy = x - center[0], y - center[1]
            if math.hypot(dx, dy) > 350:
                continue
            mx, my = int(round(self.pos[0] + dx / self.scale)), int(round(self.pos[1] + dy / self.scale))
            if not (0 <= mx < W and 0 <= my < H) or not self.seen[my, mx] or self.cover[my, mx]:
                continue
            if blur is None:
                blur = cv2.blur(self.bg, (3, 3))
            self.wbins[_bins(blur[my:my + 1, mx:mx + 1])[0, 0]] += 0.25
            self.lut = None

    def route(self, d, dist_screen, now):
        """往畫面上的目標走：中間有障礙就在小地圖上繞路；完全走不到回傳 None"""
        if d is None:
            return d
        s = self.scale or DEFAULT_SCALE
        r = dist_screen / s
        if r < 2.0:
            return d
        rr = min(r, 40.0)
        if self.free_dist(d, rr) >= rr - 0.6:
            return self.steer(d, need=r)
        px, py = self.pos
        gx, gy = px + d[0] * r, py + d[1] * r
        rc = self.route_cache
        if rc and now - rc[0] < 0.6 and math.hypot(rc[1][0] - gx, rc[1][1] - gy) < 4 and \
                math.hypot(rc[2][0] - px, rc[2][1] - py) < 3:
            path = rc[3]
        else:
            obst = self._obstacles(now)
            H, W = obst.shape
            x0, y0 = int(max(0, min(px, gx) - 25)), int(max(0, min(py, gy) - 25))
            x1, y1 = int(min(W, max(px, gx) + 26)), int(min(H, max(py, gy) + 26))
            win = obst[y0:y1, x0:x1].copy()
            sx, sy = int(px) - x0, int(py) - y0
            ex = int(min(max(gx - x0, 0), win.shape[1] - 1))
            ey = int(min(max(gy - y0, 0), win.shape[0] - 1))
            win[max(0, sy - 2):sy + 3, max(0, sx - 2):sx + 3] = False   # 自己站的地方
            win[max(0, ey - 2):ey + 3, max(0, ex - 2):ex + 3] = False   # 目標附近
            p = astar(win, (sx, sy), (ex, ey))
            path = [(x + x0, y + y0) for x, y in p] if p else None
            self.route_cache = (now, (gx, gy), (px, py), path)
            if path is None:
                self.stats["unreach"] += 1
            else:
                self.stats["reroute"] += 1
        if path is None:
            return None
        wp = next((p for p in path if math.hypot(p[0] - px, p[1] - py) >= 4), path[-1])
        nd = _norm((wp[0] - px, wp[1] - py))
        if nd is None:
            return d
        return self.steer(nd, need=math.hypot(wp[0] - px, wp[1] - py))

    # ---------- 除錯圖 ----------
    def debug_image(self, d=None):
        if self.bg is None:
            return None
        obst = self._obstacles(time.time())
        img = self.bg.copy()
        img[obst] = (img[obst] * 0.4 + np.array([0, 0, 200]) * 0.6).astype(np.uint8)
        img[~self.seen] = (img[~self.seen] * 0.5).astype(np.uint8)
        img = cv2.resize(img, None, fx=3, fy=3, interpolation=cv2.INTER_NEAREST)
        for t in self.trail[-200:]:
            cv2.circle(img, (int(t[1] * 3), int(t[2] * 3)), 1, (255, 255, 0), -1)
        for x, y in self.chest_pts:
            cv2.rectangle(img, (int(x * 3) - 15, int(y * 3) - 12), (int(x * 3) + 15, int(y * 3) + 12), (0, 215, 255), 2)
        for x, y in self.magnet_pts:
            cv2.circle(img, (int(x * 3), int(y * 3)), 16, (255, 0, 255), 2)
        if self.goal_path:
            for a, b in zip(self.goal_path, self.goal_path[1:]):
                cv2.line(img, (a[0] * 3, a[1] * 3), (b[0] * 3, b[1] * 3), (0, 255, 0), 2)
        if self.pos is not None:
            p = (int(self.pos[0] * 3), int(self.pos[1] * 3))
            cv2.circle(img, p, 6, (0, 255, 0), 2)
            if d is not None:
                cv2.arrowedLine(img, p, (int(p[0] + d[0] * 30), int(p[1] + d[1] * 30)), (0, 255, 255), 2)
        txt = f"{self.name or '?'} samples={self.samples()} scale={self.scale or 0:.1f}"
        cv2.putText(img, txt.encode("ascii", "replace").decode(), (4, 14), cv2.FONT_HERSHEY_SIMPLEX,
                    0.45, (255, 255, 255), 1)
        return img


# ---------------------------------------------------------------------
#  保險：小地圖任何地方出錯，只會關掉這場的小地圖功能，不會讓整個程式停掉
# ---------------------------------------------------------------------
def _safe(name, default):
    fn = getattr(MiniMap, name)

    def wrapper(self, *a, **k):
        if self.disabled:
            return default(a) if callable(default) else default
        try:
            return fn(self, *a, **k)
        except Exception as e:
            if not self.err_logged:
                self.log(f"小地圖出錯（這場先不用小地圖，照舊走路）：{e}")
                self.err_logged = True
            self.disabled = True
            return default(a) if callable(default) else default
    wrapper.__name__ = name
    setattr(MiniMap, name, wrapper)


for _n, _d in (("ready", False), ("steer", lambda a: a[0] if a else None),
               ("route", lambda a: a[0] if a else None), ("goal_dir", None), ("explore_dir", None),
               ("escape_dir", None), ("mark_block", None), ("add_items", None), ("magnet_near", False),
               ("goal_close", False), ("debug_image", None), ("save", None),
               ("free_dist", lambda a: a[1] if len(a) > 1 else 0.0)):
    _safe(_n, _d)
