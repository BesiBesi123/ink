<#
  ink installer (Windows, NVIDIA GPU).
  Sets up everything ink needs, the same way it runs on the author's PC:
    - ink's llama.cpp build (upstream + patches, see llama/README.md) -> ~/.ink/bin/llama
    - the models from models.json                                     -> ~/.ink/models   (resumable downloads)
    - the terminal UI (npm packages)                                  -> tui/node_modules
    - an `ink` command on your PATH                                   -> ~/.ink/bin/ink.cmd
  usage:  powershell -ExecutionPolicy Bypass -File install.ps1 [-Models all|fast|smart|none] [-Repo owner/name]
#>
param(
  [ValidateSet("all", "fast", "smart", "none")] [string]$Models = "all",
  [string]$Repo = "BesiBesi123/ink",
  [switch]$Yes
)
$ErrorActionPreference = "Stop"
$App = $PSScriptRoot
$Home_ = Join-Path $env:USERPROFILE ".ink"
$Bin = Join-Path $Home_ "bin"
$LlamaDir = Join-Path $Bin "llama"
$ModelDir = Join-Path $Home_ "models"

function Say($m) { Write-Host "  $m" }
function Head($m) { Write-Host ""; Write-Host "== $m" -ForegroundColor Cyan }
function Ask($q) {
  if ($Yes) { return $true }
  $a = Read-Host "  $q [Y/n]"
  return ($a -eq "" -or $a -match "^[yY]")
}
function Have($cmd) { return [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

Write-Host "ink installer" -ForegroundColor Cyan
Say "app folder: $App"
Say "data folder: $Home_"

# ---------------------------------------------------------------- prerequisites
Head "Checking your PC"
if (-not (Have "nvidia-smi")) { throw "No NVIDIA driver found (nvidia-smi). ink needs an NVIDIA GPU with a recent driver." }
$gpu = (& nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader | Select-Object -First 1)
Say "GPU: $gpu"
$vram = [int](($gpu -split ",")[1] -replace "[^0-9]", "")
if ($vram -lt 7500) { Write-Warning "ink is tuned for 8 GB+ GPUs; with $vram MiB it may not start the models." }
$ramGB = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
Say "RAM: $ramGB GB"
if ($ramGB -lt 30) { Write-Warning "ink keeps most model weights in RAM; 32 GB is recommended." }

$py = $null
foreach ($c in @("python", "py")) {
  if (Have $c) {
    $v = & $c -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    if ($v -and [version]$v -ge [version]"3.11") { $py = (Get-Command $c).Source; break }
  }
}
if (-not $py) {
  if ((Have "winget") -and (Ask "Python 3.11+ not found. Install Python 3.12 with winget?")) {
    winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    $py = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
  } else { throw "Python 3.11+ is required (https://www.python.org/downloads/)." }
}
Say "Python: $py"

$node = Have "node"
if (-not $node -and (Have "winget") -and (Ask "Node.js not found (needed for the terminal UI). Install Node.js LTS with winget?")) {
  winget install -e --id OpenJS.NodeJS.LTS --accept-package-agreements --accept-source-agreements
  $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
  $node = Have "node"
}
if ($node) { Say "Node: $(node --version)" } else { Say "Node: not installed - ink will use its plain terminal interface" }
if (Test-Path "C:\Program Files\Git\bin\bash.exe") { Say "Git Bash: found (ink's run tool uses it)" }
else { Say "Git Bash: not found - ink's run tool will use PowerShell (install Git for Windows for best results)" }

# ---------------------------------------------------------------- llama.cpp build
Head "llama.cpp (ink build)"
New-Item -ItemType Directory -Force $LlamaDir | Out-Null
if (Test-Path (Join-Path $LlamaDir "llama-server.exe")) {
  Say "already installed in $LlamaDir"
} else {
  $rel = Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/latest" -Headers @{ "User-Agent" = "ink-installer" }
  foreach ($pattern in @("llama-ink-win-cuda*.zip", "cudart-win-cuda*.zip")) {
    $asset = $rel.assets | Where-Object { $_.name -like $pattern } | Select-Object -First 1
    if (-not $asset) { throw "Release $($rel.tag_name) of $Repo has no $pattern" }
    $zip = Join-Path $env:TEMP $asset.name
    Say "downloading $($asset.name) ($([math]::Round($asset.size / 1MB)) MB)"
    curl.exe -sSfL -o $zip $asset.browser_download_url
    Expand-Archive -Force $zip $LlamaDir
    Remove-Item $zip
  }
  Say "installed $($rel.tag_name) to $LlamaDir"
}

# ---------------------------------------------------------------- models
Head "Models"
New-Item -ItemType Directory -Force $ModelDir | Out-Null
$manifest = Get-Content (Join-Path $App "models.json") -Raw | ConvertFrom-Json
$want = $manifest.models | Where-Object {
  $Models -eq "all" -or ($Models -eq "fast" -and $_.role -eq "fast") -or ($Models -eq "smart" -and $_.role -in @("smart", "draft"))
}
if ($Models -eq "none") { $want = @() }
$need = 0
foreach ($m in $want) {
  $f = Join-Path $ModelDir $m.file
  $have = if (Test-Path $f) { (Get-Item $f).Length } else { 0 }
  $need += [math]::Max(0, $m.size - $have)
}
$free = (Get-PSDrive ($ModelDir.Substring(0, 1))).Free
Say ("to download: {0:N1} GB, free on disk: {1:N1} GB" -f ($need / 1GB), ($free / 1GB))
if ($need -gt $free - 2GB) { throw "Not enough disk space for the models (need $([math]::Round($need/1GB)) GB + 2 GB spare)." }
foreach ($m in $want) {
  $f = Join-Path $ModelDir $m.file
  if ((Test-Path $f) -and (Get-Item $f).Length -ge $m.size) { Say "$($m.file): already downloaded"; continue }
  Say "downloading $($m.file) ($([math]::Round($m.size / 1GB, 1)) GB) - resumable, re-run the installer if it stops"
  curl.exe -fL -C - --retry 5 --retry-delay 5 -o $f $m.url
  if ($LASTEXITCODE -ne 0) { throw "download failed: $($m.url) (re-run install.ps1 to resume)" }
}

# ---------------------------------------------------------------- terminal UI
if ($node) {
  Head "Terminal UI"
  Push-Location (Join-Path $App "tui")
  npm ci --omit=dev --no-audit --no-fund
  Pop-Location
}

# ---------------------------------------------------------------- launcher + PATH
Head "ink command"
New-Item -ItemType Directory -Force $Bin | Out-Null
$launcher = @"
@echo off
rem ink launcher (written by install.ps1)
set "INK_PY=$py"
where node >nul 2>nul
if errorlevel 1 (
  "%INK_PY%" -X utf8 "$App\ink.py" %*
  exit /b %errorlevel%
)
if not exist "$App\tui\node_modules" (
  "%INK_PY%" -X utf8 "$App\ink.py" %*
  exit /b %errorlevel%
)
node "$App\tui\ink-tui.mjs" %*
"@
Set-Content -Path (Join-Path $Bin "ink.cmd") -Value $launcher -Encoding ascii
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (($userPath -split ";") -notcontains $Bin) {
  [Environment]::SetEnvironmentVariable("Path", "$userPath;$Bin", "User")
  Say "added $Bin to your PATH (open a new terminal)"
}

Head "Done"
Say "Open a new terminal in a project folder and type:  ink"
Say "First start tunes the GPU layout for your card (a minute or two); after that ink starts in seconds."
Say "Docs: $App\README.md"
