# 大华 IPC 视图库（GA/T 1400）批量配置工具

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python: 3.9+](https://img.shields.io/badge/python-3.9+-brightgreen.svg)](https://www.python.org/)
[![Platform: Windows](https://img.shields.io/badge/platform-Windows-lightgrey.svg)]()
[![Standard: GA/T 1400](https://img.shields.io/badge/standard-GA%2FT%201400-orange.svg)]()

专为安防与弱电工程设计的大华 IPC 批量视图库配置工具。通过 CSV / XLSX 模板为海量大华网络摄像机（Web5.0 固件）极速下发**视图库（GAVI / GA/T 1400）**接入参数，支持每台设备唯一的 20 位国标设备编号与通道编码。

---

## 核心特性

- **超高速 RPC 引擎**：深入逆向大华 Web5.0 的 RPC2 `configManager.getConfig("VSP_GAVI")` 接口，纯 HTTP/JSON 直读直写，**单台仅需 ~1 秒，800+ 台实测 30 秒内全部完成**。
- **Playwright UI 兜底引擎**：内置浏览器模拟表单操作，作为对部分老旧/定制固件的兼容兜底方案。
- **TCP 快速预检**：下发前并发探测 80 端口（2 秒超时），秒级剔除掉线/死机设备，避免高并发下超时等待堆积。
- **长连接复用（Keep-Alive）**：同一设备多步请求复用底层 HTTP 连接，大幅提升下发吞吐，降低嵌入式设备端口开销。
- **断点续传（`--only-failed`）**：自动提取上次结果文件中的失败项，网络波动或部分设备离线后可一键重跑增量。
- **长数字编号保护**：针对 Excel/WPS 易将 20 位国标数字编码转为科学计数法或丢失前导零的痛点，内建文本格式映射，支持 104 万行 XLSX。
- **多端支持**：提供 **PySide6 桌面 GUI**、**本地 Web GUI** 以及 **无头 CLI 命令行** 三种使用方式。

---

## 引擎对比

| 指标 | RPC 引擎（推荐默认） | Playwright 引擎（兜底） |
| :--- | :--- | :--- |
| **用法** | `--engine rpc`（默认） | `--engine playwright` |
| **原理** | RPC2 直读直写 `VSP_GAVI` 明文配置节 | 无头 Chromium 模拟表单点击 |
| **单台耗时** | **~1 秒** | ~6-8 秒 |
| **建议并发** | **30 - 50** | 5 - 15 |
| **资源占用** | 极低（纯 HTTP JSON） | 较高（每个 Worker 对应一个 Chromium 进程） |
| **820 台耗时**| **~30 秒** | 9 - 11 分钟 |
| **固件要求** | 固件暴露 `VSP_GAVI` 配置节 | 任意 Web5.0 固件 |

---

## 安装与依赖

```powershell
# 1. 克隆仓库
git clone https://github.com/your-username/dahua-gavi-batch-config.git
cd dahua-gavi-batch-config

# 2. 安装 Python 依赖
pip install -r requirements.txt

# 3. （可选）安装 Playwright 浏览器内核（仅使用 Playwright 兜底引擎时需要）
playwright install chromium
```

---

## 快速上手

### 1. 桌面 GUI 模式

#### 方式一：直接运行（需 Python 环境）
```powershell
python -m src gui
```

#### 方式二：一键打包免安装绿色版
```powershell
powershell -ExecutionPolicy Bypass -File build.ps1
```
打包后生成的 `dist\大华视图库批量配置工具\` 目录为独立绿色文件夹，拷贝给现场运维人员即可双击使用。

---

### 2. 命令行 CLI 模式

```powershell
# 1. 校验模板格式与字段有效性
python -m src validate config_template.csv
python -m src validate config_template.xlsx

# 2. 小批量验证（dry-run 预演只读不写）
python -m src import config_template.csv -w 10 --dry-run

# 3. 全量批量下发（支持 CSV 或 XLSX，并发推荐 30）
python -m src import devices.xlsx -w 30 --result run1.xlsx

# 4. 针对失败设备断点续传（自动从上次结果提取 FAIL 项重试）
python -m src import devices.xlsx -w 30 --only-failed --result run2.xlsx
```

---

## 配置模板说明

项目提供标准样例模板：[`config_template.csv`](config_template.csv) 与 [`config_template.xlsx`](config_template.xlsx)。

| 列名 | 说明 | 对应 VSP_GAVI 字段 |
| :--- | :--- | :--- |
| `device_ip` / `username` / `password` | 设备登录信息（必填） | — |
| `config_index` | 视图库配置序号（1-based） | `RegisterServer[i]` 数组下标 |
| `enable` | 是否启用（TRUE/FALSE） | `Enable` |
| `server_ip` / `server_port` | 视图库服务器地址与端口 | `Address` / `Port` |
| `keepalive` | 心跳周期（10-90 秒） | `KeepAlive` |
| `max_timeout` | 最大心跳超时次数（2-5 次） | `MaxTimeoutTimes` |
| `device_id` | 设备国标编码（**20位，每台唯一**） | `DeviceID` |
| `user_id` / `password_viid` | 视图库认证账号与密码（留空保留原密码） | `UserId` / `Password` |
| `reg_interval` | 注册重发间隔（30-300 秒） | `RegInterval` |
| `platform` | 接入识别码平台 Code | `Platform` |
| `channel` / `channel_encode` | 通道索引（从 0 开始）与通道国标编码（20位） | `ChannelInfoList[c].ChannelEncode` |
| `collect_*` | 采集类型开关（人脸、车辆、人员等） | `NotifyObjectList.*.Checked` |

> [!NOTE]
> `password_viid` 留空或填 `******` 表示**不修改**设备现有视图库密码（原样保留），避免因模板明文泄露现场密码。

---

## 辅助诊断命令

```powershell
# 从已配置好的单台标杆设备导出配置到 CSV 做模板
python -m src export --device 192.168.1.108 --username admin --password 'your_password' -o exported.csv

# 测试 RPC2 登录
python -m src test-login --device 192.168.1.108 --username admin --password 'your_password'

# 探测设备支持的配置节名称
python -m src probe --device 192.168.1.108 --username admin --password 'your_password' --name VSP_GAVI

# 抓包录制视图库接口请求（用于分析特殊定制新固件）
python -m src sniff --device 192.168.1.108 --username admin --password 'your_password'
```

---

## 常见问题排查

| 现象 | 原因与解决方案 |
| :--- | :--- |
| **刚下发完回读显示“离线”** | **属正常现象**。IPC 向视图库平台建立 TCP/SIP 连接与心跳通常需要 5~15 秒，且取决于平台侧是否已添加该国标编码授权。 |
| **TCP 预检不可达** | 设备断电或内网未打通，程序已自动标记为 FAIL，待设备恢复后使用 `--only-failed` 续传即可。 |
| **读取 VSP_GAVI 失败** | 该老旧固件未暴露此 JSON 节，可使用 `--engine playwright` 走浏览器 UI 自动化兜底配置。 |
| **RegisterServer[i] 不存在** | `config_index` 超出设备当前已有条目数，需确保设备支持多配置或先在 Web 界面新增序号。 |

---

## 免责声明

本工具仅供合法授权的网络管理员和安防弱电技术人员在专用局域网内进行设备批量运维管理。严禁用于任何未经授权的探测、访问或网络攻击活动。作者不对任何因不当操作导致的设备异常或网络问题承担法律责任。

## 开源协议

本项目基于 [MIT License](LICENSE) 开源。
