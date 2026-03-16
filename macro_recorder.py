import os, sys, time, json, threading, queue, ctypes, subprocess, math
from pathlib import Path
from collections import Counter

try:
    from pynput.mouse import Button, Controller as MC, Listener as ML
    from pynput.keyboard import Key, Controller as KC, Listener as KL
except ImportError:
    sys.exit("pip install pynput")

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

try:
    from PIL import ImageGrab, Image, ImageTk, ImageDraw
    _PIL = True
except ImportError:
    _PIL = False

try:
    import cv2, numpy as np
    _CV2 = True
except ImportError:
    _CV2 = False

try:
    import pystray
    _TRAY = True
except ImportError:
    _TRAY = False

MACROS_FILE      = Path.home() / ".macro_recorder_macros.json"
MOVE_INTERVAL_MS = 16
MOVE_MIN_DIST_SQ = 16
SPIN_THRESHOLD_S = 0.0015
DEFAULT_SPEED    = 1.0
DEFAULT_LOOPS    = 1
PANIC_SUPPRESS_S = 2.0

T_MOVE   = sys.intern("mouse_move")
T_CLICK  = sys.intern("mouse_click")
T_SCROLL = sys.intern("mouse_scroll")
T_KEY    = sys.intern("key")
T_HOLD   = sys.intern("key_hold")
T_COND   = sys.intern("condition")

BG     = "#1a1a1f"
BG2    = "#23232b"
BG3    = "#2c2c36"
BORDER = "#3a3a48"
FG     = "#e8e8f0"
FG2    = "#9090a8"
RED    = "#e05c5c"
GREEN  = "#5ccc82"
BLUE   = "#5b9cf6"
AMBER  = "#f0a840"
TEAL   = "#40c8c0"
PURPLE = "#a882f0"
FONT   = ("Consolas", 10)
FONTSM = ("Consolas", 9)
FONTLG = ("Consolas", 12, "bold")


def _load_json(path, default):
    try:
        with open(path) as f: return json.load(f)
    except Exception: return default

def _save_json_atomic(path, data):
    tmp = Path(path).with_suffix(".tmp")
    with open(tmp, "w") as f: json.dump(data, f, separators=(",", ":"))
    tmp.replace(path)

def _screen_size():
    if sys.platform == "win32":
        return ctypes.windll.user32.GetSystemMetrics(0), ctypes.windll.user32.GetSystemMetrics(1)
    try:
        out = subprocess.check_output(["xdpyinfo"], stderr=subprocess.DEVNULL).decode()
        for ln in out.splitlines():
            if "dimensions:" in ln:
                part = ln.split()[1]
                w,h = part.split("x"); return int(w),int(h)
    except Exception: pass
    return 1920,1080

def _active_window_title():
    if sys.platform == "win32":
        buf = ctypes.create_unicode_buffer(512)
        ctypes.windll.user32.GetWindowTextW(ctypes.windll.user32.GetForegroundWindow(), buf, 512)
        return buf.value
    try:
        return subprocess.check_output(
            ["xdotool","getactivewindow","getwindowname"], stderr=subprocess.DEVNULL
        ).decode().strip()
    except Exception: return ""

def _rdp_simplify(points, epsilon=1.5):
    if len(points) < 3: return points
    def _perp(p, a, b):
        if a == b: return math.hypot(p[0]-a[0], p[1]-a[1])
        dx,dy = b[0]-a[0], b[1]-a[1]
        return abs(dy*p[0]-dx*p[1]+b[0]*a[1]-b[1]*a[0]) / math.hypot(dx,dy)
    def _rdp(pts, eps):
        if len(pts) < 3: return pts
        dmax, idx = 0, 0
        for i in range(1, len(pts)-1):
            d = _perp(pts[i], pts[0], pts[-1])
            if d > dmax: dmax,idx = d,i
        if dmax > eps:
            return _rdp(pts[:idx+1], eps)[:-1] + _rdp(pts[idx:], eps)
        return [pts[0], pts[-1]]
    return _rdp(points, epsilon)


class EvMove:
    __slots__ = ("x","y","t","type","rel")
    def __init__(self,x,y,t,rel=False): self.x=x; self.y=y; self.t=t; self.type=T_MOVE; self.rel=rel
    def to_dict(self): return {"type":T_MOVE,"x":self.x,"y":self.y,"t":self.t,"rel":self.rel}

class EvClick:
    __slots__ = ("x","y","button","pressed","t","type","rel")
    def __init__(self,x,y,b,p,t,rel=False): self.x=x; self.y=y; self.button=b; self.pressed=p; self.t=t; self.type=T_CLICK; self.rel=rel
    def to_dict(self): return {"type":T_CLICK,"x":self.x,"y":self.y,"button":self.button,"pressed":self.pressed,"t":self.t,"rel":self.rel}

class EvScroll:
    __slots__ = ("x","y","dx","dy","t","type")
    def __init__(self,x,y,dx,dy,t): self.x=x; self.y=y; self.dx=dx; self.dy=dy; self.t=t; self.type=T_SCROLL
    def to_dict(self): return {"type":T_SCROLL,"x":self.x,"y":self.y,"dx":self.dx,"dy":self.dy,"t":self.t}

class EvKey:
    __slots__ = ("key","pressed","t","type")
    def __init__(self,k,p,t): self.key=k; self.pressed=p; self.t=t; self.type=T_KEY
    def to_dict(self): return {"type":T_KEY,"key":self.key,"pressed":self.pressed,"t":self.t}

class EvHold:
    __slots__ = ("key","duration","t","type")
    def __init__(self,k,dur,t): self.key=k; self.duration=dur; self.t=t; self.type=T_HOLD
    def to_dict(self): return {"type":T_HOLD,"key":self.key,"duration":self.duration,"t":self.t}

class EvCond:
    __slots__ = ("kind","x","y","color","tolerance","t","type","template_path")
    def __init__(self,kind,x,y,color,tol,t,tp=None):
        self.kind=kind; self.x=x; self.y=y; self.color=color
        self.tolerance=tol; self.t=t; self.type=T_COND; self.template_path=tp
    def to_dict(self): return {"type":T_COND,"kind":self.kind,"x":self.x,"y":self.y,
                                "color":self.color,"tolerance":self.tolerance,"t":self.t,
                                "template_path":self.template_path}

_EV_CTORS = {
    T_MOVE:   lambda d: EvMove(d["x"],d["y"],d["t"],d.get("rel",False)),
    T_CLICK:  lambda d: EvClick(d["x"],d["y"],d["button"],d["pressed"],d["t"],d.get("rel",False)),
    T_SCROLL: lambda d: EvScroll(d["x"],d["y"],d["dx"],d["dy"],d["t"]),
    T_KEY:    lambda d: EvKey(d["key"],d["pressed"],d["t"]),
    T_HOLD:   lambda d: EvHold(d["key"],d["duration"],d["t"]),
    T_COND:   lambda d: EvCond(d["kind"],d["x"],d["y"],d.get("color"),d.get("tolerance",10),d["t"],d.get("template_path")),
}

def _ev_from_dict(d):
    c = _EV_CTORS.get(d.get("type"))
    if c is None: raise ValueError(d.get("type"))
    return c(d)


_perf = time.perf_counter

def _precision_wait(target):
    gap = target - _perf()
    if gap <= 0: return
    if gap > SPIN_THRESHOLD_S + 0.001: time.sleep(gap - SPIN_THRESHOLD_S)
    while _perf() < target: pass

_BTN_MAP   = {"Button.left":Button.left,"Button.right":Button.right,"Button.middle":Button.middle}
_btn_cache: dict = {}
_key_cache: dict = {}

def _btn(s):
    try: return _btn_cache[s]
    except KeyError:
        v=_BTN_MAP.get(s,Button.left); _btn_cache[s]=v; return v

def _key(s):
    try: return _key_cache[s]
    except KeyError:
        v=s if len(s)==1 else getattr(Key,s[4:],s) if s.startswith("Key.") else s
        _key_cache[s]=v; return v

def _pixel_color(x, y):
    if not _PIL: return None
    img = ImageGrab.grab(bbox=(x,y,x+1,y+1))
    return img.getpixel((0,0))[:3]

def _color_match(a, b, tol):
    if a is None or b is None: return True
    return all(abs(int(a[i])-int(b[i])) <= tol for i in range(3))

def _template_find(template_path):
    if not _CV2 or not _PIL: return None
    try:
        screen = np.array(ImageGrab.grab())
        screen_gray = cv2.cvtColor(screen, cv2.COLOR_RGB2GRAY)
        tmpl = cv2.imread(str(template_path), cv2.IMREAD_GRAYSCALE)
        if tmpl is None: return None
        res = cv2.matchTemplate(screen_gray, tmpl, cv2.TM_CCOEFF_NORMED)
        _, maxval, _, maxloc = cv2.minMaxLoc(res)
        if maxval < 0.8: return None
        th, tw = tmpl.shape
        return (maxloc[0] + tw//2, maxloc[1] + th//2)
    except Exception: return None


class MacroRecorder:
    __slots__ = (
        "_lock","_recording","_playing","_start_time",
        "_buf","events","macros","tags","chains",
        "_stop_play","_panic","_suppress_until",
        "_play_thread","_mc","_kc","_hotkeys",
        "_last_move_t","_lx","_ly","ui_q",
        "_rel_mode","_anchor_x","_anchor_y",
        "_window_guard","_dry_run",
        "_key_down_times",
    )

    def __init__(self):
        self._lock          = threading.Lock()
        self._recording     = False
        self._playing       = False
        self._start_time    = 0.0
        self._buf           = []
        self.events         = []
        self.macros         = _load_json(MACROS_FILE, {})
        self.tags           = {}
        self.chains         = {}
        self._stop_play     = threading.Event()
        self._panic         = threading.Event()
        self._suppress_until= 0.0
        self._play_thread   = None
        self._mc            = MC()
        self._kc            = KC()
        self._hotkeys       = frozenset({Key.f9,Key.f10,Key.f11,Key.f12})
        self._last_move_t   = 0.0
        self._lx = self._ly = -999
        self.ui_q           = queue.SimpleQueue()
        self._rel_mode      = False
        self._anchor_x      = 0
        self._anchor_y      = 0
        self._window_guard  = None
        self._dry_run       = False
        self._key_down_times= {}

    def _ui(self, msg): self.ui_q.put_nowait(msg)

    def panic(self):
        self._stop_play.set()
        self._panic.set()
        self._suppress_until = _perf() + PANIC_SUPPRESS_S
        self._playing = False
        self._ui(("state","idle"))
        self._ui(("toast","⚠ PANIC — playback killed, inputs suppressed 2s"))
        threading.Timer(PANIC_SUPPRESS_S, self._panic.clear).start()

    def set_anchor(self, x, y):
        self._anchor_x = x; self._anchor_y = y

    def start_recording(self):
        with self._lock:
            if self._recording: return
            self._buf.clear()
            self._last_move_t = 0.0
            self._lx = self._ly = -999
            self._start_time  = _perf()
            self._recording   = True
            self._key_down_times.clear()
        self._ui(("state","recording"))

    def stop_recording(self):
        with self._lock:
            if not self._recording: return
            self._recording = False
            self.events, self._buf = self._buf, []
        self._ui(("state","idle"))
        self._ui(("recorded", len(self.events),
                  self.events[-1].t if self.events else 0, self._counts()))

    def on_move(self, x, y):
        if not self._recording: return
        dx=x-self._lx; dy=y-self._ly
        if dx*dx+dy*dy < MOVE_MIN_DIST_SQ: return
        rx,ry = (x-self._anchor_x, y-self._anchor_y) if self._rel_mode else (x,y)
        now = (_perf()-self._start_time)*1000.0
        buf = self._buf
        if now-self._last_move_t < MOVE_INTERVAL_MS and buf and buf[-1].type is T_MOVE:
            ev=buf[-1]; ev.x=rx; ev.y=ry; ev.t=now
        else:
            buf.append(EvMove(rx,ry,now,self._rel_mode)); self._last_move_t=now
        self._lx=x; self._ly=y

    def on_click(self, x, y, button, pressed):
        if not self._recording: return
        rx,ry = (x-self._anchor_x, y-self._anchor_y) if self._rel_mode else (x,y)
        ev = EvClick(rx,ry,str(button),pressed,(_perf()-self._start_time)*1000.0,self._rel_mode)
        self._buf.append(ev)
        self._ui(("event",ev))

    def on_scroll(self, x, y, dx, dy):
        if not self._recording: return
        ev = EvScroll(x,y,dx,dy,(_perf()-self._start_time)*1000.0)
        self._buf.append(ev); self._ui(("event",ev))

    def on_key(self, key, pressed):
        if not self._recording or key in self._hotkeys: return
        try: ks = key.char if key.char else str(key)
        except AttributeError: ks = str(key)
        now = (_perf()-self._start_time)*1000.0
        if pressed:
            self._key_down_times[ks] = now
            ev = EvKey(ks,True,now)
            self._buf.append(ev); self._ui(("event",ev))
        else:
            down_t = self._key_down_times.pop(ks, None)
            if down_t is not None:
                dur = now - down_t
                if dur > 150:
                    for i in range(len(self._buf)-1,-1,-1):
                        e=self._buf[i]
                        if e.type is T_KEY and e.key==ks and e.pressed:
                            self._buf[i]=EvHold(ks,round(dur,1),down_t)
                            self._ui(("event_replace",i,self._buf[i]))
                            break
                    return
            ev = EvKey(ks,False,now)
            self._buf.append(ev); self._ui(("event",ev))

    def start_playback(self, events=None, speed=DEFAULT_SPEED,
                       loops=DEFAULT_LOOPS, loop_delay=0.0, dry_run=False):
        if self._playing: return
        src = events if events is not None else self.events
        if not src: self._ui(("toast","Nothing to play.")); return
        self._stop_play.clear()
        self._panic.clear()
        self._playing = True
        self._dry_run = dry_run
        self._ui(("state","playing"))
        if dry_run: self._ui(("dryrun_start", src))
        self._play_thread = threading.Thread(
            target=self._play_worker, args=(src,speed,loops,loop_delay,dry_run), daemon=True)
        self._play_thread.start()

    def stop_playback(self):
        if not self._playing: return
        self._stop_play.set()
        self._playing = False
        self._ui(("state","idle"))

    def _play_worker(self, events, speed, loops, loop_delay, dry_run):
        inv      = 1.0/speed
        stopped  = self._stop_play.is_set
        panicked = self._panic.is_set
        fire     = self._fire_dry if dry_run else self._fire
        pw       = _precision_wait
        perf     = _perf
        slp      = time.sleep
        total    = len(events)*loops
        done     = 0; last_pct = -1
        guard    = self._window_guard
        try:
            for li in range(loops):
                if stopped(): break
                if guard:
                    title = _active_window_title()
                    if guard.lower() not in title.lower():
                        self._ui(("toast",f"Window '{guard}' not focused — skipping loop {li+1}"))
                        continue
                origin = perf()
                for ev in events:
                    if stopped(): break
                    if ev.type is T_COND:
                        if not self._check_condition(ev):
                            self._ui(("toast","Condition failed — playback paused"))
                            self._ui(("state","idle"))
                            self._playing = False
                            return
                        continue
                    pw(origin + ev.t*0.001*inv)
                    if not panicked(): fire(ev)
                    done += 1
                    pct = int(done*200/total)
                    if pct != last_pct:
                        self._ui(("progress",done/total)); last_pct=pct
                if loop_delay>0 and li<loops-1 and not stopped(): slp(loop_delay)
        finally:
            self._playing = False
            self._ui(("state","idle"))
            self._ui(("progress",0.0))
            if dry_run: self._ui(("dryrun_end",))
            if not stopped() and not panicked():
                self._ui(("toast","Playback complete."))

    def _check_condition(self, ev):
        if ev.kind == "pixel":
            got = _pixel_color(ev.x, ev.y)
            target = tuple(ev.color) if ev.color else None
            return _color_match(got, target, ev.tolerance)
        elif ev.kind == "template":
            return _template_find(ev.template_path) is not None
        return True

    def _fire(self, ev): self._dispatch[ev.type](self, ev)
    def _fire_dry(self, ev): self._ui(("dryrun_event", ev))

    _dispatch: dict = {}

    def _fire_move(self, ev):
        if ev.rel: self._mc.position=(ev.x+self._anchor_x, ev.y+self._anchor_y)
        else:      self._mc.position=(ev.x,ev.y)
    def _fire_scroll(self, ev): self._mc.scroll(ev.dx,ev.dy)
    def _fire_click(self, ev):
        x = ev.x+self._anchor_x if ev.rel else ev.x
        y = ev.y+self._anchor_y if ev.rel else ev.y
        self._mc.position=(x,y)
        b=_btn(ev.button)
        (self._mc.press if ev.pressed else self._mc.release)(b)
    def _fire_key(self, ev):
        k=_key(ev.key)
        (self._kc.press if ev.pressed else self._kc.release)(k)
    def _fire_hold(self, ev):
        k=_key(ev.key)
        self._kc.press(k); time.sleep(ev.duration*0.001); self._kc.release(k)
    def _fire_cond(self, ev): pass

    def compress_keys(self):
        out=[]; i=0; evs=self.events
        while i < len(evs):
            ev=evs[i]
            if ev.type is T_KEY and ev.pressed:
                j=i+1
                while j<len(evs) and not(evs[j].type is T_KEY and evs[j].key==ev.key and not evs[j].pressed): j+=1
                if j<len(evs):
                    dur = evs[j].t - ev.t
                    if dur > 150:
                        out.append(EvHold(ev.key,round(dur,1),ev.t)); i=j+1; continue
            out.append(ev); i+=1
        removed=len(self.events)-len(out); self.events=out; return removed

    def simplify_moves(self, epsilon=1.5):
        segments=[]; seg=[]; out=[]
        for ev in self.events:
            if ev.type is T_MOVE: seg.append(ev)
            else:
                if seg: segments.append(seg); seg=[]
                segments.append([ev])
        if seg: segments.append(seg)
        removed=0
        for seg in segments:
            if len(seg)==1 and seg[0].type is not T_MOVE: out.append(seg[0]); continue
            pts=[(e.x,e.y) for e in seg]
            kept_pts=set(map(tuple,_rdp_simplify(pts,epsilon)))
            for e in seg:
                if (e.x,e.y) in kept_pts: out.append(e)
                else: removed+=1
        self.events=out; return removed

    def trim(self, start_ms=0.0, end_ms=None):
        if not self.events: return False,"Nothing to trim."
        end=end_ms if end_ms is not None else self.events[-1].t
        kept=[e for e in self.events if start_ms<=e.t<=end]
        if not kept: return False,"No events in range."
        off=kept[0].t
        for e in kept: e.t=round(e.t-off,1)
        self.events=kept
        return True,f"Trimmed → {len(kept)} events ({kept[-1].t:.0f}ms)"

    def dedupe_moves(self, min_px=2.0):
        sq=min_px*min_px; out=[]; prev=None
        for ev in self.events:
            if ev.type is not T_MOVE: out.append(ev); prev=None; continue
            if prev is None: out.append(ev); prev=ev; continue
            dx=ev.x-prev.x; dy=ev.y-prev.y
            if dx*dx+dy*dy>=sq: out.append(ev); prev=ev
        n=len(self.events)-len(out); self.events=out; return n

    def speed_scale(self, f):
        for e in self.events: e.t=round(e.t*f,1)

    def move_event(self, idx, dt):
        if not (0<=idx<len(self.events)): return
        self.events[idx].t = round(self.events[idx].t+dt, 1)
        self.events.sort(key=lambda e: e.t)

    def set_event_pos(self, idx, x, y):
        if not (0<=idx<len(self.events)): return
        ev=self.events[idx]
        if hasattr(ev,'x'): ev.x=x
        if hasattr(ev,'y'): ev.y=y

    def add_condition(self, kind, x, y, color=None, tol=10, after_idx=0, tmpl=None):
        t = self.events[after_idx].t+0.1 if self.events else 0
        ev=EvCond(kind,x,y,color,tol,t,tmpl)
        self.events.insert(after_idx+1,ev)

    def save_current(self, name, tag=""):
        if not self.events: return False,"Nothing to save."
        self.macros[name]={
            "events":[e.to_dict() for e in self.events],
            "saved_at":time.strftime("%Y-%m-%d %H:%M:%S"),
            "event_count":len(self.events),
            "duration_ms":self.events[-1].t,
            "tag":tag,
        }
        _save_json_atomic(MACROS_FILE,self.macros)
        return True,name

    def rename_macro(self, old, new):
        if old not in self.macros: return False,f"'{old}' not found."
        if new in self.macros:     return False,f"'{new}' already exists."
        self.macros[new]=self.macros.pop(old)
        _save_json_atomic(MACROS_FILE,self.macros)
        return True,""

    def delete_macro(self, name):
        if name not in self.macros: return
        del self.macros[name]; _save_json_atomic(MACROS_FILE,self.macros)

    def load_macro(self, name):
        if name not in self.macros: return None
        return [_ev_from_dict(d) for d in self.macros[name]["events"]]

    def export_json(self, path):
        _save_json_atomic(path,[e.to_dict() for e in self.events])

    def import_json(self, path):
        raw=_load_json(Path(path),None)
        if raw is None: return False
        self.events=[_ev_from_dict(d) for d in raw]; return True

    def _counts(self):
        c=Counter()
        for e in self.events:
            t=e.type
            if (t is T_CLICK or t is T_KEY) and e.pressed: c[t]+=1
            elif t is T_SCROLL or t is T_HOLD or t is T_MOVE: c[t]+=1
        return c

    def density(self, buckets=60):
        if not self.events: return [0]*buckets
        dur=max(e.t for e in self.events); step=dur/buckets if dur>0 else 1
        out=[0]*buckets
        for e in self.events:
            if e.type is T_MOVE: continue
            i=min(int(e.t/step),buckets-1); out[i]+=1
        return out


MacroRecorder._dispatch = {
    T_MOVE:   MacroRecorder._fire_move,
    T_CLICK:  MacroRecorder._fire_click,
    T_SCROLL: MacroRecorder._fire_scroll,
    T_KEY:    MacroRecorder._fire_key,
    T_HOLD:   MacroRecorder._fire_hold,
    T_COND:   MacroRecorder._fire_cond,
}


class Controller:
    __slots__ = ("rec","_ml","_kl","_hk","_panic_armed")

    def __init__(self, rec):
        self.rec=rec; self._ml=self._kl=None; self._panic_armed=False
        self._hk={
            Key.f9:rec.start_recording, Key.f10:rec.stop_recording,
            Key.f11:rec.start_playback, Key.f12:rec.stop_playback,
        }

    def start(self):
        r=self.rec
        self._ml=ML(on_move=r.on_move,on_click=r.on_click,on_scroll=r.on_scroll)
        self._kl=KL(on_press=self._press,on_release=self._release)
        self._ml.start(); self._kl.start()

    def stop(self):
        for l in (self._ml,self._kl):
            try: l and l.stop()
            except Exception: pass

    def _press(self, key):
        if key == Key.shift: self._panic_armed=True; return
        if self._panic_armed and key == Key.esc: self.rec.panic(); return
        a=self._hk.get(key)
        if a: a()
        else: self.rec.on_key(key,True)

    def _release(self, key):
        if key == Key.shift: self._panic_armed=False; return
        self.rec.on_key(key,False)


class DryRunOverlay(tk.Toplevel):
    def __init__(self, parent, sw, sh):
        super().__init__(parent)
        self.overrideredirect(True)
        self.attributes("-topmost",True)
        self.attributes("-alpha",0.35)
        self.geometry(f"{sw}x{sh}+0+0")
        self.configure(bg="black")
        self.canvas=tk.Canvas(self,bg="black",highlightthickness=0,
                              width=sw,height=sh)
        self.canvas.pack(fill="both",expand=True)
        self._rings=[]

    def flash(self, ev):
        if not hasattr(ev,"x"): return
        x,y=ev.x,ev.y
        col = BLUE if ev.type is T_CLICK else GREEN if ev.type is T_KEY else TEAL
        r=self.canvas.create_oval(x-14,y-14,x+14,y+14,outline=col,width=2)
        d=self.canvas.create_oval(x-4,y-4,x+4,y+4,fill=col,outline="")
        self._rings.append((r,d,_perf()+0.5))
        self.after(520,self._reap)

    def _reap(self):
        now=_perf(); keep=[]
        for r,d,exp in self._rings:
            if now>=exp: self.canvas.delete(r); self.canvas.delete(d)
            else: keep.append((r,d,exp))
        self._rings=keep


class TimelineCanvas(tk.Canvas):
    H = 56
    def __init__(self, parent, app, **kw):
        super().__init__(parent,bg=BG2,height=self.H,highlightthickness=0,**kw)
        self._app=app; self._sel=None; self._drag_idx=None; self._drag_ox=0
        self.bind("<Button-1>",self._click)
        self.bind("<B1-Motion>",self._drag)
        self.bind("<ButtonRelease-1>",self._drop)
        self.bind("<Button-3>",self._rclick)

    def redraw(self, events):
        self.delete("all")
        if not events: return
        w=self.winfo_width() or 600
        dur=max(e.t for e in events) or 1
        self._dur=dur; self._w=w; self._events=events
        for i,ev in enumerate(events):
            x=int(ev.t/dur*(w-4))+2
            col=BLUE if ev.type is T_CLICK else GREEN if ev.type is T_KEY else \
                TEAL if ev.type is T_SCROLL else PURPLE if ev.type is T_HOLD else \
                AMBER if ev.type is T_COND else FG2
            h=self.H-8 if ev.type is not T_MOVE else 8
            y0=self.H//2-h//2; y1=y0+h
            tag=f"ev{i}"
            self.create_line(x,y0,x,y1,fill=col,width=2 if ev.type is not T_MOVE else 1,tags=tag)
            if i==self._sel:
                self.create_rectangle(x-4,2,x+4,self.H-2,outline=AMBER,width=1)

    def _x_to_idx(self, x):
        evs=getattr(self,"_events",None)
        if not evs: return None
        w=getattr(self,"_w",600); dur=getattr(self,"_dur",1)
        t=x/w*dur
        best=None; bd=1e9
        for i,e in enumerate(evs):
            d=abs(e.t-t)
            if d<bd: bd=d; best=i
        return best if bd < dur*0.02 else None

    def _click(self, e):
        idx=self._x_to_idx(e.x)
        self._sel=idx; self._drag_idx=idx; self._drag_ox=e.x
        self._app._timeline_select(idx)
        self.redraw(getattr(self,"_events",[]))

    def _drag(self, e):
        if self._drag_idx is None: return
        evs=getattr(self,"_events",[]); w=getattr(self,"_w",600); dur=getattr(self,"_dur",1)
        dx=e.x-self._drag_ox; dt=dx/w*dur
        self._app.rec.move_event(self._drag_idx, dt)
        self._drag_ox=e.x
        self.redraw(self._app.rec.events)

    def _drop(self, _): self._drag_idx=None

    def _rclick(self, e):
        idx=self._x_to_idx(e.x)
        if idx is None: return
        self._sel=idx
        self.redraw(getattr(self,"_events",[]))
        self._app._timeline_ctx(idx, e.x_root, e.y_root)


class DensityGraph(tk.Canvas):
    H=36
    def __init__(self, parent, **kw):
        super().__init__(parent,bg=BG2,height=self.H,highlightthickness=0,**kw)

    def redraw(self, buckets):
        self.delete("all")
        if not buckets or max(buckets)==0: return
        w=self.winfo_width() or 600
        mx=max(buckets); bw=w/len(buckets)
        for i,v in enumerate(buckets):
            h=int(v/mx*(self.H-4))
            x0=i*bw; x1=x0+bw-1; y0=self.H-2-h; y1=self.H-2
            self.create_rectangle(x0,y0,x1,y1,fill=BLUE,outline="")


class ChainEditor(tk.Toplevel):
    def __init__(self, parent, rec):
        super().__init__(parent)
        self.title("Macro Chain Editor"); self.configure(bg=BG)
        self.minsize(420,320); self._rec=rec
        self.columnconfigure(0,weight=1); self.rowconfigure(1,weight=1)
        tk.Label(self,text="Macro chain — drag to reorder, double-click to remove",
                 font=FONTSM,bg=BG,fg=FG2).grid(row=0,column=0,columnspan=2,padx=10,pady=(8,2),sticky="w")
        self._lb=tk.Listbox(self,bg=BG2,fg=FG,font=FONT,selectbackground=BG3,
                            relief="flat",borderwidth=0,activestyle="none")
        self._lb.grid(row=1,column=0,sticky="nsew",padx=(10,0),pady=4)
        sb=ttk.Scrollbar(self,orient="vertical",command=self._lb.yview)
        sb.grid(row=1,column=1,sticky="ns",padx=(0,10),pady=4)
        self._lb.configure(yscrollcommand=sb.set)
        self._lb.bind("<Double-1>",self._remove)
        bf=ttk.Frame(self); bf.grid(row=2,column=0,columnspan=2,sticky="ew",padx=10,pady=(0,8))
        bf.columnconfigure(0,weight=1)
        ttk.Button(bf,text="+ Add macro",command=self._add).grid(row=0,column=0,sticky="ew",padx=(0,4))
        self._delay_var=tk.DoubleVar(value=0.5)
        tk.Label(bf,text="delay (s)",font=FONTSM,bg=BG,fg=FG2).grid(row=0,column=1,padx=(4,2))
        ttk.Entry(bf,textvariable=self._delay_var,width=6,font=FONT).grid(row=0,column=2,padx=(0,4))
        ttk.Button(bf,text="▶ Run chain",command=self._run).grid(row=0,column=3)
        self._chain=[]
        self._refresh()

    def _refresh(self):
        self._lb.delete(0,"end")
        for name in self._chain: self._lb.insert("end",f"  {name}")

    def _add(self):
        names=list(self._rec.macros.keys())
        if not names: return
        d=_PickDialog(self,"Add macro","Select macro:",names)
        if d.result: self._chain.append(d.result); self._refresh()

    def _remove(self, _):
        i=self._lb.curselection()
        if i: del self._chain[i[0]]; self._refresh()

    def _run(self):
        if not self._chain: return
        delay=self._delay_var.get()
        def _worker():
            for name in self._chain:
                evs=self._rec.load_macro(name)
                if evs:
                    self._rec.start_playback(events=evs)
                    while self._rec._playing: time.sleep(0.05)
                    if delay>0: time.sleep(delay)
        threading.Thread(target=_worker,daemon=True).start()


class _PickDialog(tk.Toplevel):
    def __init__(self, parent, title, label, options):
        super().__init__(parent); self.title(title); self.configure(bg=BG)
        self.resizable(False,False); self.grab_set(); self.result=None
        tk.Label(self,text=label,font=FONT,bg=BG,fg=FG).pack(padx=14,pady=(10,4))
        self._var=tk.StringVar(value=options[0] if options else "")
        om=ttk.OptionMenu(self,self._var,options[0] if options else "",*options)
        om.pack(padx=14,pady=4,fill="x")
        fr=ttk.Frame(self); fr.pack(pady=8)
        ttk.Button(fr,text="OK",    command=self._ok    ).pack(side="left",padx=6)
        ttk.Button(fr,text="Cancel",command=self.destroy).pack(side="right",padx=6)
        self.bind("<Return>",lambda _:self._ok())
        self.bind("<Escape>",lambda _:self.destroy())
        self.wait_window()
    def _ok(self): self.result=self._var.get(); self.destroy()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Macro Recorder")
        self.configure(bg=BG)
        self.resizable(True,True)
        self.minsize(820,600)
        self.rec   = MacroRecorder()
        self.ctrl  = Controller(self.rec)
        self.ctrl.start()
        self._state      = "idle"
        self._pulse_on   = False
        self._overlay    = None
        self._sel_ev_idx = None
        self._sw, self._sh = _screen_size()
        self._build_styles()
        self._build_ui()
        self._build_poll_dispatch()
        self._poll()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        if _TRAY: self._start_tray()

    def _build_styles(self):
        s=ttk.Style(self); s.theme_use("clam")
        s.configure(".",background=BG,foreground=FG,font=FONT)
        s.configure("TFrame",background=BG)
        s.configure("Card.TFrame",background=BG2)
        s.configure("TLabel",background=BG,foreground=FG,font=FONT)
        s.configure("TButton",background=BG3,foreground=FG,font=FONT,
                    borderwidth=1,relief="flat",padding=(10,5))
        s.map("TButton",background=[("active",BORDER),("pressed",BG)],foreground=[("active",FG)])
        s.configure("Rec.TButton",background="#3d1e1e",foreground=RED)
        s.map("Rec.TButton",background=[("active","#5a2424")])
        s.configure("Play.TButton",background="#1e3d2a",foreground=GREEN)
        s.map("Play.TButton",background=[("active","#254d33")])
        s.configure("Stop.TButton",background="#3d2e1e",foreground=AMBER)
        s.map("Stop.TButton",background=[("active","#5a421e")])
        s.configure("Dry.TButton",background="#1e2a3d",foreground=PURPLE)
        s.map("Dry.TButton",background=[("active","#253352")])
        s.configure("TScale",background=BG2,troughcolor=BG3,sliderlength=14,sliderrelief="flat")
        s.configure("TProgressbar",troughcolor=BG3,background=GREEN,borderwidth=0,thickness=4)
        s.configure("Macro.Treeview",background=BG2,foreground=FG,
                    fieldbackground=BG2,borderwidth=0,font=FONT,rowheight=22)
        s.configure("Macro.Treeview.Heading",background=BG3,foreground=FG2,
                    font=FONTSM,borderwidth=0,relief="flat")
        s.map("Macro.Treeview",background=[("selected",BG3)],foreground=[("selected",FG)])
        s.configure("TNotebook",background=BG,borderwidth=0,tabmargins=0)
        s.configure("TNotebook.Tab",background=BG3,foreground=FG2,font=FONTSM,
                    padding=(12,5),borderwidth=0)
        s.map("TNotebook.Tab",background=[("selected",BG2)],foreground=[("selected",FG)])
        s.configure("TEntry",fieldbackground=BG3,foreground=FG,insertcolor=FG,
                    borderwidth=0,relief="flat")
        s.configure("TCheckbutton",background=BG2,foreground=FG,font=FONTSM)
        s.map("TCheckbutton",background=[("active",BG2)])

    def _build_ui(self):
        top=ttk.Frame(self,style="Card.TFrame")
        top.pack(fill="x")
        top.columnconfigure(1,weight=1)
        self._dot=tk.Label(top,text="●",font=("Consolas",14),bg=BG2,fg=FG2)
        self._dot.grid(row=0,column=0,padx=(14,6),pady=10)
        self._status_lbl=tk.Label(top,text="idle",font=FONTLG,bg=BG2,fg=FG2)
        self._status_lbl.grid(row=0,column=1,sticky="w")
        hint=tk.Label(top,text="F9 rec  F10 stop  F11 play  F12 stop  Shift+Esc PANIC",
                      font=FONTSM,bg=BG2,fg=FG2)
        hint.grid(row=0,column=2,padx=14)
        self._pbar=ttk.Progressbar(self,style="TProgressbar",maximum=100,value=0)
        self._pbar.pack(fill="x")

        nb=ttk.Notebook(self); nb.pack(fill="both",expand=True)
        self._nb=nb

        pg_main=ttk.Frame(nb); nb.add(pg_main,text="  Recorder  ")
        pg_timeline=ttk.Frame(nb); nb.add(pg_timeline,text="  Timeline  ")
        pg_macros=ttk.Frame(nb); nb.add(pg_macros,text="  Macros  ")
        pg_settings=ttk.Frame(nb); nb.add(pg_settings,text="  Settings  ")

        self._build_recorder(pg_main)
        self._build_timeline(pg_timeline)
        self._build_macros_tab(pg_macros)
        self._build_settings(pg_settings)

    def _build_recorder(self, p):
        p.columnconfigure(0,weight=3); p.columnconfigure(1,weight=2)
        p.rowconfigure(0,weight=1)

        left=ttk.Frame(p); left.grid(row=0,column=0,sticky="nsew",padx=(8,4),pady=8)
        right=ttk.Frame(p); right.grid(row=0,column=1,sticky="nsew",padx=(4,8),pady=8)

        left.columnconfigure(0,weight=1); left.rowconfigure(4,weight=1)

        br=ttk.Frame(left); br.grid(row=0,column=0,sticky="ew",pady=(0,6))
        for i in range(5): br.columnconfigure(i,weight=1)
        self._btn_rec =ttk.Button(br,text="⏺  Record",style="Rec.TButton",command=self._toggle_record)
        self._btn_play=ttk.Button(br,text="▶  Play",  style="Play.TButton",command=self._do_play)
        self._btn_dry =ttk.Button(br,text="◎  Dry Run",style="Dry.TButton",command=self._do_dry_run)
        self._btn_stop=ttk.Button(br,text="⏹  Stop",  style="Stop.TButton",command=self._do_stop)
        self._btn_save=ttk.Button(br,text="💾  Save",                       command=self._do_save)
        for i,b in enumerate((self._btn_rec,self._btn_play,self._btn_dry,self._btn_stop,self._btn_save)):
            b.grid(row=0,column=i,sticky="ew",padx=(0 if i==0 else 3, 0 if i==4 else 3))

        cfg=ttk.Frame(left,style="Card.TFrame"); cfg.grid(row=1,column=0,sticky="ew",pady=4)
        for i in range(6): cfg.columnconfigure(i,weight=1 if i%2==1 else 0)

        def _sl(par,label,var,lo,hi,fmt,col,row=0):
            tk.Label(par,text=label,font=FONTSM,bg=BG2,fg=FG2).grid(row=row,column=col,padx=(10,2),pady=5,sticky="e")
            vl=tk.Label(par,font=FONTSM,bg=BG2,fg=FG,width=6,anchor="w"); vl.grid(row=row,column=col+2,pady=5)
            def _cb(_,v=vl,f=fmt,sv=var): v.config(text=f(sv.get()))
            ttk.Scale(par,from_=lo,to=hi,variable=var,orient="horizontal",command=_cb
                      ).grid(row=row,column=col+1,sticky="ew",padx=4,pady=5)
            _cb(None); return vl

        self._speed_var=tk.DoubleVar(value=1.0)
        self._loops_var=tk.IntVar(value=1)
        self._delay_var=tk.DoubleVar(value=0.0)
        _sl(cfg,"speed",self._speed_var,0.1,5.0,lambda v:f"{v:.2f}×",0,0)
        _sl(cfg,"loops",self._loops_var,1,20,lambda v:str(int(v)),3,0)
        _sl(cfg,"delay (s)",self._delay_var,0.0,10.0,lambda v:f"{v:.1f}s",0,1)

        sf=ttk.Frame(left,style="Card.TFrame"); sf.grid(row=2,column=0,sticky="ew",pady=(0,4))
        self._stat_labels={}
        for i,k in enumerate(("events","clicks","keys","duration")):
            sf.columnconfigure(i,weight=1)
            f=ttk.Frame(sf,style="Card.TFrame"); f.grid(row=0,column=i,padx=10,pady=6,sticky="ew")
            tk.Label(f,text=k,font=FONTSM,bg=BG2,fg=FG2).pack(anchor="w")
            lbl=tk.Label(f,text="—",font=("Consolas",13,"bold"),bg=BG2,fg=FG); lbl.pack(anchor="w")
            self._stat_labels[k]=lbl

        tk.Label(left,text="Density",font=FONTSM,bg=BG,fg=FG2).grid(row=3,column=0,sticky="w",pady=(4,0))
        self._density=DensityGraph(left); self._density.grid(row=3,column=0,sticky="ew",pady=(18,2))

        lhdr=ttk.Frame(left); lhdr.grid(row=4,column=0,sticky="ew",pady=(4,0))
        tk.Label(lhdr,text="Event log",font=FONTSM,bg=BG,fg=FG2).pack(side="left")
        ttk.Button(lhdr,text="clear",command=self._clear_log).pack(side="right")

        lf=ttk.Frame(left,style="Card.TFrame"); lf.grid(row=5,column=0,sticky="nsew",pady=(2,0))
        left.rowconfigure(5,weight=1)
        self._log=tk.Text(lf,bg=BG2,fg=FG,font=FONTSM,state="disabled",
                          relief="flat",borderwidth=0,selectbackground=BG3,wrap="none",insertbackground=FG)
        lsb=ttk.Scrollbar(lf,orient="vertical",command=self._log.yview)
        self._log.configure(yscrollcommand=lsb.set)
        self._log.pack(side="left",fill="both",expand=True,padx=4,pady=4)
        lsb.pack(side="right",fill="y")
        for tag,col in (("click",BLUE),("key",GREEN),("scroll",TEAL),("hold",PURPLE),("cond",AMBER),("ts",FG2)):
            self._log.tag_configure(tag,foreground=col)

        er=ttk.Frame(left); er.grid(row=6,column=0,sticky="ew",pady=(4,0))
        for i in range(5): er.columnconfigure(i,weight=1)
        tools=[("✂ Trim",self._do_trim),("⊘ Dedupe",self._do_dedupe),
               ("⟳ Scale",self._do_scale),("⇱ Compress",self._do_compress),
               ("↑ Export",self._do_export)]
        for i,(txt,cmd) in enumerate(tools):
            ttk.Button(er,text=txt,command=cmd).grid(row=0,column=i,sticky="ew",
                padx=(0 if i==0 else 3, 0 if i==4 else 3))

        right.columnconfigure(0,weight=1); right.rowconfigure(0,weight=1)
        rf=ttk.Frame(right,style="Card.TFrame"); rf.grid(row=0,column=0,sticky="nsew")
        rf.columnconfigure(0,weight=1); rf.rowconfigure(0,weight=1)
        self._tree=ttk.Treeview(rf,style="Macro.Treeview",
                                columns=("events","duration","tag","saved"),
                                show="headings",selectmode="browse")
        for col,w,anc in (("events",52,"e"),("duration",70,"e"),("tag",70,"w"),("saved",120,"w")):
            self._tree.heading(col,text=col,anchor=anc)
            self._tree.column(col,width=w,anchor=anc,stretch=(col=="saved"))
        tsb=ttk.Scrollbar(rf,orient="vertical",command=self._tree.yview)
        self._tree.configure(yscrollcommand=tsb.set)
        self._tree.pack(side="left",fill="both",expand=True,padx=2,pady=2)
        tsb.pack(side="right",fill="y")
        self._tree.bind("<Double-1>",lambda _:self._ctx_load())
        self._tree.bind("<Button-3>",self._tree_ctx)

        self._ctx=tk.Menu(self,tearoff=0,bg=BG2,fg=FG,activebackground=BG3,activeforeground=FG,font=FONT,borderwidth=0)
        for lbl,cmd in (("Load",self._ctx_load),("Play",self._ctx_play),
                         ("Dry Run",self._ctx_dry),("Rename",self._ctx_rename),
                         ("Tag",self._ctx_tag),(None,None),("Delete",self._ctx_delete)):
            if lbl is None: self._ctx.add_separator()
            else: self._ctx.add_command(label=lbl,command=cmd)

        sf2=ttk.Frame(right); sf2.grid(row=1,column=0,sticky="ew",pady=(6,0))
        sf2.columnconfigure(0,weight=1); sf2.columnconfigure(1,weight=1); sf2.columnconfigure(2,weight=1)
        ttk.Button(sf2,text="Load",  command=self._ctx_load  ).grid(row=0,column=0,sticky="ew",padx=(0,3))
        ttk.Button(sf2,text="Chain", command=self._open_chain ).grid(row=0,column=1,sticky="ew",padx=3)
        ttk.Button(sf2,text="Delete",command=self._ctx_delete ).grid(row=0,column=2,sticky="ew",padx=(3,0))

        sf3=ttk.Frame(right); sf3.grid(row=2,column=0,sticky="ew",pady=(4,0))
        tk.Label(sf3,text="search",font=FONTSM,bg=BG,fg=FG2).pack(side="left")
        self._search_var=tk.StringVar()
        self._search_var.trace_add("write",lambda *_:self._filter_tree())
        ttk.Entry(sf3,textvariable=self._search_var,font=FONTSM,width=16).pack(side="left",padx=(4,8))
        ttk.Button(sf3,text="↓ Import",command=self._do_import).pack(side="right")

        self._toast=tk.Label(right,text="",font=FONTSM,bg=BG,fg=AMBER)
        self._toast.grid(row=3,column=0,pady=(4,0))

        self._refresh_tree()

    def _build_timeline(self, p):
        p.columnconfigure(0,weight=1); p.rowconfigure(1,weight=1)
        hdr=ttk.Frame(p); hdr.grid(row=0,column=0,sticky="ew",padx=10,pady=(8,4))
        tk.Label(hdr,text="Timeline — drag events, right-click to edit or delete",
                 font=FONTSM,bg=BG,fg=FG2).pack(side="left")
        ttk.Button(hdr,text="Refresh",command=self._refresh_timeline).pack(side="right")
        ttk.Button(hdr,text="Simplify moves",command=self._do_simplify).pack(side="right",padx=4)
        ttk.Button(hdr,text="+ Condition",command=self._do_add_condition).pack(side="right",padx=4)

        self._timeline=TimelineCanvas(p,self); self._timeline.grid(row=1,column=0,sticky="ew",padx=10)

        inf=ttk.Frame(p,style="Card.TFrame"); inf.grid(row=2,column=0,sticky="ew",padx=10,pady=6)
        inf.columnconfigure(1,weight=1)
        tk.Label(inf,text="selected:",font=FONTSM,bg=BG2,fg=FG2).grid(row=0,column=0,padx=8,pady=6)
        self._sel_info=tk.Label(inf,text="—",font=FONTSM,bg=BG2,fg=FG)
        self._sel_info.grid(row=0,column=1,sticky="w")
        ttk.Button(inf,text="Delete event",command=self._del_sel_event).grid(row=0,column=2,padx=8)
        ttk.Button(inf,text="Edit pos",    command=self._edit_sel_pos  ).grid(row=0,column=3,padx=(0,8))

        lf=ttk.Frame(p,style="Card.TFrame"); lf.grid(row=3,column=0,sticky="nsew",padx=10,pady=(0,10))
        p.rowconfigure(3,weight=1)
        self._evlist=tk.Text(lf,bg=BG2,fg=FG,font=FONTSM,state="disabled",
                             relief="flat",borderwidth=0,selectbackground=BG3,wrap="none")
        esb=ttk.Scrollbar(lf,orient="vertical",command=self._evlist.yview)
        self._evlist.configure(yscrollcommand=esb.set)
        self._evlist.pack(side="left",fill="both",expand=True,padx=4,pady=4)
        esb.pack(side="right",fill="y")
        for tag,col in (("click",BLUE),("key",GREEN),("scroll",TEAL),("hold",PURPLE),("cond",AMBER),("move",FG2),("sel",AMBER)):
            self._evlist.tag_configure(tag,foreground=col)

    def _build_macros_tab(self, p):
        p.columnconfigure(0,weight=1); p.rowconfigure(0,weight=1)
        f=ttk.Frame(p,style="Card.TFrame"); f.grid(row=0,column=0,sticky="nsew",padx=10,pady=10)
        f.columnconfigure(0,weight=1); f.rowconfigure(0,weight=1)
        self._tree2=ttk.Treeview(f,style="Macro.Treeview",
                                 columns=("events","duration","tag","saved"),
                                 show="headings",selectmode="browse")
        for col,w,anc in (("events",60,"e"),("duration",80,"e"),("tag",100,"w"),("saved",160,"w")):
            self._tree2.heading(col,text=col,anchor=anc)
            self._tree2.column(col,width=w,anchor=anc,stretch=(col=="saved"))
        sb2=ttk.Scrollbar(f,orient="vertical",command=self._tree2.yview)
        self._tree2.configure(yscrollcommand=sb2.set)
        self._tree2.pack(side="left",fill="both",expand=True,padx=2,pady=2)
        sb2.pack(side="right",fill="y")
        self._tree2.bind("<Double-1>",lambda _:self._macro_tab_load())

        bf=ttk.Frame(p); bf.grid(row=1,column=0,sticky="ew",padx=10,pady=(0,10))
        for i in range(4): bf.columnconfigure(i,weight=1)
        for i,(t,c) in enumerate((("Load",self._macro_tab_load),("Play",self._macro_tab_play),
                                    ("Chain",self._open_chain),("Delete",self._macro_tab_delete))):
            ttk.Button(bf,text=t,command=c).grid(row=0,column=i,sticky="ew",
                padx=(0 if i==0 else 3, 0 if i==3 else 3))

    def _build_settings(self, p):
        p.columnconfigure(1,weight=1)
        row=[0]
        def _row(): r=row[0]; row[0]+=1; return r

        def _label(text):
            r=_row()
            tk.Label(p,text=text,font=FONTSM,bg=BG,fg=FG2).grid(row=r,column=0,padx=(16,8),pady=6,sticky="e")
            return r

        r=_label("Relative coords")
        self._rel_var=tk.BooleanVar(value=False)
        def _rel_toggle():
            self.rec._rel_mode=self._rel_var.get()
            if self._rel_var.get(): self._pick_anchor()
        ttk.Checkbutton(p,variable=self._rel_var,command=_rel_toggle,
                        text="record positions relative to anchor").grid(row=r,column=1,sticky="w")

        r=_label("Anchor point")
        af=ttk.Frame(p); af.grid(row=r,column=1,sticky="w")
        self._anchor_lbl=tk.Label(af,text="(0, 0)",font=FONTSM,bg=BG,fg=FG)
        self._anchor_lbl.pack(side="left")
        ttk.Button(af,text="Pick",command=self._pick_anchor).pack(side="left",padx=8)

        r=_label("Window guard")
        wf=ttk.Frame(p); wf.grid(row=r,column=1,sticky="w")
        self._guard_var=tk.StringVar()
        ttk.Entry(wf,textvariable=self._guard_var,font=FONT,width=28).pack(side="left")
        ttk.Button(wf,text="Use active",command=lambda:self._guard_var.set(_active_window_title()[:40])
                   ).pack(side="left",padx=6)
        ttk.Button(wf,text="Clear",command=lambda:self._guard_var.set("")).pack(side="left")
        self._guard_var.trace_add("write",lambda *_: setattr(self.rec,"_window_guard",self._guard_var.get() or None))

        r=_label("Move interval (ms)")
        self._minterval_var=tk.IntVar(value=MOVE_INTERVAL_MS)
        ttk.Scale(p,from_=8,to=100,variable=self._minterval_var,orient="horizontal",
                  command=lambda _: self._apply_move_interval()).grid(row=r,column=1,sticky="ew",padx=(0,16),pady=4)

        r=_label("Move min dist (px)")
        self._mdist_var=tk.IntVar(value=4)
        ttk.Scale(p,from_=1,to=20,variable=self._mdist_var,orient="horizontal",
                  command=lambda _: self._apply_move_dist()).grid(row=r,column=1,sticky="ew",padx=(0,16),pady=4)

        r=_label("Panic suppress (s)")
        self._panic_var=tk.DoubleVar(value=PANIC_SUPPRESS_S)
        ttk.Scale(p,from_=0.5,to=10.0,variable=self._panic_var,orient="horizontal"
                  ).grid(row=r,column=1,sticky="ew",padx=(0,16),pady=4)

        r=_label("Image anchoring")
        if _CV2 and _PIL:
            ia_f=ttk.Frame(p); ia_f.grid(row=r,column=1,sticky="w")
            ttk.Button(ia_f,text="Capture template",command=self._capture_template).pack(side="left")
            ttk.Button(ia_f,text="Test find",command=self._test_template).pack(side="left",padx=6)
            self._tmpl_path=tk.StringVar(value="")
            ttk.Entry(ia_f,textvariable=self._tmpl_path,width=20,font=FONTSM).pack(side="left",padx=4)
        else:
            tk.Label(p,text="pip install opencv-python Pillow for image anchoring",
                     font=FONTSM,bg=BG,fg=FG2).grid(row=r,column=1,sticky="w")

        r=_label("About")
        tk.Label(p,text="Shift+Esc = panic  |  F9-F12 = global hotkeys  |  Right-click macros for options",
                 font=FONTSM,bg=BG,fg=FG2).grid(row=r,column=1,sticky="w")

    def _apply_move_interval(self):
        global MOVE_INTERVAL_MS; MOVE_INTERVAL_MS=int(self._minterval_var.get())
    def _apply_move_dist(self):
        global MOVE_MIN_DIST_SQ; v=int(self._mdist_var.get()); MOVE_MIN_DIST_SQ=v*v

    def _pick_anchor(self):
        self._show_toast("Move mouse to anchor point then press F9…")
        def _wait():
            while True:
                time.sleep(0.05)
                if self.rec._recording: break
            time.sleep(0.05)
            x,y=self.rec._lx,self.rec._ly
            self.rec.set_anchor(x,y)
            self.rec.stop_recording()
            self.after(0,lambda:self._anchor_lbl.config(text=f"({x}, {y})"))
            self.after(0,lambda:self._show_toast(f"Anchor set to ({x},{y})"))
        threading.Thread(target=_wait,daemon=True).start()

    def _capture_template(self):
        if not (_CV2 and _PIL): return
        self._show_toast("Drag to select region on screen…")
        def _sel():
            time.sleep(0.3)
            import tempfile
            img=ImageGrab.grab()
            path=Path(tempfile.gettempdir())/"macro_tmpl.png"
            img.save(path)
            self.after(0,lambda:self._tmpl_path.set(str(path)))
            self.after(0,lambda:self._show_toast(f"Template saved: {path.name}"))
        threading.Thread(target=_sel,daemon=True).start()

    def _test_template(self):
        if not (_CV2 and _PIL): return
        p=self._tmpl_path.get()
        if not p: self._show_toast("No template path set."); return
        loc=_template_find(p)
        if loc: self._show_toast(f"Found at {loc}")
        else:   self._show_toast("Template not found on screen.")

    def _build_poll_dispatch(self):
        sl=self._stat_labels
        def _recorded(n,dur,counts):
            sl["events"].config(text=str(n)); sl["clicks"].config(text=str(counts[T_CLICK]))
            sl["keys"].config(text=str(counts[T_KEY])); sl["duration"].config(text=f"{dur:.0f}ms")
            self._density.redraw(self.rec.density())
            self._refresh_timeline()
        self._poll_dispatch={
            "state":    lambda m: self._set_state(m[1]),
            "event":    lambda m: self._log_event(m[1]),
            "recorded": lambda m: _recorded(m[1],m[2],m[3]),
            "progress": lambda m: self.__setattr__("_pbar_val",m[1]) or self._pbar.configure(value=m[1]*100),
            "toast":    lambda m: self._show_toast(m[1]),
            "dryrun_start": lambda m: self._start_overlay(m[1]),
            "dryrun_end":   lambda m: self._stop_overlay(),
            "dryrun_event": lambda m: self._overlay and self._overlay.flash(m[1]),
            "event_replace":lambda m: None,
        }

    def _poll(self):
        get=self.rec.ui_q.get_nowait; d=self._poll_dispatch
        try:
            while True:
                msg=get(); h=d.get(msg[0])
                if h: h(msg)
        except queue.Empty: pass
        self.after(40,self._poll)

    _STATE_CFG={
        "recording":("recording",RED,  "⏹  Stop Rec",None,  True),
        "playing":  ("playing",  GREEN,None,"▶  Playing…",   True),
        "idle":     ("idle",     FG2,  "⏺  Record","▶  Play",False),
    }

    def _set_state(self, s):
        self._state=s
        lt,dc,rt,pt,puls=self._STATE_CFG[s]
        self._status_lbl.config(text=lt,fg=dc); self._dot.config(fg=dc)
        if rt: self._btn_rec.config(text=rt)
        if pt: self._btn_play.config(text=pt)
        if s=="recording": self._btn_play.state(["disabled"]); self._log_divider("● REC START")
        elif s=="playing": self._btn_rec.state(["disabled"])
        else:
            self._btn_rec.state(["!disabled"]); self._btn_play.state(["!disabled"])
            self._pulse_on=False
        if puls: self._pulse()

    def _pulse(self):
        if self._state=="idle": return
        c=RED if self._state=="recording" else GREEN
        self._dot.config(fg=c if self._pulse_on else BG2)
        self._pulse_on=not self._pulse_on
        self.after(500,self._pulse)

    _LOG_FMT={
        T_CLICK: lambda ev:(f"click  {ev.button.replace('Button.',''):<6} ({ev.x},{ev.y})\n","click",ev.pressed),
        T_KEY:   lambda ev:(f"key    {ev.key}\n","key",ev.pressed),
        T_SCROLL:lambda ev:(f"scroll ({ev.dx},{ev.dy}) @ ({ev.x},{ev.y})\n","scroll",True),
        T_HOLD:  lambda ev:(f"hold   {ev.key} {ev.duration:.0f}ms\n","hold",True),
        T_COND:  lambda ev:(f"cond   {ev.kind} ({ev.x},{ev.y})\n","cond",True),
    }

    def _log_event(self, ev):
        fmt=self._LOG_FMT.get(ev.type)
        if fmt is None: return
        text,tag,show=fmt(ev)
        if not show: return
        log=self._log; log.config(state="normal")
        ts=f"[{ev.t:>8.1f}ms] "
        log.insert("end",ts,"ts"); log.insert("end",text,tag)
        log.see("end"); log.config(state="disabled")

    def _log_divider(self, text):
        log=self._log; log.config(state="normal")
        log.insert("end",f"── {text} ──\n","ts"); log.config(state="disabled")

    def _clear_log(self):
        self._log.config(state="normal"); self._log.delete("1.0","end"); self._log.config(state="disabled")

    def _refresh_tree(self):
        q=self._search_var.get().lower() if hasattr(self,"_search_var") else ""
        for tree in (self._tree, getattr(self,"_tree2",None)):
            if tree is None: continue
            for r in tree.get_children(): tree.delete(r)
            for name,d in self.rec.macros.items():
                tag=d.get("tag","")
                if q and q not in name.lower() and q not in tag.lower(): continue
                tree.insert("","end",iid=name,values=(
                    d["event_count"],f"{d['duration_ms']:.0f}ms",tag,d.get("saved_at","?")))

    def _filter_tree(self): self._refresh_tree()

    def _sel(self):
        s=self._tree.selection(); return s[0] if s else None

    def _tree_ctx(self, e):
        r=self._tree.identify_row(e.y)
        if r: self._tree.selection_set(r)
        self._ctx.post(e.x_root,e.y_root)

    def _ctx_load(self):
        name=self._sel()
        if not name: return
        evs=self.rec.load_macro(name)
        if not evs: return
        self.rec.events=evs; c=self.rec._counts(); sl=self._stat_labels
        sl["events"].config(text=str(len(evs))); sl["clicks"].config(text=str(c[T_CLICK]))
        sl["keys"].config(text=str(c[T_KEY])); sl["duration"].config(text=f"{evs[-1].t:.0f}ms")
        self._density.redraw(self.rec.density()); self._refresh_timeline()
        self._show_toast(f"Loaded '{name}' ({len(evs)} events)")

    def _ctx_play(self):
        name=self._sel()
        if not name: return
        evs=self.rec.load_macro(name)
        if evs: self.rec.events=evs; self._do_play()

    def _ctx_dry(self):
        name=self._sel()
        if not name: return
        evs=self.rec.load_macro(name)
        if evs: self.rec.events=evs; self._do_dry_run()

    def _ctx_rename(self):
        name=self._sel()
        if not name: return
        new=simpledialog.askstring("Rename",f"New name for '{name}':",initialvalue=name,parent=self)
        if not new or new==name: return
        ok,msg=self.rec.rename_macro(name,new)
        if ok: self._refresh_tree()
        else: self._show_toast(msg)

    def _ctx_tag(self):
        name=self._sel()
        if not name: return
        cur=self.rec.macros[name].get("tag","")
        t=simpledialog.askstring("Tag",f"Tag for '{name}':",initialvalue=cur,parent=self)
        if t is None: return
        self.rec.macros[name]["tag"]=t
        _save_json_atomic(MACROS_FILE,self.rec.macros)
        self._refresh_tree()

    def _ctx_delete(self):
        name=self._sel()
        if not name: return
        if messagebox.askyesno("Delete",f"Delete '{name}'?",parent=self):
            self.rec.delete_macro(name); self._refresh_tree()

    def _macro_tab_load(self):
        s=self._tree2.selection()
        if not s: return
        evs=self.rec.load_macro(s[0])
        if evs:
            self.rec.events=evs; self._show_toast(f"Loaded '{s[0]}'")
            self._density.redraw(self.rec.density()); self._refresh_timeline()

    def _macro_tab_play(self):
        s=self._tree2.selection()
        if not s: return
        evs=self.rec.load_macro(s[0])
        if evs: self.rec.events=evs; self._do_play()

    def _macro_tab_delete(self):
        s=self._tree2.selection()
        if not s: return
        if messagebox.askyesno("Delete",f"Delete '{s[0]}'?",parent=self):
            self.rec.delete_macro(s[0]); self._refresh_tree()

    def _open_chain(self): ChainEditor(self,self.rec)

    def _toggle_record(self):
        if self._state=="recording": self.rec.stop_recording()
        else:
            self._clear_log()
            for k in self._stat_labels.values(): k.config(text="—")
            self.rec.start_recording()

    def _do_play(self):
        if self._state=="playing": return
        self.rec.start_playback(speed=round(self._speed_var.get(),2),
                                loops=int(self._loops_var.get()),
                                loop_delay=round(self._delay_var.get(),1))

    def _do_dry_run(self):
        if self._state=="playing": return
        self.rec.start_playback(speed=round(self._speed_var.get(),2),
                                loops=1,loop_delay=0,dry_run=True)

    def _do_stop(self):
        if self._state=="recording": self.rec.stop_recording()
        elif self._state=="playing": self.rec.stop_playback()

    def _do_save(self):
        if not self.rec.events: self._show_toast("Nothing to save."); return
        name=simpledialog.askstring("Save macro","Name:",
                                    initialvalue=f"macro_{len(self.rec.macros)+1}",parent=self)
        if not name: return
        tag=simpledialog.askstring("Tag","Tag (optional, press OK to skip):",initialvalue="",parent=self) or ""
        ok,result=self.rec.save_current(name,tag)
        if ok: self._refresh_tree(); self._show_toast(f"Saved '{result}'")
        else: self._show_toast(result)

    def _do_trim(self):
        if not self.rec.events: self._show_toast("Nothing to trim."); return
        d=_TwoFieldDialog(self,"Trim","Start (ms):","End (ms, blank=end):","0","")
        if d.result is None: return
        s,e=d.result
        ok,msg=self.rec.trim(float(s) if s else 0.0,float(e) if e else None)
        self._show_toast(msg)
        if ok: self._post_edit()

    def _do_dedupe(self):
        if not self.rec.events: self._show_toast("Nothing to dedupe."); return
        v=simpledialog.askfloat("Dedupe","Min distance (px):",initialvalue=2.0,minvalue=0.5,maxvalue=100,parent=self)
        if v is None: return
        n=self.rec.dedupe_moves(v); self._show_toast(f"Removed {n} move events."); self._post_edit()

    def _do_scale(self):
        if not self.rec.events: self._show_toast("Nothing to scale."); return
        v=simpledialog.askfloat("Scale","Factor (0.5 = 2× faster):",initialvalue=1.0,minvalue=0.01,maxvalue=100,parent=self)
        if v is None: return
        self.rec.speed_scale(v); self._show_toast(f"Scaled ×{v}"); self._post_edit()

    def _do_compress(self):
        if not self.rec.events: self._show_toast("Nothing to compress."); return
        n=self.rec.compress_keys(); self._show_toast(f"Compressed {n} key holds."); self._post_edit()

    def _do_simplify(self):
        if not self.rec.events: self._show_toast("Nothing to simplify."); return
        v=simpledialog.askfloat("Simplify","Epsilon (px tolerance):",initialvalue=1.5,minvalue=0.1,maxvalue=20,parent=self)
        if v is None: return
        n=self.rec.simplify_moves(v); self._show_toast(f"Removed {n} move points."); self._post_edit()

    def _do_export(self):
        if not self.rec.events: self._show_toast("Nothing to export."); return
        path=filedialog.asksaveasfilename(parent=self,defaultextension=".json",filetypes=[("JSON","*.json"),("All","*.*")])
        if not path: return
        self.rec.export_json(path); self._show_toast(f"Exported → {Path(path).name}")

    def _do_import(self):
        path=filedialog.askopenfilename(parent=self,filetypes=[("JSON","*.json"),("All","*.*")])
        if not path: return
        ok=self.rec.import_json(path)
        if ok: self._show_toast(f"Imported {len(self.rec.events)} events"); self._post_edit()
        else: self._show_toast("Import failed.")

    def _do_add_condition(self):
        if not self.rec.events: self._show_toast("No events loaded."); return
        kinds=["pixel","template"]
        d=_PickDialog(self,"Add Condition","Condition type:",kinds)
        if not d.result: return
        if d.result=="pixel":
            x=simpledialog.askinteger("Pixel X","X coordinate:",parent=self)
            y=simpledialog.askinteger("Pixel Y","Y coordinate:",parent=self)
            if x is None or y is None: return
            col=_pixel_color(x,y) if _PIL else None
            idx=self._sel_ev_idx or 0
            self.rec.add_condition("pixel",x,y,list(col) if col else None,10,idx)
            self._show_toast(f"Condition added at event {idx}")
        elif d.result=="template":
            path=filedialog.askopenfilename(parent=self,filetypes=[("PNG","*.png"),("All","*.*")])
            if not path: return
            idx=self._sel_ev_idx or 0
            self.rec.add_condition("template",0,0,None,0,idx,path)
            self._show_toast("Template condition added")
        self._post_edit()

    def _post_edit(self):
        c=self.rec._counts(); evs=self.rec.events; sl=self._stat_labels
        if evs:
            sl["events"].config(text=str(len(evs))); sl["duration"].config(text=f"{evs[-1].t:.0f}ms")
            sl["clicks"].config(text=str(c[T_CLICK])); sl["keys"].config(text=str(c[T_KEY]))
        self._density.redraw(self.rec.density()); self._refresh_timeline()

    def _refresh_timeline(self):
        self._timeline.after_idle(lambda:self._timeline.redraw(self.rec.events))
        self._refresh_evlist()

    def _refresh_evlist(self):
        evs=self.rec.events
        el=self._evlist; el.config(state="normal"); el.delete("1.0","end")
        _TTAG={T_CLICK:"click",T_KEY:"key",T_SCROLL:"scroll",T_HOLD:"hold",T_COND:"cond",T_MOVE:"move"}
        for i,ev in enumerate(evs):
            tag=_TTAG.get(ev.type,"ts")
            sel_tag="sel" if i==self._sel_ev_idx else tag
            line=f"{i:>4}  [{ev.t:>8.1f}ms]  {ev.type:<14}"
            if hasattr(ev,"x"): line+=f"  ({ev.x},{ev.y})"
            if hasattr(ev,"key"): line+=f"  {ev.key}"
            if ev.type is T_HOLD: line+=f"  dur={ev.duration:.0f}ms"
            el.insert("end",line+"\n",sel_tag)
        el.config(state="disabled")
        if self._sel_ev_idx is not None:
            el.see(f"{self._sel_ev_idx+1}.0")

    def _timeline_select(self, idx):
        self._sel_ev_idx=idx
        if idx is None or not (0<=idx<len(self.rec.events)):
            self._sel_info.config(text="—"); return
        ev=self.rec.events[idx]
        info=f"#{idx}  {ev.type}  t={ev.t:.1f}ms"
        if hasattr(ev,"x"): info+=f"  ({ev.x},{ev.y})"
        if hasattr(ev,"key"): info+=f"  key={ev.key}"
        self._sel_info.config(text=info)
        self._refresh_evlist()

    def _timeline_ctx(self, idx, rx, ry):
        m=tk.Menu(self,tearoff=0,bg=BG2,fg=FG,activebackground=BG3,activeforeground=FG,font=FONT,borderwidth=0)
        m.add_command(label=f"Delete event #{idx}",command=lambda:self._del_ev(idx))
        m.add_command(label="Edit position",        command=lambda:self._edit_pos(idx))
        m.post(rx,ry)

    def _del_sel_event(self):
        i=self._sel_ev_idx
        if i is None: return
        self._del_ev(i)

    def _del_ev(self, idx):
        if 0<=idx<len(self.rec.events):
            del self.rec.events[idx]
            self._sel_ev_idx=None; self._sel_info.config(text="—")
            self._post_edit()

    def _edit_sel_pos(self): self._edit_pos(self._sel_ev_idx)

    def _edit_pos(self, idx):
        if idx is None or not (0<=idx<len(self.rec.events)): return
        ev=self.rec.events[idx]
        if not hasattr(ev,"x"): self._show_toast("This event has no position."); return
        d=_TwoFieldDialog(self,"Edit Position","X:","Y:",str(ev.x),str(ev.y))
        if d.result is None: return
        try:
            self.rec.set_event_pos(idx,int(d.result[0]),int(d.result[1]))
            self._post_edit()
        except ValueError: self._show_toast("Invalid coordinates.")

    def _start_overlay(self, events):
        if not _PIL: return
        if self._overlay: self._overlay.destroy()
        self._overlay=DryRunOverlay(self,self._sw,self._sh)

    def _stop_overlay(self):
        if self._overlay:
            self._overlay.destroy(); self._overlay=None

    def _show_toast(self, msg, ms=3500):
        self._toast.config(text=msg)
        self.after(ms,lambda:self._toast.config(text=""))

    def _start_tray(self):
        def _icon_img():
            img=Image.new("RGBA",(64,64),(0,0,0,0))
            d=ImageDraw.Draw(img)
            d.ellipse((8,8,56,56),fill=(224,92,92,255))
            d.ellipse((24,24,40,40),fill=(26,26,31,255))
            return img
        def _show(_,__=None): self.after(0,self.deiconify)
        def _quit(_,__=None): self.after(0,self._on_close)
        menu=pystray.Menu(pystray.MenuItem("Show",_show,default=True),
                          pystray.MenuItem("Quit",_quit))
        self._tray_icon=pystray.Icon("MacroRecorder",_icon_img(),"Macro Recorder",menu)
        threading.Thread(target=self._tray_icon.run,daemon=True).start()
        self.protocol("WM_DELETE_WINDOW",self._hide_to_tray)

    def _hide_to_tray(self): self.withdraw()

    def _on_close(self):
        self.rec.stop_recording(); self.rec.stop_playback()
        self.ctrl.stop()
        if _TRAY and hasattr(self,"_tray_icon"):
            try: self._tray_icon.stop()
            except Exception: pass
        self.destroy()


class _TwoFieldDialog(tk.Toplevel):
    def __init__(self, parent, title, l1, l2, i1, i2):
        super().__init__(parent); self.title(title); self.configure(bg=BG)
        self.resizable(False,False); self.grab_set(); self.result=None
        self._entries=[]
        for i,(lbl,init) in enumerate(((l1,i1),(l2,i2))):
            tk.Label(self,text=lbl,font=FONT,bg=BG,fg=FG).grid(row=i,column=0,padx=14,pady=6,sticky="e")
            e=tk.Entry(self,font=FONT,bg=BG3,fg=FG,insertbackground=FG,relief="flat",bd=4)
            e.insert(0,str(init)); e.grid(row=i,column=1,padx=14,pady=6)
            self._entries.append(e)
        fr=ttk.Frame(self); fr.grid(row=2,column=0,columnspan=2,pady=10)
        ttk.Button(fr,text="OK",    command=self._ok    ).pack(side="left",padx=6)
        ttk.Button(fr,text="Cancel",command=self.destroy).pack(side="right",padx=6)
        self._entries[0].focus_set()
        self.bind("<Return>",lambda _:self._ok()); self.bind("<Escape>",lambda _:self.destroy())
        self.wait_window()
    def _ok(self): self.result=tuple(e.get() for e in self._entries); self.destroy()


if __name__=="__main__":
    App().mainloop()