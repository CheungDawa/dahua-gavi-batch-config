"""CSV / XLSX 模板解析与校验。"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REQUIRED_COLUMNS = ("device_ip", "username", "password")

OPTIONAL_COLUMNS = (
    "config_index",
    "enable",
    "server_ip",
    "server_port",
    "keepalive",
    "max_timeout",
    "device_id",
    "user_id",
    "password_viid",
    "reg_interval",
    "platform",
    "channel",
    "channel_encode",
    "collect_face",
    "collect_image",
    "collect_motor",
    "collect_nonmotor",
    "collect_person",
    "collect_file",
    "collect_video",
)

COLLECT_MAP = {
    "collect_face": "Face",
    "collect_image": "Image",
    "collect_motor": "MotorVehicle",
    "collect_nonmotor": "NonMotorVehicle",
    "collect_person": "Person",
    "collect_file": "File",
    "collect_video": "VideoSlice",
}

IPV4_RE = re.compile(
    r"^((25[0-5]|2[0-4][0-9]|1[0-9]{2}|0[0-9]{2}|[0-9]{1,2})\.){3}"
    r"(25[0-5]|2[0-4][0-9]|1[0-9]{2}|0[0-9]{2}|[0-9]{1,2})$"
)


@dataclass
class GaviConfigRow:
    """单行 CSV 配置。"""

    device_ip: str
    username: str
    password: str
    config_index: int = 1
    enable: bool | None = None
    server_ip: str | None = None
    server_port: int | None = None
    keepalive: int | None = None
    max_timeout: int | None = None
    device_id: str | None = None
    user_id: str | None = None
    password_viid: str | None = None
    reg_interval: int | None = None
    platform: int | None = None
    channel: int | None = None
    channel_encode: str | None = None
    collect: dict[str, bool | None] = field(default_factory=dict)
    row_number: int = 0

    def has_gavi_fields(self) -> bool:
        """是否包含除登录信息外的 GAVI 配置字段。"""
        if any(
            value is not None
            for value in (
                self.enable,
                self.server_ip,
                self.server_port,
                self.keepalive,
                self.max_timeout,
                self.device_id,
                self.user_id,
                self.password_viid,
                self.reg_interval,
                self.platform,
                self.channel,
                self.channel_encode,
            )
        ):
            return True
        return any(value is not None for value in self.collect.values())


def parse_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered in {"true", "1", "yes", "y", "是", "on"}:
        return True
    if lowered in {"false", "0", "no", "n", "否", "off"}:
        return False
    raise ValueError(f"无法解析布尔值: {value}")


def parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return int(text)


def _clean_row(raw: dict[str, str | None]) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for key, value in raw.items():
        if key is None:
            continue
        name = key.strip()
        if not name:
            continue
        cleaned[name] = value.strip() if value is not None else ""
    return cleaned


def validate_row(row: GaviConfigRow) -> list[str]:
    """返回校验错误列表，空列表表示通过。"""
    errors: list[str] = []
    prefix = f"第 {row.row_number} 行"

    if not row.device_ip:
        errors.append(f"{prefix}: device_ip 不能为空")
    if not row.username:
        errors.append(f"{prefix}: username 不能为空")
    if not row.password:
        errors.append(f"{prefix}: password 不能为空")
    if row.config_index < 1:
        errors.append(f"{prefix}: config_index 必须 >= 1")

    if row.server_ip and not IPV4_RE.match(row.server_ip):
        errors.append(f"{prefix}: server_ip 格式无效")

    if row.server_port is not None and not (1 <= row.server_port <= 65535):
        errors.append(f"{prefix}: server_port 必须在 1-65535")

    if row.keepalive is not None and not (10 <= row.keepalive <= 90):
        errors.append(f"{prefix}: keepalive 必须在 10-90")

    if row.max_timeout is not None and not (2 <= row.max_timeout <= 5):
        errors.append(f"{prefix}: max_timeout 必须在 2-5")

    if row.reg_interval is not None and not (30 <= row.reg_interval <= 300):
        errors.append(f"{prefix}: reg_interval 必须在 30-300")

    if row.device_id is not None and len(row.device_id) != 20:
        errors.append(f"{prefix}: device_id 必须为 20 位")

    if row.channel_encode is not None and len(row.channel_encode) != 20:
        errors.append(f"{prefix}: channel_encode 必须为 20 位")

    if row.channel is not None and row.channel < 0:
        errors.append(f"{prefix}: channel 不能为负数")

    return errors


def row_from_dict(raw: dict[str, str], row_number: int) -> GaviConfigRow:
    data = _clean_row(raw)
    if not any(data.values()):
        raise ValueError(f"第 {row_number} 行为空")

    first_cell = next(iter(data.values()), "")
    if first_cell.startswith("#"):
        raise ValueError("comment")

    collect: dict[str, bool | None] = {}
    for csv_key, json_key in COLLECT_MAP.items():
        if csv_key in data and data[csv_key] != "":
            collect[json_key] = parse_bool(data[csv_key])

    return GaviConfigRow(
        device_ip=data.get("device_ip", ""),
        username=data.get("username", ""),
        password=data.get("password", ""),
        config_index=parse_int(data.get("config_index")) or 1,
        enable=parse_bool(data.get("enable")),
        server_ip=data.get("server_ip") or None,
        server_port=parse_int(data.get("server_port")),
        keepalive=parse_int(data.get("keepalive")),
        max_timeout=parse_int(data.get("max_timeout")),
        device_id=data.get("device_id") or None,
        user_id=data.get("user_id") or None,
        password_viid=data.get("password_viid") or None,
        reg_interval=parse_int(data.get("reg_interval")),
        platform=parse_int(data.get("platform")),
        channel=parse_int(data.get("channel")),
        channel_encode=data.get("channel_encode") or None,
        collect=collect,
        row_number=row_number,
    )


def load_csv(path: str | Path) -> list[GaviConfigRow]:
    """加载并校验 CSV 或 XLSX，跳过空行与 # 注释行。

    根据文件后缀自动选择解析器：.xlsx 用 openpyxl，其余按 CSV 处理。
    """
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(str(file_path))

    suffix = file_path.suffix.lower()
    if suffix == ".xlsx":
        raw_rows = _read_xlsx(file_path)
    else:
        raw_rows = _read_csv(file_path)

    if not raw_rows:
        raise ValueError("文件中没有数据行")

    headers = [name.strip() for name in raw_rows[0].keys()]
    missing = [col for col in REQUIRED_COLUMNS if col not in headers]
    if missing:
        raise ValueError(f"缺少必填列: {', '.join(missing)}")

    rows: list[GaviConfigRow] = []
    errors: list[str] = []

    for line_no, raw in enumerate(raw_rows, start=2):
        if not any(str(value or "").strip() for value in raw.values()):
            continue
        first_value = next((str(v).strip() for v in raw.values() if v is not None), "")
        if first_value.startswith("#"):
            continue
        try:
            row = row_from_dict(raw, line_no)
            row_errors = validate_row(row)
            if row_errors:
                errors.extend(row_errors)
            else:
                rows.append(row)
        except ValueError as exc:
            if str(exc) != "comment":
                errors.append(f"第 {line_no} 行: {exc}")

    if errors:
        raise ValueError("\n".join(errors))
    if not rows:
        raise ValueError("文件中没有有效数据行")
    return rows


def _read_csv(file_path: Path) -> list[dict[str, str]]:
    """读取 CSV 文件，返回字典列表。"""
    with file_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError("CSV 缺少表头")
        return [dict(raw) for raw in reader]


def _read_xlsx(file_path: Path) -> list[dict[str, str]]:
    """读取 XLSX 文件，返回字典列表（第一行为表头）。

    对长数字编号（如 20 位国标码），openpyxl 可能将其解析为数值丢失前导零，
    因此对数值类型的单元格回查原始字符串值。
    """
    import openpyxl
    wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
    ws = wb.active
    rows_iter = ws.iter_rows(values_only=False)
    try:
        header_cells = next(rows_iter)
    except StopIteration:
        wb.close()
        raise ValueError("XLSX 为空")
    headers = [str(cell.value).strip() if cell.value is not None else "" for cell in header_cells]

    # 需要保持文本格式的列（长数字编号）
    text_cols = {"device_id", "channel_encode", "user_id", "password_viid", "device_ip"}

    result: list[dict[str, str]] = []
    for row_cells in rows_iter:
        row_dict: dict[str, str] = {}
        for i, cell in enumerate(row_cells):
            if i >= len(headers) or not headers[i]:
                continue
            col_name = headers[i]
            val = cell.value
            if val is None:
                row_dict[col_name] = ""
            elif col_name in text_cols:
                # 编号类字段：强制保持字符串
                row_dict[col_name] = str(val).strip()
            elif isinstance(val, float):
                # 浮点数且为整数时去掉小数点
                if val == int(val):
                    row_dict[col_name] = str(int(val))
                else:
                    row_dict[col_name] = str(val).strip()
            else:
                row_dict[col_name] = str(val).strip()
        result.append(row_dict)
    wb.close()
    return result


def row_to_export_dict(row: GaviConfigRow) -> dict[str, Any]:
    """导出为 CSV 字典。"""
    data: dict[str, Any] = {
        "device_ip": row.device_ip,
        "username": row.username,
        "password": row.password,
        "config_index": row.config_index,
        "enable": row.enable,
        "server_ip": row.server_ip,
        "server_port": row.server_port,
        "keepalive": row.keepalive,
        "max_timeout": row.max_timeout,
        "device_id": row.device_id,
        "user_id": row.user_id,
        "password_viid": row.password_viid or "",
        "reg_interval": row.reg_interval,
        "platform": row.platform,
        "channel": row.channel,
        "channel_encode": row.channel_encode,
    }
    for csv_key, json_key in COLLECT_MAP.items():
        data[csv_key] = row.collect.get(json_key)
    return data


EXPORT_FIELDNAMES = list(REQUIRED_COLUMNS) + list(OPTIONAL_COLUMNS)


def write_rows(path: str | Path, rows: list[dict[str, Any]]) -> None:
    """写出配置行到 CSV 或 XLSX（根据后缀自动选择）。"""
    file_path = Path(path)
    suffix = file_path.suffix.lower()
    if suffix == ".xlsx":
        _write_xlsx(file_path, rows)
    else:
        _write_csv(file_path, rows)


def _write_csv(file_path: Path, rows: list[dict[str, Any]]) -> None:
    with file_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=EXPORT_FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _write_xlsx(file_path: Path, rows: list[dict[str, Any]]) -> None:
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "GAVI Config"
    ws.append(EXPORT_FIELDNAMES)

    # 编号类列设为文本格式（防止 Excel 自动转数字丢前导零）
    text_col_indices = set()
    for i, col in enumerate(EXPORT_FIELDNAMES, 1):
        if col in ("device_id", "channel_encode", "user_id", "password_viid", "device_ip", "server_ip"):
            text_col_indices.add(i)

    for row in rows:
        row_values = []
        for col in EXPORT_FIELDNAMES:
            val = row.get(col, "")
            if val is None:
                val = ""
            row_values.append(str(val))
        ws.append(row_values)

    # 对编号列设置文本格式
    from openpyxl.styles import numbers
    for col_idx in text_col_indices:
        for cell in ws.iter_rows(min_col=col_idx, max_col=col_idx, min_row=2):
            for c in cell:
                c.number_format = "@"

    wb.save(file_path)
    wb.close()
