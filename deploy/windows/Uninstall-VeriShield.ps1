<#
.SYNOPSIS
    Removes the VeriShield service registration and firewall rules. Files and data are kept unless asked.

.EXAMPLE
    .\Uninstall-VeriShield.ps1                          # stop and unregister, keep files and data
    .\Uninstall-VeriShield.ps1 -RemoveApplication       # also delete C:\VeriShield (application, Python, venv)
    .\Uninstall-VeriShield.ps1 -RemoveApplication -RemoveData   # also delete all data (asks for confirmation)
#>
[CmdletBinding()]
param(
    [string]$InstallDir = "C:\VeriShield",
    [string]$DataDir = "C:\ProgramData\VeriShield",
    [switch]$RemoveApplication,
    [switch]$RemoveData
)
$ErrorActionPreference = "Stop"
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw "Run as Administrator." }

Stop-ScheduledTask -TaskName "VeriShield" -TaskPath "\VeriShield\" -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*run_verishield.py*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Unregister-ScheduledTask -TaskName "VeriShield" -TaskPath "\VeriShield\" -Confirm:$false -ErrorAction SilentlyContinue
Get-NetFirewallRule -Group "VeriShield" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
Write-Host "Service registration and firewall rules removed."

if ($RemoveApplication -and (Test-Path $InstallDir)) {
    Remove-Item $InstallDir -Recurse -Force
    Write-Host "Removed $InstallDir"
}
if ($RemoveData -and (Test-Path $DataDir)) {
    $answer = Read-Host "Type DELETE to permanently remove all VeriShield data in $DataDir (database, evidence, reports, backups)"
    if ($answer -eq "DELETE") { Remove-Item $DataDir -Recurse -Force; Write-Host "Removed $DataDir" }
    else { Write-Host "Data kept." }
} else {
    Write-Host "Data kept in $DataDir"
}
