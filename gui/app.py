import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
import threading
import logging
import json
import sys
import os

# Adjust path if needed
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

class LogHandler(logging.Handler):
    def __init__(self, text_widget):
        super().__init__()
        self.text_widget = text_widget

    def emit(self, record):
        msg = self.format(record)
        def append():
            self.text_widget.configure(state='normal')
            self.text_widget.insert(tk.END, msg + '\n')
            self.text_widget.configure(state='disabled')
            self.text_widget.yview(tk.END)
        self.text_widget.after(0, append)

SETTINGS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "settings.json"
)

class AutoLoginApp:
    def __init__(self, root):
        self.root = root
        app_ver = config.get_app_version() if hasattr(config, "get_app_version") else getattr(config, "APP_VERSION", "V 1.0.1")
        self.root.title(f"Auto Login & Register Tool - {app_ver}")
        self.root.geometry("780x700")
        
        self.stop_flag = threading.Event()
        self.processing_thread = None
        self.process_func = None  # Sẽ được set từ main.py
        
        self._create_widgets()
        self._load_settings()  # Load settings đã lưu
        
        # Lưu settings khi đóng cửa sổ
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        
    def _create_widgets(self):
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)
        
        # Google Sheet URL
        ttk.Label(main_frame, text="Google Sheet URL:").grid(row=0, column=0, sticky=tk.W, pady=2)
        self.entry_sheet_url = ttk.Entry(main_frame, width=50)
        self.entry_sheet_url.insert(0, getattr(config, 'GOOGLE_SHEET_URL', ''))
        self.entry_sheet_url.grid(row=0, column=1, columnspan=2, sticky=tk.EW, pady=2)
        
        # Sheet Name
        ttk.Label(main_frame, text="Sheet Name:").grid(row=1, column=0, sticky=tk.W, pady=2)
        self.entry_sheet_name = ttk.Entry(main_frame, width=50)
        self.entry_sheet_name.insert(0, getattr(config, 'SHEET_NAME', ''))
        self.entry_sheet_name.grid(row=1, column=1, columnspan=2, sticky=tk.EW, pady=2)
        
        # Rows
        ttk.Label(main_frame, text="From Row:").grid(row=2, column=0, sticky=tk.W, pady=2)
        row_frame = ttk.Frame(main_frame)
        row_frame.grid(row=2, column=1, columnspan=2, sticky=tk.W, pady=2)
        
        self.entry_start_row = ttk.Entry(row_frame, width=10)
        self.entry_start_row.insert(0, str(getattr(config, 'START_ROW', 2)))
        self.entry_start_row.pack(side=tk.LEFT)
        
        ttk.Label(row_frame, text=" To Row:").pack(side=tk.LEFT, padx=5)
        self.entry_end_row = ttk.Entry(row_frame, width=10)
        self.entry_end_row.insert(0, str(getattr(config, 'END_ROW', 10)))
        self.entry_end_row.pack(side=tk.LEFT)

        ttk.Label(row_frame, text="   Số luồng:").pack(side=tk.LEFT, padx=5)
        self.entry_threads = ttk.Entry(row_frame, width=6)
        self.entry_threads.insert(0, str(getattr(config, 'MAX_THREADS', 4)))
        self.entry_threads.pack(side=tk.LEFT)
        
        # Service Account File
        ttk.Label(main_frame, text="Service Account JSON:").grid(row=3, column=0, sticky=tk.W, pady=2)
        self.entry_sa = ttk.Entry(main_frame, width=40)
        self.entry_sa.insert(0, getattr(config, 'GOOGLE_SERVICE_ACCOUNT_FILE', ''))
        self.entry_sa.grid(row=3, column=1, sticky=tk.EW, pady=2)
        ttk.Button(main_frame, text="Browse", command=self._browse_sa).grid(row=3, column=2, padx=5, pady=2)
        
        # Separator & Notebook Tabs (Tab 1: Reg Clone | Tab 2: Reg Gmail)
        ttk.Separator(main_frame, orient=tk.HORIZONTAL).grid(row=4, column=0, columnspan=3, sticky=tk.EW, pady=(8, 6))

        self.notebook = ttk.Notebook(main_frame)
        self.notebook.grid(row=5, column=0, columnspan=3, sticky=tk.EW, pady=2)

        tab_clone = ttk.Frame(self.notebook, padding="10")
        tab_gmail = ttk.Frame(self.notebook, padding="10")
        self.notebook.add(tab_clone, text="   Reg Clone   ")
        self.notebook.add(tab_gmail, text="   Reg Gmail   ")

        # ==========================================================
        # TAB 1: REG CLONE (Facebook + Hotmail + Shopee Affiliate)
        # ==========================================================
        self.cb_vars = {}

        # Bước 1: Login Facebook (+ tuỳ chọn Không login bằng Cookie mà dùng ID + Pass)
        self.cb_vars[1] = tk.BooleanVar(value=False)
        self.fb_no_cookie_var = tk.BooleanVar(value=False)
        fb_row_frame = ttk.Frame(tab_clone)
        fb_row_frame.pack(fill=tk.X, anchor=tk.W, pady=2)
        ttk.Checkbutton(
            fb_row_frame, text="1. Login Facebook", variable=self.cb_vars[1]
        ).pack(side=tk.LEFT)
        ttk.Checkbutton(
            fb_row_frame,
            text="Không login bằng Cookie (dùng ID + Pass)",
            variable=self.fb_no_cookie_var,
            command=self._on_fb_no_cookie_change,
        ).pack(side=tk.LEFT, padx=(20, 0))

        # Bước 2: Login tài khoản Shopee PH hoặc MY hoặc ID bằng Facebook
        self.cb_vars[2] = tk.BooleanVar(value=False)
        self.shopee_region_var = tk.StringVar(value="Shopee.PH")
        shopee_row_frame = ttk.Frame(tab_clone)
        shopee_row_frame.pack(fill=tk.X, anchor=tk.W, pady=2)
        ttk.Checkbutton(
            shopee_row_frame,
            text="2. Login tài khoản Shopee bằng Facebook:",
            variable=self.cb_vars[2],
        ).pack(side=tk.LEFT)
        ttk.Radiobutton(
            shopee_row_frame,
            text="Shopee.PH",
            variable=self.shopee_region_var,
            value="Shopee.PH",
        ).pack(side=tk.LEFT, padx=(12, 6))
        ttk.Radiobutton(
            shopee_row_frame,
            text="Shopee MY",
            variable=self.shopee_region_var,
            value="Shopee MY",
        ).pack(side=tk.LEFT, padx=6)
        ttk.Radiobutton(
            shopee_row_frame,
            text="Shopee ID",
            variable=self.shopee_region_var,
            value="Shopee ID",
        ).pack(side=tk.LEFT, padx=6)

        # Bước 3: Login Hotmail và đổi password Hotmail (+ tuỳ chọn Thay mail khôi phục về @nguyenmaucole.io.vn hoặc @fviainboxes.com)
        self.cb_vars[3] = tk.BooleanVar(value=False)
        self.hotmail_change_recovery_var = tk.BooleanVar(value=False)
        self.hotmail_recovery_domain_var = tk.StringVar(value="nguyenmaucole.io.vn")
        hotmail_row_frame = ttk.Frame(tab_clone)
        hotmail_row_frame.pack(fill=tk.X, anchor=tk.W, pady=2)
        ttk.Checkbutton(
            hotmail_row_frame,
            text="3. Login Hotmail và đổi password Hotmail",
            variable=self.cb_vars[3],
        ).pack(side=tk.LEFT)
        ttk.Checkbutton(
            hotmail_row_frame,
            text="Thay mail khôi phục về:",
            variable=self.hotmail_change_recovery_var,
            command=self._on_hotmail_change_recovery,
        ).pack(side=tk.LEFT, padx=(14, 4))
        ttk.Radiobutton(
            hotmail_row_frame,
            text="@nguyenmaucole.io.vn",
            variable=self.hotmail_recovery_domain_var,
            value="nguyenmaucole.io.vn",
        ).pack(side=tk.LEFT, padx=4)
        ttk.Radiobutton(
            hotmail_row_frame,
            text="@fviainboxes.com",
            variable=self.hotmail_recovery_domain_var,
            value="fviainboxes.com",
        ).pack(side=tk.LEFT, padx=4)

        # Bước 4: Thêm Email forwarding (chọn @nguyenmaucole.io.vn hoặc @fviainboxes.com)
        self.cb_vars[4] = tk.BooleanVar(value=False)
        self.hotmail_forwarding_domain_var = tk.StringVar(value="nguyenmaucole.io.vn")
        forwarding_row_frame = ttk.Frame(tab_clone)
        forwarding_row_frame.pack(fill=tk.X, anchor=tk.W, pady=2)
        ttk.Checkbutton(
            forwarding_row_frame,
            text="4. Thêm Email forwarding về:",
            variable=self.cb_vars[4],
        ).pack(side=tk.LEFT)
        ttk.Radiobutton(
            forwarding_row_frame,
            text="@nguyenmaucole.io.vn",
            variable=self.hotmail_forwarding_domain_var,
            value="nguyenmaucole.io.vn",
        ).pack(side=tk.LEFT, padx=6)
        ttk.Radiobutton(
            forwarding_row_frame,
            text="@fviainboxes.com",
            variable=self.hotmail_forwarding_domain_var,
            value="fviainboxes.com",
        ).pack(side=tk.LEFT, padx=6)

        # Bước 5: Reg aff Shopee theo thị trường chọn ở bước 2
        self.cb_vars[5] = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            tab_clone,
            text="5. Reg Aff Shopee theo thị trường chọn ở bước 2",
            variable=self.cb_vars[5],
        ).pack(fill=tk.X, anchor=tk.W, pady=2)

        # Bước 6: Đổi Pass Shopee
        self.cb_vars[6] = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            tab_clone,
            text="6. Đổi Pass Shopee",
            variable=self.cb_vars[6],
        ).pack(fill=tk.X, anchor=tk.W, pady=(2, 4))

        # Hotmail & Shopee password (cùng 1 hàng trong Tab Reg Clone)
        clone_pass_frame = ttk.Frame(tab_clone)
        clone_pass_frame.pack(fill=tk.X, anchor=tk.W, pady=(6, 2))
        ttk.Label(clone_pass_frame, text="New Hotmail Password:").pack(side=tk.LEFT)
        self.entry_hotmail_pass = ttk.Entry(clone_pass_frame, width=18)
        self.entry_hotmail_pass.insert(0, getattr(config, 'NEW_HOTMAIL_PASSWORD', 'Khoa01@@c'))
        self.entry_hotmail_pass.pack(side=tk.LEFT, padx=(6, 0))

        ttk.Label(clone_pass_frame, text="Pass mới Shopee:").pack(side=tk.LEFT, padx=(18, 5))
        self.entry_shopee_pass = ttk.Entry(clone_pass_frame, width=18)
        self.entry_shopee_pass.insert(0, getattr(config, 'NEW_SHOPEE_PASSWORD', 'Khoa01@c'))
        self.entry_shopee_pass.pack(side=tk.LEFT)

        # ==========================================================
        # TAB 2: REG GMAIL (Gmail Forwarding + Shopee Affiliate)
        # ==========================================================
        self.gmail_cb_vars = {}

        # Bước 1: Login Gmail - Thêm Forwarding mail về @nguyenmaucole.io.vn (hoặc @fviainboxes.com)
        self.gmail_cb_vars[1] = tk.BooleanVar(value=True)
        self.gmail_forwarding_domain_var = tk.StringVar(value="nguyenmaucole.io.vn")
        gm_step1_frame = ttk.Frame(tab_gmail)
        gm_step1_frame.pack(fill=tk.X, anchor=tk.W, pady=3)
        ttk.Checkbutton(
            gm_step1_frame,
            text="1. Login Gmail - Thêm Forwarding mail về:",
            variable=self.gmail_cb_vars[1],
        ).pack(side=tk.LEFT)
        ttk.Radiobutton(
            gm_step1_frame,
            text="@nguyenmaucole.io.vn",
            variable=self.gmail_forwarding_domain_var,
            value="nguyenmaucole.io.vn",
        ).pack(side=tk.LEFT, padx=(10, 6))
        ttk.Radiobutton(
            gm_step1_frame,
            text="@fviainboxes.com",
            variable=self.gmail_forwarding_domain_var,
            value="fviainboxes.com",
        ).pack(side=tk.LEFT, padx=6)

        # Bước 2: Login Shopee bằng Gmail với các thị trường PH, MY, ID
        self.gmail_cb_vars[2] = tk.BooleanVar(value=True)
        self.gmail_shopee_region_var = tk.StringVar(value="Shopee.PH")
        gm_step2_frame = ttk.Frame(tab_gmail)
        gm_step2_frame.pack(fill=tk.X, anchor=tk.W, pady=3)
        ttk.Checkbutton(
            gm_step2_frame,
            text="2. Login Shopee bằng Gmail:",
            variable=self.gmail_cb_vars[2],
        ).pack(side=tk.LEFT)
        ttk.Radiobutton(
            gm_step2_frame,
            text="Shopee.PH",
            variable=self.gmail_shopee_region_var,
            value="Shopee.PH",
        ).pack(side=tk.LEFT, padx=(12, 6))
        ttk.Radiobutton(
            gm_step2_frame,
            text="Shopee MY",
            variable=self.gmail_shopee_region_var,
            value="Shopee MY",
        ).pack(side=tk.LEFT, padx=6)
        ttk.Radiobutton(
            gm_step2_frame,
            text="Shopee ID",
            variable=self.gmail_shopee_region_var,
            value="Shopee ID",
        ).pack(side=tk.LEFT, padx=6)

        # Bước 3: Reg AFF Shopee thị trường tương ứng với bước 2 đã chọn
        self.gmail_cb_vars[3] = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            tab_gmail,
            text="3. Reg AFF Shopee theo thị trường chọn ở bước 2",
            variable=self.gmail_cb_vars[3],
        ).pack(fill=tk.X, anchor=tk.W, pady=3)

        # Bước 4: Change Pass Shopee
        self.gmail_cb_vars[4] = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            tab_gmail,
            text="4. Change Pass Shopee (Đổi Pass Shopee)",
            variable=self.gmail_cb_vars[4],
        ).pack(fill=tk.X, anchor=tk.W, pady=3)

        # Pass mới Shopee trong Tab Reg Gmail
        gm_pass_frame = ttk.Frame(tab_gmail)
        gm_pass_frame.pack(fill=tk.X, anchor=tk.W, pady=(8, 2))
        ttk.Label(gm_pass_frame, text="Pass mới Shopee:").pack(side=tk.LEFT)
        self.entry_gmail_shopee_pass = ttk.Entry(gm_pass_frame, width=20)
        self.entry_gmail_shopee_pass.insert(0, getattr(config, 'NEW_SHOPEE_PASSWORD', 'Khoa01@c'))
        self.entry_gmail_shopee_pass.pack(side=tk.LEFT, padx=(8, 0))

        row_idx = 6

        # Buttons
        btn_frame = ttk.Frame(main_frame)
        btn_frame.grid(row=row_idx, column=0, columnspan=3, pady=8)
        
        self.btn_start = ttk.Button(btn_frame, text="BẮT ĐẦU", command=self.start_processing)
        self.btn_start.pack(side=tk.LEFT, padx=5)
        
        self.btn_stop = ttk.Button(btn_frame, text="DỪNG", command=self.stop_processing, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=5)
        row_idx += 1
        
        # Progress Bar
        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(main_frame, variable=self.progress_var, maximum=100)
        self.progress_bar.grid(row=row_idx, column=0, columnspan=3, sticky=tk.EW, pady=5)
        row_idx += 1
        
        self.lbl_progress = ttk.Label(main_frame, text="Tiến độ: 0/0 profiles (0%)")
        self.lbl_progress.grid(row=row_idx, column=0, columnspan=3, pady=2)
        row_idx += 1
        
        # Log Area
        self.log_area = scrolledtext.ScrolledText(main_frame, height=15, state='disabled')
        self.log_area.grid(row=row_idx, column=0, columnspan=3, sticky=tk.NSEW, pady=5)
        
        main_frame.rowconfigure(row_idx, weight=1)
        main_frame.columnconfigure(1, weight=1)

    def _load_settings(self):
        """Load settings từ file JSON (nếu có)."""
        if not os.path.exists(SETTINGS_FILE):
            return
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                settings = json.load(f)

            # Điền lại các ô nhập liệu
            for entry, key in [
                (self.entry_sheet_url, "sheet_url"),
                (self.entry_sheet_name, "sheet_name"),
                (self.entry_start_row, "start_row"),
                (self.entry_end_row, "end_row"),
                (self.entry_threads, "max_threads"),
                (self.entry_sa, "sa_file"),
                (self.entry_hotmail_pass, "hotmail_pass"),
                (self.entry_shopee_pass, "shopee_pass"),
                (self.entry_gmail_shopee_pass, "gmail_shopee_pass"),
            ]:
                if key in settings:
                    entry.delete(0, tk.END)
                    entry.insert(0, str(settings[key]))

            # Tab đang chọn (0: Reg Clone, 1: Reg Gmail)
            active_tab = settings.get("active_tab", 0)
            if active_tab in (0, 1):
                self.notebook.select(active_tab)

            # Checkboxes Tab Reg Clone (1 -> 6)
            if settings.get("cb_fb"):
                self.cb_vars[1].set(True)
            if settings.get("cb_fb_no_cookie"):
                self.fb_no_cookie_var.set(True)
            if "cb_shopee_login" in settings:
                self.cb_vars[2].set(bool(settings.get("cb_shopee_login")))
            if settings.get("cb_hotmail"):
                self.cb_vars[3].set(True)
            if settings.get("cb_hotmail_change_recovery"):
                self.hotmail_change_recovery_var.set(True)
            rec_dom = settings.get("hotmail_recovery_domain")
            if rec_dom in ("nguyenmaucole.io.vn", "fviainboxes.com"):
                self.hotmail_recovery_domain_var.set(rec_dom)
            if settings.get("cb_forwarding"):
                self.cb_vars[4].set(True)
            fwd_dom = settings.get("hotmail_forwarding_domain")
            if fwd_dom in ("nguyenmaucole.io.vn", "fviainboxes.com"):
                self.hotmail_forwarding_domain_var.set(fwd_dom)
            if "cb_shopee_aff" in settings:
                self.cb_vars[5].set(bool(settings.get("cb_shopee_aff")))
            if settings.get("cb_shopee_pass"):
                self.cb_vars[6].set(True)

            # Thị trường Shopee Tab Reg Clone
            market = settings.get("shopee_market")
            if market in ("Shopee.PH", "Shopee MY", "Shopee ID"):
                self.shopee_region_var.set(market)
            else:
                old_val = settings.get("shopee_region", 0)
                old_map = {3: "Shopee.PH", 4: "Shopee.PH", 5: "Shopee MY", 6: "Shopee ID"}
                if old_val in old_map:
                    self.shopee_region_var.set(old_map[old_val])
                    if "cb_shopee_login" not in settings:
                        self.cb_vars[2].set(True)
                    if "cb_shopee_aff" not in settings:
                        self.cb_vars[5].set(True)

            # Checkboxes & Radio Tab Reg Gmail (1 -> 4)
            for s in (1, 2, 3, 4):
                k = f"gmail_cb_{s}"
                if k in settings:
                    self.gmail_cb_vars[s].set(bool(settings.get(k)))
            gm_fwd_dom = settings.get("gmail_forwarding_domain")
            if gm_fwd_dom in ("nguyenmaucole.io.vn", "fviainboxes.com"):
                self.gmail_forwarding_domain_var.set(gm_fwd_dom)
            gm_market = settings.get("gmail_shopee_market")
            if gm_market in ("Shopee.PH", "Shopee MY", "Shopee ID"):
                self.gmail_shopee_region_var.set(gm_market)

        except Exception:
            pass  # File lỗi thì bỏ qua, dùng default

    def _save_settings(self):
        """Lưu settings hiện tại ra file JSON."""
        try:
            active_tab = self.notebook.index(self.notebook.select())
        except Exception:
            active_tab = 0

        settings = {
            "active_tab": active_tab,
            "sheet_url": self.entry_sheet_url.get(),
            "sheet_name": self.entry_sheet_name.get(),
            "start_row": self.entry_start_row.get(),
            "end_row": self.entry_end_row.get(),
            "max_threads": self.entry_threads.get(),
            "sa_file": self.entry_sa.get(),
            "hotmail_pass": self.entry_hotmail_pass.get(),
            "shopee_pass": self.entry_shopee_pass.get(),
            "gmail_shopee_pass": self.entry_gmail_shopee_pass.get(),
            "cb_fb": self.cb_vars[1].get(),
            "cb_fb_no_cookie": self.fb_no_cookie_var.get(),
            "cb_shopee_login": self.cb_vars[2].get(),
            "shopee_market": self.shopee_region_var.get(),
            "cb_hotmail": self.cb_vars[3].get(),
            "cb_hotmail_change_recovery": self.hotmail_change_recovery_var.get(),
            "hotmail_recovery_domain": self.hotmail_recovery_domain_var.get(),
            "cb_forwarding": self.cb_vars[4].get(),
            "hotmail_forwarding_domain": self.hotmail_forwarding_domain_var.get(),
            "cb_shopee_aff": self.cb_vars[5].get(),
            "cb_shopee_pass": self.cb_vars[6].get(),
            "gmail_cb_1": self.gmail_cb_vars[1].get(),
            "gmail_cb_2": self.gmail_cb_vars[2].get(),
            "gmail_cb_3": self.gmail_cb_vars[3].get(),
            "gmail_cb_4": self.gmail_cb_vars[4].get(),
            "gmail_forwarding_domain": self.gmail_forwarding_domain_var.get(),
            "gmail_shopee_market": self.gmail_shopee_region_var.get(),
        }
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _on_close(self):
        """Lưu settings trước khi đóng cửa sổ và thoát hoàn toàn tiến trình."""
        self.stop_flag.set()
        self._save_settings()
        try:
            self.root.destroy()
        except Exception:
            pass
        os._exit(0)

    def _browse_sa(self):
        filepath = filedialog.askopenfilename(filetypes=[("JSON files", "*.json"), ("All files", "*.*")])
        if filepath:
            self.entry_sa.delete(0, tk.END)
            self.entry_sa.insert(0, filepath)
            
    def _on_fb_no_cookie_change(self):
        if self.fb_no_cookie_var.get() and not self.cb_vars[1].get():
            self.cb_vars[1].set(True)

    def _on_hotmail_change_recovery(self):
        if self.hotmail_change_recovery_var.get() and not self.cb_vars[3].get():
            self.cb_vars[3].set(True)

    def get_active_mode(self) -> str:
        """Trả về 'gmail' nếu đang ở Tab 'Reg Gmail', ngược lại trả về 'clone'."""
        try:
            return "gmail" if self.notebook.index(self.notebook.select()) == 1 else "clone"
        except Exception:
            return "clone"

    def get_selected_steps(self):
        mode = self.get_active_mode()
        steps = []
        if mode == "gmail":
            for s in (1, 2, 3, 4):
                if self.gmail_cb_vars[s].get():
                    steps.append(s)
        else:
            for s in (1, 2, 3, 4, 5, 6):
                if self.cb_vars[s].get():
                    steps.append(s)
        return steps
        
    def get_config(self):
        try:
            start_row = int(self.entry_start_row.get())
            end_row = int(self.entry_end_row.get())
        except ValueError:
            start_row = 2
            end_row = 10

        try:
            max_threads = max(1, int(self.entry_threads.get()))
        except ValueError:
            max_threads = getattr(config, 'MAX_THREADS', 4)

        mode = self.get_active_mode()
        if mode == "gmail":
            return {
                'MODE': 'gmail',
                'GOOGLE_SHEET_URL': self.entry_sheet_url.get(),
                'SHEET_NAME': self.entry_sheet_name.get(),
                'START_ROW': start_row,
                'END_ROW': end_row,
                'MAX_THREADS': max_threads,
                'GOOGLE_SERVICE_ACCOUNT_FILE': self.entry_sa.get(),
                'NEW_SHOPEE_PASSWORD': self.entry_gmail_shopee_pass.get(),
                'SHOPEE_REGION': self.gmail_shopee_region_var.get(),
                'GMAIL_FORWARDING_DOMAIN': self.gmail_forwarding_domain_var.get(),
            }

        return {
            'MODE': 'clone',
            'GOOGLE_SHEET_URL': self.entry_sheet_url.get(),
            'SHEET_NAME': self.entry_sheet_name.get(),
            'START_ROW': start_row,
            'END_ROW': end_row,
            'MAX_THREADS': max_threads,
            'GOOGLE_SERVICE_ACCOUNT_FILE': self.entry_sa.get(),
            'NEW_HOTMAIL_PASSWORD': self.entry_hotmail_pass.get(),
            'NEW_SHOPEE_PASSWORD': self.entry_shopee_pass.get(),
            'FB_NO_COOKIE': self.fb_no_cookie_var.get(),
            'SHOPEE_REGION': self.shopee_region_var.get(),
            'HOTMAIL_CHANGE_RECOVERY': self.hotmail_change_recovery_var.get(),
            'HOTMAIL_RECOVERY_DOMAIN': self.hotmail_recovery_domain_var.get(),
            'HOTMAIL_FORWARDING_DOMAIN': self.hotmail_forwarding_domain_var.get(),
        }

    def update_progress(self, current, total):
        def _update():
            pct = (current / total) * 100 if total > 0 else 0
            self.progress_var.set(pct)
            self.lbl_progress.config(text=f"Tiến độ: {current}/{total} profiles ({pct:.1f}%)")
        try:
            self.root.after(0, _update)
        except Exception:
            pass

    def add_log(self, message):
        def _add():
            self.log_area.configure(state='normal')
            self.log_area.insert(tk.END, message + '\n')
            self.log_area.configure(state='disabled')
            self.log_area.yview(tk.END)
        try:
            self.root.after(0, _add)
        except Exception:
            pass

    def start_processing(self):
        selected_steps = self.get_selected_steps()
        if not selected_steps:
            messagebox.showerror("Lỗi", "Vui lòng chọn ít nhất 1 thao tác.")
            return
            
        gui_config = self.get_config()
        self._save_settings()
        
        self.btn_start.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)
        self.stop_flag.clear()
        self.log_area.configure(state='normal')
        self.log_area.delete(1.0, tk.END)
        self.log_area.configure(state='disabled')
        
        self.processing_thread = threading.Thread(
            target=self._run_processing,
            args=(gui_config, selected_steps)
        )
        self.processing_thread.daemon = True
        self.processing_thread.start()
        
    def stop_processing(self):
        self.add_log("Đang yêu cầu dừng...")
        self.stop_flag.set()
        self.btn_stop.config(state=tk.DISABLED)

    def _run_processing(self, gui_config, selected_steps):
        try:
            if self.process_func is None:
                self.add_log("Lỗi: Chưa set hàm xử lý (process_func)")
                return
            self.process_func(
                gui_config=gui_config,
                selected_steps=selected_steps,
                stop_flag=self.stop_flag,
                progress_callback=self.update_progress,
                log_callback=self.add_log
            )
        except Exception as e:
            self.add_log(f"Lỗi: {str(e)}")
        finally:
            def _reset_btns():
                self.btn_start.config(state=tk.NORMAL)
                self.btn_stop.config(state=tk.DISABLED)
                self.add_log("Hoàn tất / Đã dừng.")
            self.root.after(0, _reset_btns)
