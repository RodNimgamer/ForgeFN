# ForgeFN // Fortnite Performance Engine & Game Optimizer

**ForgeFN (v3.0.0)** is a transparent, open-source, and reversible performance tuner designed to stabilize frame times, lower network ping/jitter, and reduce input latency in Fortnite on Windows 10 & 11.

---

## ✨ Features

* **🌐 Dedicated Network & Ping Engine:**
  * **Fortnite Regional Ping & Jitter Tester:** Live latency, jitter, and packet-loss test across all 8 official Epic Games server regions (`EU`, `NA-East`, `NA-Central`, `NA-West`, `Oceania`, `Brazil`, `Asia`, `Middle East`).
  * **Gaming DNS Switcher:** One-click switch to Cloudflare (`1.1.1.1`), Google (`8.8.8.8`), Quad9 (`9.9.9.9`), or AdGuard (`94.140.14.14`) with automatic original-DNS snapshot and restore.
  * **Network Stack Optimizations:** Disable Nagle's Algorithm (`TCPNoDelay`), tune TCP RSS / disable RSC coalescing, disable NIC Power Saving & Energy-Efficient Ethernet (EEE), and apply Fortnite QoS DSCP 46 priority.
* **⚡ Zero-Delay Input & Latency Library:** ~60 hardware-aware, searchable tweaks including Windows 11 Global 1ms Timer Resolution, low-latency mouse/keyboard buffer queues, 1:1 raw mouse precision, MMCSS Games scheduling, and High-Performance GPU enforcement.
* **🔍 Dynamic Hardware Scanner:** Detects your exact CPU, GPU vendor (NVIDIA/AMD/Intel), RAM, storage type (SSD/HDD), and chassis type (Desktop/Laptop) to hide incompatible tweaks.
* **🧠 Adaptive Memory Management:** Automatically sizes the pagefile based on installed RAM (`<=8GB` fixed 1.5x, `16GB` 8–12GB, `32GB+` system-managed) and keeps kernel/drivers in RAM on 16 GB+ systems.
* **🎮 Direct `GameUserSettings.ini` Editor:** One-click Performance Mode (`DX11` `ES3.1`), all-low scalability enforcement, safe **Competitive Potato Graphics** (low meshes + 66% 3D resolution without disappearing floors), and aspect-ratio-aware resolutions (`16:9`, `16:10`, `21:9`, and `4:3` stretched).
* **🧹 Deep System Cleaner:** Clears DirectX / NVIDIA / AMD / Intel shader caches, Windows Update download cache, WER crash dumps, Fortnite logs, temp files, and the Recycle Bin.
* **↩️ 100% Reversible:** Every tweak snapshots its original state in `%ProgramData%\ForgeFN\state.json` before applying, with System Restore point & `.reg` backup export support.

---

## 🛡️ Anti-Cheat Compliance

ForgeFN is built to operate strictly within Epic Games' **Easy Anti-Cheat (EAC)** and **BattlEye** guidelines:

* **No file modifications:** Game binaries and launcher files are never modified or replaced.
* **No memory injection:** Does not read, hook, or inject into game memory or open handles to Fortnite/anti-cheat processes.
* **Standard Windows APIs only:** All changes are limited to documented Windows Registry values, `powercfg` profiles, `netsh`/QoS policies, service start types, and Fortnite's user `.ini` file.

---

## 🚀 Running from Source

```bash
git clone https://github.com/RodNimgamer/ForgeFN.git
cd ForgeFN
pip install -r requirements.txt
python forgefn.py
(If pip is not recognized on Windows, use py -m pip install -r requirements.txt and py forgefn.py.)
📦 Building a Clean .exe (Optional)
To compile a standalone Windows build without UPX or --onefile packer heuristics:
Bash
pip install pyinstaller
python build_exe.py
The compiled application will be output to dist\ForgeFN\ForgeFN.exe (keep the _internal folder in the same directory as ForgeFN.exe).
