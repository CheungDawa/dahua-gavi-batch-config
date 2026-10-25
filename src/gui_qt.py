"""大华视图库批量配置工具 — 桌面 GUI（PySide6）。"""

from __future__ import annotations

import csv
import json
import os
import queue
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtGui import QFont, QTextCursor, QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QTabWidget, QVBoxLayout, QHBoxLayout,
    QFormLayout, QLineEdit, QSpinBox, QCheckBox, QComboBox, QPushButton,
    QFileDialog, QLabel, QProgressBar, QTextEdit, QGroupBox, QMessageBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QSplitter,
)

# 确保能 import 项目模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from .csv_loader import EXPORT_FIELDNAMES, load_csv, row_to_export_dict, write_rows
from .dahua_login import DahuaLoginError, DahuaSession
from .gavi_client import GaviClient, GaviClientError
from .vsp_api import VspApiClient, VspApiError, read_config, _get_server_entry
from .batch_configure import _load_failed_items, _precheck_devices, _tcp_alive, write_result_file


# ── 颜色 ────────────────────────────────────────────────
OK_COLOR = QColor("#2d7d2d")
FAIL_COLOR = QColor("#c0392b")
INFO_COLOR = QColor("#1a5276")
WARN_COLOR = QColor("#b7950b")


# ── 导入工作线程 ────────────────────────────────────────

class ImportWorker(QThread):
    progress = Signal(int, int, str, str)   # done, total, message, cls
    finished_signal = Signal(int, int, int)  # ok, fail, total

    def __init__(self, csv_file: str, engine: str, workers: int, retry: int,
                 result_file: str, only_failed: bool, no_precheck: bool,
                 dry_run: bool):
        super().__init__()
        self.csv_file = csv_file
        self.engine = engine
        self.workers = workers
        self.retry = retry
        self.result_file = result_file
        self.only_failed = only_failed
        self.no_precheck = no_precheck
        self.dry_run = dry_run
        self._stop = False
        self.task_q: queue.Queue | None = None

    def stop(self):
        self._stop = True
        if self.task_q is not None:
            try:
                while not self.task_q.empty():
                    self.task_q.get_nowait()
            except Exception:
                pass

    def log(self, msg: str, cls: str = "info"):
        self.progress.emit(-1, -1, msg, cls)

    def run(self):
        try:
            rows = load_csv(self.csv_file)
        except Exception as exc:
            self.log(f"加载配置失败: {exc}", "fail")
            self.finished_signal.emit(0, 1, 1)
            return

        if self.only_failed:
            failed_items = _load_failed_items(self.result_file)
            if not failed_items:
                self.log("结果文件中没有失败项可重试", "warn")
                self.finished_signal.emit(0, 0, 0)
                return
            rows = [r for r in rows if (r.device_ip, r.config_index) in failed_items]
            self.log(f"断点续传: {len(rows)} 行待重跑", "info")

        # TCP 预检
        precheck_dead = []
        if not self.no_precheck and len(rows) > 1:
            self.log(f"TCP 预检 {len(rows)} 台设备...", "info")
            rows, precheck_dead = _precheck_devices(rows)
            if precheck_dead:
                self.log(f"预检发现 {len(precheck_dead)} 台不可达", "warn")
            if not rows:
                self.log("所有设备均不可达", "fail")
                self.finished_signal.emit(0, len(precheck_dead), len(precheck_dead))
                return

        total = len(rows) + len(precheck_dead)
        done = [0]
        ok_count = [0]
        fail_count = [0]
        results: list[tuple] = []
        lock = threading.Lock()

        workers = max(1, min(self.workers, len(rows)))
        self.log(f"开始导入: {len(rows)} 台, {workers} 并发, 引擎={self.engine}", "info")

        def _process(row):
            if self._stop:
                return
            client = VspApiClient() if self.engine == "rpc" else GaviClient(headless=True)
            for attempt in range(1, self.retry + 1):
                if self._stop:
                    return
                try:
                    r = client.apply_config(row, dry_run=self.dry_run)
                    status = r.register_status or "-"
                    if r.success:
                        with lock:
                            done[0] += 1
                            ok_count[0] += 1
                            results.append((row, "OK", r.message, status))
                            self.progress.emit(done[0], total,
                                f"  OK   {row.device_ip} cfg{row.config_index} | {r.message} | {status}", "ok")
                        return
                    if attempt == self.retry:
                        with lock:
                            done[0] += 1
                            fail_count[0] += 1
                            results.append((row, "FAIL", r.message, status))
                            self.progress.emit(done[0], total,
                                f"  FAIL {row.device_ip} cfg{row.config_index} | {r.message}", "fail")
                    else:
                        self.log(f"  重试 {row.device_ip} cfg{row.config_index} 第 {attempt + 1}/{self.retry} 次 | {r.message}", "warn")
                except (GaviClientError, VspApiError) as exc:
                    if attempt == self.retry:
                        with lock:
                            done[0] += 1
                            fail_count[0] += 1
                            results.append((row, "FAIL", str(exc), "-"))
                            self.progress.emit(done[0], total,
                                f"  FAIL {row.device_ip} cfg{row.config_index} | {exc}", "fail")
                    else:
                        self.log(f"  重试 {row.device_ip} cfg{row.config_index} 第 {attempt + 1}/{self.retry} 次 | {exc}", "warn")
                if attempt < self.retry and not self._stop:
                    time.sleep(1)

        # 预检死设备直接记账
        for row in precheck_dead:
            done[0] += 1
            fail_count[0] += 1
            results.append((row, "FAIL", "TCP 预检不可达", "-"))
            self.progress.emit(done[0], total,
                f"  SKIP {row.device_ip} cfg{row.config_index} | TCP 预检不可达", "fail")

        if not self._stop and rows:
            self.task_q = queue.Queue()
            for row in rows:
                self.task_q.put(row)

            if self.engine == "rpc":
                def _rpc_worker():
                    client = VspApiClient()
                    while not self._stop:
                        try:
                            row = self.task_q.get_nowait()
                        except queue.Empty:
                            break
                        _process(row)
            else:
                def _rpc_worker():
                    client = GaviClient(headless=True)
                    with client:
                        while not self._stop:
                            try:
                                row = self.task_q.get_nowait()
                            except queue.Empty:
                                break
                            _process(row)

            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(_rpc_worker) for _ in range(workers)]
                for f in as_completed(futures):
                    f.result()

        # 写结果
        if self.result_file and results:
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
            write_result_file(self.result_file, result_rows)
            self.log(f"结果已写入 {self.result_file}", "info")

        self.log(f"\n完成: 成功 {ok_count[0]}/{total}, 失败 {fail_count[0]}", "ok" if fail_count[0] == 0 else "warn")
        self.finished_signal.emit(ok_count[0], fail_count[0], total)


# ── 通用：导出/诊断线程 ─────────────────────────────────

class SimpleWorker(QThread):
    finished_signal = Signal(bool, str, object)  # ok, message, extra

    def __init__(self, func, *args):
        super().__init__()
        self._func = func
        self._args = args

    def run(self):
        try:
            result = self._func(*self._args)
            self.finished_signal.emit(True, result[0] if isinstance(result, tuple) else str(result), result)
        except Exception as exc:
            self.finished_signal.emit(False, str(exc), None)


# ── 导入页签 ────────────────────────────────────────────

class ImportTab(QWidget):
    def __init__(self):
        super().__init__()
        self.worker = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # 参数组
        grp = QGroupBox("配置参数")
        form = QFormLayout()

        # CSV 文件选择
        csv_row = QHBoxLayout()
        self.csv_input = QLineEdit("config_template.csv")
        btn_browse = QPushButton("浏览...")
        btn_browse.clicked.connect(self._browse_csv)
        csv_row.addWidget(self.csv_input)
        csv_row.addWidget(btn_browse)
        form.addRow("CSV 文件:", self._wrap(csv_row))

        # 引擎选择
        self.engine_combo = QComboBox()
        self.engine_combo.addItem("RPC API（推荐，~1秒/台）", "rpc")
        self.engine_combo.addItem("Playwright UI（兜底，~8秒/台）", "playwright")
        form.addRow("配置引擎:", self.engine_combo)

        # 并发/重试
        params_row = QHBoxLayout()
        self.workers_spin = QSpinBox()
        self.workers_spin.setRange(1, 100)
        self.workers_spin.setValue(30)
        self.retry_spin = QSpinBox()
        self.retry_spin.setRange(1, 5)
        self.retry_spin.setValue(1)
        params_row.addWidget(QLabel("并发数:"))
        params_row.addWidget(self.workers_spin)
        params_row.addSpacing(20)
        params_row.addWidget(QLabel("重试次数:"))
        params_row.addWidget(self.retry_spin)
        params_row.addStretch()
        form.addRow("批量参数:", self._wrap(params_row))

        # 结果文件
        self.result_input = QLineEdit("import_results.csv")
        form.addRow("结果文件:", self.result_input)

        # 选项
        opts_row = QHBoxLayout()
        self.chk_only_failed = QCheckBox("仅重跑失败项")
        self.chk_no_precheck = QCheckBox("跳过 TCP 预检")
        self.chk_dry_run = QCheckBox("预演（不写入）")
        opts_row.addWidget(self.chk_only_failed)
        opts_row.addWidget(self.chk_no_precheck)
        opts_row.addWidget(self.chk_dry_run)
        opts_row.addStretch()
        form.addRow("选项:", self._wrap(opts_row))

        grp.setLayout(form)
        layout.addWidget(grp)

        # 按钮
        btn_row = QHBoxLayout()
        self.btn_validate = QPushButton("校验 CSV")
        self.btn_validate.clicked.connect(self._validate)
        self.btn_import = QPushButton("开始导入")
        self.btn_import.setStyleSheet("QPushButton { background-color: #1890ff; color: white; font-weight: bold; padding: 8px 24px; }")
        self.btn_import.clicked.connect(self._import)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setStyleSheet("QPushButton { background-color: #ff4d4f; color: white; }")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop)
        btn_row.addWidget(self.btn_validate)
        btn_row.addWidget(self.btn_import)
        btn_row.addWidget(self.btn_stop)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        # 进度条
        self.progress = QProgressBar()
        self.progress.setFormat("就绪")
        layout.addWidget(self.progress)

        # 汇总
        self.summary_label = QLabel("")
        self.summary_label.setStyleSheet("font-size: 14px; font-weight: bold;")
        layout.addWidget(self.summary_label)

        # 日志
        layout.addWidget(QLabel("运行日志:"))
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(QFont("Consolas", 9))
        self.log_text.setStyleSheet("QTextEdit { background-color: #1e1e1e; color: #d4d4d4; }")
        layout.addWidget(self.log_text, stretch=1)

    @staticmethod
    def _wrap(layout):
        w = QWidget()
        w.setLayout(layout)
        return w

    def _browse_csv(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择 CSV/XLSX 文件", "", "配置模板 (*.csv *.xlsx);;CSV 文件 (*.csv);;Excel 文件 (*.xlsx)")
        if path:
            self.csv_input.setText(path)

    def _append_log(self, text: str, cls: str = "info"):
        color_map = {"ok": OK_COLOR, "fail": FAIL_COLOR, "info": INFO_COLOR, "warn": WARN_COLOR}
        color = color_map.get(cls, QColor("#d4d4d4"))
        cursor = self.log_text.textCursor()
        cursor.movePosition(QTextCursor.End)
        fmt = cursor.charFormat()
        fmt.setForeground(color)
        cursor.setCharFormat(fmt)
        cursor.insertText(text + "\n")
        self.log_text.setTextCursor(cursor)
        self.log_text.ensureCursorVisible()

    def _validate(self):
        csv_file = self.csv_input.text().strip()
        if not csv_file:
            QMessageBox.warning(self, "提示", "请填写 CSV 文件路径")
            return
        try:
            rows = load_csv(csv_file)
            QMessageBox.information(self, "校验通过", f"共 {len(rows)} 行有效配置")
        except Exception as exc:
            QMessageBox.critical(self, "校验失败", str(exc))

    def _import(self):
        csv_file = self.csv_input.text().strip()
        if not csv_file:
            QMessageBox.warning(self, "提示", "请填写 CSV 文件路径")
            return
        if not os.path.isfile(csv_file):
            QMessageBox.warning(self, "提示", f"文件不存在: {csv_file}")
            return

        self.log_text.clear()
        self.summary_label.setText("")
        self.progress.setValue(0)
        self.progress.setFormat("启动中...")

        self.worker = ImportWorker(
            csv_file=csv_file,
            engine=self.engine_combo.currentData(),
            workers=self.workers_spin.value(),
            retry=self.retry_spin.value(),
            result_file=self.result_input.text().strip(),
            only_failed=self.chk_only_failed.isChecked(),
            no_precheck=self.chk_no_precheck.isChecked(),
            dry_run=self.chk_dry_run.isChecked(),
        )
        self.worker.progress.connect(self._on_progress)
        self.worker.finished_signal.connect(self._on_finished)
        self.worker.start()

        self.btn_import.setEnabled(False)
        self.btn_validate.setEnabled(False)
        self.btn_stop.setEnabled(True)

    def _on_progress(self, done: int, total: int, msg: str, cls: str):
        if done >= 0 and total > 0:
            pct = int(done / total * 100)
            self.progress.setValue(pct)
            self.progress.setFormat(f"{done}/{total} ({pct}%)")
        self._append_log(msg, cls)

    def _on_finished(self, ok: int, fail: int, total: int):
        self.summary_label.setText(
            f"  成功: {ok}    失败: {fail}    总计: {total}"
        )
        color = "color: #2d7d2d;" if fail == 0 else "color: #c0392b;"
        self.summary_label.setStyleSheet(f"font-size: 14px; font-weight: bold; {color}")
        self.btn_import.setEnabled(True)
        self.btn_validate.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.progress.setFormat("完成")

    def _stop(self):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self._append_log("正在停止...", "warn")


# ── 导出页签 ────────────────────────────────────────────

class ExportTab(QWidget):
    def __init__(self):
        super().__init__()
        self.worker = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        grp = QGroupBox("导出设备配置")
        form = QFormLayout()

        self.device = QLineEdit("192.168.1.108")
        form.addRow("设备 IP:", self.device)

        self.username = QLineEdit("admin")
        form.addRow("用户名:", self.username)

        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        form.addRow("密码:", self.password)

        self.output = QLineEdit("exported_config.csv")
        out_row = QHBoxLayout()
        out_row.addWidget(self.output)
        btn = QPushButton("浏览...")
        btn.clicked.connect(self._browse_out)
        out_row.addWidget(btn)
        form.addRow("输出文件:", self._wrap(out_row))

        idx_row = QHBoxLayout()
        self.config_index = QSpinBox()
        self.config_index.setRange(1, 10)
        self.config_index.setValue(1)
        self.channel = QSpinBox()
        self.channel.setRange(0, 32)
        idx_row.addWidget(QLabel("配置序号:"))
        idx_row.addWidget(self.config_index)
        idx_row.addSpacing(20)
        idx_row.addWidget(QLabel("通道:"))
        idx_row.addWidget(self.channel)
        idx_row.addStretch()
        form.addRow("索引:", self._wrap(idx_row))

        self.engine = QComboBox()
        self.engine.addItem("RPC API（快）", "rpc")
        self.engine.addItem("Playwright（浏览器）", "playwright")
        form.addRow("引擎:", self.engine)

        grp.setLayout(form)
        layout.addWidget(grp)

        self.btn_export = QPushButton("导出配置")
        self.btn_export.setStyleSheet("QPushButton { background-color: #1890ff; color: white; font-weight: bold; padding: 8px 24px; }")
        self.btn_export.clicked.connect(self._export)
        layout.addWidget(self.btn_export)

        self.result_text = QTextEdit()
        self.result_text.setReadOnly(True)
        self.result_text.setFont(QFont("Consolas", 9))
        layout.addWidget(self.result_text, stretch=1)

    @staticmethod
    def _wrap(layout):
        w = QWidget()
        w.setLayout(layout)
        return w

    def _browse_out(self):
        path, _ = QFileDialog.getSaveFileName(self, "保存到", "", "CSV 文件 (*.csv);;Excel 文件 (*.xlsx)")
        if path:
            self.output.setText(path)

    def _export(self):
        device = self.device.text().strip()
        if not device:
            QMessageBox.warning(self, "提示", "请填写设备 IP")
            return

        self.result_text.clear()
        self.btn_export.setEnabled(False)
        self.result_text.append("正在导出...")

        engine = self.engine.currentData()
        if engine == "rpc":
            func = self._export_rpc
        else:
            func = self._export_playwright

        self.worker = SimpleWorker(func)
        self.worker.finished_signal.connect(self._on_result)
        self.worker.start()

    def _export_rpc(self):
        device = self.device.text().strip()
        session = DahuaSession(device, self.username.text().strip(), self.password.text())
        session.login()
        table = read_config(session)
        entry = _get_server_entry(table, self.config_index.value())
        if not entry:
            session.close()
            raise ValueError(f"配置序号 {self.config_index.value()} 不存在")
        channels = entry.get("ChannelInfoList", [])
        chan = channels[self.channel.value()] if channels else {}
        notify = chan.get("NotifyObjectList", {})
        row_dict = {k: str(v) for k, v in {
            "device_ip": device, "username": self.username.text().strip(),
            "password": self.password.text(), "config_index": self.config_index.value(),
            "enable": "TRUE" if entry.get("Enable") else "FALSE",
            "server_ip": entry.get("Address", ""), "server_port": entry.get("Port", ""),
            "keepalive": entry.get("KeepAlive", ""), "max_timeout": entry.get("MaxTimeoutTimes", ""),
            "device_id": entry.get("DeviceID", ""), "user_id": entry.get("UserId", ""),
            "password_viid": "", "reg_interval": entry.get("RegInterval", ""),
            "platform": entry.get("Platform", ""), "channel": self.channel.value(),
            "channel_encode": chan.get("ChannelEncode", ""),
            "collect_face": "TRUE" if notify.get("Face", {}).get("Checked") else "FALSE",
            "collect_image": "TRUE" if notify.get("Image", {}).get("Checked") else "FALSE",
            "collect_motor": "TRUE" if notify.get("MotorVehicle", {}).get("Checked") else "FALSE",
            "collect_nonmotor": "TRUE" if notify.get("NonMotorVehicle", {}).get("Checked") else "FALSE",
            "collect_person": "TRUE" if notify.get("Person", {}).get("Checked") else "FALSE",
            "collect_file": "TRUE" if notify.get("File", {}).get("Checked") else "FALSE",
            "collect_video": "TRUE" if notify.get("VideoSlice", {}).get("Checked") else "FALSE",
        }.items()}

        output = self.output.text().strip()
        write_rows(output, [row_dict])
        session.close()
        return f"已导出到 {output}\n\n" + self._format_row(row_dict), row_dict

    def _export_playwright(self):
        client = GaviClient(headless=False)
        row = client.export_config(
            self.device.text().strip(),
            self.username.text().strip(),
            self.password.text(),
            config_index=self.config_index.value(),
            channel=self.channel.value(),
        )
        output = self.output.text().strip()
        row_dict = row_to_export_dict(row)
        write_rows(output, [row_dict])
        return f"已导出到 {output}\n\n" + self._format_row(row_dict), row_dict

    @staticmethod
    def _format_row(row: dict) -> str:
        fields = ["device_ip", "device_id", "server_ip", "server_port",
                   "channel_encode", "enable", "keepalive", "max_timeout"]
        lines = []
        for k in fields:
            if k in row:
                lines.append(f"  {k}: {row[k]}")
        collect = {k: "✓" if row.get(k) == "TRUE" else "✗" for k in
                   ["collect_face", "collect_image", "collect_motor",
                    "collect_nonmotor", "collect_person"]}
        lines.append(f"  采集: 人脸={collect['collect_face']} 图片={collect['collect_image']} "
                     f"机动车={collect['collect_motor']} 非机动车={collect['collect_nonmotor']} "
                     f"人员={collect['collect_person']}")
        return "\n".join(lines)

    def _on_result(self, ok: bool, message: str, extra):
        self.btn_export.setEnabled(True)
        if ok:
            self.result_text.clear()
            self.result_text.setTextColor(QColor("#2d7d2d"))
            self.result_text.append(message)
        else:
            self.result_text.clear()
            self.result_text.setTextColor(QColor("#c0392b"))
            self.result_text.append(f"导出失败:\n{message}")


# ── 诊断页签 ────────────────────────────────────────────

class DiagTab(QWidget):
    def __init__(self):
        super().__init__()
        self.worker = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        grp = QGroupBox("设备连接测试")
        form = QFormLayout()
        self.device = QLineEdit("192.168.1.108")
        self.username = QLineEdit("admin")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        form.addRow("设备 IP:", self.device)
        form.addRow("用户名:", self.username)
        form.addRow("密码:", self.password)
        grp.setLayout(form)
        layout.addWidget(grp)

        btn_row = QHBoxLayout()
        self.btn_login = QPushButton("测试登录")
        self.btn_login.clicked.connect(self._test_login)
        self.btn_probe = QPushButton("探测配置节")
        self.btn_probe.clicked.connect(self._probe)
        btn_row.addWidget(self.btn_login)
        btn_row.addWidget(self.btn_probe)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self.result_text = QTextEdit()
        self.result_text.setReadOnly(True)
        self.result_text.setFont(QFont("Consolas", 9))
        layout.addWidget(self.result_text, stretch=1)

    def _test_login(self):
        device = self.device.text().strip()
        if not device:
            QMessageBox.warning(self, "提示", "请填写设备 IP")
            return
        self.result_text.clear()
        self.result_text.append("正在测试登录...")
        self.btn_login.setEnabled(False)

        def _do():
            session = DahuaSession(device, self.username.text().strip(), self.password.text())
            sid = session.login()
            session.close()
            return f"登录成功\nsession={sid}", None

        self.worker = SimpleWorker(_do)
        self.worker.finished_signal.connect(self._on_result)
        self.worker.start()

    def _probe(self):
        device = self.device.text().strip()
        if not device:
            QMessageBox.warning(self, "提示", "请填写设备 IP")
            return
        self.result_text.clear()
        self.result_text.append("正在探测配置节...")
        self.btn_probe.setEnabled(False)

        names = ["VSP_GAVI", "VSP_GAYS", "VSP_GAYS_SERVER", "Network", "VIID", "GA1400"]

        def _do():
            session = DahuaSession(device, self.username.text().strip(), self.password.text())
            session.login()
            results = []
            for name in names:
                try:
                    resp = session.rpc("configManager.getConfig", {"name": name, "options": []})
                    ok = resp.get("result") is not False
                    detail = json.dumps(resp.get("params", {}).get("table", resp.get("error", "")),
                                       ensure_ascii=False)[:300]
                    results.append((name, ok, detail))
                except Exception as exc:
                    results.append((name, False, str(exc)[:200]))
            session.close()
            lines = ["探测结果:\n"]
            for name, ok, detail in results:
                mark = "OK" if ok else "FAIL"
                lines.append(f"  [{mark}] {name}")
                if detail:
                    lines.append(f"       {detail[:200]}")
                lines.append("")
            return "\n".join(lines), None

        self.worker = SimpleWorker(_do)
        self.worker.finished_signal.connect(self._on_result)
        self.worker.start()

    def _on_result(self, ok: bool, message: str, extra):
        self.btn_login.setEnabled(True)
        self.btn_probe.setEnabled(True)
        self.result_text.clear()
        if ok:
            self.result_text.setTextColor(QColor("#2d7d2d") if "成功" in message or "OK" in message else QColor("#1a5276"))
        else:
            self.result_text.setTextColor(QColor("#c0392b"))
            self.result_text.append(f"失败:\n{message}")


# ── 主窗口 ──────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("大华 IPC 视图库批量配置工具")
        self.setMinimumSize(700, 600)
        self.resize(750, 700)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 8)

        tabs = QTabWidget()
        tabs.addTab(ImportTab(), "批量导入")
        tabs.addTab(ExportTab(), "配置导出")
        tabs.addTab(DiagTab(), "设备诊断")
        layout.addWidget(tabs)

        # 底部状态栏
        self.statusBar().showMessage("就绪")


def run_gui() -> int:
    # 打包环境下设置 Qt 插件路径
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
        for plugins_path in [
            os.path.join(base, "_internal", "PySide6", "plugins"),
            os.path.join(base, "PySide6", "plugins"),
        ]:
            if os.path.isdir(plugins_path):
                os.environ["QT_PLUGIN_PATH"] = plugins_path
                break

    # 隐藏控制台窗口（打包后 console=True 但不想看到黑框）
    try:
        import ctypes
        ctypes.windll.kernel32.GetConsoleWindow.restype = ctypes.c_void_p
        ctypes.windll.user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:
        pass

    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # 全局样式
    app.setStyleSheet("""
        QMainWindow { background: #f0f2f5; }
        QGroupBox { font-weight: bold; border: 1px solid #d9d9d9; border-radius: 6px; margin-top: 8px; padding-top: 12px; }
        QGroupBox::title { left: 12px; padding: 0 4px; }
        QPushButton { padding: 6px 16px; border-radius: 4px; }
        QPushButton:hover { opacity: 0.85; }
        QLineEdit, QSpinBox, QComboBox { padding: 4px 8px; border: 1px solid #d9d9d9; border-radius: 4px; }
        QLineEdit:focus, QSpinBox:focus, QComboBox:focus { border-color: #1890ff; }
        QProgressBar { border: 1px solid #d9d9d9; border-radius: 4px; text-align: center; }
        QProgressBar::chunk { background-color: #1890ff; border-radius: 3px; }
        QTabWidget::pane { border: 1px solid #d9d9d9; border-radius: 4px; }
        QTabBar::tab { padding: 8px 20px; border: 1px solid #d9d9d9; border-bottom: none; border-radius: 4px 4px 0 0; margin-right: 2px; }
        QTabBar::tab:selected { background: #1890ff; color: white; }
    """)

    window = MainWindow()
    window.show()
    return app.exec()
