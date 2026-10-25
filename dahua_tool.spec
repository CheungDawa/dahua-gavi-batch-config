# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — 大华视图库批量配置工具

打包内容：
  - Python 运行时 + 全部 py 模块
  - PySide6 GUI 框架
  - Playwright + Chromium 浏览器（Playwright 引擎依赖）
  - 大华 DSL DLL（dhnetsdk / dhconfigsdk 等，SDK 路线备用）
  - requests / cryptography 等第三方库
"""

import os
import sys
import shutil
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

# ── 路径常量 ────────────────────────────────────────────
PROJECT_ROOT = Path(SPECPATH)
SRC_DIR = PROJECT_ROOT / "src"

# Playwright
PW_PKG = Path(sys.prefix) / "Lib" / "site-packages" / "playwright"
PW_DRIVER = PW_PKG / "driver"

# Chromium 浏览器（取最新的 chromium-XXXX）
ms_pw = Path(os.environ["LOCALAPPDATA"]) / "ms-playwright"
chromium_dirs = sorted(ms_pw.glob("chromium-*"), reverse=True)
CHROMIUM_DIR = chromium_dirs[0] if chromium_dirs else None

# 大华 DLL
DAHUA_DLL_DIR = Path(r"C:\Program Files (x86)\ConfigTool\ConfigTool")

# ── 收集数据文件 ────────────────────────────────────────
datas = []

# 1) src 模块（含 gui_app.html）
datas += [(str(SRC_DIR), "src")]

# 2) Playwright driver（node.exe + package）
if PW_DRIVER.exists():
    datas.append((str(PW_DRIVER), "playwright/driver"))
    print(f"[spec] Playwright driver -> {PW_DRIVER}")

# 3) Playwright Python 包数据
datas += collect_data_files("playwright")

# 4) Chromium 浏览器
if CHROMIUM_DIR and CHROMIUM_DIR.exists():
    datas.append((str(CHROMIUM_DIR), f"ms-playwright/{CHROMIUM_DIR.name}"))
    print(f"[spec] Chromium -> {CHROMIUM_DIR}")
else:
    print("[spec] 警告: 未找到 Chromium 浏览器目录")

# 5) 大华 DLL（全部 .dll）
if DAHUA_DLL_DIR.exists():
    for dll in DAHUA_DLL_DIR.glob("*.dll"):
        datas.append((str(dll), "dahua_dll"))
    # 也拷贝配置目录
    cfg_dir = DAHUA_DLL_DIR / "Config"
    if cfg_dir.exists():
        datas.append((str(cfg_dir), "dahua_dll/Config"))
    print(f"[spec] 大华 DLL -> {DAHUA_DLL_DIR} ({len(list(DAHUA_DLL_DIR.glob('*.dll')))} files)")
else:
    print("[spec] 警告: 未找到大华 DLL 目录")

# ── 收集隐藏导入 ────────────────────────────────────────
hiddenimports = []
hiddenimports += collect_submodules("src")
hiddenimports += collect_submodules("PySide6")
hiddenimports += collect_submodules("playwright")
hiddenimports += [
    "requests",
    "urllib3",
    "certifi",
    "charset_normalizer",
    "idna",
    "cryptography",
]

# ── 二进制文件（DLL）────────────────────────────────────
binaries = []
# 大华 DLL 作为二进制也加一份（确保 PyInstaller 分析依赖）
if DAHUA_DLL_DIR.exists():
    for dll in DAHUA_DLL_DIR.glob("*.dll"):
        binaries.append((str(dll), "dahua_dll"))

# ── 分析 ────────────────────────────────────────────────

a = Analysis(
    [str(SRC_DIR / "__main__.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "test", "unittest", "pydoc", "asyncio.unix_events"],
    noarchive=False,
)

pyz = PYZ(a.pure)

# ── EXE ────────────────────────────────────────────────
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="大华视图库批量配置工具",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX 压缩对大文件不友好且可能被杀软误报
    console=True,  # 保留 console，GUI 启动后自动隐藏
    disable_windowed_traceback=False,
    icon=None,  # 如有 icon.ico 可指定
)

# ── 目录结构 ────────────────────────────────────────────
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="大华视图库批量配置工具",
)
