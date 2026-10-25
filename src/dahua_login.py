"""大华 RPC2 两阶段 MD5 登录与会话保持。"""

from __future__ import annotations

import hashlib
import threading
import time
from typing import Any

import requests


class DahuaLoginError(Exception):
    """登录失败。"""


class DahuaSession:
    """大华设备 RPC2 会话。"""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        client_type: str = "Web5.0",
        timeout: float | tuple[float, float] = (3.0, 12.0),
        use_https: bool = False,
    ) -> None:
        self.host = host.strip()
        self.username = username
        self.password = password
        self.client_type = client_type
        self.timeout = timeout
        scheme = "https" if use_https else "http"
        self.base_url = f"{scheme}://{self.host}"
        self.login_url = f"{self.base_url}/RPC2_Login"
        self.rpc_url = f"{self.base_url}/RPC2"
        self.session_id: str | None = None
        self._request_id = 0
        self._keepalive_thread: threading.Thread | None = None
        self._keepalive_stop = threading.Event()
        self._http = requests.Session()

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    @staticmethod
    def _md5_upper(text: str) -> str:
        return hashlib.md5(text.encode("utf-8")).hexdigest().upper()

    def _post(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        allow_challenge: bool = False,
    ) -> dict[str, Any]:
        response = self._http.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("result") is False:
            error = data.get("error", {})
            message = error.get("message", str(data))
            if allow_challenge and "login challenge" in message.lower():
                return data
            raise DahuaLoginError(message)
        return data

    def login(self) -> str:
        """执行两阶段登录，返回 session id。"""
        first = self._post(
            self.login_url,
            {
                "method": "global.login",
                "params": {
                    "userName": self.username,
                    "password": "",
                    "clientType": self.client_type,
                },
                "id": self._next_id(),
            },
            allow_challenge=True,
        )
        temp_session = first["session"]
        params = first.get("params") or {}
        realm = params.get("realm")
        random_value = params.get("random")
        if not realm or random_value is None:
            raise DahuaLoginError("登录挑战缺少 realm 或 random")

        pwd_hash = self._md5_upper(f"{self.username}:{realm}:{self.password}")
        pass_hash = self._md5_upper(f"{self.username}:{random_value}:{pwd_hash}")

        second = self._post(
            self.login_url,
            {
                "method": "global.login",
                "params": {
                    "userName": self.username,
                    "password": pass_hash,
                    "clientType": self.client_type,
                    "authorityType": "Default",
                    "passwordType": "Default",
                    "realm": realm,
                    "random": random_value,
                },
                "id": self._next_id(),
                "session": temp_session,
            },
        )
        self.session_id = second["session"]
        return self.session_id

    def rpc(self, method: str, params: Any = None) -> dict[str, Any]:
        """调用 RPC2 接口。"""
        if not self.session_id:
            raise DahuaLoginError("尚未登录")
        payload: dict[str, Any] = {
            "method": method,
            "params": params,
            "id": self._next_id(),
            "session": self.session_id,
        }
        return self._post(self.rpc_url, payload)

    def raw_rpc(self, method: str, params: Any = None) -> dict[str, Any]:
        """调用 RPC2 接口，不检查 result，返回原始响应。"""
        if not self.session_id:
            raise DahuaLoginError("尚未登录")
        payload: dict[str, Any] = {
            "method": method,
            "params": params,
            "id": self._next_id(),
            "session": self.session_id,
        }
        response = self._http.post(
            self.rpc_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=self.timeout,
        )
        return {"status_code": response.status_code, "body": response.text}

    def keep_alive(self, timeout: int = 300) -> None:
        """发送一次 keepAlive。"""
        self.rpc("global.keepAlive", {"timeout": timeout, "active": True})

    def start_keepalive(self, interval: int = 55) -> None:
        """后台线程定期 keepAlive。"""
        if self._keepalive_thread and self._keepalive_thread.is_alive():
            return

        self._keepalive_stop.clear()

        def _loop() -> None:
            while not self._keepalive_stop.wait(interval):
                try:
                    self.keep_alive()
                except Exception:
                    break

        self._keepalive_thread = threading.Thread(target=_loop, daemon=True)
        self._keepalive_thread.start()

    def stop_keepalive(self) -> None:
        """停止 keepAlive 线程。"""
        self._keepalive_stop.set()

    def logout(self) -> None:
        """注销会话，释放设备端 session 资源。"""
        if self.session_id:
            try:
                self.rpc("global.logout")
            except Exception:
                pass
            finally:
                self.session_id = None

    def close(self) -> None:
        self.stop_keepalive()
        self.logout()
        try:
            self._http.close()
        except Exception:
            pass

    def __enter__(self) -> "DahuaSession":
        self.login()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
