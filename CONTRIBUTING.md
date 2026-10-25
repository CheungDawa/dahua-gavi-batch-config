# 贡献指南 (Contributing Guide)

感谢你关注本项目！我们非常欢迎并感谢社区提供 Issue 报告、固件兼容性测试反馈和代码贡献。

## 开发准备

1. **Fork 本仓库** 并 Clone 到本地。
2. **准备 Python 环境**（建议 Python 3.9+）：
   ```powershell
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```
3. 若需要调试 Playwright 浏览器引擎（兜底引擎）：
   ```powershell
   playwright install chromium
   ```

## 代码规范

- 遵循 PEP 8 风格指南与 Python 类型注解（`type hints`）。
- 保持文档与注释简洁清晰。
- **严禁提交任何真实客户 IP、生产环境密码、设备序列号或抓包密文**。提交模板与测试用例时一律使用 RFC 5737 示例保留地址（如 `192.168.1.x`）与脱敏参数。

## 提交 Pull Request

1. 从 `main` 分支拉取新的特性分支：`git checkout -b feature/your-feature-name`
2. 本地测试语法与功能：
   ```powershell
   python -m py_compile src/*.py
   python -m src validate config_template.csv
   ```
3. 提交 Commit（推荐 Conventional Commits 规范，如 `fix: 修复某些固件下的连接复用问题`）。
4. 推送到你的 Fork 仓库并向本项目发起 Pull Request。
