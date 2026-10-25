"""大华视图库批量配置工具 — 本地 Web GUI 后端。

启动后自动打开浏览器，提供图形化操作界面。
用法: python -m src gui [--port 8765]
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

from .csv_loader import EXPORT_FIELDNAMES, load_csv, row_to_export_dict, write_rows
from .dahua_login import DahuaLoginError, DahuaSession
from .gavi_client import GaviClient, GaviClientError
from .vsp_api import VspApiClient, VspApiError
from .batch_configure import _load_failed_items, _precheck_devices, _tcp_alive, write_result_file

_HTML_PATH = Path(__file__).parent / "gui_app.html"


class ImportState:
    """导入任务状态（线程安全）。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.finished = False
        self.total = 0
        self.done = 0
        self.ok = 0
        self.fail = 0
        self.logs: list[dict[str, str]] = []

    def reset(self) -> None:
        with self.lock:
            self.running = False
            self.finished = False
            self.total = 0
            self.done = 0
            self.ok = 0
            self.fail = 0
            self.logs = []

    def log(self, text: str, cls: str = "") -> None:
        with self.lock:
            self.logs.append({"text": text, "cls": cls})

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "running": self.running,
                "finished": self.finished,
                "total": self.total,
                "done": self.done,
                "ok": self.ok,
                "fail": self.fail,
                "logs": list(self.logs),
            }


_state = ImportState()


def _run_import(csv_file: str, engine: str, workers: int, retry: int,
                result_file: str, only_failed: bool, no_precheck: bool,
                dry_run: bool) -> None:
    """在后台线程执行导入。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import queue as queue_mod

    try:
        rows = load_csv(csv_file)
    except Exception as exc:
        _state.log(f"加载 CSV 失败: {exc}", "log-fail")
        _state.finished = True
        _state.running = False
        return

    if only_failed:
        failed_items = _load_failed_items(result_file)
        if not failed_items:
            _state.log(f"结果文件 {result_file} 中没有失败项", "log-warn")
            _state.finished = True
            _state.running = False
            return
        rows = [r for r in rows if (r.device_ip, r.config_index) in failed_items]
        _state.log(f"断点续传: {len(rows)} 行待重跑", "log-info")

    precheck_dead = []
    if not no_precheck and len(rows) > 1:
        _state.log(f"TCP 预检 {len(rows)} 台设备...", "log-info")
        rows, precheck_dead = _precheck_devices(rows)
        if precheck_dead:
            _state.log(f"预检发现 {len(precheck_dead)} 台不可达", "log-warn")
        if not rows:
            _state.log("所有设备均不可达", "log-fail")
            _state.finished = True
            _state.running = False
            return

    _state.total = len(rows)
    workers = max(1, min(workers, len(rows)))
    _state.log(f"开始导入: {len(rows)} 台, {workers} 并发, 引擎={engine}, dry_run={dry_run}", "log-info")

    results: list[tuple[Any, str, str, str]] = []
    lock = threading.Lock()

    def _process(row):
        client = VspApiClient() if engine == "rpc" else GaviClient(headless=True)
        for attempt in range(1, retry + 1):
            try:
                r = client.apply_config(row, dry_run=dry_run)
                status = r.register_status or "-"
                if r.success:
                    with lock:
                        _state.done += 1
                        _state.ok += 1
                        _state.log(f"  OK   {row.device_ip} cfg{row.config_index} | {r.message} | {status}", "log-ok")
                        results.append((row, "OK", r.message, status))
                    return
                if attempt == retry:
                    with lock:
                        _state.done += 1
                        _state.fail += 1
                        _state.log(f"  FAIL {row.device_ip} cfg{row.config_index} | {r.message}", "log-fail")
                        results.append((row, "FAIL", r.message, status))
                else:
                    with lock:
                        _state.log(f"  RETRY {row.device_ip} cfg{row.config_index} | {r.message} (第 {attempt}/{retry} 次)", "log-warn")
            except (GaviClientError, VspApiError) as exc:
                if attempt == retry:
                    with lock:
                        _state.done += 1
                        _state.fail += 1
                        _state.log(f"  FAIL {row.device_ip} cfg{row.config_index} | {exc}", "log-fail")
                        results.append((row, "FAIL", str(exc), "-"))
                else:
                    with lock:
                        _state.log(f"  RETRY {row.device_ip} cfg{row.config_index} | {exc} (第 {attempt}/{retry} 次)", "log-warn")
            if attempt < retry:
                time.sleep(1)

    for row in precheck_dead:
        with lock:
            _state.done += 1
            _state.fail += 1
            _state.log(f"  SKIP {row.device_ip} cfg{row.config_index} | TCP 预检不可达", "log-fail")
            results.append((row, "FAIL", "TCP 预检不可达", "-"))
    _state.total = len(rows) + len(precheck_dead)

    if engine == "rpc":
        task_q = queue_mod.Queue()
        for row in rows:
            task_q.put(row)

        def _rpc_worker():
            client = VspApiClient()
            while True:
                try:
                    row = task_q.get_nowait()
                except queue_mod.Empty:
                    break
                _process(row)

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(_rpc_worker) for _ in range(workers)]
            for f in as_completed(futures):
                f.result()
    else:
        task_q = queue_mod.Queue()
        for row in rows:
            task_q.put(row)

        def _pw_worker():
            client = GaviClient(headless=True)
            with client:
                while True:
                    try:
                        row = task_q.get_nowait()
                    except queue_mod.Empty:
                        break
                    _process(row)

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(_pw_worker) for _ in range(workers)]
            for f in as_completed(futures):
                f.result()

    if result_file:
        from datetime import datetime
        now = datetime.now().isoformat(timespec="seconds")
        result_rows = [
            {
                "timestamp": now,
                "device_ip": row.device_ip,
                "config_index": row.config_index,
                "status": status,
                "message": msg,
                "register_status": reg,
            }
            for row, status, msg, reg in results
        ]
        write_result_file(result_file, result_rows)
        _state.log(f"结果已写入 {result_file}", "log-info")

    _state.log(f"\n完成: 成功 {_state.ok}/{_state.total}, 失败 {_state.fail}", "log-ok" if _state.fail == 0 else "log-warn")
    _state.finished = True
    _state.running = False


class Handler(BaseHTTPRequestHandler):
    def _json(self, code: int, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            try:
                html = _HTML_PATH.read_text(encoding="utf-8")
            except FileNotFoundError:
                self._json(404, {"error": "gui_app.html not found"})
                return
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self):
        body = self._read_body()

        if self.path == "/api/validate":
            self._handle_validate(body)
        elif self.path == "/api/import":
            self._handle_import(body)
        elif self.path == "/api/status":
            self._json(200, _state.snapshot())
        elif self.path == "/api/export":
            self._handle_export(body)
        elif self.path == "/api/test-login":
            self._handle_test_login(body)
        elif self.path == "/api/probe":
            self._handle_probe(body)
        else:
            self._json(404, {"error": "not found"})

    def _handle_validate(self, body: dict):
        try:
            rows = load_csv(body.get("csv_file", ""))
            self._json(200, {"ok": True, "count": len(rows)})
        except Exception as exc:
            self._json(200, {"ok": False, "message": str(exc)})

    def _handle_import(self, body: dict):
        if _state.running:
            self._json(200, {"ok": False, "message": "已有导入任务在运行中"})
            return
        csv_file = body.get("csv_file", "").strip()
        if not csv_file:
            self._json(200, {"ok": False, "message": "请填写 CSV 文件路径"})
            return
        _state.reset()
        _state.running = True
        thread = threading.Thread(target=_run_import, kwargs=dict(
            csv_file=csv_file,
            engine=body.get("engine", "rpc"),
            workers=int(body.get("workers", 5)),
            retry=int(body.get("retry", 1)),
            result_file=body.get("result", "import_results.csv"),
            only_failed=body.get("only_failed", False),
            no_precheck=body.get("no_precheck", False),
            dry_run=body.get("dry_run", False),
        ), daemon=True)
        thread.start()
        self._json(200, {"ok": True, "message": f"导入已启动: {csv_file}"})

    def _handle_export(self, body: dict):
        device = body.get("device", "").strip()
        if not device:
            self._json(200, {"ok": False, "message": "请填写设备 IP"})
            return
        engine = body.get("engine", "rpc")
        if engine == "rpc":
            try:
                session = DahuaSession(device, body.get("username", "admin"), body.get("password", ""))
                session.login()
                from .vsp_api import read_config
                table = read_config(session)
                from .vsp_api import _get_server_entry
                entry = _get_server_entry(table, int(body.get("config_index", 1)))
                if not entry:
                    self._json(200, {"ok": False, "message": f"配置序号 {body.get('config_index')} 不存在"})
                    session.close()
                    return
                channels = entry.get("ChannelInfoList", [])
                chan = channels[int(body.get("channel", 0))] if channels else {}
                notify = chan.get("NotifyObjectList", {})
                row_dict = {
                    "device_ip": device, "username": body.get("username", "admin"),
                    "password": body.get("password", ""), "config_index": body.get("config_index", 1),
                    "enable": "TRUE" if entry.get("Enable") else "FALSE",
                    "server_ip": entry.get("Address", ""), "server_port": entry.get("Port", ""),
                    "keepalive": entry.get("KeepAlive", ""), "max_timeout": entry.get("MaxTimeoutTimes", ""),
                    "device_id": entry.get("DeviceID", ""), "user_id": entry.get("UserId", ""),
                    "password_viid": "", "reg_interval": entry.get("RegInterval", ""),
                    "platform": entry.get("Platform", ""), "channel": body.get("channel", 0),
                    "channel_encode": chan.get("ChannelEncode", ""),
                    "collect_face": "TRUE" if notify.get("Face", {}).get("Checked") else "FALSE",
                    "collect_image": "TRUE" if notify.get("Image", {}).get("Checked") else "FALSE",
                    "collect_motor": "TRUE" if notify.get("MotorVehicle", {}).get("Checked") else "FALSE",
                    "collect_nonmotor": "TRUE" if notify.get("NonMotorVehicle", {}).get("Checked") else "FALSE",
                    "collect_person": "TRUE" if notify.get("Person", {}).get("Checked") else "FALSE",
                    "collect_file": "TRUE" if notify.get("File", {}).get("Checked") else "FALSE",
                    "collect_video": "TRUE" if notify.get("VideoSlice", {}).get("Checked") else "FALSE",
                }
                output = body.get("output", "exported_config.csv")
                write_rows(output, [row_dict])
                session.close()
                self._json(200, {"ok": True, "message": f"已导出到 {output}", "row": row_dict})
            except Exception as exc:
                self._json(200, {"ok": False, "message": str(exc)})
        else:
            try:
                client = GaviClient(headless=False)
                row = client.export_config(
                    device, body.get("username", "admin"), body.get("password", ""),
                    config_index=int(body.get("config_index", 1)),
                    channel=int(body.get("channel", 0)),
                )
                output = body.get("output", "exported_config.csv")
                write_rows(output, [row_to_export_dict(row)])
                self._json(200, {"ok": True, "message": f"已导出到 {output}", "row": row_to_export_dict(row)})
            except Exception as exc:
                self._json(200, {"ok": False, "message": str(exc)})

    def _handle_test_login(self, body: dict):
        device = body.get("device", "").strip()
        if not device:
            self._json(200, {"ok": False, "message": "请填写设备 IP"})
            return
        try:
            session = DahuaSession(device, body.get("username", "admin"), body.get("password", ""))
            sid = session.login()
            session.close()
            self._json(200, {"ok": True, "message": f"登录成功 session={sid}"})
        except Exception as exc:
            self._json(200, {"ok": False, "message": f"登录失败: {exc}"})

    def _handle_probe(self, body: dict):
        device = body.get("device", "").strip()
        if not device:
            self._json(200, {"ok": False, "message": "请填写设备 IP"})
            return
        names = body.get("names") or ["VSP_GAVI", "VSP_GAYS", "VSP_GAYS_SERVER", "Network", "VIID", "GA1400"]
        try:
            session = DahuaSession(device, body.get("username", "admin"), body.get("password", ""))
            session.login()
            results = []
            for name in names:
                try:
                    resp = session.rpc("configManager.getConfig", {"name": name, "options": []})
                    ok = resp.get("result") is not False
                    detail = json.dumps(resp.get("params", {}).get("table", resp.get("error", "")), ensure_ascii=False)[:500] if ok else resp.get("error", {}).get("message", "")
                    results.append({"name": name, "ok": ok, "detail": detail})
                except Exception as exc:
                    results.append({"name": name, "ok": False, "detail": str(exc)[:200]})
            session.close()
            self._json(200, {"ok": True, "results": results})
        except Exception as exc:
            self._json(200, {"ok": False, "message": str(exc)})

    def log_message(self, *args):
        pass


def run_gui(port: int = 8765) -> int:
    # 保证相对路径基于用户启动目录
    import os
    os.chdir(os.path.dirname(os.path.abspath(__file__)) + os.sep + "..")
    print(f"Web GUI 启动中... http://127.0.0.1:{port}")
    threading.Timer(0.5, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    server = HTTPServer(("127.0.0.1", port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()
    return 0
