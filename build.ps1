# -*- mode: powershell -*-
#
# 大华视图库批量配置工具 — 一键打包脚本
# 用法: powershell -ExecutionPolicy Bypass -File build.ps1
# 输出: dist\大华视图库批量配置工具\（整个文件夹拷给别人即可用）
#
# 包含所有依赖:
#   - Python 运行时 + 项目代码
#   - PySide6 GUI 框架
#   - Playwright + Chromium 浏览器（~430MB）
#   - 大华 DLL（dhnetsdk / dhconfigsdk 等，~85MB）
#   - requests / cryptography 等
#
# 最终文件夹约 600-700MB，可压缩为 zip 分发

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  大华视图库批量配置工具 — 打包脚本" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

# ── [1] 检查依赖 ────────────────────────────────────────
Write-Host "[1/5] 检查 Python 依赖..." -ForegroundColor Yellow
$pkgs = @{
    "PySide6"      = "GUI 框架"
    "requests"     = "HTTP 请求"
    "playwright"   = "浏览器自动化"
    "PyInstaller"  = "打包工具"
    "cryptography" = "加密库"
}
foreach ($pkg in $pkgs.Keys) {
    $desc = $pkgs[$pkg]
    $check = python -c "import $pkg; print('OK')" 2>$null
    if ($check -ne "OK") {
        Write-Host "  安装 $pkg ($desc)..." -ForegroundColor Yellow
        python -m pip install $pkg 2>&1 | Out-Null
    }
    Write-Host "  $pkg ($desc) OK" -ForegroundColor Green
}

# 检查 Playwright Chromium
$chromiumDir = Get-ChildItem "$env:LOCALAPPDATA\ms-playwright" -Directory -Filter "chromium-*" -ErrorAction SilentlyContinue | Sort-Object Name -Descending | Select-Object -First 1
if (-not $chromiumDir) {
    Write-Host "  安装 Playwright Chromium..." -ForegroundColor Yellow
    python -m playwright install chromium
}
Write-Host "  Chromium OK ($($chromiumDir.Name))" -ForegroundColor Green

# 检查大华 DLL（可选备用）
$dllDir = "C:\Program Files (x86)\ConfigTool\ConfigTool"
if (Test-Path "$dllDir\dhnetsdk.dll") {
    $dllCount = (Get-ChildItem $dllDir -Filter "*.dll").Count
    Write-Host "  大华 DLL OK ($dllCount 个文件)" -ForegroundColor Green
} else {
    Write-Host "  提示: 未找到大华 ConfigTool DLL ($dllDir)，跳过 SDK 备用库" -ForegroundColor Gray
}

# ── [2] 清理旧构建 ──────────────────────────────────────
Write-Host ""
Write-Host "[2/5] 清理旧构建..." -ForegroundColor Yellow
Remove-Item -Path "build", "dist" -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -Path "*.spec" -Force -ErrorAction SilentlyContinue
Write-Host "  完成" -ForegroundColor Green

# ── [3] 打包 ────────────────────────────────────────────
Write-Host ""
Write-Host "[3/5] 开始打包（onedir 模式，含 Chromium + 大华 DLL）..." -ForegroundColor Yellow
Write-Host "  预计需要 3-5 分钟，请耐心等待..." -ForegroundColor Gray
Write-Host ""

python -m PyInstaller dahua_tool.spec --noconfirm 2>&1 | ForEach-Object {
    if ($_ -match "ERROR|FAIL") {
        Write-Host "  $_" -ForegroundColor Red
    } elseif ($_ -match "completed successfully|Building EXE|Building COLLECT") {
        Write-Host "  $_" -ForegroundColor Green
    } else {
        Write-Host "  $_" -ForegroundColor Gray
    }
}

if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "打包失败！请检查上方错误信息。" -ForegroundColor Red
    exit 1
}

# ── [4] 验证输出 ────────────────────────────────────────
Write-Host ""
Write-Host "[4/5] 验证输出..." -ForegroundColor Yellow
$outDir = "dist\大华视图库批量配置工具"
$exe = "$outDir\大华视图库批量配置工具.exe"

if (Test-Path $exe) {
    $totalSize = (Get-ChildItem $outDir -Recurse | Measure-Object -Property Length -Sum).Sum / 1MB
    $totalSizeGB = [math]::Round($totalSize, 0)
    Write-Host "  EXE: $exe" -ForegroundColor Green
    Write-Host "  文件夹大小: $totalSizeGB MB" -ForegroundColor Green

    # 验证关键文件
    $checks = @(
        @("_internal\playwright\driver\node.exe", "Playwright driver"),
        @("_internal\ms-playwright\chromium-*\chrome-win\chrome.exe", "Chromium 浏览器"),
        @("_internal\src\gui_qt.py", "GUI 模块"),
        @("_internal\src\vsp_api.py", "VSP_GAVI API 模块")
    )
    foreach ($check in $checks) {
        $pattern = $check[0]
        $desc = $check[1]
        $found = Get-ChildItem "$outDir\_internal\$pattern" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($found) {
            Write-Host "  [OK] $desc" -ForegroundColor Green
        } else {
            Write-Host "  [缺失] $desc" -ForegroundColor Red
        }
    }
} else {
    Write-Host "  未找到输出: $exe" -ForegroundColor Red
    exit 1
}

# ── [5] 完成 ────────────────────────────────────────────
Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  打包完成！" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  输出目录: $outDir" -ForegroundColor White
Write-Host "  启动方式: 双击 大华视图库批量配置工具.exe" -ForegroundColor White
Write-Host "  CLI 模式: 大华视图库批量配置工具.exe import current.csv --engine rpc -w 30" -ForegroundColor White
Write-Host "  GUI 模式: 大华视图库批量配置工具.exe gui" -ForegroundColor White
Write-Host ""
Write-Host "  分发方式: 将整个文件夹压缩为 zip 拷给别人即可" -ForegroundColor Yellow
Write-Host "  注意: 首次启动可能需要几秒钟初始化" -ForegroundColor Yellow
Write-Host ""
