# Automated VPS Deployment Script for AgentAI
# Run in PowerShell as Administrator on your VPS

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  AgentAI - VPS Automated Setup Script  " -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

# 1. Allow Firewall Ports (5500 for Dashboard, 8000 for FastAPI)
Write-Host "`n[1/4] Configuring Windows Firewall..." -ForegroundColor Yellow
try {
    New-NetFirewallRule -DisplayName "AgentAI Dashboard (5500)" -Direction Inbound -LocalPort 5500 -Protocol TCP -Action Allow -ErrorAction SilentlyContinue
    New-NetFirewallRule -DisplayName "AgentAI Backend API (8000)" -Direction Inbound -LocalPort 8000 -Protocol TCP -Action Allow -ErrorAction SilentlyContinue
    Write-Host " Firewall rules created successfully!" -ForegroundColor Green
} catch {
    Write-Host " Failed to set firewall rules automatically. Please ensure ports 5500 and 8000 are allowed." -ForegroundColor Red
}

# 2. Check Python installation
Write-Host "`n[2/4] Checking Python environment..." -ForegroundColor Yellow
$pythonCheck = Get-Command python -ErrorAction SilentlyContinue
if ($null -eq $pythonCheck) {
    Write-Host " Python is not installed or not in PATH! Please download Python 3.11+ from python.org and check 'Add Python to PATH'." -ForegroundColor Red
    Exit
}
Write-Host " Python found: $(python --version)" -ForegroundColor Green

# 3. Install Python Dependencies
Write-Host "`n[3/4] Installing Python requirements..." -ForegroundColor Yellow
cd "$PSScriptRoot\backend"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if ($LASTEXITCODE -eq 0) {
    Write-Host " Python packages installed successfully!" -ForegroundColor Green
} else {
    Write-Host " Error installing dependencies. Please check output." -ForegroundColor Red
}

# 4. Check .env configuration
Write-Host "`n[4/4] Checking backend\.env..." -ForegroundColor Yellow
if (-not (Test-Path "$PSScriptRoot\backend\.env")) {
    Write-Host " backend\.env file not found! Copying from .env.example..." -ForegroundColor Yellow
    Copy-Item "$PSScriptRoot\backend\.env.example" "$PSScriptRoot\backend\.env"
    Write-Host " Please edit backend\.env to insert your GEMINI_API_KEY, GROQ_API_KEY, and Telegram tokens!" -ForegroundColor Red
} else {
    Write-Host " backend\.env is present." -ForegroundColor Green
}

Write-Host "`n========================================" -ForegroundColor Cyan
Write-Host " Setup complete! Next steps:" -ForegroundColor Green
Write-Host " 1. Open MetaTrader 5 on this VPS and login to your broker account." -ForegroundColor White
Write-Host " 2. Double-click 'start_system.bat' to launch AgentAI." -ForegroundColor White
Write-Host " 3. Access Dashboard from your PC browser: http://178.104.154.252:5500" -ForegroundColor White
Write-Host "========================================" -ForegroundColor Cyan
