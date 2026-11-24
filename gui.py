"""
掛機小幫手 — 視窗版（支援 RO Survivors 網頁版）
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
import tkinter as tk
from tkinter import ttk, messagebox

import ro_bot as rb

DISCORD_URL = "https://discord.gg/HsUFaePnXF"   # Discord 社群邀請連結
import updater

# 沒有線上更新的版本：程式資料夾裡有 no_update.flag 這個檔案
NO_UPDATE = os.path.exists(os.path.join(rb.BASE_DIR, "no_update.flag"))
import licensing

APP_TITLE = "掛機小幫手"
DEITY_NONE = "不選（自己選）"
CHEST_EFFECT = dict(getattr(rb, "CHEST_ITEMS", []))
CHEST_CAT = dict(getattr(rb, "CHEST_CATEGORY", {}))
SKILLS = ["火箭術", "冰箭術", "雷擊術", "聖靈召喚", "心靈爆破",
          "火焰之壁", "冰凍術", "火狩", "雷爆術", "攻擊：屬性附加"]


def split_list(text):
    for sep in ("，", "、", "\n", ";", "；"):
        text = text.replace(sep, ",")
    return [t.strip() for t in text.split(",") if t.strip()]


class App:
    def __init__(self, root):
        self.root = root
        root.title(f"{APP_TITLE}  v{rb.VERSION}")
        root.geometry("680x940")
        root.minsize(560, 600)
        self.q = queue.Queue()
        self.thread = None
        rb.LOG_HOOKS.append(self.q.put)

        self.cfg = self.load_cfg()
        self.cfg_mtime = self._cfg_mtime()
        self.build()
        self.fill_from_cfg()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self.poll)
        if not NO_UPDATE:
            root.after(2500, lambda: self.check_update(quiet=True))
        root.after(1500, self.verify_update)
        root.after(300, self.startup_license)

    # ---------------- 設定 ----------------
    def load_cfg(self):
        if os.path.exists(rb.CONFIG_PATH):
            try:
                with open(rb.CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                    raw = json.load(f)
                return rb.migrate_config(rb.deep_merge(rb.DEFAULT_CONFIG, raw),
                                         raw.get("config_version", 1))
            except Exception as e:
                messagebox.showwarning(APP_TITLE, f"設定檔讀取失敗，改用預設值：\n{e}")
        return json.loads(json.dumps(rb.DEFAULT_CONFIG))

    def clear_route(self):
        rb.clear_route()
        self.add_log("已清除記住的城鎮路線，下次會重新規劃並記住新的")

    def _cfg_mtime(self):
        try:
            return os.path.getmtime(rb.CONFIG_PATH)
        except OSError:
            return 0

    def reload_if_edited(self):
        """設定檔在程式外被改過（例如用記事本改）→ 先讀回來、更新畫面，再存，才不會把它蓋掉"""
        mt = self._cfg_mtime()
        if mt and getattr(self, "cfg_mtime", None) is not None and abs(mt - self.cfg_mtime) > 0.01:
            try:
                self.cfg = self.load_cfg()
                self.fill_from_cfg()
                self.add_log("偵測到設定檔被修改過 → 已重新讀取")
            except Exception as e:
                self.add_log(f"設定檔讀取失敗（請檢查格式）：{e}")
                return False
        return True

    def save_cfg(self, quiet=False):
        if not self.reload_if_edited():
            return
        c = self.cfg
        c["window_title"] = self.v_title.get().strip()
        c.setdefault("browser", {})["mode"] = self.v_mode.get() or "cdp"
        c["browser"]["lock_window_size"] = self.v_lockwin.get()
        try:
            c["browser"]["restart_hours"] = max(0.0, float(self.v_restart.get() or 0))
        except ValueError:
            pass
        c["dry_run"] = self.v_dry.get()
        c["levelup"]["enabled"] = self.v_lv.get()
        # 勾選的技能（主力依清單順序）＋設定檔裡清單以外的名稱照舊保留
        old_pri = [x for x in c["levelup"].get("priority", []) if x not in SKILLS]
        old_ban = [x for x in c["levelup"].get("banish", []) if x not in SKILLS]
        c["levelup"]["priority"] = [n for n in SKILLS if self.v_main[n].get()] + old_pri
        c["levelup"]["banish"] = [n for n in SKILLS if self.v_ban[n].get()] + old_ban
        try:
            c["levelup"]["target_until_level"] = int(self.v_until.get())
        except ValueError:
            pass
        c["levelup"]["rarity_first"] = self.v_rarity.get()
        dv = self.v_deity.get()
        c.setdefault("deity", {})["enabled"] = dv != DEITY_NONE
        if dv in rb.DEITY_CHOICES:
            c["deity"]["choice"] = dv
        c["levelup"]["auto_after_banish"] = self.v_autopick.get()
        c["chest"]["auto"] = self.v_chest_auto.get()
        try:
            lo, hi = int(self.v_bmin.get()), int(self.v_bmax.get())
            c["levelup"]["batch_min"], c["levelup"]["batch_max"] = min(lo, hi), max(lo, hi)
        except ValueError:
            pass
        try:
            c["levelup"]["max_level"] = max(0, int(float(self.v_maxlv.get() or 0)))
        except ValueError:
            pass
        sch = c.setdefault("schedule", {})
        sch["enabled"] = self.v_sch.get()
        for key, var, lo in (("games_min", self.v_gmin, 1), ("games_max", self.v_gmax, 1),
                             ("rest_min_minutes", self.v_rmin, 1),
                             ("rest_max_minutes", self.v_rmax, 1),
                             ("stop_after_games", self.v_stopn, 0)):
            try:
                sch[key] = max(lo, int(float(var.get())))
            except ValueError:
                pass
        if sch.get("games_max", 0) < sch.get("games_min", 0):
            sch["games_max"] = sch["games_min"]
        if sch.get("rest_max_minutes", 0) < sch.get("rest_min_minutes", 0):
            sch["rest_max_minutes"] = sch["rest_min_minutes"]
        c["chest"]["enabled"] = self.v_chest.get()
        self._sync_late()
        c["chest"]["priority"] = list(self.chest_lists["early"])
        c["chest"]["priority_late"] = list(self.chest_lists["late"]) if self.late_custom else []
        c["chest"]["prefer_rarity"] = self.v_chest_rarity.get()
        for key_, var_ in (("reroll_rank", self.v_chest_rr), ("banish_rank", self.v_chest_br),
                           ("focus_rank", self.v_chest_fr), ("late_from", self.v_chest_late)):
            try:
                c["chest"][key_] = max(0, int(var_.get()))
            except ValueError:
                pass
        c["move"]["enabled"] = self.v_move.get()
        c["move"]["boss_dodge"] = self.v_dodge.get()
        loop = self.v_loop.get()
        for k in ("result", "lobby", "town"):
            c[k]["enabled"] = loop
        c["debug_snapshots"] = self.v_snap.get()
        c["town"]["learn_route"] = self.v_route.get()
        st_ = self.v_stage.get()
        if st_.startswith("遠征："):
            c["town"]["mode"], c["town"]["stage"] = "expedition", st_[len("遠征："):]
        else:
            c["town"]["mode"] = "festival"
        c["town"]["difficulty"] = next((k for k, v in self.DIFF_CHOICES.items() if v == self.v_diff.get()),
                                       "keep")
        for k in ("afk", "login", "charselect"):
            c.setdefault(k, dict(rb.DEFAULT_CONFIG[k]))["enabled"] = self.v_login.get()
        c["charselect"]["slot"] = int(self.v_slot.get())
        try:
            with open(rb.CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(c, f, ensure_ascii=False, indent=2)
            self.cfg_mtime = self._cfg_mtime()
            if not quiet:
                self.add_log("設定已儲存")
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"儲存設定失敗：\n{e}")

    # ---------------- 介面 ----------------
    def build(self):
        pad = {"padx": 8, "pady": 4}
        style = ttk.Style()
        try:
            style.theme_use("vista" if sys.platform == "win32" else "clam")
        except Exception:
            pass
        style.configure("Big.TButton", font=("Microsoft JhengHei UI", 11, "bold"), padding=6)
        style.configure("Status.TLabel", font=("Microsoft JhengHei UI", 12, "bold"))

        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)
        self.status = ttk.Label(top, text="● 待命", style="Status.TLabel", foreground="#666")
        self.status.pack(side="left")
        ttk.Label(top, text="F8 暫停／繼續　F9 停止").pack(side="right")

        btns = ttk.Frame(self.root)
        btns.pack(fill="x", **pad)
        self.b_start = ttk.Button(btns, text="▶ 開始", style="Big.TButton", command=self.start)
        self.b_pause = ttk.Button(btns, text="‖ 暫停", style="Big.TButton", command=self.pause,
                                  state="disabled")
        self.b_stop = ttk.Button(btns, text="■ 停止", style="Big.TButton", command=self.stop,
                                 state="disabled")
        for b in (self.b_start, self.b_pause, self.b_stop):
            b.pack(side="left", expand=True, fill="x", padx=4)

        # 掛機統計（每秒更新：場數、每小時幾場、平均每場時間、每場撞牆次數）
        srow = ttk.Frame(self.root)
        srow.pack(fill="x", padx=12, pady=(0, 2))
        ttk.Button(srow, text="加入 Discord 社群", command=lambda: webbrowser.open(DISCORD_URL)).pack(
            side="right")
        ttk.Button(srow, text="今日報表", command=self.open_report).pack(side="right", padx=4)
        self.stats_lbl = ttk.Label(srow, text=rb.stats_text(), foreground="#444")
        self.stats_lbl.pack(side="left", fill="x", expand=True)
        self.stats_next = 0

        nb = ttk.Notebook(self.root)
        nb.pack(fill="x", **pad)

        # 基本
        f1 = ttk.Frame(nb, padding=8)
        nb.add(f1, text="基本")
        self.v_title = tk.StringVar()
        self.v_dry = tk.BooleanVar()
        self.v_lv = tk.BooleanVar()
        self.v_chest = tk.BooleanVar()
        self.v_move = tk.BooleanVar()
        self.v_dodge = tk.BooleanVar()
        self.v_loop = tk.BooleanVar()
        self.v_snap = tk.BooleanVar()
        ttk.Label(f1, text="遊戲視窗標題（部分文字即可）").grid(row=0, column=0, sticky="w")
        ttk.Entry(f1, textvariable=self.v_title).grid(row=0, column=1, sticky="ew", padx=6)
        f1.columnconfigure(1, weight=1)
        self.v_mode = tk.StringVar()
        mf = ttk.Frame(f1)
        mf.grid(row=11, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(mf, text="操作方式：").pack(side="left")
        ttk.Radiobutton(mf, text="背景（推薦，可同時用其他視窗）", value="cdp",
                        variable=self.v_mode).pack(side="left")
        ttk.Radiobutton(mf, text="前景（模擬鍵盤滑鼠）", value="screen",
                        variable=self.v_mode).pack(side="left", padx=6)
        ef = ttk.Frame(f1)
        ef.grid(row=12, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Button(ef, text="開啟遊戲用的 Edge", command=self.open_game_edge).pack(side="left")
        ttk.Button(ef, text="Edge 效能設定", command=self.open_edge_settings).pack(side="left",
                                                                              padx=4)
        ttk.Label(ef, text="第一次請在裡面登入遊戲；不要縮到最小",
                  foreground="#666").pack(side="left", padx=6)
        self.v_restart = tk.StringVar()
        rsf = ttk.Frame(f1)
        rsf.grid(row=16, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Label(rsf, text="背景模式：每").pack(side="left")
        ttk.Spinbox(rsf, from_=0, to=48, width=4, textvariable=self.v_restart).pack(side="left", padx=2)
        ttk.Label(rsf, text="小時重開一次遊戲用的 Edge（一局打完時；需要開自動登入；0 = 不重開）").pack(
            side="left")
        self.v_lockwin = tk.BooleanVar()
        ttk.Checkbutton(f1, text="背景模式：固定遊戲視窗大小 1280×960（被縮小時自動還原）",
                        variable=self.v_lockwin).grid(row=13, column=0, columnspan=2,
                                                      sticky="w", pady=(4, 0))
        checks = [
            (self.v_dry, "測試模式（只辨識、不操作）"),
            (self.v_lv, "升級自動選技能"),
            (self.v_chest, "寶箱自動選裝備"),
            (self.v_move, "自動走位撿東西"),
            (self.v_dodge, "頭目（MVP）出現時閃避地上的紅色預警範圍"),
            (self.v_loop, "自動循環（結算 → 大廳 → 主持人 → 開始遊戲 → 前往野外）"),
            (self.v_snap, "儲存除錯截圖到 logs"),
        ]
        dodge_cb = None
        for r, (v, t) in enumerate(checks, start=1):
            cb_ = ttk.Checkbutton(f1, text=t, variable=v)
            cb_.grid(row=r, column=0, columnspan=2, sticky="w", pady=1)
            if v is self.v_dodge:
                dodge_cb = cb_
        lg = ttk.LabelFrame(f1, text="自動登入（暫離彈窗→接受、首頁→開始遊戲、選角色→開始遊戲）",
                            padding=6)
        lg.grid(row=len(checks) + 2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.v_login = tk.BooleanVar()
        self.v_slot = tk.IntVar(value=1)
        ttk.Checkbutton(lg, text="啟用自動登入", variable=self.v_login).grid(
            row=0, column=0, columnspan=5, sticky="w")
        ttk.Label(lg, text="要用哪一格的角色（跟遊戲裡角色選擇畫面的位置一樣）：",
                  foreground="#666").grid(row=1, column=0, columnspan=5, sticky="w", pady=(2, 2))
        for n in range(1, 16):
            r_, c_ = divmod(n - 1, 5)
            ttk.Radiobutton(lg, text=f"第 {n} 格", value=n, variable=self.v_slot).grid(
                row=2 + r_, column=c_, sticky="w", padx=(0, 8))
        uf = ttk.LabelFrame(f1, text=f"線上更新（目前版本 v{rb.VERSION}）", padding=6)
        if not NO_UPDATE:
            uf.grid(row=len(checks) + 3, column=0, columnspan=2, sticky="ew", pady=(6, 0))
            ttk.Button(uf, text="檢查更新", command=lambda: self.check_update(quiet=False)).pack(
                side="left")
        self.upd_status = ttk.Label(uf, text="", foreground="#666")
        self.upd_status.pack(side="left", padx=8)
        af = ttk.LabelFrame(f1, text="授權", padding=6)
        af.grid(row=14, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.lic_label = ttk.Label(af, text="")
        self.lic_label.pack(side="left")
        ttk.Button(af, text="輸入序號", command=self.show_activate).pack(side="right")
        sf = ttk.LabelFrame(f1, text="刷哪一關", padding=6)
        sf.grid(row=15, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.STAGE_CHOICES = ["節慶主持人（原本的關卡）"] + [f"遠征：{x}" for x in rb.EXPEDITION_STAGES]
        self.DIFF_CHOICES = {"keep": "不調整（照遊戲目前的設定）", "min": "最簡單", "max": "最難"}
        self.v_stage = tk.StringVar(value=self.STAGE_CHOICES[0])
        self.v_diff = tk.StringVar(value=self.DIFF_CHOICES["keep"])
        ttk.Label(sf, text="關卡").grid(row=0, column=0, sticky="w")
        ttk.Combobox(sf, textvariable=self.v_stage, values=self.STAGE_CHOICES, state="readonly",
                     width=26).grid(row=0, column=1, sticky="w", padx=6, pady=2)
        ttk.Label(sf, text="難度").grid(row=1, column=0, sticky="w")
        ttk.Combobox(sf, textvariable=self.v_diff, values=list(self.DIFF_CHOICES.values()),
                     state="readonly", width=26).grid(row=1, column=1, sticky="w", padx=6, pady=2)
        ttk.Label(sf, text="遠征會走到遠征隊長、選關、出發；難度在按「前往野外」前調整",
                  foreground="#666").grid(row=2, column=0, columnspan=2, sticky="w")
        keep = {lg, uf, mf, ef, af, sf, dodge_cb, rsf}   # 精簡介面時基本頁只留這幾區（加上頭目閃避）
        rf = ttk.Frame(f1)
        rf.grid(row=len(checks) + 1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.v_route = tk.BooleanVar()
        ttk.Checkbutton(rf, text="城鎮記住成功的路線（下次照走，比較不會卡）",
                        variable=self.v_route).pack(side="left")
        ttk.Button(rf, text="清除記住的路線", command=self.clear_route).pack(side="left", padx=6)

        # 技能
        f2 = ttk.Frame(nb, padding=8)
        nb.add(f2, text="技能")
        self.v_until = tk.StringVar()
        self.v_rarity = tk.BooleanVar()
        sk = ttk.LabelFrame(f2, text="勾選技能（主力：會優先選；放逐：出現就放逐。兩者擇一）",
                            padding=6)
        sk.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.v_main, self.v_ban = {}, {}
        half = (len(SKILLS) + 1) // 2
        for col in range(2):
            base = col * 4
            ttk.Label(sk, text="技能", foreground="#666").grid(row=0, column=base, sticky="w")
            ttk.Label(sk, text="主力", foreground="#666").grid(row=0, column=base + 1)
            ttk.Label(sk, text="放逐", foreground="#666").grid(row=0, column=base + 2)
            if col == 0:
                ttk.Separator(sk, orient="vertical").grid(row=0, column=3, rowspan=half + 1,
                                                          sticky="ns", padx=12)
        for i, name in enumerate(SKILLS):
            col, row = divmod(i, half)
            base = col * 4
            vm, vb = tk.BooleanVar(), tk.BooleanVar()
            self.v_main[name], self.v_ban[name] = vm, vb
            ttk.Label(sk, text=name).grid(row=row + 1, column=base, sticky="w", pady=1)
            ttk.Checkbutton(sk, variable=vm,
                            command=lambda n=name: self.v_ban[n].set(False)
                            if self.v_main[n].get() else None).grid(row=row + 1, column=base + 1)
            ttk.Checkbutton(sk, variable=vb,
                            command=lambda n=name: self.v_main[n].get() and self.v_main[n].set(False)
                            if self.v_ban[n].get() else None).grid(row=row + 1, column=base + 2)
        self.v_autopick = tk.BooleanVar()
        ttk.Checkbutton(f2, text="技能自動：放逐完「攻擊：屬性附加」和第四技能之後，改按遊戲的「自動」",
                        variable=self.v_autopick).grid(row=1, column=0, columnspan=2, sticky="w",
                                                       pady=(4, 0))
        uf = ttk.Frame(f2)
        uf.grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(uf, text="優先「增加目標」到 Lv").pack(side="left")
        ttk.Spinbox(uf, from_=1, to=99, width=5, textvariable=self.v_until).pack(side="left", padx=4)
        ttk.Label(uf, text="（之後改成優先傷害／冷卻）").pack(side="left")
        self.v_bmin = tk.StringVar()
        self.v_bmax = tk.StringVar()
        bf = ttk.Frame(f2)
        bf.grid(row=5, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Label(bf, text="累積").pack(side="left")
        ttk.Spinbox(bf, from_=1, to=20, width=4, textvariable=self.v_bmin).pack(side="left", padx=2)
        ttk.Label(bf, text="～").pack(side="left")
        ttk.Spinbox(bf, from_=1, to=20, width=4, textvariable=self.v_bmax).pack(side="left", padx=2)
        ttk.Label(bf, text="次升級才按 B 一次選完（打完 Boss 的最後倒數不再升級）").pack(side="left")
        self.v_maxlv = tk.StringVar()
        mxf = ttk.Frame(f2)
        mxf.grid(row=6, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Label(mxf, text="角色等級達到 Lv").pack(side="left")
        ttk.Spinbox(mxf, from_=0, to=999, width=5, textvariable=self.v_maxlv).pack(side="left", padx=4)
        ttk.Label(mxf, text="之後不再升級（0 = 不限）").pack(side="left")
        self.v_deity = tk.StringVar()
        dyf = ttk.Frame(f2)
        dyf.grid(row=7, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Label(dyf, text="神祇（角色 20 等後道具欄旁的「!」）：選").pack(side="left")
        ttk.Combobox(dyf, textvariable=self.v_deity, state="readonly", width=12,
                     values=rb.DEITY_CHOICES + [DEITY_NONE]).pack(side="left", padx=4)
        ttk.Checkbutton(f2, text="卡片等級（普通<優良<稀有<史詩<傳說）最優先，同等級再看效果",
                        variable=self.v_rarity).grid(row=3, column=0, columnspan=2, sticky="w",
                                                     pady=4)
        ttk.Label(f2, text="勾放逐的技能會先選一次當第四技能，確認選到後才開始放逐（攻擊：屬性附加 也等第四技能選到才放逐）",
                  foreground="#666").grid(row=4, column=0, columnspan=2, sticky="w")

        # 寶箱
        f4 = ttk.Frame(nb, padding=8)
        nb.add(f4, text="寶箱")
        self.v_chest_rarity = tk.BooleanVar()
        self.v_chest_phase = tk.StringVar(value="early")
        phf = ttk.Frame(f4)
        phf.grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(phf, text="寶箱的優先順序（越上面越優先；拖曳或用右邊按鈕排）　正在編輯：").pack(side="left")
        ttk.Radiobutton(phf, text="前期排名", value="early", variable=self.v_chest_phase,
                        command=self.switch_chest_phase).pack(side="left")
        ttk.Radiobutton(phf, text="後期排名", value="late", variable=self.v_chest_phase,
                        command=self.switch_chest_phase).pack(side="left", padx=4)
        lf2 = ttk.Frame(f4)
        lf2.grid(row=1, column=0, sticky="nsew", pady=4)
        f4.columnconfigure(0, weight=1)
        self.lb_chest = tk.Listbox(lf2, height=14, activestyle="none", exportselection=False,
                                   font=("Microsoft JhengHei UI", 10))
        self.lb_chest.bind("<Button-1>", self._drag_start)
        self.lb_chest.bind("<B1-Motion>", self._drag_move)
        sb2 = ttk.Scrollbar(lf2, command=self.lb_chest.yview)
        self.lb_chest.configure(yscrollcommand=sb2.set)
        self.lb_chest.pack(side="left", fill="both", expand=True)
        sb2.pack(side="right", fill="y")
        bf2 = ttk.Frame(f4)
        bf2.grid(row=1, column=1, sticky="n", padx=(6, 0), pady=4)
        for txt, fn in (("⤒ 置頂", lambda: self.move_chest("top")),
                        ("▲ 上移", lambda: self.move_chest(-1)),
                        ("▼ 下移", lambda: self.move_chest(1)),
                        ("⤓ 置底", lambda: self.move_chest("bottom"))):
            ttk.Button(bf2, text=txt, width=8, command=fn).pack(pady=2)
        self.v_chest_auto = tk.BooleanVar()
        ttk.Checkbutton(f4, text="寶箱自動：直接按遊戲的「自動寶箱」（不勾 = 照上面的優先順序選）",
                        variable=self.v_chest_auto).grid(row=3, column=0, columnspan=2, sticky="w")
        rbf = ttk.Frame(f4)
        rbf.grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.v_chest_rr = tk.StringVar()
        self.v_chest_br = tk.StringVar()
        ttk.Label(rbf, text="排名在前").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(rbf, from_=0, to=99, width=4, textvariable=self.v_chest_rr).grid(row=0, column=1)
        ttk.Label(rbf, text="名之外的卡 → 按那張卡的「重抽」（0 = 不重抽）").grid(row=0, column=2, sticky="w")
        ttk.Label(rbf, text="排名在前").grid(row=1, column=0, sticky="w")
        ttk.Spinbox(rbf, from_=0, to=99, width=4, textvariable=self.v_chest_br).grid(row=1, column=1)
        ttk.Label(rbf, text="名之外的卡 → 按「放逐」（0 = 不放逐；技能放逐完才會做）").grid(
            row=1, column=2, sticky="w")
        self.v_chest_fr = tk.StringVar()
        ttk.Label(rbf, text="排名在前").grid(row=2, column=0, sticky="w")
        ttk.Spinbox(rbf, from_=0, to=99, width=4, textvariable=self.v_chest_fr).grid(row=2, column=1)
        ttk.Label(rbf, text="名的卡 → 先按「專注」再選（之後更常出現；每種每局一次，0 = 不用）").grid(
            row=2, column=2, sticky="w")
        self.v_chest_late = tk.StringVar()
        ttk.Label(rbf, text="遊戲時間第").grid(row=3, column=0, sticky="w")
        ttk.Spinbox(rbf, from_=0, to=20, width=4, textvariable=self.v_chest_late).grid(row=3, column=1)
        ltf = ttk.Frame(rbf)
        ltf.grid(row=3, column=2, sticky="w")
        ttk.Label(ltf, text="分鐘起改用「後期排名」（0 = 不分前後期）").pack(side="left")
        ttk.Button(ltf, text="後期排名重設成跟前期一樣", command=self.reset_late).pack(side="left", padx=6)
        ttk.Checkbutton(f4, text="清單以外的寶箱，選卡片等級高的（普通<優良<稀有<史詩<傳說）",
                        variable=self.v_chest_rarity).grid(row=2, column=0, columnspan=2,
                                                           sticky="w")

        # 精簡介面：只顯示自動登入、操作方式、線上更新；其他設定隱藏但照常生效
        # （設定檔 "ui": {"show_all": true} 可以全部顯示）
        if not self.cfg.get("ui", {}).get("show_all", False):
            for w in f1.grid_slaves():
                if w not in keep:
                    w.grid_remove()

        # 休息排程
        f3 = ttk.Frame(nb, padding=8)
        nb.add(f3, text="休息排程")
        self.v_sch = tk.BooleanVar()
        self.v_gmin = tk.StringVar()
        self.v_gmax = tk.StringVar()
        self.v_rmin = tk.StringVar()
        self.v_rmax = tk.StringVar()
        self.v_stopn = tk.StringVar()
        ttk.Checkbutton(f3, text="啟用休息排程（一局結束回到城鎮後才休息，不會打斷戰鬥）",
                        variable=self.v_sch).grid(row=0, column=0, columnspan=6, sticky="w")

        def spin(row, label, v1, v2, unit, to):
            ttk.Label(f3, text=label).grid(row=row, column=0, sticky="w", pady=3)
            ttk.Spinbox(f3, from_=1, to=to, width=5, textvariable=v1).grid(row=row, column=1, padx=2)
            ttk.Label(f3, text="～").grid(row=row, column=2)
            ttk.Spinbox(f3, from_=1, to=to, width=5, textvariable=v2).grid(row=row, column=3, padx=2)
            ttk.Label(f3, text=unit).grid(row=row, column=4, sticky="w")

        spin(1, "每玩", self.v_gmin, self.v_gmax, "場（每輪隨機）", 99)
        spin(2, "休息", self.v_rmin, self.v_rmax, "分鐘（每次隨機）", 600)
        ttk.Label(f3, text="玩滿").grid(row=3, column=0, sticky="w", pady=3)
        ttk.Spinbox(f3, from_=0, to=999, width=5, textvariable=self.v_stopn).grid(row=3, column=1,
                                                                                padx=2)
        ttk.Label(f3, text="場自動停止（0 = 不限）").grid(row=3, column=2, columnspan=3, sticky="w")

        compact = not self.cfg.get("ui", {}).get("show_all", False)
        # 工具列（精簡介面時隱藏；設定改成關閉視窗和按「開始」時自動儲存）
        tools = ttk.Frame(self.root)
        if not compact:
            tools.pack(fill="x", **pad)
        ttk.Button(tools, text="儲存設定", command=self.save_cfg).pack(side="left", padx=4)
        ttk.Button(tools, text="進階設定檔…", command=self.open_cfg).pack(side="left", padx=4)
        ttk.Button(tools, text="開啟紀錄資料夾", command=self.open_logs).pack(side="left", padx=4)
        ttk.Button(tools, text="清除畫面", command=lambda: self.txt.delete("1.0", "end")).pack(
            side="right", padx=4)

        # 紀錄
        lf = ttk.LabelFrame(self.root, text="執行紀錄")
        if not compact:     # 精簡介面不顯示，但紀錄照樣寫到 logs 資料夾
            lf.pack(fill="both", expand=True, **pad)
        self.txt = tk.Text(lf, height=16, wrap="word", font=("Consolas", 10),
                           bg="#1e1e1e", fg="#d4d4d4", insertbackground="#fff", relief="flat")
        sb = ttk.Scrollbar(lf, command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.txt.pack(side="left", fill="both", expand=True)
        self.txt.tag_configure("err", foreground="#f48771")
        self.txt.tag_configure("act", foreground="#9cdcfe")
        if compact:
            self.root.minsize(560, 300)

            def fit():
                self.root.update_idletasks()
                self.root.geometry(f"{max(680, self.root.winfo_reqwidth())}x{self.root.winfo_reqheight()}")
            fit()
            self.root.after(200, fit)

    def fill_from_cfg(self):
        c = self.cfg
        self.v_title.set(c.get("window_title", ""))
        self.v_mode.set(c.get("browser", {}).get("mode", "screen"))
        self.v_lockwin.set(c.get("browser", {}).get("lock_window_size", True))
        self.v_restart.set(f'{float(c.get("browser", {}).get("restart_hours", 6)):g}')
        self.v_dry.set(c.get("dry_run", False))
        self.v_lv.set(c["levelup"]["enabled"])
        self.v_chest.set(c["chest"]["enabled"])
        self.v_move.set(c["move"]["enabled"])
        self.v_dodge.set(c["move"].get("boss_dodge", True))
        self.v_loop.set(c["result"]["enabled"] and c["lobby"]["enabled"] and c["town"]["enabled"])
        self.v_route.set(c["town"].get("learn_route", True))
        if c["town"].get("mode") == "expedition":
            self.v_stage.set("遠征：" + c["town"].get("stage", rb.EXPEDITION_STAGES[0]))
        else:
            self.v_stage.set(self.STAGE_CHOICES[0])
        self.v_diff.set(self.DIFF_CHOICES.get(c["town"].get("difficulty", "keep"), self.DIFF_CHOICES["keep"]))
        self.v_login.set(c.get("login", {}).get("enabled", True))
        self.v_slot.set(int(c.get("charselect", {}).get("slot", 1)))
        self.v_snap.set(c.get("debug_snapshots", True))
        names = [n for n, _ in rb.CHEST_ITEMS]
        order = [x for x in c["chest"].get("priority", []) if x]
        early = order + [n for n in names if n not in order]
        late = [x for x in (c["chest"].get("priority_late") or []) if x]
        self.late_custom = bool(late)
        late = (late + [n for n in early if n not in late]) if late else list(early)
        self.chest_lists = {"early": early, "late": late}
        self.v_chest_phase.set("early")
        self.show_chest()
        self.v_chest_rarity.set(c["chest"].get("prefer_rarity", True))
        self.v_chest_rr.set(str(c["chest"].get("reroll_rank", 10)))
        self.v_chest_br.set(str(c["chest"].get("banish_rank", 0)))
        self.v_chest_fr.set(str(c["chest"].get("focus_rank", 3)))
        self.v_chest_late.set(str(c["chest"].get("late_from", 7)))
        pri = c["levelup"].get("priority", [])
        ban = c["levelup"].get("banish", [])
        for n in SKILLS:
            self.v_main[n].set(any(rb.match(n, k) and rb.match(k, n) for k in pri))
            self.v_ban[n].set(not self.v_main[n].get() and
                              any(rb.match(n, k) and rb.match(k, n) for k in ban))
        self.v_until.set(str(c["levelup"].get("target_until_level", 11)))
        self.v_rarity.set(c["levelup"].get("rarity_first", False))
        dc = c.get("deity", {})
        self.v_deity.set(dc.get("choice", "雷擊術") if dc.get("enabled", True) else DEITY_NONE)
        self.v_autopick.set(c["levelup"].get("auto_after_banish", False))
        self.v_chest_auto.set(c["chest"].get("auto", False))
        sch = c.get("schedule", {})
        self.v_sch.set(sch.get("enabled", False))
        self.v_gmin.set(str(sch.get("games_min", 3)))
        self.v_gmax.set(str(sch.get("games_max", 5)))
        self.v_rmin.set(str(sch.get("rest_min_minutes", 10)))
        self.v_rmax.set(str(sch.get("rest_max_minutes", 20)))
        self.v_stopn.set(str(sch.get("stop_after_games", 0)))
        self.v_maxlv.set(str(c["levelup"].get("max_level", 0)))
        self.v_bmin.set(str(c["levelup"].get("batch_min", 3)))
        self.v_bmax.set(str(c["levelup"].get("batch_max", 4)))

    # ---------------- 動作 ----------------
    def add_log(self, line):
        tag = ""
        if "錯誤" in line or "失敗" in line or "Traceback" in line:
            tag = "err"
        elif "→" in line or "選擇" in line or "放逐" in line:
            tag = "act"
        self.txt.insert("end", line + "\n", tag)
        if int(self.txt.index("end-1c").split(".")[0]) > 2000:
            self.txt.delete("1.0", "500.0")
        self.txt.see("end")

    def poll(self):
        try:
            while True:
                self.add_log(self.q.get_nowait())
        except queue.Empty:
            pass
        running = self.thread is not None and self.thread.is_alive()
        if running:
            self.was_running = True
            if rb.CONTROL.get("paused"):
                self.status.config(text="● 暫停中", foreground="#c08000")
                self.b_pause.config(text="▶ 繼續")
            elif rb.CONTROL.get("status"):
                self.status.config(text="● " + rb.CONTROL["status"], foreground="#2f6fb5")
                self.b_pause.config(text="‖ 暫停")
            else:
                self.status.config(text="● 執行中" + ("（測試模式）" if self.cfg["dry_run"] else ""),
                                   foreground="#1a7f37")
                self.b_pause.config(text="‖ 暫停")
        elif getattr(self, "was_running", False):
            # 執行緒已經結束（按了停止、自動停止或出錯）→ 恢復成可以再按「開始」
            self.was_running = False
            self.set_idle()
            self.stats_lbl.config(text=rb.stats_text())
        if running and time.time() >= self.stats_next:
            self.stats_next = time.time() + 1.0
            self.stats_lbl.config(text=rb.stats_text())
        self.root.after(150, self.poll)

    def set_idle(self):
        self.status.config(text="● 待命", foreground="#666")
        self.b_start.config(state="normal")
        self.b_pause.config(state="disabled", text="‖ 暫停")
        self.b_stop.config(state="disabled")

    # ---------------- 授權 ----------------
    def refresh_license(self):
        r = licensing.check()
        self.lic = r
        color = "#1a7f37" if r["ok"] and r["days_left"] > 3 else ("#c08000" if r["ok"] else "#c0392b")
        txt = r["msg"] + ("　快到期了，記得續購" if r["ok"] and r["days_left"] <= 3 else "")
        self.lic_label.config(text=txt, foreground=color)
        return r

    def startup_license(self):
        r = self.refresh_license()
        if not r["ok"]:
            self.show_activate()

    def show_activate(self):
        win = tk.Toplevel(self.root)
        win.title("輸入序號")
        win.transient(self.root)
        win.resizable(False, False)
        fr = ttk.Frame(win, padding=14)
        fr.pack(fill="both", expand=True)
        ttk.Label(fr, text="1. 把這台電腦的機器碼，連同付款人姓名寄給賣家：").grid(row=0, column=0,
                                                                     columnspan=2, sticky="w")
        mc = tk.StringVar(value=licensing.machine_code())
        e = ttk.Entry(fr, textvariable=mc, width=26, font=("Consolas", 13), state="readonly")
        e.grid(row=1, column=0, sticky="w", pady=(4, 10))

        def copy_code():
            self.root.clipboard_clear()
            self.root.clipboard_append(mc.get())
            msg.config(text="已複製機器碼", foreground="#1a7f37")
        ttk.Button(fr, text="複製", command=copy_code).grid(row=1, column=1, sticky="w", padx=6,
                                                          pady=(4, 10))
        ttk.Label(fr, text="2. 收到序號後貼在下面，按「啟用」：").grid(row=2, column=0, columnspan=2,
                                                            sticky="w")
        txt = tk.Text(fr, width=52, height=4, font=("Consolas", 10), wrap="char")
        txt.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(4, 6))
        msg = ttk.Label(fr, text=self.lic["msg"] if getattr(self, "lic", None) else "")
        msg.grid(row=4, column=0, columnspan=2, sticky="w")

        def do_activate():
            key = txt.get("1.0", "end").strip()
            if not key:
                msg.config(text="請先貼上序號", foreground="#c0392b")
                return
            r = licensing.activate(key)
            if r["ok"]:
                self.refresh_license()
                self.add_log("序號啟用成功：" + r["msg"])
                messagebox.showinfo(APP_TITLE, "啟用成功！" + r["msg"], parent=win)
                win.destroy()
            else:
                msg.config(text=r["msg"], foreground="#c0392b")
        bf = ttk.Frame(fr)
        bf.grid(row=5, column=0, columnspan=2, sticky="e", pady=(8, 0))
        ttk.Button(bf, text="啟用", command=do_activate).pack(side="right")
        ttk.Button(bf, text="關閉", command=win.destroy).pack(side="right", padx=6)
        txt.focus_set()
        win.grab_set()

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        if not self.refresh_license()["ok"]:
            messagebox.showwarning(APP_TITLE, self.lic["msg"])
            self.show_activate()
            return
        self.save_cfg(quiet=True)
        cfg = json.loads(json.dumps(self.cfg))
        rb.CONTROL.update(stop=False, toggle_pause=False, paused=False)
        self.b_start.config(state="disabled")
        self.b_pause.config(state="normal")
        self.b_stop.config(state="normal")
        self.thread = threading.Thread(target=self.worker, args=(cfg,), daemon=True)
        self.thread.start()

    def worker(self, cfg):
        crashes = []
        while True:
            try:
                rb.run(cfg)
            except rb.FailSafe:
                rb.log("已觸發緊急停止（滑鼠在左上角）")
            except Exception:
                tb = traceback.format_exc()
                rb.log("發生錯誤：\n" + tb)
                try:
                    with open(os.path.join(rb.LOG_DIR, "error.txt"), "w", encoding="utf-8") as f:
                        f.write(tb)
                except Exception:
                    pass
                # 程式出錯不要整個停掉：10 分鐘內出錯 5 次以內就自動重新開始
                now = time.time()
                crashes = [t for t in crashes if now - t < 600] + [now]
                if len(crashes) <= 5 and not rb.CONTROL.get("stop"):
                    rb.log(f"5 秒後自動重新開始（10 分鐘內第 {len(crashes)} 次出錯）")
                    for _ in range(50):
                        if rb.CONTROL.get("stop"):
                            break
                        time.sleep(0.1)
                    if not rb.CONTROL.get("stop"):
                        continue
                else:
                    rb.log("短時間內出錯太多次，先停下來；請把 logs\\error.txt 傳給開發者")
            break
        rb.log("已停止")

    @property
    def chest_order(self):
        """清單上正在編輯的那一份（前期或後期排名）"""
        return self.chest_lists[self.v_chest_phase.get()]

    def _sync_late(self):
        # 後期排名沒有自己改過 → 一直跟前期一樣
        if not self.late_custom:
            self.chest_lists["late"] = list(self.chest_lists["early"])

    def _touch(self):
        if self.v_chest_phase.get() == "late":
            self.late_custom = True

    def switch_chest_phase(self):
        self._sync_late()
        self.show_chest()

    def reset_late(self):
        self.late_custom = False
        self._sync_late()
        self.show_chest()
        self.add_log("後期排名已重設成跟前期一樣")

    def show_chest(self, select=None):
        top = self.lb_chest.yview()[0]
        self.lb_chest.delete(0, "end")
        for i, n in enumerate(self.chest_order, 1):
            eff = CHEST_EFFECT.get(n, "")
            cat = CHEST_CAT.get(n, "")
            self.lb_chest.insert("end", f"{i:2d}. [{cat}] {n}　{eff}")
        self.lb_chest.yview_moveto(top)
        if select is not None:
            self.lb_chest.selection_set(select)
            self.lb_chest.see(select)

    def _drag_start(self, e):
        self._drag_i = self.lb_chest.nearest(e.y)

    def _drag_move(self, e):
        i = getattr(self, "_drag_i", None)
        j = self.lb_chest.nearest(e.y)
        if i is None or j == i or j < 0:
            return
        self._touch()
        item = self.chest_order.pop(i)
        self.chest_order.insert(j, item)
        self._drag_i = j
        self.show_chest(j)

    def move_chest(self, how):
        sel = self.lb_chest.curselection()
        if not sel:
            return
        i = sel[0]
        self._touch()
        item = self.chest_order.pop(i)
        if how == "top":
            j = 0
        elif how == "bottom":
            j = len(self.chest_order)
        else:
            j = min(max(0, i + how), len(self.chest_order))
        self.chest_order.insert(j, item)
        self.show_chest(j)

    def open_report(self):
        """打開今天的掛機報表（每打完一場自動更新）；今天還沒有就開最近一天的"""
        try:
            fs = sorted(f for f in os.listdir(rb.LOG_DIR) if f.startswith("report_") and f.endswith(".txt"))
        except Exception:
            fs = []
        today = time.strftime("report_%Y%m%d.txt")
        name = today if today in fs else (fs[-1] if fs else None)
        if not name:
            messagebox.showinfo(APP_TITLE, "還沒有報表：每打完一場會自動記錄，\n"
                                           "存在 logs 資料夾的 report_日期.txt（摘要）和 report_日期.csv（每場明細）")
            return
        path = os.path.join(rb.LOG_DIR, name)
        try:
            if sys.platform == "win32":
                os.startfile(path)
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"打不開報表：{e}\n檔案在 {path}")

    def open_game_edge(self):
        self.save_cfg(quiet=True)
        b = self.cfg.get("browser", {})

        def work():
            try:
                import browser_cdp as bc
                exe = bc.find_edge(b.get("edge_path", ""))
                bc.launch_browser(exe, b.get("url", "https://ro-survivors.com"),
                                  int(b.get("port", 9229)),
                                  os.path.join(rb.BASE_DIR, "edge_profile"),
                                  tuple(b.get("window_size", [1280, 960])), log=rb.log)
            except Exception as e:
                rb.log(f"開啟 Edge 失敗：{e}")
        threading.Thread(target=work, daemon=True).start()

    def open_edge_settings(self):
        b = self.cfg.get("browser", {})
        try:
            import browser_cdp as bc
            exe = bc.find_edge(b.get("edge_path", ""))
            if bc.open_settings(exe, os.path.join(rb.BASE_DIR, "edge_profile")):
                messagebox.showinfo(APP_TITLE, "已在遊戲用的 Edge 開啟「系統與效能」設定。\n\n"
                                    "請把「效率模式」關掉，\n"
                                    "並在「睡眠索引標籤」的例外清單加入 ro-survivors.com，\n"
                                    "避免切到其他程式時遊戲變慢。")
            else:
                messagebox.showwarning(APP_TITLE, "找不到 Edge（msedge.exe）")
        except Exception as e:
            messagebox.showerror(APP_TITLE, str(e))

    def pause(self):
        rb.CONTROL["toggle_pause"] = True

    def stop(self):
        rb.CONTROL["stop"] = True
        self.b_stop.config(state="disabled")
        self.status.config(text="● 停止中…", foreground="#666")

    def open_path(self, path):
        try:
            if sys.platform == "win32":
                os.startfile(path)
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            messagebox.showerror(APP_TITLE, str(e))

    def open_cfg(self):
        self.save_cfg(quiet=True)
        self.open_path(rb.CONFIG_PATH)
        messagebox.showinfo(APP_TITLE, "在記事本改完並存檔後，請重新開啟本程式讓設定生效。")

    def open_logs(self):
        os.makedirs(rb.LOG_DIR, exist_ok=True)
        self.open_path(rb.LOG_DIR)

    # ---------------- 線上更新 ----------------
    def check_update(self, quiet):
        if NO_UPDATE:
            return
        """quiet=True：開程式時自動檢查，沒新版或出錯都不吵你"""
        uc = dict(rb.DEFAULT_CONFIG["update"])
        uc.update(self.cfg.get("update", {}))
        repo = (uc.get("repo") or "").strip() or rb.DEFAULT_CONFIG["update"]["repo"]
        if quiet and not uc.get("check_on_start", True):
            return
        token = uc.get("token", "")
        if not quiet:
            self.add_log("檢查更新中…")
            self.set_upd("檢查中…（最多 15 秒）")

        def work():
            try:
                info = updater.check(repo, rb.VERSION, token)
            except Exception as e:
                if not quiet:
                    self.set_upd("檢查失敗")
                    self.root.after(0, lambda m=str(e): messagebox.showwarning(
                        APP_TITLE, f"檢查更新失敗：{m}\n\n（連不上 GitHub 時，可以到 Discord 下載新版手動解壓覆蓋）"))
                    self.q.put(f"檢查更新失敗：{e}")
                return
            if info is None:
                if not quiet:
                    self.set_upd(f"已經是最新版 v{rb.VERSION}")
                    self.q.put(f"已經是最新版 v{rb.VERSION}")
                    self.root.after(0, lambda: messagebox.showinfo(APP_TITLE, f"已經是最新版 v{rb.VERSION}"))
                return
            self.root.after(0, lambda: self.ask_update(info, token))
        threading.Thread(target=work, daemon=True).start()

    def set_upd(self, text):
        lb = getattr(self, "upd_status", None)
        if lb is not None:
            self.root.after(0, lambda: lb.configure(text=text))

    def verify_update(self):
        """上次按了更新 → 重開後確認真的換成新版了"""
        to = updater.take_pending()
        if not to:
            return
        if updater.parse_ver(rb.VERSION) >= updater.parse_ver(to):
            self.add_log(f"已更新到 v{rb.VERSION}")
            return
        messagebox.showwarning(
            APP_TITLE, f"更新沒有成功：現在還是 v{rb.VERSION}（要更新到 v{to}）。\n\n"
                       "常見原因：\n"
                       "・程式放在 Program Files、OneDrive 同步中之類不能寫入的資料夾\n"
                       "・防毒軟體擋住了檔案覆蓋\n"
                       "・有另一個掛機小幫手還開著\n\n"
                       "請把整個資料夾搬到桌面或 D:\\ 再試，或下載新版手動解壓覆蓋。\n"
                       "詳細記錄在 logs\\update.log")

    def ask_update(self, info, token):
        notes = info["notes"].strip() or "（沒有寫更新內容）"
        if len(notes) > 900:
            notes = notes[:900] + "…"
        if not messagebox.askyesno(
                APP_TITLE, f"有新版本 v{info['version']}（目前 v{rb.VERSION}）\n\n{notes}\n\n"
                           "要現在更新嗎？（設定、記住的路線、登入資料都會保留，更新完會自動重開）"):
            return
        if self.thread and self.thread.is_alive():
            messagebox.showinfo(APP_TITLE, "程式正在執行中，請先按「停止」再更新。")
            return
        if not updater.can_write():
            messagebox.showwarning(
                APP_TITLE, f"程式所在的資料夾不能寫入，沒辦法自動更新：\n{rb.BASE_DIR}\n\n"
                           "請把整個資料夾搬到桌面或 D:\\ 之類的地方再按更新，"
                           "或下載新版手動解壓覆蓋。")
            return
        self.save_cfg(quiet=True)
        self.add_log(f"下載新版 v{info['version']}…")
        last = [-1]

        def prog(done, total):
            pct = int(done * 100 / total)
            if pct // 10 != last[0]:
                last[0] = pct // 10
                self.q.put(f"  下載中 {pct}%")
                self.set_upd(f"下載中 {pct}%")

        def work():
            try:
                tmp, src = updater.download(info, token, prog)
            except Exception as e:
                self.q.put(f"下載失敗：{e}")
                self.root.after(0, lambda m=str(e): messagebox.showwarning(APP_TITLE, f"下載失敗：{m}"))
                return
            self.q.put("下載完成，關閉程式並換成新版…")
            self.set_upd("下載完成，程式會關掉，幾秒後自動重開")

            def finish():
                try:
                    updater.mark_pending(info["version"])
                    updater.apply_and_restart(tmp, src)
                except Exception as e:
                    messagebox.showwarning(APP_TITLE, f"更新失敗：{e}")
                    return
                self.root.destroy()
                os._exit(0)
            self.root.after(500, finish)
        threading.Thread(target=work, daemon=True).start()

    def on_close(self):
        try:
            self.save_cfg(quiet=True)
        except Exception:
            pass
        if self.thread and self.thread.is_alive():
            rb.CONTROL["stop"] = True
            self.thread.join(timeout=3)
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
