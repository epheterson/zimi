# Zimi Windows Installer
# Run: powershell -ExecutionPolicy Bypass -File install-windows.ps1

$ErrorActionPreference = "Stop"
$ZimiDir = "$env:LOCALAPPDATA\Zimi"
$Branch = "v1.3"

Write-Host ""
Write-Host "=== Zimi Installer ===" -ForegroundColor Yellow
Write-Host ""

# --- Python ---
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Host "Installing Python 3.11..." -ForegroundColor Cyan
    winget install Python.Python.3.11 --accept-package-agreements --accept-source-agreements
    # Refresh PATH
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) {
        Write-Host "Python installed but PATH not updated. Please close and reopen PowerShell, then run this script again." -ForegroundColor Red
        pause
        exit 1
    }
}
Write-Host "Python: $(python --version)" -ForegroundColor Green

# --- Git ---
$git = Get-Command git -ErrorAction SilentlyContinue
if (-not $git) {
    Write-Host "Installing Git..." -ForegroundColor Cyan
    winget install Git.Git --accept-package-agreements --accept-source-agreements
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
    $git = Get-Command git -ErrorAction SilentlyContinue
    if (-not $git) {
        Write-Host "Git installed but PATH not updated. Please close and reopen PowerShell, then run this script again." -ForegroundColor Red
        pause
        exit 1
    }
}
Write-Host "Git: $(git --version)" -ForegroundColor Green

# --- Clone or update repo ---
if (Test-Path "$ZimiDir\.git") {
    Write-Host "Updating Zimi..." -ForegroundColor Cyan
    git -C $ZimiDir fetch origin
    git -C $ZimiDir checkout $Branch
    git -C $ZimiDir pull origin $Branch
} else {
    Write-Host "Downloading Zimi..." -ForegroundColor Cyan
    if (Test-Path $ZimiDir) { Remove-Item $ZimiDir -Recurse -Force }
    git clone --branch $Branch https://github.com/epheterson/Zimi.git $ZimiDir
}
Write-Host "Zimi installed to: $ZimiDir" -ForegroundColor Green

# --- Python dependencies ---
Write-Host "Installing dependencies..." -ForegroundColor Cyan
python -m pip install --upgrade pip --quiet
python -m pip install libzim "pywebview[winforms]" PyMuPDF certifi --quiet
Write-Host "Dependencies installed" -ForegroundColor Green

# --- Desktop shortcut ---
Write-Host "Creating desktop shortcut..." -ForegroundColor Cyan
$Desktop = [Environment]::GetFolderPath("Desktop")
$ShortcutPath = "$Desktop\Zimi.lnk"
$WScriptShell = New-Object -ComObject WScript.Shell
$Shortcut = $WScriptShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = (Get-Command python).Source
$Shortcut.Arguments = "zimi_desktop.py"
$Shortcut.WorkingDirectory = $ZimiDir
$Shortcut.Description = "Zimi - Offline Knowledge Library"
# Use the icon if it exists
$IconPath = "$ZimiDir\assets\icon.ico"
if (Test-Path $IconPath) {
    $Shortcut.IconLocation = $IconPath
}
$Shortcut.Save()
Write-Host "Desktop shortcut created: Zimi" -ForegroundColor Green

# --- Done ---
Write-Host ""
Write-Host "=== Installation Complete ===" -ForegroundColor Yellow
Write-Host ""
Write-Host "Double-click 'Zimi' on your Desktop to launch." -ForegroundColor White
Write-Host "Your ZIM files go in: $ZimiDir\zims" -ForegroundColor White
Write-Host ""
pause
