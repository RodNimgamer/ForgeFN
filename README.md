# ForgeFN // Fortnite Performance Engine & Game Optimizer

ForgeFN is a transparent, open-source, and reversible performance tuner designed to stabilize frame times and reduce latency in Fortnite on Windows 10 & 11.

## ✨ Features
- **Dynamic Hardware Scanner**: Detects your exact CPU, GPU vendor (NVIDIA/AMD/Intel), RAM, and chassis type.
- **Adaptive Memory Management**: Automatically sizes the pagefile based on installed RAM (aggressive overflow sizing for <=8GB, system-managed for 32GB+).
- **Aspect-Ratio-Aware Resolutions**: Offers resolution ladders matching your display (16:9, 16:10, 21:9, and competitive 4:3 stretched).
- **Direct `GameUserSettings.ini` Editor**: One-click Performance Mode (DX11 ES3.1) and all-low scalability enforcement.
- **100% Reversible**: Every tweak snapshots its original state in `%ProgramData%\ForgeFN\state.json` before applying.

## 🛡️ Anti-Cheat Compliance
ForgeFN is built to operate strictly within Epic Games' Easy Anti-Cheat (EAC) and BattlEye guidelines:
- **No file modifications**: Game binaries and launcher files are never modified or replaced.
- **No memory injection**: Does not read, hook, or inject into game memory.
- **Standard Windows API**: All changes are limited to Windows Registry values, powercfg profiles, and Fortnite's user `.ini` file.

## 🚀 Running from Source
```bash
git clone https://github.com/<YOUR-USERNAME>/ForgeFN.git
cd ForgeFN
pip install -r requirements.txt
python forgefn.py
