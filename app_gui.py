# -*- coding: utf-8 -*-
"""桌面图形界面（CustomTkinter 现代版）：批量抓取、表格编辑、AI 设置、Excel 导出。

基于 CustomTkinter 重构，保留全部原有功能：
- 三种抓取模式（直接 / AI 智能 / 自动）
- 多线程并行抓取、日志与进度显示
- AI 设置（服务商预设、多配置保存/切换、默认配置、测试连接、获取模型列表）
- 表格编辑（双击单元格、右键菜单、多选、选择性导出）
- 自动保存未导出数据
"""

import json
import os
import queue
import threading
import time
import traceback
from datetime import datetime

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

import ai_extractor
import config as config_mod
import exporter
import scraper

# ---------- 全局主题 ----------
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# 中文字体设置：优先使用微软雅黑，中文显示更清晰自然
# 说明：Roboto 等西文字体仅作参考，中文界面下效果不如微软雅黑
try:
    ctk.ThemeManager.theme["CTkFont"]["family"] = "Microsoft YaHei UI"
except Exception:
    pass

# 柔和文字色：浅色用深灰、深色用浅灰，两种主题都清楚，切换时不刺眼
try:
    _tm = ctk.ThemeManager.theme
    for _key in ("CTkLabel", "CTkTextbox", "CTkEntry", "CTkComboBox"):
        if isinstance(_tm.get(_key), dict) and _tm[_key].get("text_color") is not None:
            _tm[_key]["text_color"] = ["#3b4048", "#d7dde3"]
except Exception:
    pass

# 去掉主题切换时“隐藏窗口→改标题栏颜色→再显示窗口”的步骤：
# 那正是切换主题时窗口闪一下、任务栏像重启一样闪烁的原因。
# 这里只保留修改标题栏深浅色的核心逻辑，不再隐藏/重现窗口。
def _smooth_titlebar_color(self, color_mode):
    """改写 CTk._windows_set_titlebar_color：只切换标题栏深浅色，不隐藏窗口。"""
    try:
        import ctypes as _ct
        import sys as _sys
        if not _sys.platform.startswith("win"):
            return
        mode = str(color_mode).lower()
        if mode == "dark":
            value = 1
        elif mode == "light":
            value = 0
        else:
            return
        hwnd = _ct.windll.user32.GetParent(self.winfo_id())
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        DWMWA_USE_IMMERSIVE_DARK_MODE_BEFORE_20H1 = 19
        ok = True
        try:
            ok = _ct.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE,
                _ct.byref(_ct.c_int(value)), _ct.sizeof(_ct.c_int(value))) == 0
        except Exception:
            ok = False
        if not ok:
            try:
                _ct.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE_BEFORE_20H1,
                    _ct.byref(_ct.c_int(value)), _ct.sizeof(_ct.c_int(value)))
            except Exception:
                pass
        try:
            self.update_idletasks()
        except Exception:
            pass
    except Exception:
        pass

for _cls in (ctk.CTk, ctk.CTkToplevel):
    if hasattr(_cls, "_windows_set_titlebar_color"):
        _cls._windows_set_titlebar_color = _smooth_titlebar_color
del _cls

# 数据类别（功能选择器）：决定表格表头与导出表头
CATEGORIES = ["常规", "京东", "淘宝", "拼多多"]

# 每个数据类别对应的表头（第一列固定为“序号”）
CATEGORY_HEADERS = {
    "常规": ["序号", "产品名称", "用途", "设备介绍", "性能特点", "设备参数", "参考图片"],
    "京东": ["序号", "商品名称", "商品详情", "商品参数", "商品价格", "参考图"],
    "淘宝": ["序号", "产品名称", "用途", "设备介绍", "性能特点", "设备参数", "参考图片"],
    "拼多多": ["序号", "产品名称", "用途", "设备介绍", "性能特点", "设备参数", "参考图片"],
}

# 每个数据类别对应的内容字段（与表头一一对应，去掉“序号”）
CATEGORY_FIELDS = {
    "常规": ["产品名称", "用途", "设备介绍", "性能特点", "设备参数", "参考图片"],
    "京东": ["商品名称", "商品详情", "商品参数", "商品价格", "参考图"],
    "淘宝": ["产品名称", "用途", "设备介绍", "性能特点", "设备参数", "参考图片"],
    "拼多多": ["产品名称", "用途", "设备介绍", "性能特点", "设备参数", "参考图片"],
}

# 每个数据类别的列宽（序号 + 内容列）
CATEGORY_WIDTHS = {
    "常规": [80, 230, 250, 290, 290, 290, 220],
    "京东": [80, 270, 320, 340, 150, 230],
    "淘宝": [80, 230, 250, 290, 290, 290, 220],
    "拼多多": [80, 230, 250, 290, 290, 290, 220],
}

# 跨类别取值兜底：语义等价字段（用于行类别与当前表头类别不一致时取值）
_FIELD_SYNONYMS = {
    "产品名称": ["产品名称", "商品名称"],
    "商品名称": ["商品名称", "产品名称"],
    "用途": ["用途"],
    "设备介绍": ["设备介绍", "商品详情", "商品介绍"],
    "商品详情": ["商品详情", "设备介绍", "商品介绍"],
    "性能特点": ["性能特点", "商品卖点", "商品特点"],
    "设备参数": ["设备参数", "商品参数", "规格参数"],
    "商品参数": ["商品参数", "设备参数", "规格参数"],
    "商品价格": ["商品价格", "价格"],
    "参考图片": ["参考图片", "参考图"],
    "参考图": ["参考图", "参考图片"],
}


def _norm_category(cat):
    """把任意类别写法归一化为合法类别（兼容旧的“京东类/普通类”）。"""
    cat = str(cat or "").strip()
    if cat == "京东类":
        return "京东"
    if cat in ("普通类", ""):
        return "常规"
    return cat if cat in CATEGORIES else "常规"


def _row_field_value(row, field):
    """按当前表头字段取值；字段不存在时用语义等价字段兜底。"""
    for k in _FIELD_SYNONYMS.get(field, [field]):
        v = row.get(k)
        if str(v or "").strip():
            return v
    return ""
# 表格配色：深浅两套色板，跟随外观主题
_TREE_PALETTES = {
    "dark": {
        "field": "#242424", "fg": "#d7dde3",
        "sel": "#454545", "sel_unfocus": "#333333", "sel_fg": "#ffffff",
        "border": "#242424",
        "head_bg": "#2e2e2e", "head_fg": "#c4ccd6",
        "head_active": "#4a4a4a", "head_active_fg": "#ffffff",
    },
    "light": {
        "field": "#ebebeb", "fg": "#1f2933",
        "sel": "#cfcfcf", "sel_unfocus": "#dedede", "sel_fg": "#222222",
        "border": "#ebebeb",
        "head_bg": "#e1e1e1", "head_fg": "#334155",
        "head_active": "#d7d7d7", "head_active_fg": "#222222",
    },
}


def _present(win):
    """窗口构建完成后一次性显示，避免“先小窗再慢慢展开”。

    原因：Tk 顶层窗口默认创建即映射显示，控件逐个加入时会反复调整
    窗口尺寸，在 Windows 上就表现为左上角小窗慢慢展开。
    这里改为：构建期间隐藏 -> 布局完成 -> 以最终尺寸一次性显示。
    """
    try:
        import re as _re
        cur = win.geometry()          # 形如 "WxH+X+Y" 或 "+X+Y"
        m = _re.search(r"[+-]\d+[+-]\d+$", cur)
        pos = m.group(0) if m else ""
        win.withdraw()
        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        if w > 10 and h > 10:
            try:
                sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
                w = min(w, max(sw - 80, 300))
                h = min(h, max(sh - 120, 220))
            except Exception:
                pass
            geo = f"{int(w)}x{int(h)}" + pos
            win.geometry(geo)
        win.deiconify()
        win.update_idletasks()
    except Exception:
        try:
            win.deiconify()
        except Exception:
            pass


def _setup_tree_style(mode="dark"):
    """按外观模式配置 ttk.Treeview 样式（浅色/深色两套色板）。"""
    if str(mode).lower().startswith("light"):
        p = _TREE_PALETTES["light"]
    else:
        p = _TREE_PALETTES["dark"]
    try:
        style = ttk.Style()
        style.theme_use("clam")
        # 去掉 1px 边框，使 Treeview 与 CustomTkinter 圆角容器过渡自然
        style.layout("CTk.Treeview", [
            ("Treeview.field", {"sticky": "nswe", "border": "0",
                                "children": [("Treeview.padding",
                                              {"sticky": "nswe",
                                               "children": [("Treeview.treearea",
                                                             {"sticky": "nswe"})]})]})
        ])
        style.layout("CTk.Treeview.Heading", [
            ("Treeheading.cell", {"sticky": "nswe"}),
            ("Treeheading.border", {"sticky": "nswe",
                                    "children": [("Treeheading.padding",
                                                  {"sticky": "nswe",
                                                   "children": [("Treeheading.image",
                                                                 {"side": "right", "sticky": ""}),
                                                                ("Treeheading.text",
                                                                 {"sticky": "we"})]})]})
        ])
        style.configure("CTk.Treeview",
                        background=p["field"],
                        fieldbackground=p["field"],
                        foreground=p["fg"],
                        bordercolor=p["border"],
                        lightcolor=p["border"],
                        darkcolor=p["border"],
                        rowheight=42,
                        borderwidth=0,
                        relief="flat",
                        font=("Microsoft YaHei UI", 12))
        style.map("CTk.Treeview",
                  background=[("selected", p["sel"]),
                              ("focus", p["sel"]),
                              ("!focus", p["sel_unfocus"])],
                  foreground=[("selected", p["sel_fg"]),
                              ("focus", p["sel_fg"]),
                              ("!focus", p["fg"])],
                  fieldbackground=[("selected", p["sel"]),
                                   ("focus", p["sel"]),
                                   ("!focus", p["sel_unfocus"])],
                  bordercolor=[("selected", p["border"]),
                               ("focus", p["border"]),
                               ("!focus", p["border"])])
        style.configure("CTk.Treeview.Heading",
                        background=p["head_bg"],
                        foreground=p["head_fg"],
                        relief="flat",
                        borderwidth=0,
                        padding=(10, 8),
                        font=("Microsoft YaHei UI", 12, "bold"))
        style.map("CTk.Treeview.Heading",
                  background=[("active", p["head_active"])],
                  foreground=[("active", p["head_active_fg"])])
    except Exception:
        pass




class ProductApp:
    """主窗口。"""

    def __init__(self, root):
        self.root = root
        self.cfg = config_mod.load_config()
        self._config_auto_fixed = config_mod.auto_apply_saved_provider(self.cfg)
        self._default_applied = config_mod.apply_default_provider(self.cfg)
        self.q = queue.Queue()
        self.rows = []          # 每行: {产品名称, 用途, ..., _url, _image_path, _data_category}
        self.working = False
        self._capture_buttons = []
        # 抓取暂停控制：set()=运行，clear()=暂停。worker 线程在取下一个地址前检查
        self._pause_ev = threading.Event()
        self._pause_ev.set()
        self.category_var = tk.StringVar(value="常规")   # 数据类别选择器
        self._build_ui()
        self._out_dir = config_mod.get_output_dir(self.cfg)
        self._autosave_path = os.path.join(self._out_dir, "未导出数据.json")
        fixed, name = self._config_auto_fixed
        if fixed:
            self.log(f"检测到当前 AI 配置缺失，已自动应用已保存配置「{name}」，可直接使用")
        applied, dname = self._default_applied
        if applied:
            self.log(f"已应用默认配置「{dname}」")
        self._restore_session()
        self.root.after(100, self._poll)
        if (self.cfg["ai"].get("api_key") or "").strip() and (self.cfg["ai"].get("base_url") or "").strip():
            self._refresh_models_main()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- 界面 ----------
    def _adapt_window_size(self):
        """Adapt window size to screen resolution and DPI scaling so the log area
        always stays visible, even on small screens (e.g. 1366x768 Win10)."""
        try:
            scale = float(self.root._get_window_scaling() or 1.0)
        except Exception:
            scale = 1.0
        try:
            import ctypes

            class _RECT(ctypes.Structure):
                _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                            ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

            rect = _RECT()
            # SPI_GETWORKAREA: usable area excluding the taskbar, so the window
            # never ends up hidden behind the taskbar on small screens
            if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
                phys_w = rect.right - rect.left
                phys_h = rect.bottom - rect.top
            else:
                phys_w = int(ctypes.windll.user32.GetSystemMetrics(0))
                phys_h = int(ctypes.windll.user32.GetSystemMetrics(1))
            if phys_w <= 0 or phys_h <= 0:
                raise OSError
        except Exception:
            # fallback: tkinter logical screen size
            phys_w = self.root.winfo_screenwidth()
            phys_h = self.root.winfo_screenheight()
        logical_w = max(phys_w / scale, 720.0)
        logical_h = max(phys_h / scale, 460.0)
        target_w, target_h, self._compact, self._tree_rows = self._compute_window(logical_w, logical_h)
        self.root.geometry(f"{target_w}x{target_h}")
        # never force a minimum larger than what actually fits on this screen
        self.root.minsize(min(1000, target_w), min(620, target_h))
        return target_w, target_h

    def _compute_window(self, logical_w, logical_h):
        """Pure sizing logic: returns (width, height, compact_tier, tree_rows)."""
        margin = 60  # small edge for window borders / breathing room
        target_w = int(min(1280, max(logical_w - margin, 720)))
        target_h = int(min(820, max(logical_h - margin, 460)))
        if target_h < 620:
            compact = 2      # very small screens: hide subtitle, shrink URL/log/table
        elif target_h < 740:
            compact = 1      # medium screens: shrink a bit
        else:
            compact = 0      # normal / large screens: full layout
        tree_rows = {0: 10, 1: 8, 2: 5}.get(compact, 8)
        return target_w, target_h, compact, tree_rows

    def _apply_saved_appearance(self):
        """启动时应用 config 中保存的外观主题。"""
        en = str(self.cfg.get("appearance") or "dark").lower()
        if en not in ("system", "light", "dark"):
            en = "dark"
        self._current_appearance = en
        try:
            ctk.set_appearance_mode(en)
        except Exception:
            pass
        self._setup_tree_tracker()

    def _on_appearance_change(self, value):
        """外观主题切换：系统 / 浅色 / 深色。"""
        mode_map = {"系统": "system", "浅色": "light", "深色": "dark"}
        en = mode_map.get(value) or "dark"
        self.cfg["appearance"] = en
        try:
            config_mod.save_config(self.cfg)
        except Exception:
            pass
        try:
            ctk.set_appearance_mode(en)
        except Exception:
            pass
        self._current_appearance = en
        self._setup_tree_tracker()
        # 表格由 ttk 绘制，需要按当前主题重建颜色
        try:
            _setup_tree_style(ctk.get_appearance_mode())
        except Exception:
            pass
        self.log(f"外观主题已切换为：{en}")

    def _setup_tree_tracker(self):
        """系统模式下自动跟随系统深浅色更新表格颜色；手动选浅/深色时取消跟随。"""
        try:
            from customtkinter.windows.widgets.appearance_mode.appearance_mode_tracker import \
                AppearanceModeTracker as _AMT
        except Exception:
            return
        cb = getattr(self, "_tree_tracker_cb", None)
        if cb is not None:
            try:
                _AMT.remove(cb)
            except Exception:
                pass
        self._tree_tracker_cb = None
        if str(getattr(self, "_current_appearance", "")).lower() != "system":
            return

        def _on_system_mode(_mode):
            try:
                _setup_tree_style(ctk.get_appearance_mode())
            except Exception:
                pass

        self._tree_tracker_cb = _on_system_mode
        try:
            _AMT.add(_on_system_mode)
            _on_system_mode(ctk.get_appearance_mode())
        except Exception:
            pass

    def _build_ui(self):
        self._apply_saved_appearance()
        self.root.title("产品信息自动抓取工具")
        self._adapt_window_size()

        # 顶部标题横幅
        banner = ctk.CTkFrame(self.root, corner_radius=0, fg_color="transparent")
        banner.pack(fill="x", padx=14, pady=(12, 2))
        ctk.CTkLabel(banner, text="产品信息自动抓取工具",
                     font=("Microsoft YaHei UI", 20, "bold")).pack(side="left")
        if getattr(self, "_compact", 0) < 2:
            ctk.CTkLabel(banner, text="  输入产品详情页地址 → 一键抓取 → 导出 Excel",
                         text_color=("gray60", "gray40"),
                         font=("Microsoft YaHei UI", 11)).pack(side="left", padx=(14, 0))
        # 外观主题切换（系统 / 浅色 / 深色）
        self.theme_selector = ctk.CTkSegmentedButton(
            banner, values=["系统", "浅色", "深色"],
            command=self._on_appearance_change,
            font=("Microsoft YaHei UI", 11), height=26,
            corner_radius=8, border_width=0,
            fg_color=("gray92", "gray13"),
            selected_color=("#cfcfcf", "#454545"),
            selected_hover_color=("#bdbdbd", "#525252"),
            unselected_color=("gray92", "gray13"),
            unselected_hover_color=("#e0e0e0", "#2a2a2a"),
            text_color=("#1f2933", "#d7dde3"))
        self.theme_selector.pack(side="right")
        ctk.CTkLabel(banner, text="外观：",
                     font=("Microsoft YaHei UI", 11),
                     text_color=("gray25", "gray75")).pack(side="right", padx=(0, 6))
        self.theme_selector.set({"dark": "深色", "light": "浅色", "system": "系统"}.get(
            getattr(self, "_current_appearance", "dark"), "深色"))

        # 1. 地址输入区
        top = ctk.CTkFrame(self.root, corner_radius=10)
        top.pack(fill="x", padx=14, pady=(8, 6))
        ctk.CTkLabel(top, text="1. 输入产品详情页地址（每行一个，支持批量）",
                     font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w", padx=12, pady=(10, 4))
        url_h = {0: 96, 1: 72, 2: 56}.get(getattr(self, "_compact", 0), 72)
        self.url_text = ctk.CTkTextbox(top, height=url_h, font=("Microsoft YaHei UI", 12),
                                       wrap="word", border_width=1)
        self.url_text.pack(fill="x", padx=12, pady=(0, 6))
        btns = ctk.CTkFrame(top, fg_color="transparent")
        btns.pack(fill="x", padx=12, pady=(0, 10))
        ctk.CTkButton(btns, text="AI 设置", width=88, command=self._open_settings).pack(side="left")
        b_direct = ctk.CTkButton(btns, text="直接抓取", width=92, command=lambda: self._on_capture("direct"))
        b_direct.pack(side="left", padx=8)
        b_ai = ctk.CTkButton(btns, text="AI 智能抓取", width=110, command=lambda: self._on_capture("ai"))
        b_ai.pack(side="left", padx=8)
        b_auto = ctk.CTkButton(btns, text="自动抓取（推荐）", width=130,
                               fg_color="#2e7d32", hover_color="#1b5e20",
                               command=lambda: self._on_capture("auto"))
        b_auto.pack(side="left", padx=8)
        self._capture_buttons = [b_direct, b_ai, b_auto]
        self.pause_btn = ctk.CTkButton(btns, text="暂停", width=76,
                                       command=self._on_toggle_pause,
                                       state="disabled")
        # 记住默认配色，恢复时还原（customtkinter 不接受 fg_color=None，透明要用 'transparent'）
        self._pause_btn_fg = self.pause_btn.cget("fg_color")
        self._pause_btn_hover = self.pause_btn.cget("hover_color")
        self.pause_btn.pack(side="left", padx=(8, 8))
        ctk.CTkButton(btns, text="清空地址", width=88, command=self._clear_urls).pack(side="left", padx=8)
        ctk.CTkButton(btns, text="京东登录", width=88, command=self._login_jd).pack(side="left", padx=(8, 0))
        ctk.CTkLabel(btns, text="数据类别：").pack(side="left", padx=(16, 4))
        self.category_menu = ctk.CTkOptionMenu(
            btns, values=CATEGORIES, variable=self.category_var, width=108, height=28,
            command=self._apply_category,
            font=("Microsoft YaHei UI", 11),
            dropdown_font=("Microsoft YaHei UI", 11))
        self.category_menu.pack(side="left", padx=4)
        ctk.CTkLabel(btns, text="模型：").pack(side="left", padx=(16, 4))
        self.model_box_var = tk.StringVar(value=str(self.cfg["ai"].get("model", "")))
        self.model_box = ctk.CTkComboBox(btns, width=230, variable=self.model_box_var,
                                         values=list(ai_extractor.COMMON_MODELS), state="normal",
                                         command=self._on_model_changed)  # 下拉选中立即生效
        self.model_box.pack(side="left", padx=4)
        ctk.CTkButton(btns, text="刷新模型", width=88, command=lambda: self._refresh_models_main(True)).pack(side="left", padx=(4, 6))
        self.model_box.bind("<Return>", self._on_model_changed)
        self.model_box.bind("<FocusOut>", self._on_model_changed)
        ctk.CTkLabel(btns, text="自动抓取 = 网页解析优先，缺字段时由 AI 补全",
                     text_color=("gray50", "gray50"),
                     font=("Microsoft YaHei UI", 10)).pack(side="left", padx=10)
        # 2. 表格区
        mid = ctk.CTkFrame(self.root, corner_radius=10, fg_color="transparent")
        mid.pack(fill="both", expand=True, padx=14, pady=6)
        ctk.CTkLabel(mid, text="2. 产品信息表（双击单元格可编辑，右键更多操作；可 Ctrl/Shift 多选行后选择性导出）",
                     font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w", padx=12, pady=(10, 4))
        wrap = ctk.CTkFrame(mid, corner_radius=10, border_width=0,
                              fg_color=("#ebebeb", "#242424"))
        wrap.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        _setup_tree_style(ctk.get_appearance_mode())
        _init_headers = CATEGORY_HEADERS[self.category_var.get()]
        _init_widths = CATEGORY_WIDTHS[self.category_var.get()]
        self.tree = ttk.Treeview(wrap, columns=[f"c{i}" for i in range(len(_init_headers))],
                                  show="headings", selectmode="extended",
                                  style="CTk.Treeview", takefocus="",
                                  height=getattr(self, "_tree_rows", 10))   # 支持 Ctrl / Shift 多选
        for i, h in enumerate(_init_headers):
            self.tree.heading(f"c{i}", text=h)
            self.tree.column(f"c{i}", width=_init_widths[i], anchor="w", stretch=(i > 0))
        vsb = ctk.CTkScrollbar(wrap, orientation="vertical", command=self.tree.yview,
                              width=18, corner_radius=9,
                              border_spacing=0,
                              button_color=("#a8a8a8", "#4a4a4a"),
                              button_hover_color=("#8a8a8a", "#606060"))
        hsb = ctk.CTkScrollbar(wrap, orientation="horizontal", command=self.tree.xview,
                               height=18, corner_radius=9,
                               border_spacing=0,
                               button_color=("#a8a8a8", "#4a4a4a"),
                               button_hover_color=("#8a8a8a", "#606060"))
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew", padx=3, pady=(12, 3))
        vsb.grid(row=0, column=1, sticky="ns", pady=6, padx=(0, 4))
        hsb.grid(row=1, column=0, sticky="ew", padx=6, pady=(3, 0))
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Button-3>", self._on_right_click)

        op = ctk.CTkFrame(mid, fg_color="transparent")
        op.pack(fill="x", padx=12, pady=(6, 12))
        for text, cmd in [
            ("＋ 添加行", lambda: self._add_row(None)),
            ("编辑整行", self._edit_row),
            ("－ 删除行", self._delete_row),
            ("↑ 上移", lambda: self._move(-1)),
            ("↓ 下移", lambda: self._move(1)),
            ("清空表格", self._clear_rows),
        ]:
            ctk.CTkButton(op, text=text, width=86, height=30, command=cmd).pack(side="left", padx=4)
        ctk.CTkButton(op, text="打开输出目录", width=110, height=30,
                      command=self._open_out).pack(side="right", padx=4)
        ctk.CTkButton(op, text="导出全部", width=104, height=30,
                      fg_color="#1565c0", hover_color="#0d47a1",
                      command=lambda: self._on_export(False)).pack(side="right", padx=4)
        ctk.CTkButton(op, text="导出选中行", width=104, height=30,
                      fg_color="#1565c0", hover_color="#0d47a1",
                      command=lambda: self._on_export(True)).pack(side="right", padx=4)

        # 3. 日志区
        bottom = ctk.CTkFrame(self.root, corner_radius=10)
        bottom.pack(fill="x", padx=14, pady=(6, 8))
        ctk.CTkLabel(bottom, text="运行日志",
                     font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w", padx=12, pady=(8, 4))
        log_h = {0: 170, 1: 130, 2: 100}.get(getattr(self, "_compact", 0), 130)
        self.log_text = ctk.CTkTextbox(bottom, height=log_h, wrap="word", border_width=1,
                                       font=("Microsoft YaHei UI", 12), fg_color=("#ffffff", "#1e1e1e"),
                                       text_color=("#1f2933", "#d4d4d4"))
        self.log_text.pack(fill="x", padx=12, pady=(0, 4))
        self.progress = ctk.CTkProgressBar(bottom, mode="indeterminate", height=8)
        self.progress.pack(fill="x", padx=12, pady=(0, 10))
        self.progress.set(0)
        self.status = ctk.CTkLabel(self.root, text="就绪", anchor="w",
                                   font=("Microsoft YaHei UI", 11))
        self.status.pack(fill="x", padx=14, pady=(0, 8))

    # ---------- 日志 / 状态 ----------
    def log(self, msg):
        self.q.put(("log", f"[{datetime.now():%H:%M:%S}] {msg}"))

    def _append_log(self, msg):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _set_busy(self, busy):
        for b in self._capture_buttons:
            b.configure(state="disabled" if busy else "normal")
        # 暂停/继续按钮：只在抓取任务进行中可用
        pb = getattr(self, "pause_btn", None)
        if pb is not None:
            pb.configure(state="normal" if busy else "disabled")
        if busy:
            self.progress.start()
        else:
            self.progress.stop()
            self.progress.set(0)
    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "done":
                    self.working = False
                    self._set_busy(False)
                    self.status.configure(text="就绪")
                elif kind == "exported":
                    self.working = False
                    self._set_busy(False)
                    self.status.configure(text="就绪")
                    path = payload
                    if messagebox.askyesno("导出成功", f"已导出：\n{path}\n\n是否打开所在文件夹？"):
                        self._open_dir(os.path.dirname(path))
                elif kind == "main_models":
                    models, err = payload
                    self._fetching_main_models_flag = False
                    if err:
                        self.log(f"获取模型列表失败：{err}")
                    else:
                        self.model_box.configure(values=models)
                        if not self.model_box_var.get().strip() and models:
                            self.model_box_var.set(models[0])
                        self.log(f"模型列表已更新，共 {len(models)} 个，可直接下拉选择")
                    self.status.configure(text="就绪")
                elif kind == "capture_all_done":
                    sorted_rows, total = payload
                    self.rows.extend(sorted_rows)
                    self._auto_switch_category(sorted_rows)
                    self._refresh_table()
                    self._autosave()
                    self.log(f"全部完成，共 {total} 条。请检查表格后导出。")
                elif kind == "warn":
                    self._set_busy(False)
                    self.status.configure(text="就绪")
                    messagebox.showwarning("提示", payload)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _run_async(self, fn, status_text="正在抓取…"):
        if self.working:
            self.status.configure(text="正在执行任务，请稍候…")
            self.log("提示：上一个任务仍在执行中，请稍候…")
            return
        self.working = True
        self._set_busy(True)
        self.status.configure(text=status_text)
        threading.Thread(target=self._worker, args=(fn,), daemon=True).start()

    def _worker(self, fn):
        try:
            fn()
        except Exception as exc:
            self.log("发生错误：" + str(exc))
            self.log(traceback.format_exc())
        finally:
            self.working = False   # 关键：任务结束必须复位忙碌状态
            self.q.put(("done", None))

    # ---------- 抓取 ----------
    def _on_model_changed(self, event=None):
        """主界面直接切换模型：下拉选中/回车/失焦即保存生效。"""
        # CustomTkinter 下拉选中时会把选中的值作为字符串传给 command，
        # 事件对象不是字符串，此时回退读取下拉框当前变量
        model = str(event).strip() if isinstance(event, str) else self.model_box_var.get().strip()
        if not model:
            return
        if model == self.cfg["ai"].get("model", ""):
            return
        self.cfg["ai"]["model"] = model
        try:
            config_mod.save_config(self.cfg)
            self.log(f"已切换模型：{model}，后续抓取将使用该模型")
        except Exception as exc:
            self.log(f"模型切换保存失败：{exc}")

    def _refresh_models_main(self, force=False):
        """填充主界面模型下拉框。

        - force=True（点“刷新模型”按钮）：总是联网获取最新列表；
        - force=False（启动时自动调用）：优先用本地缓存（秒开、不联网），
          缓存过期或缺失时才后台联网获取。
        """
        if getattr(self, "_fetching_main_models_flag", False):
            self.log("正在获取模型列表中，请稍候…")
            return
        if not (self.cfg["ai"].get("api_key") or "").strip():
            self.log("提示：未配置 AI API Key，无法获取模型列表（可在 AI 设置中配置）")
            return
        if not force:
            cached = ai_extractor.load_models_cache(self.cfg)
            if cached:
                try:
                    self.model_box.configure(values=cached)
                    if not self.model_box_var.get().strip() and cached:
                        self.model_box_var.set(cached[0])
                except Exception:
                    pass
                if self.model_box_var.get() and self.model_box_var.get() not in cached:
                    self.model_box_var.set(cached[0])
                self.log("已载入模型列表（本地缓存，共 %d 个），可直接下拉选择" % len(cached))
                return
        self._fetching_main_models_flag = True
        self.status.configure(text="正在获取模型列表…")
        threading.Thread(target=self._do_refresh_main_models, daemon=True).start()

    def _do_refresh_main_models(self):
        try:
            models = ai_extractor.fetch_models(self.cfg)
            self.q.put(("main_models", (models, None)))
        except Exception as exc:
            self.q.put(("main_models", (None, str(exc))))

    def _clear_urls(self):
        self.url_text.delete("1.0", "end")

    def _login_jd(self):
        """打开可见浏览器完成京东登录，成功后保存登录状态供后续抓取复用。"""
        if self.working:
            self.log("提示：正在执行任务，请稍后再登录京东。")
            return
        self._run_async(self._do_login_jd, status_text="等待京东登录…")

    def _do_login_jd(self):
        self.log("正在打开京东登录窗口，请在弹出的浏览器中扫码或账号登录…")
        try:
            ok = scraper.login_jd(self.cfg, on_log=self.log, timeout=300)
            if ok:
                self.log("京东登录成功，登录状态已保存，后续抓取自动复用该登录")
            else:
                self.log("京东登录未完成或超时，未保存新的登录状态")
        except Exception as exc:
            self.log(f"京东登录失败：{exc}")

    def _on_capture(self, mode):
        urls = [u.strip() for u in self.url_text.get("1.0", "end").splitlines() if u.strip()]
        if not urls:
            messagebox.showwarning("提示", "请先输入产品详情页地址（每行一个）")
            return
        self._run_async(lambda: self._capture_all(urls, mode))

    def _on_toggle_pause(self):
        """暂停/继续抓取：暂停后正在处理的条目跑完即停，不再领取新地址。"""
        ev = getattr(self, "_pause_ev", None)
        if ev is None:
            return
        if ev.is_set():
            ev.clear()
            self.pause_btn.configure(text="继续", fg_color="#b26a00", hover_color="#8f5300")
            self.status.configure(text="已暂停（当前条目完成后暂停）")
            self.log("已暂停：正在完成的条目结束后暂停，点击『继续』恢复")
        else:
            ev.set()
            self.pause_btn.configure(text="暂停", fg_color=self._pause_btn_fg,
                                     hover_color=self._pause_btn_hover)
            self.status.configure(text="正在抓取…")
            self.log("已恢复抓取")

    def _capture_all(self, urls, mode):
        total = len(urls)
        self.log(f"开始 {mode} 模式抓取，共 {total} 个地址…")
        jd_urls = [u for u in urls if scraper.is_jd_url(u)]
        if jd_urls:
            _st, _n = scraper.jd_session_status(self.cfg)
            if _st == "valid":
                self.log(f"已检测到京东登录状态（{_n} 个有效 Cookie），抓取京东商品时将自动复用")
            elif _st == "expired":
                self.log("检测到京东登录状态已过期，请点击『京东登录』重新扫码一次（会覆盖保存）")
            else:
                self.log("提示：未检测到京东登录状态；若京东商品抓不到数据，请先点击『京东登录』扫码一次")
        workers = max(1, int(self.cfg["scraper"].get("max_workers", 4)))

        # 新任务默认处于运行状态（避免复用上次残留的暂停状态）
        self._pause_ev.set()
        self.pause_btn.configure(text="暂停", fg_color=self._pause_btn_fg,
                                     hover_color=self._pause_btn_hover)

        from queue import Queue, Empty
        q = Queue()
        for item in enumerate(urls, 1):
            q.put(item)
        results = {}
        lock = threading.Lock()
        done = [0]
        self.log(f"并行数 {workers}，抓取期间可随时点击『暂停』（正在处理的条目跑完后暂停）")

        def worker():
            while True:
                # 暂停时阻塞在这里，直到用户点击『继续』
                self._pause_ev.wait()
                try:
                    item = q.get_nowait()
                except Empty:
                    return
                i, url = item
                try:
                    row = self._capture_one(url, mode, i)
                    with lock:
                        results[i] = row
                except Exception as exc:
                    self.log(f"{url} 失败：{exc}")
                    row = self._empty_row(scraper.data_category(url))
                    row["_url"] = url
                    with lock:
                        results[i] = row
                finally:
                    q.task_done()
                    with lock:
                        done[0] += 1
                        if done[0] == 1 or done[0] % 5 == 0 or done[0] == total:
                            self.log(f"进度：{done[0]}/{total}")

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
        for t in threads:
            t.start()
        # 等待全部完成；暂停期间主线程同样等待（不占用 UI 线程）
        while True:
            self._pause_ev.wait()
            if q.empty() and not any(t.is_alive() for t in threads):
                break
            time.sleep(0.25)
        for t in threads:
            t.join(timeout=1)
        sorted_rows = [results.get(i) for i in range(1, total + 1)]
        for i in range(1, total + 1):
            if i not in results:
                r = self._empty_row(scraper.data_category(urls[i - 1]))
                r["_url"] = urls[i - 1]
                sorted_rows[i - 1] = r
        self.q.put(("capture_all_done", (sorted_rows, total)))

    def _capture_one(self, url, mode, index):
        import log_manager
        _t0 = time.time()
        _log_buf = []

        def _tee(msg):
            _log_buf.append(str(msg))
            self.log(msg)

        result, meta = {}, {"url": url, "image_url": ""}
        _cat = scraper.data_category(url)
        _err = ""
        try:
            if mode in ("direct", "auto"):
                result, meta = scraper.scrape(url, self.cfg, on_log=_tee)
            if mode == "ai":
                html, final_url = scraper.fetch_page_html(url, self.cfg, on_log=_tee)
                img_urls = scraper.collect_page_product_images(html, final_url)
                cat = scraper.data_category(url, final_url)
                result = ai_extractor.ai_extract(url, scraper.html_to_text(html), self.cfg,
                                                 on_log=_tee, context={"来源网址": url},
                                                 image_urls=img_urls, category=cat)
                meta["final_url"] = final_url
                if not (result.get("参考图片") or result.get("参考图") or "").strip():
                    if img_urls:
                        result["参考图片"] = "\n".join(img_urls)
                        result["参考图"] = result["参考图片"]
                        meta["image_urls"] = img_urls
                        meta["image_url"] = img_urls[0]
            _cat = scraper.data_category(url, meta.get("final_url") or "")
            if mode == "auto":
                missing = scraper.missing_fields(result, _cat)
                if missing:
                    if (self.cfg["ai"].get("api_key") or "").strip():
                        self.log("AI 补全缺失字段：" + "、".join(missing))
                        try:
                            text, final_url = scraper.fetch_page_text(url, self.cfg, on_log=_tee)
                            imgs = [ln.strip() for ln in (result.get("参考图片") or result.get("参考图") or "").splitlines()
                                    if ln.strip()]
                            # 参数/详情区域为图片时，把图片一并交给 AI 视觉识别文字
                            param_imgs = meta.get("param_image_urls") or []
                            detail_imgs = meta.get("detail_image_urls") or []
                            extra_imgs = list(param_imgs)
                            for _u in detail_imgs:
                                if _u not in extra_imgs:
                                    extra_imgs.append(_u)
                            if extra_imgs:
                                imgs = (extra_imgs + imgs)[:6]
                            ai_row = ai_extractor.ai_extract(url, text, self.cfg, on_log=_tee,
                                                             existing={k: result.get(k) for k in missing},
                                                             context={"来源网址": url},
                                                             image_urls=imgs or None,
                                                             category=_cat)
                            for k in missing:
                                if ai_row.get(k):
                                    result[k] = ai_row[k]
                            # 京东字段与通用字段互相同步
                            for gk, jk in (("产品名称", "商品名称"), ("设备介绍", "商品详情"),
                                           ("设备参数", "商品参数"), ("参考图片", "参考图")):
                                if not (result.get(gk) or "").strip() and (result.get(jk) or "").strip():
                                    result[gk] = result[jk]
                                elif not (result.get(jk) or "").strip() and (result.get(gk) or "").strip():
                                    result[jk] = result[gk]
                            if not (result.get("参考图片") or "").strip() and (ai_row.get("参考图片") or "").strip():
                                result["参考图片"] = ai_row["参考图片"]
                            if not (result.get("参考图") or "").strip() and (ai_row.get("参考图") or "").strip():
                                result["参考图"] = ai_row["参考图"]
                        except Exception as exc:
                            # 网页解析已经成功，AI 补全失败不应让整行数据丢失
                            self.log(f"AI 补全失败（已保留网页解析结果）：{exc}")
                    else:
                        self.log("提示：未配置 AI，缺失字段请在表格中手动补充。")
            if not (result.get("产品名称") or result.get("商品名称") or "").strip():
                result["产品名称"] = url
                result["商品名称"] = url
            result["_data_category"] = _cat
            self._handle_image(result, meta, index)
            return result
        except Exception as exc:
            _err = str(exc)
            raise
        finally:
            try:
                log_manager.log_scrape(url, _cat, mode, ok=not _err,
                                       duration_ms=(time.time() - _t0) * 1000,
                                       row=result, error=_err, logs=_log_buf,
                                       cfg=self.cfg)
            except Exception:
                pass

    def _handle_image(self, row, meta, index):
        """参考图片：默认只保留最多 3 个产品图片链接（不下载），导出快；
        若在配置中开启 download_images 才下载首图用于嵌入 Excel。"""
        urls_text = (row.get("参考图片") or "").strip() or (meta.get("image_url") or "").strip()
        row["_url"] = meta.get("url") or meta.get("final_url") or ""
        row["_image_path"] = ""
        if not urls_text:
            row["参考图片"] = ""
            return
        if urls_text.startswith("http") and self.cfg["export"].get("download_images", False):
            first = urls_text.splitlines()[0].strip()
            path = scraper.download_image(first, os.path.join(self._out_dir, "images"), index,
                                          row.get("产品名称") or "", self.cfg)
            if path:
                row["_image_path"] = path
                row["参考图片"] = path
                row["参考图"] = path
                self.log(f"图片已下载：{os.path.basename(path)}")
                return
            self.log("图片下载失败，已保留图片链接")
        img_text = scraper.format_reference_images(urls_text)
        row["参考图片"] = img_text
        row["参考图"] = img_text

    def _auto_switch_category(self, rows):
        """抓取完成后，若所有新行属于同一数据类别，自动切换表头类别。"""
        cats = {_norm_category(r.get("_data_category") or r.get("数据类别") or "常规")
                for r in rows}
        if len(cats) == 1:
            cat = cats.pop()
            if cat in CATEGORIES and cat != _norm_category(self.category_var.get()):
                self.category_var.set(cat)
                self._apply_category()
                self.log(f"已自动切换到数据类别：{cat}")

    # ---------- 表格 ----------
    @staticmethod
    def _short(v, n=120):
        text = str(v or "").replace("\n", " ")
        return text if len(text) <= n else text[:n] + "…"

    def _apply_category(self, value=None):
        """数据类别切换：更新表头与列宽，并刷新表格内容。"""
        cat = _norm_category(self.category_var.get())
        headers = CATEGORY_HEADERS[cat]
        widths = CATEGORY_WIDTHS[cat]
        cols = [f"c{i}" for i in range(len(headers))]
        self.tree.configure(columns=cols)
        for i, h in enumerate(headers):
            self.tree.heading(f"c{i}", text=h)
            self.tree.column(f"c{i}", width=widths[i], anchor="w", stretch=(i > 0))
        self._refresh_table()

    def _refresh_table(self):
        self.tree.delete(*self.tree.get_children())
        fields = CATEGORY_FIELDS.get(_norm_category(self.category_var.get()), CATEGORY_FIELDS["常规"])
        for idx, row in enumerate(self.rows, 1):
            vals = [idx] + [self._short(_row_field_value(row, f)) for f in fields]
            self.tree.insert("", "end", values=vals, iid=str(idx - 1))

    def _selected_index(self):
        sel = self.tree.selection()
        if not sel:
            return None
        try:
            return int(sel[0])
        except (ValueError, TypeError):
            return None
    @staticmethod
    def _empty_row(category="常规"):
        """按数据类别创建空行（含该类别全部字段）。"""
        cat = _norm_category(category)
        row = {f: "" for f in CATEGORY_FIELDS[cat]}
        row["_data_category"] = cat
        row["_url"] = ""
        row["_image_path"] = ""
        return row

    def _add_row(self, pos):
        cat = _norm_category(self.category_var.get())
        row = self._empty_row(cat)
        row[CATEGORY_FIELDS[cat][0]] = f"新产品{len(self.rows) + 1}"
        if pos is None:
            sel = self._selected_index()
            pos = (sel + 1) if sel is not None else len(self.rows)
        self.rows.insert(pos, row)
        self._refresh_table()
        self._autosave()
        self.tree.selection_set(str(pos))
        self.tree.focus(str(pos))

    def _delete_row(self):
        idx = self._selected_index()
        if idx is None:
            messagebox.showwarning("提示", "请先选择一行")
            return
        del self.rows[idx]
        self._refresh_table()
        self._autosave()

    def _move(self, delta):
        idx = self._selected_index()
        if idx is None:
            return
        new = idx + delta
        if new < 0 or new >= len(self.rows):
            return
        self.rows[idx], self.rows[new] = self.rows[new], self.rows[idx]
        self._refresh_table()
        self._autosave()
        self.tree.selection_set(str(new))
        self.tree.focus(str(new))

    def _clear_rows(self):
        if not self.rows:
            return
        if not messagebox.askyesno("确认", "确定清空表格中的所有数据吗？"):
            return
        self.rows = []
        self._refresh_table()
        self._autosave()

    def _on_double_click(self, event):
        region = self.tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        try:
            col = int(self.tree.identify_column(event.x)[1:]) - 1
            row_id = self.tree.identify_row(event.y)
            if not row_id:
                return
        except Exception:
            return
        idx = int(row_id)
        if col == 0:
            self._edit_row(idx)
            return
        cat = _norm_category(self.category_var.get())
        fields = CATEGORY_FIELDS[cat]
        headers = CATEGORY_HEADERS[cat]
        if col - 1 >= len(fields):
            return
        key, title = fields[col - 1], headers[col]
        dlg = CellEditor(self.root, f"编辑「{title}」", self.rows[idx].get(key, ""))
        self.root.wait_window(dlg)
        if dlg.result is not None:
            self.rows[idx][key] = dlg.result
            self._refresh_table()
            self._autosave()

    def _edit_row(self, idx=None):
        if idx is None:
            idx = self._selected_index()
        if idx is None:
            messagebox.showwarning("提示", "请先选择一行")
            return
        dlg = RowEditor(self.root, self.rows[idx])
        self.root.wait_window(dlg)
        if dlg.result is not None:
            self.rows[idx] = dlg.result
            self._refresh_table()
            self._autosave()

    def _on_right_click(self, event):
        row_id = self.tree.identify_row(event.y)
        if row_id:
            self.tree.selection_set(row_id)
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="编辑整行", command=self._edit_row)
        menu.add_command(label="在上方插入行", command=lambda: self._add_row(self._selected_index()))
        sel = self._selected_index()
        menu.add_command(label="在下方插入行",
                         command=lambda: self._add_row((self._selected_index() or 0) + 1))
        menu.add_separator()
        menu.add_command(label="－ 删除该行", command=self._delete_row)
        menu.add_separator()
        menu.add_command(label="↑ 上移", command=lambda: self._move(-1))
        menu.add_command(label="↓ 下移", command=lambda: self._move(1))
        menu.tk_popup(event.x_root, event.y_root)
    # ---------- 导出 ----------
    def _on_export(self, selected_only=False):
        """导出 Excel：可导出全部行，也可只导出选中的行。"""
        if not self.rows:
            messagebox.showwarning("提示", "表格为空，请先抓取或添加数据")
            return

        rows = self.rows
        label = f"全部 {len(rows)} 行"
        if selected_only:
            sel = self.tree.selection()
            if not sel:
                messagebox.showwarning(
                    "提示", "请先在表格中选择要导出的行（按住 Ctrl 或 Shift 可多选）")
                return
            indices = sorted(int(i) for i in sel)
            rows = [self.rows[i] for i in indices]
            label = f"选中的 {len(rows)} 行"

        default_name = f"产品信息表_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
        path = filedialog.asksaveasfilename(
            parent=self.root, defaultextension=".xlsx", initialfile=default_name,
            filetypes=[("Excel 工作簿", "*.xlsx")], initialdir=self._out_dir)
        if not path:
            return
        self._run_async(lambda: self._do_export(path, rows, label),
                        status_text="正在导出 Excel…")

    def _do_export(self, path, rows, label):
        exporter.export_excel(rows, path, self.cfg, on_log=self.log,
                              category=self.category_var.get())
        self.log(f"导出成功（{label}）：{path}")
        self.q.put(("exported", path))

    # ---------- 设置 / 会话 ----------
    def _open_settings(self):
        dlg = SettingsDialog(self.root, self.cfg)
        self.root.wait_window(dlg)
        if dlg.result is not None:
            self.cfg = dlg.result
            config_mod.save_config(self.cfg)
            self.log("AI 设置已保存")
            # 同步模型选择到主界面
            self.model_box_var.set(self.cfg["ai"].get("model", ""))
            try:
                vals = dlg.model_combo.cget("values")
                if vals:
                    self.model_box.configure(values=list(vals))
            except Exception:
                pass

    def _autosave(self):
        try:
            payload = {"saved_at": datetime.now().isoformat(), "rows": self.rows}
            with open(self._autosave_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _restore_session(self):
        if os.path.exists(self._autosave_path):
            try:
                with open(self._autosave_path, "r", encoding="utf-8-sig") as f:
                    data = json.load(f)
                rows = data.get("rows") or []
                if rows:
                    self.rows = [self._migrate_row(r) for r in rows]
                    self.log(f"已自动恢复上次未导出的数据（{len(rows)} 条）。")
                    self._refresh_table()
            except Exception:
                pass

    @staticmethod
    def _migrate_row(row):
        """兼容旧会话数据：把旧的“数据类别/普通类/京东类”迁移到新类别，并补齐字段。"""
        row = dict(row or {})
        cat = _norm_category(row.get("数据类别") or row.get("_data_category") or "常规")
        row["_data_category"] = cat
        if "数据类别" in row:
            del row["数据类别"]
        # 补齐该类别与常规类别的字段，保证两种表头都能取值
        for f in CATEGORY_FIELDS[cat]:
            row.setdefault(f, "")
        for f in CATEGORY_FIELDS["常规"]:
            row.setdefault(f, "")
        row.setdefault("_url", "")
        row.setdefault("_image_path", "")
        return row

    def _on_close(self):
        self._autosave()
        # 先退出 mainloop，再延时销毁，避免 CustomTkinter 在销毁瞬间
        # 触发 _update_dimensions_event 重绘已销毁控件而产生无效命令报错
        try:
            self.root.quit()
            self.root.after(60, self.root.destroy)
        except Exception:
            try:
                self.root.destroy()
            except Exception:
                pass

    def _open_out(self):
        self._open_dir(self._out_dir)

    @staticmethod
    def _open_dir(path):
        os.makedirs(path, exist_ok=True)
        if os.name == "nt":
            os.startfile(path)
        else:
            import subprocess
            subprocess.Popen(["xdg-open", path])

class CellEditor(ctk.CTkToplevel):
    """单元格编辑小窗口。"""

    def __init__(self, parent, title, value):
        super().__init__(parent)
        self.withdraw()          # 构建期间保持隐藏，避免“小窗展开”
        self.title(title)
        self.result = None
        self.transient(parent)
        self.geometry("+%d+%d" % (parent.winfo_rootx() + 140, parent.winfo_rooty() + 140))
        body = ctk.CTkFrame(self, corner_radius=10)
        body.pack(fill="both", expand=True, padx=12, pady=12)
        self.txt = ctk.CTkTextbox(body, width=600, height=220, font=("Microsoft YaHei UI", 12),
                                  wrap="word", border_width=1)
        self.txt.insert("1.0", value or "")
        self.txt.pack(fill="both", expand=True)
        btns = ctk.CTkFrame(body, fg_color="transparent")
        btns.pack(pady=(10, 0))
        ctk.CTkButton(btns, text="确定", width=90, command=self._ok).pack(side="left", padx=6)
        ctk.CTkButton(btns, text="取消", width=90,
                      fg_color=("gray93", "gray30"), hover_color=("gray85", "gray20"), text_color=("gray10", "#DCE4EE"),
                      command=self.destroy).pack(side="left", padx=6)
        self.bind("<Escape>", lambda e: self.destroy())
        _present(self)           # 构建完成，一次性显示
        self.txt.focus_set()

    def _ok(self):
        self.result = self.txt.get("1.0", "end").rstrip("\n")
        self.destroy()


class RowEditor(ctk.CTkToplevel):
    """整行编辑窗口（按该行所属数据类别显示对应字段）。"""

    def __init__(self, parent, row):
        super().__init__(parent)
        self.withdraw()          # 构建期间保持隐藏，避免“小窗展开”
        self.title("编辑产品信息")
        self.row = dict(row)
        self.result = None
        self.transient(parent)
        self.geometry("+%d+%d" % (parent.winfo_rootx() + 100, parent.winfo_rooty() + 80))
        body = ctk.CTkFrame(self, corner_radius=10)
        body.pack(fill="both", expand=True, padx=12, pady=12)
        body.columnconfigure(1, weight=1)
        self.cat = _norm_category(self.row.get("_data_category")
                                  or self.row.get("数据类别") or "常规")
        fields = CATEGORY_FIELDS[self.cat]
        self.entries = {}
        heights = {"产品名称": 2, "商品名称": 2, "参考图片": 2, "参考图": 2,
                   "设备介绍": 3, "商品详情": 3, "设备参数": 3, "商品参数": 3}
        ctk.CTkLabel(body, text=f"数据类别：{self.cat}",
                     font=("Microsoft YaHei UI", 12, "bold"),
                     text_color=("gray35", "gray70")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        for i, key in enumerate(fields, start=1):
            ctk.CTkLabel(body, text=key + "：",
                         font=("Microsoft YaHei UI", 12)).grid(
                row=i, column=0, sticky="ne", pady=4, padx=(0, 6))
            t = ctk.CTkTextbox(body, width=560, height=heights.get(key, 6) * 40,
                               font=("Microsoft YaHei UI", 12), wrap="word", border_width=1)
            t.insert("1.0", str(self.row.get(key, "") or ""))
            t.grid(row=i, column=1, sticky="ew", pady=4)
            self.entries[key] = t
        btns = ctk.CTkFrame(body, fg_color="transparent")
        btns.grid(row=len(fields) + 2, column=1, sticky="w", pady=(8, 0))
        self._img_key = "参考图片" if "参考图片" in fields else "参考图"
        ctk.CTkButton(btns, text="浏览本地图片", width=110, command=self._browse).pack(side="left", padx=3)
        ctk.CTkButton(btns, text="保存", width=80, command=self._save).pack(side="left", padx=3)
        ctk.CTkButton(btns, text="取消", width=80,
                      fg_color=("gray93", "gray30"), hover_color=("gray85", "gray20"), text_color=("gray10", "#DCE4EE"),
                      command=self.destroy).pack(side="left", padx=3)
        _present(self)           # 构建完成，一次性显示

    def _browse(self):
        path = filedialog.askopenfilename(
            parent=self,
            filetypes=[("图片文件", "*.jpg *.jpeg *.png *.webp *.gif *.bmp")],
            title="选择产品参考图片")
        if path:
            self.entries[self._img_key].delete("1.0", "end")
            self.entries[self._img_key].insert("1.0", path)

    def _save(self):
        fields = CATEGORY_FIELDS[self.cat]
        new = dict(self.row)
        new["_data_category"] = self.cat
        for k in fields:
            val = self.entries[k].get("1.0", "end").rstrip("\n")
            new[k] = val
        img = (new.get("参考图片") or new.get("参考图") or "").strip()
        if img.startswith("http://") or img.startswith("https://"):
            new["_image_path"] = ""          # 需要重新下载
        elif img and os.path.isfile(img):
            new["_image_path"] = img
        elif not img:
            new["_image_path"] = ""
        self.result = new
        self.destroy()

# 常用服务商预设（参考 CC-Switch 供应商管理思路，一键填入）
PROVIDER_PRESETS = {
    "白嫖中转（baipiao）": {"base_url": "https://api.baipiao.eu.org", "model": "deepseek-v4-flash"},
    "OpenAI": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    "DeepSeek": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    "通义千问": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus"},
    "智谱 GLM": {"base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash"},
    "Kimi（月之暗面）": {"base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k"},
    "硅基流动": {"base_url": "https://api.siliconflow.cn/v1", "model": "Qwen/Qwen2.5-7B-Instruct"},
    "Ollama 本地": {"base_url": "http://localhost:11434/v1", "model": "llama3.1"},
}


class SaveConfigDialog(ctk.CTkToplevel):
    """保存配置时输入配置名称的弹窗，样式与主界面一致。"""

    def __init__(self, parent, prompt, initial):
        super().__init__(parent)
        self.withdraw()          # 构建期间保持隐藏，避免“小窗展开”
        self.title("保存配置")
        self.result = None
        self.transient(parent)
        self.geometry("+%d+%d" % (parent.winfo_rootx() + 120, parent.winfo_rooty() + 120))
        body = ctk.CTkFrame(self, corner_radius=10)
        body.pack(fill="both", expand=True, padx=12, pady=10)
        ctk.CTkLabel(body, text="保存配置",
                     font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w", pady=(0, 4))
        ctk.CTkLabel(body, text=prompt,
                     text_color=("gray50", "gray60"),
                     font=("Microsoft YaHei UI", 11),
                     wraplength=340).pack(anchor="w", pady=(0, 6))
        self.entry = ctk.CTkEntry(body, width=340, border_width=1,
                                  font=("Microsoft YaHei UI", 12))
        self.entry.insert(0, initial or "")
        self.entry.pack(fill="x", pady=(0, 12))
        btns = ctk.CTkFrame(body, fg_color="transparent")
        btns.pack(pady=(4, 0))
        ctk.CTkButton(btns, text="确定", width=90,
                      fg_color="#2e7d32", hover_color="#1b5e20",
                      command=self._ok).pack(side="left", padx=6)
        ctk.CTkButton(btns, text="取消", width=90,
                      fg_color=("gray93", "gray30"), hover_color=("gray85", "gray20"), text_color=("gray10", "#DCE4EE"),
                      command=self.destroy).pack(side="left", padx=6)
        self.bind("<Escape>", lambda e: self.destroy())
        self.bind("<Return>", lambda e: self._ok())
        _present(self)           # 构建完成，一次性显示
        self.entry.focus_set()
        self.entry.select_range(0, "end")
        try:
            self.grab_set()
        except Exception:
            pass

    def _ok(self):
        self.result = self.entry.get().strip()
        self.destroy()


class SettingsDialog(ctk.CTkToplevel):
    """AI 设置窗口：服务商预设 + 多配置保存/切换 + 模型下拉选择。"""

    def __init__(self, parent, cfg):
        super().__init__(parent)
        self.withdraw()          # 构建期间保持隐藏，避免“小窗展开”
        self.title("AI 设置")
        self.result = None
        self.transient(parent)
        self.cfg = json.loads(json.dumps(cfg))
        self.providers = list(self.cfg.get("providers") or [])
        self.default_provider = str(self.cfg.get("default_provider") or "")
        self.last_preset = str(self.cfg.get("last_preset") or "")
        body = ctk.CTkFrame(self, corner_radius=12)
        body.pack(fill="both", expand=True, padx=10, pady=8)
        body.columnconfigure(1, weight=1)
        self.vars = {}
        row = 0

        def add_entry(label, key, secret, hint):
            nonlocal row
            ctk.CTkLabel(body, text=label + "：",
                         font=("Microsoft YaHei UI", 11), height=25).grid(row=row, column=0, sticky="w", pady=2, padx=(0, 8))
            v = tk.StringVar(value=str(self.cfg["ai"].get(key, "")))
            ent = ctk.CTkEntry(body, textvariable=v, width=210, height=25,
                               show="*" if secret else "", border_width=1)
            ent.grid(row=row, column=1, sticky="w", pady=2)
            self.vars[key] = v
            ctk.CTkLabel(body, text=hint, text_color=("gray50", "gray60"),
                         font=("Microsoft YaHei UI", 10), height=25, wraplength=200).grid(row=row, column=2, sticky="w", padx=8)
            row += 1

        # 已保存配置（可多套切换，参考 CC-Switch 供应商管理）
        ctk.CTkLabel(body, text="已保存配置：",
                     font=("Microsoft YaHei UI", 11), height=25).grid(row=row, column=0, sticky="w", pady=2, padx=(0, 8))
        self.provider_var = tk.StringVar()
        self.provider_combo = ctk.CTkComboBox(body, variable=self.provider_var,
                                              values=self._all_config_names(), width=210, height=25,
                                              state="readonly",
                                              command=lambda v: self._on_provider_selected())
        self.provider_combo.grid(row=row, column=1, sticky="w", pady=2)
        pbtns = ctk.CTkFrame(body, fg_color="transparent")
        pbtns.grid(row=row, column=2, sticky="w", padx=8)
        ctk.CTkButton(pbtns, text="设为默认", width=68, height=25, command=self._set_default).pack(side="left", padx=2)
        ctk.CTkButton(pbtns, text="保存配置", width=68, height=25, command=self._save_provider).pack(side="left", padx=2)
        ctk.CTkButton(pbtns, text="删除配置", width=68, height=25,
                      fg_color=("gray93", "gray30"), hover_color=("gray85", "gray20"), text_color=("gray10", "#DCE4EE"),
                      command=self._del_provider).pack(side="left", padx=2)
        row += 1

        # 服务商预设
        ctk.CTkLabel(body, text="服务商预设：",
                     font=("Microsoft YaHei UI", 11), height=25).grid(row=row, column=0, sticky="w", pady=2, padx=(0, 8))
        self.preset_var = tk.StringVar()
        self.preset_combo = ctk.CTkComboBox(body, variable=self.preset_var,
                                            values=list(PROVIDER_PRESETS.keys()), width=210, height=25,
                                            state="readonly",
                                            command=lambda v: self._on_preset_selected())
        self.preset_combo.grid(row=row, column=1, sticky="w", pady=2)
        ctk.CTkLabel(body, text="选择后自动填入地址与模型",
                     text_color=("gray50", "gray60"),
                     font=("Microsoft YaHei UI", 10), height=25).grid(row=row, column=2, sticky="w", padx=8)
        # 恢复上次选择的预设（仅显示，不改动表单内容）
        if self.last_preset in PROVIDER_PRESETS:
            self.preset_var.set(self.last_preset)
        row += 1

        add_entry("API Key", "api_key", True, "服务商提供的 API 密钥")
        add_entry("API 请求地址", "base_url", False,
                  "中转站自动补全 /v1")
        # 模型：可下拉选择，也可手动输入任意模型名
        ctk.CTkLabel(body, text="模型名称：",
                     font=("Microsoft YaHei UI", 11), height=25).grid(row=row, column=0, sticky="w", pady=2, padx=(0, 8))
        self.model_var = tk.StringVar(value=str(self.cfg["ai"].get("model", "")))
        self.model_combo = ctk.CTkComboBox(body, variable=self.model_var, width=210, height=25,
                                           values=[], state="normal",
                                           command=self._on_model_combo_selected)
        self.model_combo.grid(row=row, column=1, sticky="w", pady=2)
        self.vars["model"] = self.model_var
        mbtns = ctk.CTkFrame(body, fg_color="transparent")
        mbtns.grid(row=row, column=2, sticky="w", padx=8)
        ctk.CTkButton(mbtns, text="获取模型列表", width=80, height=25,
                      command=self._fetch_models).pack(side="left", padx=2)
        ctk.CTkLabel(mbtns, text="可下拉或手动输入",
                     text_color=("gray50", "gray60"),
                     font=("Microsoft YaHei UI", 10), height=25).pack(side="left")
        row += 1

        add_entry("Temperature", "temperature", False, "0~2，越低越严谨，默认 0.2")
        add_entry("请求超时", "timeout", False, "默认 120 秒")
        add_entry("文本截断", "max_chars", False, "默认 20000 字")

        ctk.CTkLabel(body, text="提示：兼容 OpenAI / DeepSeek / 通义千问 / 智谱 / Kimi / Ollama 及各类中转站；"
                                "模型可随意选择或手动输入，中转站建议使用其模型列表中的名称。",
                     text_color=("gray55", "gray55"),
                     font=("Microsoft YaHei UI", 10), height=25,
                     wraplength=520).grid(row=row, column=0, columnspan=3, sticky="w", pady=(6, 0))
        row += 1
        self.status = ctk.CTkLabel(body, text="导出全部", text_color="#ef5350",
                                   font=("Microsoft YaHei UI", 11), wraplength=520)
        self.status.grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1
        btns = ctk.CTkFrame(body, fg_color="transparent")
        btns.grid(row=row, column=0, columnspan=3, pady=(8, 0))
        ctk.CTkButton(btns, text="测试连接", width=80, height=28, command=self._test).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="保存", width=76, height=28,
                      fg_color="#2e7d32", hover_color="#1b5e20",
                      command=self._save).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="取消", width=76, height=28,
                      fg_color=("gray93", "gray30"), hover_color=("gray85", "gray20"), text_color=("gray10", "#DCE4EE"),
                      command=self.destroy).pack(side="left", padx=4)

        # 打开界面时自动载入默认配置
        if self.default_provider in self._provider_names():
            self.provider_var.set(self.default_provider)
            self._on_provider_selected()
            self.status.configure(
                text=f"已载入默认配置「{self.default_provider}」，可直接使用或修改后保存",
                text_color="#2e7d32")
        _present(self)           # 构建完成，一次性显示
    # ---------- 已保存配置管理 ----------
    def _provider_names(self):
        names = [p.get("name", "") for p in self.providers]
        return [n for n in names if n]

    def _all_config_names(self):
        """已保存配置 + 内置服务商预设（标注为预设），合并显示在下拉列表。"""
        return self._provider_names() + [f"{k}（预设）" for k in PROVIDER_PRESETS]

    def _on_provider_selected(self):
        name = self.provider_var.get()
        # 1) 已保存配置
        for p in self.providers:
            if p.get("name") == name:
                self.vars["api_key"].set(p.get("api_key", ""))
                self.vars["base_url"].set(p.get("base_url", ""))
                self.model_var.set(p.get("model", ""))
                self.status.configure(
                    text=f"已载入配置「{name}」，可直接使用或修改后保存", text_color="#2e7d32")
                return
        # 2) 内置服务商预设
        if name.endswith("（预设）"):
            pkey = name[: -len("（预设）")]
            preset = PROVIDER_PRESETS.get(pkey)
            if preset:
                self.vars["base_url"].set(preset["base_url"])
                self.model_var.set(preset["model"])
                self.last_preset = pkey
                self.cfg["last_preset"] = pkey
                self.preset_var.set(pkey)
                try:
                    config_mod.save_config(self.cfg)
                except Exception:
                    pass
                key = self.vars["api_key"].get().strip()
                if "api.openai.com" in preset["base_url"] and key and len(key) < 40:
                    self.status.configure(
                        text=f"已载入预设「{pkey}」，但当前 API Key 可能不是 OpenAI 官方 Key（长度过短）；"
                             f"请改用可用中转站或填入正确 Key",
                        text_color="#ef5350")
                else:
                    self.status.configure(
                        text=f"已载入预设「{pkey}」，可直接使用；点「保存配置」可存入配置列表",
                        text_color="#2e7d32")

    def _set_default(self):
        name = self.provider_var.get().strip()
        if not name:
            self.status.configure(text="请先选择要设为默认的配置", text_color="#ef5350")
            return
        if name.endswith("（预设）"):
            self.status.configure(
                text="预设为内置项，请先点「保存配置」将其保存为配置，再设为默认", text_color="#ef5350")
            return
        self.default_provider = name
        self.cfg["default_provider"] = name
        try:
            config_mod.save_config(self.cfg)
            self.status.configure(
                text=f"已将「{name}」设为默认配置（下次打开界面自动载入）", text_color="#2e7d32")
        except Exception as exc:
            self.status.configure(text=f"设置默认配置保存失败：{exc}", text_color="#ef5350")

    def _save_provider(self):
        ai = self._collect()["ai"]
        name = self.provider_var.get().strip()
        if name.endswith("（预设）"):
            pkey = name[: -len("（预设）")]
            name = self._ask_save_name(
                f"将预设「{pkey}」保存为配置，请输入配置名称：", pkey)
            if not name:
                return
        if not name:
            name = self._ask_save_name("请输入配置名称：", self.last_preset or "配置1")
            if not name:
                return
        for p in self.providers:
            if p.get("name") == name:
                p.update({"api_key": ai["api_key"], "base_url": ai["base_url"],
                          "model": ai["model"]})
                break
        else:
            self.providers.append({"name": name, "api_key": ai["api_key"],
                                   "base_url": ai["base_url"], "model": ai["model"]})
        self.provider_combo.configure(values=self._all_config_names())
        self.provider_var.set(name)
        # 立即把「已保存配置」写入 config.json，避免只点「保存配置」关闭后丢失
        self.cfg["providers"] = self.providers
        self.cfg["last_preset"] = self.last_preset
        if not self.default_provider:
            self.default_provider = name
            self.cfg["default_provider"] = name
        else:
            self.cfg["default_provider"] = self.default_provider
        try:
            config_mod.save_config(self.cfg)
        except Exception:
            pass
        if self.default_provider == name:
            self.status.configure(
                text=f"配置「{name}」已保存，并已设为默认配置", text_color="#2e7d32")
        else:
            self.status.configure(text=f"配置「{name}」已保存", text_color="#2e7d32")

    def _ask_save_name(self, prompt, initial):
        """弹出与主界面同风格的输入弹窗，返回输入的名称。"""
        dlg = SaveConfigDialog(self, prompt, initial)
        self.wait_window(dlg)
        return dlg.result

    def _del_provider(self):
        name = self.provider_var.get().strip()
        if not name:
            self.status.configure(text="请先选择要删除的配置", text_color="#ef5350")
            return
        if name.endswith("（预设）"):
            self.status.configure(text="预设为内置项，无需删除（可选中后点「保存配置」另存）", text_color="#ef5350")
            return
        if not messagebox.askyesno("删除配置", f"确定删除配置「{name}」吗？", parent=self):
            return
        self.providers = [p for p in self.providers if p.get("name") != name]
        self.provider_combo.configure(values=self._all_config_names())
        self.provider_var.set("")
        if self.default_provider == name:
            self.default_provider = ""
            self.cfg["default_provider"] = ""
            self.status.configure(text=f"已删除配置「{name}」（原默认配置已清除）", text_color="#ef5350")
        else:
            self.status.configure(text=f"已删除配置「{name}」", text_color="#ef5350")
        # 删除后立即持久化到 config.json，避免仅关闭窗口后配置仍残留
        self.cfg["providers"] = self.providers
        self.cfg["default_provider"] = self.default_provider
        try:
            config_mod.save_config(self.cfg)
        except Exception:
            pass

    # ---------- 服务商预设 ----------
    def _on_preset_selected(self):
        name = self.preset_var.get()
        preset = PROVIDER_PRESETS.get(name)
        if not preset:
            return
        self.vars["base_url"].set(preset["base_url"])
        self.model_var.set(preset["model"])
        # 记住本次选择的预设，重新打开设置界面时仍显示
        self.last_preset = name
        self.cfg["last_preset"] = name
        try:
            config_mod.save_config(self.cfg)
        except Exception:
            pass
        # 清空“已保存配置”选择，避免点“保存配置”时误覆盖当前选中的配置
        self.provider_var.set("")
        key = self.vars["api_key"].get().strip()
        if "api.openai.com" in preset["base_url"] and key and len(key) < 40:
            self.status.configure(
                text="当前 API Key 可能不是 OpenAI 官方 Key（长度过短），保存后连接会失败；"
                     "请填入正确 Key 或改用可用中转站配置",
                text_color="#ef5350")
            return
        self.status.configure(
            text=f"已填入「{name}」预设，请确认/填写 API Key 后点击「获取模型列表」选择模型",
            text_color="#2e7d32")
    def _on_model_combo_selected(self, value):
        """AI 设置：下拉选中模型时给出明确反馈（保存后正式生效）。"""
        model = str(value or "").strip()
        if model:
            self.model_var.set(model)
            self.status.configure(
                text=f"已选择模型：{model}（点「保存」后正式生效）", text_color="#2e7d32")

    # ---------- 字段收集 ----------
    def _collect(self):
        ai = self.cfg["ai"]
        ai["api_key"] = self.vars["api_key"].get().strip()
        ai["base_url"] = self.vars["base_url"].get().strip()
        ai["model"] = self.model_var.get().strip()
        try:
            ai["temperature"] = float(self.vars["temperature"].get() or 0.2)
        except ValueError:
            ai["temperature"] = 0.2
        try:
            ai["timeout"] = int(float(self.vars["timeout"].get() or 120))
        except ValueError:
            ai["timeout"] = 120
        try:
            ai["max_chars"] = int(self.vars["max_chars"].get() or 20000)
        except ValueError:
            ai["max_chars"] = 20000
        return self.cfg   # 返回完整配置（含 ai 等字段），供 AI 模块使用

    def _save(self):
        cfg = self._collect()
        ai = cfg["ai"]
        base = ai.get("base_url", "")
        key = ai.get("api_key", "")
        if "api.openai.com" in base and key and len(key) < 40:
            if not messagebox.askyesno(
                    "确认保存",
                    "检测到：请求地址为 OpenAI 官方，但当前 API Key 长度较短（很可能不是官方 Key）。\n"
                    "使用该组合保存后，测试连接会失败（Incorrect API key）。\n\n是否仍要保存？",
                    parent=self):
                self.status.configure(text="已取消保存，请检查 API Key 或改用可用中转站配置", text_color="#ef5350")
                return
        self.cfg["providers"] = self.providers
        self.cfg["default_provider"] = self.default_provider
        self.cfg["last_preset"] = self.last_preset
        self.result = self.cfg
        self.destroy()

    # ---------- 测试连接 ----------
    def _test(self):
        cfg = self._collect()
        self.status.configure(text="测试中…（免费中转站可能较慢，请稍候）", text_color="#ef5350")
        t = threading.Thread(target=self._do_test, args=(cfg,), daemon=True)
        t.start()

    def _do_test(self, cfg):
        # 后台线程中调用；回主线程更新前先确认设置窗口仍存在，避免已关闭时报错
        def _safe(cb):
            try:
                self.after(0, cb)
            except Exception:
                pass

        try:
            reply = ai_extractor.test_connection(cfg)

            def ok():
                try:
                    self.status.configure(
                        text=str(reply)[:120], text_color="#2e7d32")
                except Exception:
                    pass
            _safe(ok)
        except Exception as exc:
            def apply(err=exc):
                try:
                    self.status.configure(text=str(err), text_color="#ef5350")
                except Exception:
                    pass
            _safe(apply)

    # ---------- 获取模型列表 ----------
    def _fetch_models(self):
        if getattr(self, "_fetching_models", False):
            self.status.configure(text="正在获取模型列表中，请稍候…", text_color="#ef5350")
            return
        self._fetching_models = True
        cfg = self._collect()
        self.status.configure(text="正在获取模型列表…", text_color="#ef5350")
        t = threading.Thread(target=self._do_fetch_models, args=(cfg,), daemon=True)
        t.start()

    def _do_fetch_models(self, cfg):
        import time as _time
        t0 = _time.time()
        # 后台线程中调用；回主线程更新前先确认设置窗口仍存在，避免已关闭时报错
        def _safe(cb):
            try:
                self.after(0, cb)
            except Exception:
                pass

        try:
            models = ai_extractor.fetch_models(cfg)
            elapsed_ms = int((_time.time() - t0) * 1000)

            def apply():
                try:
                    self.model_combo.configure(values=models)
                    if not self.model_var.get().strip() and models:
                        self.model_var.set(models[0])
                    self.status.configure(
                        text="%d 个模型已获取成功，耗时 %d ms" % (len(models), elapsed_ms),
                        text_color="#2e7d32")
                except Exception:
                    pass
                self._fetching_models = False
            _safe(apply)
        except Exception as exc:
            def apply(err=exc):
                try:
                    loaded = list(self.model_combo.cget("values"))
                    if loaded:
                        # 保留已载入的模型，可直接使用
                        self.status.configure(
                            text="模型获取失败，已保留已载入的 %d 个模型，可直接使用（%s）"
                                 % (len(loaded), err),
                            text_color="#ef5350")
                    else:
                        # 从未载入过时才载入常用模型兜底
                        self.model_combo.configure(values=ai_extractor.COMMON_MODELS)
                        if not self.model_var.get().strip() and ai_extractor.COMMON_MODELS:
                            self.model_var.set(ai_extractor.COMMON_MODELS[0])
                        self.status.configure(
                            text="模型获取失败：%s" % str(err),
                            text_color="#ef5350")
                except Exception:
                    pass
                self._fetching_models = False
            _safe(apply)

