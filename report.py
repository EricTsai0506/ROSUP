"""
report.py — 每日報表

每打完一場記一筆：打到第幾分鐘、有沒有倒下、可能被什麼打倒、哪隻頭目扣最多血。
  logs\\report_data\\YYYYMMDD.jsonl   原始資料（程式自己用）
  logs\\report_YYYYMMDD.csv           每場一列，可以用 Excel 開
  logs\\report_YYYYMMDD.txt           當天的摘要（每打完一場自動更新）
"""
import csv
import json
import os
import time
from collections import Counter, defaultdict

COLS = ["日期", "開始", "結束", "關卡", "結果", "遊戲時間", "倒下時間", "可能原因", "最後HP",
        "最低HP%", "角色等級", "扣血最多的頭目", "該頭目扣血%", "頭目紀錄", "實際花費"]

# 已知的頭目（遊戲裡的寫法）。文字辨識常讀成簡體字、少字、後面黏數字（舞狮678、邪浪人、白素真）
KNOWN_BOSSES = ["舞獅", "猴桃木", "邪骸浪人", "機木人", "元宵童子", "白素貞", "蛇王", "彈簧兔",
                "月夜貓", "狂暴月夜貓", "海盜之王", "妖君", "邪骸士長", "梅納海葵"]      # 後面這幾隻是遠征關卡的
_S2T = str.maketrans("狮弹贞机猫盗纳", "獅彈貞機貓盜納")
NO_NAME = "（沒讀到名字）"


def boss_name(nm):
    """頭目名字校正：去掉數字符號、簡體換繁體，跟已知的頭目比，夠像就用已知的寫法"""
    import difflib
    import re
    nm = re.sub(r"[^\u4e00-\u9fff]", "", (nm or "")).translate(_S2T)
    if not nm:
        return NO_NAME
    best, sc = None, 0.0
    for k in KNOWN_BOSSES:
        r = difflib.SequenceMatcher(None, nm, k).ratio()
        if k in nm or (len(nm) >= 2 and nm in k):
            r = max(r, 0.9)
        if r > sc:
            best, sc = k, r
    return best if sc >= 0.6 else nm


def merged_bosses(rec):
    """同一局、同一隻頭目只算一筆（血條忽隱忽現會被記成很多次「出現」）：
    出現時間取第一次、出現時的 HP 取第一次、最低 HP 取全部的最低。
    沒讀到名字的，併到時間最接近（前後 90 秒內）的那一隻"""
    out = {}
    unnamed = []
    for b in rec.get("bosses", []):
        nm = boss_name(b.get("name"))
        if nm == NO_NAME:
            unnamed.append(b)
            continue
        m = out.get(nm)
        if m is None:
            out[nm] = dict(b, name=nm, last_t=b.get("t") or 0)
            continue
        m["last_t"] = max(m["last_t"], b.get("t") or 0)
        if m.get("pct0") is None:
            m["pct0"] = b.get("pct0")
        if b.get("min_pct") is not None:
            m["min_pct"] = b["min_pct"] if m.get("min_pct") is None else min(m["min_pct"], b["min_pct"])
    rest = None
    for b in unnamed:
        t = b.get("t") or 0
        near = [m for m in out.values() if m.get("t", 0) - 90 <= t <= m["last_t"] + 90]
        if near:
            m = min(near, key=lambda m_: abs((m_.get("t") or 0) - t))
        else:
            if rest is None:
                rest = dict(b, name=NO_NAME, last_t=t)
                continue
            m = rest
        if b.get("min_pct") is not None:
            m["min_pct"] = b["min_pct"] if m.get("min_pct") is None else min(m["min_pct"], b["min_pct"])
    res = sorted(out.values(), key=lambda m_: m_.get("t") or 0)
    if rest is not None and lost_pct(rest):
        res.append(rest)
    return res


def mmss(sec):
    if sec is None:
        return ""
    sec = int(sec)
    return f"{sec // 60}:{sec % 60:02d}"


def _paths(log_dir, day):
    return (os.path.join(log_dir, "report_data", f"{day}.jsonl"),
            os.path.join(log_dir, f"report_{day}.csv"),
            os.path.join(log_dir, f"report_{day}.txt"))


def load(log_dir, day):
    path = _paths(log_dir, day)[0]
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        pass
    except FileNotFoundError:
        pass
    return rows


def record(log_dir, rec, boss_times=None):
    """存一場的紀錄，重新產生當天的 csv 和摘要。回傳摘要檔路徑"""
    day = time.strftime("%Y%m%d", time.localtime(rec.get("end", time.time())))
    data, csv_path, txt_path = _paths(log_dir, day)
    os.makedirs(os.path.dirname(data), exist_ok=True)
    with open(data, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    rows = load(log_dir, day)
    try:      # Excel 開著這個檔的時候會寫不進去 → 這次先跳過，下一場再一起補上
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(COLS)
            for r in rows:
                w.writerow(csv_row(r))
    except OSError:
        pass
    try:
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(summary(rows, day, boss_times))
    except OSError:
        pass
    return txt_path


def lost_pct(b):
    """這隻頭目在場時，HP 從出現時掉到最低點掉了最大 HP 的幾 %"""
    p0, pm = b.get("pct0"), b.get("min_pct")
    if p0 is None or pm is None:
        return None
    return max(0.0, p0 - pm) * 100


def worst_boss(rec):
    best = None
    for b in merged_bosses(rec):
        lp = lost_pct(b)
        if lp is not None and (best is None or lp > best[1]):
            best = (b, lp)
    return best


def csv_row(r):
    t0, t1 = r.get("start"), r.get("end")
    wb = worst_boss(r)
    bosses = "；".join(f"{boss_name(b.get('name'))} {mmss(b.get('t'))}"
                      + (f" -{lost_pct(b):.0f}%" if lost_pct(b) is not None else "")
                      for b in merged_bosses(r))
    hp = r.get("last_hp")
    return [time.strftime("%Y-%m-%d", time.localtime(t1)) if t1 else "",
            time.strftime("%H:%M:%S", time.localtime(t0)) if t0 else "",
            time.strftime("%H:%M:%S", time.localtime(t1)) if t1 else "",
            r.get("stage", ""), r.get("result", ""), mmss(r.get("max_timer")),
            mmss(r.get("max_timer")) if r.get("result") == "倒下" else "",
            r.get("cause", ""),
            f"{hp[0]}/{hp[1]}" if hp else "",
            f"{r['hp_min_pct'] * 100:.0f}" if r.get("hp_min_pct") is not None else "",
            r.get("char_lv") or "",
            boss_name(wb[0].get("name")) if wb else "",
            f"{wb[1]:.0f}" if wb else "",
            bosses,
            mmss(t1 - t0) if t0 and t1 else ""]


def _bucket(sec, times):
    """倒下的時間落在哪個區間（用頭目出現的時間切）"""
    edges = [0] + sorted(t for t in times if t > 0)
    for a, b in zip(edges, edges[1:]):
        if a <= sec < b:
            return (a, f"{mmss(a)}～{mmss(b)}")
    return (edges[-1], f"{mmss(edges[-1])} 以後")


def slow_rows(rows):
    """花特別久的場次：[(那一場, 同一關平常花多久)]。同一關至少 3 場才比；比中位數多 60% 而且多 6 分鐘以上才算"""
    by = defaultdict(list)
    for r in rows:
        if r.get("start") and r.get("end"):
            by[r.get("stage") or "？"].append(r)
    out = []
    for rs in by.values():
        if len(rs) < 3:
            continue
        ds = sorted(r["end"] - r["start"] for r in rs)
        med = ds[len(ds) // 2]
        out += [(r, med) for r in rs if r["end"] - r["start"] > max(med * 1.6, med + 360)]
    out.sort(key=lambda x: x[0]["start"])
    return out


def summary(rows, day, boss_times=None):
    times = boss_times or [150, 330, 450, 630, 750, 840]
    n = len(rows)
    head = f"{day[:4]}-{day[4:6]}-{day[6:]} 掛機報表（更新於 {time.strftime('%H:%M')}）"
    if not n:
        return head + "\n\n今天還沒有打完的場次\n"
    ok = sum(1 for r in rows if r.get("result") == "通關")
    dead = [r for r in rows if r.get("result") == "倒下"]
    unk = n - ok - len(dead)
    L = [head, ""]
    L.append(f"共 {n} 場：通關 {ok} 場、倒下 {len(dead)} 場" + (f"、結果不確定 {unk} 場" if unk else "")
             + f"（通關率 {ok * 100 // n}%）")
    durs = [r["end"] - r["start"] for r in rows if r.get("start") and r.get("end")]
    if durs:
        L.append(f"平均每場實際花 {mmss(sum(durs) / len(durs))}")
    # 各關卡分開看（一天裡換過關卡的話，合在一起的通關率看不出問題在哪一關）
    by_stage = defaultdict(list)
    for r in rows:
        by_stage[r.get("stage") or "？"].append(r)
    if len(by_stage) > 1:
        L += ["", "■ 各關卡"]
        for st, rs in by_stage.items():
            o = sum(1 for r in rs if r.get("result") == "通關")
            dd = sum(1 for r in rs if r.get("result") == "倒下")
            ds = [r["end"] - r["start"] for r in rs if r.get("start") and r.get("end")]
            L.append(f"  {st}：{len(rs)} 場、通關 {o}、倒下 {dd}（通關率 {o * 100 // len(rs)}%）"
                     + (f"、平均每場實際花 {mmss(sum(ds) / len(ds))}" if ds else ""))
    # 花特別久的場次：遊戲本身跑得慢（螢幕關閉、電腦省電、被別的程式佔用…），不是打得久
    slow = slow_rows(rows)
    if slow:
        tot = sum(r["end"] - r["start"] - med for r, med in slow)
        L += ["", f"■ 跑得特別慢的場次：{len(slow)} 場，比平常多花了約 {mmss(tot)}"
                  "（遊戲本身變慢：螢幕關閉、電腦省電、被別的程式佔用…）"]
        for r, med in slow[:12]:
            L.append(f"  {time.strftime('%H:%M', time.localtime(r['start']))}～"
                     f"{time.strftime('%H:%M', time.localtime(r['end']))}  實際花 {mmss(r['end'] - r['start'])}"
                     f"（同一關平常 {mmss(med)}）")
    if dead:
        L += ["", "■ 倒下的時間（遊戲計時，用頭目出現的時間分段）"]
        bk = Counter(_bucket(r.get("max_timer") or 0, times) for r in dead)
        for (a, label), c in sorted(bk.items()):
            L.append(f"  {label}：{c} 場")
        L += ["", "■ 倒下的可能原因"]
        for cause, c in Counter(r.get("cause") or "不明" for r in dead).most_common():
            L.append(f"  {cause}：{c} 場")
    agg = defaultdict(list)
    for r in rows:
        for b in merged_bosses(r):
            lp = lost_pct(b)
            if lp is not None:
                agg[b["name"]].append(lp)
    if agg:
        L += ["", "■ 頭目扣血排行（每場平均扣掉最大 HP 的 %，越上面越危險）"]
        # 只出現過一次、名字不認得、又沒扣到血的（多半是讀錯的字）不列
        agg = {nm: v for nm, v in agg.items() if nm in KNOWN_BOSSES or len(v) > 1 or max(v) >= 1}
        for nm, v in sorted(agg.items(), key=lambda kv: -sum(kv[1]) / len(kv[1]))[:10]:
            L.append(f"  {nm}：平均 {sum(v) / len(v):.0f}%、最多 {max(v):.0f}%（{len(v)} 場有遇到）")
    if dead:
        top_cause, tc = Counter(r.get("cause") or "不明" for r in dead).most_common(1)[0]
        (a, label), bc = Counter(_bucket(r.get("max_timer") or 0, times) for r in dead).most_common(1)[0]
        L += ["", f"■ 最需要加強：最常倒在 {label}（{bc} 場），原因最常是「{top_cause}」（{tc} 場）"]
    L += ["", "■ 每一場"]
    multi = len(by_stage) > 1
    for r in rows:
        wb = worst_boss(r)
        L.append(f"  {time.strftime('%H:%M', time.localtime(r['end'])) if r.get('end') else '':>5} "
                 f"{r.get('result', ''):<4} 計時 {mmss(r.get('max_timer')):>5}"
                 + (f"  實際 {mmss(r['end'] - r['start']):>5}" if r.get("start") and r.get("end") else "")
                 + (f"  {(r.get('stage') or '').replace('遠征：', '')}" if multi else "")
                 + (f"  原因：{r['cause']}" if r.get("result") == "倒下" and r.get("cause") else "")
                 + (f"  扣最多：{boss_name(wb[0].get('name'))} {wb[1]:.0f}%" if wb and wb[1] >= 1 else "")
                 + (f"  Lv {r['char_lv']}" if r.get("char_lv") else ""))
    L += ["", "（每場的詳細資料在同一個資料夾的 report_日期.csv，可以用 Excel 開）", ""]
    return "\n".join(L)
