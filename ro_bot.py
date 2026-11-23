"""
ro_bot.py — 升級自動選技能 / 寶箱自動選裝備 / 自動走位撿東西

熱鍵：F8 暫停／繼續    F9 結束
緊急停止：滑鼠甩到螢幕左上角
設定檔：同資料夾的 bot_config.json（第一次執行會自動產生）
"""

import ctypes
import json
import math
import os
import random
import re
import sys
import threading
import time
import traceback

# ---- 最早期的錯誤保護：任何錯誤（包含程式當掉）都寫進 logs ----
import faulthandler

_BOOT_DIR = os.path.dirname(sys.executable if getattr(sys, "frozen", False)
                            else os.path.abspath(__file__))
try:
    os.makedirs(os.path.join(_BOOT_DIR, "logs"), exist_ok=True)
    _crash_f = open(os.path.join(_BOOT_DIR, "logs", "crash.txt"), "w", encoding="utf-8")
    faulthandler.enable(_crash_f)
except Exception:
    pass


def _fatal(msg):
    print(msg, flush=True)
    try:
        with open(os.path.join(_BOOT_DIR, "logs", "error.txt"), "w", encoding="utf-8") as f:
            f.write(msg)
    except Exception:
        pass
    if sys.stdin is None or not sys.stdout:  # 視窗版（沒有命令視窗）
        try:
            import tkinter
            from tkinter import messagebox
            r = tkinter.Tk()
            r.withdraw()
            messagebox.showerror("啟動失敗", msg[-1500:] + "\n\n（錯誤也存在 logs\\error.txt）")
        except Exception:
            pass
        sys.exit(1)
    try:
        input("\n啟動失敗，請把上面的訊息傳給我。按 Enter 關閉…")
    except Exception:
        time.sleep(30)
    sys.exit(1)


IS_WIN = sys.platform == "win32"
if IS_WIN:
    from ctypes import wintypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
else:
    user32 = kernel32 = None

# onnxruntime 一定要比 opencv 先載入，否則可能因 DLL 版本衝突而當掉
try:
    import onnxruntime  # noqa: F401
except Exception:
    _fatal("載入 onnxruntime 失敗：\n" + traceback.format_exc() +
           "\n請安裝最新的 Microsoft Visual C++ 可轉散發套件（x64）：\n"
           "https://aka.ms/vs/17/release/vc_redist.x64.exe")

try:
    import cv2
    import numpy as np
except Exception:
    _fatal("載入 opencv / numpy 失敗：\n" + traceback.format_exc())

try:
    import mss
except BaseException:
    mss = None
try:
    import pyautogui
    pyautogui.PAUSE = 0
    pyautogui.MINIMUM_DURATION = 0
    pyautogui.MINIMUM_SLEEP = 0
    pyautogui.FAILSAFE = True
    FailSafe = pyautogui.FailSafeException
except BaseException:  # 背景模式不需要 pyautogui；沒有螢幕的環境也能匯入（測試用）
    pyautogui = None

    class FailSafe(Exception):
        pass


class _NoScreen:
    """背景模式不需要螢幕截圖工具"""
    monitors = [{}, {"left": 0, "top": 0, "width": 0, "height": 0}]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _new_mss():
    if mss is None or getattr(IO, "cdp", False):
        return _NoScreen()
    return mss.MSS() if hasattr(mss, "MSS") else mss.mss()


VERSION = "1.6.0"   # 線上更新用：發新版時改這裡，GitHub 上的標籤要是 v1.0.0 這種格式
FROZEN = getattr(sys, "frozen", False)
BASE_DIR = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "bot_config.json")
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
LOG_DIR = os.path.join(BASE_DIR, "logs")

# 寶箱能力（順序可在介面「寶箱」分頁調整）
# (清單上的名稱, 效果, 分類, 卡片標題, 分辨用) ── 分辨用：同名卡片靠效果文字裡有沒有「近戰」區分
_CHEST_RAW = [
    ("神", [
        ("貪婪之神", "+20% 金幣 → +40% 金幣"),
        ("戰爭之神", "+10% 傷害 · +3% 爆擊 → +20% 傷害 · +5% 爆擊"),
        ("處刑之神", "+3% 爆擊 · +10% 爆擊傷害 → +5% 爆擊 · +20% 爆擊傷害"),
        ("力量之神", "+3 STR · +10% 傷害 → +5 STR · +20% 傷害"),
        ("思想之神", "+3 INT · +10% 傷害 → +5 INT · +20% 傷害"),
        ("記憶之神", "+3 DEX · +3 詠唱速度 → +5 DEX · +5 詠唱速度"),
        ("堡壘之神", "+3 護甲 · +10% 最大 HP → +5 護甲 · +20% 最大 HP"),
        ("疾風之神", "+10% 速度 · +3% 迴避 → +20% 速度 · +5% 迴避"),
        ("生命之神", "每 5 秒 +5 HP · +10% 最大 HP → 每 5 秒 +10 HP · +20% 最大 HP"),
        ("智慧之神", "+20% 經驗值 → +40% 經驗值"),
        ("束縛之神", "+10% 範圍 · +10% 傷害 → +20% 範圍 · +20% 傷害"),
        ("詭計之神", "+3% 迴避 · +3% 爆擊 → +5% 迴避 · +5% 爆擊"),
        ("折磨之神", "+10% 毒、燃燒與流血傷害 · +10% 傷害 → +20% 毒、燃燒與流血傷害 · +20% 傷害"),
    ]),
    ("藥水", [
        ("恢復藥水", "每 5 秒 +1 HP → 每 5 秒 +5 HP"),
        ("冷卻藥水", "急速 +6 → 急速 +15"),
        ("生命藥水", "+10% 最大 HP → +20% 最大 HP"),
        ("護甲藥水", "+1 護甲 → +5 護甲"),
        ("廣域藥水（範圍）", "+18% 範圍 → +40% 範圍"),
        ("採集藥水", "拾取範圍 +1 公尺 → 拾取範圍 +5 公尺"),
        ("貪欲藥水", "+10% Zeny → +20% Zeny"),
        ("速度藥水", "+10% 移動速度 → +20% 移動速度"),
        ("詠唱藥水", "+6 詠唱速度 → +15 詠唱速度"),
        ("廣域藥水（近戰）", "+10% 近戰距離 → +20% 近戰距離（只限近戰流派）"),
        ("傷害藥水", "+18% 傷害 → +40% 傷害"),
    ]),
    ("秘藥", [
        ("爆擊秘藥", "+1% 爆擊 → +5% 爆擊"),
        ("傷害秘藥", "+10% 傷害 → +20% 傷害"),
        ("生命秘藥", "+15 最大 HP → +30 最大 HP"),
        ("爆擊傷害藥劑", "+18% 爆擊傷害 → +40% 爆擊傷害"),
        ("驚奇秘藥", "+10% 尋寶率 → +20% 尋寶率"),
        ("持續傷害藥劑", "中毒、燃燒與出血傷害 +10% → +20%（僅在已造成中毒、燃燒或出血後）"),
        ("迴避秘藥", "+1% 迴避（Flee）→ +5% 迴避"),
        ("詠唱秘藥", "+6 詠唱速度 → +15 詠唱速度"),
        ("廣域秘藥", "+10% 範圍 → +20% 範圍"),
        ("護甲秘藥", "-1% 承受傷害 → -5% 承受傷害"),
    ]),
    ("祝福", [
        ("傷害祝福", "+18% 傷害 → +40% 傷害"),
        ("恢復祝福", "每 5 秒 +1% 最大 HP → 每 5 秒 +5% 最大 HP"),
        ("冷卻祝福", "急速 +4 → 急速 +10"),
        ("吸血祝福", "+0.1% 吸血 → +0.5% 吸血"),
        ("雙倍投擲祝福", "+1% 雙倍投擲機率 → +5% 雙倍投擲機率"),
        ("豐收祝福", "+10% Zeny 與 +10% 尋寶率 → +20% Zeny 與 +20% 尋寶率"),
        ("詠唱祝福", "+4 詠唱速度 → +10 詠唱速度"),
        ("廣域祝福（範圍）", "+10% 範圍 → +20% 範圍"),
        ("廣域祝福（近戰）", "+10% 近戰距離 → +20% 近戰距離（只限近戰流派）"),
        ("睿智祝福", "+10% EXP → +20% EXP"),
    ]),
    ("神祇（技能）", [
        ("火箭術", "+15% 傷害 · 火球術：每個投射物爆成火球，對周圍造成 30% 傷害（1.5 公尺，隨範圍成長）"),
        ("冰箭術", "+15% 傷害 · 累積冰霜：每個投射物在敵人身上留下一層冰霜，累積 5 層時冰凍 2 秒"),
        ("雷擊術", "+15% 傷害 · 連鎖閃電：每一發跳向附近 2 個敵人，造成 50% 傷害"),
        ("聖靈召喚", "+15% 傷害 · 吸魂術：每用聖靈召喚擊殺一隻怪物獲得一層充能，再次施放時每層 +1 目標"),
        ("心靈爆破", "+12% 範圍 · 念力連擊：打擊多重複 2 次並施加詛咒，被命中的敵人 4 秒內受到傷害提高 15%"),
        ("火焰之壁", "+25% 持續時間 · 火柱：火牆熄滅時，每團火焰爆成火柱，對周圍造成 60% 傷害"),
        ("冰凍術", "冰凍 +5 秒 · 冰凍爆發：目標在冰凍中死亡會爆裂，對周圍敵人造成其最大 HP 35% 的傷害"),
        ("火狩", "+1 團火焰 · 火之獵殺：結束時火焰向外射出，命中時爆炸造成 150% 傷害"),
        ("雷爆術", "+12% 範圍 · 怒雷強擊：風暴最後在整個區域釋放一次大放電，對所有敵人造成 150% 傷害"),
    ]),
    ("料理", [
        ("STR（力量）料理", "+1 STR → +5 STR"),
        ("AGI（敏捷）料理", "+1 AGI → +5 AGI"),
        ("VIT（體力）料理", "+1 VIT → +5 VIT"),
        ("INT（智力）料理", "+1 INT → +5 INT"),
        ("DEX（靈巧）料理", "+1 DEX → +5 DEX"),
        ("LUK（幸運）料理", "+1 LUK → +5 LUK"),
    ]),
]
CHEST_ITEMS = [(name, eff) for _, lst in _CHEST_RAW for name, eff in lst]
RENAMED_17 = {"爆擊靈藥": "爆擊秘藥", "傷害靈藥": "傷害秘藥", "生命靈藥": "生命秘藥", "驚奇靈藥": "驚奇秘藥",
              "詠唱靈藥": "詠唱秘藥", "廣域靈藥": "廣域秘藥", "護甲靈藥": "護甲秘藥"}
# 卡片標題沒讀到（讀成下面的說明）時，用說明裡的關鍵字認出是哪個寶箱
CHEST_KEYWORDS = [("火球術", "火箭術"), ("累積冰霜", "冰箭術"), ("連鎖閃電", "雷擊術"),
                  ("吸魂術", "聖靈召喚"), ("念力連擊", "心靈爆破"), ("火柱", "火焰之壁"),
                  ("冰凍爆發", "冰凍術"), ("火之獵殺", "火狩"), ("怒雷強擊", "雷爆術"),
                  ("拾取範圍", "採集藥水"), ("吸血", "吸血祝福"), ("雙倍投擲", "雙倍投擲祝福"),
                  ("經驗值", "智慧之神"), ("移動速度", "速度藥水")]
CHEST_CATEGORY = {name: cat for cat, lst in _CHEST_RAW for name, _ in lst}


# 預設排在最前面的寶箱（依序）
CHEST_TOP = ["思想之神", "處刑之神", "戰爭之神", "火箭術", "冰箭術", "雷擊術", "採集藥水", "冷卻祝福", "冷卻藥水",
             "傷害祝福", "傷害藥水", "傷害秘藥", "爆擊秘藥", "爆擊傷害藥劑",
             "詠唱藥水", "詠唱秘藥", "詠唱祝福"]


def chest_default_order(current=None):
    base = current or [n for n, _ in CHEST_ITEMS]
    rest = [n for n in base if n not in CHEST_TOP]
    rest += [n for n, _ in CHEST_ITEMS if n not in CHEST_TOP and n not in rest]
    return CHEST_TOP + rest


def chest_title(name):
    """清單名稱 → 卡片上的標題（去掉「（範圍）」「（近戰）」）"""
    return re.sub(r"（(範圍|近戰)）$", "", name)


def chest_variant(name):
    m = re.search(r"（(範圍|近戰)）$", name)
    return m.group(1) if m else None



# =====================================================================
#  預設設定
# =====================================================================
DEFAULT_CONFIG = {
    "config_version": 17,
    "window_title": "",
    "dry_run": True,
    "debug_snapshots": True,
    "levelup": {
        "enabled": True,
        "hotkey": "b",
        "batch_min": 3,
        "batch_max": 4,
        "max_level": 0,
        "stop_before_end_sec": 25,
        "priority": ["火箭術", "冰箭術", "雷擊術"],
        "fourth_skill": "",
        "banish_fourth": True,
        "banish": ["聖靈召喚", "心靈爆破", "火焰之壁", "冰凍術", "火狩", "雷爆術", "攻擊：屬性附加"],
        "banish_all_listed": False,  # true = 放逐清單裡的技能出現都放逐（次數會不夠放逐攻擊：屬性附加）
        "auto_after_banish": False,  # 放逐完後改按遊戲的「自動」選技能（預設不按）
        "throwaway_until_level": 3,
        "target_until_level": 11,
        "target_words": ["目標"],
        "late_words": ["傷害", "冷卻"],
        "rarity_first": True,
        "avoid": [],
        "max_reroll": 0,
        "prefer_rarity": False,
        "think_sec": [0.15, 0.4],
    },
    "result": {
        "enabled": True,
        "button": "返回角色",
        "think_sec": [0.5, 1.0],
    },
    "lobby": {
        "enabled": True,
        "button": "單人大廳",
        "think_sec": [0.3, 0.7],
    },
    "ui": {
        "show_all": False,       # true = 顯示全部設定（技能、寶箱頁和基本頁的進階選項）
    },
    "update": {
        "repo": "EricTsai0506/ROSUP",   # GitHub 儲存庫「帳號/名稱」
        "token": "",             # 私人儲存庫才需要（唯讀權限的 token）
        "check_on_start": True,
    },
    "expedition": {
        "enabled": True,
    },
    "afk": {
        "enabled": True,
        "button": "接受",
        "think_sec": [0.8, 1.6],
    },
    "login": {
        "enabled": True,
        "button": "開始遊戲",
        "think_sec": [0.8, 1.6],
    },
    "charselect": {
        "enabled": True,
        "button": "開始遊戲",
        "slot": 1,            # 1~15：第一排 1-5、第二排 6-10、第三排 11-15
        "think_sec": [0.8, 1.6],
    },
    "town": {
        "enabled": True,
        "npc_name": "節慶主持人",
        "npc_keyword": "主持人",
        "talk_key": "space",
        "npc_body_offset": 75,
        "feet_offset": 40,
        "talk_cooldown": 1.5,
        "start_button": "開始遊戲",
        "start_button_pos": [0.786, 0.743],
        "field_button": "前往野外",
        "learn_route": True,
        "click_move": True,          # 點主持人讓遊戲自己走過去（遊戲內建尋路，不會亂走）；只點一次
        "click_wait": 30,            # 點完幾秒還沒走到主持人旁邊，就改回自己走
        "zoom_out": 10,              # 進城鎮後滾輪拉遠幾格，從出生點就看得到主持人（0 = 不調整）
        "mode": "festival",          # festival = 節慶主持人（原本的關卡）；expedition = 遠征隊長
        "stage": "普隆德拉原野",       # 遠征要刷的關卡
        "difficulty": "keep",          # 出發前的難度：keep 不調整、min 最簡單、max 最難
        "boss_hold": {"斐揚洞穴": 820},  # 遠征關卡：計時到幾秒後停在原地（只在躲鐘時移動）
    },
    "chest": {
        "enabled": True,
        "priority": chest_default_order(),
        "avoid": [],
        "banish_avoid": False,
        "max_reroll": 0,
        "reroll_rank": 10,           # 卡片排名在前 N 名之外 → 按那張卡下面的「重抽」（0 = 不重抽）
        "reroll_limit": 6,           # 每個寶箱選單最多重抽幾次
        "focus_rank": 3,             # 排名前 N 名的寶箱先按「專注」再選（之後更常出現）；0 = 不用
        "priority_late": [],         # 後期排名（空的 = 跟前期一樣）
        "late_from": 7,              # 遊戲時間第幾分鐘起改用後期排名（0 = 不分前後期）
        "new_item_rank": 10,         # 道具欄還沒滿時，新道具要在前 N 名內才拿（新道具會永久佔一格）；0 = 不限制
        "banish_rank": 0,            # 卡片排名在前 N 名之外 → 按「放逐」（0 = 不放逐）；技能放逐完才會做
        "prefer_rarity": True,
        "auto": False,               # true = 寶箱直接按遊戲的「自動寶箱」（預設不按，照優先順序選）
        "think_sec": [0.2, 0.5],
    },
    "move": {
        "enabled": True,
        "mode": "keys",
        "zoom_out": 10,              # 進入戰鬥後滾輪往上滾幾格，把視野拉大（0 = 不調整）
        "keys": {"up": "w", "down": "s", "left": "a", "right": "d"},
        "mouse_radius": 160,
        "coin_weight": 1.0,
        "ore_weight": 1.0,
        "chest_timeout": 7,
        "ore_hsv_low": [100, 40, 135],
        "ore_hsv_high": [135, 135, 225],
        "chest_threshold": 0.6,
        "chest_scales": [1.0],
        "item_detector": "shape",    # shape = 用礦石形狀找（各地圖都適用）；color = 舊的顏色判斷
        "ore_match": 0.7,
        "coin_hsv_low": [16, 70, 90],
        "coin_hsv_high": [34, 255, 255],
        "character_offset": [0, 0],
        "stuck_check": True,
        "minimap": True,             # 用右上角小地圖避開牆壁／水（會自己學每張地圖的地形）
        "minimap_goals": True,       # 畫面上沒東西撿時，往小地圖上的寶箱／磁鐵走
        "boss_capture_games": 3,     # 打頭目時每秒存一張截圖（logs\\boss），每次啟動存幾場（0 = 不存）
        "boss_capture_from": 830,    # 計時到幾秒開始存（830 = 13:50）
        "magnet_first": True,        # 小地圖上附近有磁鐵就先去撿（畫面上有寶箱時還是先撿寶箱）
        "boss_dodge": True,          # 頭目（MVP）出現時：看地上紅色預警範圍，站在裡面就躲出去、走路不踩進去
        "boss_kite": True,           # 頭目戰：不追遠處的寶箱／磁鐵，一直在空曠的地方繞圈移動
        "boss_times": [150, 330, 450, 630, 750, 840],   # 頭目出現的遊戲時間（秒）：約 2:30、5:30、7:30、10:30、12:30、14:00
        "boss_kite_hp": 0.85,        # 一般頭目在場時 HP 低於這個比例才改成繞圈躲（滿血照常撿東西）
        "boss_prep_sec": 10,         # 頭目出現前幾秒先走到空曠的地方、開始繞圈
        "low_hp_kite": 0.4,          # HP 低於這個比例 → 不撿東西，先繞圈躲（0 = 不用）
        "crowd_kite": True,          # 「一大群怪物包圍」或 HP 幾秒內掉很多 → 往怪少、空曠的地方跑
        "hp_drop_kite": 0.15,        # 4.5 秒內掉超過最大 HP 的這個比例就跑
        "crowd_level": 0.55,         # 周圍「在動的東西」比例超過這個（而且掉血中）就當成被圍住
        "end_capture": 5,            # 一局提早結束（還沒到 13:50 就結束，多半是角色倒下）→ 存最後 20 秒截圖，每次啟動存幾場
    },
    "browser": {
        "mode": "cdp",
        "url": "https://ro-survivors.com",
        "port": 9229,
        "edge_path": "",
        "window_size": [1280, 960],
        "lock_window_size": True,
        "restart_hours": 6,          # 背景模式：每幾小時重開一次遊戲用的 Edge（一局打完時；0 = 不重開）
    },
    "schedule": {
        "enabled": False,
        "games_min": 3,
        "games_max": 5,
        "rest_min_minutes": 10,
        "rest_max_minutes": 20,
        "stop_after_games": 0,
    },
    "hud_ignore": [
        [0.00, 0.00, 0.36, 0.17],
        [0.37, 0.00, 0.63, 0.15],
        [0.80, 0.00, 1.00, 0.31],
        [0.34, 0.82, 0.66, 1.00],
    ],
    "coord_region": [0.84, 0.00, 1.00, 0.07],
    "menu_ocr_scale": 1.0,
}


def deep_merge(base, over):
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def migrate_config(cfg, ver=None):
    """舊版設定檔升級；ver = 使用者設定檔原本的版本號（沒有就是 1）"""
    if ver is None:
        ver = cfg.get("config_version", 1)
    if ver < 2:
        cfg["levelup"]["rarity_first"] = True  # 卡片等級最優先
    if ver < 3:      # 加快速度
        cfg["menu_ocr_scale"] = 1.0
        for k, v in (("levelup", [0.15, 0.4]), ("chest", [0.2, 0.5]),
                     ("result", [0.5, 1.0]), ("lobby", [0.3, 0.7])):
            cfg[k]["think_sec"] = v
    if ver < 4:
        cfg["town"]["talk_cooldown"] = 1.5
        cfg["levelup"]["throwaway_until_level"] = 3
    if ver < 5:
        cfg["levelup"].setdefault("batch_min", 3)
        cfg["levelup"].setdefault("batch_max", 4)
        cfg["levelup"].setdefault("stop_before_end_sec", 25)
    if ver < 7:
        cfg.setdefault("browser", {})["mode"] = "cdp"  # 預設改用背景模式
    if ver < 8 and not cfg["chest"].get("priority"):
        cfg["chest"]["priority"] = [n for n, _ in CHEST_ITEMS]
    if ver < 9:  # 新增藥水、靈藥、祝福、料理，接在原本順序後面
        have = cfg["chest"].get("priority", [])
        cfg["chest"]["priority"] = have + [n for n, _ in CHEST_ITEMS if n not in have]
    if ver < 10:  # 指定的 14 項放到最前面，其他維持原本順序
        cfg["chest"]["priority"] = chest_default_order(cfg["chest"].get("priority"))
    if ver < 11:  # 主力以外的技能預設全部勾放逐（使用者自己改過的就不動）
        if cfg["levelup"].get("banish", []) in ([], ["攻擊：屬性附加"]):
            cfg["levelup"]["banish"] = ["聖靈召喚", "心靈爆破", "火焰之壁", "冰凍術", "火狩", "雷爆術", "攻擊：屬性附加"]
    if ver < 13:  # 遊戲介面變大，戰鬥畫面要忽略的 HUD 範圍跟著放大
        if cfg.get("hud_ignore") == [[0.00, 0.00, 0.28, 0.11], [0.40, 0.00, 0.60, 0.15],
                                     [0.69, 0.00, 1.00, 0.23], [0.38, 0.85, 0.62, 1.00]]:
            cfg["hud_ignore"] = [[0.00, 0.00, 0.36, 0.17], [0.37, 0.00, 0.63, 0.15],
                                 [0.80, 0.00, 1.00, 0.31], [0.34, 0.82, 0.66, 1.00]]
    if ver < 14:  # 技能、寶箱預設都不按遊戲的「自動」
        cfg["levelup"]["auto_after_banish"] = False
        cfg["chest"]["auto"] = False
    if ver < 15:  # 新增技能神祇：主力技能的排在第 4 名起，其他接在最後
        have = list(cfg["chest"].get("priority", []))
        main = [n for n in cfg["levelup"].get("priority", ["火箭術", "冰箭術", "雷擊術"])
                if CHEST_CATEGORY.get(n) == "神祇（技能）" and n not in have]
        have[3:3] = main
        have += [n for n, _ in CHEST_ITEMS if CHEST_CATEGORY[n] == "神祇（技能）" and n not in have]
        cfg["chest"]["priority"] = have
    if ver < 16:  # 「Flee（迴避）秘藥」卡片上寫的是「迴避秘藥」
        cfg["chest"]["priority"] = ["迴避秘藥" if n == "Flee（迴避）秘藥" else n
                                    for n in cfg["chest"].get("priority", [])]
    if ver < 17 and cfg.get("move", {}).get("boss_times") == [180, 300, 420, 600, 720, 840]:
        cfg["move"]["boss_times"] = [150, 330, 450, 630, 750, 840]   # 依實際紀錄調整頭目出現時間
    if ver < 17:  # 遊戲把「靈藥」改名「秘藥」（卡片上是「驚奇秘藥」「生命秘藥」）
        ren = RENAMED_17
        for key in ("priority", "avoid", "priority_late"):
            cfg["chest"][key] = [ren.get(n, n) for n in cfg["chest"].get(key, []) or []]
    late = [n for n in (cfg["chest"].get("priority_late") or []) if n]
    if late:    # 後期排名缺的項目（新增的道具）接在最後面
        cfg["chest"]["priority_late"] = late + [n for n in cfg["chest"].get("priority", []) if n not in late]
    cfg["config_version"] = 17
    return cfg


def load_config():
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        log(f"已建立設定檔：{CONFIG_PATH}")
        log("請先看「說明.txt」修改設定，再重新執行。")
        return None
    with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
        raw = json.load(f)
    return migrate_config(deep_merge(DEFAULT_CONFIG, raw), raw.get("config_version", 1))


# =====================================================================
#  記錄
# =====================================================================
_logf = None
LOG_HOOKS = []   # 視窗介面用：收到每一行紀錄
CONTROL = {"stop": False, "toggle_pause": False, "paused": False, "status": ""}


def log(*a):
    global _logf
    s = time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a)
    try:
        if sys.stdout:
            print(s, flush=True)
    except Exception:
        pass
    for h in LOG_HOOKS:
        try:
            h(s)
        except Exception:
            pass
    try:
        if _logf is None:
            os.makedirs(LOG_DIR, exist_ok=True)
            _logf = open(os.path.join(LOG_DIR, time.strftime("bot_%Y%m%d_%H%M%S.log")),
                         "w", encoding="utf-8")
            # 紀錄檔只留最新 40 個，舊的自動刪掉（不然 logs 資料夾會越來越大）
            try:
                olds = sorted(f for f in os.listdir(LOG_DIR) if f.startswith("bot_") and f.endswith(".log"))
                for f in olds[:-40]:
                    os.remove(os.path.join(LOG_DIR, f))
            except Exception:
                pass
        _logf.write(s + "\n")
        _logf.flush()
    except Exception:
        pass


# =====================================================================
#  文字正規化與比對（OCR 會繁簡混雜、偶有錯字）
# =====================================================================
_PAIRS = (
    "級级 擇择 選选 寶宝 術术 擊击 靈灵 喚唤 聖圣 傳传 說说 優优 專专 劍剑 詠咏 雙双 "
    "復复 擴扩 範范 圍围 敵敌 傷伤 屬属 藥药 鬥斗 護护 閃闪 禦御 衝冲 彈弹 風风 雲云 "
    "電电 龍龙 鳳凤 獸兽 體体 減减 攝摄 暫暂 連连 頭头 綠绿 藍蓝 紅红 黃黄 縛缚 轉转 "
    "鏈链 強强 裝装 備备 幣币 動动 凍冻 詛诅 聲声 點点 數数 層层 時时 發发 會会 學学 "
    "對对 鎧铠 飾饰 項项 鍊链 師师 迴回 輕轻 驗验 經经 歷历 魂魂 盜盗 罰罚 審审 鎮镇 "
    "陣阵 環环 廳厅 廰厅 來来 與与 節节 慶庆 欲欲 採采 驚惊 劑剂 秘秘 豐丰 睿睿 擲掷 倍倍 巧巧 運运 敏敏 捷捷 續续 毒毒 燒烧 血血 貪贪 爭争 處处 憶忆 壘垒 詭诡 計计 擊击 祇祇 張张 隕陨 牆墙 癒愈 鳴鸣 撃击 擎击 標标 卻却 屡属 貫贯 開开 遊游 戲戏 燈灯 籠笼 穫获 記记 錄录 寵宠 員员 遠远 徵征 單单 戰战 隊队 長长 談谈 噴喷 場场 廣广 線线 爐炉 燒烧 熱热 靜静 線线 鋒锋 銳锐 鐵铁 錘锤 槍枪 鬼鬼 衔术 循术 専专"
)
_PAIRS += " 離离 沒没 欄栏 著着 帳帐 號号 刪删 線线 暫暂 擇择 項项 歡欢 迎迎"
_PAIRS += " 陽阳 揚扬 楊杨 輪轮 亞亚 嵐岚"   # 地圖名稱（小地圖標題）
_PAIRS += " 鐘钟 躱躲"                       # 頭目機制「躲到鐘下！」
T2S = {}
for _p in _PAIRS.split():
    if _p[0] != _p[1]:
        T2S[ord(_p[0])] = _p[1]
_PUNCT = re.compile(r"[\s·・•,，.。:：;；!！?？()（）\[\]【】「」『』'\"*★☆+\-/\\|~～_]")


def norm(s):
    return _PUNCT.sub("", s.translate(T2S).lower())


def _lcs(a, b):
    dp = [0] * (len(b) + 1)
    for ca in a:
        prev = 0
        for j, cb in enumerate(b, 1):
            cur = dp[j]
            dp[j] = prev + 1 if ca == cb else max(dp[j], dp[j - 1])
            prev = cur
    return dp[-1]


# 不佔技能欄位、出現就直接放逐（不會被選來當第四技能）
ALWAYS_BANISH = ["攻擊：屬性附加"]
# 主力類型的技能：勾了放逐只算「額外放逐」，不會被選來當第四技能
MAIN_TYPE = ["火箭術", "冰箭術", "雷擊術"]

# 已知技能名稱：用來避免把「雷爆術」誤認成「雷擊術」這類相近名字
KNOWN_SKILLS = ["火箭術", "冰箭術", "雷擊術", "雷爆術", "聖靈召喚", "心靈爆破", "火狩",
                "攻擊：屬性附加", "冰凍術", "火焰之壁", "火球術", "暴風雪", "隕石術",
                "地震術", "石化術", "冰刃之牆", "怒雷強擊", "火焰術", "冰霜術", "雷鳴術",
                "聖光術", "治癒術", "加速術", "魔法盾"]


# 新技能卡上有英文名稱「(Thunderstorm) · 範圍 · 詠唱 0.6 s」，比中文標題好認
EN_SKILLS = {"firebolt": "火箭術", "coldbolt": "冰箭術", "lightningbolt": "雷擊術",
             "thunderstorm": "雷爆術", "soulstrike": "聖靈召喚", "napalmbeat": "心靈爆破",
             "sight": "火狩", "frostdiver": "冰凍術", "firewall": "火焰之壁"}


def en_skill(text):
    """「(Napalm Beat) · 範圍」→ 心靈爆破；讀不到回傳 None"""
    m = re.search(r"[(（]\s*([A-Za-z][A-Za-z0-9 .'-]{2,24})\s*[)）]", text or "")
    if not m:
        return None
    w = re.sub(r"[^a-z]", "", m.group(1).lower().replace("1", "l").replace("0", "o"))
    if w in EN_SKILLS:
        return EN_SKILLS[w]
    import difflib
    best = difflib.get_close_matches(w, list(EN_SKILLS), n=1, cutoff=0.8)
    return EN_SKILLS[best[0]] if best else None


def canon_skill(text):
    """卡片標題 → 標準技能名稱。兩個技能一樣像（例如「冰速術」像冰箭術也像冰凍術）就回傳 None，
    不要亂猜"""
    t = norm(text)
    if not t:
        return None
    for k in sorted(KNOWN_SKILLS, key=len, reverse=True):
        if norm(k) in t[: len(norm(k)) + 2]:
            return k
    sc = []
    for k in KNOWN_SKILLS:
        n = norm(k)
        h = t[: len(n) + 2]
        sc.append((_lcs(h, n) / len(n) + (0.15 if h[:1] == n[:1] else 0), k))
    sc.sort(reverse=True)
    if sc and sc[0][0] >= 0.6 and (len(sc) == 1 or sc[0][0] > sc[1][0] + 0.01):
        return sc[0][1]
    return None


def match(text, kw):
    """卡片標題是否符合關鍵字（容許 OCR 少一個字或錯一個字）"""
    t, k = norm(text), norm(kw)
    if not k or not t:
        return False
    head = t[: len(k) + 2]
    if k in head:
        return True
    # 標題剛好是另一個已知技能 → 不是這個
    for other in KNOWN_SKILLS:
        o = norm(other)
        if o != k and o in t[: len(o) + 2]:
            return False
    # 技能名稱：跟所有已知技能比相似度，最像這個才算
    # 例：冰箭 → 冰箭術、雷辈術／雷擎術／雷術 → 雷擊術、心爆破 → 心靈爆破
    known_norm = [norm(x) for x in KNOWN_SKILLS] + \
        list(dict.fromkeys(norm(chest_title(x)) for x, _ in CHEST_ITEMS))
    if k in known_norm:
        def score(n):
            h = t[: len(n) + 2]
            sc = _lcs(h, n) / len(n)
            return sc + (0.15 if h[:1] == n[:1] else 0)
        mine = score(k)
        if mine < 0.6:
            return False
        return mine >= max((score(o) for o in known_norm if o != k), default=0)
    return len(k) >= 4 and _lcs(head, k) >= len(k) - max(1, len(k) // 3)


# =====================================================================
#  視窗與畫面
# =====================================================================
_ZW = re.compile(r"[\s\u200b\u200c\u200d\u2060\ufeff]")


def _clean_title(t):
    return _ZW.sub("", t).lower()


def list_windows():
    if not IS_WIN:
        return []
    own = kernel32.GetConsoleWindow()
    out = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if hwnd != own and user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                out.append((hwnd, buf.value))
        return True

    user32.EnumWindows(cb, 0)
    return out


def find_window(sub):
    if not IS_WIN or not sub:
        return None
    key = _clean_title(sub)
    for hwnd, title in list_windows():
        if key in _clean_title(title):
            return hwnd, title
    return None


def client_rect(hwnd):
    r = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    pt = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return pt.x, pt.y, r.right, r.bottom


def is_foreground(hwnd):
    return (not IS_WIN) or hwnd is None or user32.GetForegroundWindow() == hwnd


class Viewport:
    """找出遊戲畫面在瀏覽器視窗裡的實際範圍（切掉分頁列、網址列、書籤列）"""

    def __init__(self):
        self.offsets = {}   # (視窗寬, 高) → (左, 上, 右, 下) 要切掉的像素
        self.logged = set()

    @staticmethod
    def _uniform(g):
        q = (g[:, ::3] // 10).astype(np.int32)
        return np.array([np.bincount(r, minlength=26).max() / r.size for r in q]) > 0.5

    def update(self, frame):
        H, W = frame.shape[:2]
        g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        ur = self._uniform(g)
        top = 0
        for y in range(0, H // 4):
            if not ur[y:y + 12].any():
                top = y
                break
        # 瀏覽器工具列至少 30 像素高；只有一兩條邊框不算
        if top < 30:
            top = 0
        # 切掉的部分必須是淺色的瀏覽器介面，暗色的遊戲選單不算
        if top > 0 and g[:top].mean() < 150:
            return
        uc = self._uniform(g.T)
        left = 0
        for x in range(0, 25):
            if not uc[x:x + 12].any():
                left = x
                break
        right = 0
        for k in range(0, 25):
            x = W - 1 - k
            if not uc[max(0, x - 12):x + 1].any():
                right = k
                break
        bottom = 0
        for k in range(0, 35):
            y = H - 1 - k
            if not ur[max(0, y - 12):y + 1].any():
                bottom = k
                break
        off = (left, top, right, bottom)
        self.offsets[(W, H)] = off
        if (W, H, off) not in self.logged and top > 0:
            log(f"偵測到瀏覽器工具列，遊戲畫面從上方第 {top} 像素開始")
            self.logged.add((W, H, off))

    def apply(self, rect):
        x, y, w, h = rect
        l, t, r, b = self.offsets.get((w, h), (0, 0, 0, 0))
        return (x + l, y + t, w - l - r, h - t - b)


VIEW = Viewport()


class ScreenIO:
    """前景模式：真實的鍵盤滑鼠（遊戲視窗必須在最前面）"""
    cdp = False

    def position(self):
        return pyautogui.position()

    def move_to(self, x, y):
        pyautogui.moveTo(x, y)

    def mouse_down(self):
        pyautogui.mouseDown()

    def mouse_up(self):
        pyautogui.mouseUp()

    def wheel(self, x, y, notches):
        pyautogui.scroll(-int(notches) * 120, x=x, y=y)

    def key_down(self, k):
        pyautogui.keyDown(k)

    def key_up(self, k):
        pyautogui.keyUp(k)


class CdpIO:
    """背景模式：直接控制遊戲用的 Edge 分頁，不碰真實的鍵盤滑鼠"""
    cdp = True

    def __init__(self, page):
        self.page = page

    def position(self):
        return self.page.mouse

    def move_to(self, x, y):
        self.page.mouse_move(x, y)

    def mouse_down(self):
        self.page.mouse_down()

    def mouse_up(self):
        self.page.mouse_up()

    def wheel(self, x, y, notches):
        self.page.mouse_wheel(x, y, int(notches) * 100)

    def key_down(self, k):
        self.page.key_down(k)

    def key_up(self, k):
        self.page.key_up(k)


IO = ScreenIO()


def grab(sct, rect):
    if IO.cdp:
        f = IO.page.latest()
        if f is None:
            raise RuntimeError("拿不到遊戲畫面")
        x, y, w, h = [int(v) for v in rect]
        return np.ascontiguousarray(f[max(0, y):y + h, max(0, x):x + w])
    return _grab_screen(sct, rect)


def _grab_screen(sct, rect):
    x, y, w, h = rect
    img = np.array(sct.grab({"left": x, "top": y, "width": w, "height": h}))[:, :, :3]
    return np.ascontiguousarray(img)


class Hotkeys:
    VK = {"F8": 0x77, "F9": 0x78}

    def __init__(self):
        self.prev = {}

    def hit(self, name):
        if not IS_WIN:
            return False
        vk = self.VK[name]
        down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
        was = self.prev.get(vk, False)
        self.prev[vk] = down
        return down and not was


# =====================================================================
#  OCR
# =====================================================================
_engines = {}
_engine_locks = {}
_engines_lock = threading.Lock()


_threads_limited = False


def _limit_ocr_threads():
    """辨識引擎預設會用滿所有 CPU 核心；限制成少數幾個，避免拖慢遊戲和其他程式"""
    global _threads_limited
    if _threads_limited:
        return
    _threads_limited = True
    try:
        import onnxruntime as ort
        import rapidocr_onnxruntime.utils as ru
        n = max(1, min(3, (os.cpu_count() or 4) // 4))

        def _opts():
            o = ort.SessionOptions()
            o.intra_op_num_threads = n
            o.inter_op_num_threads = 1
            return o
        if hasattr(ru, "SessionOptions"):
            ru.SessionOptions = _opts
            log(f"文字辨識每個引擎最多使用 {n} 個 CPU 核心")
    except Exception:
        pass


def lower_own_priority():
    """把本程式的優先順序調成『低於一般』：CPU 不夠時讓遊戲和其他程式先跑"""
    if IS_WIN:
        try:
            kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x00004000)
        except Exception:
            pass


def get_ocr(name="main"):
    """取得文字辨識引擎。背景辨識（bg）和主流程（main）各用一個，不用互相排隊。
    調整：小圖不再放大到 736（limit_type=max）、不檢查文字方向（遊戲文字都是正的）"""
    with _engines_lock:
        if name not in _engines:
            _limit_ocr_threads()
            from rapidocr_onnxruntime import RapidOCR
            try:
                eng = RapidOCR(det_model_path=None, det_limit_side_len=1600,
                               det_limit_type="max", use_angle_cls=False)
            except Exception:
                eng = RapidOCR()
            _engines[name] = eng
            _engine_locks[name] = threading.Lock()
        return _engines[name]


def ocr(img, ox=0, oy=0, scale=1.0, min_score=0.45, engine="main"):
    if scale != 1.0:
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    eng = get_ocr(engine)
    with _engine_locks[engine]:
        res, _ = eng(img)
    items = []
    for box, text, score in res or []:
        if float(score) < min_score:
            continue
        xs = [p[0] / scale for p in box]
        ys = [p[1] / scale for p in box]
        x, y = min(xs), min(ys)
        w, h = max(xs) - x, max(ys) - y
        items.append({"text": text, "n": norm(text), "x": ox + x, "y": oy + y,
                      "w": w, "h": h, "cx": ox + x + w / 2, "cy": oy + y + h / 2})
    return items


# =====================================================================
#  選單判斷與選卡
# =====================================================================
TAGS = {"技能": 0, "普通": 1, "优良": 2, "稀有": 3, "史诗": 4, "传说": 5, "神话": 6}


def tag_rank(n):
    """稀有度標籤（容許 OCR 錯一個字，例如『傅说』）"""
    if n in TAGS:
        return TAGS[n]
    if len(n) == 2:
        for k, v in TAGS.items():
            if n[0] == k[0] or n[1] == k[1]:
                return v
    return None
BUTTONS = {"重抽", "专注", "放逐", "重抽全部", "新"}
SLOT_RE = re.compile(r"^(技能|道具)\d{2,}$")


EXPEDITION_STAGES = ["普隆德拉原野", "普隆德拉地下水道", "失事輪", "柏伊亞嵐島", "斐揚森林",
                     "斐揚洞穴", "普隆德拉城"]


def stage_of(text):
    """OCR 讀到的字最像哪一個遠征關卡（不像就回傳 None）"""
    import difflib
    n = norm(text)
    if len(n) < 2:
        return None
    best, score = None, 0.0
    for st_ in EXPEDITION_STAGES:
        r = difflib.SequenceMatcher(None, n, norm(st_)).ratio()
        if r > score:
            best, score = st_, r
    return best if score >= 0.6 else None


def stage_in(text):
    """這行字裡是哪一個遠征關卡：允許前後多字（「普隆德拉原野 Lv 30」「已通過」），
    也允許讀錯一兩個字；兩個關卡一樣像（例如只讀到「普隆德拉」）就回傳 None"""
    import difflib
    n = re.sub(r"[^\u4e00-\u9fff]", "", norm(text))
    if len(n) < 2:
        return None
    sc = []
    for st_ in EXPEDITION_STAGES:
        k = norm(st_)
        if k in n:
            sc.append((1.0 + len(k) / 100, st_))      # 完整包含：越長越準（地下水道 > 原野）
            continue
        best = 0.0
        L = len(k)
        for i in range(0, max(1, len(n) - L + 2)):
            best = max(best, difflib.SequenceMatcher(None, n[i:i + L + 1], k).ratio())
        sc.append((best, st_))
    sc.sort(reverse=True)
    if sc[0][0] >= 0.7 and (len(sc) == 1 or sc[0][0] > sc[1][0] + 0.1):
        return sc[0][1]
    return None


def _rows_joined(items):
    """同一列、靠在一起的字接起來（關卡名稱有時被拆成兩段讀）"""
    out = []
    for i in sorted(items, key=lambda i: (round(i["cy"] / 12), i["x"])):
        if out and abs(out[-1]["cy"] - i["cy"]) < i["h"] * 0.6 and \
                0 <= i["x"] - (out[-1]["x"] + out[-1]["w"]) < i["h"] * 1.5:
            o = out[-1]
            x0, x1 = min(o["x"], i["x"]), max(o["x"] + o["w"], i["x"] + i["w"])
            out[-1] = dict(o, text=o["text"] + i["text"], n=o["n"] + i["n"], x=x0, w=x1 - x0,
                           cx=(x0 + x1) / 2)
        else:
            out.append(dict(i))
    return out


MAP_NAMES = ["洛陽", "斐揚洞穴", "普隆德拉地下水道", "失事輪甲板", "普隆德拉原野", "柏伊亞嵐神殿",
             "斐揚森林", "普隆德拉城"]


def map_name_of(text):
    """小地圖標題「斐揚洞穴 (168, 182)」→ 地圖名稱（讀不出來回傳 None）"""
    import difflib
    name = re.split(r"[(（\d]", text or "", maxsplit=1)[0]
    name = re.sub(r"[\s,，.:：]", "", name)
    n = norm(name)
    if len(n) < 2:
        return None
    best, score = None, 0.0
    for m_ in MAP_NAMES:
        r = difflib.SequenceMatcher(None, n, norm(m_)).ratio()
        if r > score:
            best, score = m_, r
    if score >= 0.6:
        return best
    return name if 2 <= len(name) <= 12 else None


def is_expedition(items):
    joined = " ".join(i["n"] for i in items)
    n_pass = joined.count(norm("已通"))
    n_mvp = sum(1 for i in items if i["n"].startswith("mvp"))
    return any(k in joined for k in ("装备分", "装分", "备分", "分敷", "分数：")) or \
        (n_pass >= 1 and n_mvp >= 1 and any(stage_of(i["text"]) for i in items))


def detect_menu(items):
    """回傳 'lobby'、'result'、'levelup'、'chest' 或 None"""
    joined = " ".join(i["n"] for i in items)

    def has(*ws):
        return any(norm(w) in joined for w in ws)
    # 暫離彈窗（5 分鐘沒動作被請離）→ 接受
    if has("暫離", "沒有動作", "已離開遊戲", "即可回來") and any(match(i["text"], "接受") for i in items):
        return "afk"
    # 遠征隊長的關卡選擇視窗
    if is_expedition(items):
        return "expedition"
    # 角色選擇（15 個欄位）
    n_slots = sum(1 for i in items if _is_empty_slot(i)
                  or ("baselv" in i["n"] and "joblv" in i["n"]))
    result_like = has("返回角色", "存活下來", "本局傷害", "獲得的戰利品", "基本經驗")
    if not result_like and (has("角色選擇") or n_slots >= 2):
        return "charselect"
    # 登入首頁（普隆德拉在等著你／開始遊戲！）
    if has("在等著你", "建立帳號", "仙境傳說風格的生存遊戲") and \
            any(match(i["text"], "開始遊戲") for i in items):
        return "login"
    if any(match(i["text"], "選擇大廳") for i in items) or "都有自己的" in joined \
            or "频道和聊天" in joined or "遥挥大" in joined:
        return "lobby"
    if "存活" in joined or "本局等级" in joined or "返回角色" in joined \
            or any(k in joined for k in ("倒下", "阵亡", "死亡", "游戏结束", "失败")):
        return "result"
    chest = any(i["n"].startswith("道具") and SLOT_RE.match(i["n"]) for i in items) \
        or "装备" in joined or "金币" in joined
    level = any(i["n"].startswith("技能") and SLOT_RE.match(i["n"]) for i in items) \
        or any(re.search(r"升[级級]\s*[!！]", i["text"]) for i in items) \
        or "选择技能" in joined or "强化技能" in joined
    if chest:
        return "chest"
    if level:
        return "levelup"
    # 遊戲外的網頁（登入首頁、暫離彈窗、角色選擇）：右上角有「請我喝杯咖啡」等按鈕
    if has("在等著你", "喝杯咖啡", "Reven4nt", "生存遊戲", "生存游"):
        return "login"
    return None


def in_game(items):
    """戰鬥畫面：上方有『升級 N B』按鈕、倒數計時器（04:15）、本局傷害或金幣（2596z）"""
    joined = " ".join(i["n"] for i in items)
    if norm("角色選擇") in joined or norm("空欄位") in joined or norm("最佳本局") in joined:
        return False
    if "本局" in joined:
        return True
    for i in items:
        n = i["n"]
        if re.match(r"^升级\d", n):
            return True
        if re.match(r"^\s*\d{1,2}\s*[:：]\s*\d{2}\s*$", i["text"]):
            return True
        if re.match(r"^\d{2,6}z", n):
            return True
        if n == "升级" and any(
                j is not i and re.match(r"^\d+", j["n"]) and abs(j["cy"] - i["cy"]) < i["h"]
                and 0 < j["x"] - (i["x"] + i["w"]) < i["h"] * 4 for j in items):
            return True
    return False


def read_char_level(items):
    """左上角頭像下方的角色等級（Lv 14 → 14）"""
    for i in items:
        m = re.match(r"^lv\s*(\d{1,3})$", i["n"])
        if m:
            return int(m.group(1))
    return None


def read_timer(items):
    """畫面上方的倒數計時（04:15 → 255 秒）；讀不到回傳 None"""
    for i in items:
        m = re.match(r"^\s*(\d{1,2})\s*[:：]\s*(\d{2})\s*$", i["text"])
        if m and int(m.group(2)) < 60:
            return int(m.group(1)) * 60 + int(m.group(2))
    return None


def hud_upgrade_count(items):
    """畫面上方的『升級 N』按鈕；回傳 N（0 = 沒有）"""
    for i in items:
        m = re.match(r"^升级(\d+)", i["n"])
        if m:
            return int(m.group(1))
        if i["n"] == "升级":
            for j in items:
                if j is not i and re.match(r"^\d+", j["n"]) and abs(j["cy"] - i["cy"]) < i["h"] \
                        and 0 < j["x"] - (i["x"] + i["w"]) < i["h"] * 4:
                    return int(re.match(r"^\d+", j["n"]).group())
    return 0


RARITY_HUES = [((5, 22), 5), ((130, 160), 4), ((95, 128), 3), ((40, 85), 2)]


def rarity_by_color(frame, title, ox, oy, columns=False):
    """OCR 沒讀到稀有度標籤時，看標籤的顏色
    手機（直排）：標籤在標題左上方；電腦（橫排）：標籤在標題正上方、卡片頂端"""
    if frame is None:
        return None
    h = max(title["h"], 12)
    if columns:
        cx = title["x"] + title["w"] / 2 - ox
        x0, x1 = int(cx - 2.5 * h), int(cx + 2.5 * h)
        y0, y1 = int(title["y"] - oy - 8 * h), int(title["y"] - oy - 4 * h)
    else:
        x0 = int(title["x"] - ox - 6 * h)
        x1 = int(title["x"] - ox)
        y0 = int(title["y"] - oy - 2.5 * h)
        y1 = int(title["y"] - oy + 0.5 * h)
    H, W = frame.shape[:2]
    x0, x1, y0, y1 = max(0, x0), min(W, x1), max(0, y0), min(H, y1)
    if x1 - x0 < 5 or y1 - y0 < 5:
        return None
    hsv = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
    sat = (hsv[:, :, 1] > 120) & (hsv[:, :, 2] > 150)
    best, best_n = None, 40
    for (lo, hi), r in RARITY_HUES:
        n = int(np.count_nonzero(sat & (hsv[:, :, 0] >= lo) & (hsv[:, :, 0] <= hi)))
        if n > best_n:
            best, best_n = r, n
    return best


def _is_button(i):
    n = i["n"]
    return n in ("重抽", "专注", "放逐") or (len(n) == 2 and (n[0] == "放" or n[1] == "逐"
                                                         or n[0] == "专" or n[1] == "注"
                                                         or n == "重抽"))


def _banish_button(cl, rank, spacing_hint=None):
    """找這張卡的『放逐』按鈕；沒讀到字就用其他按鈕的位置推算（絕不拿『重抽』代替）"""
    for i in cl:
        if rank(i) == 2:
            return i
    cl = sorted(cl, key=lambda i: i["cx"])
    last = max(cl, key=rank)
    if len(cl) >= 2 and rank(cl[-1]) != rank(cl[0]):
        spacing = (cl[-1]["cx"] - cl[0]["cx"]) / (rank(cl[-1]) - rank(cl[0]))
    elif spacing_hint:
        spacing = spacing_hint  # 參考其他卡片按鈕的實際間距
    else:
        spacing = max(last["w"] * 1.8, last["h"] * 3.2)
    cx = last["cx"] + (2 - rank(last)) * spacing
    return {"text": "放逐", "n": "放逐", "x": cx - last["w"] / 2, "y": last["y"],
            "w": last["w"], "h": last["h"], "cx": cx, "cy": last["cy"], "estimated": True}


def _reroll_button(cl, rank, spacing_hint=None, want=0, label="重抽"):
    """這張卡的『重抽』（want=0）或『專注』（want=1）按鈕；沒讀到就用其他按鈕推算"""
    for i in cl:
        if rank(i) == want:
            return i
    cl = sorted(cl, key=lambda i: i["cx"])
    first = min(cl, key=rank)
    if len(cl) >= 2 and rank(cl[-1]) != rank(cl[0]):
        spacing = (cl[-1]["cx"] - cl[0]["cx"]) / (rank(cl[-1]) - rank(cl[0]))
    elif spacing_hint:
        spacing = spacing_hint
    else:
        spacing = max(first["w"] * 1.8, first["h"] * 3.2)
    cx = first["cx"] + (want - rank(first)) * spacing
    return {"text": label, "n": label, "x": cx - first["w"] / 2, "y": first["y"],
            "w": first["w"], "h": first["h"], "cx": cx, "cy": first["cy"], "estimated": True}


def find_cards(items, frame_h, frame=None, ox=0, oy=0):
    """以每張卡片下方那排按鈕（重抽／專注／放逐）為基準，找出卡片標題與稀有度。
    卡片直排（手機）或橫排（電腦）都適用。"""
    # 先分成一排一排，每排再由左到右排序
    rows = []
    for b in sorted([i for i in items if _is_button(i)], key=lambda i: i["cy"]):
        if rows and abs(b["cy"] - rows[-1][0]["cy"]) < 25:
            rows[-1].append(b)
        else:
            rows.append([b])
    btns = [b for row in rows for b in sorted(row, key=lambda i: i["cx"])]
    # 把同一排、靠在一起的按鈕合成一張卡
    def rank(i):
        n = i["n"]
        return 0 if n == "重抽" else (1 if (n.startswith("专") or n.endswith("注")) else 2)

    clusters = []
    for b in btns:
        for cl in clusters:
            last = cl[-1]
            # 同一排、距離不遠，而且按鈕順序是 重抽 → 專注 → 放逐
            if abs(b["cy"] - last["cy"]) < 25 and b["x"] - (last["x"] + last["w"]) < 200 \
                    and rank(b) > rank(last):
                cl.append(b)
                break
        else:
            clusters.append([b])
    if not clusters:
        return []
    # 按鈕間距（重抽→專注→放逐 每一格的距離），取所有卡片的中位數
    gaps = []
    for cl in clusters:
        cs = sorted(cl, key=lambda i: i["cx"])
        if len(cs) >= 2 and rank(cs[-1]) != rank(cs[0]):
            gaps.append((cs[-1]["cx"] - cs[0]["cx"]) / (rank(cs[-1]) - rank(cs[0])))
    spacing_hint = sorted(gaps)[len(gaps) // 2] if gaps else None

    cards = []
    for cl in clusters:
        cl.sort(key=lambda i: i["cx"])
        x0 = min(i["x"] for i in cl)
        x1 = max(i["x"] + i["w"] for i in cl)
        cards.append({"cx": (x0 + x1) / 2, "cy": sum(i["cy"] for i in cl) / len(cl),
                      "banish": _banish_button(cl, rank, spacing_hint),
                      "reroll": _reroll_button(cl, rank, spacing_hint),
                      "focus": _reroll_button(cl, rank, spacing_hint, 1, "專注")})

    # 每張卡左右能涵蓋的範圍：同一排相鄰卡片距離的一半
    for c in cards:
        same_row = [d for d in cards if d is not c and abs(d["cy"] - c["cy"]) < 40]
        c["tol"] = min([abs(d["cx"] - c["cx"]) for d in same_row] + [600]) * 0.5
        c["columns"] = bool(same_row)

    # 標題區上緣：技能欄／說明文字之下
    header = [i for i in items if SLOT_RE.match(i["n"]) or "已满" in i["n"]
              or "选择技能" in i["n"] or "强化技能" in i["n"]]
    top_y = max((i["y"] + i["h"] for i in header), default=0)

    def usable(i):
        return i["n"] and not _is_button(i) and not SLOT_RE.match(i["n"]) \
            and len(i["n"]) >= 2 and i["n"] not in ("新", "重抽全部") \
            and not re.match(r"^l[vy]+\d", i["n"]) and not i["n"].startswith("选一张") \
            and not i["text"].strip().startswith(("+", "-", "＋", "－")) and "%" not in i["text"]

    groups = {id(c): [] for c in cards}
    for i in items:
        if i["cy"] <= top_y or _is_button(i):
            continue
        cand = [c for c in cards if c["cy"] > i["cy"] and abs(i["cx"] - c["cx"]) < c["tol"]]
        if cand:
            groups[id(min(cand, key=lambda c: c["cy"] - i["cy"]))].append(i)

    out = []
    for c in cards:
        g = sorted(groups[id(c)], key=lambda i: i["cy"])
        tags = [i for i in g if tag_rank(i["n"]) is not None]
        texts = [i for i in g if usable(i) and tag_rank(i["n"]) is None]
        if not texts:
            continue
        rarity, title = 0, None
        # 技能名稱下面那行說明（「(Frost Diver)·單體·詠唱 1.1 s」「冰凍：2.6s」「Lv 2」）不能當名稱；
        # 名稱沒讀到時，那行會變成第一行，要跳過
        def subtitle_like(i):
            t_ = i["text"].strip()
            return t_.startswith(("(", "（", "《")) or bool(re.search(r"\d+(\.\d+)?\s*[sS秒]\s*$", t_)) \
                or bool(re.search(r"[:：]\s*\d", t_)) or "唱" in t_ or "·" in t_ and len(t_) > 8
        known = [i for i in texts if any(match(i["text"], k) for k in KNOWN_SKILLS)]
        if tags:
            t = tags[0]
            rarity = tag_rank(t["n"])
            below = [i for i in texts if i["cy"] > t["cy"]]
            kb = [i for i in below if i in known]
            if kb:
                title = kb[0]
            elif below:
                if subtitle_like(below[0]):
                    continue      # 名稱沒讀到（第一行是說明）→ 這張先當看不清楚，重讀畫面
                title = below[0]
        elif known:
            title = known[0]
        elif subtitle_like(texts[0]):
            continue
        if title is None:
            title = texts[0]
            r = rarity_by_color(frame, title, ox, oy, c.get("columns", False))
            if r is not None:
                rarity = r
        lines = [i for i in g if i["cy"] > title["cy"] and not _is_button(i)]
        # 新技能卡有英文名稱 → 用它確定是哪個技能（中文標題常被讀錯、或讀成下面的說明）
        en = next((en_skill(i["text"]) for i in g if en_skill(i["text"])), None)
        if en and canon_skill(title["text"]) != en:
            title = dict(title, text=en, n=norm(en))
        out.append({"title": title, "rarity": rarity, "banish": c["banish"], "reroll": c["reroll"],
                    "focus": c["focus"],
                    "lines": lines, "en": en})
    return out


def chest_match(card, name):
    """卡片是不是清單上的這一項；同名的（廣域藥水／廣域祝福）再看效果有沒有「近戰」"""
    if not match(card["title"]["text"], chest_title(name)):
        return False
    v = chest_variant(name)
    if v is None:
        return True
    text = " ".join(i["n"] for i in card.get("lines", []))
    melee = "近战" in text
    return melee if v == "近戰" else not melee


def chest_identify(c):
    """這張寶箱卡是清單上的哪一項（全部寶箱裡）；標題沒讀到時看說明的關鍵字。認不出來回傳 None"""
    for name, _ in CHEST_ITEMS:
        if chest_match(c, name):
            return name
    txt = norm(" ".join([c["title"]["text"]] + [i["text"] for i in c.get("lines", [])]))
    for kw, name in CHEST_KEYWORDS:
        if norm(kw) in txt:
            return name
    return None


def chest_priority(sec):
    """寶箱目前要用的優先清單：遊戲時間到了 late_from 分鐘、而且有設定後期排名 → 用後期排名"""
    late = [x for x in (sec.get("priority_late") or []) if x]
    try:
        lf = float(sec.get("late_from", 0) or 0)
    except (TypeError, ValueError):
        lf = 0
    if late and lf > 0 and GAME.get("max_timer", 0) >= lf * 60:
        if not GAME.get("late_logged"):
            GAME["late_logged"] = True
            log(f"遊戲時間過了 {lf:g} 分鐘 → 寶箱改用後期排名（前 3 名：{'、'.join(late[:3])}）")
        return late
    return sec.get("priority", [])


def choose_card(cards, sec, item_slots=None):
    """寶箱選卡：照優先清單排名。道具欄還沒滿時，新道具會永久佔一格 →
    排名太後面的新道具（前 new_item_rank 名之外）先不拿，寧可升級已經有的"""
    slots_free = bool(item_slots) and item_slots[0] < item_slots[1]
    new_rank = int(sec.get("new_item_rank", sec.get("reroll_rank", 10)) or 0)
    for c in cards:
        s = c["rarity"] if sec.get("prefer_rarity", True) else 0
        c["pri"] = False
        c["rank"] = None          # 在優先清單的第幾名（1 起算）；不在清單 = None
        c["item"] = chest_identify(c)
        for idx, kw in enumerate(sec.get("priority", [])):
            if chest_match(c, kw) or (c["item"] and c["item"] == kw):
                s += 1000 - 10 * idx
                c["pri"] = True
                c["rank"] = idx + 1
                break
        # 標題後面有「+數字」= 已經有的道具（升級）；沒有 = 新道具
        c["owned"] = bool(re.search(r"\+\s*\d+\s*$", c["title"]["text"]))
        c["bad_new"] = slots_free and not c["owned"] and new_rank > 0 and \
            (c["rank"] is None or c["rank"] > new_rank)
        if c["bad_new"]:
            s -= 3000
        c["avoid"] = any(match(c["title"]["text"], kw) for kw in sec.get("avoid", []))
        if c["avoid"]:
            s -= 5000
        c["score"] = s
    return max(cards, key=lambda c: c["score"]) if cards else None


# =====================================================================
#  擬人化滑鼠
# =====================================================================
def _min_jerk(t):
    return t ** 3 * (10 - 15 * t + 6 * t * t)


def human_path(x0, y0, x1, y1, target_w=30):
    dist = math.hypot(x1 - x0, y1 - y0)
    if dist < 1:
        return [(x1, y1, 0.0)]
    dur = (0.08 + 0.09 * math.log2(dist / max(target_w, 5) + 1)) * random.uniform(0.85, 1.25)
    n = max(10, int(dur / 0.008))
    ux, uy = (x1 - x0) / dist, (y1 - y0) / dist
    px, py = -uy, ux
    spread = min(dist * 0.3, 160)
    o1 = random.uniform(-spread, spread)
    o2 = o1 * random.uniform(0.1, 0.9) + random.uniform(-spread, spread) * 0.3
    a1, a2 = random.uniform(0.2, 0.4), random.uniform(0.6, 0.85)
    c1 = (x0 + ux * dist * a1 + px * o1, y0 + uy * dist * a1 + py * o1)
    c2 = (x0 + ux * dist * a2 + px * o2, y0 + uy * dist * a2 + py * o2)
    pts = []
    for i in range(1, n + 1):
        t = i / n
        s = _min_jerk(t)
        u = 1 - s
        bx = u**3 * x0 + 3 * u**2 * s * c1[0] + 3 * u * s**2 * c2[0] + s**3 * x1
        by = u**3 * y0 + 3 * u**2 * s * c1[1] + 3 * u * s**2 * c2[1] + s**3 * y1
        j = (1 - s) * min(1.5, dist / 300)
        pts.append((bx + random.gauss(0, j), by + random.gauss(0, j), dur * t))
    pts[-1] = (x1, y1, dur)
    return pts


def _play(path):
    t0 = time.perf_counter()
    last = None
    for x, y, t in path:
        while time.perf_counter() - t0 < t:
            time.sleep(0.001)
        p = (round(x), round(y))
        if p != last:
            IO.move_to(*p)
            last = p


def human_move(x1, y1, target_w=30):
    x0, y0 = IO.position()
    dist = math.hypot(x1 - x0, y1 - y0)
    if dist > 250 and random.random() < 0.35:
        ux, uy = (x1 - x0) / dist, (y1 - y0) / dist
        over = random.uniform(4, 14)
        ox, oy = x1 + ux * over + random.uniform(-4, 4), y1 + uy * over + random.uniform(-4, 4)
        _play(human_path(x0, y0, ox, oy, target_w))
        time.sleep(random.uniform(0.03, 0.12))
        _play(human_path(ox, oy, x1, y1, target_w))
    else:
        _play(human_path(x0, y0, x1, y1, target_w))


ACTION = {}   # 最近一次實際做的動作（除錯截圖標記用）


class Input:
    def __init__(self, dry):
        self.dry = dry

    def click_item(self, item, label=""):
        x, y, w, h = item["x"], item["y"], item["w"], item["h"]
        tx = round(x + w / 2 + max(-0.3, min(0.3, random.gauss(0, 0.12))) * w)
        ty = round(y + h / 2 + max(-0.3, min(0.3, random.gauss(0, 0.12))) * h)
        if self.dry:
            log(f"  [測試模式] 會點擊「{label or item['text']}」於 ({tx}, {ty})")
            return
        human_move(tx, ty, target_w=min(w, h))
        time.sleep(random.uniform(0.06, 0.22))
        IO.mouse_down()
        time.sleep(random.uniform(0.05, 0.13))
        IO.mouse_up()
        ACTION.update(kind="click", x=tx, y=ty, label=label or item["text"], t=time.time())
        return tx, ty

    def scroll(self, x, y, notches, label=""):
        """滾輪；notches > 0 往下捲"""
        if self.dry:
            log(f"  [測試模式] 會在 ({round(x)}, {round(y)}) 捲動 {notches} 格 {label}")
            return
        human_move(x, y, target_w=40)
        time.sleep(random.uniform(0.08, 0.2))
        step = 1 if notches > 0 else -1
        for _ in range(abs(int(notches))):
            IO.wheel(x, y, step)
            time.sleep(random.uniform(0.05, 0.12))

    def press(self, key):
        if self.dry:
            log(f"  [測試模式] 會按下 {key.upper()}")
            return
        IO.key_down(key)
        time.sleep(random.uniform(0.05, 0.12))
        IO.key_up(key)


# =====================================================================
#  移動（背景執行緒）
# =====================================================================
class Mover(threading.Thread):
    def __init__(self, mcfg, dry):
        super().__init__(daemon=True)
        self.mode = mcfg.get("mode", "keys")
        self.keymap = mcfg.get("keys", DEFAULT_CONFIG["move"]["keys"])
        self.radius = mcfg.get("mouse_radius", 160)
        self.dry = dry
        self.lock = threading.Lock()
        self.dir = None
        self.active = False
        self.center = (0, 0)
        self.held = set()
        self.mouse_held = False
        self.stop_flag = False

    def set(self, direction, active, center=None):
        with self.lock:
            self.dir, self.active = direction, active
            if center:
                self.center = center

    def _keys_for(self, d):
        dx, dy = d
        ks = set()
        if dy < -0.38:
            ks.add(self.keymap["up"])
        if dy > 0.38:
            ks.add(self.keymap["down"])
        if dx < -0.38:
            ks.add(self.keymap["left"])
        if dx > 0.38:
            ks.add(self.keymap["right"])
        return ks

    def _apply_keys(self, want):
        for k in list(self.held - want):
            if not self.dry:
                IO.key_up(k)
            self.held.discard(k)
        for k in list(want - self.held):
            time.sleep(random.uniform(0.01, 0.05))
            if not self.dry:
                IO.key_down(k)
            self.held.add(k)

    def _apply_mouse(self, d, c):
        if d is None:
            if self.mouse_held and not self.dry:
                IO.mouse_up()
            self.mouse_held = False
            return
        tx = c[0] + d[0] * self.radius + random.uniform(-6, 6)
        ty = c[1] + d[1] * self.radius + random.uniform(-6, 6)
        if self.dry:
            self.mouse_held = True
            return
        px, py = IO.position()
        if math.hypot(tx - px, ty - py) > 25:
            human_move(tx, ty, target_w=60)
        if not self.mouse_held:
            IO.mouse_down()
            self.mouse_held = True

    def release(self):
        self._apply_keys(set())
        self._apply_mouse(None, None)

    def run(self):
        next_break = time.time() + random.uniform(8, 20)
        while not self.stop_flag:
            with self.lock:
                d, act, c = self.dir, self.active, self.center
            try:
                if act and d is not None and time.time() >= next_break:
                    self.release()  # 偶爾放開一下，像真人
                    time.sleep(random.uniform(0.08, 0.35))
                    next_break = time.time() + random.uniform(8, 20)
                    continue
                if self.mode == "keys":
                    self._apply_keys(self._keys_for(d) if act and d is not None else set())
                else:
                    self._apply_mouse(d if act else None, c)
            except FailSafe:
                self.stop_flag = True
                break
            except Exception:
                # 和 Edge 的連線暫時斷掉（例如定時重開 Edge）：當成按鍵都放開了，等一下再繼續
                self.held.clear()
                self.mouse_held = False
                time.sleep(0.3)
            time.sleep(random.uniform(0.03, 0.06))
        try:
            self.release()
        except Exception:
            pass


# =====================================================================
#  影像：金幣、礦石、寶箱
# =====================================================================
def _blobs(mask, amin, amax, smax):
    n, _, st, cen = cv2.connectedComponentsWithStats(mask)
    pts = [(float(cen[i][0]), float(cen[i][1])) for i in range(1, n)
           if amin <= st[i, 4] <= amax and st[i, 2] <= smax and st[i, 3] <= smax]
    merged = []
    for p in pts:  # 同一顆東西可能被切成兩塊
        if all(abs(p[0] - q[0]) > 9 or abs(p[1] - q[1]) > 9 for q in merged):
            merged.append(p)
    return merged


class Vision:
    def __init__(self, cfg):
        m = cfg["move"]
        self.coin_lo = np.array(m["coin_hsv_low"], np.uint8)
        self.coin_hi = np.array(m["coin_hsv_high"], np.uint8)
        self.ore_lo = np.array(m["ore_hsv_low"], np.uint8)
        self.ore_hi = np.array(m["ore_hsv_high"], np.uint8)
        self.th = m["chest_threshold"]
        self.scales = m["chest_scales"]
        self.item_mode = m.get("item_detector", "shape")
        self.ore_thr = float(m.get("ore_match", 0.7))
        self.ore_tpl = None
        try:
            t = cv2.imread(os.path.join(TEMPLATE_DIR, "ore.png"), cv2.IMREAD_GRAYSCALE)
            tm = cv2.imread(os.path.join(TEMPLATE_DIR, "ore_mask.png"), cv2.IMREAD_GRAYSCALE)
            if t is not None and tm is not None:
                self.ore_tpl = (t, tm)
        except Exception:
            pass
        self.hud = cfg["hud_ignore"]
        self.templates = []
        if os.path.isdir(TEMPLATE_DIR):
            for fn in sorted(os.listdir(TEMPLATE_DIR)):
                if fn.lower().startswith("chest") and fn.lower().endswith(".png"):
                    img = cv2.imdecode(np.fromfile(os.path.join(TEMPLATE_DIR, fn), np.uint8),
                                       cv2.IMREAD_COLOR)
                    if img is not None:
                        self.templates.append(img)
        log(f"載入寶箱範本 {len(self.templates)} 張")
        self._mask_cache = {}
        self.last_water = None

    def _base_mask(self, h, w):
        key = (h, w)
        if key not in self._mask_cache:
            mask = np.full((h, w), 255, np.uint8)
            for x0, y0, x1, y1 in self.hud:
                mask[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)] = 0
            r = int(min(w, h) * 0.06)  # 角色本身
            cv2.circle(mask, (w // 2, h // 2), r, 0, -1)
            self._mask_cache[key] = mask
        return self._mask_cache[key]

    def water_grid(self, frame, hsv=None, cell=8):
        """戰鬥地圖上的水池（藍色、大塊）；回傳每 8 像素一格的障礙圖（1 = 水）"""
        h, w = frame.shape[:2]
        gw, gh = w // cell, h // cell
        if hsv is None:
            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        small = cv2.resize(hsv, (gw, gh), interpolation=cv2.INTER_AREA)
        m = ((small[:, :, 0] >= 95) & (small[:, :, 0] <= 125) & (small[:, :, 1] >= 90)
             & (small[:, :, 2] >= 50) & (small[:, :, 2] <= 200)).astype(np.uint8)
        hud = cv2.resize(self._base_mask(h, w), (gw, gh), interpolation=cv2.INTER_NEAREST) > 0
        m[~hud] = 0
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))   # 去掉細長的閃電
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        n, lab, st, _ = cv2.connectedComponentsWithStats(m)
        out = np.zeros_like(m)
        for i in range(1, n):
            if st[i, 4] >= 25:          # 水池是大塊的
                out[lab == i] = 1
        return out

    def _items_by_shape(self, frame, hsv, mask):
        """礦石：用形狀比對（灰階、只看礦石本體，不受地圖明暗和色調影響）；
        金幣：黃色小圓，而且旁邊有礦石（金幣都跟礦石一起掉）或顏色非常明確"""
        g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        tpl, tmask = self.ore_tpl
        r = cv2.matchTemplate(g, tpl, cv2.TM_CCOEFF_NORMED, mask=tmask)
        r = np.nan_to_num(r, nan=0, posinf=0, neginf=0)
        th, tw = tpl.shape
        Hh, S, V = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
        ores = []
        ys, xs = np.where(r >= self.ore_thr)
        for i in np.argsort(-r[ys, xs])[:400]:
            px, py = xs[i] + tw / 2, ys[i] + th / 2
            ix, iy = int(px), int(py)
            if mask[iy, ix] == 0:
                continue
            if not (S[iy, ix] < 60 or 95 <= Hh[iy, ix] <= 150):     # 礦石不是鮮豔的暖色
                continue
            if all(abs(px - q[0]) > 7 or abs(py - q[1]) > 7 for q in ores):
                ores.append((float(px), float(py)))
        loc = cv2.blur(V, (31, 31)).astype(np.int32)
        cm = ((Hh >= 15) & (Hh <= 35) & (S >= 80) & (V >= 55)
              & (V.astype(np.int32) * 100 >= loc * 115)).astype(np.uint8)
        cm[mask == 0] = 0
        n, _, st, cen = cv2.connectedComponentsWithStats(cm)
        coins = []
        for i in range(1, n):
            x, y, w_, h_, a = st[i]
            if not (14 <= a <= 140 and w_ <= 15 and h_ <= 15 and 0.5 <= w_ / max(h_, 1) <= 2
                    and a >= 0.45 * w_ * h_):
                continue
            cx, cy = cen[i]
            strong = V[int(cy), int(cx)] >= 225 and S[int(cy), int(cx)] >= 105
            if strong or any(abs(cx - q[0]) < 16 and abs(cy - q[1]) < 16 for q in ores):
                coins.append((float(cx), float(cy)))
        return coins, ores

    def detect(self, frame):
        h, w = frame.shape[:2]
        base = self._base_mask(h, w)
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)

        # 閃電等特效（大片亮白）附近不算
        glow = cv2.inRange(hsv, (0, 0, 236), (180, 70, 255))
        glow = cv2.dilate(glow, np.ones((15, 15), np.uint8))
        mask = cv2.bitwise_and(base, cv2.bitwise_not(glow))

        if self.ore_tpl is not None and self.item_mode == "shape":
            coins, ores = self._items_by_shape(frame, hsv, base)   # 形狀比對不需要排除亮光
        else:
            cm = cv2.inRange(hsv, self.coin_lo, self.coin_hi) & mask
            cm = cv2.morphologyEx(cm, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
            coins = _blobs(cm, 25, 200, 18)
            om = cv2.inRange(hsv, self.ore_lo, self.ore_hi) & mask
            ores = _blobs(om, 8, 80, 14)

        # 寶箱：先在縮小一半的畫面粗找，再用原尺寸確認（快很多）
        chests = []
        small = cv2.resize(frame, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
        for tpl in self.templates:
            for s in self.scales:
                t = tpl if s == 1.0 else cv2.resize(tpl, None, fx=s, fy=s)
                th_, tw_ = t.shape[:2]
                ts = cv2.resize(t, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
                if ts.shape[0] < 6 or th_ >= h or tw_ >= w:
                    continue
                r = cv2.matchTemplate(small, ts, cv2.TM_CCOEFF_NORMED)
                ys, xs = np.where(r >= self.th - 0.18)
                order = np.argsort(-r[ys, xs])[:40]
                for k in order:
                    x, y = int(xs[k]) * 2, int(ys[k]) * 2
                    x0, y0 = max(0, x - 6), max(0, y - 6)
                    patch = frame[y0:min(h, y + th_ + 6), x0:min(w, x + tw_ + 6)]
                    if patch.shape[0] < th_ or patch.shape[1] < tw_:
                        continue
                    rr = cv2.matchTemplate(patch, t, cv2.TM_CCOEFF_NORMED)
                    _, sc, _, loc = cv2.minMaxLoc(rr)
                    if sc < self.th:
                        continue
                    cx, cy = x0 + loc[0] + tw_ / 2, y0 + loc[1] + th_ / 2
                    if base[int(cy), int(cx)] == 0:
                        continue
                    chests.append((float(sc), cx, cy))
        chests.sort(reverse=True)
        picked = []
        for sc, cx, cy in chests:
            if all(abs(cx - a) > 20 or abs(cy - b) > 20 for a, b in picked):
                picked.append((cx, cy))
        # 寶箱附近的「礦石／金幣」多半是寶箱本身的顏色，去掉
        ores = [p for p in ores if all(math.hypot(p[0] - a, p[1] - b) > 18 for a, b in picked)]
        coins = [p for p in coins if all(math.hypot(p[0] - a, p[1] - b) > 18 for a, b in picked)]

        # 水池裡的東西撿不到，不追
        self.last_water = self.water_grid(frame, hsv)
        if self.last_water.mean() > 0.2:      # 一大片藍色 = 地板本身是藍的（例如神殿），不是水池
            self.last_water[:] = 0
        if self.last_water.any():
            wd = cv2.dilate(self.last_water, np.ones((3, 3), np.uint8))
            gh, gw = wd.shape

            def dry(p):
                cx, cy = int(p[0] // 8), int(p[1] // 8)
                return not (0 <= cx < gw and 0 <= cy < gh and wd[cy, cx])
            coins = [p for p in coins if dry(p)]
            ores = [p for p in ores if dry(p)]
            picked = [p for p in picked if dry(p)]
        return coins, ores, picked


class Planner:
    """決定要往哪裡走：先寶箱，再往金幣／礦石最密集的地方"""

    def __init__(self, mcfg):
        self.m = mcfg
        self.mode = None
        self.since = 0
        self.ignore_chest_until = 0
        self.target = None
        self.repick_at = 0

    def plan(self, coins, ores, chests, cx, cy, now, avoid=()):
        m = self.m
        if avoid:  # 之前卡住、走不到的地方附近的東西先不追
            def ok(p):
                return all(math.hypot(p[0] - a, p[1] - b) > 80 for a, b in avoid)
            coins = [p for p in coins if ok(p)]
            ores = [p for p in ores if ok(p)]
            chests = [p for p in chests if ok(p)]
        if chests and now >= self.ignore_chest_until:
            t = min(chests, key=lambda p: math.hypot(p[0] - cx, p[1] - cy))
            if self.mode != "chest":
                self.mode, self.since = "chest", now
                log(f"發現寶箱 {len(chests)} 個，前往最近的一個")
            elif now - self.since > m["chest_timeout"]:
                log("寶箱一直撿不到（可能走不過去），先去撿別的")
                self.ignore_chest_until = now + 10
                self.mode = None
            self.target = t
            return t
        if self.mode == "chest":
            self.mode = None
            self.repick_at = 0

        pts = [(x, y, m["coin_weight"]) for x, y in coins] + \
              [(x, y, m["ore_weight"]) for x, y in ores]
        if not pts:
            self.mode, self.target = None, None
            return None

        # 已經接近目標，或時間到了，才重新挑（避免左右搖擺）
        if self.target is not None and now < self.repick_at and self.mode == "items" \
                and math.hypot(self.target[0] - cx, self.target[1] - cy) > 30:
            return None  # 沿用目前方向

        a = np.array(pts, dtype=np.float32)
        d = np.hypot(a[:, 0] - cx, a[:, 1] - cy)
        if len(a) > 600:
            idx = np.argsort(d)[:600]
            a, d = a[idx], d[idx]
        dx = a[:, 0][:, None] - a[:, 0][None, :]
        dy = a[:, 1][:, None] - a[:, 1][None, :]
        near = (dx * dx + dy * dy) < 90 * 90
        density = near.astype(np.float32) @ a[:, 2]
        score = density / ((d + 60) / 100) ** 1.3
        score[d < 45] = -1  # 太近的會自己被吸過來，不用特地走
        if score.max() <= 0:
            self.mode, self.target = None, None
            return None
        i = int(np.argmax(score))
        self.target = (float(a[i, 0]), float(a[i, 1]))
        self.mode = "items"
        self.repick_at = now + random.uniform(0.4, 0.8)
        return self.target


def rotate(d, deg):
    a = math.radians(deg)
    return (d[0] * math.cos(a) - d[1] * math.sin(a), d[0] * math.sin(a) + d[1] * math.cos(a))


def blend(old, new, k=0.6):
    if old is None:
        return new
    x, y = old[0] * (1 - k) + new[0] * k, old[1] * (1 - k) + new[1] * k
    m = math.hypot(x, y) or 1
    return x / m, y / m


# =====================================================================
#  背景文字辨識（選單、升級按鈕、小地圖座標）
# =====================================================================
class OcrWatcher(threading.Thread):
    def __init__(self, cfg):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.lock = threading.Lock()
        self.rect = None
        self.res = {"kind": None, "hud": 0, "coord": None, "t": 0, "coord_t": 0, "ingame_t": 0}
        self.stop_flag = False
        self.paused = False

    def set_rect(self, rect):
        with self.lock:
            self.rect = rect

    def get(self):
        with self.lock:
            return dict(self.res)

    def run(self):
        coord_next = 0
        with _new_mss() as sct:
            while not self.stop_flag:
                with self.lock:
                    rect = self.rect
                if rect is None or self.paused:
                    time.sleep(0.1)
                    continue
                try:
                    x, y, W, H = rect
                    sx, sw, sh = int(W * 0.2), int(W * 0.6), int(H * 0.4)
                    img = grab(sct, (x + sx, y, sw, sh))
                    items = ocr(img, x + sx, y, engine="bg")
                    upd = {"kind": detect_menu(items), "hud": hud_upgrade_count(items),
                           "timer": read_timer(items), "t": time.time()}
                    if in_game(items):
                        upd["ingame_t"] = upd["t"]
                    # 頭目機制：「躲到鐘下！」
                    if any(k in i["n"] for i in items for k in ("钟下", "躲到", "到钟")):
                        upd["bell_t"] = upd["t"]
                    # 「一大群怪物包圍…」這類事件提示 → 準備跑出包圍
                    if any("包围" in i["n"] or "一大群" in i["n"] for i in items):
                        upd["swarm_t"] = upd["t"]
                    # 頭目（MVP）出現：上方有「MVP 名字」和頭目血條
                    for i in items:
                        if "mvp" in i["n"] or (i["n"].startswith("头目") and len(i["n"]) <= 10
                                                and i["cy"] < y + H * 0.3 and not upd["kind"]):
                            upd["mvp_t"] = upd["t"]
                            nm_ = re.sub(r"(?i)mvp|頭目|头目|出現了|出现了|[!！\s]", "", i["text"])
                            if not nm_:
                                nx_ = [j for j in items if j is not i and abs(j["cy"] - i["cy"]) < i["h"]
                                       and 0 < j["x"] - (i["x"] + i["w"]) < i["h"] * 3]
                                if nx_:
                                    nm_ = re.sub(r"出現了|出现了|[!！\s]", "", nx_[0]["text"])
                            if nm_:
                                upd["mvp_name"] = nm_[:8]
                            break
                    now = time.time()
                    if now >= self.__dict__.get("lv_next", 0):
                        self.lv_next = now + 1.5
                        lsub = grab(sct, (x, y, max(10, int(W * 0.3)), max(10, int(H * 0.12))))
                        litems = ocr(lsub, engine="bg")
                        lv_ = read_char_level(litems)
                        if lv_ is not None:
                            upd["char_lv"], upd["char_lv_t"] = lv_, now
                        for i in litems:      # 左上角 HP「466 / 466」
                            mh = re.match(r"^\s*(\d{1,6})\s*/\s*(\d{1,6})\s*$", i["text"])
                            if mh and 0 < int(mh.group(2)) and int(mh.group(1)) <= int(mh.group(2)):
                                upd["hp"], upd["hp_max"], upd["hp_t"] = int(mh.group(1)), int(mh.group(2)), now
                                break
                    if self.cfg["move"]["stuck_check"] and now >= coord_next:
                        coord_next = now + 1.2
                        cx0, cy0, cx1, cy1 = self.cfg["coord_region"]
                        sub = grab(sct, (x + int(cx0 * W), y + int(cy0 * H),
                                         max(10, int((cx1 - cx0) * W)), max(10, int((cy1 - cy0) * H))))
                        txt = " ".join(i["text"] for i in ocr(sub, scale=2.0, engine="bg"))
                        mm = re.search(r"(\d+)\s*[,，.]\s*(\d+)", txt)
                        if mm:
                            upd["coord"] = (int(mm.group(1)), int(mm.group(2)))
                            upd["coord_t"] = now
                            nm = map_name_of(txt)
                            if nm and nm == self.__dict__.get("last_map"):
                                upd["map_name"] = nm
                            self.last_map = nm
                    with self.lock:
                        self.res.update(upd)
                except Exception as e:
                    log(f"背景辨識錯誤：{e}")
                    time.sleep(0.5)
                time.sleep(0.12)


# =====================================================================
#  除錯截圖
# =====================================================================
_snap_files = []


def save_action_snapshot(frame, ox, oy, rel):
    """把實際按下／點擊的位置標在截圖上（logs/menu_act_*.jpg）"""
    img = frame.copy()
    for i in rel:
        cv2.rectangle(img, (int(i["x"]), int(i["y"])), (int(i["x"] + i["w"]), int(i["y"] + i["h"])),
                      (0, 200, 255), 1)
    x, y = int(ACTION.get("x", 0) - ox), int(ACTION.get("y", 0) - oy)
    color = (0, 0, 255) if ACTION.get("kind") == "click" else (0, 255, 0)
    cv2.circle(img, (x, y), 14, color, 3)
    cv2.line(img, (x - 22, y), (x + 22, y), color, 2)
    cv2.line(img, (x, y - 22), (x, y + 22), color, 2)
    save_snapshot(img, "menu_act")


def save_snapshot(frame, name, coins=(), chests=(), center=None, d=None, items=(), chosen=None,
                  ores=(), target=None):
    os.makedirs(LOG_DIR, exist_ok=True)
    img = frame.copy()
    for x, y in ores:
        cv2.circle(img, (int(x), int(y)), 6, (255, 0, 255), 1)
    for x, y in coins:
        cv2.circle(img, (int(x), int(y)), 8, (0, 0, 255), 2)
    for x, y in chests:
        cv2.rectangle(img, (int(x) - 16, int(y) - 13), (int(x) + 16, int(y) + 13), (0, 255, 0), 2)
    if target:
        cv2.drawMarker(img, (int(target[0]), int(target[1])), (0, 255, 255), cv2.MARKER_CROSS, 28, 3)
    if center and d:
        cv2.arrowedLine(img, (int(center[0]), int(center[1])),
                        (int(center[0] + d[0] * 120), int(center[1] + d[1] * 120)),
                        (255, 255, 0), 3, tipLength=0.25)
    for i in items:
        cv2.rectangle(img, (int(i["x"]), int(i["y"])), (int(i["x"] + i["w"]), int(i["y"] + i["h"])),
                      (0, 200, 255), 1)
    if chosen:
        cv2.rectangle(img, (int(chosen["x"]) - 4, int(chosen["y"]) - 4),
                      (int(chosen["x"] + chosen["w"]) + 4, int(chosen["y"] + chosen["h"]) + 4),
                      (0, 255, 0), 3)
    path = os.path.join(LOG_DIR, time.strftime(f"{name}_%H%M%S.jpg"))
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
    if ok:
        buf.tofile(path)
        _snap_files.append(path)
    while len(_snap_files) > 40:
        try:
            os.remove(_snap_files.pop(0))
        except Exception:
            pass


def save_boss_shot(frame, game_no, prefix="boss"):
    """頭目戰截圖存到 logs\\boss（最多留 600 張，舊的自動刪）"""
    try:
        d = os.path.join(LOG_DIR, "boss")
        os.makedirs(d, exist_ok=True)
        t = time.time()
        path = os.path.join(d, time.strftime(f"{prefix}_g{game_no:03d}_%H%M%S", time.localtime(t))
                            + f"_{int(t * 10) % 10}.jpg")
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if ok:
            buf.tofile(path)
        files = sorted(f for f in os.listdir(d) if f.endswith(".jpg"))
        for f in files[:-600]:
            os.remove(os.path.join(d, f))
    except Exception as e:
        log(f"頭目截圖存檔失敗：{e}")


END_RING = []          # 最近 20 秒的畫面（JPEG），一局提早結束時存下來看是怎麼倒下的
END_CAP = {"games": 0}


def hp_dropping(now, mcfg):
    """最近 4.5 秒 HP 掉超過最大 HP 的 15%（被一群怪圍著打）→ 接下來 6 秒先跑開"""
    hh = GAME.get("hp_hist") or []
    rec = [h for h in hh if now - h[0] <= 4.5]      # HP 每 1.5 秒讀一次
    if len(rec) < 2:
        return False
    # 讀錯的數字（最大 HP 跟其他次差很多）不算
    mxs = sorted(h[2] for h in rec)
    mx = mxs[len(mxs) // 2]
    rec = [h for h in rec if mx and abs(h[2] - mx) <= mx * 0.2 and 0 <= h[1] <= h[2]]
    if len(rec) < 2:
        return False
    top = max(h[1] for h in rec)
    cur = rec[-1][1]
    if mx and (top - cur) / mx >= mcfg.get("hp_drop_kite", 0.15):
        GAME["hp_drop_until"] = now + 6
        return True
    return False


def crowded(now, dodger, hp, mcfg):
    """周圍很多東西在動（怪物）持續 2 秒，而且正在掉血 → 被圍住了"""
    lvl = dodger.crowd_level() if dodger is not None else None
    if lvl is None:
        return GAME.get("kite_why") == "被怪物圍住" and now < GAME.get("crowd_until", 0)
    hi = mcfg.get("crowd_level", 0.55)
    if GAME.get("kite_why") == "被怪物圍住":
        if lvl >= hi * 0.8:
            GAME["crowd_until"] = now + 2
        return now < GAME.get("crowd_until", 0)
    if lvl >= hi and hp and hp[1] and hp[0] < hp[1] * 0.85:
        GAME.setdefault("crowd_since", now)
        if now - GAME["crowd_since"] >= 2.0:
            GAME["crowd_until"] = now + 2
            return True
    else:
        GAME.pop("crowd_since", None)
    return False


def ring_push(frame, now):
    if END_RING and now - END_RING[-1][0] < 1.0:
        return
    try:
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
        if ok:
            END_RING.append((now, buf))
            del END_RING[:-20]
    except Exception:
        pass


def dump_end_ring(cfg, result_frame=None):
    """結果畫面出現時呼叫：還沒打到最後頭目就結束 → 把最後 20 秒存到 logs\\boss\\end_*"""
    try:
        mt = GAME.get("max_timer", 0)
        hp = GAME.get("last_hp")
        hp_s = f"、最後看到的 HP {hp[0]}/{hp[1]}" if hp else ""
        log(f"這局計時最多到 {mt // 60}:{mt % 60:02d}{hp_s}")
        if mt >= cfg["move"].get("boss_capture_from", 830) or not END_RING:
            return
        if END_CAP["games"] >= int(cfg["move"].get("end_capture", 5) or 0):
            return
        END_CAP["games"] += 1
        d = os.path.join(LOG_DIR, "boss")
        os.makedirs(d, exist_ok=True)
        tag = time.strftime("end_%m%d_%H%M%S")
        for k, (t, buf) in enumerate(END_RING):
            buf.tofile(os.path.join(d, f"{tag}_{k:02d}.jpg"))
        if result_frame is not None:
            ok, buf = cv2.imencode(".jpg", result_frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if ok:
                buf.tofile(os.path.join(d, f"{tag}_result.jpg"))
        log(f"這局提早結束（可能角色倒下）→ 最後 {len(END_RING)} 秒的畫面存在 logs\\boss\\{tag}_*.jpg")
    except Exception as e:
        log(f"結束前截圖存檔失敗：{e}")
    finally:
        END_RING.clear()


# =====================================================================
#  每日報表（logs\report_日期.txt / .csv）
# =====================================================================
def report_hp(hp):
    """每次讀到 HP：記這局最低 HP%，頭目在場時記頭目戰的最低 HP%"""
    if not hp or not hp[1]:
        return
    pct = hp[0] / hp[1]
    if GAME.get("hp_min_pct") is None or pct < GAME["hp_min_pct"]:
        GAME["hp_min_pct"] = pct
    b = GAME.get("boss_cur")
    if b is not None:
        if b.get("pct0") is None:
            b["pct0"] = pct
        b["min_pct"] = pct if b.get("min_pct") is None else min(b["min_pct"], pct)


def report_boss_start(name, t, hp):
    pct = hp[0] / hp[1] if hp and hp[1] else None
    b = {"name": name or "", "t": t, "pct0": pct, "min_pct": pct}
    GAME.setdefault("boss_log", []).append(b)
    GAME["boss_cur"] = b


def death_cause(now):
    """倒下的可能原因：頭目在場（或剛打完）→ 那隻頭目；不然看倒下前 30 秒在躲什麼"""
    import report as _rep
    b = GAME.get("boss_cur")
    if b is None and GAME.get("boss_log") and now - GAME.get("boss_end_t", 0) < 15:
        b = GAME["boss_log"][-1]
    if b is not None:
        return "頭目 " + _rep.boss_name(b.get("name"))
    recent = {w for w, t in GAME.get("kite_hist", []) if now - t < 30}
    for w, label in (("一大群怪物包圍", "一大群怪物包圍"), ("被怪物圍住", "被怪物圍住"),
                     ("HP 掉很快", "HP 掉很快（小怪）"), ("HP 太低", "HP 太低（小怪）")):
        if w in recent:
            return label
    return "不明（沒有頭目在場）"


def report_game_end(cfg):
    """結果畫面出現時呼叫：這一場記到每日報表"""
    try:
        import report as _rep
        now = time.time()
        mt = int(GAME.get("max_timer", 0) or 0)
        hp = GAME.get("last_hp")
        dead_hp = bool(hp and hp[0] == 0)
        if GAME.get("ending_logged") and not dead_hp:
            result = "通關"          # 有看到打完最後頭目的倒數
        elif dead_hp or 0 < mt < cfg["move"].get("boss_capture_from", 830):
            result = "倒下"          # HP 0，或還沒到最後頭目就結束
        else:
            result = "不確定"
        cause = death_cause(now) if result == "倒下" else ""
        tc = cfg["town"]
        stage = ("遠征：" + tc.get("stage", "")) if town_mode(tc) == "expedition" else "節慶主持人"
        rec = {"start": GAME.get("battle_t0"), "end": now, "stage": stage, "result": result,
               "max_timer": mt, "cause": cause, "last_hp": list(hp) if hp else None,
               "hp_min_pct": GAME.get("hp_min_pct"), "char_lv": GAME.get("char_lv"),
               "bosses": [dict(b) for b in GAME.get("boss_log", [])]}
        path = _rep.record(LOG_DIR, rec, cfg["move"].get("boss_times"))
        wb = _rep.worst_boss(rec)
        log(f"報表：{result}，計時 {mt // 60}:{mt % 60:02d}" + (f"，可能原因：{cause}" if cause else "")
            + (f"，扣最多血的頭目：{_rep.boss_name(wb[0].get('name'))} {wb[1]:.0f}%" if wb and wb[1] >= 1 else "")
            + f"（今天的報表：logs\\{os.path.basename(path)}）")
    except Exception as e:
        log(f"報表寫入失敗（不影響掛機）：{e}")
    finally:
        GAME.update(boss_log=[], boss_cur=None, hp_min_pct=None, kite_hist=[])


def save_image(img, name):
    """把圖存到 logs（跟其他除錯截圖一起輪替，最多留 40 張）"""
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, time.strftime(f"{name}_%H%M%S.jpg"))
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if ok:
        buf.tofile(path)
        _snap_files.append(path)
    while len(_snap_files) > 40:
        try:
            os.remove(_snap_files.pop(0))
        except Exception:
            pass


# =====================================================================
#  選單處理
# =====================================================================
MOVER = None
GAME = {"phase": None, "battle_seen": 0, "fourth": None, "fourth_ok": False, "fourth_pick": None,
        "slots": 0, "banish_off": False,
        "last_banish": None, "last_banish_t": 0, "banish_fail": 0}


def canon_name(text):
    """把讀錯的字對應回已知名稱（雷攀術、雷擎術 → 雷擊術），比對畫面有沒有換卡用"""
    for k in KNOWN_SKILLS + [chest_title(n) for n, _ in CHEST_ITEMS]:
        if match(text, k):
            return norm(k)
    return norm(text)


def similar(a, b):
    return match(a, b) or match(b, a)


def read_slots(items, kind="技能"):
    """「技能 3/4」→ (3, 4)；讀不到或讀起來不合理回傳 None"""
    for i in items:
        if i["n"].startswith(kind) and SLOT_RE.match(i["n"]):
            dg = i["n"][2:]
            used, total = int(dg[:-1]), int(dg[-1])
            if 1 <= total <= 8 and 0 <= used <= total:
                return used, total
    return None


def levelup_full(items, slots):
    """技能欄滿了沒（滿了就只會出現已經有的技能）：True／False／None（看不出來）"""
    if any("已满" in i["n"] or "只能强化" in i["n"] for i in items):
        return True
    if slots:
        return slots[0] >= slots[1]
    return None


def read_level(items):
    """從『Lv 5：+3% 傷害 · +0.5% HP』讀出目前等級"""
    for i in items:
        t = i["text"]
        if "%" in t and ("HP" in t.upper() or "伤害" in i["n"]):
            m = re.search(r"[Ll][Vv]\s*(\d+)", t)
            if m:
                return int(m.group(1))
    return None


def card_effect(c):
    """卡片上的效果行（例如『+20% 傷害』）與數值"""
    effs = [i for i in c.get("lines", []) if "%" in i["text"]
            or i["text"].strip().startswith(("+", "-", "＋", "－"))]
    text = " ".join(i["n"] for i in effs)
    raw = " ".join(i["text"] for i in effs)
    m = re.search(r"(\d+(?:\.\d+)?)", raw)
    return text, (float(m.group(1)) if m else 0.0)


def rank_main_skills(pri, items, sec):
    lv = read_level(items)
    if lv is None:
        lv = GAME.get("levelups", 0) + 2
    early = lv <= sec.get("target_until_level", 11)
    tw = [norm(w) for w in sec.get("target_words", ["目標"])]
    lw = [norm(w) for w in sec.get("late_words", ["傷害", "冷卻"])]
    order = {k: i for i, k in enumerate(sec["priority"])}
    for c in pri:
        eff, val = card_effect(c)
        # OCR 常漏字（例如「+40%害」），所以任何一個字對上就算
        is_target = any(ch in eff for w in tw for ch in w)
        is_late = any(ch in eff for w in lw for ch in w)
        if early:
            cat = 2 if is_target else (1 if is_late else 0)
        else:
            cat = 2 if is_late else (1 if is_target else 0)
        rar = c.get("rarity", 0) or 1  # 讀不到標籤就當普通
        pri_idx = min(order[k] for k in sec["priority"] if match(c["title"]["text"], k))
        # 排序：類型 → 稀有度（普通<優良<稀有<史詩<傳說）→ 數值 → 技能順序
        if sec.get("rarity_first", False):
            c["score"] = (rar, cat, val, -pri_idx)
        else:
            c["score"] = (cat, rar, val, -pri_idx)
        c["eff"] = eff
        c["rar_name"] = {1: "普通", 2: "優良", 3: "稀有", 4: "史詩", 5: "傳說", 6: "神話"}.get(rar, "?")
    best = max(pri, key=lambda c: c["score"])
    phase = "前期：優先增加目標" if early else "後期：優先傷害／冷卻"
    return best, lv, phase


def pick_card(card, cards, items, inp, use_keys=True):
    """選卡：畫面有「選一張卡片（1–3）」且三張卡都認到時，直接按數字鍵（快又不會點偏）"""
    if use_keys and len(cards) == 3 and any("一张卡片" in i["n"] or "1-3" in i["text"]
                                            or "1–3" in i["text"] for i in items):
        idx = cards.index(card) + 1
        inp.press(str(idx))
        t = card["title"]
        ACTION.update(kind="key", x=t["x"] + t["w"] / 2, y=t["y"] + t["h"] / 2,
                      label=f"按 {idx}：{t['text']}")
        return
    inp.click_item(card["title"])


def find_bell(frame):
    """斐揚洞穴頭目機制的金色大鐘：回傳最大那個鐘的位置（畫面座標）或 None"""
    b = find_bells(frame)
    return max(b, key=lambda c: c[2])[:2] if b else None


def find_bells(frame, relaxed=False):
    """畫面上所有的金色大鐘：[(x, y, 面積), ...]。
    relaxed：「躲到鐘下」期間用，比較小、或一半在畫面外的鐘也算"""
    H, W = frame.shape[:2]
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    m = ((hsv[:, :, 0] >= 10) & (hsv[:, :, 0] <= 28) & (hsv[:, :, 1] >= 70)
         & (hsv[:, :, 2] >= 90)).astype(np.uint8)
    m[:int(H * 0.16), :] = 0                                   # 上方 HUD
    m[:int(H * 0.35), int(W * 0.8):] = 0                       # 右上小地圖
    m[int(H * 0.86):, int(W * 0.33):int(W * 0.67)] = 0         # 下方技能欄
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    n, _, st, _ = cv2.connectedComponentsWithStats(m)
    out = []
    for i in range(1, n):
        x, y, w, h, a = st[i]
        edge = x <= 2 or x + w >= W - 2 or y + h >= H - 2 or y <= int(H * 0.16) + 2
        if w > W * 0.35 or h > H * 0.45:          # 整片沙地、木頭地板不是鐘
            continue
        if a >= 9000 and 0.5 < w / max(h, 1) < 2.0 and a >= 0.35 * w * h:
            pass
        elif relaxed and a >= 3500 and 0.4 < w / max(h, 1) < 2.5 and a >= 0.3 * w * h:
            pass                                  # 比較小（離比較遠／被擋住一部分）
        elif relaxed and edge and a >= 2000:
            pass                                  # 一半在畫面外
        else:
            continue
        out.append((x + w / 2, y + h * 0.6, a))
    return out


BELL_EP_SEC = 15     # 每次「躲到鐘下！」最多花幾秒找鐘、站在鐘底下


def bell_step(frame, lc, world, now, banner, hold_on):
    """頭目機制「躲到鐘下！」。回傳 ("move", 方向)／("stay", None)／None（現在不用躲鐘）
    - 讀到提示字 → 開始一次躲鐘（最多 15 秒）：一直找鐘、走過去、站在鐘底下（提示字消失也繼續）
    - 第二次躲鐘時，先去還沒站過的鐘（第一個鐘可能還留在畫面上）
    - 等頭目時看到新的鐘也會去（提示字沒讀到也不會漏掉）"""
    ep = GAME.get("bell_ep")
    done = GAME.setdefault("bells_done", [])
    wx, wy = world
    if GAME.get("ending_logged"):
        return None                                   # 頭目死了、最後倒數：不用再躲
    if ep is not None and now > ep["until"]:
        if not ep["reached"]:
            log(f"第 {ep['n']} 次躲鐘結束，沒有走到鐘底下")
        ep = GAME["bell_ep"] = None
    start = None
    if ep is None:
        if banner:
            start = "看到提示字"
        elif hold_on and GAME.get("bell_count", 0) >= 1:
            cw = [(bx - lc[0] + wx, by - lc[1] + wy) for bx, by, _ in find_bells(frame)]
            if any(all(math.hypot(c[0] - d[0], c[1] - d[1]) > 150 for d in done) for c in cw):
                start = "看到新的鐘"
    if start:
        n = GAME["bell_count"] = GAME.get("bell_count", 0) + 1
        ep = GAME["bell_ep"] = {"n": n, "start": now, "until": now + BELL_EP_SEC, "target": None,
                                "seen_t": 0.0, "reached": False, "missing": False}
        log(f"頭目機制：第 {n} 次躲到鐘下（{start}）→ 找鐘、走到鐘底下")
        save_image(frame, f"bell{n}")
    if ep is None:
        return None
    cw = [(bx - lc[0] + wx, by - lc[1] + wy) for bx, by, _ in find_bells(frame, relaxed=True)]
    fresh = [c for c in cw if all(math.hypot(c[0] - d[0], c[1] - d[1]) > 150 for d in done)]
    pool = fresh or (cw if now - ep["start"] > 4 else [])   # 只看到站過的鐘：等 4 秒再回那個鐘
    if pool:
        cur = ep["target"]
        same = [c for c in pool if cur is not None and math.hypot(c[0] - cur[0], c[1] - cur[1]) < 100]
        ep["target"] = same[0] if same else min(pool, key=lambda c: math.hypot(c[0] - wx, c[1] - wy))
        ep["seen_t"] = now
    tgt = ep["target"] if ep["target"] is not None and now - ep["seen_t"] < 6 else None
    if tgt is None:
        if now - ep["start"] > 2 and not ep["missing"]:
            ep["missing"] = True
            log(f"第 {ep['n']} 次躲鐘：畫面上找不到鐘（畫面存在 logs 的 bell_missing_*.jpg）")
            save_image(frame, "bell_missing")
        return ("stay", None)
    dx, dy = tgt[0] - wx, tgt[1] - wy
    dist = math.hypot(dx, dy)
    if dist > 28:
        return ("move", (dx / dist, dy / dist), dist)
    if not ep["reached"]:
        ep["reached"] = True
        done.append(tgt)
        log(f"第 {ep['n']} 次躲鐘：已經站到鐘底下")
    return ("stay", None)


def skill_panel_open(frame):
    """戰鬥中誤點技能欄跳出的技能說明框（左邊一大塊白色＋上方藍色標題列）"""
    H, W = frame.shape[:2]
    x0, x1 = int(W * 0.03), int(W * 0.45)
    hsv = cv2.cvtColor(frame[:, x0:x1], cv2.COLOR_BGR2HSV)
    white = ((hsv[:, :, 1] < 45) & (hsv[:, :, 2] > 215)).mean(axis=1)
    top = hsv[: int(H * 0.15)]
    blue = ((top[:, :, 0] >= 95) & (top[:, :, 0] <= 130) & (top[:, :, 1] > 60)
            & (top[:, :, 2] > 120)).mean(axis=1)
    return (white > 0.6).mean() > 0.75 and int((blue > 0.6).sum()) >= 25


def park_mouse(rect):
    """把滑鼠移到畫面中間偏上的空地，不要停在下方技能欄上"""
    if CONTROL.get("dry"):
        return
    x = rect[0] + rect[2] * random.uniform(0.55, 0.7)
    y = rect[1] + rect[3] * random.uniform(0.3, 0.4)
    try:
        human_move(x, y, target_w=80)
    except Exception:
        pass


def close_skill_panel(rect, inp):
    """關掉技能說明框：先再點一次剛才誤點的位置，不行再點畫面空地"""
    last = dict(ACTION)
    if last.get("kind") == "click" and time.time() - last.get("t", 0) < 120 \
            and last.get("y", 0) > rect[1] + rect[3] * 0.75:
        log(f"誤點到技能欄，跳出技能說明 → 再點一次同一個位置關掉（{last['x']},{last['y']}）")
        inp.click_item({"text": "技能欄", "x": last["x"] - 3, "y": last["y"] - 3, "w": 6, "h": 6})
    else:
        log("畫面被技能說明框擋住 → 點畫面空地關掉")
        x = rect[0] + rect[2] * 0.8
        y = rect[1] + rect[3] * 0.5
        inp.click_item({"text": "空地", "x": x - 5, "y": y - 5, "w": 10, "h": 10})
    ACTION["t"] = 0     # 下次不要再點同一個位置
    park_mouse(rect)


def menu_still_there(st, item, words=("自动", "重抽")):
    """點按鈕前用最新畫面確認選單還在（遊戲可能已經自己選完關掉了）"""
    sct, rect = st.get("sct"), st.get("rect")
    if rect is None:
        return True
    try:
        fr = grab(sct, rect)
    except Exception:
        return True
    ox, oy = rect[0], rect[1]
    H, W = fr.shape[:2]
    x0 = max(0, int(item["x"] - ox - item["w"] * 3))
    x1 = min(W, int(item["x"] - ox + item["w"] * 4))
    y0 = max(0, int(item["y"] - oy - item["h"] * 2))
    y1 = min(H, int(item["y"] - oy + item["h"] * 3))
    if x1 - x0 < 10 or y1 - y0 < 10:
        return True
    its = ocr(fr[y0:y1, x0:x1], ox + x0, oy + y0, scale=1.5, min_score=0.3)
    return any(w in i["n"] for i in its for w in words)


def find_auto_button(items, frame, ox, oy):
    """遊戲的「自動」選技能按鈕（在「重抽全部」右邊）"""
    btn = next((i for i in items if i["n"] == "自动"), None)
    if btn:
        return btn
    rr = next((i for i in items if i["n"] == "重抽全部"), None)
    if rr is None or frame is None:
        return None
    h = max(rr["h"], 10)
    x0 = max(0, int(rr["x"] - ox))
    x1 = min(frame.shape[1], int(rr["x"] - ox + rr["w"] * 4))
    y0 = max(0, int(rr["y"] - oy - 2 * h))
    y1 = min(frame.shape[0], int(rr["y"] - oy + 3 * h))
    if x1 - x0 > 10 and y1 - y0 > 10:
        it = ocr(frame[y0:y1, x0:x1], ox + x0, oy + y0, scale=1.5, min_score=0.3)
        btn = next((i for i in it if i["n"] == "自动"), None)
        if btn:
            return btn
    cx = rr["cx"] + rr["w"] * 1.4  # 讀不到字：照固定距離推算
    return {"text": "自動", "n": "自动", "x": cx - h, "y": rr["y"], "w": h * 2, "h": rr["h"],
            "cx": cx, "cy": rr["cy"], "estimated": True}


def reread_skill(c, st):
    """標題看不出是哪個技能（例如「冰速術」）→ 把標題那一塊放大再讀一次"""
    fr = st.get("frame")
    t = c["title"]
    if fr is None:
        return None
    ox, oy = st.get("ox", 0), st.get("oy", 0)
    x0 = int(max(0, t["x"] - ox - t["h"] * 2))
    x1 = int(min(fr.shape[1], t["x"] - ox + t["w"] + t["h"] * 2))
    y0 = int(max(0, t["y"] - oy - t["h"] * 0.4))
    y1 = int(min(fr.shape[0], t["y"] - oy + t["h"] * 1.4))
    if x1 - x0 < 10 or y1 - y0 < 6:
        return None
    try:
        its = ocr(fr[y0:y1, x0:x1], ox + x0, oy + y0, scale=2.5, min_score=0.3)
    except Exception:
        return None
    for i in sorted(its, key=lambda i: -i["w"]):
        k = canon_skill(i["text"])
        if k:
            return k
    return None


def _choose_fourth(c, cards, items, inp, sec, msg):
    """選一張卡當第四技能；先記成「待確認」，下一個升級選單換了卡片才算真的選到"""
    GAME["fourth"] = c.get("name") or c["title"]["text"]
    GAME["fourth_ok"] = False
    GAME["fourth_pick"] = {"set": frozenset(norm(x["title"]["text"]) for x in cards),
                           "t": time.time(), "tries": 1}
    GAME["levelups"] = GAME.get("levelups", 0) + 1
    log(msg)
    pick_card(c, cards, items, inp, sec.get("number_keys", True))


def handle_levelup(cards, items, sec, inp, st):
    # 偵測新的一局（技能欄數變少）
    slots = read_slots(items)
    full = levelup_full(items, slots)
    if slots:
        if slots[0] < GAME["slots"]:
            log("技能欄變少了，判斷為新的一局，重設第四技能")
            GAME.update(fourth=None, banish_off=False, banish_fail=0, last_banish=None, levelups=0,
                        attack_banished=False, fourth_banished=False, auto_logged=False,
                        fourth_ok=False, fourth_pick=None, banished_names=set(), seen_full=set(),
                        full_menus=0)
        GAME["slots"] = slots[0]
    fixed4 = sec.get("fourth_skill")
    fourth = fixed4 or GAME["fourth"]

    # 每張卡是哪個技能：標題看不出來（兩個技能一樣像）就放大再讀一次；還是不行就當「看不清楚」
    for c in cards:
        nm = canon_skill(c["title"]["text"])
        if nm is None and len(norm(c["title"]["text"])) <= 6:
            nm = reread_skill(c, st)
            if nm:
                c["title"] = dict(c["title"], text=nm, n=norm(nm))
        c["name"] = nm
        t = nm or c["title"]["text"]
        if nm:
            c["pri"] = nm in sec["priority"] or any(match(nm, k) for k in sec["priority"])
            c["listed"] = any(similar(nm, k) for k in sec.get("banish", []))
            c["maintype"] = nm in MAIN_TYPE
        else:
            # 看不清楚的卡：不放逐、不當第四技能，盡量不選
            c["pri"] = c["listed"] = c["maintype"] = False
        c["always"] = c["listed"] and any(similar(t, k) for k in ALWAYS_BANISH)
        c["is4"] = bool(fourth) and bool(nm) and similar(nm, fourth)
        c["four"] = (c["is4"] and sec.get("banish_fourth", True)) or c["always"] \
            or (c["listed"] and sec.get("banish_all_listed", False))
        c["extra"] = c["listed"] and not c["four"]

    # ---- 第四技能是哪個、選到了沒：看「技能 N/4」最準 ----
    if fixed4:
        GAME["fourth_ok"] = True
    elif full:
        # 技能欄滿了：第四格一定有東西了；這時出現的非主力技能就是真正的第四技能
        GAME["full_menus"] = GAME.get("full_menus", 0) + 1
        GAME.setdefault("seen_full", set()).update(c["name"] for c in cards if c["name"])
        if not GAME.get("fourth_ok"):
            GAME["fourth_ok"] = True
            log("  技能欄滿了（" + (f"{slots[0]}/{slots[1]}" if slots else "已滿")
                + "）→ 第四技能確定，開始放逐")
        own = [c for c in cards if c["name"] and not c["maintype"] and not c["always"] and not c["pri"]]
        own.sort(key=lambda c: 0 if c["listed"] else 1)
        # 原本記的第四技能有出現 → 沒問題；沒出現、卻出現別的非主力技能 → 那個才是真正的第四技能
        if own and not any(c["is4"] for c in cards):
            log(f"  技能欄滿了，實際的第四技能是「{own[0]['name']}」"
                + (f"（不是「{fourth}」，剛才那個沒選到）" if fourth else ""))
            GAME["fourth"] = fourth = own[0]["name"]
            GAME["fourth_banished"] = False
            for c in cards:
                c["is4"] = c is own[0] or (c["name"] and similar(c["name"], fourth))
                c["four"] = (c["is4"] and sec.get("banish_fourth", True)) or c["always"] \
                    or (c["listed"] and sec.get("banish_all_listed", False))
                c["extra"] = c["listed"] and not c["four"]
    elif full is False:
        # 技能欄還沒滿 → 第四技能還沒選到
        if GAME.get("fourth_ok"):
            GAME["fourth_ok"] = False
        if GAME["fourth"]:
            fp = GAME.get("fourth_pick") or {}
            again = next((c for c in cards if c["is4"]), None)
            if again and fp.get("tries", 1) < 4:
                fp["tries"] = fp.get("tries", 1) + 1
                fp["t"] = time.time()
                GAME["fourth_pick"] = fp
                log(f"  技能欄還沒滿（{slots[0]}/{slots[1]}），剛才「{GAME['fourth']}」沒選到 → 再選一次")
                pick_card(again, cards, items, inp, sec.get("number_keys", True))
                return True
            log(f"  技能欄還沒滿，「{GAME['fourth']}」沒選到 → 重新挑第四技能")
            GAME.update(fourth=None, fourth_pick=None)
            fourth = None
            for c in cards:
                c["is4"] = False
                c["four"] = c["always"] or (c["listed"] and sec.get("banish_all_listed", False))
                c["extra"] = c["listed"] and not c["four"]
    elif GAME["fourth"] and not GAME.get("fourth_ok"):
        # 讀不到技能欄：退回舊方法（同一組卡片又出現 = 沒選到；換了卡片 = 選到了）
        fp = GAME.get("fourth_pick") or {}
        cur = frozenset(norm(x["title"]["text"]) for x in cards)
        if fp and cur == fp.get("set") and time.time() - fp.get("t", 0) < 10:
            again = next((c for c in cards if c["is4"]), None)
            if again and fp.get("tries", 1) < 4:
                fp["tries"] = fp.get("tries", 1) + 1
                fp["t"] = time.time()
                log(f"  剛才「{GAME['fourth']}」好像沒選到（同一組卡片還在）→ 再選一次")
                pick_card(again, cards, items, inp, sec.get("number_keys", True))
                return True
            log(f"  「{GAME['fourth']}」一直選不到 → 重新挑第四技能")
            GAME.update(fourth=None, fourth_pick=None)
            fourth = None
        else:
            GAME["fourth_ok"] = True
            log(f"  確認第四技能「{GAME['fourth']}」已經選到，之後才開始放逐")

    lv = read_level(items)
    early_lv = lv if lv is not None else GAME.get("levelups", 0) + 2

    # 0. 還沒有第四技能：勾了「放逐」的技能先選一次當第四技能，下次出現才放逐
    #    （第四技能確認選到之前，什麼都不放逐，連攻擊：屬性附加也先不放逐）
    if not fourth and not full:
        cand = [c for c in cards if c["listed"] and not c["always"] and not c["pri"]
                and not c["maintype"]]
        if cand:
            c = random.choice(cand)
            _choose_fourth(c, cards, items, inp, sec,
                           f"  先選「{c['title']['text']}」當第四技能（確認選到後，下次出現才放逐）")
            return True

    # 1. 第四技能確認選到之後：攻擊：屬性附加 → 第四技能 → 其他勾了放逐的（冰箭術這類）依序放逐
    four = sorted([c for c in cards if c["four"] and not c["pri"]],
                  key=lambda c: 0 if c["always"] else 1)
    if not four and GAME.get("fourth_ok") and (core_banish_done(sec) or GAME.get("full_menus", 0) >= 5):
        four = [c for c in cards if c["extra"] and not c["pri"]]
    if four and not GAME.get("fourth_ok"):
        four = []
    if four and not GAME["banish_off"]:
        key = norm(four[0]["title"]["text"])
        now = time.time()
        if GAME["last_banish"] == key and now - GAME["last_banish_t"] < 5:
            GAME["banish_fail"] += 1
            if GAME["banish_fail"] >= 2:
                GAME["banish_off"] = True
                log("  放逐好像沒有作用（次數可能用完了），這局不再放逐")
        dis_ = None if GAME["banish_off"] else button_really_disabled(st, four[0]["banish"], "skill")
        if dis_ is None and not GAME["banish_off"]:
            time.sleep(0.6)
            return True         # 按鈕暗暗的：等動畫播完再看一次
        if dis_:
            GAME["banish_off"] = True
            log("  放逐按鈕變灰色了（次數用完），這局不再放逐")
        if not GAME["banish_off"]:
            b = four[0]["banish"]
            pos = inp.click_item(b, "放逐") or (round(b["cx"]), round(b["cy"]))
            if four[0]["always"]:
                GAME["attack_banished"] = True
            elif four[0].get("is4"):
                GAME["fourth_banished"] = True
            GAME.setdefault("banished_names", set()).add(four[0].get("name") or four[0]["title"]["text"])
            log(f"  放逐：{four[0]['title']['text']}（點擊 {pos[0]},{pos[1]}"
                + ("，位置是推算的" if b.get("estimated") else "") + "）")
            GAME["last_banish"], GAME["last_banish_t"] = key, now
            return True

    # 1.5 該放逐的技能都放逐完了 → 改按遊戲的「自動」選技能
    if sec.get("auto_after_banish", False) and skill_banish_done(sec):
        btn = find_auto_button(items, st.get("frame"), st.get("ox", 0), st.get("oy", 0))
        if btn and not menu_still_there(st, btn):
            log("  升級選單已經關掉了（遊戲自動選好了）→ 不點，避免點到技能欄")
            return True
        if btn:
            if not GAME.get("auto_logged"):
                log("  該放逐的技能都放逐完了 → 之後改按「自動」選技能")
                GAME["auto_logged"] = True
            pos = inp.click_item(btn, "自動") or (round(btn["cx"]), round(btn["cy"]))
            log(f"  按「自動」（點擊 {pos[0]},{pos[1]}" + ("，位置是推算的" if btn.get("estimated")
                                                        else "") + "）")
            return True

    # 2. 前幾次升級：放逐清單裡的技能都沒出現 → 隨便挑一個非主力技能當第四技能
    if not fourth and not full and early_lv <= sec.get("throwaway_until_level", 3):
        non = [c for c in cards if c["name"] and not c["pri"] and not c["listed"] and not c["maintype"]]
        if non:
            c = random.choice(non)
            _choose_fourth(c, cards, items, inp, sec,
                           f"  Lv {early_lv}：先選「{c['title']['text']}」當第四技能（之後出現會放逐）")
            return True

    # 3. 主力技能
    pri = [c for c in cards if c["pri"]]
    if pri:
        best, lv, phase = rank_main_skills(pri, items, sec)
        log(f"  Lv {lv}（{phase}）：" + "、".join(
            f"{c['title']['text']}[{c['rar_name']} {c['eff'] or '?'}]" for c in pri))
        log(f"  選擇主力技能：{best['title']['text']}")
        pick_card(best, cards, items, inp, sec.get("number_keys", True))
        GAME["levelups"] = GAME.get("levelups", 0) + 1
        return True

    # 3. 還沒有第四技能 → 隨便選一個當第四
    if not fourth and not full:
        pool = [c for c in cards if c["name"] and not c["listed"] and not c["always"]
                and not c["maintype"]] or \
            [c for c in cards if c["name"] and c["listed"] and not c["always"] and not c["maintype"]]
        if pool:
            c = random.choice(pool)
            _choose_fourth(c, cards, items, inp, sec,
                           f"  沒有主力技能，選「{c['title']['text']}」當第四技能（之後出現會放逐）")
            return True
        # 只剩冰箭術這類額外放逐的技能：先選一個，但不當第四技能
        c = random.choice([c for c in cards if not c["always"]] or cards)
        GAME["levelups"] = GAME.get("levelups", 0) + 1
        log(f"  只能選「{c['title']['text']}」（不當第四技能）")
        pick_card(c, cards, items, inp, sec.get("number_keys", True))
        return True

    # 4. 已有第四技能、又沒主力 → 重抽，不然隨便選一個非第四的
    if st["rerolls"] < sec.get("max_reroll", 0):
        rr = next((i for i in items if i["n"] == "重抽全部"), None)
        if rr:
            log("  沒有主力技能 → 重抽全部")
            inp.click_item(rr, "重抽全部")
            st["rerolls"] += 1
            return True
    # 沒主力可選：先選已經有的第四技能（不多佔欄位），再來是沒勾放逐的，
    # 再來是攻擊：屬性附加（不佔欄位），最後才是冰箭術這類要額外放逐的
    own4 = [c for c in cards if c.get("is4")]
    others = own4 or [c for c in cards if c["name"] and not c["listed"]] or \
        [c for c in cards if c["always"]] or [c for c in cards if c["name"] and not c["maintype"]] or \
        [c for c in cards if not c["maintype"]] or cards
    c = random.choice(others)
    GAME["levelups"] = GAME.get("levelups", 0) + 1
    log(f"  沒有主力技能，選：{c['title']['text']}"
        + ("（已經有的第四技能）" if own4 else ""))
    pick_card(c, cards, items, inp, sec.get("number_keys", True))
    return True


_GRID = {"raw": None}
_DIRS8 = [(math.cos(a), math.sin(a)) for a in [i * math.pi / 4 for i in range(8)]]


def safe_key_dir(desired, start, blocked=(), look=40, raw=None, cell=8, prefer=None):
    """方向鍵只有 8 個方向：挑一個「前方 look 像素內沒有障礙」而且最接近想走方向的。
    都不行就回傳 None"""
    if raw is None:
        raw = _GRID.get("raw")
        cell = _GRID.get("cell", 8)
    if raw is None or desired is None:
        return desired
    gh, gw = raw.shape
    obst = cv2.dilate(raw, np.ones((3, 3), np.uint8))
    sx, sy = start
    cands = sorted(_DIRS8, key=lambda v: -(v[0] * desired[0] + v[1] * desired[1]))
    # 上一次的方向還算順路就繼續用，避免左右來回抖
    if prefer is not None and prefer[0] * desired[0] + prefer[1] * desired[1] > 0.6:
        cands = [prefer] + [v for v in cands if v != prefer]
    for v in cands:
        if v[0] * desired[0] + v[1] * desired[1] < -0.2:   # 不要往回走太多
            break
        ok = True
        for t in range(12, look + 1, 6):
            px, py = sx + v[0] * t, sy + v[1] * t
            cx_, cy_ = int(px // cell), int(py // cell)
            if not (0 <= cx_ < gw and 0 <= cy_ < gh) or obst[cy_, cx_]:
                ok = False
                break
            if any(math.hypot(px - bx, py - by) < 14 for bx, by in blocked):
                ok = False
                break
        if ok:
            return v
    return None


def plan_path(frame, goal, cell=8, start=None, blocked=(), clearance=7, obstacles=None):
    """在畫面上規劃一條避開花圃／圍欄／水池的路線。
    淺灰色石板路當作可走，其餘當障礙；回傳畫面座標的路徑點清單或 None"""
    import heapq
    H, W = frame.shape[:2]
    gw, gh = W // cell, H // cell
    if obstacles is not None:          # 戰鬥：外面算好的障礙（水池）
        raw = cv2.resize(obstacles.astype(np.uint8), (gw, gh), interpolation=cv2.INTER_NEAREST)
    else:                              # 城鎮：淺灰石板路以外都是障礙
        small = cv2.resize(frame, (gw, gh), interpolation=cv2.INTER_AREA)
        hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
        walk = ((hsv[:, :, 1] < 50) & (hsv[:, :, 2] > 95)).astype(np.uint8)
        walk = cv2.morphologyEx(walk, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        # 噴水池：藍色水面＋外圍一圈灰色石頭邊（顏色跟石板路很像，要另外擋掉）
        water = ((hsv[:, :, 0] >= 95) & (hsv[:, :, 0] <= 125) & (hsv[:, :, 1] >= 60)
                 & (hsv[:, :, 2] >= 60)).astype(np.uint8)
        water = cv2.morphologyEx(water, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        n_, lab_, st_, _ = cv2.connectedComponentsWithStats(water)
        for i in range(1, n_):
            if st_[i, 4] >= 40:
                k_ = max(3, int(max(st_[i, 2], st_[i, 3]) * 0.35)) | 1
                one = (lab_ == i).astype(np.uint8)
                rim = cv2.dilate(one, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k_, k_)))
                walk[rim > 0] = 0
        raw = (1 - walk).astype(np.uint8)
    k = int(clearance) | 1
    obst = cv2.dilate(raw, np.ones((k, k), np.uint8))  # 角色有寬度：太窄的縫當作走不通
    if start is None:
        start = (W / 2, H / 2 + 8)
    sx = min(gw - 1, max(0, int(start[0] // cell)))
    sy = min(gh - 1, max(0, int(start[1] // cell)))
    gx = min(gw - 1, max(0, int(goal[0] // cell)))
    gy = min(gh - 1, max(0, int(goal[1] // cell)))
    # 角色附近改用「沒加寬」的障礙圖，才不會一開始就被困住
    yy, xx = np.mgrid[0:gh, 0:gw]
    near = (xx - sx) ** 2 + (yy - sy) ** 2 <= 6 ** 2
    obst[near] = raw[near]
    obst[max(0, sy - 6):sy + 1, max(0, sx - 2):sx + 3] = 0  # 角色身體（腳以上）
    obst[max(0, gy - 3):gy + 4, max(0, gx - 3):gx + 4] = 0  # 目標 NPC
    for bx, by in blocked:  # 之前撞到過的地方
        cx_, cy_ = int(bx // cell), int(by // cell)
        if 0 <= cx_ < gw and 0 <= cy_ < gh and abs(cx_ - sx) + abs(cy_ - sy) > 1:
            obst[max(0, cy_ - 1):cy_ + 2, max(0, cx_ - 1):cx_ + 2] = 1
    _GRID.update(raw=raw, cell=cell, shape=(H, W))
    clear = cv2.distanceTransform((1 - obst).astype(np.uint8), cv2.DIST_L2, 3)
    start = (sx, sy)
    openq = [(0.0, start)]
    came = {start: None}
    g = {start: 0.0}
    steps = 0
    while openq and steps < 40000:
        steps += 1
        _, cur = heapq.heappop(openq)
        if abs(cur[0] - gx) <= 1 and abs(cur[1] - gy) <= 1:
            path = []
            while cur:
                path.append((cur[0] * cell + cell / 2, cur[1] * cell + cell / 2))
                cur = came[cur]
            return path[::-1]
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                nx, ny = cur[0] + dx, cur[1] + dy
                if not (0 <= nx < gw and 0 <= ny < gh) or obst[ny, nx]:
                    continue
                # 越靠近障礙物成本越高 → 走開闊的地方
                ng = g[cur] + (1.414 if dx and dy else 1.0) * (1 + 4.0 / (clear[ny, nx] + 0.5))
                if ng < g.get((nx, ny), 1e18):
                    g[(nx, ny)] = ng
                    came[(nx, ny)] = cur
                    heapq.heappush(openq, (ng + math.hypot(gx - nx, gy - ny), (nx, ny)))
    return None


def cam_shift(a, b):
    """前後兩張畫面，背景移動了多少（角色移動時鏡頭會跟著走）"""
    H, W = a.shape[:2]
    y0, y1, x0, x1 = int(H * .12), int(H * .82), int(W * .08), int(W * .92)
    ga = cv2.resize(cv2.cvtColor(a[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY), None, fx=.5, fy=.5)
    gb = cv2.resize(cv2.cvtColor(b[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY), None, fx=.5, fy=.5)
    ga, gb = ga.astype(np.float32), gb.astype(np.float32)
    win = cv2.createHanningWindow(ga.shape[::-1], cv2.CV_32F)
    (dx, dy), resp = cv2.phaseCorrelate(ga, gb, win)
    return dx * 2, dy * 2, resp


_btn_tpl = {}


def find_button_by_image(frame, name, threshold=0.8):
    """用按鈕外觀（templates 裡的圖）找按鈕，視窗大小不同也找得到"""
    if name not in _btn_tpl:
        path = os.path.join(TEMPLATE_DIR, name)
        img = None
        if os.path.exists(path):
            img = cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_COLOR)
        _btn_tpl[name] = {"img": img, "scale": None}
    info = _btn_tpl[name]
    tpl = info["img"]
    if tpl is None:
        return None
    scales = [info["scale"]] if info["scale"] else list(np.linspace(0.6, 1.6, 21))
    best = (0, None, None)
    for sc in scales:
        t = cv2.resize(tpl, None, fx=sc, fy=sc)
        if t.shape[0] >= frame.shape[0] or t.shape[1] >= frame.shape[1]:
            continue
        r = cv2.matchTemplate(frame, t, cv2.TM_CCOEFF_NORMED)
        _, mx, _, loc = cv2.minMaxLoc(r)
        if mx > best[0]:
            best = (mx, (loc[0], loc[1], t.shape[1], t.shape[0]), sc)
    if best[0] >= threshold:
        info["scale"] = best[2]
        return best[1]
    if info["scale"]:
        info["scale"] = None  # 視窗大小可能變了，下次重新找
    return None


ROUTE_FILE = os.path.join(BASE_DIR, "town_route.json")
_route = {"data": None, "loaded": False}


def set_route_mode(mode):
    """節慶主持人和遠征隊長各記各的路線"""
    global ROUTE_FILE
    f = os.path.join(BASE_DIR, "town_route.json" if mode != "expedition" else "town_route_expedition.json")
    if f != ROUTE_FILE:
        ROUTE_FILE = f
        _route.update(data=None, loaded=False)


def load_route():
    if not _route["loaded"]:
        _route["loaded"] = True
        try:
            with open(ROUTE_FILE, encoding="utf-8") as f:
                _route["data"] = json.load(f)
        except FileNotFoundError:
            # 第一次用：還沒有自己記的路線 → 用程式附的預設路線（開發者實際走成功的）
            _route["data"] = None
            try:
                with open(os.path.join(BASE_DIR, "defaults", os.path.basename(ROUTE_FILE)),
                          encoding="utf-8") as f:
                    d = json.load(f)
                d["default"] = True
                _route["data"] = d
                log("第一次使用：先用內建的預設路線找主持人，走成功後會記住自己的路線")
            except Exception:
                pass
        except Exception:
            _route["data"] = None
        if _route["data"] is not None and not _route["data"].get("points"):
            _route["data"] = None       # 預設路線失敗過、被停用
    return _route["data"]


def save_route(tst, frame_shape):
    """成功走到主持人 → 把這次從出生點走過去的路線記下來（比之前記的好才換）"""
    trail = tst.get("trail") or []
    npc = tst.get("npc_world")
    if not tst.get("fresh") or len(trail) < 3 or npc is None or tst.get("zoomed"):
        return
    length = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(trail, trail[1:]))
    cand = {"points": [[round(x, 1), round(y, 1)] for x, y in trail], "npc": [npc[0], npc[1]],
            "stuck": tst.get("stuck_total", 0), "len": length,
            "size": [frame_shape[1], frame_shape[0]]}
    old = load_route()
    if tst.get("route_checked") and tst.get("route_on_success") and old:
        if old.get("fails"):
            old["fails"] = 0
            try:
                with open(ROUTE_FILE, "w", encoding="utf-8") as f:
                    json.dump(old, f)
            except Exception:
                pass
        return
    straight = math.hypot(npc[0], npc[1])
    moved = tst.get("spawn_moved")
    if length > (straight * 3 + 300 if moved else straight * 1.6 + 150):     # 繞太遠的路線不記
        return
    if old and old.get("size") == cand["size"] and not moved:
        better = length < old.get("len", 1e9) * 0.9 or \
            (cand["stuck"] < old.get("stuck", 99) and length < old.get("len", 1e9) * 1.1)
        if not better:
            return
    try:
        with open(ROUTE_FILE, "w", encoding="utf-8") as f:
            json.dump(cand, f)
        _route["data"] = cand
        log(f"記住了走到主持人的路線（{length:.0f} 像素、卡住 {cand['stuck']} 次），下次照這條走")
    except Exception as e:
        log(f"路線存檔失敗：{e}")


def route_failed():
    """記住的路線又失敗一次；連續失敗 2 次就丟掉，重新記"""
    rt = load_route()
    if not rt:
        return
    rt["fails"] = rt.get("fails", 0) + 1
    if rt["fails"] >= 2:
        clear_route()
        if rt.get("default"):
            # 預設路線不適用這台電腦 → 留一個記號，之後不再用預設路線
            try:
                with open(ROUTE_FILE, "w", encoding="utf-8") as f:
                    json.dump({"disabled_default": True}, f)
            except Exception:
                pass
        log("記住的路線連續失敗 2 次 → 刪掉，下次重新規劃並記住新的")
        return
    try:
        with open(ROUTE_FILE, "w", encoding="utf-8") as f:
            json.dump(rt, f)
    except Exception:
        pass


def clear_route():
    _route["data"], _route["loaded"] = None, True
    for f in ("town_route.json", "town_route_expedition.json"):
        try:
            os.remove(os.path.join(BASE_DIR, f))
        except OSError:
            pass


def town_reset(tst):
    """回到出生點（剛從大廳進城）：重設地圖座標"""
    for k in ("prev", "target", "route_on", "route_checked", "route_off", "npc_world", "npc_first",
              "route_on_success", "npc_w_reached",
              "blocked", "hist", "detour_until", "route_i", "route_stuck", "key_dir",
              "path_w", "plan_t", "plan_tgt", "move_since", "last_dir",
              "spawn_moved", "guess_used", "spiral_t0",
              "zoom_done", "zoomed", "click_next", "click_hold_until", "label_h", "click_n", "click_fail", "click_d", "click_logged", "click_t", "click_gave_up"):
        tst.pop(k, None)
    tst.update(world=(0.0, 0.0), trail=[(0.0, 0.0)], fresh=True, stuck_total=0, stuck_n=0)


def town_ocr(frame, rect, cfg, inp, tst):
    """城鎮：讀畫面上的字。處理按鈕／交談提示；找到主持人就更新 tst['target']。
    回傳 'acted'（按了東西）、'town'、'other'（不是城鎮）"""
    tc = cfg["town"]
    H, W = frame.shape[:2]
    ox, oy = rect[0], rect[1]
    items = ocr(frame, ox, oy)
    mode = town_mode(tc)
    set_route_mode(mode)
    if mode == "expedition":
        kw, name = norm("隊長"), "遠征隊長"
        kws = [norm(k) for k in ("隊長", "遠征", "征隊", "長交", "隧長", "际長")]
    else:
        kw, name = norm(tc["npc_keyword"]), tc.get("npc_name", "節慶主持人")
        kws = [kw, norm("持人")]
    joined = " ".join(i["n"] for i in items)

    def click(item, label):
        MOVER.set(None, False)
        time.sleep(random.uniform(0.5, 1.2))
        log(f"按「{label}」")
        inp.click_item(item, label)
        tst["cooldown_until"] = time.time() + 3
        tst["target"] = None

    dm = detect_menu(items)
    if dm == "expedition":
        handle_expedition(frame, items, rect, cfg, inp)
        tst["cooldown_until"] = time.time() + 0.5
        return "acted"
    if dm in ("lobby", "result", "afk", "login", "charselect"):
        return "other"

    # 「升級」視窗 → 前往野外
    btn = next((i for i in items if match(i["text"], tc["field_button"])), None)
    if btn:
        # 難度視窗：先照設定調滑桿，下一輪再按前往野外
        if tc.get("difficulty", "keep") in ("min", "max") and \
                time.time() - tst.get("diff_set_t", 0) > 20 and find_sliders(frame, items, ox, oy):
            tst["diff_set_t"] = time.time()
            if set_difficulty(frame, items, rect, cfg, inp):
                tst["cooldown_until"] = time.time() + 0.6
                return "acted"
        click(btn, tc["field_button"])
        GAME["phase"], GAME["battle_seen"] = "battle", time.time()
        GAME.update(max_timer=0, ending_logged=False, batch_target=None, game_counted=False,
                    maxlv_logged=False, hold_logged=False, bell_ep=None, bells_done=[], bell_count=0,
                    late_logged=False, focused=set(), focus_off=False)
        log("進入戰鬥")
        return "acted"

    # 節慶活動視窗 → 開始遊戲（先用按鈕外觀找，再用文字，最後用固定位置）
    start_txt = next((i for i in items if match(i["text"], tc["start_button"])
                      and not match(i["text"], "開始遊戲！")), None)
    if mode == "expedition" and ("扭蛋" in joined or "碎片" in joined or "收藏" in joined):
        back = next((i for i in items if i["n"].startswith(norm("返回")) and len(i["n"]) <= 3), None)
        log("跟節慶主持人對話了（目前設定是刷遠征）→ 按「返回」")
        if back:
            click(back, "返回")
        else:
            inp.press("escape")
        tst["cooldown_until"] = time.time() + 1.5
        return "acted"
    if "扭蛋" in joined or "碎片" in joined or "收藏" in joined or \
            (start_txt is not None and time.time() < tst.get("expect_start_until", 0)):
        box = find_button_by_image(frame, "start_button.png")
        if box:
            btn = {"text": tc["start_button"], "x": ox + box[0], "y": oy + box[1],
                   "w": box[2], "h": box[3]}
        else:
            big = ocr(frame, ox, oy, scale=1.5, min_score=0.3)
            btn = next((i for i in big if match(i["text"], tc["start_button"])), None)
        if btn is None:
            px, py = tc["start_button_pos"]
            btn = {"text": tc["start_button"], "x": ox + W * px - 40, "y": oy + H * py - 12,
                   "w": 80, "h": 24}
            log("找不到「開始遊戲」按鈕，改按它平常的位置")
        click(btn, tc["start_button"])
        return "acted"

    # 跟錯的 NPC 對話（例如旁邊的遠征隊長）→ 按「返回」關掉
    back = next((i for i in items if i["n"] in (norm("返回"), norm("返回 ")) or
                 (i["n"].startswith(norm("返回")) and len(i["n"]) <= 3 and "角" not in i["n"])), None)
    if back and kw not in joined and "开始" not in joined:
        log("跟錯的 NPC 對話了 → 按「返回」")
        click(back, "返回")
        tst["cooldown_until"] = time.time() + 1.5
        tst["expect_start_until"] = 0
        return "acted"

    if not ("聊天" in joined or "频道" in joined or "切换角色" in joined):
        return "other"

    # 畫面下方中間的交談提示（例如「與節慶主持人交談 空格」）
    # 用字的內容分辨，不只看位置（遊戲文字放大後提示框會變大、往上移）
    hints = [j for j in items if j["n"] in ("空格", "空白") or "space" in j["n"]]

    def is_prompt(i):
        if not (kw in i["n"] and i["cy"] - oy > H * 0.55 and abs(i["cx"] - ox - W / 2) < W * 0.3):
            return False
        if any(norm(k) in i["n"] for k in ("交談", "對話", "空格", "空白", "space")):
            return True
        # 只讀到名字：同一列右邊要有「空格」按鍵提示才是交談提示框
        # （不然只是 NPC 剛好走到畫面下方，頭上的名字被當成提示 → 太遠按空白鍵沒反應）
        return any(abs(j["cy"] - i["cy"]) < H * 0.035 and 0 < j["cx"] - i["cx"] < W * 0.35 for j in hints)
    prompt = [i for i in items if is_prompt(i)]
    # 提示框的字常常讀不到，只讀到「空格」→ 放大那一小塊再讀一次，看是跟誰交談
    if not prompt:
        sp = next((i for i in items if i["n"] in ("空格", "空白") and i["cy"] - oy > H * 0.55
                   and abs(i["cx"] - ox - W / 2) < W * 0.3), None)
        if sp is not None:
            x0 = int(max(0, sp["x"] - ox - 280))
            y0 = int(max(0, sp["y"] - oy - 20))
            x1 = int(min(W, sp["x"] - ox + sp["w"] + 10))
            y1 = int(min(H, sp["y"] - oy + sp["h"] + 20))
            zt = " ".join(i["n"] for i in ocr(frame[y0:y1, x0:x1], ox + x0, oy + y0, scale=1.5,
                                              min_score=0.3))
            if any(k in zt for k in kws) and not (mode == "expedition" and "持人" in zt):
                prompt = [sp]
    if prompt and time.time() - tst.get("prompt_pending", 0) > 3:
        # 第一次看到提示：先停下來，馬上再讀一次確認還是主持人（旁邊的隊長等 NPC 也會跳提示）
        MOVER.set(None, False)
        tst["prompt_pending"] = time.time()
        tst["ocr_soon"] = True
        return "acted"
    if not prompt:
        tst.pop("prompt_pending", None)
    if prompt:
        tst.pop("prompt_pending", None)
        log(f"出現與{name}交談的提示 → 按空白鍵")
        save_route(tst, frame.shape)
        tst["fresh"] = False
        MOVER.set(None, False)
        time.sleep(random.uniform(0.3, 0.7))
        inp.press(tc["talk_key"])
        tst["cooldown_until"] = time.time() + tc["talk_cooldown"]
        tst["expect_start_until"] = time.time() + 10
        tst["click_hold_until"] = time.time() + 12     # 已經到主持人旁邊了，不要再點（會把對話框點掉、點到別的 NPC）
        tst["target"] = None
        tst["last_dir"] = None          # 按完空白鍵不要再往前走
        tst["hist"], tst["move_since"] = [], None
        return "acted"

    # 主持人頭上的名字：字數跟名字差不多（聊天訊息、提示句子那種長句不算）
    def label_ok(i):
        return any(k in i["n"] for k in kws) and not is_prompt(i) \
            and len(i["n"]) <= len(norm(name)) + 3 \
            and not any(norm(k) in i["n"] for k in ("交談", "對話", "空格", "空白"))
    labels = [i for i in items if label_ok(i)]
    if not labels and time.time() >= tst.get("zoom_next", 0):
        # NPC 頭上的名字很小，讀不到時放大畫面中間再讀一次（每 2 秒最多一次）
        tst["zoom_next"] = time.time() + 2
        x0, y0, x1, y1 = int(W * .1), int(H * .08), int(W * .9), int(H * .78)
        labels = [i for i in ocr(frame[y0:y1, x0:x1], ox + x0, oy + y0, scale=1.5, min_score=0.3)
                  if label_ok(i)]
    if labels:
        lc = (W / 2, H / 2)
        lab = min(labels, key=lambda i: math.hypot(i["cx"] - ox - lc[0], i["cy"] - oy - lc[1]))
        tst["target"] = (lab["cx"] - ox, lab["cy"] - oy + tc["npc_body_offset"])
        tst["label_h"] = lab["h"]
        tst["last_seen"] = time.time()
        # 主持人在地圖上的位置（以出生點為原點）
        flc = (W / 2, H / 2 + tc.get("feet_offset", 30))
        wx_, wy_ = tst.get("world", (0.0, 0.0))
        npc_w = (wx_ + tst["target"][0] - flc[0], wy_ + tst["target"][1] - flc[1])
        tst["npc_world"] = npc_w
        # 城鎮拉遠視野後距離比例不一樣，舊的路線不能用
        rt = load_route() if tc.get("learn_route", True) and not tst.get("zoomed") else None
        if rt and tst.get("fresh") and rt.get("size") == [W, H]:
            off = (npc_w[0] - rt["npc"][0], npc_w[1] - rt["npc"][1])
            if not tst.get("route_checked"):
                tst["npc_first"] = (npc_w, off)
                tst["route_checked"] = True
                if math.hypot(*off) < 250:      # 名字標籤固定偏一點（例如遊戲更新後）也算同一個出生點
                    tst["route_on"], tst["route_i"] = True, 0
                    log("出生點跟上次一樣 → 照記住的路線走")
                else:
                    tst["spawn_moved"] = True    # 出生點換了（例如遊戲更新）→ 這次走成功就換掉舊路線
                    log(f"出生點跟記住的不一樣（差 {math.hypot(*off):.0f} 像素）→ 自己規劃路線")
            if tst.get("route_on"):
                # 只校正「走路途中累積的誤差」（跟第一次看到時比），
                # 名字標籤位置因文字大小改變造成的固定偏差不算進去
                f_w, _ = tst["npc_first"]
                tst["route_off"] = (npc_w[0] - f_w[0], npc_w[1] - f_w[1])
        tst["npc_w_reached"] = False
        if not tst.get("seen"):
            log(f"看到{name}，走過去")
            tst["seen"] = True
            tst["lost_logged"] = False
    else:
        if tst.get("target") is not None and time.time() - tst.get("last_seen", 0) > 4:
            tst["target"] = None
        tst["seen"] = False
    return "town"


def _town_stuck(tst, now, world, step_dir):
    """城鎮卡住偵測：持續按著方向鍵 1.2 秒以上，畫面（地圖位置）卻幾乎沒動才算卡住。
    停下來讀字、剛起步、畫面沒更新的時間都不算。卡住回傳要閃開的方向，不然回傳 None"""
    hist = tst.get("hist") or []
    ms = tst.get("move_since")
    if ms is None or now - ms < 1.2 or len(hist) < 3 or now - hist[0][0] < 1.0:
        return None
    total = math.hypot(world[0] - hist[0][1][0], world[1] - hist[0][1][1])
    if total > 40:
        tst["stuck_n"] = 0
    if total >= 12:
        return None
    if tst.get("route_on") and tst.get("route_stuck", 0) < 4:
        # 照路線走時被邊角卡到：先試旁邊 45° 的方向，不急著放棄路線
        k = tst.get("route_stuck", 0)
        tst["route_stuck"] = k + 1
        tst["detour"] = rotate(step_dir, 45 if k % 2 == 0 else -45)
        tst["detour_until"] = now + 0.35 + 0.15 * k
        tst["hist"] = []
        return tst["detour"]
    # 記住撞到的地方（角色前方一格），之後規劃路線會避開
    bl = tst.setdefault("blocked", [])
    for k in (16, 28):
        bl.append((world[0] + step_dir[0] * k, world[1] + step_dir[1] * k))
    del bl[:-300]
    n = tst.get("stuck_n", 0)
    if n % 3 == 2:
        det, what = (-step_dir[0], -step_dir[1]), "先退後一點"
    else:
        det, what = rotate(step_dir, 90 if n % 2 == 0 else -90), "往旁邊閃一下"
    tst["detour"] = det
    tst["detour_until"] = now + random.uniform(0.45, 0.7)
    tst["stuck_n"] = n + 1
    tst["stuck_total"] = tst.get("stuck_total", 0) + 1
    tst["plan_t"] = 0               # 閃完馬上重新規劃（會避開剛剛撞到的地方）
    if tst.get("route_on"):
        tst["route_on"] = False
        route_failed()
        log("照記住的路線走卻卡住 → 改成自己規劃")
    tst["hist"] = []
    log(f"好像被擋住了 → {what}，再重新規劃路線")
    return det


def town_click(frame, rect, cfg, inp, tst, now, lc):
    """看到主持人 → 只點一次他頭上名字的正下方，遊戲會自己找路走過去（繞過噴水池、花圃）。
    點完就不再點，等交談提示出現；太久沒到才改回自己走。
    回傳 None = 交給遊戲走、程式不按方向鍵；False = 改回自己走"""
    tc = cfg["town"]
    if tst.get("click_n", 0) >= 1:
        # 已經點過了：等遊戲走到，不再點（再點會把對話框點掉、點到別的 NPC）
        if now - tst.get("click_t", now) < tc.get("click_wait", 30):
            return None
        if not tst.get("click_gave_up"):
            tst["click_gave_up"] = True
            log(f"點了主持人 {tc.get('click_wait', 30)} 秒還沒走到 → 改成自己走過去")
        return False
    target = tst.get("target")
    if target is None:
        return False
    if now < tst.get("expect_start_until", 0):
        return None
    H, W = frame.shape[:2]
    off = tc.get("npc_body_offset", 75)
    lh = tst.get("label_h", 18)
    lx, ly = target[0], target[1] - off             # 名字標籤的位置
    cx, cy = lx, ly + lh * 1.6                      # 名字正下方 = 主持人頭的位置
    d = math.hypot(lx - W / 2, ly - (H / 2 - lh * 3))
    if d < max(70, lh * 4):
        return None                                 # 已經在旁邊了，等交談提示
    ox, oy = rect[0], rect[1]
    MOVER.set(None, False)
    log(f"點{tc.get('npc_name', '節慶主持人')}一次，讓角色自己走過去（距離約 {d:.0f} 像素）")
    inp.click_item({"text": tc.get("npc_name", "節慶主持人"), "x": ox + cx - 5, "y": oy + cy - 5,
                    "w": 10, "h": 10}, tc.get("npc_name", "節慶主持人"))
    park_mouse(rect)
    tst["click_n"] = 1
    tst["click_t"] = now
    tst["ocr_next"] = min(tst.get("ocr_next", now + 1), now + 0.8)
    return None


def town_step(frame, rect, cfg, inp, tst):
    """城鎮每一格畫面：追蹤主持人位置、規劃路線，回傳移動方向或 None"""
    tc = cfg["town"]
    now = time.time()
    H, W = frame.shape[:2]
    lc = (W / 2, H / 2 + tc.get("feet_offset", 30))  # 角色腳下（碰撞判定的位置）

    # 鏡頭位移 → 更新目標在畫面上的位置，並累計角色在地圖上的位置
    # （背景模式：畫面沒有更新就不算，避免誤判成「沒在動」）
    stamp = IO.page.frame_t if IO.cdp else now
    fresh = stamp != tst.get("frame_stamp")
    tst["frame_stamp"] = stamp
    track_ok = False
    prev = tst.get("prev")
    if fresh and prev is not None and prev.shape == frame.shape:
        dx, dy, resp = cam_shift(prev, frame)
        if resp > 0.05 and abs(dx) < W * 0.3 and abs(dy) < H * 0.3:
            if tst.get("target") is not None:
                tx, ty = tst["target"]
                tst["target"] = (tx + dx, ty + dy)
            wx, wy = tst.get("world", (0.0, 0.0))
            tst["world"] = (wx - dx, wy - dy)
            track_ok = True
    if fresh:
        tst["prev"] = frame
    if "trail" not in tst:
        town_reset(tst)
        tst["prev"] = frame
    world = tst.get("world", (0.0, 0.0))
    tr = tst["trail"]
    if math.hypot(world[0] - tr[-1][0], world[1] - tr[-1][1]) >= 12 and len(tr) < 2000:
        tr.append(world)
    if track_ok:
        hist = tst.setdefault("hist", [])
        hist.append((now, world))
        while hist and now - hist[0][0] > 1.6:
            hist.pop(0)
    # 撞過的位置換算回目前畫面座標
    blocked_screen = [(bx - world[0] + lc[0], by - world[1] + lc[1])
                      for bx, by in tst.get("blocked", [])]

    # 剛按完空白鍵：直接用按鈕外觀找「開始遊戲」，一出現就按
    if now < tst.get("expect_start_until", 0) and now > tst.get("cooldown_until", 0) - 0.8:
        box = find_button_by_image(frame, "start_button.png")
        if box:
            ox, oy = rect[0], rect[1]
            MOVER.set(None, False)
            time.sleep(random.uniform(0.3, 0.7))
            log(f"按「{tc['start_button']}」")
            inp.click_item({"text": tc["start_button"], "x": ox + box[0], "y": oy + box[1],
                            "w": box[2], "h": box[3]}, tc["start_button"])
            tst["expect_start_until"] = 0
            tst["cooldown_until"] = time.time() + 2
            tst["ocr_next"] = 0
            return None

    if now < tst.get("cooldown_until", 0):
        return None

    # 剛進城鎮：滾輪拉遠視野，從出生點就看得到主持人
    if not tst.get("zoom_done"):
        tst["zoom_done"] = True
        zn = int(tc.get("zoom_out", 10) or 0)
        if zn and tc.get("click_move", True):
            MOVER.set(None, False)
            log(f"城鎮拉遠視野（滾輪 {zn} 格）")
            inp.scroll(rect[0] + W * 0.5, rect[1] + H * 0.45, zn, "城鎮拉遠視野")
            park_mouse(rect)
            tst["zoomed"] = True
            tst["prev"] = None
            tst["ocr_next"] = 0
            time.sleep(0.5)
            return None

    # 定期讀字（讀字時先停下來，避免衝過頭）；離主持人還很遠時少讀幾次，走路比較順
    if now >= tst.get("ocr_next", 0) or tst.get("target") is None:
        MOVER.set(None, False)
        tst["hist"], tst["move_since"] = [], None     # 停下來的時間不算進卡住偵測
        r = town_ocr(frame, rect, cfg, inp, tst)
        gap = 0.8
        if tst.get("target") is not None:
            td_ = math.hypot(tst["target"][0] - lc[0], tst["target"][1] - lc[1])
            gap = 3.0 if td_ > 350 else 2.0 if td_ > 150 else 1.2
        tst["ocr_next"] = time.time() + gap
        if tst.pop("ocr_soon", False):
            tst["ocr_next"] = time.time() + 0.35
        tst["prev"] = frame  # 目標座標是以這張畫面為準，下一張再算位移
        if r == "other":
            town_reset(tst)
            if not tst.get("unknown_logged"):
                log("不是戰鬥也不是城鎮畫面，等待中…")
                tst["unknown_logged"] = True
            return None
        tst["unknown_logged"] = False
        if r == "acted":
            return None

    # 看得到主持人 → 點他，讓遊戲自己走過去
    if tc.get("click_move", True):
        rc = town_click(frame, rect, cfg, inp, tst, now, lc)
        if rc is not False:
            return rc

    if now < tst.get("detour_until", 0):
        return tst["detour"]

    target = tst.get("target")
    if target is None:
        if tst.get("last_dir") and now - tst.get("last_seen", 0) < 2.5:
            return tst["last_dir"]
        npc_w = tst.get("npc_world")
        if npc_w is None and not tst.get("guess_used") and tc.get("learn_route", True) \
                and not tst.get("zoomed"):
            rt_ = load_route()
            H_, W_ = frame.shape[:2]
            if rt_ and rt_.get("size") == [W_, H_] and rt_.get("npc"):
                tst["guess_used"] = True
                npc_w = tst["npc_world"] = (float(rt_["npc"][0]), float(rt_["npc"][1]))
                log(f"畫面上看不到{tc.get('npc_name', '節慶主持人')} → 先往上次主持人的位置走")
        if npc_w is not None and not tst.get("npc_w_reached"):
            # 剛剛有看到過主持人 → 先走回記得的位置（不要亂走）
            wx, wy = tst.get("world", (0.0, 0.0))
            gx, gy = npc_w[0] - wx, npc_w[1] - wy
            gd = math.hypot(gx, gy)
            if not tst.get("lost_logged"):
                log(f"畫面上暫時看不到{tc.get('npc_name', '節慶主持人')} → 往剛才看到的位置走")
                tst["lost_logged"] = True
            if gd > 40:
                desired = (gx / gd, gy / gd)
                plan_path(frame, (lc[0] + gx, lc[1] + gy), start=lc)  # 更新障礙圖
                sd = safe_key_dir(desired, lc, blocked_screen, prefer=tst.get("key_dir"))
                tst["key_dir"] = sd
                step = sd or desired
                return _town_stuck(tst, now, world, step) or step
            tst["npc_w_reached"] = True
            tst["lost_logged"] = False
        if npc_w is not None:
            # 走到記得的位置還是看不到：繞著那個位置一圈一圈往外找（不要亂走到很遠）
            if not tst.get("lost_logged"):
                log(f"畫面上找不到{tc.get('npc_name', '節慶主持人')} → 在剛才看到的位置附近繞圈找")
                tst["lost_logged"] = True
                tst["spiral_t0"] = now
            el = now - tst.get("spiral_t0", now)
            rad = 60 + min(240, el * 12)                 # 半徑慢慢變大，最大 300 像素
            ang = el * 0.9
            wx, wy = tst.get("world", (0.0, 0.0))
            gx, gy = npc_w[0] + rad * math.cos(ang) - wx, npc_w[1] + rad * math.sin(ang) - wy
            gd = math.hypot(gx, gy) or 1
            plan_path(frame, (lc[0] + gx, lc[1] + gy), start=lc)  # 更新障礙圖
            sd = safe_key_dir((gx / gd, gy / gd), lc, blocked_screen, prefer=tst.get("key_dir"))
            tst["key_dir"] = sd
            if el > 25:                                  # 繞太久：回到記得的位置重新開始
                tst["spiral_t0"] = now
                tst["npc_w_reached"] = False
            step = sd or (gx / gd, gy / gd)
            return _town_stuck(tst, now, world, step) or step
        if not tst.get("lost_logged"):
            log(f"畫面上找不到{tc.get('npc_name', '節慶主持人')}，四處找找")
            tst["lost_logged"] = True
        if now >= tst.get("wander_next", 0):
            ang = random.uniform(0, 2 * math.pi)
            tst["wander"] = (math.cos(ang), math.sin(ang))
            tst["wander_next"] = now + random.uniform(1.5, 3.0)
        return tst["wander"]

    tx, ty = target
    dx, dy = tx - lc[0], ty - lc[1]
    dist = math.hypot(dx, dy) or 1
    if dist < 28:
        tst["ocr_next"] = 0  # 到了，馬上讀字看有沒有交談提示
        return None

    path = None
    rt = load_route()
    if tst.get("route_on") and rt:
        # 照記住的路線走：找路線上離自己最近的點，往前看約 4 個點
        offx, offy = tst.get("route_off", (0.0, 0.0))
        pts = rt["points"]
        i0 = tst.get("route_i", 0)
        best = min(range(i0, min(len(pts), i0 + 25)),
                   key=lambda i: math.hypot(pts[i][0] + offx - world[0], pts[i][1] + offy - world[1]))
        tst["route_i"] = best
        j = min(len(pts) - 1, best + 1)
        if math.hypot(pts[j][0] + offx - world[0], pts[j][1] + offy - world[1]) < 8:
            j = min(len(pts) - 1, j + 1)
        rx, ry = pts[j][0] + offx - world[0], pts[j][1] + offy - world[1]
        rd = math.hypot(rx, ry)
        if best >= len(pts) - 2 or rd < 6:
            tst["route_on"] = False  # 路線走完，剩下直接走到主持人
            tst["route_on_success"] = True
        else:
            step_dir = (rx / rd, ry / rd)
            path = "route"
    if path is None:
        # 完整規劃（A*）每 0.4 秒做一次；中間沿著上次規劃好的路線走（路線用地圖座標記）
        tgt_w = (tx - lc[0] + world[0], ty - lc[1] + world[1])
        pw = tst.get("path_w")
        ptg = tst.get("plan_tgt") or (1e9, 1e9)
        if pw is None or now >= tst.get("plan_t", 0) + 0.4 or \
                math.hypot(tgt_w[0] - ptg[0], tgt_w[1] - ptg[1]) > 40:
            p = plan_path(frame, (tx, ty), start=lc, blocked=blocked_screen)
            tst["plan_t"], tst["plan_tgt"] = now, tgt_w
            tst["path_w"] = pw = [(x - lc[0] + world[0], y - lc[1] + world[1]) for x, y in p] if p else None
        else:
            plan_path(frame, (lc[0], lc[1] - 1), start=lc)  # 只更新障礙圖（檢查前方用）
        path = [(x - world[0] + lc[0], y - world[1] + lc[1]) for x, y in pw] if pw else None
    else:
        plan_path(frame, (lc[0], lc[1] - 1), start=lc)  # 只為了更新障礙圖
    if path == "route":
        pass
    elif path and len(path) > 2:
        i0 = min(range(len(path)), key=lambda i: math.hypot(path[i][0] - lc[0], path[i][1] - lc[1]))
        wx, wy = path[min(len(path) - 1, i0 + 5)]
        wdx, wdy = wx - lc[0], wy - lc[1]
        wd = math.hypot(wdx, wdy) or 1
        step_dir = (wdx / wd, wdy / wd)
        if not tst.get("path_logged"):
            log(f"規劃好路線（約 {len(path)} 步），繞過障礙物")
            tst["path_logged"] = True
        if cfg["debug_snapshots"] and now >= tst.get("snap_next", 0):
            tst["snap_next"] = now + 3
            img = frame.copy()
            for a_, b_ in zip(path, path[1:]):
                cv2.line(img, (int(a_[0]), int(a_[1])), (int(b_[0]), int(b_[1])), (255, 255, 0), 3)
            cv2.drawMarker(img, (int(tx), int(ty)), (0, 0, 255), cv2.MARKER_CROSS, 30, 3)
            save_snapshot(img, "town")
    else:
        step_dir = (dx / dist, dy / dist)
    # 換成實際按得出來的 8 方向，並確認前方不是障礙
    if dist > 60 and path != "route":   # 記住的路線是走過的，不用再檢查
        sd = safe_key_dir(step_dir, lc, blocked_screen, prefer=tst.get("key_dir"))
        if sd is not None:
            step_dir = sd
        tst["key_dir"] = sd
    tst["last_dir"] = step_dir
    return _town_stuck(tst, now, world, step_dir) or step_dir


def handle_menu(kind, sct, rect, cfg, inp, st):
    """處理一次選單；回傳 True = 有處理，False = 畫面上已經沒有選單"""
    sec = cfg.get(kind, {})
    if st.get("first", True):
        time.sleep(random.uniform(*sec.get("think_sec", [0.2, 0.5])))  # 第一張才「看一眼」
    frame = grab(sct, rect)
    st["frame"] = frame
    ox, oy = rect[0], rect[1]
    items = ocr(frame, ox, oy, scale=cfg["menu_ocr_scale"])
    now_kind = detect_menu(items)
    if now_kind is None:
        if any(_is_button(i) for i in items) and kind in ("levelup", "chest"):
            now_kind = kind  # 標題沒讀到，但卡片按鈕還在
        else:
            return False
    if not cfg.get(now_kind, {}).get("enabled", True):
        return False
    kind = now_kind
    rel = [dict(i, x=i["x"] - ox, y=i["y"] - oy) for i in items]

    if kind == "expedition":
        return handle_expedition(frame, items, rect, cfg, inp)

    if kind in ("afk", "login", "charselect"):
        return handle_login(kind, frame, items, rect, cfg, inp)

    if kind in ("result", "lobby"):
        sec = cfg[kind]
        btn = next((i for i in items if match(i["text"], sec["button"])), None)
        label = "這局結束了" if kind == "result" else "選擇大廳"
        if not btn:
            log(f"{label}：找不到「{sec['button']}」按鈕，稍後再試")
            if cfg["debug_snapshots"]:
                save_snapshot(frame, f"menu_{kind}_nobutton", items=rel)
            time.sleep(0.5)
            return True
        if kind == "result" and not GAME.get("game_counted"):
            dump_end_ring(cfg, frame)
            report_game_end(cfg)
        log(f"{label} → 按「{sec['button']}」")
        inp.click_item(btn)
        GAME["phase"] = "town"
        GAME.update(max_timer=0, ending_logged=False)
        if kind == "result" and not GAME.get("game_counted"):
            GAME["game_counted"] = True  # 每一局只算一次，進入下一場戰鬥才重設
            GAME["games_done"] = GAME.get("games_done", 0) + 1
            GAME["games_since_rest"] = GAME.get("games_since_rest", 0) + 1
            log(f"第 {GAME['games_done']} 場結束")
            GAME.update(fourth=None, slots=0, banish_off=False, banish_fail=0, last_banish=None,
                        levelups=0, attack_banished=False, fourth_banished=False,
                        fourth_ok=False, fourth_pick=None, banished_names=set(), seen_full=set(),
                        full_menus=0,
                        auto_logged=False, chest_reroll_off=False, chest_reroll_fail=0,
                        chest_banish_off=False, chest_banish_fail=0, chest_banish_wait_logged=False,
                        focused=set(), focus_off=False, late_logged=False)
        time.sleep(random.uniform(0.8, 1.4))
        return True

    sec = cfg[kind]
    name = "升級" if kind == "levelup" else "寶箱"
    if kind == "chest" and sec.get("auto", False):
        ab = next((i for i in items if match(i["text"], "自動寶箱")), None)
        if ab:
            time.sleep(random.uniform(*sec.get("think_sec", [0.2, 0.5])))
            pos = inp.click_item(ab, "自動寶箱") or (round(ab["cx"]), round(ab["cy"]))
            log(f"寶箱選單 → 按「自動寶箱」（點擊 {pos[0]},{pos[1]}）")
            return True
    cards = find_cards(items, rect[3], frame, ox, oy)
    if len(cards) < 3 and cfg["menu_ocr_scale"] < 1.5:
        # 三張卡沒讀齊 → 放大再讀一次，取讀到比較多張的結果
        items2 = ocr(frame, ox, oy, scale=1.5)
        cards2 = find_cards(items2, rect[3], frame, ox, oy)
        if len(cards2) > len(cards):
            items, cards = items2, cards2
            rel = [dict(i, x=i["x"] - ox, y=i["y"] - oy) for i in items]
    if not cards:
        log(f"{name}選單：看不到卡片，稍後再試")
        if cfg["debug_snapshots"]:
            save_snapshot(frame, f"menu_{kind}_nocard", items=rel)
        time.sleep(0.4)
        return True

    # 要比對的文字區塊：卡片標題、上方等級（畫面座標）
    regions = []
    for c in cards:
        t = c["title"]
        regions.append((t["x"] - ox - 4, t["y"] - oy - 4, t["x"] - ox + t["w"] + 4,
                        t["y"] - oy + t["h"] + 4))
    for i in items:
        if "%" in i["text"] and ("HP" in i["text"].upper() or "伤害" in i["n"]):
            regions.append((i["x"] - ox - 4, i["y"] - oy - 4, i["x"] - ox + i["w"] + 4,
                            i["y"] - oy + i["h"] + 4))
            break
    st["regions"] = regions

    # 確認畫面上的卡片已經換成新的一組（避免換卡動畫還沒播完就照舊卡片選）
    sig = (read_level(items), tuple(canon_name(c["title"]["text"]) for c in cards))
    if sig == GAME.get("last_sig") and time.time() - GAME.get("last_sig_t", 0) < 2.0:
        time.sleep(0.1)
        st["acted"] = False
        return True
    GAME["last_sig"], GAME["last_sig_t"] = sig, time.time()

    log(f"{name}選單：" + "、".join(c["title"]["text"] for c in cards))
    if cfg["debug_snapshots"]:
        save_snapshot(frame, f"menu_{kind}", items=rel)

    if kind == "levelup":
        ACTION.clear()
        st["ox"], st["oy"] = ox, oy
        handle_levelup(cards, items, sec, inp, st)
        st["acted"] = True
        if cfg["debug_snapshots"] and ACTION:
            save_action_snapshot(frame, ox, oy, rel)
        return True

    sec = dict(sec, priority=chest_priority(sec))   # 前期／後期排名
    best = choose_card(cards, sec, read_slots(items, "道具"))
    st["ox"], st["oy"] = ox, oy
    if chest_rank_actions(cards, best, sec, inp, st, cfg.get("levelup", {})):
        st["acted"] = True
        return True
    if sec.get("banish_avoid") and st["banished"] < 3:
        bad = [c for c in cards if c["avoid"]]
        if bad:
            log(f"  放逐：{bad[0]['title']['text']}")
            inp.click_item(bad[0]["banish"], "放逐")
            st["banished"] += 1
            st["acted"] = True
            return True
    # 排名前幾的寶箱：先按「專注」（之後更常出現），再選它；每種道具每局只專注一次
    fr_ = int(sec.get("focus_rank", 3) or 0)
    nm_ = best.get("item") or best["title"]["text"]
    fb_ = best.get("focus")
    # 專注按鈕沒讀到、位置又只能用猜的 → 不按（怕按到旁邊的「放逐」把最想要的卡放逐掉）
    fsafe_ = bool(fb_) and (not fb_.get("estimated") or (
        not (best.get("reroll") or {"estimated": True}).get("estimated") and
        not (best.get("banish") or {"estimated": True}).get("estimated")))
    if fr_ > 0 and best.get("rank") and best["rank"] <= fr_ and not GAME.get("focus_off") \
            and nm_ not in GAME.setdefault("focused", set()) and fsafe_:
        dis_f = button_really_disabled(st, best["focus"], "chest_f")
        if dis_f is None:
            time.sleep(0.6)
            st["acted"] = True
            return True
        if dis_f:
            GAME["focus_off"] = True
            log("  專注按鈕變灰色了（次數用完），這局不再專注")
        else:
            b = best["focus"]
            pos = inp.click_item(b, "專注") or (round(b["cx"]), round(b["cy"]))
            GAME["focused"].add(nm_)
            log(f"  專注：{best['title']['text']}（第 {best['rank']} 名，之後更常出現；點擊 {pos[0]},{pos[1]}"
                + ("，位置是推算的" if b.get("estimated") else "") + "）")
            st["acted"] = True
            time.sleep(0.5)
            return True
    if not best["pri"] and st["rerolls"] < sec.get("max_reroll", 0) and sec.get("priority"):
        rr = next((i for i in items if i["n"] == "重抽全部"), None)
        if rr:
            log("  沒有想要的裝備 → 重抽全部")
            inp.click_item(rr, "重抽全部")
            st["rerolls"] += 1
            st["acted"] = True
            return True
    log(f"  選擇：{best['title']['text']}")
    pick_card(best, cards, items, inp, sec.get("number_keys", True))
    st["acted"] = True
    return True


def button_disabled(st, b):
    """按鈕是不是灰色的（次數用完）：亮白色 = 可以按，暗灰色 = 不能按"""
    fr = st.get("frame")
    if fr is None or b is None:
        return False
    ox, oy = st.get("ox", 0), st.get("oy", 0)
    cx, cy = b["cx"] - ox, b["cy"] - oy
    w, h = max(6, b.get("w", 30) * 0.3), max(4, b.get("h", 20) * 0.3)
    x0, x1 = int(max(0, cx - w)), int(min(fr.shape[1], cx + w))
    y0, y1 = int(max(0, cy - h)), int(min(fr.shape[0], cy + h))
    if x1 - x0 < 3 or y1 - y0 < 3:
        return False
    v = cv2.cvtColor(fr[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)[:, :, 2]
    return float(np.median(v)) < 160


def button_really_disabled(st, b, key):
    """按鈕灰色要連續看到兩次（隔 0.5 秒以上）才算數：
    卡片剛換、動畫還在播的時候按鈕也會暫時變暗，會被誤判成次數用完"""
    if not button_disabled(st, b):
        GAME.pop("dis_" + key, None)
        return False
    t0 = GAME.get("dis_" + key)
    now = time.time()
    if t0 is None or now - t0 > 6:
        GAME["dis_" + key] = now
        return None             # 第一次看到：先等一下再看
    return now - t0 >= 0.5


def core_banish_done(cfg_lv):
    """第四技能、攻擊：屬性附加 都放逐過了（技能欄滿了 8 次還沒出現的，當成不會出現）"""
    long_ = GAME.get("full_menus", 0) >= 8
    seen = GAME.get("seen_full", set())
    need_attack = any(k in ALWAYS_BANISH for k in cfg_lv.get("banish", []))
    attack_ok = GAME.get("attack_banished") or not need_attack or \
        (long_ and not any(similar(n, k) for n in seen for k in ALWAYS_BANISH))
    need_fourth = cfg_lv.get("banish_fourth", True)
    f4 = cfg_lv.get("fourth_skill") or GAME.get("fourth")
    fourth_ok = GAME.get("fourth_banished") or not need_fourth or \
        (long_ and GAME.get("fourth_ok") and not (f4 and any(similar(n, f4) for n in seen)))
    return bool(attack_ok and fourth_ok)


def skill_banish_done(cfg_lv):
    """技能該放逐的都放逐完了：第四技能、攻擊：屬性附加，再加上勾了放逐的主力類型技能（例如冰箭術）。
    放逐次數是技能和寶箱共用的，所以寶箱要等這個完成才放逐"""
    if not core_banish_done(cfg_lv):
        return False
    done = GAME.get("banished_names", set())
    seen = GAME.get("seen_full", set())
    long_ = GAME.get("full_menus", 0) >= 8
    for k in cfg_lv.get("banish", []):
        if k not in MAIN_TYPE or any(similar(k, p) for p in cfg_lv.get("priority", [])):
            continue
        if any(similar(k, d) for d in done):
            continue
        # 技能欄滿了以後一直沒出現 → 這隻角色沒有這個技能，不用等
        if long_ and not any(similar(k, n) for n in seen):
            continue
        return False
    return True


def chest_rank_actions(cards, best, sec, inp, st, lv_sec=None):
    """寶箱：排名太後面的卡 → 放逐（技能放逐完之後才做）或 按那張卡的「重抽」。有動作回傳 True"""
    def rk(c):
        return c.get("rank") or 9999

    def label(c):
        return f"{c['title']['text']}（" + (f"第 {c['rank']} 名" if c.get("rank") else "不在清單") \
            + ("、新道具" if c.get("bad_new") else "") + "）"

    def bad_for(c, n):
        return rk(c) > n or c.get("bad_new")

    sig = tuple(norm(c["title"]["text"]) for c in cards)

    def turn_off(kind, why):
        if not GAME.get(kind + "_off"):
            GAME[kind + "_off"] = True
            log(f"  寶箱{'重抽' if kind == 'chest_reroll' else '放逐'}{why}，這局不再使用")

    # 上次按了重抽／放逐，同一個位置的卡片還是同一張（文字辨識可能有點不同）→ 沒有作用
    la = st.get("chest_last")
    if la and time.time() - la["t"] < 8:
        cs = sorted(cards, key=lambda c: c["title"]["cx"])
        same = la["idx"] < len(cs) and similar(cs[la["idx"]]["title"]["text"], la["title"])
        if same:
            GAME[la["kind"] + "_fail"] = GAME.get(la["kind"] + "_fail", 0) + 1
            if GAME[la["kind"] + "_fail"] >= 2:
                turn_off(la["kind"], "按了卡片沒變（次數可能用完了）")
        else:
            GAME[la["kind"] + "_fail"] = 0
    st["chest_last"] = None

    def remember(kind, c):
        cs = sorted(cards, key=lambda x: x["title"]["cx"])
        st["chest_last"] = {"kind": kind, "t": time.time(), "idx": cs.index(c),
                            "title": c["title"]["text"], "sig": sig}

    # 1. 放逐：排名在前 N 名之外，而且技能那邊已經放逐完
    br = int(sec.get("banish_rank", 0) or 0)
    if br > 0 and not GAME.get("chest_banish_off") and st.get("card_banishes", 0) < 3:
        # 只放逐「認得出來」的寶箱（標題讀錯、認不出是什麼的不放逐，免得放逐到好東西）
        bad = [c for c in cards if c is not best and c.get("item") and
               (rk(c) > br or c.get("item") not in sec.get("priority", []))]
        if bad:
            if not skill_banish_done(lv_sec or {}):
                if not GAME.get("chest_banish_wait_logged"):
                    GAME["chest_banish_wait_logged"] = True
                    log("  寶箱有排名太後面的卡，但技能還沒放逐完 → 先不放逐寶箱")
            elif (dis_b := button_really_disabled(st, max(bad, key=rk)["banish"], "chest_b")) is None:
                time.sleep(0.6)
                return True
            elif dis_b:
                turn_off("chest_banish", "按鈕變灰色了（放逐次數用完）")
            else:
                c = max(bad, key=rk)
                b = c["banish"]
                pos = inp.click_item(b, "放逐") or (round(b["cx"]), round(b["cy"]))
                log(f"  寶箱放逐：{label(c)}，在前 {br} 名之外（點擊 {pos[0]},{pos[1]}"
                    + ("，位置是推算的" if b.get("estimated") else "") + "）")
                remember("chest_banish", c)
                st["card_banishes"] = st.get("card_banishes", 0) + 1
                GAME["chest_banishes"] = GAME.get("chest_banishes", 0) + 1
                return True

    # 2. 重抽：三張卡裡有排名在前 N 名之外的 → 按那張卡的「重抽」（三張都在前 N 名就直接選）
    rr = int(sec.get("reroll_rank", 0) or 0)
    if rr > 0 and not GAME.get("chest_reroll_off") \
            and st.get("card_rerolls", 0) < int(sec.get("reroll_limit", 6) or 0):
        # 要拿的那張已經在前 N 名內（而且不是會佔欄位的爛新道具）→ 直接拿，不浪費重抽
        bad = [c for c in cards if bad_for(c, rr)] if bad_for(best, rr) else []
        dis_r = button_really_disabled(st, max(bad, key=rk)["reroll"], "chest_r") if bad else False
        if dis_r is None:
            time.sleep(0.6)
            return True
        if dis_r:
            turn_off("chest_reroll", "按鈕變灰色了（重抽次數用完）")
            bad = []
        if bad:
            c = max(bad, key=rk)
            b = c["reroll"]
            pos = inp.click_item(b, "重抽") or (round(b["cx"]), round(b["cy"]))
            st["card_rerolls"] = st.get("card_rerolls", 0) + 1
            log(f"  寶箱重抽：{label(c)}，在前 {rr} 名之外（點擊 {pos[0]},{pos[1]}"
                + ("，位置是推算的" if b.get("estimated") else "") + "）")
            remember("chest_reroll", c)
            return True
    return False


def _is_empty_slot(i):
    n = i["n"]
    return len(n) <= 4 and n.startswith("空") and n.endswith("位")   # 空欄位（常讀成 空橱位）


def char_slot_pos(items, frame, ox, oy, slot):
    """角色選擇畫面：第 slot 個欄位（1~15，5 欄 × 3 排）的中心點（螢幕座標）。
    用每格裡讀得到的字（「Base Lv …」那行、「空欄位」）推出整個 5×3 格子。
    回傳 ((x, y), 開始遊戲按鈕 或 'empty')；看不出格子回傳 (None, None)"""
    H, W = frame.shape[:2]
    anchors = []   # (卡片中心 x, 卡片中心 y 的估計, 是否空格)
    for i in items:
        if "baselv" in i["n"] and "job" in i["n"]:
            anchors.append([i["cx"], i["cy"], 64, False])
        elif _is_empty_slot(i):
            anchors.append([i["cx"], i["cy"], 16, True])
    if len(anchors) < 2:
        return None, None
    # 欄：x 分群
    xs = sorted(a[0] for a in anchors)
    cols = []
    for x in xs:
        if cols and x - cols[-1][-1] < 40:
            cols[-1].append(x)
        else:
            cols.append([x])
    cx_ = [sum(c) / len(c) for c in cols]
    if len(cx_) >= 2:
        steps = [b_ - a_ for a_, b_ in zip(cx_, cx_[1:])]
        pitch = sorted(steps)[0]
        # 欄距可能跨過沒讀到的欄：取最接近整數倍的最小間距
        pitch = min(st_ / max(1, round(st_ / pitch)) for st_ in steps)
    else:
        pitch = 152.3 * W / 1264.0
    sc = pitch / 152.3
    for a in anchors:
        a[1] = a[1] - a[2] * sc            # 換成卡片中心
    # 第一欄的 x：最左邊那群往左推（畫面最多 5 欄）
    x0 = cx_[0]
    while x0 - pitch > (sum(cx_) / len(cx_)) - 2.5 * pitch and x0 - pitch > 0 \
            and round((cx_[-1] - (x0 - pitch)) / pitch) <= 4:
        x0 -= pitch
    # 排：y 分群
    ys = sorted(a[1] for a in anchors)
    rows = []
    for y in ys:
        if rows and y - rows[-1][-1] < 50 * sc:
            rows[-1].append(y)
        else:
            rows.append([y])
    ry = [sum(r) / len(r) for r in rows]
    rpitch = 166.5 * sc
    y0 = ry[0]
    while y0 - rpitch > H * 0.15 and round((ry[-1] - (y0 - rpitch)) / rpitch) <= 2:
        y0 -= rpitch
    r, c = divmod(max(1, min(15, int(slot))) - 1, 5)
    x, y = x0 + c * pitch, y0 + r * rpitch
    empty = any(a[3] and abs(a[0] - x) < pitch / 2 and abs(a[1] - y) < rpitch / 2 for a in anchors)
    # 「開始遊戲」按鈕：讀得到就用；否則在「建立」右邊，或用格子位置推算
    btn = next((i for i in items if match(i["text"], "開始遊戲") and i["cy"] - oy > H * 0.6), None)
    if btn is None:
        bx = x0 + 4 * pitch + 25 * sc
        by = y0 + 2 * rpitch + 186 * sc
        mk = next((i for i in items if i["n"] == norm("建立") and i["cy"] - oy > H * 0.6), None)
        if mk:
            bx, by = mk["cx"] + 85 * sc, mk["cy"]
        btn = {"text": "開始遊戲", "x": bx - 35 * sc, "y": by - 12 * sc, "w": 70 * sc, "h": 24 * sc}
    return (x, y), ("empty" if empty else btn)


EXP = {}


def find_sliders(frame, items, ox, oy):
    """難度視窗：每一列百分比文字左邊的滑桿軌道（左端、右端、y），由上到下"""
    H, W = frame.shape[:2]
    out = []
    for i in items:
        t = i["text"].replace(" ", "")
        if not (t.endswith("%") and t[:-1].isdigit()) or i["cx"] - ox < W * 0.5:
            continue
        y = int(i["cy"] - oy)
        x0 = max(0, int(i["cx"] - ox - 420))
        x1 = int(i["x"] - ox - 6)
        if x1 - x0 < 80:
            continue
        row = frame[max(0, y - 1):y + 2, x0:x1].min(axis=2).min(axis=0) < 246
        runs, st = [], None
        for k, v in enumerate(row):
            if v and st is None:
                st = k
            if not v and st is not None:
                runs.append((st, k))
                st = None
        if st is not None:
            runs.append((st, len(row)))
        runs = [r for r in runs if r[1] - r[0] > 60]
        if runs:
            a, b = runs[-1]
            out.append((ox + x0 + a, ox + x0 + b, oy + y, t))
    return sorted(out, key=lambda r: r[2])


def set_difficulty(frame, items, rect, cfg, inp):
    """把難度滑桿全部拉到最左（最簡單）或最右（最難）"""
    level = cfg["town"].get("difficulty", "keep")
    if level not in ("min", "max"):
        return False
    sl = find_sliders(frame, items, rect[0], rect[1])
    if not sl:
        log("難度視窗：找不到滑桿，維持遊戲目前的難度")
        return False
    want = "最簡單" if level == "min" else "最難"
    log(f"難度 → 全部調成{want}（目前：{'、'.join(r[3] for r in sl)}）")
    MOVER.set(None, False)
    for a, b, y, _ in sl:
        x = a + 3 if level == "min" else b - 3
        inp.click_item({"text": "難度滑桿", "x": x - 1, "y": y - 2, "w": 2, "h": 4}, "難度滑桿")
        time.sleep(random.uniform(0.15, 0.35))
    return True


def town_mode(tc):
    """這一場要刷什麼：設定是遠征，但遠征連續失敗 → 先改刷節慶主持人一陣子，不要卡著"""
    mode = tc.get("mode", "festival")
    if mode == "expedition" and time.time() < GAME.get("exp_fallback_until", 0):
        return "festival"
    return mode


def expedition_fail(frame, items, rect, inp, reason):
    """遠征選關失敗：存一張圖（方便查原因）、關掉視窗；連續 3 次就先改刷節慶主持人 20 分鐘"""
    ox, oy = rect[0], rect[1]
    H, W = frame.shape[:2]
    EXP["fails"] = EXP.get("fails", 0) + 1
    log(f"遠征：{reason} → 關掉視窗重試（第 {EXP['fails']} 次）")
    try:
        save_snapshot(frame, "expedition_fail", items=[dict(i, x=i["x"] - ox, y=i["y"] - oy) for i in items])
        log("  畫面存在 logs\\expedition_fail_*.jpg，可以傳給開發者看")
    except Exception:
        pass
    close = next((i for i in items if match(i["text"], "關閉") and i["cy"] - oy > H * 0.8), None)
    if close:
        inp.click_item(close, "關閉")
    else:
        inp.press("escape")
    fails = EXP["fails"]
    EXP.clear()
    EXP["fails"] = fails
    if fails >= 3:
        GAME["exp_fallback_until"] = time.time() + 20 * 60
        EXP["fails"] = 0
        log("遠征連續失敗 3 次 → 這 20 分鐘先改刷節慶主持人，之後再試遠征")
    time.sleep(1.5)
    return True


def handle_expedition(frame, items, rect, cfg, inp):
    """遠征視窗：左邊選關卡 → 確認右邊大標題是那一關 → 按「出發」"""
    tc = cfg["town"]
    ox, oy = rect[0], rect[1]
    H, W = frame.shape[:2]
    MOVER.set(None, False)

    def click(item, label):
        time.sleep(random.uniform(0.4, 0.9))
        log(f"遠征 → 按「{label}」")
        inp.click_item(item, label)

    close = next((i for i in items if match(i["text"], "關閉") and i["cy"] - oy > H * 0.8), None)
    if town_mode(tc) != "expedition":
        log("不小心打開了遠征視窗（這一場要刷節慶主持人）→ 關閉")
        EXP["closes"] = EXP.get("closes", 0) + 1 if time.time() - EXP.get("close_t", 0) < 20 else 1
        EXP["close_t"] = time.time()
        if close:
            click(close, "關閉")
        elif EXP["closes"] == 1:
            inp.press("escape")
        else:   # Esc 關不掉 → 點左下角「關閉」平常的位置
            click({"text": "關閉", "x": ox + W * 0.06 - 30, "y": oy + H * 0.917 - 12, "w": 60, "h": 24},
                  "關閉")
        time.sleep(1.0)
        return True

    stage = tc.get("stage") or EXPEDITION_STAGES[0]
    if time.time() - EXP.get("t", 0) > 60:          # 新的一次選關
        fails = EXP.get("fails", 0)
        EXP.clear()
        EXP["fails"] = fails
    EXP["t"] = time.time()
    rows = _rows_joined(items)
    # 右邊大標題：目前選到哪一關（範圍放寬，名字長的關卡標題位置會不一樣）
    titles = [i for i in rows if W * 0.36 < i["cx"] - ox < W * 0.98 and H * 0.12 < i["cy"] - oy < H * 0.55
              and stage_in(i["text"])]
    shown = stage_in(max(titles, key=lambda i: i["h"])["text"]) if titles else None

    def find_go():
        go = next((i for i in items if match(i["text"], "出發") and i["cy"] - oy > H * 0.75
                   and i["cx"] - ox > W * 0.55), None)
        if go is None:
            big = ocr(frame[int(H * 0.8):, int(W * 0.55):], ox + int(W * 0.55), oy + int(H * 0.8),
                      scale=1.5, min_score=0.3)
            go = next((i for i in big if match(i["text"], "出發")), None)
        return go

    # 標題讀不到，但已經點過那一關兩次 → 照點的那一關出發（不要一直卡在這裡）
    sure = shown == stage or (shown is None and EXP.get("clicked", 0) >= 2)
    if sure:
        go = find_go()
        if go is None:
            return expedition_fail(frame, items, rect, inp,
                                   f"「{stage}」沒有「出發」按鈕（可能還沒解鎖、或要先看劇情）")
        if shown is None:
            log(f"遠征：右邊標題讀不到，照剛才點的「{stage}」出發")
        click(go, "出發")
        log(f"出發：{stage}（接著調難度、按前往野外）")
        EXP.clear()
        time.sleep(1.5)
        return True

    # 左邊清單找關卡名稱
    left = [i for i in rows if i["cx"] - ox < W * 0.4 and H * 0.1 < i["cy"] - oy < H * 0.92]
    hit = next((i for i in left if stage_in(i["text"]) == stage), None)
    if hit:
        if EXP.get("clicked", 0) >= 4:
            return expedition_fail(frame, items, rect, inp,
                                   f"點了「{stage}」好幾次，右邊還是「{shown or '讀不到'}」")
        EXP["clicked"] = EXP.get("clicked", 0) + 1
        click(hit, stage)
        time.sleep(0.8)
        return True
    # 清單裡沒看到 → 捲動清單（先往下，捲到底再往上）
    d = EXP.setdefault("dir", 1)
    EXP["n"] = EXP.get("n", 0) + 1
    if EXP["n"] > 6:
        EXP["dir"], EXP["n"] = -d, 0
        EXP["cycles"] = EXP.get("cycles", 0) + 1
        if EXP["cycles"] >= 3:
            return expedition_fail(frame, items, rect, inp, f"清單裡找不到「{stage}」")
    if not EXP.get("scroll_logged"):
        log(f"清單裡還沒看到「{stage}」→ 捲動找找")
        EXP["scroll_logged"] = True
    inp.scroll(ox + W * 0.18, oy + H * 0.55, 3 * EXP["dir"], "遠征清單")
    time.sleep(0.6)
    return True


def handle_login(kind, frame, items, rect, cfg, inp):
    """暫離彈窗 → 接受；登入首頁 → 開始遊戲！；角色選擇 → 點選設定的欄位 → 開始遊戲"""
    ox, oy = rect[0], rect[1]
    H, W = frame.shape[:2]
    sec = cfg[kind]
    MOVER.set(None, False)
    if kind == "afk":
        btn = next((i for i in items if match(i["text"], sec["button"])), None)
        if btn:
            log("跳出「暫離」（太久沒動作被請離）→ 按「接受」")
            inp.click_item(btn)
            time.sleep(random.uniform(1.0, 1.8))
        return True
    if kind == "login":
        cands = [i for i in items if match(i["text"], sec["button"])]
        if not cands:
            return False
        btn = max(cands, key=lambda i: i["w"] * i["h"])
        log("登入首頁 → 按「開始遊戲！」")
        inp.click_item(btn)
        time.sleep(random.uniform(1.5, 2.5))
        return True
    # 角色選擇
    slot = int(sec.get("slot", 1) or 1)
    pos, btn = char_slot_pos(items, frame, ox, oy, slot)
    if pos is None:
        log("角色選擇：找不到「開始遊戲」按鈕，稍後再試")
        if cfg["debug_snapshots"]:
            save_snapshot(frame, "menu_charselect_nobutton",
                          items=[dict(i, x=i["x"] - ox, y=i["y"] - oy) for i in items])
        time.sleep(0.8)
        return True
    if btn == "empty":
        log(f"角色選擇：第 {slot} 格是空的，請到「基本」頁改選有角色的欄位（先停在這裡）")
        time.sleep(5)
        return True
    log(f"角色選擇 → 點第 {slot} 格角色")
    inp.click_item({"text": f"角色{slot}", "x": pos[0] - 30, "y": pos[1] - 30, "w": 60, "h": 60})
    time.sleep(random.uniform(0.6, 1.0))
    log("角色選擇 → 按「開始遊戲」")
    inp.click_item(btn)
    GAME["phase"] = "town"
    time.sleep(random.uniform(1.5, 2.5))
    return True


def process_menus(kind, sct, rect, cfg, inp, st, dry):
    """連續處理選單，直到畫面上沒有選單為止（累積多次升級時一路選完）"""
    MOVER.set(None, False)
    time.sleep(0.1)
    st["first"] = True
    st["sct"], st["rect"] = sct, rect
    misses = 0
    for _ in range(3 if dry else 40):
        st["acted"], st["frame"], st["regions"] = False, None, None
        if not handle_menu(kind, sct, rect, cfg, inp, st):
            # 連續選卡中途讀到「沒有選單」：可能還在換場，再等一下確認
            misses += 1
            if misses >= (2 if st["first"] else 3):
                break
            time.sleep(0.2)
            continue
        misses = 0
        st["first"] = False
        if st.get("acted") and not dry:
            # 選完後等一下就讀下一張；是不是新的一組卡，由內容（Lv、卡片名稱）判斷，
            # 不比對像素（卡片的光暈、流光動畫會讓像素一直變）
            time.sleep(0.45 if ACTION.get("kind") == "click" else 0.3)
    st.update(rerolls=0, banished=0, first=True, card_rerolls=0, card_banishes=0, chest_last=None)


def _menu_sig_img(frame):
    """選單中央區域的縮圖（用來比對畫面有沒有換）"""
    h, w = frame.shape[:2]
    c = frame[int(h * 0.2):int(h * 0.85), int(w * 0.15):int(w * 0.85)]
    return cv2.resize(cv2.cvtColor(c, cv2.COLOR_BGR2GRAY), (96, 64),
                      interpolation=cv2.INTER_AREA).astype(np.int16)


def _changed_frac(a, b):
    """兩張縮圖裡「大幅改變」的像素比例：換卡約 0.15，卡片光暈閃動約 0"""
    return float((np.abs(a - b) > 40).mean())


def _regions_changed(a, b, regions, thr=0.2):
    """只比對卡片標題、等級這些文字區塊（不受卡片外圍流動的光暈影響）"""
    h, w = a.shape[:2]
    for x0, y0, x1, y1 in regions:
        x0, y0 = max(0, int(x0)), max(0, int(y0))
        x1, y1 = min(w, int(x1)), min(h, int(y1))
        if x1 - x0 < 4 or y1 - y0 < 4:
            continue
        ga = cv2.cvtColor(a[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY).astype(np.int16)
        gb = cv2.cvtColor(b[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY).astype(np.int16)
        if (np.abs(ga - gb) > 40).mean() > thr:
            return True
    return False


def _regions_have_text(img, regions):
    """文字區塊裡真的有字（有明暗對比）＝不是換場時的黑畫面或淡出中的畫面"""
    h, w = img.shape[:2]
    n = 0
    for x0, y0, x1, y1 in regions:
        x0, y0 = max(0, int(x0)), max(0, int(y0))
        x1, y1 = min(w, int(x1)), min(h, int(y1))
        if x1 - x0 < 4 or y1 - y0 < 4:
            continue
        g = cv2.cvtColor(img[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
        if g.std() > 25:
            n += 1
    return n >= max(1, len(regions) // 2)


def wait_screen_change(sct, rect, before, timeout=2.5, regions=None):
    """等畫面換掉、而且穩定下來就馬上回來（取代固定等待）。
    有 regions 時只看那些文字區塊（選卡之後）；沒有時看整個中央區域（按 B 打開選單時）"""
    t0 = time.time()
    if regions:
        changed, prev, stable = False, None, 0
        while time.time() - t0 < timeout:
            time.sleep(0.05)
            try:
                cur = grab(sct, rect)
            except Exception:
                return
            if cur.shape != before.shape:
                return
            if not changed:
                changed = _regions_changed(cur, before, regions)
            elif prev is not None and not _regions_changed(cur, prev, regions, thr=0.05):
                # 穩定了；選單還在的話，要等文字區塊真的有字（不是換場的黑畫面）
                if _regions_have_text(cur, regions) or stable >= 8:
                    stable += 1
                    if stable >= 2:
                        return
                else:
                    stable += 1 if stable < 8 else 0
            else:
                stable = 0
            prev = cur
        return
    # 按 B 打開選單：偵測到畫面變了，再等一下讓選單出現就開始讀
    # （選單有流動的光暈，畫面不會完全靜止，所以不等「穩定」）
    ref = _menu_sig_img(before)
    while time.time() - t0 < timeout:
        time.sleep(0.05)
        try:
            cur = _menu_sig_img(grab(sct, rect))
        except Exception:
            return
        if _changed_frac(cur, ref) > 0.04:
            time.sleep(0.25)
            return


# =====================================================================
#  主流程
# =====================================================================
STATS = {"start": None, "active": 0.0, "last": 0.0, "games": []}


def stats_reset():
    t = time.time()
    STATS.update(start=t, active=0.0, last=t, games=[])


def stats_text():
    """視窗上顯示的掛機統計（只算打完的場次；休息、暫停的時間不算）"""
    if STATS.get("start") is None:
        return "本次掛機：還沒開始"
    act = STATS["active"]
    hh, mm = divmod(int(act // 60), 60)
    head = f"本次掛機 {hh}:{mm:02d}"
    done = [g for g in STATS["games"] if g["done"]]
    if not done:
        return head + "｜第一場打完後顯示統計"
    n = len(done)
    gph = n / max(act / 3600.0, 1e-6)
    avg = sum(g["dur"] for g in done) / n
    return f"{head}｜{n} 場｜每小時 {gph:.1f} 場｜平均每場 {int(avg // 60)}:{int(avg % 60):02d}"


def restart_edge(bcfg, old_page):
    """定時重開遊戲用的 Edge：關掉 → 重新開 → 重新連線。回傳新的連線；失敗回傳 None"""
    import browser_cdp as bc
    port = int(bcfg.get("port", 9229))
    log("掛機一段時間了 → 重開遊戲用的 Edge（瀏覽器開太久會越來越慢、越吃記憶體）")
    try:
        old_page.close()
    except Exception:
        pass
    if not bc.close_browser(port):
        log("Edge 沒有關掉，這次先不重開，繼續用原本的")
        try:
            return bc.CdpPage(port, "ro-survivors", log=log)
        except Exception as e:
            log(f"重新連線失敗：{e}")
            return None
    time.sleep(3)          # 等 Edge 完全結束（設定檔解除鎖定）
    exe = bc.find_edge(bcfg.get("edge_path", ""))
    if not bc.launch_browser(exe, bcfg.get("url", "https://ro-survivors.com"), port,
                             os.path.join(BASE_DIR, "edge_profile"),
                             tuple(bcfg.get("window_size", [1280, 960])), log=log):
        return None
    page = None
    for _ in range(30):
        try:
            page = bc.CdpPage(port, "ro-survivors", log=log)
            break
        except Exception:
            time.sleep(1)
    if page is None:
        log("Edge 重開了，但連不上")
        return None
    if bcfg.get("lock_window_size", True):
        try:
            ws = bcfg.get("window_size", [1280, 960])
            page.ensure_window(ws[0], ws[1])
        except Exception:
            pass
    page.latest(wait=20)
    log("Edge 重開好了 → 接著自動登入、選角色、繼續掛機")
    return page


def run(cfg):
    global MOVER
    dry = cfg["dry_run"]
    CONTROL["dry"] = dry
    try:
        import licensing
        _lic = licensing.check()
    except Exception as e:
        _lic = {"ok": False, "msg": f"授權檢查失敗：{e}"}
    if not _lic["ok"]:
        log(_lic["msg"])
        return
    log(_lic["msg"])
    CONTROL["lic_next"] = time.time() + 600
    inp = Input(dry)
    log("模式：" + ("測試（只記錄、不操作）" if dry else "正式執行"))
    log("F8 暫停／繼續，F9 結束" + ("" if cfg.get("browser", {}).get("mode") == "cdp"
                                     else "，滑鼠甩到左上角緊急停止"))

    global IO
    bcfg = cfg.get("browser", {})
    use_cdp = bcfg.get("mode") == "cdp"
    page = None
    hwnd = None
    edge_t0 = time.time()
    if use_cdp:
        import browser_cdp as bc
        exe = bc.find_edge(bcfg.get("edge_path", ""))
        profile = os.path.join(BASE_DIR, "edge_profile")
        if not bc.launch_browser(exe, bcfg.get("url", "https://ro-survivors.com"),
                                 int(bcfg.get("port", 9229)), profile,
                                 tuple(bcfg.get("window_size", [1280, 960])), log=log):
            return
        page = bc.CdpPage(int(bcfg.get("port", 9229)), "ro-survivors", log=log)
        IO = CdpIO(page)
        edge_t0 = time.time()
        if bcfg.get("lock_window_size", True):
            try:
                ws = bcfg.get("window_size", [1280, 960])
                did = page.ensure_window(ws[0], ws[1])
                log(f"遊戲視窗{did}" if did else f"遊戲視窗大小固定為 {ws[0]}×{ws[1]}")
            except Exception as e:
                log(f"沒辦法調整遊戲視窗大小：{e}")
        log("背景模式：直接控制遊戲用的 Edge，可以把它放到其他視窗後面"
            + ("（被縮到最小會自動還原）" if bcfg.get("lock_window_size", True) else "，但不要縮到最小"))
    else:
        IO = ScreenIO()
    if not use_cdp and cfg["window_title"]:
        w = find_window(cfg["window_title"])
        if not w:
            log(f"找不到標題含「{cfg['window_title']}」的視窗。目前開著的視窗有：")
            for _, t in list_windows():
                t = _ZW.sub(" ", t).strip()
                if t:
                    log(f"    {t}")
            log("請從上面挑一個，把其中一段文字填進 bot_config.json 的 window_title。")
            return
        hwnd = w[0]
        log(f"目標視窗：{_ZW.sub(' ', w[1])}")
        try:
            user32.SetForegroundWindow(hwnd)
        except Exception:
            pass

    lower_own_priority()
    log("載入文字辨識模型…")
    get_ocr("main")
    get_ocr("bg")
    vision = Vision(cfg)
    mcfg = cfg["move"]
    planner = Planner(mcfg)
    mmap = None
    if mcfg.get("minimap", True):
        try:
            import minimap as _mmod
            mmap = _mmod.MiniMap(BASE_DIR, keys=mcfg.get("mode", "keys") == "keys", log=log)
        except Exception as e:
            log(f"小地圖功能載入失敗（照舊走路）：{e}")
    dodger = None
    if mcfg.get("boss_dodge", True):
        try:
            import boss as _boss
            dodger = _boss.BossDodge()
        except Exception as e:
            log(f"頭目閃避功能載入失敗：{e}")
    MOVER = Mover(mcfg, dry)
    MOVER.start()
    watcher = OcrWatcher(cfg)
    watcher.start()
    hot = Hotkeys()

    for s in range(3, 0, -1):
        if CONTROL["stop"]:
            break
        log(f"{s} 秒後開始，請切換到遊戲畫面…")
        time.sleep(1)

    paused = False
    CONTROL["paused"] = False
    CONTROL["status"] = ""
    stats_reset()
    battle_t0 = time.time()
    GAME.update(games_done=0, games_since_rest=0, rest_target=None, rest_until=None,
                game_counted=False, phase=None, battle_seen=0, last_sig=None,
                batch_target=None, max_timer=0, ending_logged=False)
    sch = cfg.get("schedule", {})
    if sch.get("enabled"):
        log(f"休息排程：每玩 {sch.get('games_min')}～{sch.get('games_max')} 場，"
            f"休息 {sch.get('rest_min_minutes')}～{sch.get('rest_max_minutes')} 分鐘"
            + (f"；玩滿 {sch.get('stop_after_games')} 場自動停止" if sch.get("stop_after_games") else ""))
    d = None
    wander = (random.uniform(-1, 1), random.uniform(-1, 1))
    wander_next = 0
    escape_until = 0
    last_coord, coord_same_since, last_coord_t = None, time.time(), 0
    snap_next = 0
    menu_st = {"rerolls": 0, "banished": 0}
    menu_done_t = 0
    last_up_press = 0
    unfocused_logged = False
    st_town = {}
    bst = {"prev": None, "world": (0.0, 0.0), "blocked": [], "avoid": [], "hist": [],
           "path_next": 0, "path_tgt": None, "path_dir": None}
    vp_next = 0
    panel_next = 0
    zoom_at, was_battle = 0, False
    bumps, mm_snap_next = 0, 0
    boss_cap = {"games": 0, "next": 0.0, "game": None}
    win_next = time.time() + 10

    try:
        with _new_mss() as sct:
            while True:
                if hot.hit("F9") or CONTROL["stop"]:
                    log("停止")
                    break
                if time.time() >= CONTROL.get("lic_next", 0):   # 每 10 分鐘確認一次授權
                    CONTROL["lic_next"] = time.time() + 600
                    _lic = licensing.check()
                    if not _lic["ok"]:
                        log(_lic["msg"] + " → 停止")
                        MOVER.set(None, False)
                        break
                if hot.hit("F8") or CONTROL["toggle_pause"]:
                    CONTROL["toggle_pause"] = False
                    paused = not paused
                    CONTROL["paused"] = paused
                    watcher.paused = paused or GAME.get("rest_until") is not None
                    log("暫停" if paused else "繼續")
                if use_cdp:
                    page.capture_paused = paused or GAME.get("rest_until") is not None
                t_ = time.time()      # 統計用：實際在跑的時間（暫停、休息不算）
                if not paused and GAME.get("rest_until") is None:
                    STATS["active"] += min(30.0, max(0.0, t_ - STATS["last"]))
                STATS["last"] = t_
                if paused:
                    MOVER.set(None, False)
                    time.sleep(0.1)
                    continue

                if use_cdp:
                    if not page.alive:
                        log("遊戲用的 Edge 被關掉了，停止")
                        break
                    if bcfg.get("lock_window_size", True) and time.time() >= win_next:
                        win_next = time.time() + 10  # 每 10 秒檢查一次視窗大小／有沒有被縮小
                        try:
                            ws = bcfg.get("window_size", [1280, 960])
                            did = page.ensure_window(ws[0], ws[1])
                            if did:
                                log(f"遊戲視窗{did}")
                        except Exception:
                            pass
                    f = page.latest()
                    if f is None:
                        time.sleep(0.3)
                        continue
                    rect = (0, 0, f.shape[1], f.shape[0])
                elif hwnd:
                    if not is_foreground(hwnd):
                        MOVER.set(None, False)
                        if not unfocused_logged:
                            log("遊戲視窗不在最前面，等待中…")
                            unfocused_logged = True
                        time.sleep(0.3)
                        continue
                    unfocused_logged = False
                    rect = client_rect(hwnd)
                else:
                    mon = sct.monitors[1]
                    rect = (mon["left"], mon["top"], mon["width"], mon["height"])
                if rect[2] < 100 or rect[3] < 100:
                    time.sleep(0.5)
                    continue
                # 切掉瀏覽器工具列，只留遊戲畫面（背景模式本來就只有網頁畫面）
                if not use_cdp and time.time() >= vp_next:
                    vp_next = time.time() + 3
                    try:
                        VIEW.update(grab(sct, rect))
                    except Exception:
                        pass
                rect = VIEW.apply(rect)
                watcher.set_rect(rect)
                now = time.time()
                res = watcher.get()

                # ---- 選單 ----
                if res["kind"] and res["t"] > menu_done_t:
                    if cfg[res["kind"]]["enabled"]:
                        process_menus(res["kind"], sct, rect, cfg, inp, menu_st, dry)
                        if GAME.get("phase") == "battle":
                            park_mouse(rect)  # 滑鼠不要停在技能欄上
                    menu_done_t = time.time()
                    if res["kind"] in ("result", "lobby", "afk", "login", "charselect"):
                        with watcher.lock:
                            watcher.res["ingame_t"] = 0  # 已離開戰鬥
                    continue
                if not res["kind"] and res["t"] > menu_done_t:
                    menu_st.update(rerolls=0, banished=0, card_rerolls=0, card_banishes=0, chest_last=None)

                # ---- 升級按鈕 ----
                lvc = cfg["levelup"]
                if GAME.get("batch_target") is None:
                    lo = int(lvc.get("batch_min", 1))
                    GAME["batch_target"] = random.randint(lo, max(lo, int(lvc.get("batch_max", lo))))
                n_up = res["hud"]
                if n_up >= 10:  # 「升級 1 B」常被讀成 10、「2 B」讀成 20、「3B」讀成 38
                    n_up = int(str(n_up)[0])
                # 計時器：0 → 14 分鐘往上數；打完最後 Boss 才開始倒數 20 秒。
                # 這局曾經數到 5 分鐘以上、後來又變成很小的數字 = 進入最後倒數 → 不再升級
                tmr = res.get("timer")
                if tmr is not None and res["t"] > GAME.get("timer_t", 0):
                    GAME["timer_t"] = res["t"]
                    GAME["max_timer"] = max(GAME.get("max_timer", 0), tmr)
                ending = tmr is not None and GAME.get("max_timer", 0) >= 300 \
                    and tmr <= lvc.get("stop_before_end_sec", 25) \
                    and now - GAME.get("bar_t", 0) > 4  # 頭目還在（頭目戰的 2 分鐘倒數）不算
                if ending and not GAME.get("ending_logged"):
                    log("Boss 已打完、進入最後倒數，不再升級")
                    GAME["ending_logged"] = True
                ready = n_up >= GAME["batch_target"] and not ending
                # 等級達到設定值之後不再升級
                max_lv = int(lvc.get("max_level", 0) or 0)
                cur_lv = res.get("char_lv")
                if max_lv and (cur_lv is None or now - res.get("char_lv_t", 0) > 10):
                    ready = False  # 有設定等級上限：要先讀到目前等級才決定
                elif max_lv and cur_lv >= max_lv:
                    if ready and not GAME.get("maxlv_logged"):
                        log(f"角色已經 Lv {cur_lv}，達到設定的 Lv {max_lv}，不再升級")
                        GAME["maxlv_logged"] = True
                    ready = False
                if lvc["enabled"] and ready and res["t"] > last_up_press \
                        and now - last_up_press > 2.5:
                    log(f"累積 {n_up} 次升級 → 按 {lvc['hotkey'].upper()} 一次選完")
                    GAME["batch_target"] = None  # 下一輪重新決定要累積幾次
                    MOVER.set(None, False)
                    try:
                        before_b = grab(sct, rect)
                    except Exception:
                        before_b = None
                    inp.press(cfg["levelup"]["hotkey"])
                    last_up_press = time.time()
                    if not dry:
                        if before_b is not None:
                            wait_screen_change(sct, rect, before_b)  # 等選單真的打開
                        process_menus("levelup", sct, rect, cfg, inp, menu_st, dry)
                        if GAME.get("phase") == "battle":
                            park_mouse(rect)  # 滑鼠不要停在技能欄上
                        menu_done_t = time.time()
                        continue

                # ---- 目前階段：戰鬥 / 城鎮 ----
                if now - res["ingame_t"] < 6:
                    GAME["game_counted"] = False
                    if GAME["phase"] != "battle":
                        GAME["phase"] = "battle"
                    GAME["battle_seen"] = max(GAME["battle_seen"], res["ingame_t"])
                if GAME["phase"] == "battle" and now - GAME["battle_seen"] > 45:
                    log("很久沒看到戰鬥畫面，改回城鎮模式")
                    GAME["phase"] = "town"
                in_battle = GAME["phase"] == "battle"
                # 每一場剛進戰鬥：記下時間，稍後用滾輪把視野拉大
                if in_battle and not was_battle:
                    zoom_at = now + 2.0
                    last_coord, coord_same_since = None, now
                    bumps = 0
                    battle_t0 = now
                    GAME.update(bell_ep=None, bells_done=[], bell_count=0, mvp_logged=False, last_hp=None,
                                kite_why=None, boss_hp0=None, boss_seen=[], hp_hist=[], hp_drop_until=0,
                                crowd_until=0, battle_t0=now, boss_log=[], boss_cur=None, hp_min_pct=None,
                                kite_hist=[], char_lv=None)
                    GAME.pop("crowd_since", None)
                    if dodger:
                        dodger.stats['dodge'] = 0
                    with watcher.lock:              # 上一場的地圖名稱不要帶到這一場
                        watcher.res.pop("map_name", None)
                        watcher.last_map = None
                    if mmap:
                        mmap.new_game()
                if was_battle and not in_battle:
                    if mmap:
                        mmap.save()
                        st_ = mmap.stats
                        log(f"這局撞到東西 {bumps} 次；小地圖幫忙轉彎 {st_['steer']} 次、"
                            f"繞路 {st_['reroute']} 次、跳過走不到的東西 {st_['unreach']} 次、"
                            f"帶去撿寶箱／磁鐵 {st_['goals']} 次")
                    else:
                        log(f"這局撞到東西 {bumps} 次")
                    done_ = bool(GAME.get("game_counted")) and now - battle_t0 > 60
                    STATS["games"].append({"dur": now - battle_t0, "bumps": bumps, "done": done_})
                    if done_:
                        log("統計：" + stats_text())
                was_battle = in_battle

                # ---- 休息排程（只在城鎮、一局結束後）----
                sch = cfg.get("schedule", {})
                if sch.get("enabled") and not in_battle:
                    stop_n = int(sch.get("stop_after_games", 0) or 0)
                    if stop_n and GAME.get("games_done", 0) >= stop_n:
                        log(f"已經玩了 {GAME['games_done']} 場，達到設定的場數，自動停止")
                        break
                    if GAME.get("rest_target") is None:
                        lo = max(1, int(sch.get("games_min", 3)))
                        GAME["rest_target"] = random.randint(lo, max(lo, int(sch.get("games_max", lo))))
                    if GAME.get("rest_until") is None and \
                            GAME.get("games_since_rest", 0) >= GAME["rest_target"]:
                        mins = random.uniform(float(sch.get("rest_min_minutes", 10)),
                                              max(float(sch.get("rest_min_minutes", 10)),
                                                  float(sch.get("rest_max_minutes", 20))))
                        GAME["rest_until"] = now + mins * 60
                        dur = f"{mins:.0f} 分鐘" if mins >= 1 else f"{mins * 60:.0f} 秒"
                        log(f"已連續玩 {GAME['games_since_rest']} 場 → 休息 {dur}"
                            f"（到 {time.strftime('%H:%M', time.localtime(GAME['rest_until']))}）")
                    if GAME.get("rest_until") is not None:
                        MOVER.set(None, False)
                        watcher.paused = True
                        left = GAME["rest_until"] - now
                        if left > 0:
                            CONTROL["status"] = f"休息中，剩 {int(left // 60)}:{int(left % 60):02d}"
                            time.sleep(0.5)
                            continue
                        if use_cdp:
                            page.capture_paused = False
                        log("休息結束，繼續玩")
                        watcher.paused = False
                        CONTROL["status"] = ""
                        GAME.update(rest_until=None, rest_target=None, games_since_rest=0)
                        st_town.clear()
                        continue

                # ---- 定時重開遊戲用的 Edge（掛久了瀏覽器會越來越慢）：一局剛打完、下一局還沒開始 ----
                rh_ = float(bcfg.get("restart_hours", 6) or 0)
                if use_cdp and not dry and rh_ > 0 and not in_battle and GAME.get("game_counted") \
                        and now - edge_t0 >= rh_ * 3600 and GAME.get("rest_until") is None:
                    if not (cfg.get("login", {}).get("enabled") and cfg.get("charselect", {}).get("enabled")):
                        if not GAME.get("restart_skip_logged"):
                            GAME["restart_skip_logged"] = True
                            log("該定時重開 Edge 了，但沒有開「自動登入」（重開後要按開始遊戲、選角色）→ 先不重開")
                    else:
                        MOVER.set(None, False)
                        watcher.paused = True
                        time.sleep(0.5)
                        new_page = restart_edge(bcfg, page)
                        if new_page is None:
                            raise RuntimeError("定時重開 Edge 失敗")   # 交給自動重新開始處理
                        page = new_page
                        IO = CdpIO(page)
                        edge_t0 = time.time()
                        watcher.paused = False
                        st_town.clear()
                        GAME.update(phase=None, last_sig=None)
                        menu_done_t = time.time()      # 重開前讀到的畫面不算
                        continue

                # ---- 不在戰鬥中：城鎮流程 ----
                if not in_battle:
                    if cfg["town"]["enabled"] and res["t"] > 0:
                        frame = grab(sct, rect)
                        td = town_step(frame, rect, cfg, inp, st_town)
                        W_, H_ = rect[2], rect[3]
                        MOVER.set(td, td is not None, (rect[0] + W_ / 2, rect[1] + H_ / 2))
                        # 記下「開始連續走路」的時間（卡住偵測要走一陣子才判斷）
                        if td is None:
                            st_town["move_since"], st_town["hist"] = None, []
                        elif st_town.get("move_since") is None:
                            st_town["move_since"] = time.time()
                        time.sleep(0.03)
                    else:
                        MOVER.set(None, False)
                        time.sleep(0.2)
                    continue
                st_town.clear()
                if not mcfg["enabled"]:
                    bst["prev"] = None
                    MOVER.set(None, False)
                    time.sleep(0.3)
                    continue

                frame = grab(sct, rect)
                H, W = frame.shape[:2]
                ox, oy = rect[0], rect[1]
                # 視野拉大（滑鼠滾輪往上）
                if zoom_at and now >= zoom_at:
                    zoom_at = 0
                    zn = int(mcfg.get("zoom_out", 10) or 0)
                    if zn != 0:
                        log(f"拉大視野（滾輪 {zn} 格）")
                        MOVER.set(None, False)
                        # 方向設定：zoom_out 正數 = 拉大視野（若遊戲改版方向相反，改成負數即可）
                        inp.scroll(ox + W * 0.75, oy + H * 0.45, zn, "拉大視野")
                        park_mouse(rect)
                        continue
                # 誤點技能欄跳出的說明框 → 關掉
                if now >= panel_next:
                    panel_next = now + 1.0
                    if not dry and skill_panel_open(frame):
                        MOVER.set(None, False)
                        close_skill_panel(rect, inp)
                        time.sleep(0.4)
                        continue
                lc = (W / 2 + mcfg["character_offset"][0], H / 2 + mcfg["character_offset"][1])
                center = (ox + lc[0], oy + lc[1])
                # 頭目戰截圖：之後拿來分析頭目的招式、做閃避策略
                ring_push(frame, now)
                if dodger is not None and now >= GAME.get("bar_next", 0):
                    GAME["bar_next"] = now + 0.5
                    try:
                        if _boss.boss_bar(frame):
                            GAME["bar_t"] = now
                    except Exception:
                        pass
                # 頭目在不在：只看畫面上方的頭目血條（文字辨識的「頭目」「MVP」常常誤讀，只拿來讀名字）
                mvp_on = now - GAME.get("bar_t", 0) < 6.0
                if res.get("hp") is not None and now - res.get("hp_t", 0) < 5:
                    hp_now = (res["hp"], res["hp_max"])
                    if hp_now != GAME.get("last_hp"):
                        hh_ = GAME.setdefault("hp_hist", [])
                        hh_.append((now, hp_now[0], hp_now[1]))
                        del hh_[:-30]
                    GAME["last_hp"] = hp_now
                    report_hp(hp_now)
                if res.get("char_lv") is not None and res.get("char_lv_t", 0) > GAME.get("battle_t0", 0):
                    GAME["char_lv"] = res["char_lv"]
                if mvp_on and not GAME.get("mvp_logged"):
                    GAME["mvp_logged"] = True
                    nm_ = res.get("mvp_name") or ""
                    t_ = GAME.get("max_timer", 0)
                    hp_ = GAME.get("last_hp")
                    GAME["boss_hp0"] = hp_
                    GAME.setdefault("boss_seen", []).append(t_)
                    report_boss_start(nm_, t_, hp_)
                    log(f"頭目 {nm_} 出現（計時 {t_ // 60}:{t_ % 60:02d}"
                        + (f"，HP {hp_[0]}/{hp_[1]}" if hp_ else "") + "）"
                        + ("→ 開始閃避地上的紅色預警範圍" if dodger else ""))
                elif not mvp_on and GAME.get("mvp_logged"):
                    # 頭目打完（血條消失）→ 記一下這場頭目戰掉了多少血，下一隻頭目再重新記
                    GAME["mvp_logged"] = False
                    GAME["boss_cur"], GAME["boss_end_t"] = None, now
                    h0, h1 = GAME.get("boss_hp0"), GAME.get("last_hp")
                    if h1:
                        log(f"頭目戰結束：HP {h1[0]}/{h1[1]}"
                            + (f"（頭目出現時 {h0[0]}/{h0[1]}）" if h0 else ""))
                bc_ = GAME.get("boss_cur")
                if mvp_on and bc_ is not None and not bc_.get("name") and res.get("mvp_name"):
                    bc_["name"] = res["mvp_name"]       # 頭目名字晚一點才讀到 → 補上（報表用）
                if mcfg.get("boss_capture_games", 3) and (mvp_on or GAME.get("max_timer", 0) >=
                        mcfg.get("boss_capture_from", 830)) and not GAME.get("ending_logged") \
                        and now >= boss_cap["next"]:
                    gid = GAME.get("games_done", 0)
                    if boss_cap["game"] != gid and boss_cap["games"] < mcfg.get("boss_capture_games", 3):
                        boss_cap["game"], boss_cap["games"] = gid, boss_cap["games"] + 1
                        log(f"頭目戰開始存截圖（logs\\boss，這次啟動第 {boss_cap['games']} 場）")
                    if boss_cap["game"] == gid:
                        boss_cap["next"] = now + 1.0
                        save_boss_shot(frame, gid + 1)
                # 鏡頭位移 → 角色在地圖上的位置（用來記住撞過的地方、鐘的位置）
                moved = None
                if bst.get("prev") is not None and bst["prev"].shape == frame.shape:
                    sdx, sdy, sresp = cam_shift(bst["prev"], frame)
                    if sresp > 0.05 and abs(sdx) < W * 0.3 and abs(sdy) < H * 0.3:
                        moved = math.hypot(sdx, sdy)
                        bst["world"] = (bst["world"][0] - sdx, bst["world"][1] - sdy)
                bst["prev"] = frame
                wx, wy = bst["world"]
                mm_ok = False
                if mmap:
                    # 地圖用「刷哪一關」的設定來分（讀小地圖標題的名字不一定讀得到）
                    tcf_ = cfg["town"]
                    mkey = tcf_.get("stage") if town_mode(tcf_) == "expedition" and tcf_.get("stage") \
                        else "節慶主持人"
                    mmap.update(frame, now, bst["world"], mkey)
                    mm_ok = not dry and mmap.ready(now)
                # 頭目快出現（例如斐揚洞穴 13:40 之後）：停在原地，等鐘出現再躲；頭目死後倒數也不動
                tcf = cfg["town"]
                hold = (tcf.get("boss_hold") or {}).get(tcf.get("stage")) \
                    if town_mode(tcf) == "expedition" else None
                hold_on = bool(hold and GAME.get("max_timer", 0) >= int(hold))
                # 頭目機制「躲到鐘下！」：找鐘、走到鐘底下，到了就站著不動
                bs = bell_step(frame, lc, bst["world"], now, now - res.get("bell_t", 0) < 3.0, hold_on)
                if bs is not None:
                    if bs[0] == "move":
                        bd_ = bs[1]
                        if mm_ok:       # 小地圖：路上有石頭、水就繞一下
                            bd_ = mmap.steer(bd_, need=bs[2] / (mmap.scale or _mmod.DEFAULT_SCALE))
                        MOVER.set(bd_, True, center)
                    else:
                        MOVER.set(None, False)
                    time.sleep(0.05)
                    continue
                # 頭目（MVP）在場：地上的紅色預警範圍 → 站在裡面就往最近的安全處走
                dodge_on = dodger is not None and not dry and (mvp_on or bool(GAME.get("kite_why")))
                if not dodge_on and dodger is not None and not dry and mcfg.get("crowd_kite", True) \
                        and now >= GAME.get("crowd_next", 0):
                    # 平常也看一下周圍怪物多不多（每 0.15 秒一次）
                    GAME["crowd_next"] = now + 0.15
                    dodger.update(frame, now, bst["world"], lc)
                if dodge_on:
                    dodger.update(frame, now, bst["world"], lc)
                    wall_ok = None
                    if mm_ok:
                        wall_ok = lambda v, dist_: mmap.free_dist(v, min(_mmod.LOOK, dist_ / (
                            mmap.scale or _mmod.DEFAULT_SCALE) + 1)) >= min(_mmod.LOOK, dist_ / (
                            mmap.scale or _mmod.DEFAULT_SCALE)) - 0.01
                    esc_ = dodger.escape(lc, now, wall_ok=wall_ok, prefer=d)
                    if esc_ is not None:
                        d = esc_[0]
                        if dodger.stats["dodge"] == 1 or now >= GAME.get("dodge_log_next", 0):
                            GAME["dodge_log_next"] = now + 10
                            log(f"踩到頭目的紅色預警範圍 → 往安全的地方躲（約 {esc_[1]} 像素）")
                        if boss_cap["game"] == GAME.get("games_done", 0) and now >= GAME.get("dodge_img_next", 0):
                            GAME["dodge_img_next"] = now + 1.0
                            try:
                                save_boss_shot(dodger.debug_image(frame, lc, d), GAME.get("games_done", 0) + 1,
                                               "dodge")
                            except Exception:
                                pass
                        MOVER.set(d, True, center)
                        time.sleep(0.03)
                        continue
                # 頭目戰走位：頭目在場、頭目快出現（3/5/7/10/12/14 分前幾秒）、HP 太低 → 不追東西，繞圈移動
                kite_why = None
                if dodger is not None and not dry and mcfg.get("boss_kite", True) and not hold_on:
                    mt_ = GAME.get("max_timer", 0)
                    prep_ = mcfg.get("boss_prep_sec", 8)
                    hp_ = GAME.get("last_hp")
                    hp_low_ = bool(hp_ and hp_[1] and hp_[0] < hp_[1] * mcfg.get("boss_kite_hp", 0.85))
                    if mvp_on and mt_ >= mcfg.get("boss_capture_from", 830):
                        kite_why = "MVP 在場"
                    elif mvp_on and (hp_low_ or GAME.get("kite_why") == "頭目在場、在掉血"):
                        # 一般頭目：滿血時照常撿東西（只躲紅色預警）；開始掉血才不撿、繞圈躲
                        kite_why = "頭目在場、在掉血"
                    elif not mvp_on and any(t_ - prep_ <= mt_ < t_ + 5 and
                                            not any(t_ - prep_ <= b_ < t_ + 60 for b_ in GAME.get("boss_seen", []))
                                            for t_ in mcfg.get("boss_times", [])):
                        kite_why = "頭目快出現了"
                    elif mcfg.get("crowd_kite", True) and now - res.get("swarm_t", 0) < 20:
                        kite_why = "一大群怪物包圍"
                    elif mcfg.get("crowd_kite", True) and hp_dropping(now, mcfg) or \
                            (GAME.get("kite_why") == "HP 掉很快" and now < GAME.get("hp_drop_until", 0)):
                        kite_why = "HP 掉很快"
                    elif mcfg.get("crowd_kite", True) and crowded(now, dodger, hp_, mcfg):
                        kite_why = "被怪物圍住"
                    elif hp_ and hp_[1] and mcfg.get("low_hp_kite", 0.4) and \
                            (hp_[0] / hp_[1] < mcfg.get("low_hp_kite", 0.4) or
                             (GAME.get("kite_why") == "HP 太低" and hp_[0] / hp_[1] < 0.7)):
                        kite_why = "HP 太低"
                if kite_why != GAME.get("kite_why"):
                    if kite_why:
                        kh_ = GAME.setdefault("kite_hist", [])
                        kh_.append((kite_why, now))
                        del kh_[:-20]
                        hp_ = GAME.get("last_hp")
                        log(f"{kite_why} → 不追遠處的東西，往怪少、空曠的地方繞圈移動"
                            + ("、躲紅色預警" if dodge_on else "")
                            + (f"（HP {hp_[0]}/{hp_[1]}）" if hp_ else ""))
                    elif GAME.get("kite_why"):
                        log("躲避走位結束 → 回到平常撿東西")
                        dodger.reset_kite()
                    GAME["kite_why"] = kite_why
                if kite_why:
                    free_ = None
                    if mm_ok:
                        free_ = lambda v: mmap.free_dist(v, _mmod.LOOK) / _mmod.LOOK
                    kd_ = dodger.kite(lc, now, d, bst["world"], free_)
                    if kd_ is not None:
                        d = blend(d, kd_, 0.6)
                        MOVER.set(d, True, center)
                        time.sleep(0.03)
                        continue
                if hold_on:
                    if not GAME.get("hold_logged"):
                        m_, s_ = divmod(int(hold), 60)
                        log(f"計時過了 {m_}:{s_:02d}，頭目快出現 → 停在原地，只在要躲鐘時移動")
                        GAME["hold_logged"] = True
                    MOVER.set(None, False)
                    time.sleep(0.1)
                    continue
                coins, ores, chests = vision.detect(frame)
                water = vision.last_water
                if mm_ok:
                    mmap.add_items(coins + ores + chests, lc, now)
                bst["blocked"] = [b for b in bst["blocked"] if now - b[2] < 40]
                bst["avoid"] = [a for a in bst["avoid"] if now - a[2] < 20]
                blocked_scr = [(bx - wx + lc[0], by - wy + lc[1]) for bx, by, _ in bst["blocked"]]
                avoid_scr = [(ax - wx + lc[0], ay - wy + lc[1]) for ax, ay, _ in bst["avoid"]]

                gdir = None
                if now >= escape_until:
                    tgt = planner.plan(coins, ores, chests, lc[0], lc[1], now, avoid=avoid_scr)
                    # 小地圖：畫面上沒東西撿 → 往小地圖上的磁鐵／寶箱走；附近有磁鐵 → 先撿磁鐵
                    if mm_ok and mcfg.get("minimap_goals", True):
                        if tgt is None and planner.target is None:
                            gdir = mmap.goal_dir(now)
                        elif mcfg.get("magnet_first", True) and planner.mode != "chest" and mmap.magnet_near():
                            gdir = mmap.goal_dir(now, only_magnet=True)
                    if gdir is not None:
                        d = blend(d, gdir, 0.75)
                    elif tgt is not None:
                        dx, dy = tgt[0] - lc[0], tgt[1] - lc[1]
                        dist = math.hypot(dx, dy) or 1
                        step = (dx / dist, dy / dist)
                        # 有水池或撞過的地方 → 規劃繞路
                        if (water is not None and water.any()) or blocked_scr:
                            if now >= bst["path_next"] or bst["path_tgt"] is None or \
                                    math.hypot(tgt[0] - bst["path_tgt"][0],
                                               tgt[1] - bst["path_tgt"][1]) > 40:
                                bst["path_next"] = now + 0.3
                                bst["path_tgt"] = tgt
                                path = plan_path(frame, tgt, start=lc, blocked=blocked_scr,
                                                 clearance=5, obstacles=water)
                                if path is None:
                                    # 走不到（在水池另一邊之類）→ 這附近先不追
                                    bst["avoid"].append((tgt[0] - lc[0] + wx, tgt[1] - lc[1] + wy, now))
                                    planner.target, planner.repick_at = None, 0
                                    bst["path_dir"] = None
                                elif len(path) > 2:
                                    px_, py_ = path[min(len(path) - 1, 5)]
                                    pd = math.hypot(px_ - lc[0], py_ - lc[1]) or 1
                                    bst["path_dir"] = ((px_ - lc[0]) / pd, (py_ - lc[1]) / pd)
                                else:
                                    bst["path_dir"] = None
                            if bst.get("path_dir"):
                                step = bst["path_dir"]
                        # 小地圖：中間有牆就繞路；完全走不到（在牆的另一邊）就先不追
                        if mm_ok:
                            tdir = (dx / dist, dy / dist)
                            st2 = mmap.route(tdir, dist, now)
                            if st2 is None:
                                bst["avoid"].append((tgt[0] - lc[0] + wx, tgt[1] - lc[1] + wy, now))
                                planner.target, planner.repick_at = None, 0
                            elif st2 != tdir:
                                step = st2          # 小地圖說要繞路 → 照小地圖走
                        d = blend(d, step, 0.75)
                    elif planner.target is None:
                        # 小地圖：前面快撞牆了 → 提早換方向
                        if mm_ok and now < wander_next and \
                                mmap.free_dist(_mmod.key_dir(wander) or wander, _mmod.LOOK) < _mmod.LOOK - 0.01:
                            wander_next = now
                        if now >= wander_next or d is None:
                            ed = mmap.explore_dir(d) if mm_ok else None
                            # 小地圖：往空曠、還沒去過的地方走；沒有小地圖就隨便轉個方向
                            wander = ed or rotate(wander if d is None else d, random.uniform(-110, 110))
                            wander_next = now + random.uniform(2, 5)
                        # 隨意走的方向前方是水 → 換個方向
                        if water is not None and water.any():
                            fx = int((lc[0] + wander[0] * 60) // 8)
                            fy = int((lc[1] + wander[1] * 60) // 8)
                            if 0 <= fy < water.shape[0] and 0 <= fx < water.shape[1] and water[fy, fx]:
                                wander = rotate(wander, random.choice([-1, 1]) * random.uniform(90, 180))
                        d = blend(d, wander, 0.5)

                # ---- 卡住偵測（看畫面有沒有在動，約 1 秒就能發現）----
                if not dry and d is not None and moved is not None and now >= escape_until:
                    bst["hist"].append((now, (wx, wy)))
                    bst["hist"] = [h_ for h_ in bst["hist"] if now - h_[0] <= 1.2]
                    # 戰鬥畫面中間有自己的技能特效、怪物圍著，畫面比對常誤判「沒在動」
                    # → 還要小地圖座標也一段時間沒變才算卡住
                    coord_ok = res.get("coord") is not None and now - res.get("coord_t", 0) < 2.0 \
                        and res["coord"] == last_coord and now - coord_same_since >= 1.1
                    if coord_ok and len(bst["hist"]) >= 4 and now - bst["hist"][0][0] > 0.9 and \
                            math.hypot(wx - bst["hist"][0][1][0], wy - bst["hist"][0][1][1]) < 10:
                        for k in (20, 36):
                            bst["blocked"].append((wx + d[0] * k, wy + d[1] * k, now))
                        if planner.target is not None:
                            t_ = planner.target
                            bst["avoid"].append((t_[0] - lc[0] + wx, t_[1] - lc[1] + wy, now))
                        esc = None
                        if mm_ok:
                            mmap.mark_block(d, now)          # 小地圖上記住這裡、也記住牆的顏色
                            esc = mmap.escape_dir(d)
                        d = esc or rotate(d, random.choice([-1, 1]) * random.uniform(100, 150))
                        bumps += 1
                        escape_until = now + random.uniform(0.7, 1.2)
                        planner.target, planner.repick_at = None, 0
                        planner.ignore_chest_until = now + 3
                        bst["hist"], bst["path_tgt"] = [], None
                        log("撞到東西了（畫面沒在動）→ 記住這裡、換方向")

                # ---- 卡住偵測（小地圖座標）----
                if mcfg["stuck_check"] and not dry and res.get("coord") and res["coord_t"] > last_coord_t:
                    last_coord_t = res["coord_t"]
                    if res["coord"] != last_coord:
                        last_coord, coord_same_since = res["coord"], now
                    elif now - coord_same_since > 2.5 and now >= escape_until:
                        esc = None
                        if mm_ok:
                            mmap.mark_block(d, now)
                            esc = mmap.escape_dir(d)
                        d = esc or rotate(d or (1, 0), random.choice([-1, 1]) * random.uniform(100, 170))
                        bumps += 1
                        escape_until = now + random.uniform(1.0, 1.8)
                        coord_same_since = now
                        planner.ignore_chest_until = now + 4
                        planner.target, planner.repick_at = None, 0
                        log(f"座標 {last_coord} 沒變，可能卡住 → 換方向")

                # 小地圖：最後再確認一次前面不是牆（目標很近就不用管後面的牆）
                if mm_ok and d is not None and not (gdir is not None and mmap.goal_close()):
                    need = None
                    if planner.target is not None:
                        need = math.hypot(planner.target[0] - lc[0], planner.target[1] - lc[1]) / \
                            (mmap.scale or _mmod.DEFAULT_SCALE)
                    d = mmap.steer(d, need)
                if dodge_on:        # 頭目在場：要走的方向會踩進紅色預警 → 轉個方向
                    d = dodger.veto(d, lc)
                MOVER.set(d, True, center)

                if cfg["debug_snapshots"] and mmap and now >= mm_snap_next:
                    mm_snap_next = now + 8
                    try:
                        img_ = mmap.debug_image(d)
                        if img_ is not None:
                            save_image(img_, "minimap")
                    except Exception as e:      # 除錯圖出錯不要讓整個程式停掉
                        log(f"小地圖除錯圖存檔失敗（不影響掛機）：{e}")
                if cfg["debug_snapshots"] and now >= snap_next:
                    snap_next = now + 4
                    save_snapshot(frame, "move", coins, chests, lc, d, ores=ores,
                                  target=planner.target)
                    log(f"金幣 {len(coins)}、礦石 {len(ores)}、寶箱 {len(chests)}")

                time.sleep(random.uniform(0.02, 0.05))
    finally:
        watcher.stop_flag = True
        MOVER.stop_flag = True
        time.sleep(0.15)
        try:
            MOVER.release()
        except Exception:
            pass
        if page is not None:
            time.sleep(0.3)
            page.close()  # 只斷開連線，遊戲用的 Edge 留著
        IO = ScreenIO()


def main():
    try:
        if pyautogui is None or mss is None:
            log("注意：缺少 pyautogui / mss，只能使用背景模式")
        cfg = load_config()
        if cfg:
            run(cfg)
    except FailSafe:
        log("已觸發緊急停止（滑鼠在左上角）")
    except KeyboardInterrupt:
        log("已手動停止")
    except Exception:
        tb = traceback.format_exc()
        log("發生錯誤：\n" + tb)
        try:
            with open(os.path.join(LOG_DIR, "error.txt"), "w", encoding="utf-8") as f:
                f.write(tb)
        except Exception:
            pass
    finally:
        if MOVER:
            MOVER.stop_flag = True
            time.sleep(0.2)
            try:
                MOVER.release()
            except Exception:
                pass
        try:
            input("\n按 Enter 關閉視窗…")
        except Exception:
            pass


if __name__ == "__main__":
    main()
