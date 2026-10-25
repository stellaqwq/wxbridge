param(
    [switch]$Mock,
    [switch]$NoBrowser,
    [string]$Config = ""
)
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
try {
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw "Please install uv first: https://docs.astral.sh/uv/getting-started/installation/"
    }
    $env:UV_CACHE_DIR = Join-Path $PSScriptRoot ".uv-cache"
    & uv sync --locked --extra onebot --no-dev
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }
    if (-not $Config) {
        $Config = if ($Mock) { "data/onebot-demo.json" } else { "data/onebot.json" }
    }
    $botArgs = @("--config", $Config)
    if ($Mock) { $botArgs += "--mock" }
    if (-not (Test-Path -LiteralPath $Config)) {
        & ./.venv/Scripts/python.exe -m wxbridge.onebot.cli @botArgs --init
        if ($LASTEXITCODE -ne 0) { throw "Configuration creation failed." }
    }
    if (-not $NoBrowser) { $botArgs += "--open-browser" }
    & ./.venv/Scripts/python.exe -m wxbridge.onebot.cli @botArgs
    exit $LASTEXITCODE
} catch {
    Write-Host "Startup failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
