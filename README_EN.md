# Dahua IPC GAVI (GA/T 1400) Batch Configuration Tool

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python: 3.9+](https://img.shields.io/badge/python-3.9+-brightgreen.svg)](https://www.python.org/)
[![Platform: Windows](https://img.shields.io/badge/platform-Windows-lightgrey.svg)]()
[![Standard: GA/T 1400](https://img.shields.io/badge/standard-GA%2FT%201400-orange.svg)]()

[中文文档 (Chinese)](README.md) | [English Documentation](README_EN.md)

A high-performance batch configuration tool designed for Dahua IP Cameras (Web 5.0 firmware). Automatically provisions **Video Image Information Library (GAVI / GA/T 1400)** platform parameters across thousands of cameras via CSV / XLSX templates, supporting unique 20-digit national standard device and channel IDs.

---

## Key Features

- **Ultra-fast RPC2 Engine**: Reverse-engineered Dahua Web 5.0 RPC2 `configManager.getConfig("VSP_GAVI")` plain JSON protocol. Direct HTTP read/write takes **~1s per camera** (800+ cameras configured in ~30 seconds).
- **Playwright UI Fallback Engine**: Headless Chromium automation as a fallback for older or custom firmware that doesn't expose the `VSP_GAVI` JSON node.
- **Concurrent TCP Pre-check**: Fast 2-second port 80 probing eliminates offline/dead cameras immediately before launching worker threads.
- **HTTP Keep-Alive**: Persistent connection pooling across multi-step authentication and verification calls.
- **Breakpoint Resumption (`--only-failed`)**: Re-run only failed items from previous execution reports (`.csv` or `.xlsx`).
- **20-digit ID Protection**: Preserves leading zeros and avoids scientific notation format issues in Excel/WPS.
- **Multiple Interfaces**: Supports **Desktop GUI (PySide6)**, **Local Web GUI**, and **Headless CLI**.

---

## Engine Comparison

| Feature | RPC Engine (Default & Recommended) | Playwright Engine (Fallback) |
| :--- | :--- | :--- |
| **Command** | `--engine rpc` | `--engine playwright` |
| **Mechanism** | RPC2 direct read/write `VSP_GAVI` JSON | Headless Chromium form automation |
| **Time per Device** | **~1 second** | ~6-8 seconds |
| **Concurrency** | **30 - 50** | 5 - 15 |
| **Resource Usage**| Extremely low (pure HTTP) | High (Chromium per worker) |
| **820 Devices** | **~30 seconds** | 9 - 11 minutes |

---

## Installation

```powershell
# Clone the repository
git clone https://github.com/your-username/dahua-gavi-batch-config.git
cd dahua-gavi-batch-config

# Install dependencies
pip install -r requirements.txt

# (Optional) Install Playwright Chromium if using fallback engine
playwright install chromium
```

---

## Quick Start

### 1. Desktop GUI

```powershell
python -m src gui
```

Or build a standalone portable executable package:
```powershell
powershell -ExecutionPolicy Bypass -File build.ps1
```

### 2. Command Line (CLI)

```powershell
# Validate template
python -m src validate config_template.csv
python -m src validate config_template.xlsx

# Dry-run test (simulation without writing)
python -m src import config_template.csv -w 10 --dry-run

# Full batch execution
python -m src import devices.xlsx -w 30 --result run1.xlsx

# Retry failed devices only
python -m src import devices.xlsx -w 30 --only-failed --result run2.xlsx
```

---

## Configuration Template

Sample templates: [`config_template.csv`](config_template.csv) and [`config_template.xlsx`](config_template.xlsx).

| Column | Description |
| :--- | :--- |
| `device_ip` / `username` / `password` | Device credentials (required) |
| `config_index` | 1-based index for GAVI server profile |
| `enable` | Enable switch (`TRUE`/`FALSE`) |
| `server_ip` / `server_port` | Target GAVI / GA/T 1400 server address and port |
| `device_id` | 20-digit national standard device ID (unique per device) |
| `user_id` / `password_viid` | Platform authentication username & password |
| `channel` / `channel_encode` | Channel index (0-based) and 20-digit channel code |
| `collect_*` | Data collection toggles (face, motor, non-motor, person, etc.) |

---

## Disclaimer

This tool is strictly intended for authorized network administrators and security engineers to manage devices on authorized internal networks. The authors assume no liability for misuse.

## License

Licensed under the [MIT License](LICENSE).
