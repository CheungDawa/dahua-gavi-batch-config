"""大华 IPC 视图库（GAVI）批量配置 CLI。"""

from __future__ import annotations

import argparse
import csv
import json
import queue
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from .csv_loader import EXPORT_FIELDNAMES, GaviConfigRow, load_csv, row_to_export_dict, validate_row, write_rows
from .dahua_login import DahuaLoginError, DahuaSession
from .gavi_client import GaviClient, GaviClientError, sniff_config
from .rpac import RPACEncoder, all_encoder_variants
from .vsp_api import VspApiClient, VspApiError

RESULT_FIELDS = [
    "timestamp", "device_ip", "config_index", "status",
    "message", "register_status",
]

PROBE_CANDIDATES = [
    "VIID",
    "VIIDPlatform",
    "PlatformVIID",
    "VIIDServer",
    "GA1400",
    "GAVI",
    "Network",
    "NetWork",
    "Platform",
]


def _write_result_xlsx(path: Path, rows: list[dict]) -> None:
    """写结果到 XLSX。"""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Import Results"
    ws.append(RESULT_FIELDS)
    for row in rows:
        ws.append([row.get(col, "") for col in RESULT_FIELDS])
    wb.save(path)
    wb.close()


def write_result_file(path: str | Path, rows: list[dict]) -> None:
    """写出结果到 CSV 或 XLSX（根据后缀自动选择）。"""
    result_path = Path(path)
    if result_path.suffix.lower() == ".xlsx":
        _write_result_xlsx(result_path, rows)
    else:
        with open(result_path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=RESULT_FIELDS)
            writer.writeheader()
            writer.writerows(rows)


def _load_failed_items(path: str | Path) -> set[tuple[str, int]]:
    """从结果 CSV/XLSX 读取失败项 (device_ip, config_index)。"""
    failed: set[tuple[str, int]] = set()
    file_path = Path(path)
    try:
        if file_path.suffix.lower() == ".xlsx":
            import openpyxl
            wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
            ws = wb.active
            rows_iter = ws.iter_rows(values_only=True)
            header = next(rows_iter, None)
            if not header:
                wb.close()
                return failed
            col_map = {str(h).strip(): i for i, h in enumerate(header) if h}
            for row in rows_iter:
                status_idx = col_map.get("status")
                ip_idx = col_map.get("device_ip")
                ci_idx = col_map.get("config_index")
                if status_idx is not None and row[status_idx] == "FAIL":
                    ip = str(row[ip_idx]) if ip_idx is not None and row[ip_idx] is not None else ""
                    ci = int(row[ci_idx]) if ci_idx is not None and row[ci_idx] is not None else 1
                    failed.add((ip, ci))
            wb.close()
        else:
            with open(file_path, encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                for raw in reader:
                    if raw.get("status") == "FAIL":
                        failed.add((raw.get("device_ip", ""), int(raw.get("config_index", "1") or 1)))
    except (OSError, ValueError):
        pass
    return failed


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        rows = load_csv(args.csv_file)
    except (ValueError, FileNotFoundError) as exc:
        print(f"校验失败: {exc}", file=sys.stderr)
        return 1
    print(f"校验通过，共 {len(rows)} 行有效配置")
    return 0


def _tcp_alive(ip: str, port: int = 80, timeout: float = 2.0) -> bool:
    """TCP 预检：端口通则设备可达。"""
    try:
        sock = socket.create_connection((ip, port), timeout=timeout)
        sock.close()
        return True
    except OSError:
        return False


def _precheck_devices(rows: list, timeout: float = 2.0) -> tuple[list, list]:
    """并发 TCP 预检，返回 (可达, 不可达)。"""
    with ThreadPoolExecutor(max_workers=50) as executor:
        flags = list(executor.map(lambda r: _tcp_alive(r.device_ip, timeout=timeout), rows))
    alive = [r for r, ok in zip(rows, flags) if ok]
    dead = [r for r, ok in zip(rows, flags) if not ok]
    return alive, dead


def cmd_import(args: argparse.Namespace) -> int:
    try:
        rows = load_csv(args.csv_file)
    except (ValueError, FileNotFoundError) as exc:
        print(f"导入失败: {exc}", file=sys.stderr)
        return 1

    if args.device:
        rows = [row for row in rows if row.device_ip == args.device]
        if not rows:
            print(f"未找到设备 {args.device} 的配置行", file=sys.stderr)
            return 1

    if args.only_failed:
        failed_items = _load_failed_items(args.result)
        if not failed_items:
            print(f"结果文件 {args.result} 中没有失败项可重试", file=sys.stderr)
            return 1
        before = len(rows)
        rows = [r for r in rows if (r.device_ip, r.config_index) in failed_items]
        print(f"断点续传: 结果文件共 {len(failed_items)} 个失败项，匹配到 {len(rows)}/{before} 行")
        if not rows:
            return 1

    # TCP 预检：2 秒识别死设备，避免后续 30s×重试 级别的超时浪费
    precheck_dead: list = []
    if not args.no_precheck and len(rows) > 1:
        print(f"TCP 预检 {len(rows)} 台设备 (端口 80)...")
        rows, precheck_dead = _precheck_devices(rows)
        if precheck_dead:
            print(f"预检发现 {len(precheck_dead)} 台不可达，已标记失败: " + ", ".join(r.device_ip for r in precheck_dead[:10]) + ("..." if len(precheck_dead) > 10 else ""))
        if not rows:
            print("所有设备均不可达", file=sys.stderr)
            return 1

    workers = max(1, min(args.workers, len(rows)))
    engine_label = "纯 RPC2 API (VSP_GAVI)" if args.engine == "rpc" else "Playwright UI"
    print(f"共 {len(rows)} 台设备，{workers} 并发线程，每台最多尝试 {args.retry} 次，引擎: {engine_label}")

    results: list[tuple[GaviConfigRow, str, str, str]] = []
    lock = threading.Lock()

    def _process(row: GaviConfigRow, client) -> tuple[GaviConfigRow, str, str, str]:
        last: tuple[GaviConfigRow, str, str, str] | None = None
        for attempt in range(1, args.retry + 1):
            try:
                result = client.apply_config(row, dry_run=args.dry_run)
                status = result.register_status or "-"
                if result.success:
                    with lock:
                        print(f"  OK   {row.device_ip} (cfg {row.config_index}) | {result.message} | 注册: {status}")
                    return (row, "OK", result.message, status)
                with lock:
                    print(f"  FAIL {row.device_ip} (cfg {row.config_index}) | {result.message}", file=sys.stderr)
                last = (row, "FAIL", result.message, status)
            except (GaviClientError, VspApiError) as exc:
                with lock:
                    print(f"  FAIL {row.device_ip} (cfg {row.config_index}) | {exc}", file=sys.stderr)
                last = (row, "FAIL", str(exc), "-")
            if attempt < args.retry:
                with lock:
                    print(f"    重试 {row.device_ip} (cfg {row.config_index}) 第 {attempt + 1}/{args.retry} 次...")
                time.sleep(1)
        return last  # type: ignore[return-value]

    if args.engine == "rpc":
        # 纯 API 引擎：无状态，每台设备独立会话，worker 仅做队列分发
        task_queue: queue.Queue = queue.Queue()
        for row in rows:
            task_queue.put(row)

        def _rpc_worker() -> list[tuple[GaviConfigRow, str, str, str]]:
            local: list[tuple[GaviConfigRow, str, str, str]] = []
            client = VspApiClient()
            while True:
                try:
                    row = task_queue.get_nowait()
                except queue.Empty:
                    break
                local.append(_process(row, client))
            return local

        if workers == 1:
            results.extend(_rpc_worker())
        else:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(_rpc_worker) for _ in range(workers)]
                for future in as_completed(futures):
                    results.extend(future.result())

    elif workers == 1:
        client = GaviClient(headless=not args.show_browser)
        for row in rows:
            results.append(_process(row, client))
    else:
        # 动态任务队列：worker 从共享队列拉任务，先完成的多干，避免慢设备拖住整片
        pw_queue: queue.Queue = queue.Queue()
        for row in rows:
            pw_queue.put(row)

        def _worker() -> list[tuple[GaviConfigRow, str, str, str]]:
            local: list[tuple[GaviConfigRow, str, str, str]] = []
            client = GaviClient(headless=not args.show_browser)
            with client:
                while True:
                    try:
                        row = pw_queue.get_nowait()
                    except queue.Empty:
                        break
                    local.append(_process(row, client))
            return local

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(_worker) for _ in range(workers)]
            for future in as_completed(futures):
                results.extend(future.result())

    # 预检不可达的设备直接记为失败
    for row in precheck_dead:
        results.append((row, "FAIL", "TCP 预检不可达(端口80)", "-"))
        print(f"  FAIL {row.device_ip} (cfg {row.config_index}) | TCP 预检不可达(端口80)", file=sys.stderr)

    success_count = sum(1 for _, s, _, _ in results if s == "OK")

    if args.result:
        result_path = Path(args.result)
        now = datetime.now().isoformat(timespec="seconds")
        result_rows = []
        for row, status, message, reg in results:
            result_rows.append({
                "timestamp": now,
                "device_ip": row.device_ip,
                "config_index": row.config_index,
                "status": status,
                "message": message,
                "register_status": reg,
            })
        write_result_file(args.result, result_rows)
        print(f"结果已写入 {args.result}")

    print(f"\n=== 汇总 ({len(rows)} 台, {workers} 并发, 成功 {success_count}/{len(rows)}) ===")
    for row, status, message, _ in results:
        print(f"{status}\t{row.device_ip}\t{row.config_index}\t{message}")

    return 0 if success_count == len(rows) else 1


def cmd_export(args: argparse.Namespace) -> int:
    client = GaviClient(headless=not args.show_browser)
    try:
        row = client.export_config(
            args.device,
            args.username,
            args.password,
            config_index=args.config_index,
            channel=args.channel,
        )
    except GaviClientError as exc:
        print(f"导出失败: {exc}", file=sys.stderr)
        return 1

    output = Path(args.output)
    if output.suffix.lower() == ".xlsx":
        write_rows(output, [row_to_export_dict(row)])
    else:
        write_header = not output.exists() or args.overwrite
        mode = "w" if args.overwrite or not output.exists() else "a"
        with output.open(mode, encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=EXPORT_FIELDNAMES, extrasaction="ignore")
            if write_header:
                writer.writeheader()
            writer.writerow(row_to_export_dict(row))

    print(f"已导出到 {output}")
    return 0


def cmd_test_login(args: argparse.Namespace) -> int:
    session = DahuaSession(args.device, args.username, args.password)
    try:
        sid = session.login()
        session.keep_alive()
        print(f"登录成功，session={sid}")
        return 0
    except (DahuaLoginError, Exception) as exc:
        print(f"登录失败: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()


def cmd_probe(args: argparse.Namespace) -> int:
    """探测设备 RPC2 接口：确认登录后能否读取视图库相关配置节。"""
    session = DahuaSession(args.device, args.username, args.password)
    try:
        sid = session.login()
        print(f"RPC2 登录成功 session={sid}")
    except Exception as exc:
        print(f"RPC2 登录失败: {exc}", file=sys.stderr)
        return 1

    try:
        try:
            info = session.rpc("magicBox.getDeviceType")
            print(f"设备型号: {json.dumps(info, ensure_ascii=False)}")
        except Exception as exc:
            print(f"getDeviceType 失败: {exc}")

        names = args.name or PROBE_CANDIDATES
        for name in names:
            try:
                data = session.rpc("configManager.getConfig", {"name": name})
                ok = data.get("result") is not False
                print(f"\n[{name}] -> {'OK' if ok else 'REJECTED'}")
                print(json.dumps(data, ensure_ascii=False, indent=2)[:4000])
            except Exception as exc:
                print(f"\n[{name}] -> ERROR: {exc}")
        return 0
    finally:
        session.close()


def cmd_sniff(args: argparse.Namespace) -> int:
    """打开浏览器抓包：捕获视图库页面所有 API 请求（读取+写入）。"""
    print(f"正在打开浏览器抓包 {args.device} ...")
    try:
        captured = sniff_config(
            args.device,
            args.username,
            args.password,
            headless=False,
            capture_apply=not args.read_only,
        )
    except Exception as exc:
        print(f"抓包失败: {exc}", file=sys.stderr)
        return 1

    print(f"\n共捕获 {len(captured)} 个 API 请求\n")
    print("=" * 80)

    for i, req in enumerate(captured, 1):
        print(f"\n[{i}] {req.method} {req.url}")
        print(f"    Status: {req.status}  Content-Type: {req.content_type or '-'}")
        if req.post_data:
            # 截断超长 post data
            display = req.post_data[:2000]
            print(f"    >>> Request Body:\n{display}")
        if req.response_body:
            display = req.response_body[:3000]
            print(f"    <<< Response Body:\n{display}")

    # 保存到 JSON 文件方便分析
    out_file = args.output or "sniff_capture.json"
    with open(out_file, "w", encoding="utf-8") as handle:
        json.dump(
            [
                {
                    "method": r.method,
                    "url": r.url,
                    "post_data": r.post_data,
                    "status": r.status,
                    "response_body": r.response_body,
                    "content_type": r.content_type,
                }
                for r in captured
            ],
            handle,
            ensure_ascii=False,
            indent=2,
        )
    print(f"\n完整抓包数据已保存到 {out_file}")
    return 0


# ── RPAC 纯 API 读写 ────────────────────────────────────

VIID_CONFIG_NAMES = [
    "VIID", "VIIDPlatform", "PlatformVIID", "GA1400",
    "VSP_GAYS_SERVER", "VSP_GAYS", "AOL",
    "VSPGB", "VSP_GB", "GB28181",
]


def cmd_rpc_config(args: argparse.Namespace) -> int:
    """通过 RPAC-256 加密通道直接读写 VIID 配置（无需浏览器）。"""
    session = DahuaSession(args.device, args.username, args.password)
    try:
        sid = session.login()
        print(f"RPC2 登录成功 session={sid}")
    except Exception as exc:
        print(f"RPC2 登录失败: {exc}", file=sys.stderr)
        return 1

    # 获取 RSA 公钥
    try:
        enc_info = session.rpc("Security.getEncryptInfo")
        pub = enc_info["params"]["pub"]
        print(f"获取 RSA 公钥: {pub[:60]}...")
    except Exception as exc:
        print(f"获取加密信息失败: {exc}", file=sys.stderr)
        return 1

    if args.brute:
        return _rpc_brute(session, pub, args)

    encoder = RPACEncoder.from_pub_string(
        pub,
        rsa_encrypt_iv=args.rsa_iv,
        aes_mode=args.aes_mode,
        aes_padding=args.aes_pad,
        rsa_padding=args.rsa_pad,
    )
    print(f"编码器: {encoder.describe()}")

    if args.action == "read":
        return _rpc_read(session, encoder, args)
    else:
        return _rpc_write(session, encoder, args)


def _rpc_brute(session: DahuaSession, pub: str, args: argparse.Namespace) -> int:
    """暴力测试所有加密组合，找出能用的那个。"""
    from .rpac import all_encoder_variants
    variants = all_encoder_variants(pub)
    test_name = (args.name or ["VIID"])[0]
    print(f"\n暴力测试 {len(variants)} 种加密组合，配置名: {test_name}")
    print("=" * 80)

    for i, encoder in enumerate(variants, 1):
        desc = encoder.describe()
        request_data = {"name": test_name, "options": []}
        try:
            salt, content, aes_key, iv = encoder.encode_and_get_decryptor(request_data)
        except Exception as exc:
            print(f"[{i:02d}/{len(variants)}] {desc} -> 加密失败: {exc}")
            continue

        try:
            raw = session.raw_rpc("Security.getConfig", {
                "cipher": "RPAC-256",
                "salt": salt,
                "content": content,
            })
        except Exception as exc:
            print(f"[{i:02d}/{len(variants)}] {desc} -> 请求异常: {exc}")
            continue

        if raw.get("status_code") != 200:
            print(f"[{i:02d}/{len(variants)}] {desc} -> HTTP {raw['status_code']}")
            continue

        try:
            resp = json.loads(raw["body"])
        except Exception:
            print(f"[{i:02d}/{len(variants)}] {desc} -> 响应非JSON")
            continue

        result = resp.get("result")
        if result is False:
            error = resp.get("error", {})
            code = error.get("code", "?")
            msg = error.get("message", "")
            print(f"[{i:02d}/{len(variants)}] {desc} -> REJECTED code={code} msg={msg}")
            continue

        # result=true 或 result 不是 false
        resp_content = resp.get("params", {}).get("content", "")
        if not resp_content:
            print(f"[{i:02d}/{len(variants)}] {desc} -> OK但无content: {json.dumps(resp, ensure_ascii=False)[:200]}")
            continue

        # 尝试解密响应
        try:
            decrypted = encoder.decrypt_response(resp_content, aes_key, iv)
            print(f"[{i:02d}/{len(variants)}] {desc} -> SUCCESS!")
            print(f"  解密内容: {json.dumps(decrypted, ensure_ascii=False, indent=2)[:2000]}")

            # 保存
            out_file = f"rpc_config_{test_name}.json"
            with open(out_file, "w", encoding="utf-8") as handle:
                json.dump(decrypted, handle, ensure_ascii=False, indent=2)
            print(f"  已保存到 {out_file}")
            print(f"\n>>> 找到可用编码器: {desc}")
            print(f">>> 参数: --rsa-iv {encoder.rsa_encrypt_iv} --aes-mode {encoder.aes_mode} --aes-pad {encoder.aes_padding} --rsa-pad {encoder.rsa_padding}")
            return 0
        except Exception as exc:
            print(f"[{i:02d}/{len(variants)}] {desc} -> result=OK但解密失败: {exc}")

    print(f"\n所有 {len(variants)} 种组合均未成功")
    return 1


def _rpc_read(session: DahuaSession, encoder, args: argparse.Namespace) -> int:
    """尝试用不同配置名读取 VIID 配置。"""
    names = args.name or VIID_CONFIG_NAMES
    found = False

    for name in names:
        print(f"\n--- 尝试读取 [{name}] ---")
        request_data = {"name": name, "options": []}
        try:
            salt, content, aes_key, iv = encoder.encode_and_get_decryptor(request_data)
        except Exception as exc:
            print(f"  加密失败: {exc}")
            continue

        try:
            raw = session.raw_rpc("Security.getConfig", {
                "cipher": "RPAC-256",
                "salt": salt,
                "content": content,
            })
        except Exception as exc:
            print(f"  请求异常: {type(exc).__name__}: {exc}")
            continue

        if raw.get("status_code") != 200:
            print(f"  HTTP {raw['status_code']}: {raw.get('body', '')[:500]}")
            continue

        try:
            resp = json.loads(raw["body"])
        except Exception:
            print(f"  响应非 JSON: {raw['body'][:500]}")
            continue

        print(f"  响应: {json.dumps(resp, ensure_ascii=False)[:600]}")

        if resp.get("result") is False:
            error = resp.get("error", {})
            print(f"  设备拒绝: code={error.get('code')} msg={error.get('message', '')}")
            continue

        resp_content = resp.get("params", {}).get("content", "")
        if not resp_content:
            print(f"  响应无 content 字段: {json.dumps(resp, ensure_ascii=False)[:200]}")
            continue

        print(f"  响应 content 长度: {len(resp_content)} chars")
        try:
            decrypted = encoder.decrypt_response(resp_content, aes_key, iv)
            print(f"  解密成功!")
            print(json.dumps(decrypted, ensure_ascii=False, indent=2)[:5000])
            found = True

            # 保存到文件
            out_file = f"rpc_config_{name}.json"
            with open(out_file, "w", encoding="utf-8") as handle:
                json.dump(decrypted, handle, ensure_ascii=False, indent=2)
            print(f"  已保存到 {out_file}")
        except Exception as exc:
            print(f"  解密失败: {exc}")
            # 打印前几十字节帮助调试
            try:
                raw = base64.b64decode(resp_content)
                print(f"  原始密文前 64 字节: {raw[:64].hex()}")
                print(f"  密文总长: {len(raw)} bytes")
            except Exception:
                pass

    if not found:
        print("\n所有配置名均未成功，可能需要更换编码器 (--encoder v2/ecb)")
        return 1
    return 0


def _rpc_write(session: DahuaSession, encoder, args: argparse.Namespace) -> int:
    """写入 VIID 配置。"""
    if not args.config_file:
        print("写入模式需要 --config-file 指定配置 JSON 文件", file=sys.stderr)
        return 1

    with open(args.config_file, encoding="utf-8") as handle:
        config_data = json.load(handle)

    name = args.name or "VIID"
    request_data = {"name": name, "table": config_data, "options": []}

    salt, content, aes_key, iv = encoder.encode_and_get_decryptor(request_data)

    try:
        resp = session.rpc("Security.setConfig", {
            "cipher": "RPAC-256",
            "salt": salt,
            "content": content,
        })
    except Exception as exc:
        print(f"写入请求失败: {exc}", file=sys.stderr)
        return 1

    if resp.get("result") is False:
        error = resp.get("error", {})
        print(f"设备拒绝: {error.get('message', resp)}", file=sys.stderr)
        return 1

    resp_content = resp.get("params", {}).get("content", "")
    if resp_content:
        try:
            decrypted = encoder.decrypt_response(resp_content, aes_key, iv)
            print(f"写入成功: {json.dumps(decrypted, ensure_ascii=False)[:500]}")
        except Exception:
            print(f"写入已提交（响应解密失败，但 result=true）")
    else:
        print(f"写入已提交 (result={resp.get('result')})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="大华 IPC 视图库（GAVI）批量配置工具")
    sub = parser.add_subparsers(dest="command", required=True)

    validate_parser = sub.add_parser("validate", help="校验 CSV 模板")
    validate_parser.add_argument("csv_file", help="CSV 文件路径")
    validate_parser.set_defaults(func=cmd_validate)

    import_parser = sub.add_parser("import", help="从 CSV 批量导入配置")
    import_parser.add_argument("csv_file", help="CSV 文件路径")
    import_parser.add_argument("--device", help="仅处理指定 IP")
    import_parser.add_argument("--dry-run", action="store_true", help="仅填写表单不提交")
    import_parser.add_argument("--workers", "-w", type=int, default=10, help="并发线程数（默认 10，RPC 引擎建议 30-50）")
    import_parser.add_argument("--engine", choices=["rpc", "playwright"], default="rpc", help="配置引擎：rpc=纯 RPC2 API 直读直写 VSP_GAVI（快 10 倍+，推荐默认），playwright=浏览器 UI 自动化（兼容性兜底）")
    import_parser.add_argument("--retry", type=int, default=1, help="每台设备最多尝试次数（默认 1，不重试）")
    import_parser.add_argument("--result", default="import_results.csv", help="结果输出 CSV（默认 import_results.csv）")
    import_parser.add_argument("--only-failed", action="store_true", help="仅重跑上次结果文件中的失败项")
    import_parser.add_argument("--no-precheck", action="store_true", help="跳过 TCP 预检")
    import_parser.add_argument("--show-browser", action="store_true", help="显示浏览器窗口")
    import_parser.set_defaults(func=cmd_import)

    export_parser = sub.add_parser("export", help="从设备导出当前配置到 CSV")
    export_parser.add_argument("--device", required=True, help="设备 IP")
    export_parser.add_argument("--username", required=True, help="登录用户名")
    export_parser.add_argument("--password", required=True, help="登录密码")
    export_parser.add_argument("-o", "--output", default="exported_config.csv", help="输出 CSV")
    export_parser.add_argument("--config-index", type=int, default=1, help="视图库配置索引(1-based)")
    export_parser.add_argument("--channel", type=int, default=0, help="通道索引(0-based)")
    export_parser.add_argument("--overwrite", action="store_true", help="覆盖已有输出文件")
    export_parser.add_argument("--show-browser", action="store_true", help="显示浏览器窗口")
    export_parser.set_defaults(func=cmd_export)

    login_parser = sub.add_parser("test-login", help="测试 RPC2 登录")
    login_parser.add_argument("--device", required=True, help="设备 IP")
    login_parser.add_argument("--username", required=True, help="用户名")
    login_parser.add_argument("--password", required=True, help="密码")
    login_parser.set_defaults(func=cmd_test_login)

    probe_parser = sub.add_parser("probe", help="探测 RPC2 配置接口（读取视图配置节）")
    probe_parser.add_argument("--device", required=True, help="设备 IP")
    probe_parser.add_argument("--username", required=True, help="用户名")
    probe_parser.add_argument("--password", required=True, help="密码")
    probe_parser.add_argument("--name", action="append", help="自定义配置节名（可重复，默认探测常用名）")
    probe_parser.set_defaults(func=cmd_probe)

    sniff_parser = sub.add_parser("sniff", help="浏览器抓包：捕获视图库页面 API 请求")
    sniff_parser.add_argument("--device", required=True, help="设备 IP")
    sniff_parser.add_argument("--username", required=True, help="用户名")
    sniff_parser.add_argument("--password", required=True, help="密码")
    sniff_parser.add_argument("--read-only", action="store_true", help="仅捕获读取接口，不点击应用")
    sniff_parser.add_argument("-o", "--output", default="sniff_capture.json", help="输出 JSON 文件（默认 sniff_capture.json）")
    sniff_parser.set_defaults(func=cmd_sniff)

    rpc_parser = sub.add_parser("rpc-config", help="通过 RPAC-256 加密通道直接读写 VIID 配置")
    rpc_parser.add_argument("--device", required=True, help="设备 IP")
    rpc_parser.add_argument("--username", required=True, help="用户名")
    rpc_parser.add_argument("--password", required=True, help="密码")
    rpc_parser.add_argument("--action", choices=["read", "write"], default="read", help="read=读取配置, write=写入配置")
    rpc_parser.add_argument("--name", action="append", help="配置节名（可重复，默认探测常用名）")
    rpc_parser.add_argument("--config-file", help="写入模式的配置 JSON 文件")
    rpc_parser.add_argument("--brute", action="store_true", help="暴力测试所有加密组合（16种）")
    rpc_parser.add_argument("--rsa-iv", type=lambda x: x.lower() in ("true", "1", "yes"), default=True, help="RSA是否加密IV (true/false)")
    rpc_parser.add_argument("--aes-mode", choices=["cbc", "ecb"], default="cbc", help="AES模式")
    rpc_parser.add_argument("--aes-pad", choices=["zero", "pkcs7"], default="pkcs7", help="AES填充")
    rpc_parser.add_argument("--rsa-pad", choices=["pkcs1v15", "oaep"], default="pkcs1v15", help="RSA填充")
    rpc_parser.set_defaults(func=cmd_rpc_config)

    gui_parser = sub.add_parser("gui", help="启动图形化 Web 界面")
    gui_parser.add_argument("--port", type=int, default=8765, help="本地端口（默认 8765）")
    gui_parser.set_defaults(func=lambda a: _cmd_gui(a))

    return parser


def _cmd_gui(args: argparse.Namespace) -> int:
    from .gui_qt import run_gui
    return run_gui()


def main(argv: list[str] | None = None) -> int:
    # 无参数时默认启动 GUI（双击 exe 场景）
    if argv is None and len(sys.argv) <= 1:
        from .gui_qt import run_gui
        return run_gui()

    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())