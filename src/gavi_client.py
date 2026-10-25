"""Playwright 桥接：读写大华 IPC 视图库（GAVI）配置。"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field

from playwright.sync_api import Page, Request, Response, TimeoutError as PlaywrightTimeoutError, sync_playwright

from .csv_loader import COLLECT_MAP, GaviConfigRow


def _setup_playwright_env() -> None:
    """打包环境下定位 Playwright Chromium 浏览器路径。"""
    if getattr(sys, "frozen", False):
        # PyInstaller 打包模式
        base = os.path.dirname(sys.executable)
        # 尝试 _internal/ms-playwright/chromium-XXXX/chrome-win/chrome.exe
        ms_pw = os.path.join(base, "_internal", "ms-playwright")
        if not os.path.isdir(ms_pw):
            ms_pw = os.path.join(base, "ms-playwright")
        if os.path.isdir(ms_pw):
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = ms_pw

    # 也处理 Playwright driver 路径
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
        for pw_path in [
            os.path.join(base, "_internal", "playwright", "driver"),
            os.path.join(base, "playwright", "driver"),
        ]:
            if os.path.isfile(os.path.join(pw_path, "node.exe")):
                os.environ.setdefault("PLAYWRIGHT_DRIVER_PATH", pw_path)
                break


_setup_playwright_env()

GAVI_ROUTE = "#/index/netset/AccessPlatform"
MASKED_PASSWORD = "******"


@dataclass
class ApplyResult:
    success: bool
    message: str
    register_status: str | None = None


class GaviClientError(Exception):
    """GAVI 操作失败。"""


class GaviClient:
    """通过 Web UI 读写 GAVI 配置。"""

    def __init__(self, headless: bool = True, timeout_ms: int = 30000) -> None:
        self.headless = headless
        self.timeout_ms = timeout_ms
        self._playwright = None
        self._browser = None

    _CHROMIUM_ARGS = [
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-component-update",
        "--mute-audio",
    ]

    _BLOCKED_EXTS = (
        ".png", ".jpg", ".jpeg", ".svg", ".ico", ".gif",
        ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".mp3",
    )

    def _launch_browser(self):
        return self._playwright.chromium.launch(
            headless=self.headless,
            args=self._CHROMIUM_ARGS,
        )

    def __enter__(self) -> "GaviClient":
        self._playwright = sync_playwright().start()
        self._browser = self._launch_browser()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None

    def _ensure_browser(self) -> bool:
        """启动 Playwright + 浏览器（若尚未启动）。返回是否由本次创建。"""
        if self._browser is not None:
            return False
        self._playwright = sync_playwright().start()
        self._browser = self._launch_browser()
        return True

    def _close_if_owned(self, owned: bool) -> None:
        if owned:
            if self._browser is not None:
                self._browser.close()
                self._browser = None
            if self._playwright is not None:
                self._playwright.stop()
                self._playwright = None

    def _new_page(self):
        page = self._browser.new_page(viewport={"width": 1920, "height": 1080})

        def _route_handler(route) -> None:
            req = route.request
            if req.resource_type in ("image", "font", "media"):
                route.abort()
                return
            url = req.url.split("?")[0].lower()
            if any(url.endswith(ext) for ext in self._BLOCKED_EXTS):
                route.abort()
                return
            route.continue_()

        page.route("**/*", _route_handler)
        return page

    def _login(self, page: Page, username: str, password: str, base_url: str) -> None:
        page.goto(f"{base_url}/#/", wait_until="domcontentloaded")
        page.get_by_role("textbox", name="用户名").fill(username)
        page.get_by_role("textbox", name="密码").fill(password)
        page.get_by_role("button", name="登 录").click()
        page.wait_for_url(re.compile(r"#/index/"), timeout=self.timeout_ms)

    def _navigate_to_platform(self, page: Page) -> None:
        """SPA 内通过菜单进入：设置 → 网络设置 → 平台接入。"""
        page.get_by_title("设置").click()
        try:
            page.wait_for_function(
                """() => Array.from(document.querySelectorAll('.ant-dropdown-menu-item'))
                    .some(el => el.innerText.includes('网络设置'))""",
                timeout=self.timeout_ms,
            )
        except PlaywrightTimeoutError as exc:
            raise GaviClientError(f"网络设置菜单未出现: {exc}") from exc
        page.evaluate(
            """() => {
            const items = Array.from(document.querySelectorAll('.ant-dropdown-menu-item'));
            const item = items.find(el => el.innerText.includes('网络设置'));
            if (item) item.click();
        }"""
        )
        try:
            page.wait_for_function(
                """() => Array.from(document.querySelectorAll('.ant-menu-item, .ant-menu-submenu-title, li'))
                    .some(el => el.innerText && el.innerText.trim() === '平台接入')""",
                timeout=self.timeout_ms,
            )
        except PlaywrightTimeoutError as exc:
            raise GaviClientError(f"平台接入菜单未出现: {exc}") from exc
        page.evaluate(
            """() => {
            const items = Array.from(document.querySelectorAll('.ant-menu-item, .ant-menu-submenu-title, li'));
            const item = items.find(el => el.innerText.trim() === '平台接入');
            if (item) item.click();
        }"""
        )
        try:
            page.wait_for_function(
                "() => location.hash.includes('AccessPlatform')",
                timeout=self.timeout_ms,
            )
        except PlaywrightTimeoutError as exc:
            raise GaviClientError(f"未能进入平台接入页面: {exc}") from exc

    def _open_gavi_tab(self, page: Page, base_url: str) -> None:
        del base_url  # 保持 SPA 会话，禁止 page.goto 跳转
        self._navigate_to_platform(page)

        gavi_tab = page.get_by_role("tab", name="视图库")
        gavi_tab.wait_for(state="visible", timeout=self.timeout_ms)
        gavi_tab.click()
        try:
            page.wait_for_selector("#VSPGB_ServerIP", state="visible", timeout=self.timeout_ms)
        except PlaywrightTimeoutError as exc:
            raise GaviClientError(f"视图库配置表单未出现: {exc}") from exc

        if page.locator("#VSPGB_ServerIP").count() == 0:
            raise GaviClientError("未找到视图库配置表单")

    def _select_config_index(self, page: Page, config_index: int) -> None:
        """选择「视图库配置信息」下拉（1-based）。"""
        if config_index <= 1:
            return
        # ant-design combobox: 点击后选择对应序号
        combobox = page.locator("._GAVI_").get_by_role("combobox").first
        if combobox.count() == 0:
            return
        combobox.click()
        option_text = str(config_index)
        page.get_by_text(option_text, exact=True).click()
        page.wait_for_timeout(800)

    def _select_channel(self, page: Page, channel: int) -> None:
        channel_area = page.locator("text=通道信息").locator("xpath=ancestor::div[contains(@class,'ant-card') or contains(@class,'ant-collapse')][1]")
        if channel_area.count() == 0:
            channel_area = page.locator("text=通道信息").locator("xpath=ancestor::div[1]")
        combobox = channel_area.get_by_role("combobox").first
        if combobox.count() == 0:
            return
        combobox.click()
        page.locator(".ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option").filter(
            has_text=str(channel + 1)
        ).first.click()
        page.wait_for_timeout(500)

    def _set_enable(self, page: Page, enabled: bool) -> None:
        switch = page.locator("._GAVI_").get_by_role("switch").first
        if switch.count() == 0:
            return
        checked = switch.get_attribute("aria-checked") == "true"
        if checked != enabled:
            switch.click()

    def _set_platform(self, page: Page, platform: int) -> None:
        """设置接入识别码；失败时保留原值。"""
        gavi = page.locator("._GAVI_")
        boxes = gavi.get_by_role("combobox")
        if boxes.count() < 2:
            return
        # 0=配置信息序号, 1=接入识别码
        boxes.nth(1).click()
        options = page.locator(".ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option")
        target = options.filter(has_text=re.compile(rf"^{platform}\("))
        if target.count() == 0:
            target = options.filter(has_text=str(platform))
        if target.count() == 0:
            page.keyboard.press("Escape")
            return
        target.first.click(timeout=5000)
        page.wait_for_timeout(400)

    def _set_collect_objects(self, page: Page, collect: dict[str, bool | None]) -> None:
        for json_key, checked in collect.items():
            if checked is None:
                continue
            checkbox = page.locator(f"input.ant-checkbox-input[value='{json_key}']")
            if checkbox.count() == 0:
                continue
            is_checked = checkbox.first.is_checked()
            if is_checked != checked:
                checkbox.first.click(force=True)

    def _read_field(self, page: Page, field_id: str) -> str:
        locator = page.locator(f"#{field_id}")
        if locator.count() == 0:
            return ""
        return locator.input_value()

    @staticmethod
    def _parse_optional_int(value: str) -> int | None:
        text = (value or "").strip()
        if not text:
            return None
        return int(text)

    def _read_register_status(self, page: Page) -> str:
        status = page.locator("text=设备注册状态").locator("xpath=following::*[1]")
        if status.count() > 0:
            return status.first.inner_text().strip()
        return ""

    def _wait_settings_visible(self, page: Page) -> None:
        """登录后等待主界面渲染完成（替代固定 sleep）。"""
        try:
            page.get_by_title("设置").wait_for(state="visible", timeout=self.timeout_ms)
        except PlaywrightTimeoutError as exc:
            raise GaviClientError(f"主界面未渲染完成: {exc}") from exc

    def export_config(
        self,
        device_ip: str,
        username: str,
        password: str,
        config_index: int = 1,
        channel: int = 0,
    ) -> GaviConfigRow:
        base_url = f"http://{device_ip}"
        owned = self._ensure_browser()
        page = self._new_page()
        try:
            page.goto(f"{base_url}/", wait_until="domcontentloaded")
            self._login(page, username, password, base_url)
            self._wait_settings_visible(page)
            self._open_gavi_tab(page, base_url)
            self._select_config_index(page, config_index)

            enable_switch = page.locator("._GAVI_").get_by_role("switch").first
            enable = enable_switch.get_attribute("aria-checked") == "true" if enable_switch.count() else None

            collect: dict[str, bool | None] = {}
            for json_key in COLLECT_MAP.values():
                cb = page.locator(f"input.ant-checkbox-input[value='{json_key}']")
                if cb.count() > 0:
                    collect[json_key] = cb.first.is_checked()

            pwd = self._read_field(page, "PasswordInput_VSPGB_Password")
            if "●" in pwd:
                pwd = ""

            row = GaviConfigRow(
                device_ip=device_ip,
                username=username,
                password=password,
                config_index=config_index,
                enable=enable,
                server_ip=self._read_field(page, "VSPGB_ServerIP") or None,
                server_port=self._parse_optional_int(self._read_field(page, "VSPGB_Port")),
                keepalive=self._parse_optional_int(self._read_field(page, "VSPGB_KeepAlive")),
                max_timeout=self._parse_optional_int(self._read_field(page, "VSPGB_MaxTimeoutTimes")),
                device_id=self._read_field(page, "VSPGB_DeviceID") or None,
                user_id=self._read_field(page, "VSPGB_UserId") or None,
                password_viid=pwd or None,
                reg_interval=self._parse_optional_int(self._read_field(page, "VSPGB_RegInterval")),
                platform=None,
                channel=channel,
                channel_encode=self._read_field(page, "VSPGB_ChannelEncode") or None,
                collect=collect,
            )
            return row
        finally:
            page.close()
            self._close_if_owned(owned)

    def apply_config(self, row: GaviConfigRow, *, dry_run: bool = False) -> ApplyResult:
        if not row.has_gavi_fields():
            return ApplyResult(False, "未提供任何 GAVI 配置字段")

        base_url = f"http://{row.device_ip}"
        owned = self._ensure_browser()
        page = self._new_page()
        try:
            page.goto(f"{base_url}/", wait_until="domcontentloaded")
            self._login(page, row.username, row.password, base_url)
            self._wait_settings_visible(page)
            self._open_gavi_tab(page, base_url)
            self._select_config_index(page, row.config_index)

            if row.enable is not None:
                self._set_enable(page, row.enable)
            if row.channel is not None and row.channel > 0 and row.channel_encode is not None:
                self._select_channel(page, row.channel)

            # 批量填充文本字段：单次 evaluate 替代 10 次 locator.fill round-trip
            fill_map: dict[str, str] = {}
            if row.server_ip is not None:
                fill_map["VSPGB_ServerIP"] = str(row.server_ip)
            if row.keepalive is not None:
                fill_map["VSPGB_KeepAlive"] = str(row.keepalive)
            if row.device_id is not None:
                fill_map["VSPGB_DeviceID"] = str(row.device_id)
            if row.user_id is not None:
                fill_map["VSPGB_UserId"] = str(row.user_id)
            if row.server_port is not None:
                fill_map["VSPGB_Port"] = str(row.server_port)
            if row.max_timeout is not None:
                fill_map["VSPGB_MaxTimeoutTimes"] = str(row.max_timeout)
            if row.reg_interval is not None:
                fill_map["VSPGB_RegInterval"] = str(row.reg_interval)
            if row.channel_encode is not None:
                fill_map["VSPGB_ChannelEncode"] = str(row.channel_encode)
            if row.password_viid and row.password_viid not in {MASKED_PASSWORD, ""}:
                fill_map["PasswordInput_VSPGB_Password"] = str(row.password_viid)
            if fill_map:
                page.evaluate(
                    """(fields) => {
                        const setNativeValue = (el, value) => {
                            const proto = el instanceof HTMLTextAreaElement
                                ? HTMLTextAreaElement.prototype
                                : HTMLInputElement.prototype;
                            Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, String(value));
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                        };
                        for (const [id, value] of Object.entries(fields)) {
                            const el = document.getElementById(id);
                            if (el) setNativeValue(el, value);
                        }
                    }""",
                    fill_map,
                )

            if row.platform is not None:
                self._set_platform(page, row.platform)
            if row.collect:
                self._set_collect_objects(page, row.collect)

            if dry_run:
                return ApplyResult(True, "dry-run: 已填写表单但未提交")

            page.get_by_role("button", name="应 用").click()
            try:
                page.wait_for_function(
                    """() => document.body.innerText.includes('操作成功')
                        || !!document.querySelector('.ant-message-error, .ant-notification-notice-message')""",
                    timeout=10000,
                )
            except PlaywrightTimeoutError:
                pass  # 未检测到明确提示时按已提交处理

            success_text = page.locator("text=操作成功")
            error_text = page.locator(".ant-message-error, .ant-notification-notice-message")
            if success_text.count() > 0:
                status = self._read_register_status(page)
                return ApplyResult(True, "操作成功", status)
            if error_text.count() > 0:
                return ApplyResult(False, error_text.first.inner_text().strip())
            return ApplyResult(True, "已提交（未检测到明确提示）", self._read_register_status(page))
        except PlaywrightTimeoutError as exc:
            raise GaviClientError(f"页面操作超时: {exc}") from exc
        finally:
            page.close()
            self._close_if_owned(owned)


# ── 抓包 ───────────────────────────────────────────────

@dataclass
class CapturedRequest:
    method: str
    url: str
    post_data: str | None
    status: int
    response_body: str | None
    content_type: str | None


def sniff_config(
    device_ip: str,
    username: str,
    password: str,
    *,
    headless: bool = False,
    timeout_ms: int = 30000,
    capture_apply: bool = True,
) -> list[CapturedRequest]:
    """打开浏览器登录并进入视图库页面，捕获所有 API 请求。

    capture_apply=True 时会尝试点击"应用"按钮以捕获写入接口。
    返回按时间顺序排列的请求列表。
    """
    captured: list[CapturedRequest] = []
    base_url = f"http://{device_ip}"

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        page = browser.new_page(viewport={"width": 1920, "height": 1080})

        def _on_response(response: Response) -> None:
            request = response.request
            url = request.url
            # 跳过静态资源
            if any(url.endswith(ext) for ext in (".js", ".css", ".png", ".jpg", ".svg", ".ico", ".woff", ".woff2", ".ttf")):
                return
            if "/static/" in url or "/assets/" in url:
                return
            post_data = request.post_data
            resp_body = None
            try:
                resp_body = response.text()
            except Exception:
                pass
            content_type = response.headers.get("content-type", "")
            captured.append(CapturedRequest(
                method=request.method,
                url=url,
                post_data=post_data,
                status=response.status,
                response_body=resp_body,
                content_type=content_type,
            ))

        page.on("response", _on_response)

        # 登录
        page.goto(f"{base_url}/#/", wait_until="domcontentloaded")
        page.get_by_role("textbox", name="用户名").fill(username)
        page.get_by_role("textbox", name="密码").fill(password)
        page.get_by_role("button", name="登 录").click()
        page.wait_for_url(re.compile(r"#/index/"), timeout=timeout_ms)

        # 等待主界面
        page.get_by_title("设置").wait_for(state="visible", timeout=timeout_ms)

        # 导航到平台接入
        page.get_by_title("设置").click()
        page.wait_for_function(
            """() => Array.from(document.querySelectorAll('.ant-dropdown-menu-item'))
                .some(el => el.innerText.includes('网络设置'))""",
            timeout=timeout_ms,
        )
        page.evaluate("""() => {
            const items = Array.from(document.querySelectorAll('.ant-dropdown-menu-item'));
            const item = items.find(el => el.innerText.includes('网络设置'));
            if (item) item.click();
        }""")
        page.wait_for_function(
            """() => Array.from(document.querySelectorAll('.ant-menu-item, .ant-menu-submenu-title, li'))
                .some(el => el.innerText && el.innerText.trim() === '平台接入')""",
            timeout=timeout_ms,
        )
        page.evaluate("""() => {
            const items = Array.from(document.querySelectorAll('.ant-menu-item, .ant-menu-submenu-title, li'));
            const item = items.find(el => el.innerText.trim() === '平台接入');
            if (item) item.click();
        }""")
        page.wait_for_function(
            "() => location.hash.includes('AccessPlatform')",
            timeout=timeout_ms,
        )

        # 点击视图库标签
        gavi_tab = page.get_by_role("tab", name="视图库")
        gavi_tab.wait_for(state="visible", timeout=timeout_ms)
        gavi_tab.click()

        # 等待配置表单加载（说明读取接口已返回）
        try:
            page.wait_for_selector("#VSPGB_ServerIP", state="visible", timeout=timeout_ms)
        except PlaywrightTimeoutError:
            pass

        # 等一下确保异步请求都完成
        page.wait_for_timeout(2000)

        read_count = len(captured)

        if capture_apply:
            # 点应用按钮捕获写入接口
            apply_btn = page.get_by_role("button", name="应 用")
            if apply_btn.count() > 0:
                apply_btn.click()
                try:
                    page.wait_for_function(
                        """() => document.body.innerText.includes('操作成功')
                            || !!document.querySelector('.ant-message-error, .ant-notification-notice-message')""",
                        timeout=15000,
                    )
                except PlaywrightTimeoutError:
                    pass
                page.wait_for_timeout(2000)

        browser.close()

    return captured
