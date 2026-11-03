# MIT License
#
# Copyright (c) 2026 <YOUR NAME OR ORGANIZATION>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""
ForgeFN - Fortnite Performance Engine & Game Optimizer

A transparent, reversible performance tuner for Fortnite on any Windows 10 / 11 PC:
stock Home / Pro or custom debloated builds, desktop or laptop, NVIDIA / AMD / Intel graphics.

Run from source:  pip install customtkinter psutil   then   python forgefn.py
Build a .exe:     build_exe.bat  (PyInstaller, single file, embedded UAC administrator manifest)

Design rules
------------
1. Snapshot first, change second. Every tweak stores the ORIGINAL value in
   %ProgramData%\\ForgeFN\\state.json before it changes anything; "Undo" restores that exact value.
2. Real changes only. Registry values, powercfg settings, services, the pagefile and Fortnite's
   GameUserSettings.ini are changed by real code. Nothing here is "instructions only".
3. Hardware-aware. Tweaks that do not apply to this PC (Intel graphics policy on an NVIDIA-only
   machine, laptop clock clamps on a desktop, ...) are hidden and refused if requested.
4. Anti-cheat safe. ForgeFN never modifies game or anti-cheat files, injects code, hooks DLLs,
   touches game memory or opens a handle to the game process. It only uses standard Windows
   settings and Fortnite's own GameUserSettings.ini. See the "Backup & Safety" tab.
5. Tweaks marked OPTIONAL are never part of the 1-Click profile.

Module layout (single file, top to bottom)
    1. constants + elevation          5. tweak classes (Reg / Power / Service / Custom)
    2. pure logic (unit-testable)     6. tweak library
    3. Windows helpers + state store  7. Fortnite config, cleaner and backup logic
    4. privileged native actions      8. GUI (ForgeApp)

ForgeFN is an independent open-source project. It is not affiliated with, endorsed by or
sponsored by Epic Games, Easy Anti-Cheat or BattlEye. "Fortnite" is a trademark of Epic Games, Inc.
"""
from __future__ import annotations

import sys

if sys.platform != "win32":
    print("ForgeFN is a Windows application (Windows 10 / 11). Please run it on Windows.")
    sys.exit(1)

import ctypes
import io
import json
import os
import queue
import re
import shutil
import stat
import subprocess
import threading
import time
import winreg
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

APP_NAME = "ForgeFN"
APP_TAGLINE = "Fortnite Performance Engine & Game Optimizer"
APP_VERSION = "2.0.0"
FN_PROC = "FortniteClient-Win64-Shipping.exe"
CREATE_NO_WINDOW = 0x08000000

# ------------------------------------------------------------------ dependencies + admin
try:
    import customtkinter as ctk
    import psutil
    import tkinter as tk
    from tkinter import messagebox
except ImportError as _e:
    ctypes.windll.user32.MessageBoxW(
        0, f"Missing Python package: {_e.name}\n\nOpen a terminal and run:\n\n    pip install customtkinter psutil",
        APP_NAME, 0x10)
    sys.exit(1)


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin() -> bool:
    exe = sys.executable
    if getattr(sys, "frozen", False):
        params = subprocess.list2cmdline(sys.argv[1:])
    else:
        if exe.lower().endswith("python.exe"):
            pyw = exe[:-10] + "pythonw.exe"
            if os.path.exists(pyw):
                exe = pyw
        params = subprocess.list2cmdline([os.path.abspath(sys.argv[0])] + sys.argv[1:])
    return ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1) > 32


if not is_admin():
    if not relaunch_as_admin():
        ctypes.windll.user32.MessageBoxW(0, "Administrator rights are required to apply system tweaks.", APP_NAME, 0x10)
    sys.exit(0)



# ------------------------------------------------------------------ constants
SYS_DRIVE = os.environ.get("SystemDrive", "C:")
DATA_DIR = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "ForgeFN")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
BACKUP_ROOT = os.path.join(os.environ.get("USERPROFILE", r"C:\Users\Public"), "ForgeFN_Backups")

# Builds before 2.0 shipped under an older name. Their original-value snapshots are imported ONCE
# (see StateStore) so "Undo" keeps working for anyone upgrading.
LEGACY_DATA_DIR = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "MacForgeFN")
LEGACY_STATE_IDS = {"pagefile_12gb": "pagefile_dynamic"}
BAK_SUFFIX = ".forgefn.bak"
LEGACY_BAK_SUFFIX = ".macforge.bak"

FN_INI = os.path.expandvars(r"%LOCALAPPDATA%\FortniteGame\Saved\Config\WindowsClient\GameUserSettings.ini")
FN_SEC = "/Script/FortniteGame.FortGameUserSettings"
FN_SG = "ScalabilityGroups"

C_BG, C_SIDE, C_CARD, C_CARD2 = "#0B0E14", "#10141D", "#161B26", "#1E2535"
C_ACC, C_ACC_H, C_PUR = "#00F0FF", "#00B8C4", "#7928CA"
C_OK, C_BAD, C_WARN = "#00E676", "#FF3366", "#FFB020"
C_TXT, C_MUT = "#E6EDF3", "#8B949E"

# <<PURE-START>>  (logic with no Windows dependency - unit-testable)
GUID_RE = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def parse_power_index(text: str):
    """Last two 0x%08x values in `powercfg /query <scheme> <sub> <setting>` are AC then DC (locale independent)."""
    hits = re.findall(r"0x[0-9a-fA-F]{8}", text)
    if len(hits) < 2:
        return None
    return int(hits[-2], 16), int(hits[-1], 16)


class IniEditor:
    """Section-aware editor for Unreal-style .ini files (keeps comments, ordering and line endings)."""

    def __init__(self, path: str):
        self.path = path
        self.lines: list[str] = []
        self.eol = "\r\n"

    def load_text(self, text: str):
        self.eol = "\r\n" if "\r\n" in text else "\n"
        self.lines = io.StringIO(text, newline="").readlines()

    def load(self):
        with open(self.path, "rb") as f:
            self.load_text(f.read().decode("utf-8-sig", errors="replace"))

    def text(self) -> str:
        return "".join(self.lines)

    def save(self):
        with open(self.path, "w", encoding="utf-8", newline="") as f:
            f.write(self.text())

    def set(self, section: str, key: str, value: str):
        header = f"[{section}]".lower()
        start = next((i for i, ln in enumerate(self.lines) if ln.strip().lower() == header), None)
        new_line = f"{key}={value}{self.eol}"
        if start is None:
            if self.lines and not self.lines[-1].endswith(("\n", "\r")):
                self.lines[-1] += self.eol
            if self.lines and self.lines[-1].strip():
                self.lines.append(self.eol)
            self.lines += [f"[{section}]{self.eol}", new_line]
            return
        end = len(self.lines)
        for j in range(start + 1, len(self.lines)):
            if self.lines[j].lstrip().startswith("["):
                end = j
                break
        pat = re.compile(rf"^\s*{re.escape(key)}\s*=", re.IGNORECASE)
        hit = False
        for j in range(start + 1, end):
            if pat.match(self.lines[j]):
                self.lines[j] = new_line
                hit = True
        if hit:
            return
        k = end
        while k - 1 > start and not self.lines[k - 1].strip():
            k -= 1
        if not self.lines[k - 1].endswith(("\n", "\r")):
            self.lines[k - 1] += self.eol
        self.lines.insert(k, new_line)


def aslist(x):
    if x is None:
        return []
    return x if isinstance(x, list) else [x]



# ------------------------------------------------------------------ hardware model
GPU_VENDOR_BY_PCI = {"10DE": "NVIDIA", "1002": "AMD", "1022": "AMD", "8086": "Intel"}
VIRTUAL_GPU_HINTS = ("microsoft basic", "microsoft remote", "remote display", "virtual", "vmware", "hyper-v",
                     "parsec", "indirect display", "displaylink", "citrix")
# SMBIOS chassis types as reported by Win32_SystemEnclosure.ChassisTypes
LAPTOP_CHASSIS = frozenset({8, 9, 10, 11, 14, 30, 31, 32})
DESKTOP_CHASSIS = frozenset({3, 4, 5, 6, 7, 13, 15, 16, 23, 24, 34, 35, 36})


@dataclass
class GpuInfo:
    """One video adapter as reported by Windows."""
    name: str
    vendor: str = "Unknown"      # "NVIDIA" | "AMD" | "Intel" | "Unknown"
    driver: str = ""
    working: bool = True         # False when Device Manager reports a problem code for the adapter


def classify_gpu_vendor(name: str, pnp_id: str = "", compat: str = "") -> str:
    """Vendor from the PCI vendor id in the PNP id (reliable), falling back to name matching."""
    m = re.search(r"VEN_([0-9A-Fa-f]{4})", pnp_id or "")
    if m and m.group(1).upper() in GPU_VENDOR_BY_PCI:
        return GPU_VENDOR_BY_PCI[m.group(1).upper()]
    text = f"{name} {compat}".lower()
    if re.search(r"nvidia|geforce|quadro|\brtx\b|\bgtx\b", text):
        return "NVIDIA"
    if re.search(r"\bamd\b|radeon|\bati\b", text):
        return "AMD"
    if re.search(r"\bintel\b|\biris\b|\buhd graphics\b|\bhd graphics\b|\barc\b", text):
        return "Intel"
    return "Unknown"


def is_virtual_gpu(name: str) -> bool:
    low = (name or "").lower()
    return any(h in low for h in VIRTUAL_GPU_HINTS)


def classify_chassis(chassis_types, has_battery: bool):
    """Return (is_laptop, reason). Chassis type wins; battery presence is the fallback.

    A battery alone is not trusted on desktops because UPS units can show up as batteries.
    """
    types = set()
    for c in aslist(chassis_types):
        try:
            types.add(int(c))
        except (TypeError, ValueError):
            pass
    if types & LAPTOP_CHASSIS:
        return True, "chassis type: portable"
    if types & DESKTOP_CHASSIS:
        return False, "chassis type: desktop"
    return (True, "battery detected") if has_battery else (False, "no battery detected")


def media_is_ssd(media, bus) -> Optional[bool]:
    """True / False for SSD / HDD, None when Windows does not say."""
    m, b = str(media or "").strip().lower(), str(bus or "").strip().lower()
    if m in ("ssd", "4", "scm", "5") or b in ("nvme", "17"):
        return True
    if m in ("hdd", "3"):
        return False
    return None


def _gpu_rank(g: GpuInfo) -> int:
    """Lower = more likely to be the GPU Fortnite renders on (heuristic: discrete beats integrated)."""
    n = g.name.lower()
    if g.vendor == "Intel":
        return 0 if re.search(r"\barc\b", n) else 3
    if g.vendor == "NVIDIA":
        return 1
    if g.vendor == "AMD":
        return 1 if re.search(r"\brx\b|\bpro w", n) else 2
    return 4


@dataclass
class SystemProfile:
    """Everything the UI needs to know about this PC. Built by build_profile() from the CIM scan."""
    manufacturer: str = ""
    model: str = ""
    cpu_name: str = "Unknown CPU"
    cpu_ghz: float = 0.0
    cores: int = 0
    threads: int = 0
    ram_bytes: int = 0
    ram_speed: int = 0
    gpus: list = field(default_factory=list)
    is_laptop: bool = False
    form_reason: str = ""
    system_ssd: Optional[bool] = None       # None = unknown
    os_caption: str = "Windows"
    os_build: int = 0
    os_arch: str = ""
    disks: list = field(default_factory=list)   # [(model, size_bytes)]

    @property
    def ram_gb(self) -> float:
        return self.ram_bytes / 2**30

    @property
    def form_factor(self) -> str:
        return "Laptop" if self.is_laptop else "Desktop"

    def has_gpu(self, vendor: str) -> bool:
        """True when a WORKING adapter of that vendor is present (a driver-error adapter does not count)."""
        return any(g.vendor == vendor and g.working for g in self.gpus)

    @property
    def vendors(self) -> list:
        out = []
        for g in self.gpus:
            if g.working and g.vendor != "Unknown" and g.vendor not in out:
                out.append(g.vendor)
        return out

    @property
    def primary_gpu(self) -> Optional[GpuInfo]:
        pool = [g for g in self.gpus if g.working] or list(self.gpus)
        return min(pool, key=_gpu_rank) if pool else None

    def summary_line(self) -> str:
        dev = f"{self.manufacturer} {self.model}".strip() or "Generic PC"
        gpu = " + ".join(self.vendors) if self.vendors else "unknown GPU"
        return f"{self.form_factor} · {dev} · {gpu} · {self.ram_gb:.0f} GB RAM"


def short_name(name: str, limit: int = 28) -> str:
    """'Intel(R) Core(TM) i5-8259U CPU @ 2.30GHz' -> 'Intel Core i5-8259U'."""
    n = re.sub(r"\((?:R|TM)\)", "", name or "", flags=re.IGNORECASE)
    n = re.sub(r"\s+CPU\s*@.*$", "", n)
    n = re.sub(r"\s+\d+-Core Processor$", "", n)
    n = re.sub(r"\s+Processor$", "", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n if len(n) <= limit else n[: limit - 1].rstrip() + "…"


def build_profile(j: dict) -> SystemProfile:
    """Turn the PowerShell/CIM JSON into a SystemProfile. Every field is optional - missing data never raises."""
    cs = j.get("cs") or {}
    cpu = (aslist(j.get("cpu")) or [{}])[0] or {}
    osj = j.get("os") or {}
    p = SystemProfile()
    p.manufacturer = (cs.get("Manufacturer") or "").strip()
    p.model = (cs.get("Model") or "").strip()
    p.cpu_name = re.sub(r"\s+", " ", cpu.get("Name") or "Unknown CPU").strip()
    maxc = cpu.get("MaxClockSpeed")
    p.cpu_ghz = (maxc / 1000.0) if maxc else 0.0
    p.cores = int(cpu.get("NumberOfCores") or 0)
    p.threads = int(cpu.get("NumberOfLogicalProcessors") or 0)
    p.ram_bytes = int(cs.get("TotalPhysicalMemory") or 0)
    p.ram_speed = next((int(r.get("ConfiguredClockSpeed") or r.get("Speed")) for r in aslist(j.get("ram"))
                        if r and (r.get("ConfiguredClockSpeed") or r.get("Speed"))), 0)
    for g in aslist(j.get("gpu")):
        name = (g or {}).get("Name")
        if not name or is_virtual_gpu(name):
            continue
        p.gpus.append(GpuInfo(
            name=str(name).strip(),
            vendor=classify_gpu_vendor(name, g.get("PNPDeviceID") or "", g.get("AdapterCompatibility") or ""),
            driver=str(g.get("DriverVersion") or ""),
            working=g.get("ConfigManagerErrorCode") in (0, None)))
    p.is_laptop, p.form_reason = classify_chassis(j.get("chassis"), bool(aslist(j.get("battery"))))
    sd = (aslist(j.get("sysdisk")) or [None])[0] or {}
    p.system_ssd = media_is_ssd(sd.get("MediaType"), sd.get("BusType"))
    p.os_caption = (osj.get("Caption") or "Windows").strip()
    p.os_arch = osj.get("OSArchitecture") or ""
    try:
        p.os_build = int(osj.get("BuildNumber") or 0)
    except (TypeError, ValueError):
        p.os_build = 0
    p.disks = [((d or {}).get("Model") or "?", int((d or {}).get("Size") or 0)) for d in aslist(j.get("disk"))]
    return p


def profile_rows(p: SystemProfile, display=None, dpi_pct=None):
    """Label / value rows for the Hardware Scanner page."""
    rows = [("Device", f"{p.manufacturer} {p.model}".strip() or "Unknown"),
            ("Form factor", f"{p.form_factor}  ({p.form_reason})" if p.form_reason else p.form_factor),
            ("Processor", f"{p.cpu_name}  (rated {p.cpu_ghz:.2f} GHz)" if p.cpu_ghz else p.cpu_name),
            ("Cores / Threads", f"{p.cores or '?'} cores / {p.threads or '?'} threads")]
    if p.gpus:
        best = p.primary_gpu
        for i, g in enumerate(p.gpus):
            tag = "   [likely render GPU]" if (g is best and len(p.gpus) > 1) else ""
            drv = f"   (driver {g.driver})" if g.driver else ""
            bad = "" if g.working else "   ⚠ driver problem"
            rows.append(("Graphics" if i == 0 else "", f"{g.vendor} · {g.name}{drv}{tag}{bad}"))
    else:
        rows.append(("Graphics", "Unknown"))
    ram = f"{p.ram_gb:.1f} GB usable" if p.ram_bytes else "Unknown"
    if p.ram_speed:
        ram += f" @ {p.ram_speed} MHz"
    rows.append(("Memory", ram))
    if p.disks:
        rows.append(("Storage", ", ".join(f"{m} ({s / 1e9:.0f} GB)" for m, s in p.disks)))
    kind = {True: "SSD / NVMe", False: "HDD"}.get(p.system_ssd, "type not reported")
    rows.append(("System drive", kind))
    rows.append(("Windows", f"{p.os_caption} {p.os_arch} (build {p.os_build or '?'})".replace("  ", " ")))
    if display is not None and display.width:
        t = f"{display.width} x {display.height}"
        if display.refresh:
            t += f" @ {display.refresh} Hz"
        t += f"   ({display.aspect}" + (f", Windows scaling {dpi_pct}%" if dpi_pct else "") + ")"
        rows.append(("Display", t))
    return rows


VENDOR_TIPS = {
    "NVIDIA": "NVIDIA Control Panel > Manage 3D settings: set Power management mode to 'Prefer maximum performance' for Fortnite.",
    "AMD": "AMD Software > Gaming > Graphics: keep Radeon Chill and Radeon Boost off for consistent frame times.",
    "Intel": "Intel Graphics Command Center > System > Power: choose Maximum Performance while plugged in, and keep the driver current.",
}


def vendor_tips(p: SystemProfile) -> list:
    """Advice lines shown on the scanner page. ForgeFN does NOT apply any of these itself."""
    tips = [f"{v}: {VENDOR_TIPS[v]}" for v in ("NVIDIA", "AMD", "Intel") if p.has_gpu(v)]
    if p.is_laptop:
        tips.append("Laptop: play plugged in and set the Windows power mode to 'Best performance' while on AC.")
    return tips or ["No supported GPU vendor detected - keep your graphics driver up to date."]


# ------------------------------------------------------------------ dynamic pagefile sizing
@dataclass(frozen=True)
class PagefilePlan:
    mode: str                 # "static" | "range" | "system"
    initial_mb: int
    max_mb: int
    required_free_bytes: int  # free space needed on the system drive (pagefile max + 2 GB headroom)
    summary: str


def plan_pagefile(ram_bytes: int) -> PagefilePlan:
    """Pick a pagefile strategy from installed RAM.

    <= 8 GB   fixed 1.5x RAM (8 GB -> 12 GB), non-resizing, so low-RAM PCs get a big overflow area
    9-20 GB   8 GB initial, growing to 12 GB (covers the 16 GB class)
    > 20 GB   keep it system-managed - there is nothing useful to gain (the 32 GB+ class)
    """
    gb = max(1, int(round(ram_bytes / 2**30)))
    if gb <= 8:
        size = max(4096, int(gb * 1.5 * 1024))
        return PagefilePlan("static", size, size, size * 2**20 + 2 * 2**30,
                            f"{gb} GB RAM -> fixed {size / 1024:g} GB pagefile (1.5x RAM, never resizes).")
    if gb <= 20:
        return PagefilePlan("range", 8192, 12288, 12288 * 2**20 + 2 * 2**30,
                            f"{gb} GB RAM -> 8 GB pagefile that can grow to 12 GB.")
    return PagefilePlan("system", 0, 0, 0,
                        f"{gb} GB RAM is plenty: keep the pagefile system-managed (or at most a small fixed one). No change made.")


# ------------------------------------------------------------------ display + resolution presets
_ASPECTS = (("16:9", 16 / 9), ("16:10", 16 / 10), ("21:9", 21 / 9), ("32:9", 32 / 9),
            ("4:3", 4 / 3), ("5:4", 5 / 4), ("3:2", 3 / 2))
PRESET_TABLE = {
    "16:9": [(1280, 720), (1600, 900), (1920, 1080), (2560, 1440), (3840, 2160)],
    "16:10": [(1152, 720), (1280, 800), (1440, 900), (1680, 1050), (1920, 1200), (2560, 1600), (2880, 1800)],
    "21:9": [(2560, 1080), (3440, 1440), (3840, 1600), (5120, 2160)],
    "32:9": [(3840, 1080), (5120, 1440)],
    "4:3": [(800, 600), (1024, 768), (1280, 960), (1400, 1050), (1600, 1200)],
    "5:4": [(1280, 1024)],
    "3:2": [(1800, 1200), (2256, 1504), (2736, 1824), (3000, 2000)],
}
STRETCHED_4_3 = [(1024, 768), (1280, 960), (1440, 1080), (1600, 1200)]


def classify_aspect(w: int, h: int) -> str:
    """'16:9', '16:10', '21:9', '32:9', '4:3', '5:4', '3:2', 'custom' (or 'unknown' without a size)."""
    if not w or not h:
        return "unknown"
    r = w / h
    name, val = min(_ASPECTS, key=lambda a: abs(a[1] - r))
    return name if abs(val - r) / val <= 0.04 else "custom"


@dataclass(frozen=True)
class ResPreset:
    w: int
    h: int
    label: str


@dataclass
class ResolutionPresets:
    aspect: str
    main: list
    stretched: list


def _even(x: float) -> int:
    return int(round(x / 2.0) * 2)


def _res_label(w: int, h: int, native: bool, note: str = "") -> str:
    base = f"{h}p  ·  {w}x{h}" if w * 9 == h * 16 else f"{w} x {h}"
    return base + ("  (native)" if native else "") + note


def build_resolution_presets(native_w: int, native_h: int, supported=None) -> ResolutionPresets:
    """Downscale presets that match the monitor's own aspect ratio.

    `supported` (a set of (w, h) display modes from Windows) filters out modes the display cannot
    actually run. The native mode is always offered. 4:3 "stretched" presets are only offered when
    the display driver exposes them.
    """
    note = ""
    if not native_w or not native_h:
        native_w, native_h, supported, note = 1920, 1080, None, "  (assumed - detection failed)"
    aspect = classify_aspect(native_w, native_h)
    cands = [m for m in PRESET_TABLE.get(aspect, []) if m[0] <= native_w and m[1] <= native_h]
    if len(cands) < 3:  # unusual shape or tiny ladder: add scaled copies of the native shape
        cands += [(_even(native_w * f), _even(native_h * f)) for f in (2 / 3, 5 / 6)]
    cands.append((native_w, native_h))
    main = []
    for w, h in sorted(set(cands), key=lambda m: (m[0] * m[1], m[0])):
        is_native = (w, h) == (native_w, native_h)
        if supported and not is_native and (w, h) not in supported:
            continue
        main.append(ResPreset(w, h, _res_label(w, h, is_native, note if is_native else "")))
    have = {(p.w, p.h) for p in main}
    stretched = []
    if supported and aspect in ("16:9", "16:10", "21:9"):
        stretched = [ResPreset(w, h, f"{w} x {h}  (4:3 stretched)") for (w, h) in STRETCHED_4_3
                     if w <= native_w and h <= native_h and (w, h) in supported and (w, h) not in have]
    return ResolutionPresets(aspect, main, stretched)


def fps_cap_options(refresh_hz) -> list:
    """Frame-cap choices up to the monitor's refresh rate (always includes the refresh rate itself)."""
    hz = int(refresh_hz or 60)
    opts = {c for c in (30, 45, 60, 90, 120, 144, 165, 240) if c <= max(hz, 60)}
    if hz > 60:
        opts.add(hz)
    return sorted(opts)


@dataclass
class DisplayInfo:
    """Primary monitor: native mode, refresh rate and every mode the driver offers."""
    width: int = 0
    height: int = 0
    refresh: int = 0
    supported: set = field(default_factory=set)

    @property
    def aspect(self) -> str:
        return classify_aspect(self.width, self.height)

    def presets(self) -> ResolutionPresets:
        return build_resolution_presets(self.width, self.height, self.supported or None)
# <<PURE-END>>


# ------------------------------------------------------------------ process / registry helpers
def run(args, timeout=120):
    """Run a command hidden. Returns (returncode, combined output)."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW, errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 1, "timed out"
    except Exception as e:  # noqa: BLE001
        return 1, str(e)


def ps(script: str, timeout=120):
    return run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                "$ErrorActionPreference='Stop'; " + script], timeout)


HIVES = {"HKLM": winreg.HKEY_LOCAL_MACHINE, "HKCU": winreg.HKEY_CURRENT_USER}
DW, SZ, MSZ = winreg.REG_DWORD, winreg.REG_SZ, winreg.REG_MULTI_SZ


def reg_read(hive, path, name):
    try:
        with winreg.OpenKey(HIVES[hive], path, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            data, typ = winreg.QueryValueEx(k, name)
            return {"exists": True, "type": typ, "data": data}
    except OSError:
        return {"exists": False}


def reg_write(hive, path, name, typ, value):
    with winreg.CreateKeyEx(HIVES[hive], path, 0, winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as k:
        winreg.SetValueEx(k, name, 0, typ, value)


def reg_delete(hive, path, name):
    try:
        with winreg.OpenKey(HIVES[hive], path, 0, winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as k:
            winreg.DeleteValue(k, name)
    except FileNotFoundError:
        pass


def reg_restore(hive, path, name, entry):
    if entry and entry.get("exists"):
        reg_write(hive, path, name, entry["type"], entry["data"])
    else:
        reg_delete(hive, path, name)



# ------------------------------------------------------------------ process list (read-only, no process handles)
TH32CS_SNAPPROCESS = 0x00000002


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260)]


def list_process_names() -> set:
    """Lower-cased image names of every running process, from a ToolHelp snapshot.

    Anti-cheat note: this reads the system process table only. ForgeFN never opens a handle to
    Fortnite, EasyAntiCheat or BattlEye processes - not even to read their name.
    """
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32FirstW.restype = wintypes.BOOL
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    names: set = set()
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap in (None, 0, ctypes.c_void_p(-1).value):
        return names
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            names.add(entry.szExeFile.lower())
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return names


def fortnite_running() -> bool:
    try:
        return FN_PROC.lower() in list_process_names()
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------ display detection
class DEVMODEW(ctypes.Structure):
    """Win32 DEVMODEW (display variant). sizeof == 220 on Windows."""
    _fields_ = [("dmDeviceName", wintypes.WCHAR * 32), ("dmSpecVersion", wintypes.WORD),
                ("dmDriverVersion", wintypes.WORD), ("dmSize", wintypes.WORD), ("dmDriverExtra", wintypes.WORD),
                ("dmFields", wintypes.DWORD), ("dmPositionX", ctypes.c_long), ("dmPositionY", ctypes.c_long),
                ("dmDisplayOrientation", wintypes.DWORD), ("dmDisplayFixedOutput", wintypes.DWORD),
                ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short), ("dmYResolution", ctypes.c_short),
                ("dmTTOption", ctypes.c_short), ("dmCollate", ctypes.c_short), ("dmFormName", wintypes.WCHAR * 32),
                ("dmLogPixels", wintypes.WORD), ("dmBitsPerPel", wintypes.DWORD), ("dmPelsWidth", wintypes.DWORD),
                ("dmPelsHeight", wintypes.DWORD), ("dmDisplayFlags", wintypes.DWORD),
                ("dmDisplayFrequency", wintypes.DWORD), ("dmICMMethod", wintypes.DWORD),
                ("dmICMIntent", wintypes.DWORD), ("dmMediaType", wintypes.DWORD), ("dmDitherType", wintypes.DWORD),
                ("dmReserved1", wintypes.DWORD), ("dmReserved2", wintypes.DWORD),
                ("dmPanningWidth", wintypes.DWORD), ("dmPanningHeight", wintypes.DWORD)]


ENUM_CURRENT_SETTINGS = 0xFFFFFFFF


def detect_display() -> DisplayInfo:
    """Native mode, refresh rate and supported modes of the primary display (physical pixels, DPI-proof)."""
    info = DisplayInfo()
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.EnumDisplaySettingsW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(DEVMODEW)]
        user32.EnumDisplaySettingsW.restype = wintypes.BOOL

        def mode(index):
            dm = DEVMODEW()
            dm.dmSize = ctypes.sizeof(DEVMODEW)
            return dm if user32.EnumDisplaySettingsW(None, index, ctypes.byref(dm)) else None

        cur = mode(ENUM_CURRENT_SETTINGS)
        if cur:
            info.width, info.height, info.refresh = int(cur.dmPelsWidth), int(cur.dmPelsHeight), int(cur.dmDisplayFrequency)
        for i in range(4000):
            m = mode(i)
            if m is None:
                break
            info.supported.add((int(m.dmPelsWidth), int(m.dmPelsHeight)))
    except Exception:  # noqa: BLE001
        pass
    if not info.width:  # last resort; may be DPI-virtualised
        try:
            info.width = int(ctypes.windll.user32.GetSystemMetrics(0))
            info.height = int(ctypes.windll.user32.GetSystemMetrics(1))
        except Exception:  # noqa: BLE001
            pass
    return info


def fallback_profile() -> SystemProfile:
    """Minimal profile used when the CIM scan fails: RAM and battery presence still come from psutil."""
    p = SystemProfile()
    try:
        p.ram_bytes = int(psutil.virtual_memory().total)
    except Exception:  # noqa: BLE001
        pass
    try:
        p.is_laptop = psutil.sensors_battery() is not None
    except Exception:  # noqa: BLE001
        p.is_laptop = False
    p.form_reason = "battery detected" if p.is_laptop else "no battery detected"
    return p


# ------------------------------------------------------------------ state store (original values for Undo)
class StateStore:
    """Persists the ORIGINAL value of everything a tweak changes so Undo can restore it exactly."""

    def __init__(self):
        self._lock = threading.RLock()
        self.data: dict = {}
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
        except OSError:
            pass
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception:  # noqa: BLE001
                self.data = {}
        else:
            self._import_legacy()

    def _import_legacy(self):
        """One-time import of a pre-2.0 state.json so Undo still works after upgrading."""
        try:
            with open(os.path.join(LEGACY_DATA_DIR, "state.json"), "r", encoding="utf-8") as f:
                old = json.load(f)
        except Exception:  # noqa: BLE001
            return
        if isinstance(old, dict) and old:
            self.data = {LEGACY_STATE_IDS.get(k, k): v for k, v in old.items()}
            self._save()

    def _save(self):
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=1)
            os.replace(tmp, STATE_FILE)
        except Exception as e:  # noqa: BLE001
            print("state save failed:", e)

    def is_applied(self, tid):
        return bool(self.data.get(tid, {}).get("applied"))

    def snapshot(self, tid):
        return self.data.get(tid, {}).get("snapshot")

    def mark(self, tid, snapshot):
        with self._lock:
            self.data[tid] = {"applied": True, "snapshot": snapshot, "time": datetime.now().isoformat(timespec="seconds")}
            self._save()

    def clear(self, tid):
        with self._lock:
            self.data.pop(tid, None)
            self._save()

    def applied_ids(self):
        return [k for k, v in self.data.items() if v.get("applied")]


# ------------------------------------------------------------------ privileges / native actions
class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]


class TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [("PrivilegeCount", wintypes.DWORD), ("Privileges", LUID_AND_ATTRIBUTES * 1)]


def enable_privilege(name: str) -> bool:
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    adv.OpenProcessToken.restype = wintypes.BOOL
    adv.LookupPrivilegeValueW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(LUID)]
    adv.LookupPrivilegeValueW.restype = wintypes.BOOL
    adv.AdjustTokenPrivileges.argtypes = [wintypes.HANDLE, wintypes.BOOL, ctypes.POINTER(TOKEN_PRIVILEGES),
                                          wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
    adv.AdjustTokenPrivileges.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    if not adv.OpenProcessToken(k32.GetCurrentProcess(), 0x0028, ctypes.byref(token)):  # ADJUST_PRIVILEGES|QUERY
        return False
    try:
        tp = TOKEN_PRIVILEGES()
        tp.PrivilegeCount = 1
        if not adv.LookupPrivilegeValueW(None, name, ctypes.byref(tp.Privileges[0].Luid)):
            return False
        tp.Privileges[0].Attributes = 0x00000002  # SE_PRIVILEGE_ENABLED
        ctypes.set_last_error(0)
        adv.AdjustTokenPrivileges(token, False, ctypes.byref(tp), 0, None, None)
        return ctypes.get_last_error() == 0  # 1300 = not all assigned
    finally:
        k32.CloseHandle(token)


def purge_standby_list():
    """Real standby-list purge (NtSetSystemInformation / SystemMemoryListInformation / MemoryPurgeStandbyList)."""
    before = psutil.virtual_memory().available
    if not enable_privilege("SeProfileSingleProcessPrivilege"):
        return False, "Could not enable SeProfileSingleProcessPrivilege (run as Administrator)."
    nt = ctypes.WinDLL("ntdll")
    nt.NtSetSystemInformation.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]
    nt.NtSetSystemInformation.restype = ctypes.c_long
    cmd = ctypes.c_int(4)  # MemoryPurgeStandbyList
    status = nt.NtSetSystemInformation(0x50, ctypes.byref(cmd), ctypes.sizeof(cmd))
    if status != 0:
        return False, f"NtSetSystemInformation failed (0x{status & 0xFFFFFFFF:08X})."
    time.sleep(0.6)
    after = psutil.virtual_memory().available
    return True, f"Standby list purged. Available RAM {before / 2**30:.2f} -> {after / 2**30:.2f} GB."


def set_mouse_params(t1: int, t2: int, accel: int) -> bool:
    arr = (ctypes.c_int * 3)(t1, t2, accel)
    # SPI_SETMOUSE, SPIF_UPDATEINIFILE | SPIF_SENDCHANGE -> applied instantly and persisted
    return bool(ctypes.windll.user32.SystemParametersInfoW(0x0004, 0, arr, 3))


# ------------------------------------------------------------------ power helpers
def active_scheme():
    rc, out = run(["powercfg", "/getactivescheme"])
    m = GUID_RE.search(out) if rc == 0 else None
    return m.group(0) if m else None


def get_power_index(scheme, sub, setting):
    rc, out = run(["powercfg", "/query", scheme, sub, setting])
    return parse_power_index(out) if rc == 0 else None


def scheme_exists(scheme) -> bool:
    return run(["powercfg", "/query", scheme])[0] == 0



# ------------------------------------------------------------------ tweak classes
class Tweak:
    """Base class for every tweak.

    impact    "Low" | "Medium" | "High" - badge only
    reboot    needs a restart to take effect
    rec       True / False / callable(profile) - part of the 1-Click profile (False = OPTIONAL)
    requires  callable(profile) -> bool. When False the tweak is hidden and apply is refused.
    scope     short badge text when requires is set (e.g. "Laptops")
    desc_fn   callable(profile) -> str for hardware-specific descriptions
    probe     callable() -> str showing the live state ("Currently: ON")
    legacy    retired tweak: never applied again, only shown (with Undo) if an old version applied it
    """

    def __init__(self, tid, cat, name, desc, impact="Medium", reboot=False, rec=True,
                 requires: Optional[Callable] = None, scope: str = "", desc_fn: Optional[Callable] = None,
                 probe: Optional[Callable] = None, legacy: bool = False):
        self.id, self.cat, self.name, self.desc = tid, cat, name, desc
        self.impact, self.reboot, self.rec = impact, reboot, rec
        self.requires, self.scope, self.desc_fn, self.probe, self.legacy = requires, scope, desc_fn, probe, legacy

    def applies_to(self, profile) -> bool:
        if self.requires is None:
            return True
        if profile is None:
            return False
        try:
            return bool(self.requires(profile))
        except Exception:  # noqa: BLE001
            return False

    def is_rec(self, profile) -> bool:
        try:
            return bool(self.rec(profile)) if callable(self.rec) else bool(self.rec)
        except Exception:  # noqa: BLE001
            return False

    def describe(self, profile) -> str:
        if self.desc_fn and profile is not None:
            try:
                return self.desc_fn(profile)
            except Exception:  # noqa: BLE001
                pass
        return self.desc

    def apply(self, store):
        raise NotImplementedError

    def undo(self, store):
        raise NotImplementedError


class RegTweak(Tweak):
    """entries: (hive, path, name, type, value, default)  -  default None = delete value when no snapshot exists."""

    def __init__(self, tid, cat, name, desc, entries, optional=(), **kw):
        super().__init__(tid, cat, name, desc, **kw)
        self.entries, self.optional = list(entries), list(optional)

    def apply(self, store):
        allv = self.entries + self.optional
        snap = store.snapshot(self.id)
        if snap is None:
            snap = [reg_read(h, p, n) for (h, p, n, *_r) in allv]
        done, errs = 0, []
        for (h, p, n, t, v, _d) in self.entries:
            try:
                reg_write(h, p, n, t, v)
                done += 1
            except OSError as e:
                errs.append(f"{n}: {e.strerror or e}")
        skipped = 0
        for (h, p, n, t, v, _d) in self.optional:
            try:
                reg_write(h, p, n, t, v)
                done += 1
            except OSError:
                skipped += 1
        if done == 0:
            return False, "Registry write denied - " + "; ".join(errs)
        store.mark(self.id, snap)
        msg = f"Set {done} registry value(s)."
        if skipped:
            msg += f" ({skipped} Windows-protected key skipped.)"
        if errs:
            msg += " Partial: " + "; ".join(errs)
        return True, msg

    def undo(self, store):
        allv = self.entries + self.optional
        snap = store.snapshot(self.id)
        errs = []
        for i, (h, p, n, t, v, d) in enumerate(allv):
            try:
                if snap and i < len(snap):
                    reg_restore(h, p, n, snap[i])
                elif d is None:
                    reg_delete(h, p, n)
                else:
                    reg_write(h, p, n, t, d)
            except OSError as e:
                if (h, p, n, t, v, d) not in self.optional:
                    errs.append(f"{n}: {e.strerror or e}")
        if errs:
            return False, "Could not restore: " + "; ".join(errs)
        store.clear(self.id)
        return True, "Restored original values." if snap else "Restored Windows defaults."


class PowerTweak(Tweak):
    """settings: (subgroup, setting, value). Applied to the active plan; originals (AC and DC) are restored on Undo.

    ac_only=True writes the AC (plugged-in) value only, so a laptop's battery life is untouched.
    """

    def __init__(self, tid, cat, name, desc, settings, ac_only=False, **kw):
        super().__init__(tid, cat, name, desc, **kw)
        self.settings, self.ac_only = settings, ac_only

    def apply(self, store):
        snap = store.snapshot(self.id)
        if snap is None:
            scheme = active_scheme()
            if not scheme:
                return False, "Could not read the active power plan."
            items = []
            for sub, st, _v in self.settings:
                cur = get_power_index(scheme, sub, st)
                if cur is None:
                    return False, "This power setting isn't exposed by the current plan/driver on this PC."
                items.append({"sub": sub, "set": st, "ac": cur[0], "dc": cur[1]})
            snap = {"scheme": scheme, "items": items}
        scheme = snap["scheme"]
        flags = ("/setacvalueindex",) if self.ac_only else ("/setacvalueindex", "/setdcvalueindex")
        for sub, st, val in self.settings:
            for flag in flags:
                rc, out = run(["powercfg", flag, scheme, sub, st, str(val)])
                if rc != 0:
                    return False, f"powercfg failed: {out.strip()[:140]}"
        if scheme == active_scheme():
            run(["powercfg", "/setactive", scheme])
        store.mark(self.id, snap)
        return True, "Applied to the active power plan (" + ("plugged in only)." if self.ac_only else "AC + battery).")

    def undo(self, store):
        snap = store.snapshot(self.id)
        if not snap:
            return True, "Nothing recorded to undo."
        scheme = snap["scheme"]
        if not scheme_exists(scheme):
            store.clear(self.id)
            return True, "That power plan no longer exists, nothing to restore."
        for it in snap["items"]:
            for flag, key in (("/setacvalueindex", "ac"), ("/setdcvalueindex", "dc")):
                rc, out = run(["powercfg", flag, scheme, it["sub"], it["set"], str(it[key])])
                if rc != 0:
                    return False, f"powercfg failed: {out.strip()[:140]}"
        if scheme == active_scheme():
            run(["powercfg", "/setactive", scheme])
        store.clear(self.id)
        return True, "Restored the original power values."


START_NAMES = {0: "boot", 1: "system", 2: "auto", 3: "demand", 4: "disabled"}


class ServiceTweak(Tweak):
    def __init__(self, tid, cat, name, desc, services, defaults, **kw):
        super().__init__(tid, cat, name, desc, **kw)
        self.services, self.defaults = services, defaults

    def apply(self, store):
        snap = store.snapshot(self.id)
        fresh = snap is None
        snap = {} if fresh else snap
        changed, failed, absent = [], [], []
        for svc in self.services:
            key = rf"SYSTEM\CurrentControlSet\Services\{svc}"
            start = reg_read("HKLM", key, "Start")
            if not start["exists"]:
                absent.append(svc)
                continue
            if fresh:
                snap[svc] = {"start": start["data"], "delayed": reg_read("HKLM", key, "DelayedAutostart").get("data", 0)}
            run(["sc", "stop", svc], timeout=40)
            rc, out = run(["sc", "config", svc, "start=", "disabled"])
            (changed if rc == 0 else failed).append(svc if rc == 0 else f"{svc} ({out.strip()[:50]})")
        if failed and not changed:
            return False, "Could not disable: " + "; ".join(failed)
        store.mark(self.id, snap)
        parts = []
        if changed:
            parts.append("Disabled " + ", ".join(changed))
        if absent:
            parts.append("already absent on this build: " + ", ".join(absent))
        if failed:
            parts.append("failed: " + "; ".join(failed))
        return True, ". ".join(parts) + "."

    def undo(self, store):
        snap = store.snapshot(self.id)
        errs = []
        targets = snap if snap else {s: {"start": self.defaults.get(s, 3), "delayed": 1 if self.defaults.get(s) == "delayed" else 0}
                                     for s in self.services if reg_read("HKLM", rf"SYSTEM\CurrentControlSet\Services\{s}", "Start")["exists"]}
        for svc, info in targets.items():
            start = info["start"]
            mode = "delayed-auto" if start == "delayed" or (start == 2 and info.get("delayed") == 1) else START_NAMES.get(start, "demand")
            rc, out = run(["sc", "config", svc, "start=", mode])
            if rc != 0:
                errs.append(f"{svc}: {out.strip()[:60]}")
            elif mode in ("auto", "delayed-auto"):
                run(["sc", "start", svc], timeout=40)
        if errs:
            return False, "; ".join(errs)
        store.clear(self.id)
        return True, "Service start types restored."


class CustomTweak(Tweak):
    def __init__(self, tid, cat, name, desc, apply_fn, undo_fn, **kw):
        super().__init__(tid, cat, name, desc, **kw)
        self._a, self._u = apply_fn, undo_fn

    def apply(self, store):
        return self._a(store)

    def undo(self, store):
        return self._u(store)


# ------------------------------------------------------------------ custom tweak implementations
MM = r"SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management"
SYSPROF = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile"
ULTIMATE = "e9a42b02-d5df-448d-aa00-03f14749eb61"
HIGHPERF = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
LAYERS = r"Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"



# ------------------------------------------------------------------ dynamic pagefile
PAGEFILE_ID = "pagefile_dynamic"


def _pagefile_desc(profile) -> str:
    plan = plan_pagefile(profile.ram_bytes)
    try:
        free = f"{shutil.disk_usage(SYS_DRIVE + chr(92)).free / 2**30:.0f} GB free on {SYS_DRIVE}"
    except OSError:
        free = f"free space on {SYS_DRIVE} unknown"
    tail = "" if plan.mode == "system" else " Free space is checked before anything is changed; activates after a reboot."
    return f"{plan.summary} ({free}.){tail}"


def _pagefile_apply(store):
    plan = plan_pagefile(psutil.virtual_memory().total)
    if plan.mode == "system":
        return True, plan.summary
    snap = store.snapshot(PAGEFILE_ID)
    if snap is None:
        free = shutil.disk_usage(SYS_DRIVE + "\\").free
        if free < plan.required_free_bytes:
            return False, (f"Only {free / 2**30:.1f} GB free on {SYS_DRIVE} - need at least "
                           f"{plan.required_free_bytes / 2**30:.0f} GB for this pagefile.")
        rc, out = ps("(Get-CimInstance Win32_ComputerSystem).AutomaticManagedPagefile")
        snap = {"auto": out.strip().lower() == "true", "paging": reg_read("HKLM", MM, "PagingFiles")}
    rc, out = ps("Get-CimInstance Win32_ComputerSystem | Set-CimInstance -Property @{AutomaticManagedPagefile=$false}")
    if rc != 0:
        return False, f"Could not turn off automatic pagefile management: {out.strip()[:140]}"
    reg_write("HKLM", MM, "PagingFiles", MSZ, [f"{SYS_DRIVE}\\pagefile.sys {plan.initial_mb} {plan.max_mb}"])
    store.mark(PAGEFILE_ID, snap)
    return True, f"{plan.summary} Configured on {SYS_DRIVE}. Reboot to activate."


def _pagefile_undo(store):
    snap = store.snapshot(PAGEFILE_ID) or {"auto": True, "paging": {"exists": True, "type": MSZ, "data": ["?:\\pagefile.sys"]}}
    reg_restore("HKLM", MM, "PagingFiles", snap["paging"])
    if snap["auto"]:
        rc, out = ps("Get-CimInstance Win32_ComputerSystem | Set-CimInstance -Property @{AutomaticManagedPagefile=$true}")
        if rc != 0:
            return False, f"Could not re-enable automatic management: {out.strip()[:140]}"
    store.clear(PAGEFILE_ID)
    return True, "Original pagefile configuration restored. Reboot to activate."


def _standby_apply(store):
    ok, msg = purge_standby_list()
    return ok, msg


def _noop_undo_factory(text):
    return lambda store: (True, text)


def _memcomp_apply(store):
    snap = store.snapshot("mem_compression")
    if snap is None:
        rc, out = ps("(Get-MMAgent).MemoryCompression")
        snap = {"enabled": out.strip().lower() == "true"}
    rc, out = ps("Disable-MMAgent -MemoryCompression")
    if rc != 0:
        return False, f"Disable-MMAgent failed: {out.strip()[:140]}"
    store.mark("mem_compression", snap)
    return True, "Memory compression disabled. Reboot to finish."


def _memcomp_undo(store):
    snap = store.snapshot("mem_compression")
    if snap is None or snap.get("enabled"):
        rc, out = ps("Enable-MMAgent -MemoryCompression")
        if rc != 0:
            return False, f"Enable-MMAgent failed: {out.strip()[:140]}"
    store.clear("mem_compression")
    return True, "Memory compression restored. Reboot to finish."


def _plan_apply(store):
    snap = store.snapshot("power_plan")
    if snap is None:
        prev = active_scheme()
        if not prev:
            return False, "Could not read the active power plan."
        snap = {"prev": prev, "created": None}
        rc, out = run(["powercfg", "-duplicatescheme", ULTIMATE])
        m = GUID_RE.search(out) if rc == 0 else None
        snap["created"] = m.group(0) if m else None
    target = snap["created"] or HIGHPERF
    rc, out = run(["powercfg", "/setactive", target])
    if rc != 0:
        if snap["created"]:
            run(["powercfg", "/delete", snap["created"]])
        return False, f"Could not activate a performance plan (this Windows build may not include them): {out.strip()[:120]}"
    store.mark("power_plan", snap)
    return True, ("Ultimate Performance" if snap["created"] else "High Performance") + " plan activated. Use on AC power."


def _plan_undo(store):
    snap = store.snapshot("power_plan")
    if not snap:
        return True, "Nothing recorded to undo."
    if scheme_exists(snap["prev"]):
        run(["powercfg", "/setactive", snap["prev"]])
    if snap.get("created"):
        run(["powercfg", "/delete", snap["created"]])
    store.clear("power_plan")
    return True, "Previous power plan restored."


def _hib_apply(store):
    snap = store.snapshot("hibernate_off")
    if snap is None:
        snap = {"was": reg_read("HKLM", r"SYSTEM\CurrentControlSet\Control\Power", "HibernateEnabled")}
    rc, out = run(["powercfg", "-h", "off"])
    if rc != 0:
        return False, f"powercfg -h off failed: {out.strip()[:120]}"
    store.mark("hibernate_off", snap)
    return True, "Hibernation off. hiberfil.sys removed (Fast Startup is off too)."


def _hib_undo(store):
    snap = store.snapshot("hibernate_off")
    was_on = snap is None or snap["was"].get("data", 1) == 1
    if was_on:
        rc, out = run(["powercfg", "-h", "on"])
        if rc != 0:
            return False, f"powercfg -h on failed: {out.strip()[:120]}"
    store.clear("hibernate_off")
    return True, "Hibernation restored." if was_on else "Hibernation was off before, left as it was."


MOUSE_KEY = r"Control Panel\Mouse"


def _mouse_apply(store):
    snap = store.snapshot("mouse_accel")
    if snap is None:
        snap = {n: reg_read("HKCU", MOUSE_KEY, n) for n in ("MouseSpeed", "MouseThreshold1", "MouseThreshold2")}
    if not set_mouse_params(0, 0, 0):
        return False, "SystemParametersInfo rejected the change."
    store.mark("mouse_accel", snap)
    return True, "Enhanced Pointer Precision off - active immediately."


def _mouse_undo(store):
    snap = store.snapshot("mouse_accel")
    try:
        if snap and all(snap[n].get("exists") for n in snap):
            a, t1, t2 = (int(snap["MouseSpeed"]["data"]), int(snap["MouseThreshold1"]["data"]), int(snap["MouseThreshold2"]["data"]))
        else:
            a, t1, t2 = 1, 6, 10
    except (ValueError, KeyError):
        a, t1, t2 = 1, 6, 10
    if not set_mouse_params(t1, t2, a):
        return False, "SystemParametersInfo rejected the change."
    store.clear("mouse_accel")
    return True, "Original pointer acceleration restored."


def find_fortnite_exe():
    cands = []
    dat = os.path.expandvars(r"%ProgramData%\Epic\UnrealEngineLauncher\LauncherInstalled.dat")
    try:
        with open(dat, "r", encoding="utf-8") as f:
            for it in json.load(f).get("InstallationList", []):
                if str(it.get("AppName", "")).lower() == "fortnite":
                    cands.append(os.path.join(it["InstallLocation"], "FortniteGame", "Binaries", "Win64", FN_PROC))
    except Exception:  # noqa: BLE001
        pass
    cands.append(os.path.join(r"C:\Program Files\Epic Games\Fortnite\FortniteGame\Binaries\Win64", FN_PROC))
    return next((c for c in cands if os.path.isfile(c)), None)


def _fn_fso_apply(store):
    exe = find_fortnite_exe()
    if not exe:
        return False, "Fortnite install not found. Install it with the Epic Games Launcher first."
    snap = store.snapshot("fn_fso_dpi") or {"exe": exe, "was": reg_read("HKCU", LAYERS, exe)}
    reg_write("HKCU", LAYERS, snap["exe"], SZ, "~ DISABLEDXMAXIMIZEDWINDOWEDMODE HIGHDPIAWARE")
    store.mark("fn_fso_dpi", snap)
    return True, "Fullscreen optimizations disabled and DPI scaling set to 'Application' for Fortnite."


def _fn_fso_undo(store):
    snap = store.snapshot("fn_fso_dpi")
    if snap:
        reg_restore("HKCU", LAYERS, snap["exe"], snap["was"])
    store.clear("fn_fso_dpi")
    return True, "Fortnite compatibility flags restored."



# ------------------------------------------------------------------ vendor-agnostic graphics / Game Mode checks
GFX = r"SYSTEM\CurrentControlSet\Control\GraphicsDrivers"
GAMEBAR = r"Software\Microsoft\GameBar"
HAGS_ID = "hags_on"


def _hags_probe() -> str:
    cur = reg_read("HKLM", GFX, "HwSchMode")
    if not cur["exists"]:
        return "Not exposed by this GPU / driver / Windows build"
    return "Currently: ON" if cur["data"] == 2 else "Currently: OFF"


def _hags_apply(store):
    cur = reg_read("HKLM", GFX, "HwSchMode")
    if not cur["exists"]:
        return False, "Not available: needs Windows 10 2004+ and a GPU driver that supports it. Nothing changed."
    snap = store.snapshot(HAGS_ID) or {"was": cur}
    reg_write("HKLM", GFX, "HwSchMode", DW, 2)
    store.mark(HAGS_ID, snap)
    return True, "Hardware-accelerated GPU scheduling enabled. Reboot to activate."


def _hags_undo(store):
    snap = store.snapshot(HAGS_ID)
    if snap:
        reg_restore("HKLM", GFX, "HwSchMode", snap["was"])
    store.clear(HAGS_ID)
    return True, "Original GPU scheduling setting restored. Reboot to activate."


def _gamemode_probe() -> str:
    cur = reg_read("HKCU", GAMEBAR, "AutoGameModeEnabled")
    if not cur["exists"]:
        return "Currently: Windows default (ON)"
    return "Currently: OFF" if cur["data"] == 0 else "Currently: ON"




# ------------------------------------------------------------------ THE TWEAK LIBRARY
GAMES = SYSPROF + r"\Tasks\Games"
IFEO_FN = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\FortniteClient-Win64-Shipping.exe\PerfOptions"
CAT_RAM, CAT_PWR, CAT_LAT = "RAM & Memory", "Power & Thermal", "Latency & Input"
CAT_GPU, CAT_WIN, CAT_RETIRED = "Graphics", "Windows Polish", "Retired (undo only)"
PROC = "SUB_PROCESSOR"

TWEAKS: list[Tweak] = [
    # ---------------- RAM & Memory
    CustomTweak(PAGEFILE_ID, CAT_RAM, "Dynamic Pagefile (sized to your RAM)",
                "Sizes the pagefile from your installed RAM: 8 GB or less gets a fixed 1.5x pagefile, 16 GB gets 8-12 GB, "
                "32 GB and up stays system-managed. Free space on the system drive is checked first; activates after a reboot.",
                _pagefile_apply, _pagefile_undo, impact="High", reboot=True, desc_fn=_pagefile_desc),
    CustomTweak("standby_purge", CAT_RAM, "Purge Standby Memory List",
                "Flushes the cached-but-idle 'standby' RAM so Fortnite can grab it as free memory (real NtSetSystemInformation call). One-time action - run it before you launch the game.",
                _standby_apply, _noop_undo_factory("One-time action - nothing to undo (Windows refills the cache by itself)."), impact="Medium"),
    CustomTweak("mem_compression", CAT_RAM, "Disable Memory Compression",
                "Stops Windows compressing idle memory, freeing a little CPU time. On PCs with 8 GB of RAM or less compression usually HELPS, so this is left out of the 1-Click profile.",
                _memcomp_apply, _memcomp_undo, impact="Low", reboot=True, rec=False),
    RegTweak("svchost_split", CAT_RAM, "Share svchost Processes (Low-RAM Mode)",
             "Groups background services into fewer svchost.exe processes, saving a few hundred MB of RAM. Offered only on PCs with 8 GB of RAM or less. Takes effect after a reboot.",
             [("HKLM", r"SYSTEM\CurrentControlSet\Control", "SvcHostSplitThresholdInKB", DW, 8388608, 3670016)],
             impact="Low", reboot=True, requires=lambda p: 0 < p.ram_gb <= 8.5, scope="8 GB or less"),
    ServiceTweak("sysmain", CAT_RAM, "Disable SysMain (Superfetch)",
                 "Stops SysMain pre-loading apps into RAM and hammering the disk in the background; pointless on an SSD. Hidden when your system drive is a hard disk, skipped if your Windows build already removed it.",
                 ["SysMain"], {"SysMain": 2}, impact="Low", requires=lambda p: p.system_ssd is not False),
    ServiceTweak("wsearch", CAT_RAM, "Disable Windows Search Indexer",
                 "Stops background file indexing that causes disk/CPU spikes. Start-menu file search gets slower, so this is optional and not in the 1-Click profile.",
                 ["WSearch"], {"WSearch": "delayed"}, impact="Low", rec=False),

    # ---------------- Power & Thermal
    CustomTweak("power_plan", CAT_PWR, "Ultimate / High Performance Power Plan",
                "Creates and activates Microsoft's Ultimate Performance plan (falls back to High Performance) so the CPU never idles down mid-fight. Best on AC power - on a laptop it drains the battery faster. Undo returns to your previous plan.",
                _plan_apply, _plan_undo, impact="High"),
    PowerTweak("cpu_clamp", CAT_PWR, "Clamp CPU Max State to 95% (Anti-Throttle)",
               "Laptops only. Caps the processor at 95%, which on most Intel CPUs switches off Turbo Boost and can keep a thermally limited laptop below its throttle point (behaviour varies by CPU vendor). You trade peak clocks for steadier frame times, so it is optional.",
               [(PROC, "PROCTHROTTLEMAX", 95)], impact="High", rec=False, requires=lambda p: p.is_laptop, scope="Laptops"),
    PowerTweak("boost_mode", CAT_PWR, "CPU Boost Mode: Efficient Enabled",
               "Laptops only. Keeps boost clocks but makes them less aggressive - a gentler alternative to the 95% clamp. Optional.",
               [(PROC, "PERFBOOSTMODE", 3)], impact="Medium", rec=False, requires=lambda p: p.is_laptop, scope="Laptops"),
    PowerTweak("core_parking", CAT_PWR, "Disable Core Parking",
               "Keeps every core awake instead of parking them, removing wake-up micro-stutter when a fight starts. Applied to the plugged-in profile only; slightly raises idle power use.",
               [(PROC, "CPMINCORES", 100)], ac_only=True, impact="Medium"),
    PowerTweak("pcie_aspm", CAT_PWR, "PCI-E Link State Power Management: Off",
               "Disables ASPM sleep states on the GPU/NVMe bus to avoid wake-up latency. Adds a little heat and power draw (plugged-in profile only), so it is optional.",
               [("SUB_PCIEXPRESS", "ASPM", 0)], ac_only=True, impact="Low", rec=False),
    PowerTweak("intel_gpu_max", CAT_GPU, "Intel Graphics: Maximum Performance",
               "Sets the Intel graphics power policy to Maximum Performance so the GPU clocks up immediately (plugged-in profile only). Offered only when an Intel GPU is detected and its driver exposes the setting. "
               "If your PC also has a discrete GPU, Fortnite normally renders on that one and this affects only the Intel GPU.",
               [("44f3beca-a7c0-460e-9df2-bb8b99e0cba6", "3619c3f2-afb2-4afc-b0e9-e7fef372de36", 2)],
               ac_only=True, impact="Medium", requires=lambda p: p.has_gpu("Intel"), scope="Intel GPU"),
    PowerTweak("disk_idle", CAT_PWR, "Never Power Down the Disk",
               "Sets the disk idle timeout to 'never' so the drive isn't put to sleep between asset loads. Small gain on SSDs.",
               [("SUB_DISK", "DISKIDLE", 0)], impact="Low"),
    PowerTweak("usb_suspend", CAT_PWR, "Disable USB Selective Suspend",
               "Stops Windows cutting power to idle USB devices, preventing mouse/keyboard/headset dropouts.",
               [("2a737441-1930-4402-8d77-b2bebba4d5a3", "48e6b7a6-50f5-4760-a579-922062fe337f", 0)], impact="Low"),
    CustomTweak("hibernate_off", CAT_PWR, "Disable Hibernation (Reclaim Disk Space)",
                "Deletes hiberfil.sys, freeing several GB on the system drive, and turns off Fast Startup. Laptops lose Hibernate, so it is only recommended on desktops. Undo turns hibernation back on.",
                _hib_apply, _hib_undo, impact="Low", rec=lambda p: not p.is_laptop),
    RegTweak("power_throttling", CAT_PWR, "Disable Windows Power Throttling",
             "Stops Windows 10/11 throttling background-classified processes to save power, so the game and launcher never get demoted. Recommended on desktops; on laptops it costs battery life, so it is optional there.",
             [("HKLM", r"SYSTEM\CurrentControlSet\Control\Power\PowerThrottling", "PowerThrottlingOff", DW, 1, None)],
             impact="Medium", reboot=True, rec=lambda p: not p.is_laptop),

    # ---------------- Latency & Input
    CustomTweak("mouse_accel", CAT_LAT, "Disable Enhanced Pointer Precision",
                "Turns off Windows mouse acceleration so your hand movement maps 1:1 to the crosshair. Takes effect instantly.",
                _mouse_apply, _mouse_undo, impact="High"),
    RegTweak("net_throttle", CAT_LAT, "Disable Network Throttling Index",
             "Removes the multimedia network packet throttle Windows applies by default. Harmless and occasionally helps ping consistency.",
             [("HKLM", SYSPROF, "NetworkThrottlingIndex", DW, 0xFFFFFFFF, 10)], impact="Low", reboot=True),
    RegTweak("sys_responsiveness", CAT_LAT, "System Responsiveness: 0%",
             "Reduces the CPU time Windows reserves for low-priority multimedia background tasks, leaving more for the game.",
             [("HKLM", SYSPROF, "SystemResponsiveness", DW, 0, 20)], impact="Medium", reboot=True),
    RegTweak("mmcss_games", CAT_LAT, "Games Scheduler Priority (MMCSS)",
             "Raises the Multimedia Class Scheduler 'Games' task to High priority so the game's threads are scheduled ahead of background work.",
             [("HKLM", GAMES, "GPU Priority", DW, 8, 8), ("HKLM", GAMES, "Priority", DW, 6, 2),
              ("HKLM", GAMES, "Scheduling Category", SZ, "High", "Medium"), ("HKLM", GAMES, "SFIO Priority", SZ, "High", "Normal")],
             impact="Medium", reboot=True),
    RegTweak("fth_off", CAT_LAT, "Disable Fault Tolerant Heap",
             "Stops Windows silently switching a crashed game onto a slower heap, which can leave Fortnite permanently slower after one crash.",
             [("HKLM", r"SOFTWARE\Microsoft\FTH", "Enabled", DW, 0, 1)], impact="Low", reboot=True),
    RegTweak("prio_separation", CAT_LAT, "Foreground Priority Boost (Short Quanta)",
             "Sets Win32PrioritySeparation to 0x26 so the focused window (Fortnite) gets short, boosted CPU time slices.",
             [("HKLM", r"SYSTEM\CurrentControlSet\Control\PriorityControl", "Win32PrioritySeparation", DW, 38, 2)], impact="Medium"),
    RegTweak("sticky_keys", CAT_LAT, "Disable Sticky / Filter / Toggle Keys Hotkeys",
             "Prevents the 'press Shift 5 times' pop-up from minimizing Fortnite mid-fight.",
             [("HKCU", r"Control Panel\Accessibility\StickyKeys", "Flags", SZ, "506", "510"),
              ("HKCU", r"Control Panel\Accessibility\ToggleKeys", "Flags", SZ, "58", "62"),
              ("HKCU", r"Control Panel\Accessibility\Keyboard Response", "Flags", SZ, "122", "126")],
             impact="Low"),

    # ---------------- Graphics (vendor-agnostic)
    CustomTweak(HAGS_ID, CAT_GPU, "Hardware-Accelerated GPU Scheduling (HAGS)",
                "Lets the GPU manage its own video memory scheduling. Works on NVIDIA, AMD and Intel with a supporting driver (Windows 10 2004+). "
                "Results vary by GPU and driver - some PCs gain smoother frame times, some see nothing - so it is optional. The live state is shown below; Undo restores your original setting.",
                _hags_apply, _hags_undo, impact="Low", reboot=True, rec=False, probe=_hags_probe),

    # ---------------- Windows Polish
    RegTweak("game_mode", CAT_WIN, "Windows Game Mode: On",
             "Makes sure Windows Game Mode is enabled so the OS prioritises the foreground game and holds back driver/Update installs while you play. The live state is shown below.",
             [("HKCU", GAMEBAR, "AutoGameModeEnabled", DW, 1, None), ("HKCU", GAMEBAR, "AllowAutoGameMode", DW, 1, None)],
             impact="Low", probe=_gamemode_probe),
    RegTweak("gamebar_off", CAT_WIN, "Disable Game Bar & Presence Writer",
             "Turns off the Xbox Game Bar overlay and its background capture hooks (the Presence Writer key is Windows-protected and may be skipped).",
             [("HKCU", GAMEBAR, "ShowStartupPanel", DW, 0, None),
              ("HKCU", GAMEBAR, "UseNexusForGameBarEnabled", DW, 0, None),
              ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\GameDVR", "AppCaptureEnabled", DW, 0, 1)],
             optional=[("HKLM", r"SOFTWARE\Microsoft\WindowsRuntime\ActivatableClassId\Windows.Gaming.GameBar.PresenceServer.Internal.PresenceWriter",
                        "ActivationType", DW, 0, 1)], impact="Medium"),
    RegTweak("gamedvr_off", CAT_WIN, "Disable Background Recording (GameDVR)",
             "Stops Windows continuously recording gameplay in the background, which costs GPU time and RAM.",
             [("HKCU", r"System\GameConfigStore", "GameDVR_Enabled", DW, 0, 1),
              ("HKLM", r"SOFTWARE\Policies\Microsoft\Windows\GameDVR", "AllowGameDVR", DW, 0, None)], impact="Medium", reboot=True),
    RegTweak("transparency_off", CAT_WIN, "Disable Transparency Effects",
             "Turns off the blur/translucency on the taskbar and menus, saving some GPU work and video memory.",
             [("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize", "EnableTransparency", DW, 0, 1)], impact="Low"),
    RegTweak("visualfx_perf", CAT_WIN, "Windows Visuals: Best Performance",
             "Switches Windows to the 'best performance' visual profile and kills window/taskbar animations. Applies fully after you sign out and in.",
             [("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Explorer\VisualEffects", "VisualFXSetting", DW, 2, 0),
              ("HKCU", r"Control Panel\Desktop\WindowMetrics", "MinAnimate", SZ, "0", "1"),
              ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "TaskbarAnimations", DW, 0, 1)], impact="Low"),
    ServiceTweak("diagtrack_off", CAT_WIN, "Disable Telemetry (DiagTrack)",
                 "Stops the Connected User Experiences and Telemetry service from using CPU, disk and network in the background. Skipped if your Windows build already removed it.",
                 ["DiagTrack"], {"DiagTrack": 2}, impact="Low"),
    RegTweak("delivery_opt_off", CAT_WIN, "Disable Delivery Optimization (P2P Updates)",
             "Stops Windows Update using your upload bandwidth to seed updates to other PCs, which can spike your ping.",
             [("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\DeliveryOptimization\Config", "DODownloadMode", DW, 0, None)], impact="Low"),
    RegTweak("bg_apps_off", CAT_WIN, "Disable Background Apps",
             "Prevents Store apps from running in the background while you play, freeing RAM and CPU wake-ups.",
             [("HKCU", r"Software\Microsoft\Windows\CurrentVersion\BackgroundAccessApplications", "GlobalUserDisabled", DW, 1, None),
              ("HKLM", r"SOFTWARE\Policies\Microsoft\Windows\AppPrivacy", "LetAppsRunInBackground", DW, 2, None)], impact="Low"),
    ServiceTweak("xbox_svcs_off", CAT_WIN, "Disable Xbox Background Services",
                 "Disables Xbox Live auth, game-save and networking services Fortnite doesn't need on the Epic launcher. Optional: don't use it if you play Microsoft Store/Xbox titles.",
                 ["XblAuthManager", "XblGameSave", "XboxNetApiSvc", "XboxGipSvc"],
                 {"XblAuthManager": 3, "XblGameSave": 3, "XboxNetApiSvc": 3, "XboxGipSvc": 3}, impact="Low", rec=False),
    CustomTweak("fn_fso_dpi", CAT_WIN, "Fortnite: No Fullscreen Optimizations + App DPI",
                "Sets the same Windows compatibility flags you would tick in the exe's Properties > Compatibility tab, so Fortnite runs true exclusive fullscreen and handles high-DPI scaling itself. "
                "The game file is never modified - this is a per-user Windows setting.",
                _fn_fso_apply, _fn_fso_undo, impact="High"),

    # ---------------- Retired (kept only so Undo still works for anyone who applied it in an older version)
    RegTweak("fn_cpu_priority", CAT_RETIRED, "Fortnite: High CPU Priority (retired)",
             "Retired for anti-cheat safety: per-executable 'Image File Execution Options' keys are the same place debugger hijacks live, so ForgeFN no longer writes them. "
             "If an older version applied this, Undo removes it.",
             [("HKLM", IFEO_FN, "CpuPriorityClass", DW, 3, None)], impact="Low", rec=False, legacy=True),
]
TWEAK_BY_ID = {t.id: t for t in TWEAKS}
CATEGORIES = [CAT_RAM, CAT_PWR, CAT_LAT, CAT_GPU, CAT_WIN]



# ------------------------------------------------------------------ Fortnite config logic
def ini_backup_path() -> str:
    """Backup of GameUserSettings.ini (re-uses a pre-2.0 backup when that is the only one that exists)."""
    new, old = FN_INI + BAK_SUFFIX, FN_INI + LEGACY_BAK_SUFFIX
    return old if (not os.path.exists(new) and os.path.exists(old)) else new


FN_LOW = {
    "sg.ViewDistanceQuality": "0", "sg.AntiAliasingQuality": "0", "sg.ShadowQuality": "0",
    "sg.PostProcessQuality": "0", "sg.TextureQuality": "0", "sg.EffectsQuality": "0",
    "sg.FoliageQuality": "0", "sg.ShadingQuality": "0", "sg.GlobalIlluminationQuality": "0",
    "sg.ReflectionQuality": "0",
}


def edits_performance_mode():
    return [(FN_SEC, "PreferredRHI", "dx11"), (FN_SEC, "PreferredFeatureLevel", "es31")]


def edits_low_quality():
    e = [(FN_SG, k, v) for k, v in FN_LOW.items()]
    e += [(FN_SEC, "bUseVSync", "False"), (FN_SEC, "bMotionBlur", "False"), (FN_SEC, "bShowGrass", "False")]
    return e


def edits_resolution(w, h, quality):
    e = [(FN_SEC, k, str(v)) for k, v in (
        ("ResolutionSizeX", w), ("ResolutionSizeY", h),
        ("LastUserConfirmedResolutionSizeX", w), ("LastUserConfirmedResolutionSizeY", h),
        ("DesiredScreenWidth", w), ("DesiredScreenHeight", h),
        ("LastUserConfirmedDesiredScreenWidth", w), ("LastUserConfirmedDesiredScreenHeight", h),
        ("FullscreenMode", 0), ("LastConfirmedFullscreenMode", 0), ("PreferredFullscreenMode", 0))]
    e.append((FN_SG, "sg.ResolutionQuality", f"{float(quality):.6f}"))
    return e


def edits_fps(fps):
    return [(FN_SEC, "FrameRateLimit", f"{float(fps):.6f}")]


def fn_ini_edit(edits):
    path = FN_INI
    if not os.path.isfile(path):
        return False, "GameUserSettings.ini not found - launch Fortnite once so it creates the file."
    if fortnite_running():
        return False, "Fortnite is running. Close it first - it rewrites this file on exit and would undo the changes."
    try:
        bak = ini_backup_path()
        if not os.path.exists(bak):
            shutil.copy2(path, bak)
        was_ro = not os.access(path, os.W_OK)
        if was_ro:
            os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
        ed = IniEditor(path)
        ed.load()
        for sec, k, v in edits:
            ed.set(sec, k, v)
        ed.save()
        if was_ro:
            os.chmod(path, stat.S_IREAD)
        return True, f"{len(edits)} setting(s) written to GameUserSettings.ini."
    except Exception as e:  # noqa: BLE001
        return False, f"Failed editing the ini: {e}"



# ------------------------------------------------------------------ cleaner logic
def _exp(p):
    return os.path.expandvars(p)


@dataclass(frozen=True)
class CleanTarget:
    """A folder group the cleaner can empty. `vendor` limits it to PCs with that GPU vendor."""
    cid: str
    label: str
    paths: tuple
    default: bool
    note: str = ""
    vendor: Optional[str] = None


CLEAN_TARGETS = [
    CleanTarget("d3d", "DirectX shader cache", (r"%LOCALAPPDATA%\D3DSCache",), True,
                "Rebuilt by games on next launch - expect a little stutter in your first match."),
    CleanTarget("nvidia", "NVIDIA shader cache", (r"%LOCALAPPDATA%\NVIDIA\DXCache", r"%LOCALAPPDATA%\NVIDIA\GLCache"), True,
                "Rebuilt automatically.", "NVIDIA"),
    CleanTarget("amd", "AMD shader cache", (r"%LOCALAPPDATA%\AMD\DxCache", r"%LOCALAPPDATA%\AMD\DxcCache",
                                            r"%LOCALAPPDATA%\AMD\VkCache", r"%LOCALAPPDATA%\AMD\GLCache"), True,
                "Rebuilt automatically.", "AMD"),
    CleanTarget("intel", "Intel shader cache", (r"%LOCALAPPDATA%\Intel\ShaderCache",), True, "Rebuilt automatically.", "Intel"),
    CleanTarget("fnlogs", "Fortnite crash dumps & logs",
                (r"%LOCALAPPDATA%\FortniteGame\Saved\Crashes", r"%LOCALAPPDATA%\FortniteGame\Saved\Logs"), True),
    CleanTarget("dumps", "Windows crash dumps", (r"%LOCALAPPDATA%\CrashDumps",), True),
    CleanTarget("utemp", "User temp files", (r"%TEMP%",), True, "Files in use are skipped."),
    CleanTarget("wtemp", "Windows temp files", (r"%SystemRoot%\Temp",), True, "Files in use are skipped."),
    CleanTarget("prefetch", "Windows Prefetch", (r"%SystemRoot%\Prefetch",), False,
                "Windows rebuilds it; first launch of each app is slightly slower."),
]


def _protected_roots():
    roots = [os.path.abspath(DATA_DIR)]
    mei = getattr(sys, "_MEIPASS", None)
    if mei:
        roots.append(os.path.abspath(mei))
    return roots


def _is_protected(path):
    ap = os.path.abspath(path)
    return any(ap == r or ap.startswith(r + os.sep) for r in _protected_roots())


def dir_size(path):
    total = 0
    stack = [path]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        if e.is_symlink() or _is_protected(e.path):
                            continue
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total


def purge_dir(path):
    """Delete everything INSIDE path (never the folder itself). Returns bytes freed."""
    freed = 0
    try:
        entries = list(os.scandir(path))
    except OSError:
        return 0
    for e in entries:
        if _is_protected(e.path):
            continue
        try:
            if e.is_symlink() or not e.is_dir(follow_symlinks=False):
                sz = e.stat(follow_symlinks=False).st_size if not e.is_symlink() else 0
                os.unlink(e.path)
                freed += sz
            else:
                sz = dir_size(e.path)
                shutil.rmtree(e.path, ignore_errors=True)
                if not os.path.exists(e.path):
                    freed += sz
        except OSError:
            pass
    return freed


def valid_clean_path(p):
    return "%" not in p and len(os.path.normpath(p).split(os.sep)) >= 3 and os.path.isdir(p)

# ------------------------------------------------------------------ backup logic
REG_BACKUP_KEYS = [
    r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management",
    r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile",
    r"HKLM\SYSTEM\CurrentControlSet\Control\PriorityControl",
    r"HKLM\SYSTEM\CurrentControlSet\Control\Power",
    r"HKLM\SOFTWARE\Microsoft\FTH",
    r"HKLM\SYSTEM\CurrentControlSet\Control\GraphicsDrivers",
    r"HKLM\SOFTWARE\Policies\Microsoft\Windows",
    r"HKCU\Control Panel\Mouse",
    r"HKCU\Control Panel\Accessibility",
    r"HKCU\System\GameConfigStore",
    r"HKCU\Software\Microsoft\GameBar",
    r"HKCU\Software\Microsoft\Windows\CurrentVersion\GameDVR",
    r"HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\VisualEffects",
    r"HKCU\Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers",
]


def create_restore_point(desc="ForgeFN_PreOptimization"):
    key = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\SystemRestore"
    prev = reg_read("HKLM", key, "SystemRestorePointCreationFrequency")
    try:
        reg_write("HKLM", key, "SystemRestorePointCreationFrequency", DW, 0)  # lift the 24h limit
    except OSError:
        pass
    rc, out = ps(f"Enable-ComputerRestore -Drive 'C:\\'; Checkpoint-Computer -Description '{desc}' -RestorePointType MODIFY_SETTINGS", timeout=300)
    try:
        reg_restore("HKLM", key, "SystemRestorePointCreationFrequency", prev)
    except OSError:
        pass
    if rc == 0:
        return True, f"System Restore point '{desc}' created."
    return False, "Restore point failed (System Restore / VSS may be disabled on this Windows build): " + out.strip()[:160]


def export_registry_backup():
    folder = os.path.join(BACKUP_ROOT, datetime.now().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(folder, exist_ok=True)
    ok = 0
    for key in REG_BACKUP_KEYS:
        fname = re.sub(r"[^A-Za-z0-9]+", "_", key)[:90] + ".reg"
        rc, _o = run(["reg", "export", key, os.path.join(folder, fname), "/y"])
        ok += rc == 0
    return (ok > 0), f"Exported {ok}/{len(REG_BACKUP_KEYS)} registry branches to {folder}", folder



# ------------------------------------------------------------------ anti-cheat compliance copy (shown in Backup & Safety)
ANTICHEAT_ALLOWED = (
    "ForgeFN is designed to stay inside Epic Games' Easy Anti-Cheat (EAC) and BattlEye rules. It ONLY uses:\n"
    "  ✔  documented Windows settings: registry values, power-plan settings, service start types, the pagefile\n"
    "  ✔  per-user Windows compatibility flags (the same ones in an exe's Properties > Compatibility tab)\n"
    "  ✔  edits to Fortnite's own GameUserSettings.ini configuration file")
ANTICHEAT_NEVER = (
    "ForgeFN NEVER:\n"
    "  ✖  modifies, patches or replaces game, launcher or anti-cheat files\n"
    "  ✖  injects code or DLLs, hooks functions, or reads / writes game memory\n"
    "  ✖  opens a handle to the Fortnite or anti-cheat processes - it only checks whether the process name exists, from a read-only system process list\n"
    "  ✖  adds overlays, touches your network traffic, or changes anything that gives a gameplay advantage beyond video settings and OS performance")
ANTICHEAT_FINEPRINT = (
    "Every change is recorded and reversible with Undo. ForgeFN is an independent project and is not affiliated with or endorsed by Epic Games, "
    "Easy Anti-Cheat or BattlEye. Anti-cheat rules can change and no tool can guarantee immunity from future policy - if you are unsure about a tweak, "
    "read its description or skip it. Use at your own risk; create a restore point first.")


# ------------------------------------------------------------------ GUI
class ForgeApp(ctk.CTk):
    """Main window: sidebar navigation plus six pages (dashboard, scanner, tweaks, config, cleaner, backup)."""

    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.title(f"{APP_NAME} // {APP_TAGLINE}")
        self.geometry("1180x780")
        self.minsize(1040, 700)
        self.configure(fg_color=C_BG)

        self.store = StateStore()
        self.q: queue.Queue = queue.Queue()
        self.stop_evt = threading.Event()
        self.work_lock = threading.Lock()
        self.busy = False
        self.profile: Optional[SystemProfile] = None      # filled in by the background hardware scan
        self.display: DisplayInfo = detect_display()
        self.target_fps = max(60, self.display.refresh or 60)
        self.res_choice = (self.display.width, self.display.height) if self.display.width else None
        self.res_quality = 100
        self.gpu_proc = None
        self.status_labels: dict = {}
        self.pending_reboot: list[str] = []
        self.clean_vars: dict = {}
        self.clean_size_lbls: dict = {}

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.build_sidebar()
        self.pages = {
            "dashboard": self.page_dashboard(), "scanner": self.page_scanner(), "tweaks": self.page_tweaks(),
            "config": self.page_config(), "cleaner": self.page_cleaner(), "backup": self.page_backup(),
        }
        self.select_page("dashboard")
        self.after(250, self._dark_titlebar)
        self.after(60, self._pump)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.log(f"{APP_NAME} {APP_VERSION} ready. Administrator rights verified.")
        threading.Thread(target=self._telemetry_loop, daemon=True).start()
        threading.Thread(target=self._gpu_loop, daemon=True).start()
        self.scan_hardware()
        self.refresh_fn_status()

    # ---------------- plumbing
    def post(self, fn, *a):
        self.q.put((fn, a))

    def _pump(self):
        try:
            while True:
                fn, a = self.q.get_nowait()
                try:
                    fn(*a)
                except Exception as e:  # noqa: BLE001
                    print("UI callback error:", e)
        except queue.Empty:
            pass
        if not self.stop_evt.is_set():
            self.after(60, self._pump)

    def log(self, msg):
        self.post(self._log_main, msg)

    def _log_main(self, msg):
        self.terminal.insert("end", f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n")
        self.terminal.see("end")

    def _dark_titlebar(self):
        try:
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            val = ctypes.c_int(1)
            for attr in (20, 19):
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), ctypes.sizeof(val))
        except Exception:  # noqa: BLE001
            pass

    def on_close(self):
        self.stop_evt.set()
        try:
            if self.gpu_proc:
                self.gpu_proc.terminate()
        except Exception:  # noqa: BLE001
            pass
        self.destroy()

    # ---------------- small widget helpers
    def font(self, size=13, weight="normal", family=None):
        return ctk.CTkFont(size=size, weight=weight, family=family) if family else ctk.CTkFont(size=size, weight=weight)

    def header(self, page, title, sub):
        ctk.CTkLabel(page, text=title, font=self.font(26, "bold"), text_color=C_TXT).pack(anchor="w")
        ctk.CTkLabel(page, text=sub, font=self.font(13), text_color=C_MUT, justify="left").pack(anchor="w", pady=(0, 16))

    def action_button(self, parent, text, cmd, kind="primary", **kw):
        colors = {"primary": (C_ACC, "#000000", C_ACC_H), "dark": (C_CARD2, C_TXT, "#2B3550"),
                  "danger": (C_BAD, "#FFFFFF", "#CC2952"), "purple": (C_PUR, "#FFFFFF", "#5D1E9B")}
        fg, tc, hv = colors[kind]
        return ctk.CTkButton(parent, text=text, command=cmd, fg_color=fg, text_color=tc, hover_color=hv,
                             font=self.font(12, "bold"), height=kw.pop("height", 38), **kw)


    # ---------------- sidebar / navigation
    def build_sidebar(self):
        sb = ctk.CTkFrame(self, width=230, corner_radius=0, fg_color=C_SIDE)
        sb.grid(row=0, column=0, sticky="nsew")
        sb.grid_propagate(False)
        sb.grid_rowconfigure(10, weight=1)
        ctk.CTkLabel(sb, text="FORGEFN", font=self.font(21, "bold"), text_color=C_ACC).grid(row=0, column=0, padx=20, pady=(24, 2), sticky="w")
        ctk.CTkLabel(sb, text="Fortnite Performance Engine", font=self.font(11), text_color=C_MUT).grid(row=1, column=0, padx=20, pady=(0, 18), sticky="w")
        self.nav = {}
        for i, (key, label) in enumerate([("dashboard", "Dashboard"), ("scanner", "Hardware Scanner"), ("tweaks", "Tweak Library"),
                                          ("config", "Fortnite Config"), ("cleaner", "System Cleaner"), ("backup", "Backup & Safety")], start=2):
            b = ctk.CTkButton(sb, text=label, anchor="w", height=38, corner_radius=8, fg_color="transparent", text_color=C_TXT,
                              hover_color=C_CARD, font=self.font(13), command=lambda k=key: self.select_page(k))
            b.grid(row=i, column=0, padx=12, pady=3, sticky="ew")
            self.nav[key] = b
        sb.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(sb, text="● Administrator", font=self.font(11, "bold"), text_color=C_OK).grid(row=11, column=0, padx=20, pady=(0, 4), sticky="sw")
        self.lbl_side_hw = ctk.CTkLabel(sb, text=f"v{APP_VERSION} · detecting hardware…", font=self.font(11), text_color=C_MUT,
                                        wraplength=196, justify="left")
        self.lbl_side_hw.grid(row=12, column=0, padx=20, pady=(0, 18), sticky="sw")

    def select_page(self, name):
        for p in self.pages.values():
            p.grid_forget()
        for k, b in self.nav.items():
            b.configure(fg_color=C_CARD if k == name else "transparent", text_color=C_ACC if k == name else C_TXT)
        self.pages[name].grid(row=0, column=1, sticky="nsew", padx=24, pady=22)
        if name == "config":
            self.refresh_fn_status()
        if name == "backup":
            self.refresh_backup_status()
        if name == "cleaner":
            self.scan_cleaner()


    # ================================================================== DASHBOARD
    def stat_card(self, parent, col, title, init):
        card = ctk.CTkFrame(parent, fg_color=C_CARD, corner_radius=10)
        card.grid(row=1, column=col, padx=5, pady=5, sticky="nsew")
        title_lbl = ctk.CTkLabel(card, text=title, font=self.font(11, "bold"), text_color=C_MUT)
        title_lbl.pack(anchor="w", padx=16, pady=(14, 2))
        val = ctk.CTkLabel(card, text=init, font=self.font(20, "bold"), text_color=C_ACC)
        val.pack(anchor="w", padx=16)
        bar = ctk.CTkProgressBar(card, progress_color=C_ACC, fg_color=C_CARD2, height=7)
        bar.set(0)
        bar.pack(fill="x", padx=16, pady=(8, 14))
        return val, bar, title_lbl

    def _fps_text(self):
        return "UNLIMITED" if self.target_fps == 0 else f"{self.target_fps} FPS"

    def page_dashboard(self):
        page = ctk.CTkFrame(self, fg_color="transparent")
        page.grid_columnconfigure((0, 1, 2, 3), weight=1, uniform="c")
        page.grid_rowconfigure(6, weight=1)
        top = ctk.CTkFrame(page, fg_color="transparent")
        top.grid(row=0, column=0, columnspan=4, sticky="ew", pady=(0, 10))
        ctk.CTkLabel(top, text="Command Center", font=self.font(26, "bold"), text_color=C_TXT).pack(anchor="w")
        self.lbl_profile = ctk.CTkLabel(top, text="Detecting hardware…", font=self.font(13), text_color=C_MUT)
        self.lbl_profile.pack(anchor="w")

        self.cpu_val, self.cpu_bar, self.cpu_title = self.stat_card(page, 0, "CPU LOAD", "0%")
        self.ram_val, self.ram_bar, self.ram_title = self.stat_card(page, 1, "RAM USED", "0 / 0 GB")
        self.gpu_val, self.gpu_bar, self.gpu_title = self.stat_card(page, 2, "GPU 3D LOAD", "…")
        fn_card = ctk.CTkFrame(page, fg_color=C_CARD, corner_radius=10)
        fn_card.grid(row=1, column=3, padx=5, pady=5, sticky="nsew")
        ctk.CTkLabel(fn_card, text="FORTNITE", font=self.font(11, "bold"), text_color=C_MUT).pack(anchor="w", padx=16, pady=(14, 2))
        self.dash_fn = ctk.CTkLabel(fn_card, text="Not running", font=self.font(20, "bold"), text_color=C_MUT)
        self.dash_fn.pack(anchor="w", padx=16)
        ctk.CTkLabel(fn_card, text="Close it before editing config", font=self.font(11), text_color=C_MUT).pack(anchor="w", padx=16, pady=(8, 14))

        fps = ctk.CTkFrame(page, fg_color=C_CARD, corner_radius=12)
        fps.grid(row=2, column=0, columnspan=4, sticky="ew", padx=5, pady=(10, 5))
        fps.grid_columnconfigure(1, weight=1)
        left = ctk.CTkFrame(fps, fg_color="transparent")
        left.grid(row=0, column=0, padx=20, pady=14, sticky="w")
        ctk.CTkLabel(left, text="TARGET FPS · PERFORMANCE MODE", font=self.font(11, "bold"), text_color=C_MUT).pack(anchor="w")
        self.fps_big = ctk.CTkLabel(left, text=self._fps_text(), font=self.font(32, "bold"), text_color=C_ACC)
        self.fps_big.pack(anchor="w")
        mid = ctk.CTkFrame(fps, fg_color="transparent")
        mid.grid(row=0, column=1, padx=10, sticky="ew")
        self.fps_bar = ctk.CTkProgressBar(mid, progress_color=C_PUR, fg_color=C_CARD2, height=10)
        self.fps_bar.set(min(1.0, self.target_fps / 240))
        self.fps_bar.pack(fill="x", pady=(4, 6))
        ctk.CTkLabel(mid, text="Written to Fortnite as the in-game frame cap. A cap your GPU can actually hold keeps fan noise\nand temperatures steadier than running uncapped. (This is a target, not a live FPS reading.)",
                     font=self.font(11), text_color=C_MUT, justify="left").pack(anchor="w")
        fps_values = [str(v) for v in fps_cap_options(self.display.refresh)] + ["Unlimited"]
        seg = ctk.CTkSegmentedButton(fps, values=fps_values, command=self._on_fps, selected_color=C_PUR,
                                     selected_hover_color="#5D1E9B", unselected_color=C_CARD2)
        seg.set(str(self.target_fps) if str(self.target_fps) in fps_values else fps_values[0])
        seg.grid(row=0, column=2, padx=20)

        ban = ctk.CTkFrame(page, fg_color=C_CARD, corner_radius=12, border_width=1, border_color=C_PUR)
        ban.grid(row=3, column=0, columnspan=4, sticky="ew", padx=5, pady=10)
        ban.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(ban, text="1-Click Fortnite Optimize", font=self.font(18, "bold"), text_color=C_ACC).grid(row=0, column=0, padx=20, pady=(16, 2), sticky="w")
        self.lbl_oneclick = ctk.CTkLabel(ban, text="Scanning your hardware to build the recommended tweak list…",
                                         font=self.font(12), text_color=C_MUT, justify="left")
        self.lbl_oneclick.grid(row=1, column=0, padx=20, sticky="w")
        self.chk_backup = ctk.CTkCheckBox(ban, text="Create restore point + registry backup first", fg_color=C_ACC, hover_color=C_ACC_H, text_color=C_TXT, checkmark_color="#000")
        self.chk_backup.select()
        self.chk_backup.grid(row=2, column=0, padx=20, pady=(8, 4), sticky="w")
        self.master_bar = ctk.CTkProgressBar(ban, progress_color=C_ACC, fg_color=C_CARD2, height=7)
        self.master_bar.set(0)
        self.master_bar.grid(row=3, column=0, padx=20, pady=(2, 14), sticky="ew")
        btns = ctk.CTkFrame(ban, fg_color="transparent")
        btns.grid(row=0, column=1, rowspan=4, padx=22, pady=14)
        self.btn_master = self.action_button(btns, "EXECUTE 1-CLICK OPTIMIZE", self.execute_master, width=230, height=48)
        self.btn_master.pack()
        self.btn_restart = self.action_button(btns, "Restart Windows now", self.restart_now, kind="dark", width=230, height=32)
        self.btn_restart.pack(pady=(8, 0))
        self.btn_restart.configure(state="disabled")

        ctk.CTkLabel(page, text="Activity log", font=self.font(14, "bold"), text_color=C_TXT).grid(row=5, column=0, columnspan=4, sticky="w", padx=5, pady=(4, 4))
        self.terminal = ctk.CTkTextbox(page, fg_color=C_CARD, text_color=C_TXT, font=ctk.CTkFont(family="Consolas", size=12), corner_radius=8, height=150)
        self.terminal.grid(row=6, column=0, columnspan=4, sticky="nsew", padx=5)
        return page

    def _on_fps(self, v):
        self.target_fps = 0 if v == "Unlimited" else int(v)
        self.fps_big.configure(text=self._fps_text())
        self.fps_bar.set(1.0 if self.target_fps == 0 else min(1.0, self.target_fps / 240))
        if hasattr(self, "lbl_cfg_fps"):
            self.lbl_cfg_fps.configure(text=f"FPS cap: {self._fps_text()} (change on the Dashboard)")

    def _update_oneclick_text(self):
        recs = [t for t in self.active_tweaks() if t.is_rec(self.profile)]
        self.lbl_oneclick.configure(
            text=f"Applies the {len(recs)} recommended tweaks for this PC (including a standby-RAM purge), then writes Performance Mode, all-Low graphics,\n"
                 "your resolution and FPS cap into Fortnite's config. OPTIONAL tweaks and ones that don't apply to your hardware are skipped. Everything can be undone.")

    # ---------------- telemetry
    def _telemetry_loop(self):
        tick = 0
        while not self.stop_evt.is_set():
            try:
                cpu = psutil.cpu_percent(interval=1.0)
                m = psutil.virtual_memory()
                self.post(self._show_telemetry, cpu, m.used, m.total)
                if tick % 3 == 0:
                    self.post(self._show_fn_running, fortnite_running())
            except Exception:  # noqa: BLE001
                pass
            tick += 1

    def _show_telemetry(self, cpu, used, total):
        self.cpu_val.configure(text=f"{cpu:.0f}%")
        self.cpu_bar.set(cpu / 100)
        self.ram_val.configure(text=f"{used / 2**30:.2f} / {total / 2**30:.1f} GB")
        self.ram_bar.set(used / total)

    def _show_fn_running(self, running):
        self.dash_fn.configure(text="● Running" if running else "Not running", text_color=C_WARN if running else C_MUT)
        if hasattr(self, "lbl_fn_run"):
            self.lbl_fn_run.configure(text="● Fortnite is running - close it before editing" if running else "Fortnite is closed - safe to edit",
                                      text_color=C_WARN if running else C_OK)

    def _gpu_loop(self):
        script = ("$ErrorActionPreference='SilentlyContinue'; while($true){ "
                  "$v=(Get-Counter '\\GPU Engine(*engtype_3D)\\Utilization Percentage').CounterSamples | Measure-Object CookedValue -Sum; "
                  "if($v -and $v.Sum -ne $null){[Console]::Out.WriteLine([math]::Round($v.Sum,1))}else{[Console]::Out.WriteLine('NA')}; "
                  "[Console]::Out.Flush(); Start-Sleep -Seconds 2 }")
        try:
            self.gpu_proc = subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], stdout=subprocess.PIPE,
                                             stderr=subprocess.DEVNULL, text=True, creationflags=CREATE_NO_WINDOW)
            for line in self.gpu_proc.stdout:
                if self.stop_evt.is_set():
                    break
                s = line.strip().replace(",", ".")
                try:
                    self.post(self._show_gpu, min(100.0, float(s)))
                except ValueError:
                    self.post(self._show_gpu, None)
        except Exception:  # noqa: BLE001
            self.post(self._show_gpu, None)

    def _show_gpu(self, val):
        if val is None:
            self.gpu_val.configure(text="N/A")
            self.gpu_bar.set(0)
        else:
            self.gpu_val.configure(text=f"{val:.0f}%")
            self.gpu_bar.set(val / 100)

    # ---------------- master optimize
    def execute_master(self):
        if self.busy:
            return
        if self.profile is None:
            self.log("Hardware scan is still running - try again in a moment.")
            return
        self.busy = True
        self.btn_master.configure(state="disabled", text="OPTIMIZING…")
        self.master_bar.set(0)
        threading.Thread(target=self._master_worker, args=(bool(self.chk_backup.get()),), daemon=True).start()

    def _master_worker(self, backup):
        with self.work_lock:
            recs = [t for t in self.active_tweaks() if t.is_rec(self.profile)]
            total = len(recs) + 1 + (2 if backup else 0)
            step = 0

            def tick():
                nonlocal step
                step += 1
                self.post(self.master_bar.set, step / total)

            self.log("=== 1-Click Fortnite Optimize started ===")
            if backup:
                self.log("Creating system restore point (can take up to a minute)…")
                ok, msg = create_restore_point()
                self.log(("✔ " if ok else "⚠ ") + msg)
                tick()
                ok, msg, _f = export_registry_backup()
                self.log(("✔ " if ok else "⚠ ") + msg)
                tick()
            good, reboot = 0, []
            for t in recs:
                ok, msg = self._run_tweak(t, "apply")
                self.log(f"{'✔' if ok else '✖'} {t.name}: {msg}")
                good += ok
                if ok and t.reboot and self.store.is_applied(t.id):
                    reboot.append(t.name)
                tick()
            ok, msg = self._fn_profile()
            self.log(("✔ " if ok else "⚠ ") + "Fortnite config: " + msg)
            tick()
            self.log(f"=== Done: {good}/{len(recs)} tweaks applied. ===")
            self.post(self._master_done, reboot)

    def _master_done(self, reboot):
        self.busy = False
        self.btn_master.configure(state="normal", text="EXECUTE 1-CLICK OPTIMIZE")
        self.master_bar.set(1)
        self.pending_reboot = sorted(set(self.pending_reboot + reboot))
        if self.pending_reboot:
            self.btn_restart.configure(state="normal")
            self.log(f"Restart Windows to finish {len(self.pending_reboot)} tweak(s) (pagefile, MMCSS, services…).")

    def restart_now(self):
        if messagebox.askyesno(APP_NAME, "Restart Windows now to finish applying tweaks?\n\nSave your work first."):
            run(["shutdown", "/r", "/t", "10", "/c", "ForgeFN: finishing tweaks"])



    # ================================================================== SCANNER + hardware profile
    def page_scanner(self):
        page = ctk.CTkFrame(self, fg_color="transparent")
        self.header(page, "Hardware Scanner", "Live readout from Windows (WMI/CIM) of the machine this app is running on.")
        self.scan_box = ctk.CTkFrame(page, fg_color=C_CARD, corner_radius=12)
        self.scan_box.pack(fill="x", pady=(0, 12))
        self.scan_rows_frame = ctk.CTkFrame(self.scan_box, fg_color="transparent")
        self.scan_rows_frame.pack(fill="x", padx=18, pady=14)
        ctk.CTkLabel(self.scan_rows_frame, text="Scanning…", text_color=C_MUT).pack(anchor="w")
        self.action_button(page, "Rescan hardware", self.scan_hardware, kind="dark", width=170).pack(anchor="w")
        tips = ctk.CTkFrame(page, fg_color=C_CARD, corner_radius=12)
        tips.pack(fill="x", pady=(12, 0))
        ctk.CTkLabel(tips, text="GPU & power tips  ·  advice only, ForgeFN does not change these", font=self.font(13, "bold"),
                     text_color=C_ACC).pack(anchor="w", padx=18, pady=(12, 2))
        self.lbl_tips = ctk.CTkLabel(tips, text="…", font=self.font(12), text_color=C_MUT, wraplength=780, justify="left")
        self.lbl_tips.pack(anchor="w", padx=18, pady=(0, 12))
        return page

    def scan_hardware(self):
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        # Each query is wrapped so one broken CIM class (common on stripped-down builds) can't blank the whole scan.
        script = ("function TryGet([scriptblock]$b){ try{ & $b }catch{ $null } };"
                  "$sd=$env:SystemDrive.Substring(0,1);"
                  "$o=[ordered]@{"
                  "cs=(TryGet { Get-CimInstance Win32_ComputerSystem | Select-Object Manufacturer,Model,TotalPhysicalMemory });"
                  "cpu=(TryGet { @(Get-CimInstance Win32_Processor | Select-Object Name,MaxClockSpeed,NumberOfCores,NumberOfLogicalProcessors) });"
                  "gpu=(TryGet { @(Get-CimInstance Win32_VideoController | Select-Object Name,DriverVersion,PNPDeviceID,AdapterCompatibility,ConfigManagerErrorCode) });"
                  "ram=(TryGet { @(Get-CimInstance Win32_PhysicalMemory | Select-Object Speed,ConfiguredClockSpeed) });"
                  "os=(TryGet { Get-CimInstance Win32_OperatingSystem | Select-Object Caption,BuildNumber,OSArchitecture });"
                  "disk=(TryGet { @(Get-CimInstance Win32_DiskDrive | Select-Object Model,Size) });"
                  "chassis=(TryGet { (Get-CimInstance Win32_SystemEnclosure).ChassisTypes });"
                  "battery=(TryGet { @(Get-CimInstance Win32_Battery | Select-Object Name) });"
                  "sysdisk=(TryGet { $n=(Get-Partition -DriveLetter $sd).DiskNumber; "
                  "Get-PhysicalDisk | Where-Object { [string]$_.DeviceId -eq [string]$n } | "
                  "Select-Object FriendlyName,@{n='MediaType';e={[string]$_.MediaType}},@{n='BusType';e={[string]$_.BusType}} })"
                  "};"
                  "$o | ConvertTo-Json -Depth 4 -Compress")
        rc, out = ps(script, timeout=90)
        warn = ""
        try:
            profile = build_profile(json.loads(out[out.index("{"):]))
        except Exception as e:  # noqa: BLE001
            profile = fallback_profile()
            warn = f"Hardware scan failed ({str(e)[:80]}). Using a minimal profile: RAM and battery only."
        try:
            dpi = round(ctypes.windll.user32.GetDpiForSystem() / 96 * 100)
        except Exception:  # noqa: BLE001
            dpi = None
        display = detect_display()
        self.post(self._on_profile, profile, display, profile_rows(profile, display, dpi), warn)

    def _on_profile(self, profile, display, rows, warn=""):
        """Runs on the UI thread once the scan finishes: refresh everything that depends on the hardware."""
        self.profile, self.display = profile, display
        if self.res_choice is None and display.width:
            self.res_choice = (display.width, display.height)
        self._show_scan(rows)
        if warn:
            self.log("⚠ " + warn)
            self.lbl_profile.configure(text="⚠ " + warn, text_color=C_WARN)
        else:
            self.lbl_profile.configure(text=f"✔ {profile.summary_line()}", text_color=C_OK)
        gpu = profile.primary_gpu
        self.cpu_title.configure(text=f"CPU LOAD · {short_name(profile.cpu_name, 22)}".upper() if profile.cpu_name != "Unknown CPU" else "CPU LOAD")
        self.ram_title.configure(text=f"RAM USED · {profile.ram_gb:.0f} GB" if profile.ram_bytes else "RAM USED")
        self.gpu_title.configure(text=f"GPU 3D LOAD · {gpu.vendor.upper()}" if gpu else "GPU 3D LOAD")
        self.lbl_side_hw.configure(text=f"v{APP_VERSION} · {profile.form_factor} · {' + '.join(profile.vendors) or 'GPU ?'} · {profile.ram_gb:.0f} GB")
        self.lbl_tips.configure(text="\n".join("• " + t for t in vendor_tips(profile)))
        self._populate_tweaks()
        self._populate_cleaner()
        self._update_oneclick_text()
        self._build_res_buttons()
        self.log(f"Hardware: {profile.summary_line()}")

    def _show_scan(self, rows):
        for w in self.scan_rows_frame.winfo_children():
            w.destroy()
        for k, v in rows:
            r = ctk.CTkFrame(self.scan_rows_frame, fg_color="transparent")
            r.pack(fill="x", pady=6)
            ctk.CTkLabel(r, text=k, width=150, anchor="w", font=self.font(13, "bold"), text_color=C_ACC).pack(side="left")
            ctk.CTkLabel(r, text=v, anchor="w", justify="left", wraplength=620, font=self.font(13), text_color=C_TXT).pack(side="left", fill="x", expand=True)

    # ================================================================== TWEAK LIBRARY
    def active_tweaks(self):
        """Tweaks that apply to this PC (hardware gates satisfied, not retired)."""
        return [t for t in TWEAKS if not t.legacy and t.applies_to(self.profile)]

    def page_tweaks(self):
        page = ctk.CTkFrame(self, fg_color="transparent")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(1, weight=1)
        top = ctk.CTkFrame(page, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.lbl_tweak_title = ctk.CTkLabel(top, text="Tweak Library", font=self.font(26, "bold"), text_color=C_TXT)
        self.lbl_tweak_title.pack(anchor="w")
        ctk.CTkLabel(top, text="Every Apply makes a real change and records the original value; Undo restores it. OPTIONAL = excluded from 1-Click. "
                               "Tweaks that don't fit this PC are hidden.", font=self.font(13), text_color=C_MUT).pack(anchor="w")
        self.tweak_scroll = ctk.CTkScrollableFrame(page, fg_color="transparent")
        self.tweak_scroll.grid(row=1, column=0, sticky="nsew")
        ctk.CTkLabel(self.tweak_scroll, text="Detecting hardware…", text_color=C_MUT).pack(anchor="w", padx=4, pady=10)
        return page

    def _populate_tweaks(self):
        for w in self.tweak_scroll.winfo_children():
            w.destroy()
        self.status_labels = {}
        shown = self.active_tweaks()
        self.lbl_tweak_title.configure(text=f"Tweak Library · {len(shown)} tweaks for this PC")
        for cat in CATEGORIES:
            rows = [t for t in shown if t.cat == cat]
            if not rows:
                continue
            ctk.CTkLabel(self.tweak_scroll, text=cat.upper(), font=self.font(14, "bold"), text_color=C_ACC).pack(anchor="w", padx=4, pady=(14, 4))
            for t in rows:
                self._tweak_row(self.tweak_scroll, t)
        retired = [t for t in TWEAKS if t.legacy and self.store.is_applied(t.id)]
        if retired:
            ctk.CTkLabel(self.tweak_scroll, text=CAT_RETIRED.upper(), font=self.font(14, "bold"), text_color=C_WARN).pack(anchor="w", padx=4, pady=(14, 4))
            for t in retired:
                self._tweak_row(self.tweak_scroll, t)
        hidden = [t.name for t in TWEAKS if not t.legacy and not t.applies_to(self.profile)]
        if hidden:
            ctk.CTkLabel(self.tweak_scroll, text="Hidden because they don't apply to this PC: " + "; ".join(hidden), font=self.font(11),
                         text_color=C_MUT, wraplength=760, justify="left").pack(anchor="w", padx=4, pady=(18, 6))

    def _tweak_row(self, parent, t: Tweak):
        card = ctk.CTkFrame(parent, fg_color=C_CARD, corner_radius=10)
        card.pack(fill="x", padx=2, pady=4)
        card.grid_columnconfigure(0, weight=1)
        info = ctk.CTkFrame(card, fg_color="transparent")
        info.grid(row=0, column=0, sticky="ew", padx=14, pady=10)
        head = ctk.CTkFrame(info, fg_color="transparent")
        head.pack(anchor="w", fill="x")
        ctk.CTkLabel(head, text=t.name, font=self.font(13, "bold"), text_color=C_TXT).pack(side="left")
        badges = [(f"{t.impact.upper()} IMPACT", C_ACC)]
        if t.scope:
            badges.append((f"{t.scope.upper()} ONLY", C_PUR))
        if t.reboot:
            badges.append(("REBOOT", C_WARN))
        if not t.is_rec(self.profile):
            badges.append(("OPTIONAL", C_MUT))
        for text, col in badges:
            ctk.CTkLabel(head, text=f" {text} ", font=self.font(9, "bold"), text_color=col, fg_color=C_CARD2, corner_radius=5).pack(side="left", padx=(8, 0))
        ctk.CTkLabel(info, text=t.describe(self.profile), font=self.font(11), text_color=C_MUT, wraplength=560, justify="left").pack(anchor="w", pady=(3, 3))
        st = ctk.CTkLabel(info, text="", font=self.font(11, "bold"))
        st.pack(anchor="w")
        self.status_labels[t.id] = st
        self._refresh_status(t)
        bf = ctk.CTkFrame(card, fg_color="transparent")
        bf.grid(row=0, column=1, padx=14)
        if not t.legacy:
            self.action_button(bf, "Apply", lambda x=t: self.single(x, "apply"), width=74, height=30).pack(side="left", padx=3)
        self.action_button(bf, "Undo", lambda x=t: self.single(x, "undo"), kind="dark", width=74, height=30).pack(side="left", padx=3)

    def _refresh_status(self, t):
        lbl = self.status_labels.get(t.id)
        if not lbl:
            return
        if self.store.is_applied(t.id):
            text, col = "● APPLIED" + ("  ·  reboot to finish" if t.reboot else ""), C_OK
        else:
            text, col = "○ Default", C_MUT
        if t.probe:
            try:
                text += "  ·  " + t.probe()
            except Exception:  # noqa: BLE001
                pass
        lbl.configure(text=text, text_color=col)

    def _run_tweak(self, t, action):
        if action == "apply":
            if t.legacy:
                return False, "This tweak was retired for anti-cheat safety and can only be undone."
            if not t.applies_to(self.profile):
                return False, "Not applicable to this PC (or the hardware scan hasn't finished)."
        try:
            return t.apply(self.store) if action == "apply" else t.undo(self.store)
        except PermissionError as e:
            return False, f"Access denied: {e}"
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"

    def single(self, t, action):
        def worker():
            with self.work_lock:
                ok, msg = self._run_tweak(t, action)
            self.log(f"{'✔' if ok else '✖'} {'Applied' if action == 'apply' else 'Undo'} · {t.name}: {msg}")
            self.post(self._refresh_status, t)
            if ok and t.reboot and action == "apply" and self.store.is_applied(t.id):
                self.post(self._mark_reboot, t.name)
        threading.Thread(target=worker, daemon=True).start()

    def _mark_reboot(self, name):
        if name not in self.pending_reboot:
            self.pending_reboot.append(name)
        self.btn_restart.configure(state="normal")


    # ================================================================== FORTNITE CONFIG
    def page_config(self):
        page = ctk.CTkFrame(self, fg_color="transparent")
        self.header(page, "Fortnite Config Editor", f"Edits GameUserSettings.ini directly. A one-time backup ({BAK_SUFFIX}) is made before the first change.")
        box = ctk.CTkFrame(page, fg_color=C_CARD, corner_radius=12)
        box.pack(fill="x", pady=(0, 10))
        self.lbl_ini = ctk.CTkLabel(box, text="", font=ctk.CTkFont(family="Consolas", size=11), wraplength=780, justify="left")
        self.lbl_ini.pack(anchor="w", padx=18, pady=(12, 2))
        self.lbl_fn_run = ctk.CTkLabel(box, text="", font=self.font(12, "bold"))
        self.lbl_fn_run.pack(anchor="w", padx=18)
        self.lbl_cfg_fps = ctk.CTkLabel(box, text=f"FPS cap: {self._fps_text()} (change on the Dashboard)", font=self.font(12), text_color=C_MUT)
        self.lbl_cfg_fps.pack(anchor="w", padx=18, pady=(2, 10))
        rb = ctk.CTkFrame(box, fg_color="transparent")
        rb.pack(fill="x", padx=18, pady=(0, 14))
        self.action_button(rb, "Refresh", self.refresh_fn_status, kind="dark", width=90, height=30).pack(side="left", padx=(0, 6))
        self.action_button(rb, "Open folder", self.open_fn_folder, kind="dark", width=110, height=30).pack(side="left", padx=6)
        self.action_button(rb, "Restore original ini", self.restore_ini, kind="dark", width=150, height=30).pack(side="left", padx=6)

        grid = ctk.CTkFrame(page, fg_color=C_CARD, corner_radius=12)
        grid.pack(fill="x", pady=(0, 10))
        inner = ctk.CTkFrame(grid, fg_color="transparent")
        inner.pack(fill="x", padx=18, pady=14)
        self.action_button(inner, "APPLY FULL PERFORMANCE PROFILE  (Performance Mode + All Low + Resolution + FPS cap)", lambda: self.fn_do("profile"), height=42).pack(fill="x", pady=(0, 8))
        two = ctk.CTkFrame(inner, fg_color="transparent")
        two.pack(fill="x", pady=4)
        two.grid_columnconfigure((0, 1), weight=1)
        self.action_button(two, "Force Performance Mode (DX11 · ES3.1)", lambda: self.fn_do("perf"), kind="purple").grid(row=0, column=0, padx=(0, 4), sticky="ew")
        self.action_button(two, "Force ALL Scalability Groups to Low (0)", lambda: self.fn_do("low"), kind="purple").grid(row=0, column=1, padx=(4, 0), sticky="ew")

        hdr = ctk.CTkFrame(inner, fg_color="transparent")
        hdr.pack(fill="x", pady=(12, 4))
        self.lbl_res_title = ctk.CTkLabel(hdr, text="Resolution", font=self.font(12, "bold"), text_color=C_ACC)
        self.lbl_res_title.pack(side="left")
        self.action_button(hdr, "Re-detect display", self.redetect_display, kind="dark", width=130, height=26).pack(side="right")
        self.res_frame = ctk.CTkFrame(inner, fg_color="transparent")
        self.res_frame.pack(fill="x")
        self.lbl_res_sel = ctk.CTkLabel(inner, text="", font=self.font(11), text_color=C_MUT)
        self.lbl_res_sel.pack(anchor="w", pady=(4, 0))
        self._build_res_buttons()

        sl = ctk.CTkFrame(inner, fg_color="transparent")
        sl.pack(fill="x", pady=(10, 0))
        self.lbl_q = ctk.CTkLabel(sl, text=f"3D resolution: {self.res_quality}%", font=self.font(12), text_color=C_TXT, width=150, anchor="w")
        self.lbl_q.pack(side="left")
        slider = ctk.CTkSlider(sl, from_=50, to=100, number_of_steps=10, progress_color=C_ACC, button_color=C_ACC, command=self._on_q)
        slider.set(self.res_quality)
        slider.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(inner, text="Resolution buttons also set exclusive Fullscreen (windowed fullscreen ignores custom resolutions). "
                                 "The 3D resolution slider renders at a lower internal resolution and scales up - a good way to gain FPS without changing your display mode; "
                                 "values under about 66% look blurry. For weaker or integrated GPUs, pick one step below native and lower the slider.",
                     font=self.font(11), text_color=C_MUT, wraplength=780, justify="left").pack(anchor="w", pady=(8, 0))
        lk = ctk.CTkFrame(page, fg_color="transparent")
        lk.pack(fill="x")
        self.action_button(lk, "Lock ini (read-only)", lambda: self.set_ini_ro(True), kind="dark", width=170, height=32).pack(side="left", padx=(0, 6))
        self.action_button(lk, "Unlock ini", lambda: self.set_ini_ro(False), kind="dark", width=120, height=32).pack(side="left")
        return page

    def _build_res_buttons(self):
        """Resolution presets generated from THIS monitor's aspect ratio and the modes its driver supports."""
        for w in self.res_frame.winfo_children():
            w.destroy()
        d = self.display
        ps_ = d.presets()
        if d.width:
            self.lbl_res_title.configure(text=f"Resolution · {ps_.aspect} display detected ({d.width} x {d.height}"
                                              + (f" @ {d.refresh} Hz)" if d.refresh else ")"))
        else:
            self.lbl_res_title.configure(text="Resolution · display not detected (showing a common 16:9 ladder)")
        groups = [(None, ps_.main)] + ([("Stretched 4:3 (competitive). Needs GPU / display scaling set to Full-screen to fill the monitor; ForgeFN doesn't change that.", ps_.stretched)] if ps_.stretched else [])
        for title, presets in groups:
            if title:
                ctk.CTkLabel(self.res_frame, text=title, font=self.font(11), text_color=C_MUT, wraplength=780, justify="left").pack(anchor="w", pady=(8, 3))
            row = None
            for i, p in enumerate(presets):
                if i % 3 == 0:
                    row = ctk.CTkFrame(self.res_frame, fg_color="transparent")
                    row.pack(fill="x", pady=2)
                    row.grid_columnconfigure((0, 1, 2), weight=1, uniform="r")
                self.action_button(row, p.label, lambda a=p.w, b=p.h: self.fn_do("res", (a, b)), kind="dark").grid(row=0, column=i % 3, padx=3, sticky="ew")
        self._show_res_sel()

    def _show_res_sel(self):
        if self.res_choice:
            self.lbl_res_sel.configure(text=f"Selected for the profile: {self.res_choice[0]} x {self.res_choice[1]} at {self.res_quality}% 3D resolution")
        else:
            self.lbl_res_sel.configure(text="No resolution selected - the profile leaves your current resolution alone.")

    def redetect_display(self):
        self.display = detect_display()
        self.log(f"Display re-detected: {self.display.width} x {self.display.height} @ {self.display.refresh} Hz ({self.display.aspect})")
        self._build_res_buttons()

    def _on_q(self, v):
        self.res_quality = int(round(v))
        self.lbl_q.configure(text=f"3D resolution: {self.res_quality}%")
        self._show_res_sel()

    def refresh_fn_status(self):
        found = os.path.isfile(FN_INI)
        self.lbl_ini.configure(text=f"{'✔ Found' if found else '✖ Not found (launch Fortnite once)'}: {FN_INI}", text_color=C_ACC if found else C_BAD)
        self._show_fn_running(fortnite_running())

    def open_fn_folder(self):
        d = os.path.dirname(FN_INI)
        if os.path.isdir(d):
            os.startfile(d)  # noqa: S606


    def _fn_profile(self):
        edits = edits_performance_mode() + edits_low_quality() + edits_fps(self.target_fps)
        if self.res_choice:
            edits += edits_resolution(self.res_choice[0], self.res_choice[1], self.res_quality)
        return fn_ini_edit(edits)

    def fn_do(self, what, arg=None):
        def worker():
            with self.work_lock:
                if what == "perf":
                    ok, msg = fn_ini_edit(edits_performance_mode())
                elif what == "low":
                    ok, msg = fn_ini_edit(edits_low_quality())
                elif what == "res":
                    self.res_choice = arg
                    ok, msg = fn_ini_edit(edits_resolution(arg[0], arg[1], self.res_quality))
                    if ok:
                        msg = f"Resolution {arg[0]}x{arg[1]} at {self.res_quality}% 3D resolution, exclusive fullscreen. " + msg
                    self.post(self._show_res_sel)
                else:
                    ok, msg = self._fn_profile()
            self.log(("✔ " if ok else "✖ ") + "Fortnite config: " + msg)
        threading.Thread(target=worker, daemon=True).start()

    def restore_ini(self):
        bak = ini_backup_path()
        if not os.path.isfile(bak):
            self.log("✖ No backup found yet - nothing to restore.")
            return
        if fortnite_running():
            self.log("✖ Close Fortnite first.")
            return
        try:
            if os.path.exists(FN_INI):
                os.chmod(FN_INI, stat.S_IREAD | stat.S_IWRITE)
            shutil.copy2(bak, FN_INI)
            self.log("✔ Original GameUserSettings.ini restored.")
        except OSError as e:
            self.log(f"✖ Restore failed: {e}")

    def set_ini_ro(self, lock):
        if not os.path.isfile(FN_INI):
            self.log("✖ ini not found.")
            return
        os.chmod(FN_INI, stat.S_IREAD if lock else stat.S_IREAD | stat.S_IWRITE)
        self.log("✔ GameUserSettings.ini is now " + ("READ-ONLY (Fortnite can't overwrite it; unlock before using in-game settings)." if lock else "writable."))


    # ================================================================== CLEANER
    def visible_clean_targets(self):
        """Vendor-specific shader caches only show up when that GPU vendor is present."""
        return [t for t in CLEAN_TARGETS
                if t.vendor is None or (self.profile is not None and self.profile.has_gpu(t.vendor))]

    def page_cleaner(self):
        page = ctk.CTkFrame(self, fg_color="transparent")
        self.header(page, "System Cleaner", "Frees disk space. Tick what to clean - sizes are scanned live.")
        box = ctk.CTkFrame(page, fg_color=C_CARD, corner_radius=12)
        box.pack(fill="x", pady=(0, 10))
        self.clean_rows = ctk.CTkFrame(box, fg_color="transparent")
        self.clean_rows.pack(fill="x")
        self.lbl_clean_total = ctk.CTkLabel(box, text="", font=self.font(13, "bold"), text_color=C_TXT)
        self.lbl_clean_total.pack(anchor="w", padx=18, pady=(8, 4))
        bf = ctk.CTkFrame(box, fg_color="transparent")
        bf.pack(fill="x", padx=18, pady=(0, 16))
        self.action_button(bf, "Rescan sizes", self.scan_cleaner, kind="dark", width=130).pack(side="left", padx=(0, 8))
        self.action_button(bf, "CLEAN SELECTED", self.run_cleaner, kind="danger", width=200).pack(side="left")
        self._populate_cleaner()
        return page

    def _populate_cleaner(self):
        for w in self.clean_rows.winfo_children():
            w.destroy()
        self.clean_vars, self.clean_size_lbls = {}, {}
        for t in self.visible_clean_targets():
            row = ctk.CTkFrame(self.clean_rows, fg_color="transparent")
            row.pack(fill="x", padx=18, pady=5)
            var = tk.BooleanVar(value=t.default)
            self.clean_vars[t.cid] = var
            ctk.CTkCheckBox(row, text=t.label, variable=var, fg_color=C_ACC, hover_color=C_ACC_H, checkmark_color="#000", text_color=C_TXT, width=280).pack(side="left")
            lbl = ctk.CTkLabel(row, text="…", width=90, font=self.font(12, "bold"), text_color=C_ACC)
            lbl.pack(side="left")
            self.clean_size_lbls[t.cid] = lbl
            if t.note:
                ctk.CTkLabel(row, text=t.note, font=self.font(11), text_color=C_MUT, wraplength=420, justify="left").pack(side="left", padx=8)

    def scan_cleaner(self):
        targets = self.visible_clean_targets()

        def worker():
            sizes = {t.cid: sum(dir_size(_exp(p)) for p in t.paths if valid_clean_path(_exp(p))) for t in targets}
            self.post(self._show_clean_sizes, sizes)
        threading.Thread(target=worker, daemon=True).start()

    def _show_clean_sizes(self, sizes):
        for cid, sz in sizes.items():
            if cid in self.clean_size_lbls:
                self.clean_size_lbls[cid].configure(text=human_size(sz))
        sel = sum(sz for cid, sz in sizes.items() if cid in self.clean_vars and self.clean_vars[cid].get())
        self.lbl_clean_total.configure(text=f"Selected: about {human_size(sel)} can be freed")

    def run_cleaner(self):
        chosen = [t for t in self.visible_clean_targets() if t.cid in self.clean_vars and self.clean_vars[t.cid].get()]
        if not chosen:
            return

        def worker():
            with self.work_lock:
                self.log("Cleaning selected locations…")
                freed = 0
                for t in chosen:
                    got = sum(purge_dir(_exp(p)) for p in t.paths if valid_clean_path(_exp(p)))
                    freed += got
                    self.log(f"  {t.label}: freed {human_size(got)}")
                self.log(f"✔ Cleanup complete - {human_size(freed)} freed.")
            self.scan_cleaner()
        threading.Thread(target=worker, daemon=True).start()

    # ================================================================== BACKUP & SAFETY
    def page_backup(self):
        page = ctk.CTkFrame(self, fg_color="transparent")
        self.header(page, "Backup & Safety", "Anti-cheat compliance, plus safety nets for every change this app makes.")

        ac = ctk.CTkFrame(page, fg_color=C_CARD, corner_radius=12, border_width=1, border_color=C_OK)
        ac.pack(fill="x", pady=(0, 10))
        ctk.CTkLabel(ac, text="🛡  Anti-cheat compliance (Epic Games EAC / BattlEye)", font=self.font(15, "bold"), text_color=C_OK).pack(anchor="w", padx=18, pady=(14, 4))
        ctk.CTkLabel(ac, text=ANTICHEAT_ALLOWED, font=self.font(12), text_color=C_TXT, wraplength=780, justify="left").pack(anchor="w", padx=18, pady=(0, 6))
        ctk.CTkLabel(ac, text=ANTICHEAT_NEVER, font=self.font(12), text_color=C_TXT, wraplength=780, justify="left").pack(anchor="w", padx=18, pady=(0, 6))
        ctk.CTkLabel(ac, text=ANTICHEAT_FINEPRINT, font=self.font(11), text_color=C_MUT, wraplength=780, justify="left").pack(anchor="w", padx=18, pady=(0, 14))

        box = ctk.CTkFrame(page, fg_color=C_CARD, corner_radius=12)
        box.pack(fill="x", pady=(0, 10))
        ctk.CTkLabel(box, text="Before you optimize", font=self.font(15, "bold"), text_color=C_ACC).pack(anchor="w", padx=18, pady=(14, 2))
        ctk.CTkLabel(box, text="A System Restore point snapshots Windows so you can roll back from the recovery menu. The registry export saves every branch these tweaks touch as .reg files you can double-click to restore.",
                     font=self.font(12), text_color=C_MUT, wraplength=780, justify="left").pack(anchor="w", padx=18, pady=(0, 10))
        self.action_button(box, "CREATE SYSTEM RESTORE POINT", self.do_restore_point, kind="purple").pack(fill="x", padx=18, pady=4)
        self.action_button(box, "EXPORT REGISTRY BACKUP (.REG FILES)", self.do_reg_backup, kind="dark").pack(fill="x", padx=18, pady=4)
        self.action_button(box, "Open backup folder", self.open_backup_folder, kind="dark", height=32).pack(anchor="w", padx=18, pady=(4, 16))
        box2 = ctk.CTkFrame(page, fg_color=C_CARD, corner_radius=12)
        box2.pack(fill="x")
        ctk.CTkLabel(box2, text="Undo everything", font=self.font(15, "bold"), text_color=C_ACC).pack(anchor="w", padx=18, pady=(14, 2))
        self.lbl_applied = ctk.CTkLabel(box2, text="", font=self.font(12), text_color=C_MUT)
        self.lbl_applied.pack(anchor="w", padx=18)
        self.action_button(box2, "UNDO ALL APPLIED TWEAKS", self.undo_all, kind="danger", width=260).pack(anchor="w", padx=18, pady=(10, 16))
        return page

    def refresh_backup_status(self):
        n = len(self.store.applied_ids())
        self.lbl_applied.configure(text=f"{n} tweak(s) currently applied. Original values are stored in {STATE_FILE}")

    def do_restore_point(self):
        def worker():
            with self.work_lock:
                self.log("Creating restore point…")
                ok, msg = create_restore_point()
            self.log(("✔ " if ok else "✖ ") + msg)
        threading.Thread(target=worker, daemon=True).start()

    def do_reg_backup(self):
        def worker():
            with self.work_lock:
                ok, msg, _f = export_registry_backup()
            self.log(("✔ " if ok else "✖ ") + msg)
        threading.Thread(target=worker, daemon=True).start()

    def open_backup_folder(self):
        os.makedirs(BACKUP_ROOT, exist_ok=True)
        os.startfile(BACKUP_ROOT)  # noqa: S606

    def undo_all(self):
        ids = [i for i in self.store.applied_ids() if i in TWEAK_BY_ID]
        if not ids:
            self.log("Nothing to undo.")
            return
        if not messagebox.askyesno(APP_NAME, f"Undo {len(ids)} applied tweak(s) and restore their original values?"):
            return

        def worker():
            with self.work_lock:
                for tid in reversed(ids):
                    t = TWEAK_BY_ID[tid]
                    ok, msg = self._run_tweak(t, "undo")
                    self.log(f"{'✔' if ok else '✖'} Undo · {t.name}: {msg}")
                    self.post(self._refresh_status, t)
            self.post(self.refresh_backup_status)
        threading.Thread(target=worker, daemon=True).start()



def main():
    ForgeApp().mainloop()


if __name__ == "__main__":
    main()
