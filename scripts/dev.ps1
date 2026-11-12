# 开发模式启动：改代码自动重启
#
#   .\scripts\dev.ps1            开发模式（推荐，改代码自动重启）
#   .\scripts\dev.ps1 -Port 8010 换个端口
#
# 只监视 src/ 与 prompts/：改代码、改进提示词会自动重启；
# 改 records/ 里的文档不会重启（文档自动保存不该打断正在进行的对话）。
# 退出：Ctrl+C。
param(
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "==> 认知镜像 P0 · 开发模式" -ForegroundColor Cyan
Write-Host "==> http://127.0.0.1:$Port  （Ctrl+C 退出）"
Write-Host "==> 改 src/ 或 prompts/ 下的代码会自动重启；改文档不会。"

python run.py --reload --port $Port
