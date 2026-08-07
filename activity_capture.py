#!/usr/bin/env python3
"""
Xeneon Activity Widget - Input Capture Script
Tracks global keyboard and mouse input and writes live stats to a JSON file.

Requirements:
    pip install pynput
    pip install bleak   (optional — only needed with --heart-rate)

Usage:
    python activity_capture.py [--output PATH] [--interval MS]
                                [--preview] [--preview-port PORT]
                                [--control-port PORT]
                                [--heart-rate] [--hr-device NAME_OR_MAC]

Defaults:
    --output           C:/Users/Public/xeneon_stats.json
    --interval         100  (ms)
    --preview-port     8080
    --control-port     7070  (always started, no flag required to enable)

Terminal shortcuts (when terminal is in focus):
    Ctrl+R  — reset all session counters
    Ctrl+C  — stop

HTTP endpoints (always available on --control-port):
    GET  /reset          — reset session counters
    GET  /pause          — pause tracking (stops pynput listeners)
    GET  /resume         — resume tracking (restarts pynput listeners)
    GET  /pause-toggle   — flip between paused and resumed

HTTP endpoints (--preview mode only, on --preview-port):
    GET  /        — widget UI
    GET  /stats   — live JSON
    GET  /reset   — reset counters (same server, same endpoint)
"""

import argparse
import asyncio
import http.server
import json
import mimetypes
import os
import signal
import socketserver
import sys
import time
import threading
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional

try:
    from pynput import keyboard, mouse
except ImportError:
    print("ERROR: pynput not installed.")
    print("Fix:   pip install pynput")
    sys.exit(1)

# ── Tunable constants ────────────────────────────────────────────────────────

POLL_INTERVAL_MS: int = 100

CPS_WINDOW_S: float = 3.0
CPS_SPAN_FLOOR_S: float = 0.05
KPM_WINDOW_S: float = 60.0

TRAIL_WINDOW_S: float = 3.0     # trail fades after 3 seconds
TRAIL_SAMPLE_S: float = 0.05   # one trail point per 50 ms

# Mouse distance: approximation only — true value requires the actual DPI of the
# specific mouse, which is not accessible without the iCUE SDK.  800 DPI is a
# common gaming-mouse default; users with different DPI settings will see
# proportionally different values.  Do NOT treat this as a precise measurement.
#
# Conversion: screen pixels → inches → feet
#   inches = pixels / ASSUMED_DPI
#   feet   = inches / 12
# so:  PIXELS_PER_FOOT = ASSUMED_DPI * 12
ASSUMED_DPI: int = 800
PIXELS_PER_FOOT: float = ASSUMED_DPI * 6    # = 4800 px per foot (×2 correction factor)

DEFAULT_CONTROL_PORT: int = 7070


# ── ActivityTracker ──────────────────────────────────────────────────────────

class ActivityTracker:
    def __init__(self):
        self.session_start = time.time()
        self.total_clicks = 0
        self.total_key_presses = 0
        self.key_counts: dict = defaultdict(int)
        self._click_times: deque = deque()
        self._key_times: deque   = deque()
        self._lock = threading.Lock()
        self._paused_duration: float = 0.0
        self._pause_start: Optional[float] = None

        # Track held keys to ignore repeat events
        self._pressed_keys: set = set()

        # Mouse distance / trail
        self._mouse_distance_px: float = 0.0
        self._last_mouse_pos: Optional[tuple] = None
        self._mouse_trail: deque = deque()          # (timestamp, x, y) — 3-sec window
        self._mouse_trail_all: deque = deque(maxlen=8000)  # full-session trail

        # Screen dimensions for normalising trail to 0-1.
        # Falls back to 1920×1080 if ctypes is unavailable.
        self._screen_w: int = 1920
        self._screen_h: int = 1080
        try:
            import ctypes as _ct
            self._screen_w = _ct.windll.user32.GetSystemMetrics(0)
            self._screen_h = _ct.windll.user32.GetSystemMetrics(1)
        except Exception:
            pass

    # ── pynput callbacks ─────────────────────────────────────────────────────

    def on_click(self, x, y, button, pressed):
        if not pressed:
            return
        with self._lock:
            self.total_clicks += 1
            self._click_times.append(time.time())

    def on_key_press(self, key):
        key_id = str(key)
        with self._lock:
            if key_id in self._pressed_keys:
                return  # ignore held-key repeat events
            self._pressed_keys.add(key_id)
            self.total_key_presses += 1
            self._key_times.append(time.time())
            self.key_counts[_key_name(key)] += 1

    def on_key_release(self, key):
        key_id = str(key)
        with self._lock:
            self._pressed_keys.discard(key_id)

    def on_move(self, x: int, y: int):
        with self._lock:
            now = time.time()
            if self._last_mouse_pos is not None:
                dx = x - self._last_mouse_pos[0]
                dy = y - self._last_mouse_pos[1]
                self._mouse_distance_px += (dx * dx + dy * dy) ** 0.5
            self._last_mouse_pos = (x, y)
            # Decimate: only record a trail point every TRAIL_SAMPLE_S seconds
            if (not self._mouse_trail or
                    now - self._mouse_trail[-1][0] >= TRAIL_SAMPLE_S):
                self._mouse_trail.append((now, x, y))
                self._mouse_trail_all.append((now, x, y))

    # ── pause / resume ───────────────────────────────────────────────────────

    def pause(self) -> None:
        with self._lock:
            if self._pause_start is None:
                self._pause_start = time.time()

    def resume(self) -> None:
        with self._lock:
            if self._pause_start is not None:
                self._paused_duration += time.time() - self._pause_start
                self._pause_start = None

    # ── reset ────────────────────────────────────────────────────────────────

    def reset(self) -> int:
        with self._lock:
            paused_so_far = self._paused_duration
            if self._pause_start is not None:
                paused_so_far += time.time() - self._pause_start
            old_elapsed = max(int(time.time() - self.session_start - paused_so_far), 0)
            self.session_start = time.time()
            self.total_clicks = 0
            self.total_key_presses = 0
            self.key_counts.clear()
            self._click_times.clear()
            self._key_times.clear()
            self._paused_duration = 0.0
            self._pressed_keys.clear()
            self._mouse_distance_px = 0.0
            self._last_mouse_pos = None
            self._mouse_trail.clear()
            self._mouse_trail_all.clear()
            if self._pause_start is not None:
                self._pause_start = time.time()
        return old_elapsed

    # ── snapshot ─────────────────────────────────────────────────────────────

    def snapshot(self, control_port: int = DEFAULT_CONTROL_PORT) -> dict:
        now = time.time()
        with self._lock:
            self._trim(now)

            is_paused     = self._pause_start is not None
            paused_so_far = self._paused_duration
            if is_paused:
                paused_so_far += now - self._pause_start
            elapsed = max(now - self.session_start - paused_so_far, 0.001)

            cps_live = _compute_cps(self._click_times, now)
            kpm_live = len(self._key_times)
            cps_avg  = round(self.total_clicks / elapsed, 2)

            top_keys = sorted(
                [{"key": k, "count": v} for k, v in self.key_counts.items()],
                key=lambda x: x["count"],
                reverse=True,
            )[:5]

            mouse_distance_px = round(self._mouse_distance_px, 1)
            mouse_distance_ft = round(
                self._mouse_distance_px / PIXELS_PER_FOOT, 1
            )

            sw = max(self._screen_w, 1)
            sh = max(self._screen_h, 1)
            trail = [
                {"x": round(t[1] / sw, 4), "y": round(t[2] / sh, 4)}
                for t in self._mouse_trail
            ]
            trail_all = [
                {"x": round(t[1] / sw, 4), "y": round(t[2] / sh, 4)}
                for t in self._mouse_trail_all
            ]

            return {
                "session_start":     self.session_start,
                "session_seconds":   int(elapsed),
                "total_clicks":      self.total_clicks,
                "total_key_presses": self.total_key_presses,
                "cps_live":          cps_live,
                "cps_avg":           cps_avg,
                "kpm_live":          kpm_live,
                "top_keys":          top_keys,
                "mouse_distance_px":  mouse_distance_px,
                "mouse_distance_ft":  mouse_distance_ft,
                "mouse_trail":       trail,
                "mouse_trail_all":   trail_all,
                "poll_interval_ms":  POLL_INTERVAL_MS,
                "control_port":      control_port,
                "paused":            is_paused,
                "ts":                now,
            }

    def _trim(self, now: float):
        cutoff_cps = now - CPS_WINDOW_S
        while self._click_times and self._click_times[0] < cutoff_cps:
            self._click_times.popleft()
        cutoff_kpm = now - KPM_WINDOW_S
        while self._key_times and self._key_times[0] < cutoff_kpm:
            self._key_times.popleft()
        cutoff_trail = now - TRAIL_WINDOW_S
        while self._mouse_trail and self._mouse_trail[0][0] < cutoff_trail:
            self._mouse_trail.popleft()


# ── PauseController ───────────────────────────────────────────────────────────

class PauseController:
    """
    Manages pause/resume of pynput listeners.

    Privacy guarantee: when paused, listeners are STOPPED completely — no
    keyboard or mouse events are captured, processed, or written to the JSON
    file. This is intentional. Do not weaken this guarantee in future refactors
    by merely ignoring events while keeping listeners alive.
    """

    def __init__(self, tracker: ActivityTracker):
        self._tracker = tracker
        self._paused = False
        self._lock = threading.Lock()
        self._mouse_listener:    Optional[mouse.Listener]    = None
        self._keyboard_listener: Optional[keyboard.Listener] = None

    def set_listeners(self, ml: mouse.Listener, kl: keyboard.Listener) -> None:
        with self._lock:
            self._mouse_listener    = ml
            self._keyboard_listener = kl

    def pause(self) -> None:
        with self._lock:
            if self._paused:
                return
            self._paused = True
            ml, kl = self._mouse_listener, self._keyboard_listener
        self._tracker.pause()
        if ml:
            ml.stop()
        if kl:
            kl.stop()

    def resume(self) -> None:
        with self._lock:
            if not self._paused:
                return
            self._paused = False
        self._tracker.resume()
        ml = mouse.Listener(
            on_click=self._tracker.on_click,
            on_move=self._tracker.on_move,
        )
        kl = keyboard.Listener(on_press=self._tracker.on_key_press,
                               on_release=self._tracker.on_key_release)
        ml.start()
        kl.start()
        with self._lock:
            self._mouse_listener    = ml
            self._keyboard_listener = kl

    def toggle(self) -> None:
        if self._paused:
            self.resume()
        else:
            self.pause()

    def stop_all(self) -> None:
        with self._lock:
            ml, kl = self._mouse_listener, self._keyboard_listener
        if ml:
            ml.stop()
        if kl:
            kl.stop()

    @property
    def is_paused(self) -> bool:
        return self._paused


# ── HRTracker ─────────────────────────────────────────────────────────────────

class HRTracker:
    """Thread-safe store for BLE heart-rate data."""

    def __init__(self) -> None:
        self._hr:     Optional[int] = None
        self._active: bool          = False
        self._lock                  = threading.Lock()

    def update(self, bpm: int) -> None:
        with self._lock:
            self._hr = bpm

    def set_active(self, active: bool) -> None:
        with self._lock:
            self._active = active
            if not active:
                self._hr = None

    @property
    def heart_rate(self) -> Optional[int]:
        with self._lock:
            return self._hr

    @property
    def is_active(self) -> bool:
        with self._lock:
            return self._active


# ── CPS calculation ───────────────────────────────────────────────────────────

def _compute_cps(click_times: deque, now: float) -> float:
    if not click_times:
        return 0.0
    elapsed = now - click_times[0]
    span = max(elapsed, 1.0)
    return round(len(click_times) / span, 2)


# ── helpers ──────────────────────────────────────────────────────────────────

def _key_name(key) -> str:
    try:
        if key.char:
            return key.char.upper()
    except AttributeError:
        pass
    raw = str(key).replace("Key.", "")
    aliases = {
        "space": "Space", "enter": "Enter", "backspace": "Bksp",
        "shift": "Shift", "shift_r": "Shift", "ctrl_l": "Ctrl",
        "ctrl_r": "Ctrl", "alt_l": "Alt", "alt_r": "Alt",
        "cmd": "Win", "tab": "Tab", "esc": "Esc",
        "up": "↑", "down": "↓", "left": "←", "right": "→",
        "caps_lock": "Caps",
    }
    return aliases.get(raw.lower(), raw.title())


def _fmt_duration(secs: int) -> str:
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _get_widget_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "widget"
    return Path(__file__).resolve().parent.parent / "widget"


# ── BLE heart-rate listener ───────────────────────────────────────────────────

def start_hr_listener(hr_tracker: HRTracker,
                      device_name: Optional[str] = None) -> None:
    """
    Start a background thread that connects to a BLE Heart Rate monitor.
    Requires bleak; silently skips if bleak is not installed.
    Reconnects automatically every 5 seconds on disconnection.
    """
    try:
        import bleak  # noqa: F401 — availability check only
    except ImportError:
        print("INFO: bleak not installed — heart rate tracking disabled.")
        print("      pip install bleak")
        return

    HR_SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"
    HR_CHAR_UUID    = "00002a37-0000-1000-8000-00805f9b34fb"

    async def _connect_and_stream(verbose: bool) -> None:
        from bleak import BleakClient, BleakScanner

        if verbose:
            print("\nScanning for BLE Heart Rate devices (10 sec)…")
        found = await BleakScanner.discover(
            timeout=10.0, service_uuids=[HR_SERVICE_UUID]
        )

        if not found:
            if verbose:
                print("  No BLE Heart Rate devices found. Retrying in 5 sec.")
            return

        if verbose:
            print(f"  Found {len(found)} device(s):")
            for d in found:
                print(f"    {d.name or '(unnamed)'}  [{d.address}]")

        target = None
        if device_name:
            dn = device_name.lower()
            for d in found:
                if dn in (d.name or "").lower() or dn == d.address.lower():
                    target = d
                    break
            if target is None:
                avail = [d.name or d.address for d in found]
                if verbose:
                    print(f"  '{device_name}' not found. Available: {avail}")
                return
        else:
            target = found[0]

        if verbose:
            print(f"  Connecting to {target.name or target.address}…")

        loop = asyncio.get_event_loop()
        disconnected_evt = asyncio.Event()

        def _on_disconnect(_client) -> None:
            loop.call_soon_threadsafe(disconnected_evt.set)

        def _hr_notification(_sender, data: bytearray) -> None:
            flags = data[0]
            bpm = int.from_bytes(data[1:3], "little") if (flags & 0x01) else data[1]
            hr_tracker.update(bpm)

        try:
            async with BleakClient(
                target.address, disconnected_callback=_on_disconnect
            ) as client:
                hr_tracker.set_active(True)
                if verbose:
                    print(
                        f"  Connected — heart rate active "
                        f"({target.name or target.address})"
                    )
                await client.start_notify(HR_CHAR_UUID, _hr_notification)
                await disconnected_evt.wait()
        finally:
            hr_tracker.set_active(False)

        print("\n  HR device disconnected. Reconnecting in 5 sec…", flush=True)

    async def _ble_loop() -> None:
        first = True
        while True:
            try:
                await _connect_and_stream(verbose=first)
            except Exception as exc:
                if first:
                    print(f"\n  BLE error: {exc}")
            first = False
            await asyncio.sleep(5)

    def _thread() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_ble_loop())
        except Exception:
            pass

    threading.Thread(target=_thread, daemon=True, name="hr-ble").start()


# ── Ctrl+R terminal reset listener ───────────────────────────────────────────

def start_reset_listener(tracker: ActivityTracker) -> None:
    """
    Listen for Ctrl+R on stdin (Windows msvcrt).
    Ctrl+C is forwarded back as SIGINT so it is not swallowed by this thread.
    """
    if sys.platform != "win32":
        return
    try:
        import msvcrt, ctypes
        if ctypes.windll.kernel32.GetConsoleWindow() == 0:
            return
    except Exception:
        return

    def _listen():
        while True:
            try:
                ch = msvcrt.getch()
            except Exception:
                return
            if ch == b"\x03":
                os.kill(os.getpid(), signal.CTRL_C_EVENT)
                return
            if ch == b"\x12":
                old = tracker.reset()
                print(f"\nSession reset at {_fmt_duration(old)}")

    threading.Thread(target=_listen, daemon=True).start()


# ── HTTP server helpers ───────────────────────────────────────────────────────

class _ReusableTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _start_server(handler_class, port: int) -> None:
    server = _ReusableTCPServer(("", port), handler_class)
    threading.Thread(target=server.serve_forever, daemon=True).start()


# ── Control server ────────────────────────────────────────────────────────────

def start_control_server(tracker: ActivityTracker, port: int,
                         pause_ctrl: "PauseController") -> None:
    class _ControlHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/reset":
                old = tracker.reset()
                self._respond(200, "text/plain",
                              f"Session reset at {_fmt_duration(old)}".encode())
            elif path == "/pause":
                pause_ctrl.pause()
                self._respond(200, "text/plain", b"paused")
            elif path == "/resume":
                pause_ctrl.resume()
                self._respond(200, "text/plain", b"resumed")
            elif path == "/pause-toggle":
                pause_ctrl.toggle()
                state = b"paused" if pause_ctrl.is_paused else b"resumed"
                self._respond(200, "text/plain", state)
            else:
                self.send_response(404)
                self.end_headers()

        def _respond(self, code: int, mime: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            pass

    _start_server(_ControlHandler, port)
    print(f"Control server : http://localhost:{port}/reset         (reset session)")
    print(f"                 http://localhost:{port}/pause-toggle  (privacy toggle)")


# ── Preview server ────────────────────────────────────────────────────────────

def start_preview_server(tracker: ActivityTracker, port: int,
                         control_port: int) -> None:
    widget_dir = _get_widget_dir()
    if not widget_dir.is_dir():
        print(f"\nWARN: widget dir not found at {widget_dir} — static files won't be served.")

    def _make_handler():
        class _PreviewHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                path = self.path.split("?")[0]

                if path == "/stats":
                    body = json.dumps(tracker.snapshot(control_port)).encode()
                    self._ok("application/json", body, cors=True)
                    return

                if path == "/reset":
                    old = tracker.reset()
                    body = f"Session reset at {_fmt_duration(old)}".encode()
                    self._ok("text/plain", body, cors=True)
                    return

                if path == "/":
                    path = "/index.html"
                fp = widget_dir / path.lstrip("/")
                if fp.is_file():
                    mime = mimetypes.guess_type(str(fp))[0] or "application/octet-stream"
                    self._ok(mime, fp.read_bytes())
                else:
                    self.send_response(404)
                    self.end_headers()

            def _ok(self, mime: str, body: bytes, cors: bool = False):
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(body)))
                if cors:
                    self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                pass

        return _PreviewHandler

    _start_server(_make_handler(), port)
    print(f"Preview server : http://localhost:{port}/")
    print(f"Live stats JSON: http://localhost:{port}/stats")


# ── Writer loop ───────────────────────────────────────────────────────────────

def write_loop(tracker: ActivityTracker, hr_tracker: Optional[HRTracker],
               output: Path, interval_s: float, control_port: int) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(".tmp")
    print(f"Output  : {output}")
    print(f"Interval: {int(interval_s * 1000)} ms  |  Ctrl+R to reset  |  Ctrl+C to stop\n")

    while True:
        data = tracker.snapshot(control_port)

        # Merge heart-rate fields (always present so widget can check heart_rate_active)
        if hr_tracker is not None:
            data["heart_rate"]        = hr_tracker.heart_rate
            data["heart_rate_active"] = hr_tracker.is_active
        else:
            data["heart_rate"]        = None
            data["heart_rate_active"] = False

        try:
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, output)
        except (PermissionError, FileNotFoundError):
            pass

        dur = _fmt_duration(data["session_seconds"])
        paused_tag = "  [PAUSED]" if data["paused"] else ""
        hr_val = data["heart_rate"]
        hr_tag = f"  HR {hr_val:>3}bpm" if hr_val is not None else ""
        print(
            f"\r[{dur}]  Clicks {data['total_clicks']:>6}  CPS {data['cps_live']:>7.2f}  "
            f"Keys {data['total_key_presses']:>7}  KPM {data['kpm_live']:>4}  "
            f"Dist {data['mouse_distance_ft']:>6.1f}ft{hr_tag}{paused_tag}          ",
            end="",
            flush=True,
        )
        time.sleep(interval_s)


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Xeneon Activity Widget — Input Capture")
    ap.add_argument("--output", "-o",
                    default=r"C:\Users\Public\xeneon_stats.json",
                    help="Path to write the JSON stats file")
    ap.add_argument("--interval", "-i",
                    type=int, default=POLL_INTERVAL_MS,
                    help=f"Write interval in milliseconds (default: {POLL_INTERVAL_MS})")
    ap.add_argument("--preview", action="store_true",
                    help="Serve the widget and live stats over HTTP for browser preview")
    ap.add_argument("--preview-port", type=int, default=8080, dest="preview_port",
                    help="Port for the preview HTTP server (default: 8080)")
    ap.add_argument("--control-port", type=int, default=DEFAULT_CONTROL_PORT,
                    dest="control_port",
                    help=f"Port for the always-on control server (default: {DEFAULT_CONTROL_PORT})")
    ap.add_argument("--heart-rate", action="store_true", dest="heart_rate",
                    help="Enable BLE heart rate monitoring (requires: pip install bleak)")
    ap.add_argument("--hr-device", default=None, dest="hr_device",
                    metavar="NAME_OR_MAC",
                    help="BLE device name or MAC address (default: first HR device found)")
    args = ap.parse_args()

    tracker    = ActivityTracker()
    pause_ctrl = PauseController(tracker)

    hr_tracker: Optional[HRTracker] = None
    if args.heart_rate:
        hr_tracker = HRTracker()
        start_hr_listener(hr_tracker, args.hr_device)

    start_control_server(tracker, args.control_port, pause_ctrl)

    if args.preview:
        start_preview_server(tracker, args.preview_port, args.control_port)

    start_reset_listener(tracker)

    mouse_listener    = mouse.Listener(
        on_click=tracker.on_click,
        on_move=tracker.on_move,
    )
    keyboard_listener = keyboard.Listener(on_press=tracker.on_key_press,
                                          on_release=tracker.on_key_release)
    mouse_listener.start()
    keyboard_listener.start()
    pause_ctrl.set_listeners(mouse_listener, keyboard_listener)

    try:
        write_loop(tracker, hr_tracker, Path(args.output), args.interval / 1000.0,
                   args.control_port)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        pause_ctrl.stop_all()


if __name__ == "__main__":
    main()
