"""大华 IPC 视图库（VSP_GAVI）纯 RPC2 API 客户端。

发现过程：视图库配置不走 Security RPAC 加密通道，而是 configManager 的
VSP_GAVI 配置节（明文 JSON）。本模块通过 configManager.getConfig /
configManager.setConfig 直接读写，无需浏览器。

字段映射（GaviConfigRow → VSP_GAVI.table.RegisterServer[i]）：
    enable          -> Enable
    server_ip       -> Address
    server_port     -> Port
    keepalive       -> KeepAlive
    max_timeout     -> MaxTimeoutTimes
    device_id       -> DeviceID
    user_id         -> UserId
    password_viid   -> Password（掩码 "******" 时跳过，不覆盖真实密码）
    reg_interval    -> RegInterval
    platform        -> Platform
    channel_encode  -> ChannelInfoList[channel].ChannelEncode
    collect_*       -> ChannelInfoList[channel].NotifyObjectList.*.Checked
"""

from __future__ import annotations

from typing import Any

from .csv_loader import GaviConfigRow
from .dahua_login import DahuaLoginError, DahuaSession

CONFIG_NAME = "VSP_GAVI"
MASK = "******"

# CSV collect 字段 -> NotifyObjectList 键
_COLLECT_MAP = {
    "collect_face": "Face",
    "collect_image": "Image",
    "collect_motor": "MotorVehicle",
    "collect_nonmotor": "NonMotorVehicle",
    "collect_person": "Person",
    "collect_file": "File",
    "collect_video": "VideoSlice",
}


class VspApiError(Exception):
    """VSP_GAVI API 调用错误。"""


class ApplyResult:
    def __init__(self, success: bool, message: str, register_status: str = "") -> None:
        self.success = success
        self.message = message
        self.register_status = register_status


def _get_server_entry(table: dict[str, Any], config_index: int) -> dict[str, Any] | None:
    """按 1-based config_index 取 RegisterServer 数组元素。"""
    servers = table.get("RegisterServer")
    if not isinstance(servers, list):
        return None
    idx = config_index - 1
    if 0 <= idx < len(servers):
        return servers[idx]
    return None


def read_config(session: DahuaSession) -> dict[str, Any]:
    """读取 VSP_GAVI 配置，返回 table。"""
    resp = session.rpc("configManager.getConfig", {"name": CONFIG_NAME, "options": []})
    table = resp.get("params", {}).get("table")
    if not isinstance(table, dict):
        raise VspApiError(f"读取 {CONFIG_NAME} 失败: 响应无 table: {str(resp)[:200]}")
    return table


def write_config(session: DahuaSession, table: dict[str, Any]) -> None:
    """写回 VSP_GAVI 配置。"""
    resp = session.rpc("configManager.setConfig", {
        "name": CONFIG_NAME,
        "table": table,
        "options": [],
    })
    if resp.get("result") is False:
        error = resp.get("error", {})
        raise VspApiError(f"写入 {CONFIG_NAME} 被拒绝: code={error.get('code')} msg={error.get('message')}")


def apply_row_to_table(row: GaviConfigRow, table: dict[str, Any]) -> dict[str, Any]:
    """把 CSV 行的字段覆盖到 table 的 RegisterServer[config_index-1] 上。

    只覆盖 CSV 中显式提供的字段，其余保持设备原值（比如第二个服务器条目、
    掩码密码等），最大限度降低副作用。
    """
    entry = _get_server_entry(table, row.config_index)
    if entry is None:
        raise VspApiError(
            f"设备配置中不存在 RegisterServer[{row.config_index - 1}]"
            f"（当前共 {len(table.get('RegisterServer') or [])} 个条目）"
        )

    if row.enable is not None:
        entry["Enable"] = bool(row.enable)
    if row.server_ip is not None:
        entry["Address"] = str(row.server_ip)
    if row.server_port is not None:
        entry["Port"] = int(row.server_port)
    if row.keepalive is not None:
        entry["KeepAlive"] = int(row.keepalive)
    if row.max_timeout is not None:
        entry["MaxTimeoutTimes"] = int(row.max_timeout)
    if row.device_id is not None:
        entry["DeviceID"] = str(row.device_id)
    if row.user_id is not None:
        entry["UserId"] = str(row.user_id)
    if row.password_viid and row.password_viid not in {MASK, ""}:
        entry["Password"] = str(row.password_viid)
    if row.reg_interval is not None:
        entry["RegInterval"] = int(row.reg_interval)
    if row.platform is not None:
        entry["Platform"] = int(row.platform)

    channel_idx = row.channel or 0
    channels = entry.get("ChannelInfoList")
    if row.channel_encode is not None or row.collect:
        if not isinstance(channels, list) or not (0 <= channel_idx < len(channels)):
            raise VspApiError(f"设备配置中不存在 ChannelInfoList[{channel_idx}]")
        chan = channels[channel_idx]
        if row.channel_encode is not None:
            chan["ChannelEncode"] = str(row.channel_encode)
        if row.collect:
            notify = chan.setdefault("NotifyObjectList", {})
            for csv_field, json_key in _COLLECT_MAP.items():
                value = getattr(row, csv_field, None)
                if value is not None:
                    notify.setdefault(json_key, {})["Checked"] = bool(value)

    return table


class VspApiClient:
    """每台设备一个实例：登录 → 读写 → 登出。线程安全（线程内独占使用）。"""

    def __init__(self, timeout: float | tuple[float, float] = (3.0, 10.0)) -> None:
        self.timeout = timeout

    def apply_config(self, row: GaviConfigRow, *, dry_run: bool = False) -> ApplyResult:
        if not row.has_gavi_fields():
            return ApplyResult(False, "未提供任何 GAVI 配置字段")

        session = DahuaSession(row.device_ip, row.username, row.password, timeout=self.timeout)
        try:
            session.login()
        except DahuaLoginError as exc:
            return ApplyResult(False, f"RPC2 登录失败: {exc}")
        except Exception as exc:  # 网络错误等
            return ApplyResult(False, f"连接设备失败: {exc}")

        try:
            table = read_config(session)

            # 应用字段
            try:
                apply_row_to_table(row, table)
            except VspApiError as exc:
                return ApplyResult(False, str(exc))

            if dry_run:
                return ApplyResult(True, f"dry-run: 将写入 {CONFIG_NAME} cfg{row.config_index}")

            # 写回
            try:
                write_config(session, table)
            except VspApiError as exc:
                return ApplyResult(False, str(exc))

            # 回读验证
            verify_table = read_config(session)
            verify_entry = _get_server_entry(verify_table, row.config_index)
            if row.device_id is not None and verify_entry and verify_entry.get("DeviceID") != str(row.device_id):
                return ApplyResult(
                    False,
                    f"回读校验失败: DeviceID 期望 {row.device_id} 实际 {verify_entry.get('DeviceID')}",
                )

            # 若配置了通道编号，也一并回读校验
            if row.channel_encode is not None and verify_entry:
                verify_channels = verify_entry.get("ChannelInfoList") or []
                c_idx = row.channel or 0
                if 0 <= c_idx < len(verify_channels):
                    actual_encode = verify_channels[c_idx].get("ChannelEncode")
                    if actual_encode != str(row.channel_encode):
                        return ApplyResult(
                            False,
                            f"回读校验失败: ChannelEncode 期望 {row.channel_encode} 实际 {actual_encode}",
                        )

            status = "-"
            if verify_entry is not None:
                status = "在线" if verify_entry.get("IsOnLine") else "离线(待同步)"
            return ApplyResult(True, f"已写入 {CONFIG_NAME} cfg{row.config_index} 并回读确认", status)
        except VspApiError as exc:
            return ApplyResult(False, str(exc))
        except Exception as exc:
            return ApplyResult(False, f"调用异常: {type(exc).__name__}: {exc}")
        finally:
            session.close()
