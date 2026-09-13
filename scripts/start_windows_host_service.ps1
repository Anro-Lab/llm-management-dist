<#
.SYNOPSIS
    Start Anro LLM System Information Service.

.DESCRIPTION
    This script starts the Anro LLM System Information Service (port 8088).
    Can run in foreground (default) or background mode.

.PARAMETER Port
    The port number for the service (default: 8088)

.PARAMETER HostAddress
    The host address to bind to (default: 127.0.0.1, use 0.0.0.0 for Docker access)

.PARAMETER Background
    Run the service in the background as a PowerShell job (default: false)

.EXAMPLE
    .\start_windows_host_service.ps1

.EXAMPLE
    .\start_windows_host_service.ps1 -Port 8088 -HostAddress 0.0.0.0

.EXAMPLE
    .\start_windows_host_service.ps1 -Background
#>

param(
    [int]$Port = 8088,
    [string]$HostAddress = "127.0.0.1",
    [switch]$Background
)

$ErrorActionPreference = "Stop"

# Get script directory
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$ServiceScript = Join-Path $ScriptDir "windows_host_service.py"

# Check if service script exists
if (-not (Test-Path $ServiceScript)) {
    Write-Error "Service script not found: $ServiceScript"
    exit 1
}

# Find Python executable
$PythonPath = Get-Command python -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source
if (-not $PythonPath) {
    $PythonPath = Get-Command python3 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source
}
if (-not $PythonPath) {
    Write-Error "Python not found. Please install Python 3.10+ and ensure it's in PATH."
    exit 1
}

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "Anro LLM System Information Service" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# Check if required packages are installed
Write-Host "Checking dependencies..." -ForegroundColor Yellow
try {
    $null = & $PythonPath -c "import fastapi, uvicorn" 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Packages missing"
    }
} catch {
    Write-Warning "Required packages may be missing. Installing..."
    & $PythonPath -m pip install fastapi uvicorn --quiet
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed to install required packages"
        exit 1
    }
}

# Check if service is already running
try {
    $Response = Invoke-WebRequest -Uri "http://localhost:${Port}/health" -TimeoutSec 1 -UseBasicParsing -ErrorAction Stop
    Write-Warning "Service is already running on port $Port"
    Write-Host "Service URL: http://localhost:${Port}" -ForegroundColor Green
    exit 0
} catch {
    # Service is not running, continue
}

Write-Host "Starting service on $HostAddress`:$Port..." -ForegroundColor Green
Write-Host ""
Write-Host "Available endpoints:" -ForegroundColor Cyan
Write-Host "  GET http://$HostAddress`:$Port/health - Health check"
Write-Host "  GET http://$HostAddress`:$Port/api/cpu/usage - Get CPU usage"
Write-Host "  GET http://$HostAddress`:$Port/api/gpu - Get GPU information"
Write-Host "  GET http://$HostAddress`:$Port/api/npu - Get NPU information"
Write-Host "  GET http://$HostAddress`:$Port/api/memory - Get memory information"
Write-Host "  GET http://$HostAddress`:$Port/api/memory/usage - Get memory usage"
Write-Host "  GET http://$HostAddress`:$Port/api/storage - Get storage information"
Write-Host "  GET http://$HostAddress`:$Port/api/system-specs - Get full system specifications"
Write-Host ""

if ($Background) {
    Write-Host "Starting service in background..." -ForegroundColor Yellow
    
    # Create a job to run the service
    $Job = Start-Job -ScriptBlock {
        param($PythonPath, $ServiceScript, $ServicePort, $ServiceHost, $ProjectRoot)
        
        try {
            Set-Location $ProjectRoot
            # Redirect both stdout and stderr
            & $PythonPath -u $ServiceScript --port $ServicePort --host $ServiceHost *>&1
        } catch {
            Write-Error "Error in job: $_"
            return $_
        }
    } -ArgumentList $PythonPath, $ServiceScript, $Port, $HostAddress, $ProjectRoot
    
    # Wait a moment for the service to start
    Start-Sleep -Seconds 3
    
    # Check job state and get output if it completed/failed
    $JobState = $Job.State
    if ($JobState -ne "Running") {
        # Job completed immediately - likely an error
        Write-Host "`nJob completed with state: $JobState" -ForegroundColor Red
        Write-Host "Retrieving job output..." -ForegroundColor Yellow
        
        # Get all output from the job
        $JobOutput = Receive-Job -Job $Job -ErrorAction SilentlyContinue 2>&1 | Out-String
        $JobError = $Job.Error | ForEach-Object { $_.Exception.Message } | Out-String
        
        Write-Host "`n=== Job Output ===" -ForegroundColor Red
        if ($JobOutput -and $JobOutput.Trim()) {
            Write-Host $JobOutput -ForegroundColor Red
        } else {
            Write-Host "(No output captured)" -ForegroundColor Gray
        }
        
        if ($JobError -and $JobError.Trim()) {
            Write-Host "`n=== Job Errors ===" -ForegroundColor Red
            Write-Host $JobError -ForegroundColor Red
        }
        
        Write-Error "Failed to start service. Job state: $JobState"
        Remove-Job -Job $Job -ErrorAction SilentlyContinue
        Write-Host "`nTroubleshooting steps:" -ForegroundColor Yellow
        Write-Host "1. Check Python installation: python --version" -ForegroundColor Yellow
        Write-Host "2. Check dependencies: python -c 'import fastapi, uvicorn'" -ForegroundColor Yellow
        Write-Host "3. Try running in foreground to see errors:" -ForegroundColor Yellow
        Write-Host "   .\scripts\start_windows_host_service.ps1 -HostAddress 0.0.0.0" -ForegroundColor Cyan
        Write-Host "4. Check if port 8088 is already in use:" -ForegroundColor Yellow
        Write-Host "   netstat -an | findstr 8088" -ForegroundColor Cyan
        exit 1
    }
    
    # Check if the job is running
    if ($Job.State -eq "Running") {
        Write-Host "Service started successfully!" -ForegroundColor Green
        Write-Host "Job ID: $($Job.Id)" -ForegroundColor Cyan
        Write-Host "Service URL: http://localhost:${Port}" -ForegroundColor Green
        
        # Test the service
        Write-Host "`nTesting service endpoint..." -ForegroundColor Yellow
        Start-Sleep -Seconds 1
        try {
            $Response = Invoke-WebRequest -Uri "http://localhost:${Port}/health" -TimeoutSec 3 -UseBasicParsing
            if ($Response.StatusCode -eq 200) {
                Write-Host "Service is responding correctly!" -ForegroundColor Green
            }
        } catch {
            Write-Warning "Service may still be starting. Please wait a few seconds."
        }
        
        Write-Host "`nTo view service output, run: Receive-Job -Id $($Job.Id)" -ForegroundColor Yellow
        Write-Host "To stop the service, run: .\scripts\stop_windows_host_service.ps1" -ForegroundColor Yellow
        Write-Host "`nNote: The service will stop when you close this PowerShell session." -ForegroundColor Yellow
        Write-Host "For autostart, use: .\scripts\install_windows_host_service.ps1" -ForegroundColor Yellow
    } else {
        Write-Error "Failed to start service. Job state: $($Job.State)"
        $JobOutput = Receive-Job -Job $Job -ErrorAction SilentlyContinue
        if ($JobOutput) {
            Write-Host "Job output:" -ForegroundColor Red
            Write-Host $JobOutput -ForegroundColor Red
        }
        $JobError = Receive-Job -Job $Job -ErrorVariable JobErrors -ErrorAction SilentlyContinue 2>&1
        if ($JobErrors) {
            Write-Host "Job errors:" -ForegroundColor Red
            Write-Host $JobErrors -ForegroundColor Red
        }
        Remove-Job -Job $Job -ErrorAction SilentlyContinue
        Write-Host "`nTroubleshooting steps:" -ForegroundColor Yellow
        Write-Host "1. Check Python installation: python --version" -ForegroundColor Yellow
        Write-Host "2. Check dependencies: python -c 'import fastapi, uvicorn'" -ForegroundColor Yellow
        Write-Host "3. Try running in foreground to see errors: .\scripts\start_windows_host_service.ps1" -ForegroundColor Yellow
        exit 1
    }
} else {
    Write-Host "Note: For Docker containers to access this service, you may need to:" -ForegroundColor Yellow
    Write-Host "  1. Use -HostAddress 0.0.0.0 to allow external access (less secure)" -ForegroundColor Yellow
    Write-Host "  2. Or configure Windows Firewall to allow port $Port" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Press Ctrl+C to stop the service" -ForegroundColor Yellow
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host ""
    
    # Start the service in foreground
    & $PythonPath $ServiceScript --port $Port --host $HostAddress
}
