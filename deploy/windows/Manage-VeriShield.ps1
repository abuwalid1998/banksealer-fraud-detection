<#
.SYNOPSIS
    Day-to-day management of VeriShield on Windows Server.

.EXAMPLE
    .\Manage-VeriShield.ps1 Status
    .\Manage-VeriShield.ps1 Restart
    .\Manage-VeriShield.ps1 Logs
    .\Manage-VeriShield.ps1 Backup
    .\Manage-VeriShield.ps1 Feed        # send synthetic live transactions to the Demo TCP channel (testing)
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("Status", "Start", "Stop", "Restart", "Logs", "Health", "Backup", "Feed")]
    [string]$Action = "Status",
    [string]$InstallDir = "C:\VeriShield",
    [string]$DataDir = "C:\ProgramData\VeriShield"
)
$ErrorActionPreference = "Stop"
$TaskName = "VeriShield"
$TaskPath = "\VeriShield\"
$AppDir = Join-Path $InstallDir "app"
$VenvPy = Join-Path $InstallDir "venv\Scripts\python.exe"
$LogFile = Join-Path $DataDir "logs\verishield.log"

function Read-EnvFile([string]$Path) {
    $map = @{}
    if (Test-Path $Path) {
        foreach ($line in Get-Content $Path) {
            if ($line -match "^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$") { $map[$Matches[1]] = $Matches[2].Trim() }
        }
    }
    return $map
}
$cfg = Read-EnvFile (Join-Path $AppDir ".env")
$Port = 8443
if ($cfg["VERISHIELD_PORT"]) { $Port = [int]$cfg["VERISHIELD_PORT"] }
$Scheme = "http"
if ($cfg["VERISHIELD_SSL_CERTFILE"]) { $Scheme = "https" }

function Get-AppProcesses {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*run_verishield.py*" }
}

function Test-Ready {
    if ($Scheme -eq "https") {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        [Net.ServicePointManager]::ServerCertificateValidationCallback = { $true }   # local check only
    }
    try {
        $r = Invoke-WebRequest -Uri "${Scheme}://localhost:$Port/ready" -UseBasicParsing -TimeoutSec 5
        return ($r.StatusCode -eq 200)
    } catch { return $false }
    finally { [Net.ServicePointManager]::ServerCertificateValidationCallback = $null }
}

function Stop-App {
    Stop-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    # the venv launcher starts a child interpreter: stop both
    Get-AppProcesses | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Write-Host "VeriShield stopped. (It is started again automatically within 5 minutes unless the task is disabled:"
    Write-Host "  Disable-ScheduledTask -TaskPath '$TaskPath' -TaskName $TaskName )"
}

function Start-App {
    Start-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Seconds 3
        if (Test-Ready) { Write-Host "VeriShield is running: ${Scheme}://$($env:COMPUTERNAME):$Port" -ForegroundColor Green; return }
    }
    Write-Host "VeriShield did not answer within 2 minutes - see $LogFile" -ForegroundColor Yellow
}

switch ($Action) {
    "Status" {
        $task = Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction SilentlyContinue
        if (-not $task) { Write-Host "VeriShield is not installed (no scheduled task)." -ForegroundColor Yellow; break }
        $info = Get-ScheduledTaskInfo -TaskName $TaskName -TaskPath $TaskPath
        $procs = @(Get-AppProcesses)
        $listening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
        Write-Host "Task state:      $($task.State)  (last run $($info.LastRunTime), result $($info.LastTaskResult))"
        Write-Host "Processes:       $($procs.Count)"
        Write-Host "Listening:       $(if ($listening) { 'yes, port ' + $Port } else { 'no' })"
        $ready = Test-Ready
        Write-Host "Ready:           $ready" -ForegroundColor $(if ($ready) { "Green" } else { "Red" })
        Write-Host "URL:             ${Scheme}://$($env:COMPUTERNAME):$Port"
        Write-Host "Database:        $(if ($cfg['DATABASE_URL']) { 'PostgreSQL (DATABASE_URL)' } else { 'SQLite in ' + $cfg['VERISHIELD_DATA_DIR'] })"
        Write-Host "Log file:        $LogFile"
    }
    "Start" { Start-App }
    "Stop" { Stop-App }
    "Restart" { Stop-App; Start-App }
    "Logs" {
        if (-not (Test-Path $LogFile)) { Write-Host "No log file yet: $LogFile"; break }
        Get-Content -Path $LogFile -Tail 200 -Wait
    }
    "Health" {
        $ready = Test-Ready
        Write-Host "${Scheme}://localhost:$Port/ready -> $ready"
        if (-not $ready) { exit 1 }
    }
    "Backup" {
        $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
        $dest = Join-Path $DataDir "backups\$stamp"
        New-Item -ItemType Directory -Force -Path $dest | Out-Null
        $dataPath = $cfg["VERISHIELD_DATA_DIR"]
        if ($cfg["DATABASE_URL"]) {
            Write-Host "PostgreSQL is configured: back up the database with pg_dump (or the bank's DB backup); files are copied below." -ForegroundColor Yellow
        } else {
            $db = Join-Path $dataPath "verishield.db"
            $code = "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()"
            & $VenvPy -c $code $db (Join-Path $dest "verishield.db")
            if ($LASTEXITCODE -ne 0) { throw "SQLite online backup failed" }
            Write-Host "Database copied (online, consistent backup)."
        }
        foreach ($sub in @("models", "evidence", "reports", "assets", "incoming")) {
            $src = Join-Path $dataPath $sub
            if (Test-Path $src) { & robocopy $src (Join-Path $dest $sub) /E /R:1 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null }
        }
        Copy-Item (Join-Path $AppDir ".env") (Join-Path $dest "env.backup") -Force   # holds the secret key (needed to restore)
        $acl = Get-Acl $dest
        $acl.SetAccessRuleProtection($true, $false)
        foreach ($r in @($acl.Access)) { [void]$acl.RemoveAccessRule($r) }
        foreach ($id in @("NT AUTHORITY\SYSTEM", "BUILTIN\Administrators")) {
            $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($id, "FullControl", "ContainerInherit,ObjectInherit", "None", "Allow")))
        }
        Set-Acl $dest $acl
        Write-Host "Backup written to $dest (restricted to Administrators)." -ForegroundColor Green
    }
    "Feed" {
        Write-Host "Streaming synthetic live transactions to the Demo TCP channel on port 9009 (Ctrl+C to stop)..."
        Write-Host "The 'TCP JSON-lines feed' channel must be started first (Integrations & Channels > Start streaming)." -ForegroundColor Yellow
        Push-Location $AppDir
        try { & $VenvPy (Join-Path $AppDir "mock_channel\generate_transactions.py") --verishield --output tcp --port 9009 }
        finally { Pop-Location }
    }
}
