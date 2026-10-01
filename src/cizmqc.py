"""
ClassIsland 通知发送器
ClassIsland ZeroMQ Client (CIZMQC)
======================
通过 IslandMQ 插件向教室电脑发送通知。
"""
import csv
import json
import logging
import platform
import queue
import sys
import threading
from ipaddress import ip_address
from logging.handlers import RotatingFileHandler
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog

import zmq

# ================= 元信息 =================
APP_NAME = "ClassIslandNoticeSender"
APP_TITLE = "ClassIsland 通知发送器"
APP_VERSION = "2.0"

BATCH_TIMEOUT_MS = 5000
BATCH_MAX_WORKERS = 10
MAX_LOG_BYTES = 1_000_000


# ================= 目录与文件路径 =================
def _get_app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = _get_app_dir()
DATA_DIR = APP_DIR / "data"

try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    DATA_DIR = Path.home() / f".{APP_NAME.lower()}"
    DATA_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_FILE = DATA_DIR / "config.json"
LOG_FILE = DATA_DIR / "app.log"
CLASSLIST_FILE = DATA_DIR / "classlist.csv"
OLD_FAVORITES_FILE = DATA_DIR / "favorites.csv"


# ================= 日志初始化 =================
logger = logging.getLogger(APP_NAME)
logger.setLevel(logging.INFO)

try:
    _handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=MAX_LOG_BYTES,
        backupCount=1,
        encoding="utf-8",
    )
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(_handler)
except Exception:
    logger.addHandler(logging.NullHandler())


# ================= 默认配置 =================
DEFAULT_CONFIG = {
    "ip": "127.0.0.1",
    "port": 5555,
    "title": "班主任通知",
    "mask_duration": 3.0,
    "overlay_duration": 5.0,
    "timeout_ms": 5000,
    "ping_timeout_ms": 2000,
    "last_body": "",
    "default_class": "",
}


# ================= 工具函数 =================
def _safe_float(value, default: float) -> float:
    try:
        val = float(value)
        if val > 0:
            return val
    except (TypeError, ValueError, OverflowError):
        pass
    return default


def _safe_int(value, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def clean_ip(ip: str) -> str:
    """去掉用户可能输入的 IPv6 方括号，方便统一存储和校验。"""
    ip = ip.strip()
    if ip.startswith("[") and ip.endswith("]"):
        ip = ip[1:-1]
    return ip


def make_endpoint(ip: str, port: str) -> str:
    """根据 IP 和端口构造 ZeroMQ endpoint，自动处理 IPv6 方括号。"""
    ip = clean_ip(ip)
    if ":" in ip:
        return f"tcp://[{ip}]:{port}"
    return f"tcp://{ip}:{port}"


def load_config() -> dict:
    cfg = DEFAULT_CONFIG.copy()
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                cfg.update(data)
            logger.info(f"已加载配置: {CONFIG_FILE}")
        except Exception as e:
            logger.warning(f"读取配置失败: {e}")

    # 数值安全处理，防止旧配置或手写配置导致崩溃
    cfg["mask_duration"] = _safe_float(
        cfg.get("mask_duration"), DEFAULT_CONFIG["mask_duration"]
    )
    cfg["overlay_duration"] = _safe_float(
        cfg.get("overlay_duration"), DEFAULT_CONFIG["overlay_duration"]
    )
    cfg["timeout_ms"] = _safe_int(
        cfg.get("timeout_ms"), DEFAULT_CONFIG["timeout_ms"]
    )
    cfg["ping_timeout_ms"] = _safe_int(
        cfg.get("ping_timeout_ms"), DEFAULT_CONFIG["ping_timeout_ms"]
    )
    cfg["port"] = _safe_int(
        cfg.get("port"), DEFAULT_CONFIG["port"]
    )

    for key in ("ip", "title", "last_body", "default_class"):
        if key not in cfg or cfg[key] is None:
            cfg[key] = DEFAULT_CONFIG[key]
        else:
            cfg[key] = str(cfg[key])

    return cfg


def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        logger.info(f"已保存配置: {CONFIG_FILE}")
    except Exception as e:
        logger.warning(f"保存配置失败: {e}")


# ---------- 班级列表 CSV ----------
CLASSLIST_FIELDS = ["班级名称", "IP", "端口"]


def migrate_old_favorites_file() -> None:
    if OLD_FAVORITES_FILE.exists() and not CLASSLIST_FILE.exists():
        try:
            OLD_FAVORITES_FILE.rename(CLASSLIST_FILE)
            logger.info("已将 favorites.csv 重命名为 classlist.csv")
        except Exception as e:
            logger.warning(f"重命名 favorites.csv 失败: {e}")


def load_classlist() -> list:
    if not CLASSLIST_FILE.exists():
        return []
    result = []
    try:
        with open(CLASSLIST_FILE, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                name = (row.get("班级名称") or "").strip()
                ip = (row.get("IP") or "").strip()
                port = (row.get("端口") or "").strip()
                if name and ip and port:
                    result.append({"name": name, "ip": ip, "port": port})
        logger.info(f"已加载班级列表: {CLASSLIST_FILE} ({len(result)} 条)")
    except Exception as e:
        logger.warning(f"读取班级列表失败: {e}")
    return result


def save_classlist(items: list) -> None:
    try:
        with open(CLASSLIST_FILE, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CLASSLIST_FIELDS)
            writer.writeheader()
            for item in items:
                writer.writerow({
                    "班级名称": item.get("name", ""),
                    "IP": item.get("ip", ""),
                    "端口": item.get("port", ""),
                })
        logger.info(f"已保存班级列表: {CLASSLIST_FILE} ({len(items)} 条)")
    except Exception as e:
        logger.warning(f"保存班级列表失败: {e}")


def center_window(win: tk.Tk, width: int, height: int) -> None:
    win.update_idletasks()
    sw = win.winfo_screenwidth()
    sh = win.winfo_screenheight()
    x = max(0, (sw - width) // 2)
    y = max(0, (sh - height) // 2)
    win.geometry(f"{width}x{height}+{x}+{y}")


def center_on_parent(child: tk.Toplevel, parent: tk.Misc, width: int, height: int) -> None:
    child.update_idletasks()
    try:
        px = parent.winfo_rootx()
        py = parent.winfo_rooty()
        pw = parent.winfo_width()
        ph = parent.winfo_height()
        x = px + max(0, (pw - width) // 2)
        y = py + max(0, (ph - height) // 2)
    except Exception:
        x = max(0, (child.winfo_screenwidth() - width) // 2)
        y = max(0, (child.winfo_screenheight() - height) // 2)
    child.geometry(f"{width}x{height}+{x}+{y}")


def set_window_icon(win: tk.Misc) -> None:
    icon_path = APP_DIR / "icon" / "icon.ico"
    if not icon_path.exists():
        return
    try:
        if isinstance(win, (tk.Tk, tk.Toplevel)):
            if platform.system() == "Windows":
                win.iconbitmap(default=str(icon_path))
            else:
                img = tk.PhotoImage(file=str(icon_path))
                win.iconphoto(True, img)
                win._icon_img_ref = img
    except Exception as e:
        logger.warning(f"设置窗口图标失败: {e}")


# ================= 核心发送函数 =================
def send_request_isolated(context: zmq.Context, endpoint: str,
                          request: dict, timeout_ms: int,
                          tag: str = "") -> tuple:
    """
    在独立 socket 上发送请求（线程安全）。
    每次调用都会创建新的 REQ socket，用完立即关闭，避免 REQ/REP 状态机残留。
    返回 (success, message)。
    """
    prefix = f"[{tag}] " if tag else ""
    sock = context.socket(zmq.REQ)
    try:
        sock.setsockopt(zmq.RCVTIMEO, timeout_ms)
        sock.setsockopt(zmq.SNDTIMEO, timeout_ms)
        sock.setsockopt(zmq.LINGER, 0)
        sock.connect(endpoint)

        payload = json.dumps(request, ensure_ascii=False)
        logger.info(f"{prefix}发送 -> {endpoint} [command={request.get('command', '?')}]")
        logger.debug(f"{prefix}完整请求: {payload}")
        sock.send_string(payload)
        reply = sock.recv_string()
        logger.debug(f"{prefix}完整响应: {reply}")

        resp = json.loads(reply)
        if isinstance(resp, dict):
            logger.info(f"{prefix}收到 <- {endpoint} [success={resp.get('success', False)}]")
            return resp.get("success", False), resp.get("message", reply)
        return True, str(resp)

    except zmq.Again:
        logger.warning(f"{prefix}{endpoint} 请求超时")
        return False, "请求超时（请检查教室电脑是否在线、IP 和端口是否正确）"
    except zmq.ZMQError as e:
        logger.error(f"{prefix}{endpoint} ZeroMQ 错误: {e}")
        return False, f"网络错误: {e}"
    except json.JSONDecodeError as e:
        logger.error(f"{prefix}{endpoint} 响应 JSON 解析失败: {e}")
        return False, "响应格式错误"
    except Exception as e:
        logger.exception(f"{prefix}{endpoint} 未捕获异常")
        return False, f"未知错误: {e}"
    finally:
        try:
            sock.close(linger=0)
        except Exception:
            pass


# ================= 主应用 =================
class NoticeSenderApp:
    def __init__(self, root: tk.Tk, config: dict):
        self.root = root
        self.config = config

        self.root.title(APP_TITLE)
        set_window_icon(self.root)
        self.root.resizable(False, False)

        # 统一使用 Context，socket 由 send_request_isolated 每次独立创建
        self.context = zmq.Context()

        self.classlist = []
        self.batch_window = None
        self._result_queue = queue.Queue()
        self._worker_running = False

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()
        self._apply_config()
        self._refresh_classlist()
        self._apply_default_class()

        center_window(self.root, 600, 560)

    # ---------- UI 构建 ----------
    def _build_ui(self):
        # --- 班级列表 ---
        class_frame = ttk.LabelFrame(self.root, text="班级列表", padding=10)
        class_frame.pack(fill="x", padx=12, pady=(10, 5))

        self.class_var = tk.StringVar()
        self.class_combo = ttk.Combobox(
            class_frame, textvariable=self.class_var, state="readonly", width=32
        )
        self.class_combo.grid(row=0, column=0, padx=(0, 6), pady=2, sticky="ew")
        self.class_combo.bind("<<ComboboxSelected>>", self._on_class_selected)

        ttk.Button(class_frame, text="添加班级", command=self._save_class).grid(
            row=0, column=1, padx=3, pady=2
        )
        ttk.Button(class_frame, text="删除班级", command=self._delete_class).grid(
            row=0, column=2, padx=3, pady=2
        )
        ttk.Button(class_frame, text="设为默认", command=self._set_default_class).grid(
            row=0, column=3, padx=3, pady=2
        )
        ttk.Button(class_frame, text="导入", command=self._import_classlist).grid(
            row=0, column=4, padx=3, pady=2
        )
        ttk.Button(class_frame, text="导出", command=self._export_classlist).grid(
            row=0, column=5, padx=3, pady=2
        )
        class_frame.columnconfigure(0, weight=1)

        # --- 连接设置 ---
        conn_frame = ttk.LabelFrame(self.root, text="连接设置", padding=10)
        conn_frame.pack(fill="x", padx=12, pady=5)

        ttk.Label(conn_frame, text="教室电脑 IP:").grid(row=0, column=0, sticky="w")
        self.ip_var = tk.StringVar()
        ttk.Entry(conn_frame, textvariable=self.ip_var, width=18).grid(
            row=0, column=1, padx=(5, 12), pady=2, sticky="w"
        )

        ttk.Label(conn_frame, text="端口:").grid(row=0, column=2, sticky="w")
        self.port_var = tk.StringVar()
        ttk.Entry(conn_frame, textvariable=self.port_var, width=8).grid(
            row=0, column=3, padx=(5, 12), pady=2, sticky="w"
        )

        self.test_btn = ttk.Button(conn_frame, text="连接测试", command=self.test_connection)
        self.test_btn.grid(row=0, column=4, padx=5, pady=2, sticky="e")

        # --- 通知内容 ---
        content_frame = ttk.LabelFrame(self.root, text="通知内容", padding=10)
        content_frame.pack(fill="both", expand=True, padx=12, pady=5)

        ttk.Label(content_frame, text="标题:").grid(row=0, column=0, sticky="w")
        self.title_var = tk.StringVar()
        ttk.Entry(content_frame, textvariable=self.title_var).grid(
            row=0, column=1, padx=5, pady=2, sticky="ew"
        )

        ttk.Label(content_frame, text="正文:").grid(row=1, column=0, sticky="nw", pady=(6, 0))
        self.body_text = tk.Text(content_frame, height=6, width=45, wrap="word")
        self.body_text.grid(row=1, column=1, padx=5, pady=(6, 0), sticky="nsew")
        self.body_text.bind("<Control-Return>", lambda e: (self.send_notice(), "break")[1])

        content_frame.columnconfigure(1, weight=1)
        content_frame.rowconfigure(1, weight=1)

        # --- 显示选项 ---
        adv_frame = ttk.LabelFrame(self.root, text="显示选项", padding=10)
        adv_frame.pack(fill="x", padx=12, pady=5)

        ttk.Label(adv_frame, text="标题显示时长(秒):").grid(row=0, column=0, sticky="w")
        self.mask_var = tk.StringVar()
        ttk.Spinbox(adv_frame, from_=0.5, to=30.0, increment=0.5,
                    textvariable=self.mask_var, width=8).grid(row=0, column=1, padx=5)

        ttk.Label(adv_frame, text="正文显示时长(秒):").grid(row=0, column=2, sticky="w", padx=(15, 0))
        self.overlay_var = tk.StringVar()
        ttk.Spinbox(adv_frame, from_=0.5, to=30.0, increment=0.5,
                    textvariable=self.overlay_var, width=8).grid(row=0, column=3, padx=5)

        # --- 快捷通知 ---
        quick_frame = ttk.LabelFrame(self.root, text="快捷通知", padding=10)
        quick_frame.pack(fill="x", padx=12, pady=5)

        quick_buttons = [
            ("大声读书", "请大声读书"),
            ("保持安静", "请保持安静"),
            ("专心学习", "请专心学习"),
            ("休息一会", "请休息一会"),
        ]
        for i, (label, body) in enumerate(quick_buttons):
            ttk.Button(
                quick_frame, text=label,
                command=lambda b=body: self._quick_send(b)
            ).grid(row=0, column=i, padx=3, pady=2, sticky="ew")
            quick_frame.columnconfigure(i, weight=1)

        # --- 发送与状态 ---
        send_frame = ttk.Frame(self.root)
        send_frame.pack(fill="x", padx=12, pady=(5, 10))

        self.send_btn = ttk.Button(send_frame, text="发送通知", command=self.send_notice)
        self.send_btn.pack(side="left", padx=(0, 5))

        self.batch_btn = ttk.Button(send_frame, text="批量发送", command=self.open_batch_window)
        self.batch_btn.pack(side="left", padx=(0, 10))

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(send_frame, textvariable=self.status_var, foreground="gray").pack(
            side="left", fill="x", expand=True
        )

    # ---------- 配置读写 ----------
    def _apply_config(self):
        cfg = self.config
        self.ip_var.set(str(cfg.get("ip", "127.0.0.1")))
        self.port_var.set(str(cfg.get("port", "5555")))
        self.title_var.set(str(cfg.get("title", "班主任通知")))
        self.mask_var.set(str(cfg.get("mask_duration", 3.0)))
        self.overlay_var.set(str(cfg.get("overlay_duration", 5.0)))
        last_body = cfg.get("last_body", "")
        if last_body:
            self.body_text.insert("1.0", last_body)

    def _collect_config(self) -> dict:
        cfg = {
            "ip": self.ip_var.get().strip(),
            "port": _safe_int(self.port_var.get().strip(), DEFAULT_CONFIG["port"]),
            "title": self.title_var.get().strip(),
            "mask_duration": _safe_float(self.mask_var.get().strip(), DEFAULT_CONFIG["mask_duration"]),
            "overlay_duration": _safe_float(self.overlay_var.get().strip(), DEFAULT_CONFIG["overlay_duration"]),
            "timeout_ms": int(self.config.get("timeout_ms", DEFAULT_CONFIG["timeout_ms"])),
            "ping_timeout_ms": int(self.config.get("ping_timeout_ms", DEFAULT_CONFIG["ping_timeout_ms"])),
            "last_body": self.body_text.get("1.0", tk.END).strip(),
            "default_class": self.config.get("default_class", ""),
        }
        return cfg

    # ---------- 班级列表 ----------
    def _class_display(self, item: dict) -> str:
        default_key = self.config.get("default_class", "")
        key = f"{item['ip']}:{item['port']}"
        prefix = "★ " if key == default_key else "   "
        return f"{prefix}{item['name']}"

    def _refresh_classlist(self, select_index=None):
        """重新加载班级列表；保留用户当前的选择（如果仍然存在）"""
        old_key = ""
        old_idx = self.class_combo.current()
        if 0 <= old_idx < len(self.classlist):
            old = self.classlist[old_idx]
            old_key = f"{old['ip']}:{old['port']}"

        self.classlist = load_classlist()
        display = [self._class_display(item) for item in self.classlist]
        self.class_combo["values"] = display

        if select_index is not None and 0 <= select_index < len(display):
            self.class_combo.current(select_index)
            self.class_var.set(display[select_index])
            return

        if old_key:
            for i, item in enumerate(self.classlist):
                if f"{item['ip']}:{item['port']}" == old_key:
                    self.class_combo.current(i)
                    self.class_var.set(display[i])
                    return

        self.class_var.set("")

    def _apply_default_class(self):
        """启动时应用默认班级的 IP/端口。仅在 IP 或端口为空时才覆盖，避免覆盖用户配置。"""
        default_key = str(self.config.get("default_class", "")).strip()
        if not default_key:
            return
        for item in self.classlist:
            if f"{item['ip']}:{item['port']}" == default_key:
                if not self.ip_var.get().strip():
                    self.ip_var.set(item["ip"])
                if not self.port_var.get().strip():
                    self.port_var.set(item["port"])
                for i, it in enumerate(self.classlist):
                    if f"{it['ip']}:{it['port']}" == default_key:
                        self.class_combo.current(i)
                        self.class_var.set(self._class_display(it))
                        break
                self.status_var.set(f"已加载默认班级: {item['name']}")
                logger.info(f"已应用默认班级: {item['name']} ({default_key})")
                return
        logger.warning(f"默认班级 {default_key} 未在班级列表中找到")

    def _on_class_selected(self, event=None):
        idx = self.class_combo.current()
        if 0 <= idx < len(self.classlist):
            item = self.classlist[idx]
            self.ip_var.set(item["ip"])
            self.port_var.set(item["port"])
            self.status_var.set(f"已选择: {item['name']}")

    def _save_class(self):
        ip = clean_ip(self.ip_var.get())
        port = self.port_var.get().strip()
        try:
            ip_address(ip)
            if not port.isdigit() or not (1 <= int(port) <= 65535):
                raise ValueError("端口不合法")
        except Exception as e:
            messagebox.showwarning("无法添加", f"当前地址无效：{e}")
            return

        self.ip_var.set(ip)

        for item in self.classlist:
            if item["ip"] == ip and item["port"] == port:
                messagebox.showinfo("提示", f"该地址已存在于班级「{item['name']}」。")
                return

        name = simpledialog.askstring("添加班级", "请输入班级名称:", parent=self.root)
        if name is None:
            return
        name = name.strip()
        if not name:
            messagebox.showwarning("无法添加", "班级名称不能为空。")
            return

        self.classlist.append({"name": name, "ip": ip, "port": port})
        save_classlist(self.classlist)
        self._refresh_classlist(select_index=len(self.classlist) - 1)
        logger.info(f"已添加班级: {name} ({ip}:{port})")
        self.status_var.set(f"✅ 已添加: {name}")

    def _delete_class(self):
        idx = self.class_combo.current()
        if idx < 0 or idx >= len(self.classlist):
            messagebox.showinfo("提示", "请先在班级列表中选择一项。")
            return
        item = self.classlist[idx]
        if not messagebox.askyesno(
            "确认删除",
            f"确定要删除班级「{item['name']}」({item['ip']}:{item['port']}) 吗？"
        ):
            return
        del self.classlist[idx]
        save_classlist(self.classlist)

        key = f"{item['ip']}:{item['port']}"
        if self.config.get("default_class", "") == key:
            self.config["default_class"] = ""
            save_config(self._collect_config())
            logger.info("默认班级已被删除，已清空默认设置")

        self._refresh_classlist()
        logger.info(f"已删除班级: {item['name']} ({item['ip']}:{item['port']})")
        self.status_var.set(f"已删除: {item['name']}")

    def _set_default_class(self):
        idx = self.class_combo.current()
        if idx < 0 or idx >= len(self.classlist):
            messagebox.showinfo("提示", "请先在班级列表中选择一项。")
            return
        item = self.classlist[idx]
        key = f"{item['ip']}:{item['port']}"
        self.config["default_class"] = key
        save_config(self._collect_config())
        self._refresh_classlist(select_index=idx)
        self.ip_var.set(item["ip"])
        self.port_var.set(item["port"])
        logger.info(f"已设置默认班级: {item['name']} ({key})")
        self.status_var.set(f"⭐ 已设为默认班级: {item['name']}")

    def _export_classlist(self):
        if not self.classlist:
            messagebox.showinfo("提示", "班级列表为空，没有可导出的内容。")
            return
        path = filedialog.asksaveasfilename(
            title="导出班级列表",
            defaultextension=".csv",
            initialfile="classlist.csv",
            filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")],
            parent=self.root,
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=CLASSLIST_FIELDS)
                writer.writeheader()
                for item in self.classlist:
                    writer.writerow({
                        "班级名称": item.get("name", ""),
                        "IP": item.get("ip", ""),
                        "端口": item.get("port", ""),
                    })
            logger.info(f"已导出班级列表到: {path} ({len(self.classlist)} 条)")
            self.status_var.set(f"✅ 已导出 {len(self.classlist)} 个班级")
            messagebox.showinfo("导出成功", f"已导出 {len(self.classlist)} 个班级到：\n{path}")
        except Exception as e:
            logger.warning(f"导出班级列表失败: {e}")
            messagebox.showerror("导出失败", f"导出失败：{e}")

    def _import_classlist(self):
        path = filedialog.askopenfilename(
            title="导入班级列表",
            filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")],
            parent=self.root,
        )
        if not path:
            return
        try:
            imported = []
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    name = (row.get("班级名称") or "").strip()
                    ip = clean_ip(row.get("IP") or "")
                    port = (row.get("端口") or "").strip()
                    if name and ip and port:
                        try:
                            ip_address(ip)
                            if port.isdigit() and 1 <= int(port) <= 65535:
                                imported.append({"name": name, "ip": ip, "port": port})
                        except ValueError:
                            continue
        except Exception as e:
            logger.warning(f"读取导入文件失败: {e}")
            messagebox.showerror("导入失败", f"读取文件失败：{e}")
            return

        if not imported:
            messagebox.showwarning("导入失败", "文件中没有有效的班级数据。")
            return

        existing_keys = {f"{item['ip']}:{item['port']}" for item in self.classlist}
        added = 0
        skipped = 0
        for item in imported:
            key = f"{item['ip']}:{item['port']}"
            if key in existing_keys:
                skipped += 1
            else:
                self.classlist.append(item)
                existing_keys.add(key)
                added += 1

        save_classlist(self.classlist)
        self._refresh_classlist()
        logger.info(f"导入班级列表: 新增 {added} 条，跳过重复 {skipped} 条")
        self.status_var.set(f"✅ 导入完成：新增 {added}，跳过 {skipped}")
        messagebox.showinfo(
            "导入完成",
            f"成功导入 {added} 个班级，跳过 {skipped} 个重复项。"
        )

    # ---------- 地址校验 ----------
    def _get_endpoint(self) -> str:
        ip = clean_ip(self.ip_var.get())
        port = self.port_var.get().strip()

        if not ip:
            raise ValueError("IP 不能为空。")
        try:
            ip_address(ip)
        except ValueError:
            raise ValueError(f"IP 地址格式不正确：{ip}")

        if not port:
            raise ValueError("端口不能为空。")
        if not port.isdigit():
            raise ValueError("端口必须是纯数字。")
        if not (1 <= int(port) <= 65535):
            raise ValueError("端口号必须在 1~65535 之间。")

        return make_endpoint(ip, port)

    # ---------- 通知参数构造 ----------
    def _build_notice_args(self, title: str, body: str,
                           mask_duration: float, overlay_duration: float) -> list:
        args = [title]
        if body:
            args.append(f"--context={body}")
        args.append(f"--mask-duration={mask_duration}")
        args.append(f"--overlay-duration={overlay_duration}")
        return args

    # ---------- 交互动作 ----------
    def _quick_send(self, body: str):
        self.body_text.delete("1.0", tk.END)
        self.body_text.insert("1.0", body)
        self.send_notice()

    def _validate_notice_inputs(self):
        title = self.title_var.get().strip()
        if not title:
            messagebox.showwarning("缺少标题", "通知标题不能为空。")
            return None

        body = self.body_text.get("1.0", tk.END).strip()

        try:
            mask = float(self.mask_var.get())
            overlay = float(self.overlay_var.get())
            if mask <= 0 or overlay <= 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning("参数错误", "时长必须是大于 0 的数字。")
            return None

        return title, body, mask, overlay

    def test_connection(self):
        if self._worker_running:
            return
        try:
            endpoint = self._get_endpoint()
        except ValueError as e:
            messagebox.showwarning("地址错误", str(e))
            return

        timeout = int(self.config.get("ping_timeout_ms", DEFAULT_CONFIG["ping_timeout_ms"]))
        request = {"version": 0, "command": "ping", "args": []}

        self._worker_running = True
        self.status_var.set("测试连接中...")
        self.test_btn.config(state="disabled")
        self.send_btn.config(state="disabled")

        threading.Thread(
            target=self._test_worker,
            args=(endpoint, request, timeout),
            daemon=True,
        ).start()
        self.root.after(100, self._poll_result_queue)

    def _test_worker(self, endpoint, request, timeout):
        try:
            success, msg = send_request_isolated(
                self.context, endpoint, request, timeout, tag="测试"
            )
            self._result_queue.put(("test", success, msg))
        except Exception as e:
            self._result_queue.put(("test", False, f"未知错误: {e}"))

    def send_notice(self):
        if self._worker_running:
            return
        values = self._validate_notice_inputs()
        if values is None:
            return
        title, body, mask, overlay = values

        try:
            endpoint = self._get_endpoint()
        except ValueError as e:
            messagebox.showwarning("地址错误", str(e))
            return

        args = self._build_notice_args(title, body, mask, overlay)
        request = {"version": 0, "command": "notice", "args": args}
        timeout = int(self.config.get("timeout_ms", DEFAULT_CONFIG["timeout_ms"]))

        self._worker_running = True
        self.status_var.set("发送中...")
        self.send_btn.config(state="disabled")
        self.test_btn.config(state="disabled")

        threading.Thread(
            target=self._send_worker,
            args=(endpoint, request, timeout),
            daemon=True,
        ).start()
        self.root.after(100, self._poll_result_queue)

    def _send_worker(self, endpoint, request, timeout):
        try:
            success, msg = send_request_isolated(
                self.context, endpoint, request, timeout, tag="单发"
            )
            self._result_queue.put(("send", success, msg))
        except Exception as e:
            self._result_queue.put(("send", False, f"未知错误: {e}"))

    def _poll_result_queue(self):
        try:
            while True:
                msg = self._result_queue.get_nowait()
                kind = msg[0]
                if kind == "test":
                    _, success, msg_text = msg
                    self._on_test_result(success, msg_text)
                    self._worker_running = False
                    return
                elif kind == "send":
                    _, success, msg_text = msg
                    self._on_send_result(success, msg_text)
                    self._worker_running = False
                    return
        except queue.Empty:
            pass

        if self._worker_running:
            self.root.after(100, self._poll_result_queue)

    def _on_test_result(self, success, msg):
        self.test_btn.config(state="normal")
        self.send_btn.config(state="normal")
        if success:
            self.status_var.set("✅ 连接正常")
            messagebox.showinfo("连接测试", f"连接成功！\n响应: {msg}")
        else:
            self.status_var.set(f"❌ 连接失败: {msg}")
            messagebox.showerror("连接测试", msg)

    def _on_send_result(self, success, msg):
        self.send_btn.config(state="normal")
        self.test_btn.config(state="normal")
        if success:
            self.status_var.set("✅ 发送成功")
        else:
            self.status_var.set(f"❌ 发送失败: {msg}")
            messagebox.showerror("发送失败", msg)

    # ---------- 批量发送 ----------
    def open_batch_window(self):
        if self.batch_window is not None and self.batch_window.winfo_exists():
            self.batch_window.lift()
            self.batch_window.focus_set()
            return

        values = self._validate_notice_inputs()
        if values is None:
            return
        title, body, mask, overlay = values

        self._refresh_classlist()
        if not self.classlist:
            messagebox.showinfo("提示", "班级列表为空，请先添加班级。")
            return

        self.batch_window = BatchSendWindow(
            parent=self.root,
            app=self,
            title=title, body=body,
            mask=mask, overlay=overlay,
            classlist=list(self.classlist),
        )

    # ---------- 退出清理 ----------
    def _on_close(self):
        try:
            save_config(self._collect_config())
        except Exception:
            pass
        try:
            self.context.term()
        except Exception:
            pass
        logger.info("程序退出")
        self.root.destroy()


# ================= 批量发送窗口 =================
class BatchSendWindow(tk.Toplevel):
    def __init__(self, parent, app: NoticeSenderApp,
                 title: str, body: str, mask: float, overlay: float,
                 classlist: list):
        super().__init__(parent)
        self.app = app
        self.classlist = classlist
        self.notice_title = title
        self.notice_body = body
        self.notice_mask = mask
        self.notice_overlay = overlay

        self._batch_queue = queue.Queue()
        self._batch_active = False
        self._stop_event = threading.Event()

        self.title("批量发送通知")
        self.resizable(False, False)
        self.transient(parent)
        try:
            set_window_icon(self)
        except Exception:
            pass

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build_ui()
        center_on_parent(self, parent, 620, 540)

    def _build_ui(self):
        sel_frame = ttk.LabelFrame(self, text="选择目标班级", padding=10)
        sel_frame.pack(fill="both", expand=True, padx=12, pady=(10, 5))

        toolbar = ttk.Frame(sel_frame)
        toolbar.pack(fill="x", pady=(0, 6))
        ttk.Button(toolbar, text="全选", command=self._select_all).pack(side="left", padx=(0, 3))
        ttk.Button(toolbar, text="全不选", command=self._select_none).pack(side="left", padx=3)
        ttk.Label(toolbar, text="（按住 Ctrl 或 Shift 可多选）",
                  foreground="#888", font=("", 8)).pack(side="left", padx=8)

        list_frame = ttk.Frame(sel_frame)
        list_frame.pack(fill="both", expand=True)

        scroll = ttk.Scrollbar(list_frame, orient="vertical")
        scroll.pack(side="right", fill="y")

        self.listbox = tk.Listbox(
            list_frame, selectmode="extended",
            yscrollcommand=scroll.set,
            height=8, activestyle="none",
        )
        self.listbox.pack(side="left", fill="both", expand=True)
        scroll.config(command=self.listbox.yview)

        default_key = self.app.config.get("default_class", "")
        for item in self.classlist:
            key = f"{item['ip']}:{item['port']}"
            prefix = "★ " if key == default_key else "   "
            self.listbox.insert("end", f"{prefix}{item['name']}")

        self.listbox.select_set(0, "end")

        preview_frame = ttk.LabelFrame(self, text="通知内容预览", padding=10)
        preview_frame.pack(fill="x", padx=12, pady=5)

        ttk.Label(preview_frame, text=f"标题：{self.notice_title}",
                  font=("", 9, "bold")).pack(anchor="w")
        body_display = self.notice_body if self.notice_body else "（无正文）"
        ttk.Label(preview_frame, text=f"正文：{body_display}",
                  wraplength=560, justify="left").pack(anchor="w", pady=(3, 0))
        ttk.Label(preview_frame,
                  text=f"标题显示时长：{self.notice_mask}s     正文显示时长：{self.notice_overlay}s",
                  foreground="#666").pack(anchor="w", pady=(3, 0))

        progress_frame = ttk.Frame(self)
        progress_frame.pack(fill="x", padx=12, pady=(0, 5))
        self.progress = ttk.Progressbar(progress_frame, mode="determinate")
        self.progress.pack(fill="x")

        action_frame = ttk.Frame(self)
        action_frame.pack(fill="x", padx=12, pady=(5, 10))

        self.send_btn = ttk.Button(action_frame, text="开始批量发送", command=self._start_batch)
        self.send_btn.pack(side="left", padx=(0, 6))

        self.cancel_btn = ttk.Button(action_frame, text="关闭", command=self.destroy)
        self.cancel_btn.pack(side="left", padx=(0, 12))

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(action_frame, textvariable=self.status_var, foreground="gray").pack(
            side="left", fill="x", expand=True
        )

    def _select_all(self):
        self.listbox.select_set(0, "end")

    def _select_none(self):
        self.listbox.select_clear(0, "end")

    def _build_request(self) -> dict:
        args = self.app._build_notice_args(
            self.notice_title,
            self.notice_body,
            self.notice_mask,
            self.notice_overlay,
        )
        return {"version": 0, "command": "notice", "args": args}

    def _start_batch(self):
        indices = self.listbox.curselection()
        if not indices:
            messagebox.showwarning("提示", "请至少选择一个班级。", parent=self)
            return

        selected = [self.classlist[i] for i in indices]
        request = self._build_request()

        self.send_btn.config(state="disabled")
        self.cancel_btn.config(state="disabled")
        self.listbox.config(state="disabled")
        self.progress["maximum"] = len(selected)
        self.progress["value"] = 0
        self.status_var.set(f"准备发送到 {len(selected)} 个班级...")

        self._batch_active = True
        self._stop_event.clear()
        thread = threading.Thread(
            target=self._send_worker,
            args=(selected, request),
            daemon=True,
        )
        thread.start()
        self.after(100, self._poll_batch_queue)

    def _send_worker(self, selected, request):
        from concurrent.futures import ThreadPoolExecutor, as_completed

        total = len(selected)
        results = []
        completed = 0

        def _send_one(item):
            if self._stop_event.is_set():
                return None
            endpoint = make_endpoint(item["ip"], item["port"])
            success, msg = send_request_isolated(
                self.app.context, endpoint, request,
                BATCH_TIMEOUT_MS, tag="批量"
            )
            return {
                "name": item["name"],
                "ip": item["ip"],
                "port": item["port"],
                "success": success,
                "message": msg,
            }

        with ThreadPoolExecutor(max_workers=BATCH_MAX_WORKERS) as executor:
            futures = {executor.submit(_send_one, item): item for item in selected}
            for future in as_completed(futures):
                if self._stop_event.is_set():
                    break
                try:
                    result = future.result()
                except Exception as e:
                    logger.exception(f"批量发送任务异常: {e}")
                    continue
                if result is None:
                    continue
                results.append(result)
                completed += 1
                self._batch_queue.put(("progress", completed, total, result["name"]))

        self._batch_queue.put(("done", results))

    def _poll_batch_queue(self):
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return

        try:
            while True:
                msg = self._batch_queue.get_nowait()
                if msg[0] == "progress":
                    _, current, total, name = msg
                    self.progress["value"] = current
                    self.status_var.set(f"已完成 {current}/{total}：{name}")
                elif msg[0] == "done":
                    _, results = msg
                    self._batch_active = False
                    self._on_batch_done(results)
                    return
        except queue.Empty:
            pass

        if self._batch_active:
            self.after(100, self._poll_batch_queue)

    def _on_close(self):
        self._stop_event.set()
        self.destroy()

    def _on_batch_done(self, results: list):
        try:
            self.send_btn.config(state="normal")
            self.cancel_btn.config(state="normal")
            self.listbox.config(state="normal")
            self.progress["value"] = len(results)
        except Exception:
            pass

        success_count = sum(1 for r in results if r["success"])
        fail_count = len(results) - success_count
        self.status_var.set(f"发送完成：成功 {success_count}，失败 {fail_count}")

        try:
            BatchResultDialog(self, results)
        except Exception as e:
            logger.exception(f"显示结果窗口失败: {e}")


# ================= 批量结果对话框 =================
class BatchResultDialog(tk.Toplevel):
    def __init__(self, parent: tk.Toplevel, results: list):
        super().__init__(parent)
        self.results = results

        self.title("批量发送结果")
        self.resizable(True, True)
        self.transient(parent)
        try:
            set_window_icon(self)
        except Exception:
            pass

        self._build_ui()
        center_on_parent(self, parent, 640, 440)

        self.after(50, self._grab)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _grab(self):
        try:
            self.grab_set()
            self.focus_force()
        except Exception:
            pass

    def _build_ui(self):
        success_count = sum(1 for r in self.results if r["success"])
        fail_count = len(self.results) - success_count

        header = ttk.Frame(self)
        header.pack(fill="x", padx=14, pady=(12, 4))

        ttk.Label(header, text=f"共 {len(self.results)} 个班级：",
                  font=("", 10)).pack(side="left")
        ttk.Label(header, text=f"✅ 成功 {success_count}",
                  foreground="#0a7d00", font=("", 10, "bold")).pack(side="left", padx=(2, 8))
        ttk.Label(header, text=f"❌ 失败 {fail_count}",
                  foreground="#c00000", font=("", 10, "bold")).pack(side="left")

        table_frame = ttk.Frame(self)
        table_frame.pack(fill="both", expand=True, padx=14, pady=6)

        scroll = ttk.Scrollbar(table_frame, orient="vertical")
        scroll.pack(side="right", fill="y")

        columns = ("name", "address", "status", "detail")
        tree = ttk.Treeview(
            table_frame, columns=columns, show="headings",
            yscrollcommand=scroll.set, height=12,
        )
        tree.pack(side="left", fill="both", expand=True)
        scroll.config(command=tree.yview)

        tree.heading("name", text="班级名称")
        tree.heading("address", text="地址")
        tree.heading("status", text="状态")
        tree.heading("detail", text="详情")

        tree.column("name", width=140, anchor="w")
        tree.column("address", width=160, anchor="w")
        tree.column("status", width=90, anchor="center")
        tree.column("detail", width=220, anchor="w")

        tree.tag_configure("success", foreground="#0a7d00")
        tree.tag_configure("fail", foreground="#c00000")

        for r in self.results:
            tag = "success" if r["success"] else "fail"
            status = "✅ 成功" if r["success"] else "❌ 失败"
            tree.insert("", "end", values=(
                r["name"],
                f"{r['ip']}:{r['port']}",
                status,
                r["message"],
            ), tags=(tag,))

        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x", padx=14, pady=(4, 12))

        if fail_count > 0:
            ttk.Button(btn_frame, text="重试失败项", command=self._retry_failed).pack(
                side="left", padx=(0, 6)
            )

        ttk.Button(btn_frame, text="关闭", command=self._close).pack(side="right")

    def _retry_failed(self):
        failed = [r for r in self.results if not r["success"]]
        if not failed:
            return
        self._close()
        try:
            parent = self.master
            if parent and hasattr(parent, "listbox"):
                parent.listbox.select_clear(0, "end")
                for r in failed:
                    for i, item in enumerate(parent.classlist):
                        if item["ip"] == r["ip"] and item["port"] == r["port"]:
                            parent.listbox.select_set(i)
                            break
                parent.lift()
                parent.focus_set()
                parent._start_batch()
        except Exception as e:
            logger.warning(f"重试失败项时出错: {e}")

    def _close(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


# ================= 入口 =================
def main():
    try:
        migrate_old_favorites_file()
        config = load_config()

        root = tk.Tk()
        app = NoticeSenderApp(root, config)
        root.mainloop()
    except Exception as e:
        logger.exception("启动失败")
        try:
            messagebox.showerror("启动失败", f"{e}\n\n日志: {LOG_FILE}")
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()