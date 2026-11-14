import os
import subprocess
import sys

VERSION_INFO = """VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=(3, 0, 0, 0),
    prodvers=(3, 0, 0, 0),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        u'040904B0',
        [
          StringStruct(u'CompanyName', u'RodNim Open Source'),
          StringStruct(u'FileDescription', u'ForgeFN - Fortnite Performance Engine & Game Optimizer'),
          StringStruct(u'FileVersion', u'3.0.0.0'),
          StringStruct(u'InternalName', u'ForgeFN'),
          StringStruct(u'LegalCopyright', u'MIT License (c) 2026 RodNim'),
          StringStruct(u'OriginalFilename', u'ForgeFN.exe'),
          StringStruct(u'ProductName', u'ForgeFN'),
          StringStruct(u'ProductVersion', u'3.0.0.0')
        ]
      )
    ]),
    VarFileInfo([VarStruct(u'Translation', [1033, 1200])])
  ]
)
"""

with open("version_info.txt", "w", encoding="utf-8") as f:
    f.write(VERSION_INFO)

cmd = [
    sys.executable, "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onedir",
    "--windowed",
    "--noupx",
    "--uac-admin",
    "--name", "ForgeFN",
    "--version-file", "version_info.txt",
    "--collect-all", "customtkinter",
    "forgefn.py",
]

subprocess.check_call(cmd)
print("\nBuilt cleanly to dist\\ForgeFN\\ForgeFN.exe")
