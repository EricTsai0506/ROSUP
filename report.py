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

# 文字辨識常把頭目名字讀成簡體字 → 報表上換回遊戲裡的寫法
BOSS_TC = {"舞狮": "舞獅", "弹簧兔": "彈簧兔", "白素贞": "白素貞", "机木人": "機木人",
           "猴桃木": "猴桃木", "邪骸浪人": "邪骸浪人", "元宵童子": "元宵童子", "蛇王": "蛇王"}


def boss_name(nm):
    nm = (nm or "").strip()
    return BOSS_TC.get(nm, nm) or "（沒讀到名字）"


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
    for b in rec.get("bosses", []):
        lp = lost_pct(b)
        if lp is not None and (best is None or lp > best[1]):
            best = (b, lp)
    return best


def csv_row(r):
    t0, t1 = r.get("start"), r.get("end")
    wb = worst_boss(r)
    bosses = "；".join(f"{boss_name(b.get('name'))} {mmss(b.get('t'))}"
                      + (f" -{lost_pct(b):.0f}%" if lost_pct(b) is not None else "")
                      for b in r.get("bosses", []))
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
        for b in r.get("bosses", []):
            lp = lost_pct(b)
            if lp is not None:
                agg[boss_name(b.get("name"))].append(lp)
    if agg:
        L += ["", "■ 頭目扣血排行（每次出現平均扣掉最大 HP 的 %，越上面越危險）"]
        for nm, v in sorted(agg.items(), key=lambda kv: -sum(kv[1]) / len(kv[1]))[:8]:
            L.append(f"  {nm}：平均 {sum(v) / len(v):.0f}%、最多 {max(v):.0f}%（出現 {len(v)} 次）")
    if dead:
        top_cause, tc = Counter(r.get("cause") or "不明" for r in dead).most_common(1)[0]
        (a, label), bc = Counter(_bucket(r.get("max_timer") or 0, times) for r in dead).most_common(1)[0]
        L += ["", f"■ 最需要加強：最常倒在 {label}（{bc} 場），原因最常是「{top_cause}」（{tc} 場）"]
    L += ["", "■ 每一場"]
    for r in rows:
        wb = worst_boss(r)
        L.append(f"  {time.strftime('%H:%M', time.localtime(r['end'])) if r.get('end') else '':>5} "
                 f"{r.get('result', ''):<4} 計時 {mmss(r.get('max_timer')):>5}"
                 + (f"  原因：{r['cause']}" if r.get("result") == "倒下" and r.get("cause") else "")
                 + (f"  扣最多：{boss_name(wb[0].get('name'))} {wb[1]:.0f}%" if wb and wb[1] >= 1 else "")
                 + (f"  Lv {r['char_lv']}" if r.get("char_lv") else ""))
    L += ["", "（每場的詳細資料在同一個資料夾的 report_日期.csv，可以用 Excel 開）", ""]
    return "\n".join(L)
