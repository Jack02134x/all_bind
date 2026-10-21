#!/usr/bin/env python3
"""
all_bind GUI and CLI - bind anything on any input device to any key or macro.

Edits ~/.config/all_bind/config.json, compiles it to all_bind.conf next to
it and drives the C engine (all_bind). The engine watches that folder and
reloads by itself whenever it's saved.

Run:  all_bind_gui.py              the app (needs PySide6 and python-evdev)
      all_bind_gui.py --cli ...    terminal version (no PySide6 needed)
      (or all_bind --gui / all_bind --cli ...)
"""

import copy
import json
import os
import selectors
import shutil
import signal
import subprocess
import sys
import threading
import time

from evdev import InputDevice, ecodes, list_devices


# =========================================================
# Paths
# =========================================================

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
ENGINE_SRC = os.path.join(SCRIPT_DIR, "all_bind.c")

# Next to all_bind.c (development folder) the engine is rebuilt from source;
# installed from a package it's /usr/bin/all_bind. The config always lives
# in ~/.config/all_bind.
DEV_MODE = os.path.exists(ENGINE_SRC)
if DEV_MODE:
    ENGINE_BIN = os.path.join(SCRIPT_DIR, "all_bind")
else:
    ENGINE_BIN = shutil.which("all_bind") or "/usr/bin/all_bind"

BASE_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                        "all_bind")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")     # GUI / CLI source of truth
CONF_PATH = os.path.join(BASE_DIR, "all_bind.conf")     # generated for the engine
ENGINE_NAME = "all_bind"
SERVICE_TEMPLATE = "all_bind@.service"

# Before configs moved to ~/.config they lived next to the script.
OLD_CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")

# Must match all_bind.c.
VIRTUAL_NAME = "all_bind virtual device"
CLONE_SUFFIX = " (all_bind)"


def is_own_device(name):
    return name == VIRTUAL_NAME or name.endswith(CLONE_SUFFIX)


# =========================================================
# Key / button names
# =========================================================

# Pseudo inputs; values are the engine's pseudo codes.
WHEEL_INPUTS = {"WHEEL_UP": -1, "WHEEL_DOWN": -2, "WHEEL_RIGHT": -3, "WHEEL_LEFT": -4}
DPAD_INPUTS = {"DPAD_UP": -5, "DPAD_DOWN": -6, "DPAD_LEFT": -7, "DPAD_RIGHT": -8}
PSEUDO_BY_CODE = {v: k for k, v in {**WHEEL_INPUTS, **DPAD_INPUTS}.items()}

FRIENDLY = {
    "BTN_LEFT": "Left click",
    "BTN_RIGHT": "Right click",
    "BTN_MIDDLE": "Middle click",
    "BTN_SIDE": "Side button (back)",
    "BTN_EXTRA": "Extra button (forward)",
    "BTN_FORWARD": "Forward button",
    "BTN_BACK": "Back button",
    "BTN_TASK": "Task button",
    "WHEEL_UP": "Wheel up",
    "WHEEL_DOWN": "Wheel down",
    "WHEEL_LEFT": "Wheel left",
    "WHEEL_RIGHT": "Wheel right",
    "DPAD_UP": "D-pad up",
    "DPAD_DOWN": "D-pad down",
    "DPAD_LEFT": "D-pad left",
    "DPAD_RIGHT": "D-pad right",
    "BTN_SOUTH": "Pad A / Cross",
    "BTN_EAST": "Pad B / Circle",
    "BTN_NORTH": "Pad X / Triangle",
    "BTN_WEST": "Pad Y / Square",
    "BTN_TL": "Pad L1",
    "BTN_TR": "Pad R1",
    "BTN_TL2": "Pad L2",
    "BTN_TR2": "Pad R2",
    "BTN_SELECT": "Pad Select / Share",
    "BTN_START": "Pad Start / Options",
    "BTN_MODE": "Pad Home / PS",
    "BTN_THUMBL": "Pad L3",
    "BTN_THUMBR": "Pad R3",
}


def _build_names():
    name_to_code = {}
    code_to_name = {}
    for name, code in ecodes.ecodes.items():
        if name.startswith(("KEY_", "BTN_")) and isinstance(code, int):
            name_to_code[name] = code
    for code, names in ecodes.keys.items():
        if isinstance(names, (list, tuple)):
            # Prefer the gamepad names (BTN_SOUTH over BTN_A) and BTN_LEFT over BTN_MOUSE.
            pick = [n for n in names if n in FRIENDLY] or list(names)
            names = pick[0]
        code_to_name[code] = names
    return name_to_code, code_to_name


NAME_TO_CODE, CODE_TO_NAME = _build_names()


def code_name(code):
    if code in PSEUDO_BY_CODE:
        return PSEUDO_BY_CODE[code]
    return CODE_TO_NAME.get(code, f"CODE_{code}")


def input_code(name):
    """Engine code for an input name, or None."""
    if name in WHEEL_INPUTS:
        return WHEEL_INPUTS[name]
    if name in DPAD_INPUTS:
        return DPAD_INPUTS[name]
    if name in NAME_TO_CODE:
        return NAME_TO_CODE[name]
    if name.startswith("CODE_"):
        try:
            return int(name[5:])
        except ValueError:
            return None
    return None


def output_allowed(name):
    """Can the engine emit this? (keyboard keys, mouse buttons, wheel)"""
    if name in WHEEL_INPUTS:
        return True
    c = input_code(name)
    if c is None or c < 0:
        return False
    return (1 <= c < 0x100 or ecodes.BTN_LEFT <= c <= ecodes.BTN_TASK
            or 0x160 <= c < 0x220 or 0x230 <= c < 0x2c0)


def friendly(name):
    if name in FRIENDLY:
        return FRIENDLY[name]
    if name.startswith("KEY_"):
        return name[4:].replace("_", " ").title()
    return name


def input_label(name):
    f = friendly(name)
    return f"{f}  [{name}]" if f != name else name


MOUSE_NAMES = ["BTN_LEFT", "BTN_RIGHT", "BTN_MIDDLE", "BTN_SIDE", "BTN_EXTRA",
               "BTN_FORWARD", "BTN_BACK", "BTN_TASK", *WHEEL_INPUTS]


def all_input_names(outputs=False):
    """Selectable names: mouse first, then keys, then everything else."""
    keys = sorted(n for n, c in NAME_TO_CODE.items()
                  if n.startswith("KEY_") and c < 0x2ff and CODE_TO_NAME.get(c) == n)
    if outputs:
        return [n for n in MOUSE_NAMES + keys if output_allowed(n)]
    pad = [n for n in FRIENDLY if n.startswith("BTN_") and n not in MOUSE_NAMES]
    others = sorted(n for n, c in NAME_TO_CODE.items()
                    if n.startswith("BTN_") and n not in MOUSE_NAMES and n not in pad
                    and CODE_TO_NAME.get(c) == n)
    return MOUSE_NAMES + list(DPAD_INPUTS) + pad + keys + others


# Text typing (US layout).
_SHIFTED = {
    '!': '1', '@': '2', '#': '3', '$': '4', '%': '5', '^': '6', '&': '7',
    '*': '8', '(': '9', ')': '0', '_': 'MINUS', '+': 'EQUAL', '{': 'LEFTBRACE',
    '}': 'RIGHTBRACE', '|': 'BACKSLASH', ':': 'SEMICOLON', '"': 'APOSTROPHE',
    '<': 'COMMA', '>': 'DOT', '?': 'SLASH', '~': 'GRAVE',
}
_PLAIN = {
    ' ': 'SPACE', '\n': 'ENTER', '\t': 'TAB', '-': 'MINUS', '=': 'EQUAL',
    '[': 'LEFTBRACE', ']': 'RIGHTBRACE', '\\': 'BACKSLASH', ';': 'SEMICOLON',
    "'": 'APOSTROPHE', ',': 'COMMA', '.': 'DOT', '/': 'SLASH', '`': 'GRAVE',
}


def char_to_key(ch):
    """(code, needs_shift) for a character on a US layout, or None."""
    if ch.isascii() and ch.isalpha():
        return NAME_TO_CODE["KEY_" + ch.upper()], ch.isupper()
    if ch.isascii() and ch.isdigit():
        return NAME_TO_CODE["KEY_" + ch], False
    if ch in _PLAIN:
        return NAME_TO_CODE["KEY_" + _PLAIN[ch]], False
    if ch in _SHIFTED:
        return NAME_TO_CODE["KEY_" + _SHIFTED[ch]], True
    return None


# =========================================================
# Devices
# =========================================================

def device_spec(dev):
    return {"name": dev.name, "vendor": dev.info.vendor, "product": dev.info.product}


def spec_key(spec):
    return (spec["name"], spec.get("vendor", 0), spec.get("product", 0))


def spec_matches(spec, dev):
    if spec.get("name") != dev.name:
        return False
    if spec.get("vendor") and spec["vendor"] != dev.info.vendor:
        return False
    if spec.get("product") and spec["product"] != dev.info.product:
        return False
    return True


def describe_device(dev):
    caps = dev.capabilities()
    keys = set(caps.get(ecodes.EV_KEY, []))
    absax = set(caps.get(ecodes.EV_ABS, []))
    absax = {a[0] if isinstance(a, tuple) else a for a in absax}
    kinds = []
    if ecodes.BTN_SOUTH in keys or ecodes.BTN_TRIGGER in keys:
        kinds.append("controller")
    if ecodes.ABS_MT_POSITION_X in absax or ecodes.BTN_TOOL_FINGER in keys:
        kinds.append("touchpad")
    if ecodes.BTN_TOOL_PEN in keys:
        kinds.append("tablet")
    if ecodes.EV_REL in caps or (ecodes.BTN_LEFT in keys and "touchpad" not in kinds):
        kinds.append("mouse")
    if ecodes.KEY_A in keys or ecodes.KEY_ENTER in keys:
        kinds.append("keyboard")
    elif keys - set(range(0x100, 0x300)):
        kinds.append("keys")
    if not kinds:
        kinds.append("sensor" if absax else "switch" if ecodes.EV_SW in caps else "other")
    return kinds


def scan_devices():
    """All InputDevices except all_bind's own outputs."""
    out = []
    for path in list_devices():
        try:
            dev = InputDevice(path)
        except OSError:
            continue
        if is_own_device(dev.name):
            dev.close()
            continue
        out.append(dev)
    return out


# =========================================================
# Config (JSON) and compiler to all_bind.conf
# =========================================================

DEFAULT_MOVEMENT = {
    "invert_x": False,
    "invert_y": False,
    "swap_xy": False,
    "sens_x": 1.0,
    "sens_y": 1.0,
    "invert_wheel": False,
    "invert_hwheel": False,
}


def new_profile():
    return {"movement": dict(DEFAULT_MOVEMENT), "bindings": []}


def default_config():
    return {
        "version": 1,
        "devices": [],
        "active_profile": "Default",
        "keep_running_on_close": True,
        "start_engine_on_open": True,
        "profiles": {"Default": new_profile()},
    }


def normalize(cfg):
    base = default_config()
    for k, v in base.items():
        if k != "profiles":
            cfg.setdefault(k, v)
    profiles = cfg.setdefault("profiles", {})
    if not profiles:
        profiles["Default"] = new_profile()
    for prof in profiles.values():
        mv = prof.setdefault("movement", {})
        for k, v in DEFAULT_MOVEMENT.items():
            mv.setdefault(k, v)
        prof.setdefault("bindings", [])
    if cfg.get("active_profile") not in profiles:
        cfg["active_profile"] = next(iter(profiles))
    return cfg


MIGRATED = None     # set to the old path when a config was moved this run


def load_config():
    global MIGRATED
    os.makedirs(BASE_DIR, exist_ok=True)
    if not os.path.exists(CONFIG_PATH) and os.path.exists(OLD_CONFIG_PATH):
        # One-time move from the script folder to ~/.config/all_bind.
        with open(OLD_CONFIG_PATH) as f:
            cfg = normalize(json.load(f))
        save_config(cfg)
        os.replace(OLD_CONFIG_PATH, OLD_CONFIG_PATH + ".bak")
        old_conf = os.path.join(SCRIPT_DIR, "all_bind.conf")
        if os.path.exists(old_conf):
            os.remove(old_conf)
        MIGRATED = OLD_CONFIG_PATH
        return cfg
    try:
        with open(CONFIG_PATH) as f:
            return normalize(json.load(f))
    except FileNotFoundError:
        cfg = default_config()
        save_config(cfg)
        return cfg


def save_config(cfg):
    """Write config.json and the compiled all_bind.conf (the engine picks it up)."""
    os.makedirs(BASE_DIR, exist_ok=True)
    _atomic_write(CONFIG_PATH, json.dumps(cfg, indent=2) + "\n")
    _atomic_write(CONF_PATH, compile_conf(cfg))


def _atomic_write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o644)    # a root service refuses group/world-writable configs
    os.replace(tmp, path)


def _one_line(s):
    return " ".join(str(s).split("\n")).strip()


def _num(v, default=0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def compile_conf(cfg):
    names = list(cfg["profiles"])
    index = {n: i for i, n in enumerate(names)}
    lines = ["# Generated by all_bind_gui.py from config.json - edit there.", ""]

    for d in cfg.get("devices", []):
        lines.append(f'device {int(d.get("vendor", 0))} {int(d.get("product", 0))} {_one_line(d["name"])}')
    lines.append(f'active {index.get(cfg.get("active_profile"), 0)}')

    for name in names:
        prof = cfg["profiles"][name]
        mv = prof["movement"]
        lines += ["", f"profile {_one_line(name)}"]
        lines.append("move {:d} {:d} {:d} {:g} {:g} {:d} {:d}".format(
            bool(mv["invert_x"]), bool(mv["invert_y"]), bool(mv["swap_xy"]),
            _num(mv["sens_x"], 1.0), _num(mv["sens_y"], 1.0),
            bool(mv["invert_wheel"]), bool(mv["invert_hwheel"])))

        for b in prof["bindings"]:
            if not b.get("enabled", True):
                continue
            trig = input_code(b["trigger"]["input"])
            if trig is None:
                continue
            act = b["action"]
            t = act.get("type", "disable")
            lines.append(f'bind {trig} {"switch" if t == "switch_profile" else t}')
            if b["trigger"].get("device"):
                lines.append(f'  dev {_one_line(b["trigger"]["device"])}')

            if t in ("remap", "turbo"):
                for k in act.get("keys", []):
                    c = input_code(k)
                    if c is not None and output_allowed(k):
                        lines.append(f"  out {c}")
            if t == "turbo":
                lines.append(f'  opt mode {act.get("mode", "hold")}')
                lines.append(f'  opt cps {_num(act.get("cps"), 10):g}')
                lines.append(f'  opt press_ms {_num(act.get("press_ms")):g}')
                lines.append(f'  opt jitter {_num(act.get("jitter")):g}')
                lines.append(f'  opt burst {int(_num(act.get("burst")))}')
            elif t == "macro":
                lines.append(f'  opt mode {act.get("mode", "once")}')
                lines.append(f'  opt count {int(_num(act.get("count"), 1))}')
                lines.append(f'  opt loop_gap_ms {_num(act.get("loop_gap_ms")):g}')
                lines += ["  " + s for s in compile_steps(act.get("steps", []))]
            elif t == "command":
                lines.append(f'  cmd {_one_line(act.get("command", ""))}')
            elif t in ("layer", "switch_profile"):
                lines.append(f'  opt profile {index.get(act.get("profile"), -1)}')
                if t == "layer":
                    lines.append(f'  opt mode {act.get("mode", "hold")}')

    return "\n".join(lines) + "\n"


def compile_steps(steps):
    out = []
    for s in steps:
        t = s.get("type")
        if t in ("tap", "down", "up"):
            c = input_code(s.get("key", ""))
            if c is None or not output_allowed(s.get("key", "")):
                continue
            if t == "tap":
                out.append(f'step tap {c} {_num(s.get("hold_ms")):g}')
            else:
                out.append(f"step {t} {c}")
        elif t == "delay":
            out.append(f'step delay {_num(s.get("ms")):g} {_num(s.get("random_ms")):g}')
        elif t == "move":
            out.append(f'step move {int(_num(s.get("dx")))} {int(_num(s.get("dy")))} '
                       f'{_num(s.get("duration_ms")):g}')
        elif t == "wheel":
            out.append(f'step wheel {int(_num(s.get("amount"), 1))} {int(bool(s.get("horizontal")))}')
        elif t == "command":
            out.append(f'step cmd {_one_line(s.get("command", ""))}')
        elif t == "text":
            # The engine has no keymap; expand to key taps here (US layout).
            delay = _num(s.get("delay_ms"), 2)
            shift = NAME_TO_CODE["KEY_LEFTSHIFT"]
            for ch in s.get("text", ""):
                m = char_to_key(ch)
                if m is None:
                    continue
                code, shifted = m
                if shifted:
                    out.append(f"step down {shift}")
                out.append(f"step tap {code} 0")
                if shifted:
                    out.append(f"step up {shift}")
                if delay:
                    out.append(f"step delay {delay:g} 0")
    return out


# =========================================================
# Engine binary / processes
# =========================================================

def engine_needs_build():
    if not DEV_MODE:
        return False
    try:
        return os.path.getmtime(ENGINE_BIN) < os.path.getmtime(ENGINE_SRC)
    except OSError:
        return True


def build_engine():
    """Compile all_bind.c (make, or gcc directly). Returns (ok, output)."""
    if not DEV_MODE:
        return False, "installed from a package - update the package to get a new engine"
    if os.path.exists(os.path.join(SCRIPT_DIR, "Makefile")) and shutil.which("make"):
        cmd = ["make", "-C", SCRIPT_DIR, "all_bind"]
    else:
        cmd = ["gcc", "-O2", "-Wall", "-Wextra", "-pthread", ENGINE_SRC, "-o", ENGINE_BIN]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, cwd=SCRIPT_DIR)
    except OSError as e:
        return False, str(e)
    return p.returncode == 0, (p.stdout + p.stderr).strip()


CLIENT_ARGS = (b"next", b"prev", b"cycle", b"reload", b"--cli", b"--gui", b"--help", b"-h")


def find_engines():
    """Pids of every running all_bind engine (zombies and `all_bind next` clients excluded)."""
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/comm") as f:
                if f.read().strip() != ENGINE_NAME:
                    continue
            with open(f"/proc/{entry}/stat") as f:
                if f.read().rsplit(")", 1)[1].split()[0] == "Z":
                    continue
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                args = f.read().split(b"\0")
            if len(args) > 1 and args[1] in CLIENT_ARGS:
                continue
        except (OSError, IndexError):
            continue
        found.append(int(entry))
    return found


def engine_is_mine(pid):
    try:
        return os.stat(f"/proc/{pid}").st_uid == os.getuid()
    except OSError:
        return False


def engine_unit(pid):
    """The systemd system unit running this engine (all_bind@NAME.service), if any."""
    try:
        with open(f"/proc/{pid}/cgroup") as f:
            data = f.read()
    except OSError:
        return None
    for part in data.replace("\n", "/").split("/"):
        if part.startswith("all_bind@") and part.endswith(".service"):
            return part
    return None


def service_name():
    return f"all_bind@{os.environ.get('USER') or os.getlogin()}.service"


def run_privileged(cmd):
    """Run a command as root via sudo (terminal) or pkexec (GUI). Returns (ok, message)."""
    if sys.stdin is not None and sys.stdin.isatty() and shutil.which("sudo"):
        pre = ["sudo"]
    elif shutil.which("pkexec"):
        pre = ["pkexec"]
    else:
        return False, "needs sudo or pkexec: " + " ".join(cmd)
    p = subprocess.run(pre + cmd)
    return p.returncode == 0, " ".join(pre + cmd)


def has_device_access():
    """Can we read input devices and create virtual ones without root (input group)?"""
    import glob
    if not os.access("/dev/uinput", os.R_OK | os.W_OK):
        return False
    return any(os.access(p, os.R_OK) for p in glob.glob("/dev/input/event*"))


def send_engine_command(cmd):
    """Drop a command file the running engine picks up (works even if it runs as root)."""
    os.makedirs(BASE_DIR, exist_ok=True)
    tmp = os.path.join(BASE_DIR, f".cmd-{os.getpid()}.tmp")
    with open(tmp, "w") as f:
        f.write(cmd + "\n")
    os.replace(tmp, os.path.join(BASE_DIR, f".command-{os.getpid()}"))


def stop_engines(pids):
    """Stop the given engines: signal our own, ask systemd for a root service,
    and use a command file for an engine that asked for the password itself."""
    msgs = []
    waiting = []
    quit_sent = False
    for pid in pids:
        unit = engine_unit(pid)
        if engine_is_mine(pid):
            try:
                os.kill(pid, signal.SIGTERM)
                waiting.append(pid)
            except OSError as e:
                msgs.append(f"pid {pid}: {e}")
        elif unit:
            ok, how = run_privileged(["systemctl", "stop", unit])
            msgs.append(f"{'stopped' if ok else 'could not stop'} {unit} ({how})")
        else:
            if not quit_sent:
                send_engine_command("quit")
                quit_sent = True
            waiting.append(pid)
    for _ in range(60):
        if not any(p in find_engines() for p in waiting):
            break
        time.sleep(0.05)
    still = [p for p in waiting if p in find_engines()]
    if still:
        msgs.append("still running: " + ", ".join(f"pid {p}" for p in still))
    return msgs


def start_engine_detached():
    """Start a headless engine for the terminal (no event stream).

    Without access to the input devices it runs as root via sudo (asks for the
    password here); the engine still uses this user's config."""
    if engine_needs_build():
        ok, out = build_engine()
        if not ok:
            raise RuntimeError("build failed:\n" + out)
    cmd = [ENGINE_BIN, CONF_PATH]
    if not has_device_access() and sys.stdin is not None and sys.stdin.isatty() \
            and shutil.which("sudo"):
        print("no access to input devices (not in the input group?) - asking for your password")
        if subprocess.run(["sudo", "-v"]).returncode != 0:
            raise RuntimeError("sudo failed - or add yourself to the input group: "
                               "sudo usermod -aG input $USER")
        cmd = ["sudo", "-n", ENGINE_BIN, "--user", os.environ.get("USER") or os.getlogin(), CONF_PATH]
    # (no terminal: the engine asks through a polkit dialog by itself)
    return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)


# =========================================================
# Device watcher (shared by "Find device" in the GUI and the CLI)
# =========================================================

def watch_devices(on_hit, stop_flag, motion_flag, keys_only=False):
    """
    Watch every non-grabbed device and call on_hit(dict) when one does
    something: a press, scroll, touch, stick/d-pad movement, a switch, or
    pointer movement while motion_flag is set. Motion sensors are ignored,
    they never stop sending. keys_only: only presses / wheel / d-pad, with
    "input" set to the bindable name.
    """
    sel = selectors.DefaultSelector()
    devs = []
    info = {}
    for d in scan_devices():
        try:
            if ecodes.INPUT_PROP_ACCELEROMETER in d.input_props():
                d.close()
                continue
            axes = {}
            for code, ai in d.capabilities(absinfo=True).get(ecodes.EV_ABS, []):
                if code < ecodes.ABS_MT_SLOT and ai.max > ai.min:
                    axes[code] = (ai.min, ai.max, ai.value)
            sel.register(d.fd, selectors.EVENT_READ, d)
        except OSError:
            d.close()
            continue
        devs.append(d)
        info[d.fd] = {"axes": axes, "moved": 0, "kinds": describe_device(d)}

    try:
        while not stop_flag.is_set():
            for key, _ in sel.select(0.2):
                d = key.data
                st = info[key.fd]
                try:
                    events = list(d.read())
                except BlockingIOError:
                    continue
                except OSError:
                    sel.unregister(key.fd)
                    continue
                for ev in events:
                    name, what = _describe_event(ev, st, motion_flag, keys_only)
                    if what:
                        on_hit({"spec": device_spec(d), "path": d.path, "kinds": st["kinds"],
                                "what": what, "input": name})
    finally:
        for d in devs:
            try:
                d.close()
            except OSError:
                pass


def _describe_event(ev, st, motion_flag, keys_only):
    """(bindable name or None, description or None) for one event."""
    if ev.type == ecodes.EV_KEY and ev.value == 1:
        name = code_name(ev.code)
        return name, f"{input_label(name)} pressed"
    if ev.type == ecodes.EV_REL:
        if ev.code in (ecodes.REL_WHEEL, ecodes.REL_HWHEEL) and ev.value:
            if ev.code == ecodes.REL_WHEEL:
                name = "WHEEL_UP" if ev.value > 0 else "WHEEL_DOWN"
            else:
                name = "WHEEL_RIGHT" if ev.value > 0 else "WHEEL_LEFT"
            return name, input_label(name)
        if not keys_only and ev.code in (ecodes.REL_X, ecodes.REL_Y) and motion_flag.is_set():
            st["moved"] += abs(ev.value)
            if st["moved"] > 40:        # ignore tiny jitter
                st["moved"] = 0
                return None, "moved"
        return None, None
    if ev.type == ecodes.EV_ABS:
        if ev.code in (ecodes.ABS_HAT0X, ecodes.ABS_HAT0Y) and ev.value:
            names = ("DPAD_LEFT", "DPAD_RIGHT") if ev.code == ecodes.ABS_HAT0X else ("DPAD_UP", "DPAD_DOWN")
            name = names[ev.value > 0]
            return name, input_label(name)
        ax = st["axes"].get(ev.code)
        if not keys_only and ax and "controller" in st["kinds"]:
            lo, hi, rest = ax
            if abs(ev.value - rest) > 0.35 * (hi - lo):
                n = ecodes.ABS.get(ev.code, f"ABS_{ev.code}")
                return None, f"{n if isinstance(n, str) else n[0]} moved"
        return None, None
    if ev.type == ecodes.EV_SW and not keys_only:
        n = ecodes.SW.get(ev.code, f"SW_{ev.code}")
        n = n if isinstance(n, str) else n[0]
        return None, f"{n} {'on' if ev.value else 'off'}"
    return None, None


# =========================================================
# Descriptions of steps and actions (GUI and CLI)
# =========================================================

STEP_TYPES = [
    ("tap", "Tap key"),
    ("down", "Press (hold down)"),
    ("up", "Release"),
    ("delay", "Delay"),
    ("move", "Move mouse"),
    ("wheel", "Scroll"),
    ("text", "Type text"),
    ("command", "Run command"),
]

STEP_DEFAULTS = {
    "tap": {"key": "BTN_LEFT", "hold_ms": 0}, "down": {"key": "BTN_LEFT"},
    "up": {"key": "BTN_LEFT"}, "delay": {"ms": 10, "random_ms": 0},
    "move": {"dx": 0, "dy": 10, "duration_ms": 0}, "wheel": {"amount": 1, "horizontal": False},
    "text": {"text": "", "delay_ms": 2}, "command": {"command": ""},
}


def step_text(s):
    t = s.get("type")
    if t == "tap":
        hold = s.get("hold_ms", 0)
        return f"Tap {input_label(s.get('key', '?'))}" + (f"  (hold {hold:g} ms)" if hold else "")
    if t == "down":
        return f"↓ Press {input_label(s.get('key', '?'))}"
    if t == "up":
        return f"↑ Release {input_label(s.get('key', '?'))}"
    if t == "delay":
        r = s.get("random_ms", 0)
        return f"⏱ Wait {s.get('ms', 0):g} ms" + (f" + random 0–{r:g} ms" if r else "")
    if t == "move":
        d = s.get("duration_ms", 0)
        return f"Move mouse by ({s.get('dx', 0)}, {s.get('dy', 0)})" + (f" over {d:g} ms" if d else " instantly")
    if t == "wheel":
        return f"Scroll {'horizontal' if s.get('horizontal') else 'vertical'} {s.get('amount', 1):+d}"
    if t == "text":
        txt = s.get("text", "").replace("\n", "⏎")
        return f'Type "{txt[:40]}{"…" if len(txt) > 40 else ""}"'
    if t == "command":
        return f"$ {s.get('command', '')}"
    return str(s)


ACTION_TYPES = [
    ("remap", "Remap to key / button / combo"),
    ("turbo", "Turbo / rapid fire"),
    ("macro", "Macro"),
    ("command", "Run command"),
    ("layer", "Shift to profile (layer)"),
    ("switch_profile", "Switch profile"),
    ("disable", "Disable"),
]


def action_summary(a):
    t = a.get("type")
    keys = " + ".join(friendly(k) for k in a.get("keys", []))
    if t == "remap":
        return f"→ {keys or '(nothing)'}"
    if t == "turbo":
        extra = []
        if a.get("press_ms"):
            extra.append(f"hold {a['press_ms']:g} ms")
        if a.get("jitter"):
            extra.append(f"±{a['jitter']:g}%")
        if a.get("burst"):
            extra.append(f"burst {a['burst']}")
        mode = "while held" if a.get("mode", "hold") == "hold" else "toggle"
        return f"Turbo {keys} @ {a.get('cps', 10):g}/s ({mode}{', ' + ', '.join(extra) if extra else ''})"
    if t == "macro":
        mode = {"once": "once", "repeat": f"×{a.get('count', 1)}", "while_held": "loop while held",
                "toggle": "loop toggle"}.get(a.get("mode", "once"), "")
        return f"Macro: {len(a.get('steps', []))} steps ({mode})"
    if t == "command":
        return f"$ {a.get('command', '')}"
    if t == "layer":
        return f"Shift to “{a.get('profile')}” ({'while held' if a.get('mode', 'hold') == 'hold' else 'toggle'})"
    if t == "switch_profile":
        return f"Switch to “{a.get('profile')}”"
    return "Disabled"


# =========================================================
# CLI  (all_bind --cli ...)
# =========================================================

CLI_HELP = """\
all_bind --cli COMMAND ...    (changes apply to the running engine immediately)

Devices
  devices                       list every input device (* = grabbed)
  grab N|NAME ... / ungrab ...  grab devices by number or name
  find [--motion]               use a device and see which one it is
  capture                       press something and see its name
  keys [FILTER]                 list key / button names

Profiles
  profiles                      list profiles (> = active)
  profile use|new|copy|delete NAME
  profile rename OLD NEW
  next | prev                   switch the running engine's profile

Bindings  (add profile=NAME to work on another profile)
  binds                         list bindings in the active profile
  bind TRIGGER ACTION ... [from=DEVICE]
      remap KEY [KEY ...]       press these instead (several = combo)
      turbo KEY [KEY ...] [rate=20] [press=0] [jitter=0] [burst=0] [mode=hold|toggle]
      macro "STEPS" [mode=once|repeat|while_held|toggle] [count=1] [gap=0]
            STEPS separated by ';':  tap KEY [HOLD_MS] | press KEY | release KEY |
            delay MS [RANDOM_MS] | move DX DY [OVER_MS] | scroll N [h] |
            type TEXT | run COMMAND
      run COMMAND ...           run a shell command
      layer PROFILE [mode=hold|toggle]
      switch PROFILE
      disable
  unbind N | on N | off N       remove / enable / disable binding N
  move [invert-x=yes] [invert-y=no] [swap=no] [sens=1] [sens-x=] [sens-y=]
       [invert-wheel=] [invert-hwheel=]          movement settings (no args: show)

Engine
  status                        engine, profile and grabbed devices
  start | stop | restart
  service                       how to run it as a root system service

Key names: KEY_A or just a, BTN_SIDE, ctrl, shift, alt, super, mouse4, mouse5,
left-click, WHEEL_UP, DPAD_LEFT ... (see `keys`).
Examples:
  all_bind --cli bind mouse4 turbo left-click rate=100
  all_bind --cli bind mouse5 remap ctrl c
  all_bind --cli bind f13 macro "tap a; delay 50; type hello; run notify-send done"
"""


class CliError(Exception):
    pass


_ALIASES = {
    "LEFT_CLICK": "BTN_LEFT", "RIGHT_CLICK": "BTN_RIGHT", "MIDDLE_CLICK": "BTN_MIDDLE",
    "LMB": "BTN_LEFT", "RMB": "BTN_RIGHT", "MMB": "BTN_MIDDLE",
    "MOUSE1": "BTN_LEFT", "MOUSE2": "BTN_RIGHT", "MOUSE3": "BTN_MIDDLE",
    "MOUSE4": "BTN_SIDE", "MOUSE5": "BTN_EXTRA",
    "CTRL": "KEY_LEFTCTRL", "SHIFT": "KEY_LEFTSHIFT", "ALT": "KEY_LEFTALT",
    "SUPER": "KEY_LEFTMETA", "WIN": "KEY_LEFTMETA", "META": "KEY_LEFTMETA",
    "ESC": "KEY_ESC", "DEL": "KEY_DELETE", "RETURN": "KEY_ENTER",
}


def resolve_input(s, outputs=False):
    u = s.strip().upper().replace(" ", "_").replace("-", "_")
    for cand in (_ALIASES.get(u, u), "KEY_" + u, "BTN_" + u):
        if input_code(cand) is not None:
            if outputs and not output_allowed(cand):
                raise CliError(f"{cand} can't be an output (only keyboard keys, mouse buttons, wheel)")
            return cand
    raise CliError(f"unknown key: {s}  (see: all_bind --cli keys)")


def _split_opts(args):
    """Split ['a', 'rate=5', 'b'] into (['a', 'b'], {'rate': '5'})."""
    pos, opts = [], {}
    for a in args:
        k, sep, v = a.partition("=")
        if sep and k and k.replace("-", "").replace("_", "").isalpha():
            opts[k.lower().replace("_", "-")] = v
        else:
            pos.append(a)
    return pos, opts


def _yes(v):
    if v.lower() in ("1", "yes", "y", "true", "on"):
        return True
    if v.lower() in ("0", "no", "n", "false", "off"):
        return False
    raise CliError(f"expected yes/no, got {v!r}")


def _cli_num(v, name, cast=float):
    try:
        return cast(v)
    except ValueError:
        raise CliError(f"{name} must be a number, got {v!r}")


def _parse_steps(text):
    steps = []
    for raw in text.split(";"):
        parts = raw.split()
        if not parts:
            continue
        cmd, rest = parts[0].lower(), parts[1:]
        try:
            if cmd == "tap":
                steps.append({"type": "tap", "key": resolve_input(rest[0], True),
                              "hold_ms": _cli_num(rest[1], "hold") if len(rest) > 1 else 0})
            elif cmd in ("press", "down"):
                steps.append({"type": "down", "key": resolve_input(rest[0], True)})
            elif cmd in ("release", "up"):
                steps.append({"type": "up", "key": resolve_input(rest[0], True)})
            elif cmd in ("delay", "wait"):
                steps.append({"type": "delay", "ms": _cli_num(rest[0], "delay"),
                              "random_ms": _cli_num(rest[1], "random") if len(rest) > 1 else 0})
            elif cmd == "move":
                steps.append({"type": "move", "dx": _cli_num(rest[0], "dx", int), "dy": _cli_num(rest[1], "dy", int),
                              "duration_ms": _cli_num(rest[2], "over") if len(rest) > 2 else 0})
            elif cmd == "scroll":
                steps.append({"type": "wheel", "amount": _cli_num(rest[0], "scroll", int),
                              "horizontal": len(rest) > 1 and rest[1].lower().startswith("h")})
            elif cmd == "type":
                steps.append({"type": "text", "text": raw.strip()[len(parts[0]):].strip(), "delay_ms": 2})
            elif cmd == "run":
                steps.append({"type": "command", "command": raw.strip()[len(parts[0]):].strip()})
            else:
                raise CliError(f"unknown macro step: {raw.strip()!r}")
        except IndexError:
            raise CliError(f"macro step needs more arguments: {raw.strip()!r}")
    if not steps:
        raise CliError("macro has no steps")
    return steps


def _profile_arg(cfg, opts):
    name = opts.pop("profile", None) or cfg["active_profile"]
    if name not in cfg["profiles"]:
        raise CliError(f"no such profile: {name}")
    return name


def _device_list():
    """[(spec, kinds, path)] for every device, de-duplicated, sorted like the GUI."""
    out, seen = [], set()
    for d in sorted(scan_devices(), key=lambda d: (d.name.lower(), d.path)):
        spec = device_spec(d)
        if spec_key(spec) not in seen:
            seen.add(spec_key(spec))
            out.append((spec, describe_device(d), d.path))
        d.close()
    return out


def _pick_devices(args):
    devs = _device_list()
    picked = []
    for a in args:
        if a.isdigit() and 1 <= int(a) <= len(devs):
            picked.append(devs[int(a) - 1][0])
            continue
        hits = [s for s, _, _ in devs if s["name"] == a] or \
               [s for s, _, _ in devs if a.lower() in s["name"].lower()]
        if len(hits) != 1:
            raise CliError(f"{'no' if not hits else 'several'} devices match {a!r} - use the number from `devices`")
        picked.append(hits[0])
    return picked


def _engine_line():
    pids = find_engines()
    if not pids:
        return "engine: stopped"
    parts = []
    for p in pids:
        unit = engine_unit(p)
        parts.append(f"pid {p}" + (f", system service {unit}" if unit and not engine_is_mine(p) else ""))
    return "engine: running (" + "; ".join(parts) + ")"


def _print_binds(cfg, prof):
    bs = cfg["profiles"][prof]["bindings"]
    print(f"profile {prof}: {len(bs)} binding{'s' if len(bs) != 1 else ''}")
    for i, b in enumerate(bs, 1):
        state = "on " if b.get("enabled", True) else "off"
        dev = f"  [from {b['trigger']['device']}]" if b["trigger"].get("device") else ""
        print(f"  {i:>2}. {state}  {input_label(b['trigger']['input'])}{dev}  ->  {action_summary(b['action'])}")
        if b["action"].get("type") == "macro":
            for s in b["action"].get("steps", []):
                print(f"            {step_text(s)}")


def cli_main(argv):
    try:
        return _cli(argv)
    except CliError as e:
        print(f"all_bind: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def _cli(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(CLI_HELP)
        if not argv:
            print(_engine_line())
        return 0

    cmd, args = argv[0], argv[1:]
    cfg = load_config()
    if MIGRATED:
        print(f"(moved your config from {MIGRATED} to {CONFIG_PATH})")
    save = False

    if cmd == "status":
        print(_engine_line())
        print(f"config: {CONFIG_PATH}")
        print(f"active profile: {cfg['active_profile']}   ({len(cfg['profiles'])} profiles)")
        print("grabbed devices:" if cfg["devices"] else "grabbed devices: none")
        for s in cfg["devices"]:
            print(f"  {s['name']}  ({s.get('vendor', 0):04x}:{s.get('product', 0):04x})")

    elif cmd == "devices":
        grabbed = {spec_key(s) for s in cfg["devices"]}
        for i, (spec, kinds, path) in enumerate(_device_list(), 1):
            mark = "*" if spec_key(spec) in grabbed else " "
            print(f"{i:>3} {mark} {spec['name']}   [{', '.join(kinds)}]  "
                  f"{spec['vendor']:04x}:{spec['product']:04x}  {os.path.basename(path)}")
        print("\n* = grabbed.  grab with: all_bind --cli grab NUMBER")

    elif cmd in ("grab", "ungrab"):
        if not args:
            raise CliError(f"usage: {cmd} NUMBER|NAME ...")
        for spec in _pick_devices(args):
            cfg["devices"] = [s for s in cfg["devices"] if spec_key(s) != spec_key(spec)]
            if cmd == "grab":
                cfg["devices"].append(spec)
            print(f"{cmd}bed {spec['name']}" if cmd == "grab" else f"released {spec['name']}")
        save = True

    elif cmd in ("find", "capture"):
        motion = threading.Event()
        if "--motion" in args:
            motion.set()
        stop = threading.Event()
        result = {}
        keys_only = cmd == "capture"

        def hit(h):
            if keys_only and not h["input"]:
                return
            result.update(h)
            stop.set()

        if cfg["devices"] and find_engines():
            print("note: devices grabbed by the running engine can't be seen here "
                  "(all_bind --cli stop first to include them)")
        print("press a key / button, scroll" + ("" if keys_only else ", touch, or move a stick")
              + " ...  (Ctrl+C to cancel)")
        time.sleep(0.3)
        t = threading.Thread(target=watch_devices, args=(hit, stop, motion, keys_only), daemon=True)
        t.start()
        while not stop.is_set():
            time.sleep(0.05)
        if keys_only:
            print(f"{result['input']}   ({friendly(result['input'])}, from {result['spec']['name']})")
        else:
            spec = result["spec"]
            print(f"device: {spec['name']}\n  input: {result['what']}\n"
                  f"  id:    {spec['vendor']:04x}:{spec['product']:04x}\n  type:  {', '.join(result['kinds'])}\n"
                  f"  node:  {result['path']}\n  grabbed: "
                  + ("yes" if any(spec_key(s) == spec_key(spec) for s in cfg["devices"]) else "no"))

    elif cmd == "keys":
        flt = args[0].lower() if args else ""
        for n in all_input_names():
            label = input_label(n)
            if flt in label.lower():
                print(("  " if output_allowed(n) else "t ") + label)
        print("\nt = trigger only (can't be sent as an output)")

    elif cmd == "profiles":
        for n, p in cfg["profiles"].items():
            mark = ">" if n == cfg["active_profile"] else " "
            print(f"{mark} {n}   ({len(p['bindings'])} bindings)")

    elif cmd == "profile":
        if not args:
            raise CliError("usage: profile use|new|copy|delete NAME  |  profile rename OLD NEW")
        sub, rest = args[0], args[1:]
        name = " ".join(rest) if sub != "rename" else None
        if sub == "use":
            if name not in cfg["profiles"]:
                raise CliError(f"no such profile: {name}")
            cfg["active_profile"] = name
        elif sub in ("new", "copy"):
            if not name or name in cfg["profiles"]:
                raise CliError("give a new, unused profile name")
            src = cfg["profiles"][cfg["active_profile"]]
            cfg["profiles"][name] = copy.deepcopy(src) if sub == "copy" else new_profile()
            cfg["active_profile"] = name
        elif sub == "delete":
            if name not in cfg["profiles"]:
                raise CliError(f"no such profile: {name}")
            if len(cfg["profiles"]) == 1:
                raise CliError("can't delete the last profile")
            del cfg["profiles"][name]
            if cfg["active_profile"] == name:
                cfg["active_profile"] = next(iter(cfg["profiles"]))
        elif sub == "rename":
            if len(rest) != 2 or rest[0] not in cfg["profiles"] or rest[1] in cfg["profiles"]:
                raise CliError("usage: profile rename OLD NEW  (quote names with spaces)")
            old, new = rest
            cfg["profiles"] = {(new if k == old else k): v for k, v in cfg["profiles"].items()}
            for p in cfg["profiles"].values():
                for b in p["bindings"]:
                    if b["action"].get("profile") == old:
                        b["action"]["profile"] = new
            if cfg["active_profile"] == old:
                cfg["active_profile"] = new
        else:
            raise CliError(f"unknown: profile {sub}")
        print(f"active profile: {cfg['active_profile']}")
        save = True

    elif cmd in ("next", "prev"):
        return subprocess.call([ENGINE_BIN, cmd])

    elif cmd == "binds":
        _, opts = _split_opts(args)
        _print_binds(cfg, _profile_arg(cfg, opts))

    elif cmd == "bind":
        pos, opts = _split_opts(args)
        prof = _profile_arg(cfg, opts)
        if len(pos) < 2:
            raise CliError("usage: bind TRIGGER ACTION ...   (see all_bind --cli help)")
        trigger, action, rest = resolve_input(pos[0]), pos[1].lower(), pos[2:]
        mode = opts.get("mode")
        if action == "remap":
            if not rest:
                raise CliError("remap needs at least one key")
            act = {"type": "remap", "keys": [resolve_input(k, True) for k in rest]}
        elif action == "turbo":
            if mode not in (None, "hold", "toggle"):
                raise CliError("turbo mode is hold or toggle")
            act = {"type": "turbo", "keys": [resolve_input(k, True) for k in rest] or ["BTN_LEFT"],
                   "cps": _cli_num(opts.get("rate", "20"), "rate"), "press_ms": _cli_num(opts.get("press", "0"), "press"),
                   "jitter": _cli_num(opts.get("jitter", "0"), "jitter", int),
                   "burst": _cli_num(opts.get("burst", "0"), "burst", int), "mode": mode or "hold"}
        elif action == "macro":
            if mode not in (None, "once", "repeat", "while_held", "while-held", "toggle"):
                raise CliError("macro mode is once, repeat, while_held or toggle")
            act = {"type": "macro", "steps": _parse_steps(" ".join(rest)),
                   "mode": (mode or "once").replace("-", "_"),
                   "count": _cli_num(opts.get("count", "1"), "count", int),
                   "loop_gap_ms": _cli_num(opts.get("gap", "0"), "gap")}
        elif action == "run":
            if not rest:
                raise CliError("run needs a command")
            act = {"type": "command", "command": " ".join(rest)}
        elif action in ("layer", "switch"):
            target = " ".join(rest)
            if target not in cfg["profiles"]:
                raise CliError(f"no such profile: {target!r}")
            act = {"type": "layer", "profile": target, "mode": mode or "hold"} if action == "layer" \
                else {"type": "switch_profile", "profile": target}
        elif action == "disable":
            act = {"type": "disable"}
        else:
            raise CliError(f"unknown action: {action}  (remap, turbo, macro, run, layer, switch, disable)")
        b = {"enabled": True, "trigger": {"input": trigger, "device": opts.get("from")}, "action": act}
        bs = cfg["profiles"][prof]["bindings"]
        dup = [i for i, o in enumerate(bs, 1) if o["trigger"]["input"] == trigger
               and (o["trigger"].get("device") or None) == (b["trigger"]["device"] or None)]
        bs.append(b)
        print(f"added #{len(bs)} in {prof}: {input_label(trigger)} -> {action_summary(act)}")
        if dup:
            print(f"note: #{dup[0]} uses the same trigger and comes first - `unbind {dup[0]}` to replace it")
        save = True

    elif cmd in ("unbind", "on", "off"):
        pos, opts = _split_opts(args)
        prof = _profile_arg(cfg, opts)
        bs = cfg["profiles"][prof]["bindings"]
        if len(pos) != 1 or not pos[0].isdigit() or not 1 <= int(pos[0]) <= len(bs):
            raise CliError(f"usage: {cmd} N   (N from `binds`, 1-{len(bs)})")
        i = int(pos[0]) - 1
        if cmd == "unbind":
            b = bs.pop(i)
            print(f"removed {input_label(b['trigger']['input'])} -> {action_summary(b['action'])}")
        else:
            bs[i]["enabled"] = cmd == "on"
            print(f"#{i + 1} {'enabled' if cmd == 'on' else 'disabled'}")
        save = True

    elif cmd == "move":
        pos, opts = _split_opts(args)
        prof = _profile_arg(cfg, opts)
        mv = cfg["profiles"][prof]["movement"]
        keys = {"invert-x": "invert_x", "invert-y": "invert_y", "swap": "swap_xy",
                "invert-wheel": "invert_wheel", "invert-hwheel": "invert_hwheel"}
        for k, v in opts.items():
            if k in keys:
                mv[keys[k]] = _yes(v)
            elif k == "sens":
                mv["sens_x"] = mv["sens_y"] = _cli_num(v, k)
            elif k in ("sens-x", "sens-y"):
                mv[k.replace("-", "_")] = _cli_num(v, k)
            else:
                raise CliError(f"unknown movement setting: {k}")
        save = bool(opts)
        yn = lambda b: "yes" if b else "no"
        print(f"profile {prof}: invert-x={yn(mv['invert_x'])} invert-y={yn(mv['invert_y'])} "
              f"swap={yn(mv['swap_xy'])} sens-x={mv['sens_x']:g} sens-y={mv['sens_y']:g} "
              f"invert-wheel={yn(mv['invert_wheel'])} invert-hwheel={yn(mv['invert_hwheel'])}")

    elif cmd == "start":
        if find_engines():
            print(_engine_line())
            return 0
        try:
            p = start_engine_detached()
        except RuntimeError as e:
            raise CliError(str(e))
        time.sleep(0.5)
        print(f"started (pid {p.pid})" if p.poll() is None else f"engine exited with code {p.returncode}")

    elif cmd == "stop":
        pids = find_engines()
        if not pids:
            print("not running")
        for m in stop_engines(pids):
            print(m)
        print(_engine_line())

    elif cmd == "restart":
        pids = find_engines()
        units = {engine_unit(p) for p in pids if not engine_is_mine(p)} - {None}
        if units:
            for u in units:
                ok, how = run_privileged(["systemctl", "restart", u])
                print(f"{'restarted' if ok else 'could not restart'} {u}")
        else:
            for m in stop_engines(pids):
                print(m)
            try:
                p = start_engine_detached()
            except RuntimeError as e:
                raise CliError(str(e))
            time.sleep(0.5)
            print(f"started (pid {p.pid})" if p.poll() is None else f"engine exited with code {p.returncode}")

    elif cmd == "service":
        unit = service_name()
        print(f"Run all_bind as root at boot, with your config ({CONFIG_PATH}):\n\n"
              f"  sudo systemctl enable --now {unit}     start now and at every boot\n"
              f"  sudo systemctl disable --now {unit}    turn it off again\n"
              f"  systemctl status {unit}\n\n"
              "Commands and notifications it triggers still run as you. Remove any\n"
              "`exec-once = all_bind` autostart line so only one engine runs.")

    else:
        raise CliError(f"unknown command: {cmd}  (all_bind --cli help)")

    if save:
        save_config(cfg)
    return 0


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--cli":
    sys.exit(cli_main(sys.argv[2:]))


# Everything below is the GUI.
from PySide6.QtCore import QObject, Qt, QTimer, Signal  # noqa: E402
from PySide6.QtGui import QAction, QColor, QFont  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QCompleter,
    QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
    QPushButton, QSpinBox, QSplitter, QStackedWidget, QTableWidget,
    QTableWidgetItem, QTabWidget, QToolButton, QVBoxLayout, QWidget,
)


# =========================================================
# Input hub: physical input events for capture / recording
# =========================================================

class InputHub(QObject):
    """
    Delivers physical presses from two sources:
      - the engine's event stream (devices the engine has grabbed)
      - a direct reader on every other device (only while someone listens)
    Grabbed devices deliver nothing to the direct reader, so no duplicates.
    """

    event = Signal(dict)    # {"input": name, "value": 0/1, "dev": name}

    def __init__(self):
        super().__init__()
        self.listeners = 0
        self.note = ""      # shown in capture dialogs
        self.thread = None
        self.stop_flag = threading.Event()

    def acquire(self):
        self.listeners += 1
        if self.listeners == 1:
            self.stop_flag = threading.Event()
            self.thread = threading.Thread(target=self._run, args=(self.stop_flag,), daemon=True)
            self.thread.start()

    def release(self):
        self.listeners = max(0, self.listeners - 1)
        if self.listeners == 0:
            self.stop_flag.set()

    def feed(self, name, value, dev):
        self.event.emit({"input": name, "value": value, "dev": dev})

    def _run(self, stop_flag):
        sel = selectors.DefaultSelector()
        devs = []
        for d in scan_devices():
            caps = d.capabilities()
            if ecodes.EV_KEY in caps or ecodes.EV_REL in caps or ecodes.EV_ABS in caps:
                sel.register(d.fd, selectors.EVENT_READ, d)
                devs.append(d)
            else:
                d.close()
        wheel = {ecodes.REL_WHEEL: ("WHEEL_UP", "WHEEL_DOWN"),
                 ecodes.REL_HWHEEL: ("WHEEL_RIGHT", "WHEEL_LEFT")}
        hat = {ecodes.ABS_HAT0X: ("DPAD_LEFT", "DPAD_RIGHT"),
               ecodes.ABS_HAT0Y: ("DPAD_UP", "DPAD_DOWN")}
        try:
            while not stop_flag.is_set():
                for key, _ in sel.select(0.2):
                    d = key.data
                    try:
                        for ev in d.read():
                            if ev.type == ecodes.EV_KEY and ev.value in (0, 1):
                                self.feed(code_name(ev.code), ev.value, d.name)
                            elif ev.type == ecodes.EV_REL and ev.code in wheel and ev.value:
                                up, down = wheel[ev.code]
                                self.feed(up if ev.value > 0 else down, 1, d.name)
                            elif ev.type == ecodes.EV_ABS and ev.code in hat and ev.value:
                                neg, pos = hat[ev.code]
                                self.feed(neg if ev.value < 0 else pos, 1, d.name)
                    except BlockingIOError:
                        pass
                    except OSError:
                        sel.unregister(key.fd)
        finally:
            for d in devs:
                try:
                    d.close()
                except OSError:
                    pass


# =========================================================
# Engine process
# =========================================================

class EngineProcess(QObject):
    log = Signal(str)
    status = Signal(dict)
    input = Signal(dict)
    state_changed = Signal()
    _exited = Signal(object, int)

    def __init__(self, hub):
        super().__init__()
        self.hub = hub
        self.proc = None
        self._exited.connect(self._on_exit)

    def child_running(self):
        return self.proc is not None and self.proc.poll() is None

    def external(self):
        """Running all_bind processes this GUI didn't start (autostart, terminal…)."""
        pids = find_engines()
        if self.child_running() and self.proc.pid in pids:
            pids.remove(self.proc.pid)
        return pids

    def running(self):
        return self.child_running() or bool(self.external())

    def services(self):
        """{pid: unit} for engines run by systemd as root (we can't signal them)."""
        return {p: engine_unit(p) for p in self.external()
                if not engine_is_mine(p) and engine_unit(p)}

    def start(self):
        if self.child_running():
            return True
        if engine_needs_build():
            self.log.emit("building all_bind from all_bind.c ...")
            ok, output = build_engine()
            if output:
                self.log.emit(output)
            if not ok:
                self.log.emit("build failed - engine not started")
                return False
            self.log.emit("build ok")
        # The engine itself stops any other running all_bind before grabbing,
        # and asks for the password (polkit) if we can't read the devices.
        if not has_device_access():
            self.log.emit("no access to input devices (not in the input group?) - "
                          "the engine will ask for your password")
        try:
            self.proc = subprocess.Popen(
                [ENGINE_BIN, "--events", CONF_PATH],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                cwd=BASE_DIR, start_new_session=True, bufsize=0,
            )
        except OSError as e:
            self.log.emit(f"cannot start engine: {e}")
            return False
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
        self.state_changed.emit()
        return True

    def _reader(self, proc):
        for raw in proc.stdout:
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                self.log.emit(line)
                continue
            t = msg.get("t")
            if t == "log":
                self.log.emit(msg.get("msg", ""))
            elif t == "status":
                self.status.emit(msg)
            elif t == "input":
                name = code_name(msg.get("code", 0))
                self.hub.feed(name, msg.get("value", 0), msg.get("dev", ""))
                self.input.emit(dict(msg, input=name))
        self._exited.emit(proc, proc.wait())

    def _on_exit(self, proc, rc):
        if proc is self.proc:
            if rc not in (0, -signal.SIGTERM):
                self.log.emit(f"engine exited with code {rc}")
            self.proc = None
        self.state_changed.emit()

    def send(self, cmd):
        if not self.child_running():
            return False
        try:
            self.proc.stdin.write((cmd + "\n").encode())
            self.proc.stdin.flush()
            return True
        except (BrokenPipeError, OSError):
            return False

    def restart(self):
        units = set(self.services().values())
        if units:
            # Root service: let systemd restart it (asks for the password).
            for u in units:
                ok, how = run_privileged(["systemctl", "restart", u])
                self.log.emit(f"{'restarted' if ok else 'could not restart'} {u}")
            self.state_changed.emit()
            return True
        self.stop()
        return self.start()

    def stop(self):
        if self.child_running():
            proc = self.proc
            self.send("quit")
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                # A child that asked for the password runs as root: we can't signal it.
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except (subprocess.TimeoutExpired, PermissionError):
                    send_engine_command("quit")
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        try:
                            proc.kill()
                        except PermissionError:
                            self.log.emit("could not stop the engine (it runs as root)")
        for m in stop_engines(self.external()):
            self.log.emit(m)
        self.state_changed.emit()

    def detach(self):
        """Leave the engine running headless after the GUI exits."""
        if self.proc is not None and self.proc.stdin:
            try:
                self.proc.stdin.close()
            except OSError:
                pass


# =========================================================
# Small widgets
# =========================================================

def hint_label(text):
    l = QLabel(text)
    l.setWordWrap(True)
    l.setStyleSheet("color: gray;")
    return l


def spin(lo, hi, val, step=1.0, decimals=0, suffix=""):
    if decimals:
        w = QDoubleSpinBox()
        w.setDecimals(decimals)
    else:
        w = QSpinBox()
    w.setRange(lo, hi)
    w.setSingleStep(step if decimals else int(step))
    w.setValue(val if decimals else int(val))
    if suffix:
        w.setSuffix(suffix)
    return w


class CaptureDialog(QDialog):
    """Waits for the next physical press and returns it."""

    def __init__(self, hub, parent=None, prompt="Press any key, button, d-pad or scroll the wheel…",
                 outputs=False):
        super().__init__(parent)
        self.setWindowTitle("Capture input")
        self.hub = hub
        self.outputs = outputs
        self.result_input = None
        self.result_device = None

        lay = QVBoxLayout(self)
        title = QLabel(prompt)
        title.setStyleSheet("font-size: 15px; font-weight: 600;")
        lay.addWidget(title)
        lay.addWidget(hint_label("Works on every device, grabbed or not.\n"
                                 "Left-clicking over Cancel cancels instead of capturing."))
        if hub.note:
            note = QLabel(hub.note)
            note.setWordWrap(True)
            note.setStyleSheet("color: #d68910;")
            lay.addWidget(note)
        self.skipped = hint_label("")
        lay.addWidget(self.skipped)
        self.cancel = QPushButton("Cancel")
        self.cancel.setAutoDefault(False)
        self.cancel.clicked.connect(self.reject)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.cancel)
        lay.addLayout(row)
        self.setMinimumWidth(440)

        hub.acquire()
        hub.event.connect(self._on_event)

    def _on_event(self, ev):
        if ev["value"] != 1 or self.result_input:
            return
        if ev["input"] == "BTN_LEFT" and self.cancel.underMouse():
            return
        if self.outputs and not output_allowed(ev["input"]):
            self.skipped.setText(f"{input_label(ev['input'])} can't be sent as an output "
                                 "(only keyboard keys, mouse buttons and wheel).")
            return
        self.result_input = ev["input"]
        self.result_device = ev["dev"]
        self.accept()

    def keyPressEvent(self, e):
        e.accept()   # Esc / Enter are capturable, not dialog shortcuts

    def done(self, r):
        try:
            self.hub.event.disconnect(self._on_event)
        except (RuntimeError, TypeError):
            pass
        self.hub.release()
        super().done(r)


class KeyPicker(QWidget):
    """Searchable key/button selector with a capture button."""

    changed = Signal()

    def __init__(self, hub, value=None, outputs=False, parent=None):
        super().__init__(parent)
        self.hub = hub
        self.outputs = outputs
        self.last_device = None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        self.combo = QComboBox()
        self.combo.setEditable(True)
        self.combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.combo.setMaxVisibleItems(20)
        for name in all_input_names(outputs):
            self.combo.addItem(input_label(name), name)
        comp = QCompleter(self.combo.model(), self.combo)
        comp.setFilterMode(Qt.MatchFlag.MatchContains)
        comp.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        comp.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.combo.setCompleter(comp)
        self.combo.currentIndexChanged.connect(lambda *_: self.changed.emit())
        self.combo.setMinimumWidth(240)
        lay.addWidget(self.combo, 1)

        rec = QPushButton("⏺ Capture")
        rec.setToolTip("Press the key / button you want")
        rec.clicked.connect(self.capture)
        lay.addWidget(rec)

        if value:
            self.set_value(value)

    def capture(self):
        dlg = CaptureDialog(self.hub, self, outputs=self.outputs)
        if dlg.exec() and dlg.result_input:
            self.last_device = dlg.result_device
            self.set_value(dlg.result_input)

    def set_value(self, name):
        idx = self.combo.findData(name)
        if idx < 0:
            self.combo.addItem(input_label(name), name)
            idx = self.combo.count() - 1
        self.combo.setCurrentIndex(idx)

    def value(self):
        text = self.combo.currentText().strip()
        idx = self.combo.findText(text)
        if idx >= 0:
            name = self.combo.itemData(idx)
        else:
            up = text.upper().replace(" ", "_")
            name = next((c for c in (up, "KEY_" + up, "BTN_" + up) if input_code(c) is not None), None)
        if name and self.outputs and not output_allowed(name):
            return None
        return name


class KeyListEditor(QWidget):
    """Ordered list of output keys (combos / multiple turbo targets)."""

    def __init__(self, hub, keys, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.picker = KeyPicker(hub, "BTN_LEFT", outputs=True)
        row.addWidget(self.picker, 1)
        add = QPushButton("Add")
        add.clicked.connect(self.add)
        row.addWidget(add)
        lay.addLayout(row)

        row2 = QHBoxLayout()
        self.list = QListWidget()
        self.list.setMaximumHeight(110)
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        row2.addWidget(self.list, 1)
        rm = QPushButton("Remove")
        rm.clicked.connect(self.remove)
        col = QVBoxLayout()
        col.addWidget(rm)
        col.addStretch(1)
        row2.addLayout(col)
        lay.addLayout(row2)

        for k in keys:
            self._append(k)

    def _append(self, name):
        it = QListWidgetItem(input_label(name))
        it.setData(Qt.ItemDataRole.UserRole, name)
        self.list.addItem(it)

    def add(self):
        v = self.picker.value()
        if v:
            self._append(v)

    def remove(self):
        for it in self.list.selectedItems():
            self.list.takeItem(self.list.row(it))

    def keys(self):
        return [self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())]


# =========================================================
# Macro editing
# =========================================================

class StepDialog(QDialog):
    def __init__(self, hub, step, parent=None):
        super().__init__(parent)
        self.step = dict(step)
        t = self.step["type"]
        self.setWindowTitle(dict(STEP_TYPES).get(t, "Step"))
        form = QFormLayout(self)
        self.w = {}

        if t in ("tap", "down", "up"):
            self.w["key"] = KeyPicker(hub, self.step.get("key", "BTN_LEFT"), outputs=True)
            form.addRow("Key / button:", self.w["key"])
            if t == "tap":
                self.w["hold_ms"] = spin(0, 60000, self.step.get("hold_ms", 0), 1, 2, " ms")
                self.w["hold_ms"].setToolTip("0 = press and release back to back (inhumanly fast)")
                form.addRow("Hold for:", self.w["hold_ms"])
        elif t == "delay":
            self.w["ms"] = spin(0, 3600000, self.step.get("ms", 10), 1, 3, " ms")
            self.w["random_ms"] = spin(0, 3600000, self.step.get("random_ms", 0), 1, 3, " ms")
            form.addRow("Wait:", self.w["ms"])
            form.addRow("Plus random up to:", self.w["random_ms"])
        elif t == "move":
            self.w["dx"] = spin(-100000, 100000, self.step.get("dx", 0))
            self.w["dy"] = spin(-100000, 100000, self.step.get("dy", 0))
            self.w["duration_ms"] = spin(0, 60000, self.step.get("duration_ms", 0), 1, 1, " ms")
            self.w["duration_ms"].setToolTip("0 = teleport in one event, otherwise spread over ~1 ms steps")
            form.addRow("ΔX (right +):", self.w["dx"])
            form.addRow("ΔY (down +):", self.w["dy"])
            form.addRow("Over:", self.w["duration_ms"])
        elif t == "wheel":
            self.w["amount"] = spin(-1000, 1000, self.step.get("amount", 1))
            self.w["horizontal"] = QCheckBox("Horizontal")
            self.w["horizontal"].setChecked(bool(self.step.get("horizontal")))
            form.addRow("Notches (up/right +):", self.w["amount"])
            form.addRow("", self.w["horizontal"])
        elif t == "text":
            self.w["text"] = QPlainTextEdit(self.step.get("text", ""))
            self.w["text"].setMinimumHeight(80)
            self.w["delay_ms"] = spin(0, 10000, self.step.get("delay_ms", 2), 1, 2, " ms")
            form.addRow("Text (US layout):", self.w["text"])
            form.addRow("Delay per character:", self.w["delay_ms"])
        elif t == "command":
            self.w["command"] = QLineEdit(self.step.get("command", ""))
            self.w["command"].setMinimumWidth(360)
            form.addRow("Shell command:", self.w["command"])

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def result_step(self):
        s = {"type": self.step["type"]}
        for k, w in self.w.items():
            if isinstance(w, KeyPicker):
                v = w.value()
                if v is None:
                    return None
                s[k] = v
            elif isinstance(w, QCheckBox):
                s[k] = w.isChecked()
            elif isinstance(w, QPlainTextEdit):
                s[k] = w.toPlainText()
            elif isinstance(w, QLineEdit):
                s[k] = w.text()
            else:
                s[k] = w.value()
        return s


class RecordDialog(QDialog):
    """Record a sequence of presses / releases with real timing."""

    def __init__(self, hub, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Record macro")
        self.hub = hub
        self.events = []
        self.recording = False
        self.steps = []

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Press Start, perform the inputs, then press Stop.\n"
                             "The click on Stop is trimmed automatically. Only keyboard keys,\n"
                             "mouse buttons and the wheel are kept (they're what can be replayed)."))
        self.keep_delays = QCheckBox("Record delays")
        self.keep_delays.setChecked(True)
        self.taps = QCheckBox("Merge quick press+release into taps")
        self.taps.setChecked(True)
        lay.addWidget(self.keep_delays)
        lay.addWidget(self.taps)
        self.view = QListWidget()
        self.view.setMinimumHeight(200)
        lay.addWidget(self.view)
        row = QHBoxLayout()
        self.btn = QPushButton("Start")
        self.btn.setAutoDefault(False)
        self.btn.clicked.connect(self.toggle)
        row.addWidget(self.btn)
        row.addStretch(1)
        self.ok = QPushButton("Use these steps")
        self.ok.setEnabled(False)
        self.ok.clicked.connect(self.accept)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        row.addWidget(self.ok)
        row.addWidget(cancel)
        lay.addLayout(row)
        self.resize(480, 440)
        hub.acquire()
        hub.event.connect(self._on_event)

    def keyPressEvent(self, e):
        e.accept()

    def toggle(self):
        if not self.recording:
            self.events = []
            self.view.clear()
            self.recording = True
            self.btn.setText("Stop")
            self.ok.setEnabled(False)
        else:
            self.recording = False
            self.btn.setText("Start")
            self._finish()

    def _on_event(self, ev):
        if not self.recording or not output_allowed(ev["input"]):
            return
        self.events.append((time.monotonic(), ev["input"], ev["value"]))
        self.view.addItem(f"{'↓' if ev['value'] else '↑'} {input_label(ev['input'])}")
        self.view.scrollToBottom()

    def _finish(self):
        evs = list(self.events)
        # Trim the click on "Stop".
        for i in range(len(evs) - 1, -1, -1):
            if evs[i][1] == "BTN_LEFT" and evs[i][2] == 1:
                evs = evs[:i]
                break
        # Drop releases whose press happened before recording started.
        down, cleaned = set(), []
        for t, name, v in evs:
            if name.startswith("WHEEL_"):
                cleaned.append((t, name, 1))
                continue
            if v == 1:
                down.add(name)
            elif name not in down:
                continue
            else:
                down.discard(name)
            cleaned.append((t, name, v))

        steps = []
        last_t = None
        for t, name, v in cleaned:
            if self.keep_delays.isChecked() and last_t is not None:
                ms = round((t - last_t) * 1000, 1)
                if ms >= 1:
                    steps.append({"type": "delay", "ms": ms, "random_ms": 0})
            last_t = t
            if name.startswith("WHEEL_"):
                steps.append({"type": "wheel", "amount": 1 if name in ("WHEEL_UP", "WHEEL_RIGHT") else -1,
                              "horizontal": name in ("WHEEL_LEFT", "WHEEL_RIGHT")})
            else:
                steps.append({"type": "down" if v else "up", "key": name})

        if self.taps.isChecked():
            merged, i = [], 0
            while i < len(steps):
                s = steps[i]
                nxt = steps[i + 1] if i + 1 < len(steps) else None
                nxt2 = steps[i + 2] if i + 2 < len(steps) else None
                if s["type"] == "down" and nxt and nxt["type"] == "up" and nxt["key"] == s["key"]:
                    merged.append({"type": "tap", "key": s["key"], "hold_ms": 0})
                    i += 2
                    continue
                if (s["type"] == "down" and nxt and nxt["type"] == "delay" and nxt2
                        and nxt2["type"] == "up" and nxt2["key"] == s["key"]):
                    merged.append({"type": "tap", "key": s["key"], "hold_ms": nxt["ms"]})
                    i += 3
                    continue
                merged.append(s)
                i += 1
            steps = merged

        self.steps = steps
        self.view.clear()
        for s in steps:
            self.view.addItem(step_text(s))
        self.ok.setEnabled(bool(steps))

    def done(self, r):
        try:
            self.hub.event.disconnect(self._on_event)
        except (RuntimeError, TypeError):
            pass
        self.hub.release()
        super().done(r)


class MacroEditor(QWidget):
    def __init__(self, hub, steps, parent=None):
        super().__init__(parent)
        self.hub = hub
        self.steps = copy.deepcopy(steps)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.list = QListWidget()
        self.list.setMinimumHeight(220)
        self.list.itemDoubleClicked.connect(lambda *_: self.edit())
        lay.addWidget(self.list, 1)

        col = QVBoxLayout()
        add = QToolButton()
        add.setText("Add step ▾")
        add.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(add)
        for t, label in STEP_TYPES:
            menu.addAction(label, lambda t=t: self.add(t))
        add.setMenu(menu)
        col.addWidget(add)
        for label, fn in (("Edit", self.edit), ("Duplicate", self.duplicate), ("Remove", self.remove),
                          ("Move up", lambda: self.move(-1)), ("Move down", lambda: self.move(1)),
                          ("⏺ Record…", self.record), ("Clear", self.clear)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            col.addWidget(b)
        col.addStretch(1)
        lay.addLayout(col)
        self.refresh()

    def refresh(self, select=None):
        self.list.clear()
        for s in self.steps:
            self.list.addItem(step_text(s))
        if select is not None and 0 <= select < len(self.steps):
            self.list.setCurrentRow(select)

    def add(self, t):
        dlg = StepDialog(self.hub, dict(STEP_DEFAULTS[t], type=t), self)
        if dlg.exec():
            s = dlg.result_step()
            if s:
                r = self.list.currentRow()
                i = len(self.steps) if r < 0 else r + 1
                self.steps.insert(i, s)
                self.refresh(i)

    def edit(self):
        r = self.list.currentRow()
        if r < 0:
            return
        dlg = StepDialog(self.hub, self.steps[r], self)
        if dlg.exec():
            s = dlg.result_step()
            if s:
                self.steps[r] = s
                self.refresh(r)

    def duplicate(self):
        r = self.list.currentRow()
        if r >= 0:
            self.steps.insert(r + 1, copy.deepcopy(self.steps[r]))
            self.refresh(r + 1)

    def remove(self):
        r = self.list.currentRow()
        if r >= 0:
            del self.steps[r]
            self.refresh(min(r, len(self.steps) - 1))

    def move(self, d):
        r = self.list.currentRow()
        j = r + d
        if r >= 0 and 0 <= j < len(self.steps):
            self.steps[r], self.steps[j] = self.steps[j], self.steps[r]
            self.refresh(j)

    def record(self):
        dlg = RecordDialog(self.hub, self)
        if dlg.exec() and dlg.steps:
            if self.steps and QMessageBox.question(
                    self, "Record", "Replace the existing steps?\n(No = append)") == QMessageBox.StandardButton.Yes:
                self.steps = []
            self.steps += dlg.steps
            self.refresh(len(self.steps) - 1)

    def clear(self):
        self.steps = []
        self.refresh()


# =========================================================
# Find device
# =========================================================

class DeviceProbe(QObject):
    """Runs watch_devices() in a thread and emits each hit."""

    hit = Signal(dict)

    def __init__(self):
        super().__init__()
        self.stop_flag = threading.Event()
        self.motion = threading.Event()     # also report pointer movement

    def start(self):
        self.stop_flag = threading.Event()
        threading.Thread(target=watch_devices, args=(self.hit.emit, self.stop_flag, self.motion),
                         daemon=True).start()

    def stop(self):
        self.stop_flag.set()


class FindDeviceDialog(QDialog):
    """Listen for input and say which device it came from."""

    def __init__(self, hub, main, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Find device")
        self.hub = hub
        self.main = main
        self.found = None
        self.listening = False

        lay = QVBoxLayout(self)
        self.prompt = QLabel()
        self.prompt.setStyleSheet("font-size: 15px; font-weight: 600;")
        lay.addWidget(self.prompt)
        lay.addWidget(hint_label("Press a key or button, click, scroll, touch the touchpad, push a "
                                 "stick or d-pad, or plug in headphones. The first device that "
                                 "reacts is shown below and listening pauses, so clicking these "
                                 "buttons doesn't change the answer."))
        self.motion = QCheckBox("Also detect mouse movement")
        self.motion.setToolTip("Off by default so moving the cursor to this window doesn't count.")
        lay.addWidget(self.motion)

        box = QGroupBox("Device")
        bl = QVBoxLayout(box)
        self.name = QLabel("—")
        self.name.setStyleSheet("font-size: 18px; font-weight: 700;")
        self.name.setWordWrap(True)
        self.name.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        bl.addWidget(self.name)
        self.details = QLabel("")
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        bl.addWidget(self.details)
        lay.addWidget(box)

        lay.addWidget(QLabel("Found so far:"))
        self.history = QListWidget()
        self.history.setMaximumHeight(120)
        lay.addWidget(self.history)

        row = QHBoxLayout()
        self.again = QPushButton("Listen again")
        self.again.clicked.connect(self.listen)
        row.addWidget(self.again)
        self.show_btn = QPushButton("Show in list")
        self.show_btn.clicked.connect(lambda: self.main.select_device(self.found["spec"]))
        row.addWidget(self.show_btn)
        self.grab_btn = QPushButton("Grab it")
        self.grab_btn.clicked.connect(self._grab)
        row.addWidget(self.grab_btn)
        row.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        row.addWidget(close)
        for b in (self.again, self.show_btn, self.grab_btn, close):
            b.setAutoDefault(False)
        lay.addLayout(row)
        self.resize(560, 460)

        self.probe = DeviceProbe()
        self.probe.hit.connect(self._on_hit)
        self.motion.toggled.connect(lambda v: self.probe.motion.set() if v else self.probe.motion.clear())
        self.probe.start()
        # Grabbed devices only report through the engine's stream.
        hub.event.connect(self._on_engine_event)
        self.listen()

    def listen(self):
        self.listening = False
        self.prompt.setText("Listening… use the device you want to find")
        self.again.setEnabled(False)
        self.show_btn.setEnabled(False)
        self.grab_btn.setEnabled(False)
        # Short arm delay so the click that started listening doesn't count.
        QTimer.singleShot(400, lambda: setattr(self, "listening", True))

    def _on_engine_event(self, ev):
        if ev["value"] != 1:
            return
        spec = next((s for s in self.main.cfg["devices"] if s["name"] == ev["dev"]),
                    {"name": ev["dev"], "vendor": 0, "product": 0})
        self._on_hit({"spec": spec, "path": None, "kinds": [],
                      "what": f"{input_label(ev['input'])} pressed", "grabbed": True})

    def _on_hit(self, hit):
        if not self.listening:
            return
        self.listening = False
        self.found = hit
        spec = hit["spec"]
        grabbed = hit.get("grabbed") or any(spec_key(s) == spec_key(spec) for s in self.main.cfg["devices"])

        self.prompt.setText("Found it")
        self.name.setText(spec["name"])
        parts = [f"Input: {hit['what']}"]
        if spec.get("vendor") or spec.get("product"):
            parts.append(f"USB id: {spec.get('vendor', 0):04x}:{spec.get('product', 0):04x}")
        if hit["kinds"]:
            parts.append(f"Type: {', '.join(hit['kinds'])}")
        if hit["path"]:
            parts.append(f"Node: {hit['path']}")
        parts.append("Grabbed by all_bind: " + ("yes" if grabbed else "no"))
        self.details.setText("\n".join(parts))
        self.history.insertItem(0, f"{spec['name']}  —  {hit['what']}")

        self.again.setEnabled(True)
        self.show_btn.setEnabled(True)
        self.grab_btn.setEnabled(not grabbed)

    def _grab(self):
        self.main.grab_device(self.found["spec"])
        spec = self.found["spec"]
        if any(spec_key(s) == spec_key(spec) for s in self.main.cfg["devices"]):
            self.grab_btn.setEnabled(False)
            self.details.setText(self.details.text().replace("Grabbed by all_bind: no",
                                                             "Grabbed by all_bind: yes"))

    def keyPressEvent(self, e):
        e.accept()   # keys are things to detect, not dialog shortcuts

    def done(self, r):
        self.probe.stop()
        try:
            self.hub.event.disconnect(self._on_engine_event)
        except (RuntimeError, TypeError):
            pass
        super().done(r)


# =========================================================
# Binding editor
# =========================================================

class BindingDialog(QDialog):
    def __init__(self, hub, binding, profiles, devices, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit binding")
        self.hub = hub
        b = copy.deepcopy(binding)
        a = b.get("action", {})
        lay = QVBoxLayout(self)

        # Trigger
        tg = QGroupBox("When this is pressed")
        tf = QFormLayout(tg)
        self.trigger = KeyPicker(hub, b["trigger"]["input"])
        tf.addRow("Input:", self.trigger)
        self.device = QComboBox()
        self.device.addItem("Any grabbed device", None)
        for d in devices:
            self.device.addItem(d, d)
        if b["trigger"].get("device"):
            i = self.device.findData(b["trigger"]["device"])
            if i < 0:
                self.device.addItem(b["trigger"]["device"], b["trigger"]["device"])
                i = self.device.count() - 1
            self.device.setCurrentIndex(i)
        tf.addRow("From:", self.device)
        self.trigger.combo.currentIndexChanged.connect(self._trigger_captured)
        self.enabled = QCheckBox("Enabled")
        self.enabled.setChecked(b.get("enabled", True))
        tf.addRow("", self.enabled)
        lay.addWidget(tg)

        # Action
        ag = QGroupBox("Do this")
        al = QVBoxLayout(ag)
        self.type = QComboBox()
        for t, label in ACTION_TYPES:
            self.type.addItem(label, t)
        al.addWidget(self.type)
        self.pages = QStackedWidget()
        al.addWidget(self.pages)
        lay.addWidget(ag, 1)

        def get(t, k, d):
            return a.get(k, d) if a.get("type") == t else d

        # remap
        p = QWidget()
        f = QVBoxLayout(p)
        self.remap_keys = KeyListEditor(hub, get("remap", "keys", ["BTN_LEFT"]))
        f.addWidget(self.remap_keys)
        f.addWidget(hint_label("Keys are pressed together in order and released in reverse "
                               "(e.g. Ctrl, C → Ctrl+C). Wheel entries scroll once per press."))
        f.addStretch(1)
        self.pages.addWidget(p)

        # turbo
        p = QWidget()
        f = QFormLayout(p)
        self.turbo_keys = KeyListEditor(hub, get("turbo", "keys", ["BTN_LEFT"]))
        f.addRow("Spam:", self.turbo_keys)
        cps_row = QHBoxLayout()
        self.cps = spin(0.1, 20000, get("turbo", "cps", 20), 1, 1, " /s")
        cps_row.addWidget(self.cps)
        for v in (10, 20, 50, 100, 500, 1000):
            qb = QPushButton(str(v))
            qb.setFixedWidth(52)
            qb.clicked.connect(lambda _=False, v=v: self.cps.setValue(v))
            cps_row.addWidget(qb)
        cps_row.addStretch(1)
        f.addRow("Rate:", cps_row)
        self.press_ms = spin(0, 10000, get("turbo", "press_ms", 0), 0.1, 2, " ms")
        self.press_ms.setToolTip("How long each press is held. 0 = press and release back to back.")
        f.addRow("Press length:", self.press_ms)
        self.jitter = spin(0, 95, get("turbo", "jitter", 0), 1, 0, " %")
        self.jitter.setToolTip("Randomise each interval by up to ± this much (0 = machine-perfect)")
        f.addRow("Interval jitter:", self.jitter)
        self.burst = spin(0, 100000, get("turbo", "burst", 0))
        self.burst.setSpecialValueText("unlimited")
        self.burst.setToolTip("Stop after this many presses (0 = unlimited)")
        f.addRow("Burst count:", self.burst)
        self.turbo_mode = QComboBox()
        self.turbo_mode.addItem("While held", "hold")
        self.turbo_mode.addItem("Toggle on/off with each press", "toggle")
        self.turbo_mode.setCurrentIndex(max(0, self.turbo_mode.findData(get("turbo", "mode", "hold"))))
        f.addRow("Mode:", self.turbo_mode)
        self.turbo_info = hint_label("")
        f.addRow("", self.turbo_info)
        self.cps.valueChanged.connect(self._turbo_hint)
        self.press_ms.valueChanged.connect(self._turbo_hint)
        self._turbo_hint()
        self.pages.addWidget(p)

        # macro
        p = QWidget()
        f = QVBoxLayout(p)
        self.macro = MacroEditor(hub, get("macro", "steps", []))
        f.addWidget(self.macro, 1)
        mrow = QHBoxLayout()
        self.macro_mode = QComboBox()
        for d, label in (("once", "Run once per press"), ("repeat", "Repeat N times per press"),
                         ("while_held", "Loop while held"), ("toggle", "Loop, toggle with each press")):
            self.macro_mode.addItem(label, d)
        self.macro_mode.setCurrentIndex(max(0, self.macro_mode.findData(get("macro", "mode", "once"))))
        mrow.addWidget(self.macro_mode)
        mrow.addWidget(QLabel("N:"))
        self.macro_count = spin(1, 1000000, get("macro", "count", 1))
        mrow.addWidget(self.macro_count)
        mrow.addWidget(QLabel("Gap between loops:"))
        self.loop_gap = spin(0, 3600000, get("macro", "loop_gap_ms", 0), 1, 2, " ms")
        mrow.addWidget(self.loop_gap)
        mrow.addStretch(1)
        f.addLayout(mrow)
        self.macro_mode.currentIndexChanged.connect(self._macro_mode)
        self._macro_mode()
        self.pages.addWidget(p)

        # command
        p = QWidget()
        f = QFormLayout(p)
        self.command = QLineEdit(get("command", "command", ""))
        self.command.setPlaceholderText("e.g. notify-send hi   or   hyprctl dispatch workspace +1")
        f.addRow("Shell command:", self.command)
        f.addRow("", hint_label("Runs via /bin/sh on press, detached from the engine."))
        self.pages.addWidget(p)

        # layer
        p = QWidget()
        f = QFormLayout(p)
        self.layer_profile = QComboBox()
        self.layer_profile.addItems(profiles)
        if get("layer", "profile", None) in profiles:
            self.layer_profile.setCurrentText(a["profile"])
        f.addRow("Profile:", self.layer_profile)
        self.layer_mode = QComboBox()
        self.layer_mode.addItem("While held", "hold")
        self.layer_mode.addItem("Toggle", "toggle")
        self.layer_mode.setCurrentIndex(max(0, self.layer_mode.findData(get("layer", "mode", "hold"))))
        f.addRow("Mode:", self.layer_mode)
        f.addRow("", hint_label("Bindings in that profile override this one; anything it doesn't bind "
                                "falls through. Its movement settings (sensitivity, inversion) apply "
                                "while active — handy for a sniper button."))
        self.pages.addWidget(p)

        # switch
        p = QWidget()
        f = QFormLayout(p)
        self.switch_profile = QComboBox()
        self.switch_profile.addItems(profiles)
        if get("switch_profile", "profile", None) in profiles:
            self.switch_profile.setCurrentText(a["profile"])
        f.addRow("Profile:", self.switch_profile)
        self.pages.addWidget(p)

        # disable
        p = QWidget()
        f = QVBoxLayout(p)
        f.addWidget(hint_label("The input is swallowed and does nothing."))
        f.addStretch(1)
        self.pages.addWidget(p)

        self.type.currentIndexChanged.connect(self.pages.setCurrentIndex)
        self.type.setCurrentIndex(max(0, self.type.findData(a.get("type", "remap"))))
        self.pages.setCurrentIndex(self.type.currentIndex())

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.resize(720, 640)

    def _trigger_captured(self):
        dev = self.trigger.last_device
        if dev and self.device.findData(dev) < 0:
            self.device.addItem(dev, dev)

    def _turbo_hint(self):
        period = 1000.0 / self.cps.value()
        press = min(self.press_ms.value(), period * 0.9)
        self.turbo_info.setText(f"One press every {period:.3f} ms, held {press:.3f} ms. "
                                f"Above ~1000/s many apps and compositors start dropping clicks.")

    def _macro_mode(self):
        m = self.macro_mode.currentData()
        self.macro_count.setEnabled(m == "repeat")
        self.loop_gap.setEnabled(m != "once")

    def _ok(self):
        if self.trigger.value() is None:
            QMessageBox.warning(self, "Binding", "Pick a valid trigger input.")
            return
        self.accept()

    def result_binding(self):
        t = self.type.currentData()
        if t == "remap":
            a = {"type": t, "keys": self.remap_keys.keys()}
        elif t == "turbo":
            a = {"type": t, "keys": self.turbo_keys.keys() or ["BTN_LEFT"], "cps": self.cps.value(),
                 "press_ms": self.press_ms.value(), "jitter": self.jitter.value(),
                 "burst": self.burst.value(), "mode": self.turbo_mode.currentData()}
        elif t == "macro":
            a = {"type": t, "steps": self.macro.steps, "mode": self.macro_mode.currentData(),
                 "count": self.macro_count.value(), "loop_gap_ms": self.loop_gap.value()}
        elif t == "command":
            a = {"type": t, "command": self.command.text()}
        elif t == "layer":
            a = {"type": t, "profile": self.layer_profile.currentText(), "mode": self.layer_mode.currentData()}
        elif t == "switch_profile":
            a = {"type": t, "profile": self.switch_profile.currentText()}
        else:
            a = {"type": "disable"}
        return {
            "enabled": self.enabled.isChecked(),
            "trigger": {"input": self.trigger.value(), "device": self.device.currentData()},
            "action": a,
        }


# =========================================================
# Main window
# =========================================================

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("all_bind")
        self.cfg = load_config()
        self.hub = InputHub()
        self.engine = EngineProcess(self.hub)
        self.engine_status = {}
        self.save_timer = QTimer(self, singleShot=True, interval=250)
        self.save_timer.timeout.connect(self._save_now)

        self._build_menu()
        self._build_ui()

        self.engine.log.connect(self.log)
        self.engine.status.connect(self._on_status)
        self.engine.input.connect(self._on_input)
        self.engine.state_changed.connect(self._update_engine_state)

        self.refresh_all()
        save_config(self.cfg)   # make sure all_bind.conf exists / is current
        if MIGRATED:
            self.log(f"moved your config from {MIGRATED} to {CONFIG_PATH}")
        self.log(f"config: {CONFIG_PATH}")

        ext = self.engine.external()
        if ext:
            self.log("already running: " + ", ".join(f"all_bind (pid {p})" for p in ext)
                     + " — edits apply to it live; press Restart to run it from this GUI")
        elif self.cfg.get("start_engine_on_open", True):
            self.engine.start()
        self._update_engine_state()

        poll = QTimer(self, interval=1500)
        poll.timeout.connect(self._update_engine_state)
        poll.start()

    # --- construction -------------------------------------------------

    def _build_menu(self):
        m = self.menuBar().addMenu("&Engine")
        m.addAction("Start", self.engine_start)
        m.addAction("Restart", self.engine_restart)
        m.addAction("Stop", self.engine_stop)
        m.addSeparator()
        m.addAction("Rebuild all_bind.c", self.rebuild)
        m.addSeparator()
        m.addAction("Quit", self.close)

        s = self.menuBar().addMenu("&Settings")
        self.act_keep = QAction("Keep engine running after closing the GUI", self, checkable=True)
        self.act_keep.setChecked(self.cfg.get("keep_running_on_close", True))
        self.act_keep.toggled.connect(lambda v: self._set_cfg("keep_running_on_close", v))
        s.addAction(self.act_keep)
        self.act_autostart = QAction("Start engine when the GUI opens", self, checkable=True)
        self.act_autostart.setChecked(self.cfg.get("start_engine_on_open", True))
        self.act_autostart.toggled.connect(lambda v: self._set_cfg("start_engine_on_open", v))
        s.addAction(self.act_autostart)

        h = self.menuBar().addMenu("&Help")
        h.addAction("Run without the GUI…", self.show_headless)
        h.addAction("Tips…", self.show_tips)

    def _build_ui(self):
        central = QWidget()
        root = QVBoxLayout(central)

        # Top bar: profile + engine control
        top = QHBoxLayout()
        top.addWidget(QLabel("Profile:"))
        self.profile_combo = QComboBox()
        self.profile_combo.setMinimumWidth(180)
        self.profile_combo.currentTextChanged.connect(self._profile_selected)
        top.addWidget(self.profile_combo)
        for label, fn in (("New", self.profile_new), ("Duplicate", self.profile_dup),
                          ("Rename", self.profile_rename), ("Delete", self.profile_delete)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            top.addWidget(b)
        top.addStretch(1)
        self.status_label = QLabel()
        self.status_label.setTextFormat(Qt.TextFormat.RichText)
        top.addWidget(self.status_label)
        self.engine_btn = QPushButton()
        self.engine_btn.setMinimumWidth(90)
        self.engine_btn.clicked.connect(self._engine_start_or_restart)
        top.addWidget(self.engine_btn)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self.engine_stop)
        top.addWidget(self.stop_btn)
        root.addLayout(top)

        split = QSplitter(Qt.Orientation.Vertical)
        upper = QSplitter(Qt.Orientation.Horizontal)

        # Devices
        dev_box = QGroupBox("Devices — check to grab")
        dl = QVBoxLayout(dev_box)
        self.dev_list = QListWidget()
        self.dev_list.itemChanged.connect(self._device_toggled)
        dl.addWidget(self.dev_list)
        drow = QHBoxLayout()
        fb = QPushButton("🔍 Find device…")
        fb.setToolTip("Use a device and see which one it is")
        fb.clicked.connect(self.find_device)
        drow.addWidget(fb)
        drow.addStretch(1)
        rb = QPushButton("Refresh")
        rb.clicked.connect(self.refresh_devices)
        drow.addWidget(rb)
        dl.addLayout(drow)
        dl.addWidget(hint_label("Every input device is listed. Checked ones are grabbed exclusively "
                                "and their input goes through all_bind. Many mice expose extra "
                                "buttons on a second “Keyboard” device — grab that too if a button "
                                "doesn't show up."))
        upper.addWidget(dev_box)

        # Tabs: bindings + movement
        tabs = QTabWidget()
        bw = QWidget()
        bl = QVBoxLayout(bw)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["On", "Trigger", "From", "Action"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        hh = self.table.horizontalHeader()
        for c in (0, 1, 2):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.cellDoubleClicked.connect(lambda r, c: self.binding_edit() if c else None)
        self.table.itemChanged.connect(self._binding_toggled)
        bl.addWidget(self.table, 1)
        brow = QHBoxLayout()
        for label, fn in (("Add…", self.binding_add), ("Quick add (capture)…", self.binding_quick),
                          ("Edit…", self.binding_edit), ("Duplicate", self.binding_dup),
                          ("Remove", self.binding_remove), ("▲", lambda: self.binding_move(-1)),
                          ("▼", lambda: self.binding_move(1))):
            b = QPushButton(label)
            b.clicked.connect(fn)
            brow.addWidget(b)
        brow.addStretch(1)
        bl.addLayout(brow)
        tabs.addTab(bw, "Bindings")

        mw = QWidget()
        ml = QFormLayout(mw)
        self.mv = {}
        for key, label in (("invert_x", "Invert X"), ("invert_y", "Invert Y"),
                           ("swap_xy", "Swap X and Y"), ("invert_wheel", "Invert scroll wheel"),
                           ("invert_hwheel", "Invert horizontal scroll")):
            cb = QCheckBox(label)
            cb.toggled.connect(lambda v, k=key: self._set_move(k, v))
            self.mv[key] = cb
            ml.addRow("", cb)
        for key, label in (("sens_x", "X sensitivity:"), ("sens_y", "Y sensitivity:")):
            sb = spin(0.01, 50.0, 1.0, 0.05, 2, " ×")
            sb.valueChanged.connect(lambda v, k=key: self._set_move(k, v))
            self.mv[key] = sb
            ml.addRow(label, sb)
        self.link_sens = QCheckBox("Link X and Y sensitivity")
        self.link_sens.setChecked(True)
        ml.addRow("", self.link_sens)
        ml.addRow("", hint_label("Applies to grabbed mice in the active profile (or the top layer "
                                 "while a “shift to profile” button is held). Sensitivity keeps "
                                 "sub-pixel remainders, so low values stay smooth."))
        tabs.addTab(mw, "Movement")
        upper.addWidget(tabs)
        upper.setStretchFactor(0, 1)
        upper.setStretchFactor(1, 3)
        upper.setSizes([360, 820])
        split.addWidget(upper)

        # Log
        log_box = QGroupBox("Engine log")
        ll = QVBoxLayout(log_box)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setFont(QFont("monospace", 9))
        ll.addWidget(self.log_view)
        lrow = QHBoxLayout()
        self.show_inputs = QCheckBox("Show physical input events (find out what a button sends)")
        lrow.addWidget(self.show_inputs)
        lrow.addStretch(1)
        cl = QPushButton("Clear")
        cl.clicked.connect(self.log_view.clear)
        lrow.addWidget(cl)
        ll.addLayout(lrow)
        split.addWidget(log_box)
        split.setSizes([520, 200])
        root.addWidget(split, 1)

        self.setCentralWidget(central)
        self.resize(1220, 800)

    # --- helpers ------------------------------------------------------

    def profile(self):
        return self.cfg["profiles"][self.cfg["active_profile"]]

    def changed(self):
        self.save_timer.start()

    def _save_now(self):
        # The engine watches the config folder and reloads by itself.
        try:
            save_config(self.cfg)
        except OSError as e:
            self.log(f"save failed: {e}")

    def _set_cfg(self, key, value):
        self.cfg[key] = value
        self.changed()

    def log(self, text):
        self.log_view.appendPlainText(text)

    def refresh_all(self):
        self.refresh_profiles()
        self.refresh_devices()
        self.refresh_bindings()
        self.refresh_movement()

    # --- profiles -----------------------------------------------------

    def refresh_profiles(self):
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItems(list(self.cfg["profiles"]))
        self.profile_combo.setCurrentText(self.cfg["active_profile"])
        self.profile_combo.blockSignals(False)

    def _profile_selected(self, name):
        if name and name in self.cfg["profiles"]:
            self.cfg["active_profile"] = name
            self.refresh_bindings()
            self.refresh_movement()
            self.changed()

    def _ask_name(self, title, default=""):
        name, ok = QInputDialog.getText(self, title, "Profile name:", text=default)
        name = name.strip()
        if not ok or not name:
            return None
        if name in self.cfg["profiles"]:
            QMessageBox.warning(self, title, "A profile with that name already exists.")
            return None
        return name

    def profile_new(self):
        name = self._ask_name("New profile")
        if name:
            self.cfg["profiles"][name] = new_profile()
            self.cfg["active_profile"] = name
            self.refresh_all()
            self.changed()

    def profile_dup(self):
        name = self._ask_name("Duplicate profile", self.cfg["active_profile"] + " copy")
        if name:
            self.cfg["profiles"][name] = copy.deepcopy(self.profile())
            self.cfg["active_profile"] = name
            self.refresh_all()
            self.changed()

    def profile_rename(self):
        old = self.cfg["active_profile"]
        name = self._ask_name("Rename profile", old)
        if not name:
            return
        self.cfg["profiles"] = {(name if k == old else k): v for k, v in self.cfg["profiles"].items()}
        for prof in self.cfg["profiles"].values():
            for b in prof["bindings"]:
                if b["action"].get("profile") == old:
                    b["action"]["profile"] = name
        self.cfg["active_profile"] = name
        self.refresh_all()
        self.changed()

    def profile_delete(self):
        if len(self.cfg["profiles"]) <= 1:
            QMessageBox.information(self, "Delete profile", "Can't delete the last profile.")
            return
        name = self.cfg["active_profile"]
        if QMessageBox.question(self, "Delete profile", f"Delete profile “{name}”?") != QMessageBox.StandardButton.Yes:
            return
        del self.cfg["profiles"][name]
        self.cfg["active_profile"] = next(iter(self.cfg["profiles"]))
        self.refresh_all()
        self.changed()

    # --- devices ------------------------------------------------------

    def refresh_devices(self):
        self.dev_list.blockSignals(True)
        self.dev_list.clear()
        failed = set(self.engine_status.get("failed", [])) if self.engine.child_running() else set()
        grabbed = set(self.engine_status.get("grabbed", [])) if self.engine.child_running() else set()
        seen = set()
        for d in sorted(scan_devices(), key=lambda d: (d.name.lower(), d.path)):
            spec = device_spec(d)
            key = spec_key(spec)
            if key in seen:
                d.close()
                continue
            seen.add(key)
            kinds = describe_device(d)
            configured = any(spec_matches(s, d) for s in self.cfg["devices"])
            state = ""
            if configured and d.name in failed:
                state = "  ·  ⚠ busy (another program has it)"
            elif configured and d.name in grabbed:
                state = "  ·  ✓ grabbed"
            label = (f"{d.name}\n    {spec['vendor']:04x}:{spec['product']:04x}  ·  "
                     f"{', '.join(kinds)}  ·  {os.path.basename(d.path)}{state}")
            it = QListWidgetItem(label)
            it.setData(Qt.ItemDataRole.UserRole, spec)
            it.setData(Qt.ItemDataRole.UserRole + 1, kinds)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if configured else Qt.CheckState.Unchecked)
            if state.startswith("  ·  ⚠"):
                it.setForeground(QColor("#d68910"))
            self.dev_list.addItem(it)
            d.close()
        # Configured but not plugged in.
        for s in self.cfg["devices"]:
            if spec_key(s) in seen:
                continue
            it = QListWidgetItem(f"{s['name']}\n    (not connected — grabbed when plugged in)")
            it.setData(Qt.ItemDataRole.UserRole, s)
            it.setData(Qt.ItemDataRole.UserRole + 1, [])
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked)
            it.setForeground(QColor("gray"))
            self.dev_list.addItem(it)
        self.dev_list.blockSignals(False)

    def _device_toggled(self, it):
        spec = it.data(Qt.ItemDataRole.UserRole)
        kinds = it.data(Qt.ItemDataRole.UserRole + 1) or []
        on = it.checkState() == Qt.CheckState.Checked
        warn = None
        if on and "keyboard" in kinds:
            warn = ("Its keys will go through all_bind. If you lock yourself out, stop the engine "
                    "from this window or run `pkill all_bind`.")
        elif on and "controller" in kinds:
            warn = ("all_bind re-creates it as “… (all_bind)” with the same buttons and sticks. "
                    "Rumble isn't forwarded, and games that read the controller directly "
                    "(Steam Input / hidraw) may still see the original.")
        if warn:
            r = QMessageBox.warning(self, "Grab device", f"Grab “{spec['name']}”?\n\n{warn}",
                                    QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
            if r != QMessageBox.StandardButton.Ok:
                self.dev_list.blockSignals(True)
                it.setCheckState(Qt.CheckState.Unchecked)
                self.dev_list.blockSignals(False)
                return
        self.cfg["devices"] = [s for s in self.cfg["devices"] if spec_key(s) != spec_key(spec)]
        if on:
            self.cfg["devices"].append(dict(spec))
        self.changed()

    def find_device(self):
        FindDeviceDialog(self.hub, self, self).exec()

    def _device_item(self, spec):
        for i in range(self.dev_list.count()):
            it = self.dev_list.item(i)
            if spec_key(it.data(Qt.ItemDataRole.UserRole)) == spec_key(spec):
                return it
        return None

    def select_device(self, spec):
        it = self._device_item(spec)
        if it is None:          # plugged in after the list was built
            self.refresh_devices()
            it = self._device_item(spec)
        if it is not None:
            self.dev_list.setCurrentItem(it)
            self.dev_list.scrollToItem(it)
        return it

    def grab_device(self, spec):
        it = self.select_device(spec)
        if it is not None and it.checkState() != Qt.CheckState.Checked:
            it.setCheckState(Qt.CheckState.Checked)     # runs _device_toggled (warnings, save)

    def device_names(self):
        return [s["name"] for s in self.cfg["devices"]]

    # --- bindings -----------------------------------------------------

    def refresh_bindings(self, select=None):
        self.table.blockSignals(True)
        bs = self.profile()["bindings"]
        self.table.setRowCount(len(bs))
        for r, b in enumerate(bs):
            on = QTableWidgetItem()
            on.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            on.setCheckState(Qt.CheckState.Checked if b.get("enabled", True) else Qt.CheckState.Unchecked)
            self.table.setItem(r, 0, on)
            self.table.setItem(r, 1, QTableWidgetItem(input_label(b["trigger"]["input"])))
            self.table.setItem(r, 2, QTableWidgetItem(b["trigger"].get("device") or "any"))
            self.table.setItem(r, 3, QTableWidgetItem(action_summary(b["action"])))
            if not b.get("enabled", True):
                for c in (1, 2, 3):
                    self.table.item(r, c).setForeground(QColor("gray"))
        self.table.blockSignals(False)
        if select is not None and 0 <= select < len(bs):
            self.table.selectRow(select)

    def _binding_toggled(self, it):
        if it.column() == 0:
            self.profile()["bindings"][it.row()]["enabled"] = it.checkState() == Qt.CheckState.Checked
            self.refresh_bindings(it.row())
            self.changed()

    def _row(self):
        rows = self.table.selectionModel().selectedRows()
        return rows[0].row() if rows else -1

    def _edit_dialog(self, binding):
        dlg = BindingDialog(self.hub, binding, list(self.cfg["profiles"]), self.device_names(), self)
        return dlg.result_binding() if dlg.exec() else None

    def _check_conflict(self, b, skip=-1):
        for i, o in enumerate(self.profile()["bindings"]):
            if i != skip and o.get("enabled", True) and b.get("enabled", True) \
                    and o["trigger"]["input"] == b["trigger"]["input"] \
                    and (o["trigger"].get("device") or None) == (b["trigger"].get("device") or None):
                QMessageBox.information(self, "Duplicate trigger",
                                        f"{input_label(b['trigger']['input'])} is already bound in this "
                                        "profile; only the first enabled binding is used.")
                return

    def binding_add(self, trigger="BTN_SIDE"):
        b = self._edit_dialog({"enabled": True, "trigger": {"input": trigger, "device": None},
                               "action": {"type": "remap", "keys": ["BTN_LEFT"]}})
        if b:
            self._check_conflict(b)
            self.profile()["bindings"].append(b)
            self.refresh_bindings(len(self.profile()["bindings"]) - 1)
            self.changed()

    def binding_quick(self):
        dlg = CaptureDialog(self.hub, self, "Press the button / key you want to bind…")
        if dlg.exec() and dlg.result_input:
            self.binding_add(dlg.result_input)

    def binding_edit(self):
        r = self._row()
        if r < 0:
            return
        b = self._edit_dialog(self.profile()["bindings"][r])
        if b:
            self._check_conflict(b, skip=r)
            self.profile()["bindings"][r] = b
            self.refresh_bindings(r)
            self.changed()

    def binding_dup(self):
        r = self._row()
        if r >= 0:
            bs = self.profile()["bindings"]
            bs.insert(r + 1, copy.deepcopy(bs[r]))
            self.refresh_bindings(r + 1)
            self.changed()

    def binding_remove(self):
        r = self._row()
        if r >= 0:
            del self.profile()["bindings"][r]
            self.refresh_bindings(min(r, len(self.profile()["bindings"]) - 1))
            self.changed()

    def binding_move(self, d):
        r = self._row()
        bs = self.profile()["bindings"]
        j = r + d
        if r >= 0 and 0 <= j < len(bs):
            bs[r], bs[j] = bs[j], bs[r]
            self.refresh_bindings(j)
            self.changed()

    # --- movement -----------------------------------------------------

    def refresh_movement(self):
        mv = self.profile()["movement"]
        for k, w in self.mv.items():
            w.blockSignals(True)
            if isinstance(w, QCheckBox):
                w.setChecked(bool(mv[k]))
            else:
                w.setValue(float(mv[k]))
            w.blockSignals(False)

    def _set_move(self, key, value):
        mv = self.profile()["movement"]
        mv[key] = value
        if key in ("sens_x", "sens_y") and self.link_sens.isChecked():
            other = "sens_y" if key == "sens_x" else "sens_x"
            mv[other] = value
            self.mv[other].blockSignals(True)
            self.mv[other].setValue(value)
            self.mv[other].blockSignals(False)
        self.changed()

    # --- engine -------------------------------------------------------

    def _engine_start_or_restart(self):
        if self.engine.running():
            self.engine_restart()
        else:
            self.engine_start()

    def engine_start(self):
        self.save_timer.stop()
        self._save_now()
        self.engine.start()
        self._update_engine_state()

    def engine_stop(self):
        self.engine.stop()
        self.engine_status = {}
        self._update_engine_state()
        self.refresh_devices()

    def engine_restart(self):
        self.log("restarting engine ...")
        self.save_timer.stop()
        self._save_now()
        self.engine_status = {}
        self.engine.restart()
        self._update_engine_state()

    def rebuild(self):
        was = self.engine.running()
        if was:
            self.engine.stop()
        ok, out = build_engine()
        self.log(out or ("build ok" if ok else "build failed"))
        if ok and was:
            self.engine_start()
        self._update_engine_state()

    def _on_status(self, st):
        old = (self.engine_status.get("grabbed"), self.engine_status.get("failed"))
        self.engine_status = st
        self._update_engine_state()
        if (st.get("grabbed"), st.get("failed")) != old:
            self.refresh_devices()

    def _on_input(self, msg):
        if self.show_inputs.isChecked():
            v = msg.get("value")
            arrow = "↓" if v == 1 else "↑" if v == 0 else str(v)
            self.log(f"  {arrow} {input_label(msg['input'])}   from {msg.get('dev', '')}")

    def _update_engine_state(self):
        child = self.engine.child_running()
        ext = self.engine.external()

        if child:
            st = self.engine_status
            prof = st.get("profile", "")
            layers = st.get("layers", [])
            grabbed = st.get("grabbed", [])
            failed = st.get("failed", [])
            txt = f"<span style='color:#2e9e44'>●</span> Running (pid {self.engine.proc.pid})"
            if prof:
                txt += f" — <b>{prof}</b>" + (f" + {', '.join(layers)}" if layers else "")
            txt += f" — {len(grabbed)} grabbed"
            if failed:
                txt += f", <span style='color:#d68910'>{len(failed)} busy</span>"
            self.status_label.setText(txt)
            self.status_label.setToolTip("\n".join(grabbed + [f"busy: {f}" for f in failed])
                                         or "no devices grabbed")
            self.hub.note = ""
        elif ext:
            services = self.engine.services()
            if services:
                self.status_label.setText("<span style='color:#2e9e44'>●</span> Running as system service: "
                                          + ", ".join(f"{u} (pid {p})" for p, u in services.items()))
                self.status_label.setToolTip("Runs as root via systemd with your config. Edits apply live.\n"
                                             "Restart / Stop go through systemctl and ask for your password.")
                self.hub.note = ("all_bind runs as a system service, so inputs on grabbed devices "
                                 "can't be captured here. Use “Show physical input events” after "
                                 "stopping it, or pick the key from the list.")
            else:
                self.status_label.setText("<span style='color:#2e9e44'>●</span> Already running: "
                                          + ", ".join(f"all_bind (pid {p})" for p in ext))
                self.status_label.setToolTip("Started outside this GUI. Edits are applied to it live.\n"
                                             "Restart runs it from the GUI so it can show live input;\n"
                                             "it keeps running after you close the GUI.")
                self.hub.note = ("all_bind is running outside the GUI, so inputs on grabbed devices "
                                 "can't be captured here. Press Restart first.")
        else:
            self.status_label.setText("<span style='color:#c0392b'>○</span> Stopped")
            self.status_label.setToolTip("")
            self.hub.note = ""

        running = child or bool(ext)
        self.engine_btn.setText("Restart" if running else "Start")
        self.stop_btn.setEnabled(running)

    # --- help ---------------------------------------------------------

    def show_headless(self):
        QMessageBox.information(
            self, "Run without the GUI",
            "The engine runs fine on its own and always reads\n"
            f"{CONF_PATH}\n"
            "(it reloads by itself whenever the GUI or CLI saves).\n\n"
            "At login (e.g. Hyprland):   exec-once = all_bind\n"
            f"Or as a root service:        sudo systemctl enable --now {service_name()}\n\n"
            "    all_bind next / prev     switch profile (with a notification)\n"
            "    all_bind --cli help      configure from a terminal\n"
            "    pkill all_bind           stop (releases devices)\n\n"
            "Starting it replaces any other running all_bind. A device another program "
            "has grabbed (e.g. mouse_invert) shows as busy and is picked up as soon as "
            "it's free.")

    def show_tips(self):
        QMessageBox.information(
            self, "Tips",
            "• Quick add: press the button you want to bind and pick what it does.\n"
            "• Triggers: any key or button on any grabbed device, wheel directions, "
            "and controller d-pad directions.\n"
            "• Turbo: up to thousands of presses per second, optional press length, jitter and burst count.\n"
            "• Macros: tap/press/release keys, exact or randomised delays, mouse moves (instant or "
            "smoothed), scroll, typed text and commands. Record real input with ⏺ Record.\n"
            "• Remap to several keys for combos (Ctrl + C), or to a wheel direction.\n"
            "• “Shift to profile” = layer: hold a button to temporarily use another profile "
            "(e.g. low sensitivity sniper mode).\n"
            "• Outputs are keyboard keys, mouse buttons and the wheel.\n"
            "• Turn on “Show physical input events” to see what each button sends.")

    # --- close --------------------------------------------------------

    def closeEvent(self, e):
        if self.save_timer.isActive():
            self.save_timer.stop()
            self._save_now()
        if self.engine.child_running():
            if self.cfg.get("keep_running_on_close", True):
                self.engine.detach()
            else:
                self.engine.stop()
        self.hub.stop_flag.set()
        super().closeEvent(e)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("all_bind")
    app.setDesktopFileName("all_bind")
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
