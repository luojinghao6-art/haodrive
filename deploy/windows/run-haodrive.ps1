# HaoDrive 在 Windows 本机跑起来。
#
#   .\deploy\windows\run-haodrive.ps1                 # 默认 8000 端口
#   .\deploy\windows\run-haodrive.ps1 -Port 8010
#   .\deploy\windows\run-haodrive.ps1 -Reinstall      # 重建虚拟环境
#
# 只绑定 127.0.0.1，本机能访问，外网访问不了。
# 要给外网用，在它前面挂 Caddy + 隧道，见 docs/DEPLOY.md 的方案 C。
#
# 首次运行会创建 .venv、安装依赖、从 .env.example 生成 .env，
# 并把随机生成的管理员密码打印一次。
param(
    [int]$Port = 8000,
    [switch]$Reinstall
)

$ErrorActionPreference = "Stop"

$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $root

$venv = Join-Path $root ".venv"
$python = Join-Path $venv "Scripts\python.exe"
$envFile = Join-Path $root ".env"
$requirements = Join-Path $root "requirements.txt"

function New-Secret {
    param([int]$Length)
    $alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    $builder = New-Object System.Text.StringBuilder
    for ($i = 0; $i -lt $Length; $i++) {
        [void]$builder.Append($alphabet[(Get-Random -Maximum $alphabet.Length)])
    }
    return $builder.ToString()
}

if ($Reinstall -and (Test-Path $venv)) {
    Write-Host "删除旧的虚拟环境 ..."
    Remove-Item -Recurse -Force $venv
}

if (-not (Test-Path $python)) {
    Write-Host "创建虚拟环境 ..."
    python -m venv $venv
    & $python -m pip install --quiet --upgrade pip
}

# 每次启动都跑一次，缺依赖会自动补上
& $python -m pip install --quiet -r $requirements
if ($LASTEXITCODE -ne 0) { throw "依赖安装失败" }

if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $root ".env.example") $envFile
    $secretPath = New-Secret 24
    $adminPassword = New-Secret 32
    $appSecret = New-Secret 48
    $content = Get-Content $envFile -Raw -Encoding UTF8
    $content = $content -replace '(?m)^SECRET_PATH=.*$', "SECRET_PATH=$secretPath"
    $content = $content -replace '(?m)^ADMIN_PASSWORD=.*$', "ADMIN_PASSWORD=$adminPassword"
    $content = $content -replace '(?m)^APP_SECRET=.*$', "APP_SECRET=$appSecret"
    # 不带 BOM，免得第一行被当成奇怪的字符
    [System.IO.File]::WriteAllText($envFile, $content, (New-Object System.Text.UTF8Encoding($false)))
    $generated = $true
} else {
    $generated = $false
}

# 把 .env 读进当前进程的环境变量（应用只认环境变量）
foreach ($line in Get-Content $envFile -Encoding UTF8) {
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
        $name = $Matches[1]
        $value = $Matches[2].Trim()
        if ($value.Length -gt 0) {
            [Environment]::SetEnvironmentVariable($name, $value, "Process")
        }
    }
}

Write-Host ""
Write-Host "HaoDrive  http://127.0.0.1:$Port/"
if ($env:SECRET_PATH) {
    Write-Host "登录路径  http://127.0.0.1:$Port/$($env:SECRET_PATH)/"
}
if ($generated) {
    Write-Host ""
    Write-Host "管理员密码（只显示这一次）：$adminPassword"
    Write-Host "已写入 .env，想改就改 .env 里的 ADMIN_PASSWORD"
} else {
    Write-Host "配置来自 .env；改完重启这个脚本即可"
}
Write-Host ""

& $python -m uvicorn app.main:app --host 127.0.0.1 --port $Port --proxy-headers
