"""
RO Survivors 自動輔助 — 視窗版
"""
import json
import os
import queue
import subprocess
import sys
import threading
import traceback
import tkinter as tk
from tkinter import ttk, messagebox

import ro_bot as rb
import updater

APP_TITLE = "RO Survivors 自動輔助"
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
        self.build()
        self.fill_from_cfg()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self.poll)
        root.after(2500, lambda: self.check_update(quiet=True))

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

    def save_cfg(self, quiet=False):
        c = self.cfg
        c["window_title"] = self.v_title.get().strip()
        c.setdefault("browser", {})["mode"] = self.v_mode.get() or "cdp"
        c["browser"]["lock_window_size"] = self.v_lockwin.get()
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
        c["levelup"]["auto_after_banish"] = self.v_autopick.get()
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
        c["chest"]["priority"] = list(self.chest_order)
        c["chest"]["prefer_rarity"] = self.v_chest_rarity.get()
        c["move"]["enabled"] = self.v_move.get()
        loop = self.v_loop.get()
        for k in ("result", "lobby", "town"):
            c[k]["enabled"] = loop
        c["debug_snapshots"] = self.v_snap.get()
        c["town"]["learn_route"] = self.v_route.get()
        for k in ("afk", "login", "charselect"):
            c.setdefault(k, dict(rb.DEFAULT_CONFIG[k]))["enabled"] = self.v_login.get()
        c["charselect"]["slot"] = int(self.v_slot.get())
        try:
            with open(rb.CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(c, f, ensure_ascii=False, indent=2)
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
        self.v_loop = tk.BooleanVar()
        self.v_snap = tk.BooleanVar()
        ttk.Label(f1, text="遊戲視窗標題（部分文字即可）").grid(row=0, column=0, sticky="w")
        ttk.Entry(f1, textvariable=self.v_title).grid(row=0, column=1, sticky="ew", padx=6)
        f1.columnconfigure(1, weight=1)
        self.v_mode = tk.StringVar()
        mf = ttk.Frame(f1)
        mf.grid(row=10, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(mf, text="操作方式：").pack(side="left")
        ttk.Radiobutton(mf, text="背景（推薦，可同時用其他視窗）", value="cdp",
                        variable=self.v_mode).pack(side="left")
        ttk.Radiobutton(mf, text="前景（模擬鍵盤滑鼠）", value="screen",
                        variable=self.v_mode).pack(side="left", padx=6)
        ef = ttk.Frame(f1)
        ef.grid(row=11, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Button(ef, text="開啟遊戲用的 Edge", command=self.open_game_edge).pack(side="left")
        ttk.Button(ef, text="Edge 效能設定", command=self.open_edge_settings).pack(side="left",
                                                                              padx=4)
        ttk.Label(ef, text="第一次請在裡面登入遊戲；不要縮到最小",
                  foreground="#666").pack(side="left", padx=6)
        self.v_lockwin = tk.BooleanVar()
        ttk.Checkbutton(f1, text="背景模式：固定遊戲視窗大小 1280×960（被縮小時自動還原）",
                        variable=self.v_lockwin).grid(row=12, column=0, columnspan=2,
                                                      sticky="w", pady=(4, 0))
        checks = [
            (self.v_dry, "測試模式（只辨識、不操作）"),
            (self.v_lv, "升級自動選技能"),
            (self.v_chest, "寶箱自動選裝備"),
            (self.v_move, "自動走位撿東西"),
            (self.v_loop, "自動循環（結算 → 大廳 → 主持人 → 開始遊戲 → 前往野外）"),
            (self.v_snap, "儲存除錯截圖到 logs"),
        ]
        for r, (v, t) in enumerate(checks, start=1):
            ttk.Checkbutton(f1, text=t, variable=v).grid(row=r, column=0, columnspan=2, sticky="w",
                                                         pady=1)
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
        uf.grid(row=len(checks) + 3, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(uf, text="檢查更新", command=lambda: self.check_update(quiet=False)).pack(
            side="left")
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
        ttk.Checkbutton(f2, text="放逐完「攻擊：屬性附加」和第四技能之後，改按遊戲的「自動」選技能",
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
        ttk.Checkbutton(f2, text="卡片等級（普通<優良<稀有<史詩<傳說）最優先，同等級再看效果",
                        variable=self.v_rarity).grid(row=3, column=0, columnspan=2, sticky="w",
                                                     pady=4)
        ttk.Label(f2, text="勾放逐的技能會先選一次當第四技能，下次出現才放逐（攻擊：屬性附加 出現就直接放逐）",
                  foreground="#666").grid(row=4, column=0, columnspan=2, sticky="w")

        # 寶箱
        f4 = ttk.Frame(nb, padding=8)
        nb.add(f4, text="寶箱")
        self.v_chest_rarity = tk.BooleanVar()
        ttk.Label(f4, text="寶箱的優先順序（越上面越優先選）：用滑鼠拖曳，或選一項後按右邊按鈕",
                  ).grid(row=0, column=0, columnspan=2, sticky="w")
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
        ttk.Checkbutton(f4, text="清單以外的寶箱，選卡片等級高的（普通<優良<稀有<史詩<傳說）",
                        variable=self.v_chest_rarity).grid(row=2, column=0, columnspan=2,
                                                           sticky="w")

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

        # 工具列
        tools = ttk.Frame(self.root)
        tools.pack(fill="x", **pad)
        ttk.Button(tools, text="儲存設定", command=self.save_cfg).pack(side="left", padx=4)
        ttk.Button(tools, text="進階設定檔…", command=self.open_cfg).pack(side="left", padx=4)
        ttk.Button(tools, text="開啟紀錄資料夾", command=self.open_logs).pack(side="left", padx=4)
        ttk.Button(tools, text="清除畫面", command=lambda: self.txt.delete("1.0", "end")).pack(
            side="right", padx=4)

        # 紀錄
        lf = ttk.LabelFrame(self.root, text="執行紀錄")
        lf.pack(fill="both", expand=True, **pad)
        self.txt = tk.Text(lf, height=16, wrap="word", font=("Consolas", 10),
                           bg="#1e1e1e", fg="#d4d4d4", insertbackground="#fff", relief="flat")
        sb = ttk.Scrollbar(lf, command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.txt.pack(side="left", fill="both", expand=True)
        self.txt.tag_configure("err", foreground="#f48771")
        self.txt.tag_configure("act", foreground="#9cdcfe")

    def fill_from_cfg(self):
        c = self.cfg
        self.v_title.set(c.get("window_title", ""))
        self.v_mode.set(c.get("browser", {}).get("mode", "screen"))
        self.v_lockwin.set(c.get("browser", {}).get("lock_window_size", True))
        self.v_dry.set(c.get("dry_run", False))
        self.v_lv.set(c["levelup"]["enabled"])
        self.v_chest.set(c["chest"]["enabled"])
        self.v_move.set(c["move"]["enabled"])
        self.v_loop.set(c["result"]["enabled"] and c["lobby"]["enabled"] and c["town"]["enabled"])
        self.v_route.set(c["town"].get("learn_route", True))
        self.v_login.set(c.get("login", {}).get("enabled", True))
        self.v_slot.set(int(c.get("charselect", {}).get("slot", 1)))
        self.v_snap.set(c.get("debug_snapshots", True))
        names = [n for n, _ in rb.CHEST_ITEMS]
        order = [x for x in c["chest"].get("priority", []) if x]
        self.chest_order = order + [n for n in names if n not in order]
        self.show_chest()
        self.v_chest_rarity.set(c["chest"].get("prefer_rarity", True))
        pri = c["levelup"].get("priority", [])
        ban = c["levelup"].get("banish", [])
        for n in SKILLS:
            self.v_main[n].set(any(rb.match(n, k) and rb.match(k, n) for k in pri))
            self.v_ban[n].set(not self.v_main[n].get() and
                              any(rb.match(n, k) and rb.match(k, n) for k in ban))
        self.v_until.set(str(c["levelup"].get("target_until_level", 11)))
        self.v_rarity.set(c["levelup"].get("rarity_first", False))
        self.v_autopick.set(c["levelup"].get("auto_after_banish", True))
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
        self.root.after(150, self.poll)

    def set_idle(self):
        self.status.config(text="● 待命", foreground="#666")
        self.b_start.config(state="normal")
        self.b_pause.config(state="disabled", text="‖ 暫停")
        self.b_stop.config(state="disabled")

    def start(self):
        if self.thread and self.thread.is_alive():
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
        rb.log("已停止")

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
        item = self.chest_order.pop(i)
        self.chest_order.insert(j, item)
        self._drag_i = j
        self.show_chest(j)

    def move_chest(self, how):
        sel = self.lb_chest.curselection()
        if not sel:
            return
        i = sel[0]
        item = self.chest_order.pop(i)
        if how == "top":
            j = 0
        elif how == "bottom":
            j = len(self.chest_order)
        else:
            j = min(max(0, i + how), len(self.chest_order))
        self.chest_order.insert(j, item)
        self.show_chest(j)

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
        """quiet=True：開程式時自動檢查，沒新版或出錯都不吵你"""
        uc = dict(rb.DEFAULT_CONFIG["update"])
        uc.update(self.cfg.get("update", {}))
        repo = (uc.get("repo") or "").strip() or rb.DEFAULT_CONFIG["update"]["repo"]
        if quiet and not uc.get("check_on_start", True):
            return
        token = uc.get("token", "")
        if not quiet:
            self.add_log("檢查更新中…")

        def work():
            try:
                info = updater.check(repo, rb.VERSION, token)
            except Exception as e:
                if not quiet:
                    self.root.after(0, lambda m=str(e): messagebox.showwarning(APP_TITLE, f"檢查更新失敗：{m}"))
                    self.q.put(f"檢查更新失敗：{e}")
                return
            if info is None:
                if not quiet:
                    self.q.put(f"已經是最新版 v{rb.VERSION}")
                    self.root.after(0, lambda: messagebox.showinfo(APP_TITLE, f"已經是最新版 v{rb.VERSION}"))
                return
            self.root.after(0, lambda: self.ask_update(info, token))
        threading.Thread(target=work, daemon=True).start()

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
        self.save_cfg(quiet=True)
        self.add_log(f"下載新版 v{info['version']}…")
        last = [-1]

        def prog(done, total):
            pct = int(done * 100 / total)
            if pct // 10 != last[0]:
                last[0] = pct // 10
                self.q.put(f"  下載中 {pct}%")

        def work():
            try:
                tmp, src = updater.download(info, token, prog)
            except Exception as e:
                self.q.put(f"下載失敗：{e}")
                self.root.after(0, lambda m=str(e): messagebox.showwarning(APP_TITLE, f"下載失敗：{m}"))
                return
            self.q.put("下載完成，關閉程式並換成新版…")

            def finish():
                try:
                    updater.apply_and_restart(tmp, src)
                except Exception as e:
                    messagebox.showwarning(APP_TITLE, f"更新失敗：{e}")
                    return
                self.root.destroy()
                os._exit(0)
            self.root.after(500, finish)
        threading.Thread(target=work, daemon=True).start()

    def on_close(self):
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
