import sys
import os
import traceback


def _bootstrap():
    """兼容 PyInstaller 打包后的模块路径。"""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
        internal = os.path.join(base, "_internal")
        if os.path.isdir(internal) and internal not in sys.path:
            sys.path.insert(0, internal)


_bootstrap()


def main_entry():
    # windowed 模式下 stdout/stderr 被重定向到 NUL，Qt 打印日志会触发 OSError
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")

    log_dir = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
    try:
        try:
            from .batch_configure import main
        except ImportError:
            from src.batch_configure import main
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        log_path = os.path.join(log_dir, "error.log")
        with open(log_path, "w", encoding="utf-8") as f:
            traceback.print_exc(file=f)
        # GUI 模式下弹窗显示错误
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox
            app = QApplication.instance() or QApplication(sys.argv)
            QMessageBox.critical(None, "启动错误", traceback.format_exc()[:2000])
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main_entry()
