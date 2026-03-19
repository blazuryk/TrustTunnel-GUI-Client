"""
TrustTunnel Client PRO
– Minimize to tray instead of closing
– Exit from tray kills VPN
– Autostart via registry (starts hidden in tray, VPN off)
– Requires: pip install pystray pillow
"""

import tkinter as tk
from tkinter import filedialog, messagebox
import subprocess
import os
import sys
import ctypes
import tempfile
import threading
import shlex
import re
import queue
import json
import math
import winreg

import pystray
from PIL import Image, ImageDraw

# ── CONSTANTS ─────────────────────────────────────────────────────────────────

APP_NAME        = "TrustTunnel"
CONFIG_DIR      = os.path.join(os.environ.get("APPDATA", os.getcwd()), APP_NAME)
CONFIG_FILE     = os.path.join(CONFIG_DIR, "settings.json")
AUTOSTART_KEY   = r"Software\Microsoft\Windows\CurrentVersion\Run"
START_MINIMIZED = "--minimized" in sys.argv   # set by autostart registry entry

# ── PALETTE (strict 6-digit hex only — Tkinter does not support 8-digit) ─────

BG         = "#0B0E14"
SURFACE    = "#131720"
SURFACE2   = "#1A1F2E"
BORDER     = "#252B3B"
ACCENT     = "#00C8FF"
ACCENT2    = "#0055CC"
SUCCESS    = "#00E676"
WARNING    = "#FFB300"
DANGER     = "#FF3D57"
TEXT       = "#E8EAF0"
TEXT_DIM   = "#606880"
TEXT_FAINT = "#2A2F40"
HOVER      = "#1E2535"

FONT_H    = ("Courier New", 11, "bold")
FONT_MONO = ("Courier New",  9)
FONT_LBL  = ("Courier New",  8)
FONT_XS   = ("Courier New",  7)

# ── GLOBALS ───────────────────────────────────────────────────────────────────

vpn_process   = None
log_queue     = queue.Queue()
vpn_connected = False
tray_icon     = None          # set after GUI is ready

# ── COLOR HELPER ──────────────────────────────────────────────────────────────

def blend(fg: str, bg: str, alpha: float) -> str:
    """Alpha-blend fg onto bg (0=transparent … 1=opaque) → #RRGGBB."""
    fr, fg2, fb = int(fg[1:3], 16), int(fg[3:5], 16), int(fg[5:7], 16)
    br, bg2, bb = int(bg[1:3], 16), int(bg[3:5], 16), int(bg[5:7], 16)
    r = int(fr * alpha + br * (1 - alpha))
    g = int(fg2 * alpha + bg2 * (1 - alpha))
    b = int(fb * alpha + bb * (1 - alpha))
    return f"#{r:02x}{g:02x}{b:02x}"

# ── ADMIN ─────────────────────────────────────────────────────────────────────

def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False

def run_as_admin():
    # Preserve --minimized flag when re-launching as admin
    args = " ".join(sys.argv)
    ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, args, None, 1)

# ── PATH ──────────────────────────────────────────────────────────────────────

def get_binary_path(name):
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, name)
    return os.path.join(os.getcwd(), name)

# ── AUTOSTART (registry) ──────────────────────────────────────────────────────

def _get_exe():
    """Path to the running executable (works for both .py and PyInstaller .exe)."""
    return sys.executable

def autostart_enabled() -> bool:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0, winreg.KEY_READ)
        winreg.QueryValueEx(key, APP_NAME)
        winreg.CloseKey(key)
        return True
    except (FileNotFoundError, OSError):
        return False

def set_autostart(enable: bool) -> bool:
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0, winreg.KEY_SET_VALUE
        )
        if enable:
            value = f'"{_get_exe()}" --minimized'
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, value)
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
        return True
    except OSError:
        return False

# ── SAVE / LOAD ───────────────────────────────────────────────────────────────

def ensure_config_dir():
    os.makedirs(CONFIG_DIR, exist_ok=True)

def save_settings():
    ensure_config_dir()
    data = {
        "hostname":    entry_hostname.get(),
        "address":     entry_address.get(),
        "username":    entry_username.get(),
        "password":    entry_password.get(),
        "trust_flags": entry_trust_flags.get(),
        "setup_flags": entry_setup_flags.get(),
    }
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
    except Exception:
        pass

def load_settings():
    if not os.path.exists(CONFIG_FILE):
        return
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return
    entry_hostname.insert(0,    data.get("hostname",    ""))
    entry_address.insert(0,     data.get("address",     ""))
    entry_username.insert(0,    data.get("username",    ""))
    entry_password.insert(0,    data.get("password",    ""))
    entry_trust_flags.insert(0, data.get("trust_flags", ""))
    entry_setup_flags.insert(0, data.get("setup_flags", ""))

# ── THREAD-SAFE QUEUE ─────────────────────────────────────────────────────────

def process_queue():
    """Runs every 100 ms in the main thread — the ONLY place that touches widgets."""
    try:
        while True:
            item = log_queue.get_nowait()
            if item[0] == "log":
                log_box.config(state="normal")
                log_box.insert(tk.END, item[1])
                log_box.see(tk.END)
                log_box.config(state="disabled")
            elif item[0] == "status":
                _apply_status(item[1])
    except queue.Empty:
        pass
    root.after(100, process_queue)

def set_status(key):
    """Thread-safe: called from any thread."""
    log_queue.put(("status", key))

def append_log(text):
    """Thread-safe: called from any thread."""
    log_queue.put(("log", text))

# ── TRAY ICON IMAGE ───────────────────────────────────────────────────────────

def _make_tray_image(connected: bool = False) -> Image.Image:
    """Draw a 64×64 RGBA icon for the system tray."""
    size = 64
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d    = ImageDraw.Draw(img)

    dot_col   = (0, 230, 118, 255) if connected else (255, 61, 87, 255)
    icon_col  = (255, 255, 255, 220)

    # filled circle background
    d.ellipse([3, 3, 61, 61], fill=dot_col)

    # power-symbol arc
    cx, cy = 32, 32
    d.arc([cx - 13, cy - 13, cx + 13, cy + 13],
          start=120, end=420, fill=icon_col, width=3)
    # vertical bar
    d.line([cx, cy - 15, cx, cy - 7], fill=icon_col, width=3)

    return img

# ── TRAY MANAGEMENT ───────────────────────────────────────────────────────────

def _update_tray():
    """Refresh tray icon image + menu. Safe to call from main thread."""
    global tray_icon
    if tray_icon is None:
        return
    tray_icon.icon = _make_tray_image(vpn_connected)
    tray_icon.update_menu()

def show_window():
    """Show main window (safe to call from tray thread via root.after)."""
    root.after(0, lambda: (root.deiconify(), root.lift(), root.focus_force()))

def hide_window():
    root.withdraw()

def on_window_close():
    """X button → hide to tray instead of quitting."""
    hide_window()

def quit_app():
    """Called from tray 'Exit'. Kills VPN, stops tray, destroys window."""
    global vpn_process
    # Kill VPN directly — don't go through queue, we're exiting now
    if vpn_process and vpn_process.poll() is None:
        vpn_process.terminate()
    try:
        subprocess.call(
            ["taskkill", "/f", "/im", "trusttunnel_client.exe"],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        pass
    if tray_icon:
        tray_icon.stop()          # unblocks tray thread → thread ends
    root.after(0, root.destroy)   # schedule destroy in main thread

def _tray_toggle_vpn():
    """Called from tray menu → schedule in main thread."""
    root.after(0, toggle_vpn)

def _build_tray_menu():
    return pystray.Menu(
        pystray.MenuItem("Show TrustTunnel", lambda: show_window(), default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            lambda _: "Disconnect VPN" if vpn_connected else "Connect VPN",
            lambda _: _tray_toggle_vpn(),
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit", lambda _: quit_app()),
    )

def _start_tray():
    """Runs in a background thread. Blocks until tray_icon.stop() is called."""
    global tray_icon
    tray_icon = pystray.Icon(
        APP_NAME,
        _make_tray_image(False),
        APP_NAME,
        menu=_build_tray_menu(),
    )
    tray_icon.run()

# ── STATUS + ANIMATED RING ────────────────────────────────────────────────────

_pulse_job   = None
_pulse_phase = 0.0
RING_R       = 68

def _apply_status(key):
    """Only called from process_queue → runs in main thread."""
    global vpn_connected, _pulse_job
    if _pulse_job:
        root.after_cancel(_pulse_job)
        _pulse_job = None

    if key == "connected":
        vpn_connected = True
        status_lbl.config(text="CONNECTED",     fg=SUCCESS)
        _draw_btn(state="on")
        _start_pulse(SUCCESS)
    elif key == "connecting":
        vpn_connected = False
        status_lbl.config(text="CONNECTING...", fg=WARNING)
        _draw_btn(state="wait")
        _start_pulse(WARNING)
    else:
        vpn_connected = False
        status_lbl.config(text="DISCONNECTED",  fg=DANGER)
        _draw_btn(state="off")
        _draw_ring(DANGER, 0.0)

    _update_tray()

def _start_pulse(color):
    global _pulse_phase
    _pulse_phase = 0.0
    _pulse_tick(color)

def _pulse_tick(color):
    global _pulse_phase, _pulse_job
    _pulse_phase += 0.08
    alpha = (math.sin(_pulse_phase) + 1) / 2
    _draw_ring(color, alpha)
    _pulse_job = root.after(50, lambda: _pulse_tick(color))

def _draw_ring(color, alpha):
    ring_canvas.delete("ring")
    cx = cy = 90
    for i in range(5, 0, -1):
        c = blend(color, BG, alpha * (i / 5) * 0.50)
        d = i * 3
        ring_canvas.create_oval(
            cx - RING_R - d, cy - RING_R - d,
            cx + RING_R + d, cy + RING_R + d,
            outline=c, width=1, tags="ring",
        )
    rim = blend(color, BG, max(alpha, 0.08))
    ring_canvas.create_oval(
        cx - RING_R, cy - RING_R,
        cx + RING_R, cy + RING_R,
        outline=rim, width=2, tags="ring",
    )

# ── POWER BUTTON ─────────────────────────────────────────────────────────────

BTN_R = 36

def _draw_btn(state="off"):
    cx = cy = 90
    btn_canvas.delete("all")
    if state == "on":
        fill, rim, icon = "#00180E", SUCCESS, SUCCESS
    elif state == "wait":
        fill, rim, icon = "#1A1408", WARNING, WARNING
    else:
        fill, rim, icon = "#0E1118", BORDER,  TEXT_DIM

    for i in range(4, 0, -1):
        c = blend(rim, BG, 0.07 * i)
        btn_canvas.create_oval(
            cx - BTN_R - i*2, cy - BTN_R - i*2,
            cx + BTN_R + i*2, cy + BTN_R + i*2,
            outline=c, width=1,
        )
    btn_canvas.create_oval(
        cx - BTN_R, cy - BTN_R,
        cx + BTN_R, cy + BTN_R,
        fill=fill, outline=rim, width=2,
    )
    btn_canvas.create_arc(
        cx - 19, cy - 19, cx + 19, cy + 19,
        start=120, extent=300,
        outline=icon, width=2, style="arc",
    )
    btn_canvas.create_line(
        cx, cy - 21, cx, cy - 10,
        fill=icon, width=2, capstyle="round",
    )

# ── TOML ─────────────────────────────────────────────────────────────────────

def generate_toml():
    return (
        'loglevel = "info"\n'
        'vpn_mode = "general"\n'
        'killswitch_enabled = true\n'
        'killswitch_allow_ports = []\n'
        'post_quantum_group_enabled = true\n'
        'exclusions = []\n'
        'dns_upstreams = ["tls://1.1.1.1"]\n'
        "\n"
        "[endpoint]\n"
        f'hostname = "{entry_hostname.get()}"\n'
        f'addresses = ["{entry_address.get()}"]\n'
        "has_ipv6 = true\n"
        f'username = "{entry_username.get()}"\n'
        f'password = "{entry_password.get()}"\n'
        'client_random = ""\n'
        "skip_verification = false\n"
        'certificate = ""\n'
        'upstream_protocol = "http2"\n'
        "anti_dpi = false\n"
        "\n"
        "[listener.tun]\n"
        'bound_if = ""\n'
        'included_routes = ["0.0.0.0/0", "2000::/3"]\n'
        'excluded_routes = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]\n'
        "mtu_size = 1280\n"
    )

def import_toml():
    path = filedialog.askopenfilename(filetypes=[("TOML files", "*.toml")])
    if not path:
        return
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    def extract(pattern):
        m = re.search(pattern, content)
        return m.group(1) if m else ""
    for e in (entry_hostname, entry_address, entry_username, entry_password):
        e.delete(0, tk.END)
    entry_hostname.insert(0, extract(r'hostname\s*=\s*"(.*?)"'))
    entry_address.insert(0,  extract(r'addresses\s*=\s*\["(.*?)"\]'))
    entry_username.insert(0, extract(r'username\s*=\s*"(.*?)"'))
    entry_password.insert(0, extract(r'password\s*=\s*"(.*?)"'))
    save_settings()
    messagebox.showinfo("Import", "TOML loaded successfully")

def export_toml():
    save_settings()
    path = filedialog.asksaveasfilename(
        defaultextension=".toml", filetypes=[("TOML files", "*.toml")]
    )
    if not path:
        return
    with open(path, "w", encoding="utf-8") as f:
        f.write(generate_toml())
    messagebox.showinfo("Export", "TOML saved")

# ── VPN ───────────────────────────────────────────────────────────────────────

def toggle_vpn():
    if vpn_connected or (vpn_process and vpn_process.poll() is None):
        stop_vpn()
    else:
        run_vpn()

def run_vpn():
    global vpn_process
    save_settings()
    tmp = os.path.join(tempfile.gettempdir(), "vpn_config.toml")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(generate_toml())

    cmd = [get_binary_path("trusttunnel_client.exe"), "-c", tmp]
    if entry_trust_flags.get().strip():
        cmd.extend(shlex.split(entry_trust_flags.get()))

    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW

    vpn_process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        startupinfo=si,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    set_status("connecting")
    threading.Thread(target=_read_output, daemon=True).start()

def _read_output():
    global vpn_process
    for line in iter(vpn_process.stdout.readline, ""):
        append_log(line)
        if "connected" in line.lower():
            set_status("connected")
    vpn_process.stdout.close()
    set_status("disconnected")

def stop_vpn():
    global vpn_process
    if vpn_process and vpn_process.poll() is None:
        vpn_process.terminate()
    subprocess.call(
        ["taskkill", "/f", "/im", "trusttunnel_client.exe"],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    set_status("disconnected")

def run_setup():
    save_settings()
    cmd = [get_binary_path("setup_wizard.exe")]
    if entry_setup_flags.get().strip():
        cmd.extend(shlex.split(entry_setup_flags.get()))
    subprocess.Popen(cmd, creationflags=subprocess.CREATE_NO_WINDOW)

# ── ADMIN CHECK ───────────────────────────────────────────────────────────────

if not is_admin():
    run_as_admin()
    sys.exit()

# ═════════════════════════════════════════════════════════════════════════════
#  BUILD GUI
# ═════════════════════════════════════════════════════════════════════════════

root = tk.Tk()
root.title(APP_NAME)
root.geometry("480x800")
root.resizable(False, False)
root.configure(bg=BG)

# ── scrollable viewport ───────────────────────────────────────────────────────

_outer = tk.Canvas(root, bg=BG, highlightthickness=0, bd=0)
_vsb   = tk.Scrollbar(root, orient="vertical", command=_outer.yview,
                      bg=SURFACE, troughcolor=BG, width=5)
_outer.configure(yscrollcommand=_vsb.set)
_vsb.pack(side="right", fill="y")
_outer.pack(side="left", fill="both", expand=True)

page = tk.Frame(_outer, bg=BG)
_outer.create_window((0, 0), window=page, anchor="nw", width=475)
page.bind("<Configure>",
          lambda e: _outer.configure(scrollregion=_outer.bbox("all")))
_outer.bind_all("<MouseWheel>",
    lambda e: _outer.yview_scroll(int(-1 * (e.delta / 120)), "units"))

# ── HEADER ────────────────────────────────────────────────────────────────────

hdr = tk.Frame(page, bg=BG)
hdr.pack(fill="x", padx=24, pady=(18, 10))

tk.Label(hdr, text="TRUST",  font=("Courier New", 20, "bold"),
         bg=BG, fg=ACCENT).pack(side="left")
tk.Label(hdr, text="TUNNEL", font=("Courier New", 20, "bold"),
         bg=BG, fg=TEXT).pack(side="left")
tk.Label(hdr, text=" v1.0",  font=FONT_XS,
         bg=BG, fg=TEXT_DIM).pack(side="left", pady=(10, 0))
tk.Label(hdr, text=" PRO ", font=("Courier New", 7, "bold"),
         bg=ACCENT2, fg="white").pack(side="right", pady=(10, 0))

tk.Frame(page, bg=BORDER, height=1).pack(fill="x", padx=20)

# ── HERO: power button ────────────────────────────────────────────────────────

hero = tk.Frame(page, bg=BG)
hero.pack(pady=26)

ring_canvas = tk.Canvas(hero, width=180, height=180,
                        bg=BG, highlightthickness=0)
ring_canvas.pack()

btn_canvas = tk.Canvas(ring_canvas, width=180, height=180,
                       bg=BG, highlightthickness=0, cursor="hand2")
btn_canvas.place(x=0, y=0)
btn_canvas.bind("<Button-1>", lambda e: toggle_vpn())

_draw_btn(state="off")
_draw_ring(DANGER, 0.0)

status_lbl = tk.Label(page, text="DISCONNECTED",
                      font=("Courier New", 13, "bold"),
                      bg=BG, fg=DANGER)
status_lbl.pack()

tk.Label(page, text="click to connect / disconnect",
         font=FONT_XS, bg=BG, fg=TEXT_DIM).pack(pady=(2, 0))

# ── SHARED WIDGET HELPERS ─────────────────────────────────────────────────────

class Section:
    """Collapsible accordion panel."""
    def __init__(self, title):
        tk.Frame(page, bg=BORDER, height=1).pack(fill="x", padx=20, pady=(14, 0))

        self._hdr = tk.Frame(page, bg=SURFACE, cursor="hand2")
        self._hdr.pack(fill="x", padx=20)

        self._arrow = tk.Label(self._hdr, text="▸", font=FONT_LBL,
                               bg=SURFACE, fg=ACCENT, width=2)
        self._arrow.pack(side="left", padx=(12, 4), pady=11)
        tk.Label(self._hdr, text=title, font=FONT_H,
                 bg=SURFACE, fg=TEXT).pack(side="left", pady=11)

        self.body = tk.Frame(page, bg=SURFACE)
        self.open  = False

        for w in [self._hdr] + list(self._hdr.winfo_children()):
            w.bind("<Button-1>", self._toggle)
        self._hdr.bind("<Enter>", lambda e: self._hdr.config(bg=HOVER))
        self._hdr.bind("<Leave>", lambda e: self._hdr.config(bg=SURFACE))

    def _toggle(self, _e=None):
        if self.open:
            self.body.pack_forget()
            self._arrow.config(text="▸")
        else:
            self.body.pack(fill="x", padx=20)
            self._arrow.config(text="▾")
        self.open = not self.open

def mk_entry(parent, label, show=None):
    tk.Label(parent, text=label, font=FONT_LBL,
             bg=SURFACE, fg=TEXT_DIM, anchor="w").pack(
                 fill="x", padx=16, pady=(10, 1))
    wrap = tk.Frame(parent, bg=BORDER, padx=1, pady=1)
    wrap.pack(fill="x", padx=16, pady=(0, 2))
    e = tk.Entry(wrap, font=FONT_MONO, show=show,
                 bg=SURFACE2, fg=TEXT, insertbackground=ACCENT,
                 relief="flat", bd=0)
    e.pack(fill="x", ipady=7, padx=1, pady=1)
    e.bind("<FocusIn>",  lambda ev, w=wrap: w.config(bg=ACCENT))
    e.bind("<FocusOut>", lambda ev, w=wrap: w.config(bg=BORDER))
    return e

def mk_btn(parent, text, cmd, fg=TEXT, bg=SURFACE2):
    wrap = tk.Frame(parent, bg=BORDER, padx=1, pady=1, cursor="hand2")
    lbl  = tk.Label(wrap, text=text, font=FONT_LBL,
                    bg=bg, fg=fg, padx=12, pady=8, cursor="hand2")
    lbl.pack(fill="both")
    for w in (wrap, lbl):
        w.bind("<Button-1>", lambda e: cmd())
    lbl.bind("<Enter>", lambda e: lbl.config(bg=HOVER))
    lbl.bind("<Leave>", lambda e: lbl.config(bg=bg))
    return wrap

def spacer(parent, h=12):
    tk.Frame(parent, bg=SURFACE, height=h).pack()

# ── SECTION 1 · Connection ────────────────────────────────────────────────────

s1 = Section("  CONNECTION")
entry_hostname = mk_entry(s1.body, "HOSTNAME / DOMAIN")
entry_address  = mk_entry(s1.body, "IP : PORT")
entry_username = mk_entry(s1.body, "USERNAME")
entry_password = mk_entry(s1.body, "PASSWORD", show="●")
spacer(s1.body)

# ── SECTION 2 · Advanced flags ────────────────────────────────────────────────

s2 = Section("  ADVANCED FLAGS")
entry_trust_flags = mk_entry(s2.body, "TRUSTTUNNEL EXTRA FLAGS")
entry_setup_flags = mk_entry(s2.body, "SETUP WIZARD FLAGS")
bf = tk.Frame(s2.body, bg=SURFACE)
bf.pack(fill="x", padx=16, pady=(10, 14))
mk_btn(bf, "⚙   RUN SETUP WIZARD", run_setup, fg=ACCENT).pack(side="left")

# ── SECTION 3 · TOML ─────────────────────────────────────────────────────────

s3 = Section("  TOML CONFIG")
tf = tk.Frame(s3.body, bg=SURFACE)
tf.pack(fill="x", padx=16, pady=14)
mk_btn(tf, "📂  IMPORT TOML", import_toml).pack(side="left", padx=(0, 8))
mk_btn(tf, "💾  EXPORT TOML", export_toml).pack(side="left")

# ── SECTION 4 · Settings ─────────────────────────────────────────────────────

s4 = Section("  SETTINGS")

auto_var = tk.BooleanVar(value=autostart_enabled())

def _on_autostart():
    ok = set_autostart(auto_var.get())
    if not ok:
        auto_var.set(not auto_var.get())
        messagebox.showerror(
            "Error",
            "Could not write to registry.\n"
            "Try running the app as Administrator."
        )

cb_row = tk.Frame(s4.body, bg=SURFACE)
cb_row.pack(fill="x", padx=16, pady=14)

cb = tk.Checkbutton(
    cb_row,
    text=" Launch with Windows  (starts hidden in tray, VPN off)",
    variable=auto_var,
    command=_on_autostart,
    font=FONT_LBL,
    bg=SURFACE,         fg=TEXT,
    activebackground=SURFACE, activeforeground=ACCENT,
    selectcolor=SURFACE2,
    relief="flat", bd=0, cursor="hand2",
)
cb.pack(anchor="w")

# small hint
tk.Label(s4.body,
         text="  When enabled the app is added to HKCU\\...\\Run",
         font=FONT_XS, bg=SURFACE, fg=TEXT_FAINT, anchor="w").pack(
             fill="x", padx=16, pady=(0, 14))

# ── SECTION 5 · Live log ──────────────────────────────────────────────────────

s5 = Section("  LIVE LOG")

log_box = tk.Text(
    s5.body,
    font=("Courier New", 7), height=14,
    bg="#07090F", fg="#3ABFFF",
    insertbackground=ACCENT,
    relief="flat", bd=0,
    state="disabled",
    wrap="none",
    highlightthickness=0,
    selectbackground=SURFACE2,
    padx=6, pady=6,
)
log_box.pack(fill="x", padx=16, pady=(4, 0))

def _clear_log():
    log_box.config(state="normal")
    log_box.delete("1.0", tk.END)
    log_box.config(state="disabled")

clr = tk.Label(s5.body, text="CLEAR LOG", font=FONT_XS,
               bg=SURFACE, fg=TEXT_DIM, cursor="hand2", pady=8)
clr.pack(anchor="e", padx=20)
clr.bind("<Button-1>", lambda e: _clear_log())
clr.bind("<Enter>", lambda e: clr.config(fg=ACCENT))
clr.bind("<Leave>", lambda e: clr.config(fg=TEXT_DIM))

# ── FOOTER ────────────────────────────────────────────────────────────────────

tk.Frame(page, bg=BORDER, height=1).pack(fill="x", padx=20, pady=(18, 0))
tk.Label(page,
         text="TrustTunnel Client PRO  ·  encrypted · secure · private",
         font=FONT_XS, bg=BG, fg=TEXT_FAINT, pady=14).pack()

# ═════════════════════════════════════════════════════════════════════════════
#  WIRE UP TRAY + CLOSE BEHAVIOUR
# ═════════════════════════════════════════════════════════════════════════════

# Override X button → hide to tray
root.protocol("WM_DELETE_WINDOW", on_window_close)

# Start hidden when launched via autostart (--minimized flag)
if START_MINIMIZED:
    root.withdraw()

# Start tray icon in background thread
threading.Thread(target=_start_tray, daemon=True).start()

# ── LAUNCH ───────────────────────────────────────────────────────────────────

load_settings()
root.after(100, process_queue)
root.mainloop()
