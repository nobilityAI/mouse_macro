# sample_app.py
# One-file prototype of the mouse-gesture -> key macro app (global-first) + Mini HUD.
# - Always-global: starts a global mouse listener at launch; the Tk window is a visualizer.
# - Mini HUD: small always-on-top mirror of the main visualizer (toggle in top bar).
# - Virtual cursor recenters to canvas middle after inactivity_reset_ms without deliberate motion.
# - Speed calculation uses per-event timestamps from the global hook (stable px/s).
# - Tabs: Main (visualizer), Macros (CRUD), Settings (thresholds incl. inactivity reset).
# - Engine: speed filter (px/s), direction quantization (U/R/D/L), edge-activation, debounce,
#           optional reset-between-hits, sequence match -> key output, JSON persistence.
#
# Run: python sample_app.py
# Requires: pip install pynput platformdirs  (optional macOS overlay: pip install pyobjc)

import json, math, queue, time, re, sys
from collections import deque
from dataclasses import dataclass, asdict
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

# Optional features gated on pynput availability
HAVE_PYNPUT = True
try:
    from pynput import mouse, keyboard
except Exception:
    HAVE_PYNPUT = False

# Platformdirs for config path (fall back to home if missing)
# ---- robust, cross-platform config dir selection ----
import os, sys
from pathlib import Path

def _pick_conf_dir() -> Path:
    # 1) Allow manual override via env var (handy when testing)
    override = os.environ.get("GESTUREKEYS_CONF_DIR")
    if override:
        return Path(override)

    # 2) Try platformdirs (nice OS-native locations)
    try:
        import platformdirs  # pip install platformdirs
        app_name = "GestureKeys"
        app_author = "YourNameOrOrg"  # optional
        return Path(platformdirs.user_config_dir(app_name, app_author))
    except Exception:
        pass

    # 3) Fallbacks by platform
    if sys.platform == "win32":
        return Path.home() / "AppData" / "Local" / "GestureKeys"
    elif sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "GestureKeys"
    else:
        return Path.home() / ".gesturekeys"

CONF_DIR = _pick_conf_dir()
try:
    CONF_DIR.mkdir(parents=True, exist_ok=True)
except Exception as e:
    # last-ditch fallback to a directory we can likely write
    CONF_DIR = Path.cwd() / "GestureKeysConfig"
    CONF_DIR.mkdir(parents=True, exist_ok=True)

CONF_FILE = CONF_DIR / "config.json"

# Optional: a tiny diagnostic so you can see the chosen folder in your terminal
print(f"[GestureKeys] Using config dir: {CONF_DIR}")


# ----------------------------- Canvas metrics (mutable) -----------------------------
# Cursor GUI canvas size (independent from outer window size)
W, H = 420, 420
CX, CY = W // 2, H // 2

# Fixed activation band thickness (no longer user-configurable).
BAND_X = 24
BAND_Y = 24

# ----------------------------- Data models -----------------------------

@dataclass
class Settings:
    speed_threshold_px_s: float = 1000      # min median speed to treat as deliberate
    activation_x_px: int = 350               # CURSOR GUI (canvas) WIDTH (px)
    activation_y_px: int = 350               # CURSOR GUI (canvas) HEIGHT (px)
    reset_radius_px: int = 50                # reset circle radius
    debounce_ms: int = 50                   # min time between accepted hits
    require_reset_between_hits: bool = False  # cursor must pass through reset circle before next hit
    trace_len: int = 30                     # segments kept in draw trace
    use_global_mouse: bool = True            # always-global default
    angle_mode: str = "axis"                 # "axis" uses abs(dx)>abs(dy) heuristic
    inactivity_reset_ms: int = 150           # recenter virtual cursor if no deliberate motion

@dataclass
class Macro:
    pattern: list[str]   # e.g., ["U","U","D"]
    key: str             # e.g., "b", "esc", "ctrl+a", "cmd+c, cmd+v"
    name: str = ""

@dataclass
class Store:
    settings: Settings
    macros: list[Macro]

    @staticmethod
    def load() -> "Store":
        if CONF_FILE.exists():
            try:
                data = json.loads(CONF_FILE.read_text(encoding="utf-8"))
                s = Settings(**data.get("settings", {}))
                ms = [Macro(**m) for m in data.get("macros", [])]
                return Store(settings=s, macros=ms)
            except Exception:
                pass
        return Store(settings=Settings(), macros=[])

    def save(self):
        data = {
            "settings": asdict(self.settings),
            "macros": [asdict(m) for m in self.macros],
        }
        CONF_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")

# ----------------------------- Gesture Engine -----------------------------

class GestureEngine:
    """
    Consumes cursor samples (t, x, y) in canvas coordinates.
    Emits direction hits when:
      - median speed >= threshold,
      - cursor is inside the edge band for that direction,
      - optional reset-circle requirement satisfied,
      - debounce observed.
    Tracks last deliberate-activity time to support inactivity recentering.
    """
    def __init__(self, settings: Settings, on_hit, on_match):
        self.s = settings
        self.on_hit = on_hit
        self.on_match = on_match

        self.samples = deque(maxlen=64)  # (t, x, y)
        self.seq: list[str] = []
        self.last_accept_ts = 0.0
        self.inside_reset = False
        self.need_reset = False

        # For UI
        self.last_dir = None
        self.speed_median = 0.0

        # Activity markers
        self.last_above_threshold_ts = 0.0
        self.last_sample_ts = 0.0

        self.macros: list[Macro] = []

    def update_settings(self, s: Settings):
        self.s = s

    def set_macros(self, macros: list[Macro]):
        self.macros = macros

    def reset(self, t: float | None = None):
        self.samples.clear()
        self.seq.clear()
        self.last_dir = None
        self.speed_median = 0.0
        self.inside_reset = False
        self.need_reset = False
        if t is None:
            t = time.time()
        self.last_accept_ts = 0.0
        self.last_above_threshold_ts = 0.0
        self.last_sample_ts = t

    @staticmethod
    def _median(vals):
        if not vals:
            return 0.0
        v = sorted(vals)
        n = len(v)
        m = n // 2
        if n % 2:
            return v[m]
        return 0.5 * (v[m-1] + v[m])

    def _compute_speed_and_dir(self):
        if len(self.samples) < 3:
            return 0.0, None, (0.0, 0.0)

        speeds, dxs, dys = [], [], []
        for i in range(1, min(6, len(self.samples))):
            t0, x0, y0 = self.samples[-i-1]
            t1, x1, y1 = self.samples[-i]
            dt = max(1e-4, t1 - t0)
            dx, dy = (x1 - x0), (y1 - y0)
            v = math.hypot(dx, dy) / dt
            speeds.append(v)
            dxs.append(dx); dys.append(dy)

        v_med = self._median(speeds)
        dx_sum = sum(dxs[-3:])
        dy_sum = sum(dys[-3:])

        if self.s.angle_mode == "axis":
            if abs(dx_sum) > abs(dy_sum):
                dirc = "R" if dx_sum > 0 else "L"
            else:
                dirc = "D" if dy_sum > 0 else "U"
        else:
            ang = math.degrees(math.atan2(-dy_sum, dx_sum))  # y up
            a = (ang + 360) % 360
            if a <= 45 or a > 315: dirc = "R"
            elif a <= 135:         dirc = "U"
            elif a <= 225:         dirc = "L"
            else:                  dirc = "D"

        return v_med, dirc, (dx_sum, dy_sum)

    def _in_band(self, x, y, d: str) -> bool:
        ax, ay = BAND_X, BAND_Y
        if d == "U":  return y <= ay
        if d == "D":  return y >= (H - ay)
        if d == "L":  return x <= ax
        if d == "R":  return x >= (W - ax)
        return False

    def _inside_reset_circle(self, x, y) -> bool:
        return (x - CX)**2 + (y - CY)**2 <= (self.s.reset_radius_px**2)

    def push(self, t, x, y):
        self.last_sample_ts = t

        was = self.inside_reset
        self.inside_reset = self._inside_reset_circle(x, y)
        if self.inside_reset and not was:
            if self.s.require_reset_between_hits:
                self.need_reset = False

        self.samples.append((t, x, y))
        v, d, _ = self._compute_speed_and_dir()
        self.speed_median = v
        self.last_dir = d

        if d is not None and v >= self.s.speed_threshold_px_s:
            self.last_above_threshold_ts = t

        now = t
        if d is None or v < self.s.speed_threshold_px_s:
            return

        _, xh, yh = self.samples[-1]
        if not self._in_band(xh, yh, d):
            return

        if (now - self.last_accept_ts) * 1000.0 < self.s.debounce_ms:
            return

        if self.s.require_reset_between_hits and self.need_reset:
            return

        self.last_accept_ts = now
        self.seq.append(d)
        self.on_hit(d)

        if self.s.require_reset_between_hits:
            self.need_reset = True

        if self.macros:
            for m in self.macros:
                if len(self.seq) >= len(m.pattern) and self.seq[-len(m.pattern):] == m.pattern:
                    self.on_match(m)
                    self.seq.clear()
                    break

# ----------------------------- Mini HUD -----------------------------

class MiniHUD:
    """Small always-on-top mirror window showing cursor, trace, reset circle, and edge flashes."""
    def __init__(self, app, target_width=220):
        self.app = app
        self.win = tk.Toplevel(app.root)
        self.win.title("Gesture HUD")
        self.win.wm_attributes("-topmost", 1)
        try:
            self.win.wm_attributes("-toolwindow", 1)  # Windows hint; harmless elsewhere
        except Exception:
            pass

        self.target_width = target_width
        self.scale = 1.0
        self.w = 1
        self.h = 1

        self.canvas = tk.Canvas(self.win, bg="black", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)

        self.update_dimensions()
        self.win.protocol("WM_DELETE_WINDOW", self._on_close)
        self._promote_on_macos()

    def _on_close(self):
        try:
            if self.app.var_hud.get():
                self.app.var_hud.set(False)
        except Exception:
            pass
        try:
            self.app.hud = None
        except Exception:
            pass
        self.destroy()

    def destroy(self):
        try:
            if self.canvas:
                self.canvas.destroy()
        except Exception:
            pass
        try:
            if self.win:
                self.win.destroy()
        except Exception:
            pass
        self.canvas = None
        self.win = None

    # --- macOS full-screen helpers (best-effort) ---
    def _promote_on_macos(self):
        if sys.platform != "darwin":
            return
        try:
            self.app.root.tk.call('::tk::unsupported::MacWindowStyle', 'style', self.win._w, 'help', 'floating')
        except Exception:
            pass
        try:
            from AppKit import NSApp, NSStatusWindowLevel, NSWindowCollectionBehaviorCanJoinAllSpaces
            try:
                from AppKit import NSWindowCollectionBehaviorFullScreenAuxiliary
                aux_flag = NSWindowCollectionBehaviorFullScreenAuxiliary
            except Exception:
                aux_flag = 0
            nsapp = NSApp()
            for w in nsapp.windows():
                try:
                    if str(w.title()) == "Gesture HUD":
                        w.setLevel_(NSStatusWindowLevel)
                        beh = int(w.collectionBehavior()) | int(NSWindowCollectionBehaviorCanJoinAllSpaces) | int(aux_flag)
                        w.setCollectionBehavior_(beh)
                        w.orderFrontRegardless()
                        break
                except Exception:
                    continue
        except Exception:
            pass

    def nudge_front(self):
        if sys.platform != "darwin":
            return
        try:
            from AppKit import NSApp
            nsapp = NSApp()
            for w in nsapp.windows():
                try:
                    if str(w.title()) == "Gesture HUD":
                        w.orderFrontRegardless()
                        break
                except Exception:
                    continue
        except Exception:
            pass

    def update_dimensions(self):
        global W, H
        self.scale = max(0.1, min(2.0, self.target_width / max(1, W)))
        self.w = max(120, int(W * self.scale))
        self.h = max(120, int(H * self.scale))
        if self.canvas:
            self.canvas.config(width=self.w, height=self.h)
        if self.win:
            self.win.geometry(f"{self.w}x{self.h}")
        self.draw_static()

    def _sx(self, x):  # scale x
        return int(x * self.scale)
    def _sy(self, y):  # scale y
        return int(y * self.scale)

    def draw_static(self):
        if not self.canvas:
            return
        try:
            self.canvas.delete("static"); self.canvas.delete("bg")
        except Exception:
            pass
        # simple dark background fill (no main GUI gradient anymore)
        self.canvas.create_rectangle(0, 0, self.w, self.h, fill="black", outline="", tags="bg")
        # reset circle
        rr = self.app.store.settings.reset_radius_px
        cx, cy = self._sx(CX), self._sy(CY)
        self.canvas.create_oval(cx-rr*self.scale, cy-rr*self.scale,
                                cx+rr*self.scale, cy+rr*self.scale,
                                outline="#00FF00", width=2, tags="static")
        # activation bands
        self.canvas.create_rectangle(0, 0, self._sx(W), self._sy(BAND_Y),
                                     outline="", fill="#003300", tags="static")
        self.canvas.create_rectangle(0, self._sy(H-BAND_Y), self._sx(W), self._sy(H),
                                     outline="", fill="#003300", tags="static")
        self.canvas.create_rectangle(0, 0, self._sx(BAND_X), self._sy(H),
                                     outline="", fill="#003300", tags="static")
        self.canvas.create_rectangle(self._sx(W-BAND_X), 0, self._sx(W), self._sy(H),
                                     outline="", fill="#003300", tags="static")

    def flash_edge(self, d):
        color = "#77FF77"
        r = None
        if d == "U": r = (0, 0, self._sx(W), self._sy(BAND_Y))
        if d == "D": r = (0, self._sy(H-BAND_Y), self._sx(W), self._sy(H))
        if d == "L": r = (0, 0, self._sx(BAND_X), self._sy(H))
        if d == "R": r = (self._sx(W-BAND_X), 0, self._sx(W), self._sy(H))
        if not r: return
        if self.canvas is None or self.win is None: return
        try:
            rid = self.canvas.create_rectangle(*r, outline="", fill=color, stipple="gray50", tags="hudflash")
        except Exception:
            return
        def safe_del():
            try:
                if self.canvas and self.canvas.winfo_exists():
                    self.canvas.delete(rid)
            except Exception:
                pass
        try:
            if self.win and self.win.winfo_exists():
                self.win.after(120, safe_del)
        except Exception:
            pass

    def update(self):
        if self.win is None or self.canvas is None:
            return
        try:
            if not self.win.winfo_exists() or not self.canvas.winfo_exists():
                return
        except Exception:
            return
        try:
            self.canvas.delete("trace")
            tlist = list(self.app.trace)
            if len(tlist) >= 2:
                x0, y0 = tlist[0]
                for x1, y1 in tlist[1:]:
                    self.canvas.create_line(self._sx(x0), self._sy(y0),
                                            self._sx(x1), self._sy(y1),
                                            fill="#00AA00", tags="trace")
                    x0, y0 = x1, y1
            # cursor dot
            self.canvas.delete("cursor")
            self.canvas.create_oval(self._sx(self.app.vx)-3, self._sy(self.app.vy)-3,
                                    self._sx(self.app.vx)+3, self._sy(self.app.vy)+3,
                                    outline="", fill="#00FF00", tags="cursor")
        except Exception:
            return

# ----------------------------- App / UI -----------------------------

class App:
    def __init__(self, root):
        self.root = root
        self.root.title("GestureKeys")

        # Load store, adopt stored size for canvas W/H
        self.store = Store.load()
        global W, H, CX, CY
        W = max(200, int(self.store.settings.activation_x_px))
        H = max(200, int(self.store.settings.activation_y_px))
        CX, CY = W // 2, H // 2

        # Ensure config file exists on first launch
        try:
            self.store.save()
        except Exception:
            pass

        # ---------- Outer window ----------
        self.root.configure(bg="black")
        # Make the app open wide so Macros "Add" is visible without manual resize
        self._apply_initial_window_size()
        # Also set a reasonable minimum size
        try:
            self.root.minsize(1100, 800)
        except Exception:
            pass

        # Keyboard injector
        self.kb = keyboard.Controller() if HAVE_PYNPUT else None

        # Top bar
        top = ttk.Frame(root); top.pack(fill=tk.X, side=tk.TOP)
        self.pause = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Pause", variable=self.pause).pack(side=tk.LEFT, padx=6, pady=6)

        # Mini HUD toggle
        self.var_hud = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Mini HUD", variable=self.var_hud, command=self._toggle_hud)\
            .pack(side=tk.LEFT, padx=6)

        self.status = tk.StringVar(value="Ready")
        ttk.Label(top, textvariable=self.status).pack(side=tk.RIGHT, padx=6)

        # Notebook
        nb = ttk.Notebook(root)
        nb.pack(fill=tk.BOTH, expand=True)

        # Main tab: visualizer (simple black background, no gradient)
        self.tab_main = ttk.Frame(nb)
        nb.add(self.tab_main, text="Main")

        # IMPORTANT: fixed-size canvas; do NOT expand with the window
        self.canvas = tk.Canvas(self.tab_main, width=W, height=H, bg="black", highlightthickness=0)
        self.canvas.pack(padx=12, pady=12)  # centered with padding, no fill/expand

        # Mini HUD handle
        self.hud: MiniHUD | None = None

        # Static overlays
        self._draw_static()

        # Info line
        self.info = tk.StringVar(value="")
        ttk.Label(self.tab_main, textvariable=self.info).pack(side=tk.TOP, pady=(0,6))

        # Virtual cursor & trace
        self.vx, self.vy = CX, CY
        self.trace = deque(maxlen=self.store.settings.trace_len)

        # Engine (create BEFORE any recenter)
        self.engine = GestureEngine(
            self.store.settings,
            on_hit=self._on_dir_hit,
            on_match=self._on_macro_match,
        )
        self.engine.set_macros(self.store.macros)

        # Global listener infra
        self.global_listener = None
        self.global_q = queue.Queue()
        self.last_global_xy = None

        # Macros & Settings tabs
        self._build_macros_tab(nb)
        self._build_settings_tab(nb)

        # Start global immediately (always-global paradigm)
        if HAVE_PYNPUT:
            self._start_global()
        else:
            self.status.set("pynput not available: global tracking disabled")

        # Window close & UI refresh
        self.root.protocol("WM_DELETE_WINDOW", self._on_quit)
        self._hud_nudge = 0
        self.root.after(16, self._pump)
        self.root.after(200, self._refresh_info)

    # ---------- Opening window size (outer window only) ----------

    def _apply_initial_window_size(self):
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        # Prefer a wide window; clamp to screen
        outer_w = min(max(1200, int(sw * 0.75)), sw - 80)
        outer_h = min(max(820,  int(sh * 0.75)), sh - 80)
        self.root.geometry(f"{outer_w}x{outer_h}")

    # ---------- Tabs ----------

    def _build_macros_tab(self, nb):
        f = ttk.Frame(nb); nb.add(f, text="Macros")

        builder = ttk.LabelFrame(f, text="Add Macro")
        builder.pack(fill=tk.X, padx=8, pady=8)

        self.build_seq = tk.StringVar(value="")
        def append_dir(d):
            self.build_seq.set(self.build_seq.get() + d)
        for d in ("U","R","D","L"):
            ttk.Button(builder, text=d, width=3, command=lambda d=d: append_dir(d)).pack(side=tk.LEFT, padx=3, pady=6)
        ttk.Button(builder, text="Reset", command=lambda: self.build_seq.set("")).pack(side=tk.LEFT, padx=6)
        ttk.Label(builder, text="Pattern:").pack(side=tk.LEFT, padx=(12,3))
        ttk.Entry(builder, textvariable=self.build_seq, width=16).pack(side=tk.LEFT)

        self.build_key = tk.StringVar(value="")
        ttk.Label(builder, text="Output:").pack(side=tk.LEFT, padx=(12,3))
        ttk.Entry(builder, textvariable=self.build_key, width=22).pack(side=tk.LEFT)
        self.build_name = tk.StringVar(value="")
        ttk.Label(builder, text="Name:").pack(side=tk.LEFT, padx=(12,3))
        ttk.Entry(builder, textvariable=self.build_name, width=14).pack(side=tk.LEFT)

        ttk.Button(builder, text="Add", command=self._add_macro).pack(side=tk.LEFT, padx=8)

        listf = ttk.LabelFrame(f, text="Saved Macros")
        listf.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0,8))
        self.mlist = tk.Listbox(listf)
        self.mlist.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8,0), pady=8)
        self._refresh_mlist()
        sb = ttk.Scrollbar(listf, orient="vertical", command=self.mlist.yview)
        sb.pack(side=tk.LEFT, fill=tk.Y, pady=8)
        self.mlist.configure(yscrollcommand=sb.set)

        btns = ttk.Frame(listf); btns.pack(side=tk.RIGHT, fill=tk.Y, padx=8, pady=8)
        ttk.Button(btns, text="Delete Selected", command=self._del_selected_macro).pack(fill=tk.X)
        ttk.Button(btns, text="Export JSON", command=self._export_macros).pack(fill=tk.X, pady=6)
        ttk.Button(btns, text="Import JSON", command=self._import_macros).pack(fill=tk.X)

    def _build_settings_tab(self, nb):
        f = ttk.Frame(nb); nb.add(f, text="Settings")
        s = self.store.settings

        try:
            ttk.Style().configure('Note.TLabel', foreground='#8aa39b', font=('TkDefaultFont', 9))
        except Exception:
            pass

        def add_row(row, label, var, note, w=12):
            ttk.Label(f, text=label).grid(row=row, column=0, sticky="w", padx=8, pady=(10,0))
            e = ttk.Entry(f, textvariable=var, width=w)
            e.grid(row=row, column=1, sticky="w", pady=(10,0))
            ttk.Label(f, text=note, style='Note.TLabel', wraplength=360, justify="left")\
                .grid(row=row+1, column=0, columnspan=3, sticky="w", padx=8, pady=(2,0))
            return e

        self.var_speed  = tk.DoubleVar(value=s.speed_threshold_px_s)
        self.var_ax     = tk.IntVar(value=s.activation_x_px)   # cursor GUI width
        self.var_ay     = tk.IntVar(value=s.activation_y_px)   # cursor GUI height
        self.var_rr     = tk.IntVar(value=s.reset_radius_px)
        self.var_db     = tk.IntVar(value=s.debounce_ms)
        self.var_req    = tk.BooleanVar(value=s.require_reset_between_hits)
        self.var_trace  = tk.IntVar(value=s.trace_len)
        self.var_inact  = tk.IntVar(value=s.inactivity_reset_ms)

        add_row(0,  "Speed threshold (px/s)", self.var_speed, "Minimum median px/s considered deliberate.")
        add_row(2,  "Cursor GUI Width (px)",  self.var_ax,   "Width of the black visualizer canvas.")
        add_row(4,  "Cursor GUI Height (px)", self.var_ay,   "Height of the black visualizer canvas.")
        add_row(6,  "Reset radius (px)",      self.var_rr,   "Size of green center circle (reset zone).")
        add_row(8,  "Debounce (ms)",          self.var_db,   "Minimum ms between accepted hits to suppress jitter.")
        add_row(10, "Trace length (segments)",self.var_trace,"How many segments of the gesture trail to draw.")
        add_row(12, "Inactivity reset (ms)",  self.var_inact,"Recenter after this many ms without deliberate motion.")
        ttk.Checkbutton(f, text="Require reset between hits", variable=self.var_req)\
            .grid(row=14, column=0, columnspan=2, padx=8, pady=(10,0), sticky="w")

        ttk.Button(f, text="Apply", command=self._apply_settings).grid(row=16, column=0, padx=8, pady=12, sticky="w")

    # ---------- Apply Settings (cursor GUI only; autosave) ----------

    def _apply_settings(self):
        from collections import deque as _deque

        s = self.store.settings
        try:
            new_speed   = float(self.var_speed.get())
            newW        = max(200, int(self.var_ax.get()))   # cursor GUI width
            newH        = max(200, int(self.var_ay.get()))   # cursor GUI height
            new_rr      = int(self.var_rr.get())
            new_db      = int(self.var_db.get())
            new_trace   = max(50, int(self.var_trace.get()))
            new_req     = bool(self.var_req.get())
            new_inact   = max(50, int(self.var_inact.get()))
        except Exception as ex:
            messagebox.showerror("Invalid settings", str(ex))
            return

        # Update settings object
        s.speed_threshold_px_s       = new_speed
        s.reset_radius_px            = new_rr
        s.debounce_ms                = new_db
        s.trace_len                  = new_trace
        s.require_reset_between_hits = new_req
        s.inactivity_reset_ms        = new_inact

        # Update engine
        self.engine.update_settings(s)

        # Resize cursor GUI canvas only (do NOT touch outer window geometry here)
        global W, H, CX, CY
        W, H = newW, newH
        CX, CY = W // 2, H // 2
        try:
            self.canvas.config(width=W, height=H)
        except Exception:
            pass

        # Reallocate/trim trace buffer
        if len(self.trace) > new_trace:
            self.trace = _deque(list(self.trace)[-new_trace:], maxlen=new_trace)
        else:
            self.trace = _deque(self.trace, maxlen=new_trace)

        # Redraw overlays and recenter virtual cursor
        self._draw_static()
        self._recenter_virtual_cursor()

        # HUD needs to rescale with canvas size
        if getattr(self, "hud", None):
            try:
                self.hud.update_dimensions()
            except Exception:
                pass

        # Persist settings (store canvas size)
        try:
            s.activation_x_px = W
            s.activation_y_px = H
            self.store.save()
        except Exception:
            pass

        self.status.set("Settings applied")

    # ---------- Drawing (inside cursor GUI) ----------

    def _draw_static(self):
        try:
            self.canvas.delete("static")
        except Exception:
            pass
        rr = self.store.settings.reset_radius_px
        self.canvas.create_oval(CX-rr, CY-rr, CX+rr, CY+rr,
                                outline="#00FF00", width=2, tags="static")
        self.canvas.create_rectangle(0, 0, W, BAND_Y, outline="", fill="#003300", tags="static")          # top
        self.canvas.create_rectangle(0, H-BAND_Y, W, H, outline="", fill="#003300", tags="static")        # bottom
        self.canvas.create_rectangle(0, 0, BAND_X, H, outline="", fill="#003300", tags="static")          # left
        self.canvas.create_rectangle(W-BAND_X, 0, W, H, outline="", fill="#003300", tags="static")        # right
        if getattr(self, "hud", None):
            self.hud.update_dimensions()

    def _draw_trace(self):
        if len(self.trace) < 2:
            return
        x0, y0 = self.trace[0]
        for x1, y1 in list(self.trace)[1:]:
            self.canvas.create_line(x0, y0, x1, y1, fill="#00AA00", tags="trace")
            x0, y0 = x1, y1

    def _flash_edge(self, d):
        color = "#77FF77"
        r = None
        if d == "U": r = (0, 0, W, BAND_Y)
        if d == "D": r = (0, H-BAND_Y, W, H)
        if d == "L": r = (0, 0, BAND_X, H)
        if d == "R": r = (W-BAND_X, 0, W, H)
        if not r: return
        rid = self.canvas.create_rectangle(*r, outline="", fill=color, stipple="gray50")
        self.root.after(120, lambda: self.canvas.delete(rid))
        if self.hud:
            self.hud.flash_edge(d)

    # ---------- Global hook ----------

    def _start_global(self):
        if not HAVE_PYNPUT:
            return
        if self.global_listener:
            self.global_listener.stop()
            self.global_listener = None

        def on_move(x, y):
            self.global_q.put((time.time(), x, y))  # (t, x, y)

        self.global_listener = mouse.Listener(on_move=on_move)
        self.global_listener.start()
        self.store.settings.use_global_mouse = True
        self._recenter_virtual_cursor()
        self.status.set("Global mouse started (grant Accessibility on macOS if needed)")

    # ---------- Cursor / engine management ----------

    def _on_quit(self):
        try:
            self.store.save()
        except Exception:
            pass
        try:
            if self.global_listener:
                self.global_listener.stop()
        except Exception:
            pass
        self.root.destroy()

    def _recenter_virtual_cursor(self):
        global CX, CY
        CX, CY = W // 2, H // 2
        self.vx, self.vy = CX, CY
        self.trace.clear()
        self.trace.append((self.vx, self.vy))
        self.engine.reset(time.time())
        t0 = time.time()
        self.engine.push(t0, CX, CY)
        self.engine.push(t0 + 0.01, CX, CY)
        if self.hud:
            self.hud.update_dimensions()

    # ---------- Mini HUD toggle ----------

    def _toggle_hud(self):
        if self.var_hud.get():
            if not self.hud:
                self.hud = MiniHUD(self)
        else:
            if self.hud:
                self.hud.destroy()
                self.hud = None

    # ---------- Pump loop ----------

    def _pump(self):
        if not self.pause.get() and self.store.settings.use_global_mouse and HAVE_PYNPUT:
            try:
                while True:
                    t, x, y = self.global_q.get_nowait()
                    if self.last_global_xy is None:
                        self.last_global_xy = (x, y)
                        continue
                    dx = x - self.last_global_xy[0]
                    dy = y - self.last_global_xy[1]
                    self.last_global_xy = (x, y)

                    self.vx = max(0, min(W-1, self.vx + dx))
                    self.vy = max(0, min(H-1, self.vy + dy))
                    self.trace.append((self.vx, self.vy))
                    self.engine.push(t, self.vx, self.vy)
            except queue.Empty:
                pass

        now = time.time()
        inact_ms = self.store.settings.inactivity_reset_ms
        last_delib = self.engine.last_above_threshold_ts or 0.0
        if (now - last_delib) * 1000.0 >= inact_ms:
            if abs(self.vx - CX) > 1 or abs(self.vy - CY) > 1:
                self._recenter_virtual_cursor()

        self.canvas.delete("trace")
        self._draw_trace()
        if self.hud:
            try:
                self.hud.update()
            except Exception:
                try: self.var_hud.set(False)
                except Exception: pass
                self.hud = None
            else:
                self._hud_nudge = (self._hud_nudge + 1) % 60
                if self._hud_nudge == 0:
                    try:
                        self.hud.nudge_front()
                    except Exception:
                        pass

        self.root.after(16, self._pump)

    def _refresh_info(self):
        e = self.engine
        self.info.set(
            f"Speed(med)={int(e.speed_median)} px/s   Dir={e.last_dir or '-'}   Seq={''.join(e.seq)}"
        )
        self.root.after(200, self._refresh_info)

    # ---------- Engine callbacks ----------

    def _on_dir_hit(self, d: str):
        self._flash_edge(d)
        self.status.set(f"Hit: {d}")

    def _on_macro_match(self, m: Macro):
        self.status.set(f"Macro: {m.name or ''.join(m.pattern)} -> {m.key}")
        self._send_keys(m.key)

    # ---------- Key injection: chords & chains ----------

    def _token_to_key(self, tok: str):
        """Map token to pynput key or char."""
        if not HAVE_PYNPUT or self.kb is None:
            return None
        t = tok.lower().strip()
        special = {
            "esc":"esc","escape":"esc","space":"space","enter":"enter","return":"enter","tab":"tab",
            "up":"up","down":"down","left":"left","right":"right",
            "home":"home","end":"end","pageup":"page_up","pagedown":"page_down",
            "backspace":"backspace","delete":"delete","del":"delete",
            "ctrl":"ctrl","control":"ctrl","alt":"alt","shift":"shift",
            "cmd":"cmd","command":"cmd","win":"cmd","meta":"cmd",
        }
        fkey = re.fullmatch(r"f([1-9]|1[0-2])", t)
        if fkey:
            return getattr(keyboard.Key, f"f{fkey.group(1)}")
        if t in special:
            return getattr(keyboard.Key, special[t])
        if len(t) == 1:
            return t  # printable char
        return t

    def _parse_output(self, s: str) -> list[list]:
        """
        Parse output spec:
          - Chords with '+': "ctrl+a", "ctrl+shift+tab"
          - Multiple chords separated by ',': "ctrl+c, ctrl+v"
        Returns list of chords, each chord is list of key tokens in order.
        """
        chords = []
        for part in s.split(","):
            part = part.strip()
            if not part:
                continue
            toks = [self._token_to_key(t) for t in part.split("+")]
            toks = [t for t in toks if t is not None]
            if toks:
                chords.append(toks)
        return chords

    def _send_keys(self, spec: str):
        if not HAVE_PYNPUT or self.kb is None:
            return
        chords = self._parse_output(spec)

        def is_mod(k):
            return k in (keyboard.Key.ctrl, keyboard.Key.alt, keyboard.Key.shift, keyboard.Key.cmd)

        for chord in chords:
            mods = [k for k in chord if isinstance(k, keyboard.Key) and is_mod(k)]
            mains = [k for k in chord if k not in mods]
            for k in mods:
                try: self.kb.press(k)
                except Exception: pass
            for k in mains:
                try:
                    self.kb.press(k); self.kb.release(k)
                except Exception:
                    if isinstance(k, str) and len(k) == 1:
                        try: self.kb.press(k); self.kb.release(k)
                        except Exception: pass
            for k in reversed(mods):
                try: self.kb.release(k)
                except Exception: pass


    # ---------- Macros CRUD ----------

    def _refresh_mlist(self):
        self.mlist.delete(0, tk.END)
        for i, m in enumerate(self.store.macros):
            label = m.name or "".join(m.pattern)
            self.mlist.insert(
                tk.END,
                f"{i+1:02d}. {''.join(m.pattern):<8} -> {m.key:<20}  {label}"
            )

    def _add_macro(self):
        pat = [c for c in self.build_seq.get().upper() if c in "URDL"]
        if not pat:
            messagebox.showwarning("Invalid", "Pattern must contain U/R/D/L.")
            return
        key = self.build_key.get().strip()
        if not key:
            messagebox.showwarning("Invalid", "Output cannot be empty.")
            return
        name = self.build_name.get().strip()
        self.store.macros.append(Macro(pattern=pat, key=key, name=name))
        self.engine.set_macros(self.store.macros)
        self._refresh_mlist()
        self.build_seq.set("")
        try:
            self.store.save()
        except Exception:
            pass
        self.status.set("Macro added")

    def _del_selected_macro(self):
        sel = self.mlist.curselection()
        if not sel:
            return
        idx = sel[0]
        del self.store.macros[idx]
        self.engine.set_macros(self.store.macros)
        self._refresh_mlist()
        try:
            self.store.save()
        except Exception:
            pass
        self.status.set("Macro deleted")

    def _export_macros(self):
        path = CONF_DIR / "macros_export.json"
        data = [asdict(m) for m in self.store.macros]
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        self.status.set(f"Exported -> {path}")

    def _import_macros(self):
        path = CONF_DIR / "macros_export.json"
        if not path.exists():
            messagebox.showwarning("Missing file", f"{path} not found")
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.store.macros = [Macro(**m) for m in data]
            self.engine.set_macros(self.store.macros)
            self._refresh_mlist()
            try:
                self.store.save()
            except Exception:
                pass
            self.status.set("Imported macros")
        except Exception as ex:
            messagebox.showerror("Import failed", str(ex))

# ----------------------------- Main -----------------------------

if __name__ == "__main__":
    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except Exception:
        pass
    App(root)
    root.mainloop()
