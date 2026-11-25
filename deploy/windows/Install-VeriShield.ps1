<#
.SYNOPSIS
    Installs (or upgrades) VeriShield on a Windows Server WITHOUT internet access, from the offline bundle.

.DESCRIPTION
    Run as Administrator from the extracted offline bundle folder (or double-click INSTALL.cmd).
    The script:
      1. checks the server (64-bit, disk space, port),
      2. installs the bundled Python 3.12 privately under <InstallDir>\python (no PATH changes),
      3. copies the application to <InstallDir>\app and creates a virtual environment,
      4. installs every library from the bundled Windows wheels (pip --no-index: no internet),
      5. creates the data, log and certificate folders with restricted permissions,
      6. configures HTTPS (self-signed, a PFX file or a certificate from the Windows store),
      7. writes the configuration (.env) with a generated secret key,
      8. initialises the database and the bootstrap IT Admin (username 1000),
      9. registers VeriShield to start automatically with Windows and restart if it stops,
     10. opens the Windows Firewall port for client PCs,
     11. starts the service and checks that it answers.
    Re-running the script upgrades the application and keeps all data, settings and the secret key.

.EXAMPLE
    .\Install-VeriShield.ps1
    Production install on port 8443 with a self-signed HTTPS certificate, reachable from any client.

.EXAMPLE
    .\Install-VeriShield.ps1 -Mode Pilot -AllowedClients "10.10.0.0/16"
    Pilot install: test accounts (2000 / 3001 / 3002 / 4000 / 5000) are created with fixed passwords, MFA optional,
    only clients from 10.10.0.0/16 can connect.

.EXAMPLE
    .\Install-VeriShield.ps1 -Https Store -CertThumbprint 3F2A...C9 -HostNames verishield.bop.local
    Use a certificate issued by the bank's CA that is already in LocalMachine\My.
#>
[CmdletBinding()]
param(
    [string]$InstallDir = "C:\VeriShield",
    [string]$DataDir = "C:\ProgramData\VeriShield",
    [int]$Port = 8443,
    [ValidateSet("Production", "Pilot")]
    [string]$Mode = "Production",
    [ValidateSet("SelfSigned", "Pfx", "Store", "None")]
    [string]$Https = "SelfSigned",
    [string]$PfxPath,
    [SecureString]$PfxPassword,
    [string]$CertThumbprint,
    [string[]]$HostNames = @(),
    [string]$DatabaseUrl,
    [string]$AllowedClients = "Any",
    [switch]$OpenChannelPorts,
    [string]$ServiceAccount,
    [SecureString]$ServicePassword,
    [SecureString]$BootstrapPassword,
    [switch]$NoDemoData,
    [switch]$DisableMfa,
    [switch]$RenewCertificate,
    [string]$TenantCode = "BOP",
    [string]$TenantName = "Bank of Palestine",
    [string]$Currency = "ILS",
    [string]$TimeZoneName = "Asia/Hebron",
    [switch]$SkipStart
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$Bundle = $PSScriptRoot
$TaskName = "VeriShield"
$installOk = $false
$bootstrapPlain = ""
$generated = $false
$TaskPath = "\VeriShield\"
$FirewallGroup = "VeriShield"

function Write-Step([string]$Text) { Write-Host ""; Write-Host "==> $Text" -ForegroundColor Cyan }
function Write-Ok([string]$Text) { Write-Host "    $Text" -ForegroundColor Green }
function Write-Note([string]$Text) { Write-Host "    $Text" -ForegroundColor Yellow }
function Fail([string]$Text) { Write-Host ""; Write-Host "ERROR: $Text" -ForegroundColor Red; throw $Text }

function ConvertTo-Plain([SecureString]$Secure) {
    if ($null -eq $Secure) { return "" }
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

function New-RandomString([int]$Bytes = 48) {
    $buffer = New-Object byte[] $Bytes
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($buffer)
    $rng.Dispose()
    return ([Convert]::ToBase64String($buffer)).Replace("+", "-").Replace("/", "_").TrimEnd("=")
}

function New-ReadablePassword {
    $words = @("Amber", "Cedar", "Coral", "Delta", "Harbor", "Indigo", "Juniper", "Maple", "Olive", "Quartz", "Summit", "Willow")
    $buffer = New-Object byte[] 4
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($buffer)
    $rng.Dispose()
    $a = $words[$buffer[0] % $words.Count]
    $b = $words[$buffer[1] % $words.Count]
    $n = 1000 + (([int]$buffer[2] * 256 + [int]$buffer[3]) % 9000)
    return "$a-$b-$n!"
}

function Invoke-Native([string]$Exe, [string[]]$Arguments, [string]$What) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { Fail "$What failed (exit code $LASTEXITCODE)" }
}

function Get-PythonVersion([string]$Exe) {
    try {
        $v = & $Exe -c "import sys, struct; print('%d.%d %d' % (sys.version_info[0], sys.version_info[1], struct.calcsize('P') * 8))" 2>$null
        if ($LASTEXITCODE -eq 0 -and $v) { return "$v".Trim() }
    } catch { }
    return ""
}

function Find-Python {
    # Candidates: private Python, py launcher, registry, PATH. Must be 64-bit 3.12 or 3.13.
    $candidates = New-Object System.Collections.Generic.List[string]
    $candidates.Add((Join-Path $InstallDir "python\python.exe"))
    foreach ($ver in @("3.12", "3.13")) {
        foreach ($root in @("HKLM:\SOFTWARE\Python\PythonCore\$ver\InstallPath", "HKLM:\SOFTWARE\Python\PythonCore\$ver-64\InstallPath")) {
            if (Test-Path $root) {
                $item = Get-ItemProperty $root -ErrorAction SilentlyContinue
                if ($item.ExecutablePath) { $candidates.Add($item.ExecutablePath) }
                elseif ($item.'(default)') { $candidates.Add((Join-Path $item.'(default)' "python.exe")) }
            }
        }
        $py = Get-Command py.exe -ErrorAction SilentlyContinue
        if ($py) {
            $exe = & $py.Source "-$ver" -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $exe) { $candidates.Add("$exe".Trim()) }
        }
    }
    $onPath = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($onPath) { $candidates.Add($onPath.Source) }
    foreach ($c in $candidates) {
        if (-not $c -or -not (Test-Path $c)) { continue }
        if ($c -like "*WindowsApps*") { continue }
        $info = Get-PythonVersion $c
        if ($info -match "^(3\.1[23]) 64$") { return @{ Exe = $c; Version = $Matches[1] } }
    }
    return $null
}

function Set-RestrictedAcl([string]$Path, [string]$ExtraAccount) {
    # Only SYSTEM, Administrators (and the service account) can read secrets.
    $isFile = Test-Path $Path -PathType Leaf
    $acl = Get-Acl -Path $Path
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($r in @($acl.Access)) { [void]$acl.RemoveAccessRule($r) }
    $inherit = "ContainerInherit,ObjectInherit"
    if ($isFile) { $inherit = "None" }
    foreach ($id in @("NT AUTHORITY\SYSTEM", "BUILTIN\Administrators", $ExtraAccount)) {
        if (-not $id) { continue }
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($id, "FullControl", $inherit, "None", "Allow")
        $acl.AddAccessRule($rule)
    }
    Set-Acl -Path $Path -AclObject $acl
}

function Grant-Modify([string]$Path, [string]$Account) {
    if (-not $Account) { return }
    $acl = Get-Acl $Path
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($Account, "Modify", "ContainerInherit,ObjectInherit", "None", "Allow")
    $acl.AddAccessRule($rule)
    Set-Acl -Path $Path -AclObject $acl
}

function Read-EnvFile([string]$Path) {
    $map = @{}
    if (Test-Path $Path) {
        foreach ($line in Get-Content $Path) {
            if ($line -match "^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$") { $map[$Matches[1]] = $Matches[2].Trim() }
        }
    }
    return $map
}

# ============================================================================ 0. preparation
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail "Run this script as Administrator (right-click PowerShell > Run as administrator, or use INSTALL.cmd)."
}
$LogDir = Join-Path $DataDir "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$transcript = Join-Path $LogDir ("install-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".log")
Start-Transcript -Path $transcript | Out-Null

try {
Write-Host ""
Write-Host "  VeriShield - offline installation for Windows Server" -ForegroundColor White
Write-Host "  Bundle: $Bundle"
Write-Host "  Install folder: $InstallDir   Data folder: $DataDir   Port: $Port   Mode: $Mode   HTTPS: $Https"

# ============================================================================ 1. checks
Write-Step "1/11 Checking the server"
if (-not [Environment]::Is64BitOperatingSystem) { Fail "A 64-bit Windows Server is required." }
if (-not (Test-Path (Join-Path $Bundle "app\verishield")) -or -not (Test-Path (Join-Path $Bundle "wheels"))) {
    Fail "Run this script from the extracted offline bundle folder (it must contain 'app' and 'wheels')."
}
Get-ChildItem -Path $Bundle -Recurse -File | Unblock-File -ErrorAction SilentlyContinue
$drive = (Split-Path -Qualifier $InstallDir)
$disk = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$drive'"
if ($disk -and $disk.FreeSpace -lt 2GB) { Fail "At least 2 GB of free space is needed on $drive." }
$existingTask = Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction SilentlyContinue
if ($existingTask) {
    Write-Note "Existing installation found - upgrading (data, settings and secret key are kept)."
    Stop-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*run_verishield.py*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}
$busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    $owner = (Get-Process -Id ($busy | Select-Object -First 1).OwningProcess -ErrorAction SilentlyContinue).ProcessName
    Fail "Port $Port is already used by '$owner'. Choose another port with -Port."
}
Write-Ok "64-bit OS, disk space and port $Port are OK"

# ============================================================================ 2. python
Write-Step "2/11 Python runtime"
$python = Find-Python
if (-not $python) {
    $installer = Get-ChildItem -Path (Join-Path $Bundle "python") -Filter "python-3.1*-amd64.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $installer) { Fail "No Python 3.12/3.13 found and no installer in the bundle's 'python' folder." }
    Write-Host "    Installing $($installer.Name) privately into $InstallDir\python (no PATH changes)..."
    $target = Join-Path $InstallDir "python"
    $pyArgs = @("/quiet", "InstallAllUsers=1", "TargetDir=`"$target`"", "PrependPath=0", "Include_launcher=0",
              "Include_test=0", "Include_doc=0", "Include_tcltk=0", "Shortcuts=0", "AssociateFiles=0", "Include_pip=1")
    $p = Start-Process -FilePath $installer.FullName -ArgumentList $pyArgs -Wait -PassThru
    if ($p.ExitCode -ne 0 -and $p.ExitCode -ne 3010) { Fail "Python installer failed (exit code $($p.ExitCode)). See %TEMP%\Python*.log" }
    $python = Find-Python
    if (-not $python) { Fail "Python was installed but could not be found. Install Python 3.12 (64-bit) manually and re-run." }
}
$tag = "cp" + $python.Version.Replace(".", "")
$wheels = Join-Path $Bundle "wheels\$tag"
if (-not (Test-Path $wheels)) { Fail "The bundle has no wheels for Python $($python.Version) (expected folder wheels\$tag)." }
Write-Ok "Python $($python.Version) (64-bit): $($python.Exe)"

# ============================================================================ 3. application files
Write-Step "3/11 Copying the application"
$AppDir = Join-Path $InstallDir "app"
New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
& robocopy (Join-Path $Bundle "app") $AppDir /MIR /XF .env /XD data __pycache__ /R:2 /W:2 /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { Fail "Copying the application failed (robocopy exit code $LASTEXITCODE)." }
Copy-Item (Join-Path $Bundle "Manage-VeriShield.ps1") $InstallDir -Force
Copy-Item (Join-Path $Bundle "Uninstall-VeriShield.ps1") $InstallDir -Force
Copy-Item (Join-Path $Bundle "README.md") (Join-Path $InstallDir "README-Windows.md") -Force
Write-Ok "Application copied to $AppDir"

# ============================================================================ 4. virtual environment + libraries
Write-Step "4/11 Installing libraries offline (no internet needed)"
$Venv = Join-Path $InstallDir "venv"
$VenvPy = Join-Path $Venv "Scripts\python.exe"
if (Test-Path $VenvPy) {
    $vinfo = Get-PythonVersion $VenvPy
    if (-not $vinfo.StartsWith($python.Version)) { Write-Note "Recreating the virtual environment for Python $($python.Version)"; Remove-Item $Venv -Recurse -Force }
}
if (-not (Test-Path $VenvPy)) { Invoke-Native $python.Exe @("-m", "venv", $Venv) "Creating the virtual environment" }
Invoke-Native $VenvPy @("-m", "pip", "install", "--no-index", "--find-links", $wheels, "--disable-pip-version-check",
    "--no-warn-script-location", "-r", (Join-Path $Bundle "requirements-windows.txt")) "Installing libraries"
if (Test-Path (Join-Path $Bundle "WITH_AI")) {
    Invoke-Native $VenvPy @("-m", "pip", "install", "--no-index", "--find-links", $wheels, "--disable-pip-version-check",
        "--no-warn-script-location", "-r", (Join-Path $Bundle "app\requirements-ai.txt")) "Installing the optional AI assistant library"
}
Invoke-Native $VenvPy @("-m", "pip", "check", "--disable-pip-version-check") "Verifying libraries"
Invoke-Native $VenvPy @("-c", "import fastapi, uvicorn, sqlalchemy, numpy, reportlab, openpyxl, cryptography, psycopg; print('    libraries import OK')") "Import test"

# ============================================================================ 5. folders + permissions
Write-Step "5/11 Data, log and certificate folders"
$DataPath = Join-Path $DataDir "data"
$CertDir = Join-Path $DataDir "certs"
foreach ($d in @($DataPath, $LogDir, $CertDir, (Join-Path $DataDir "backups"))) { New-Item -ItemType Directory -Force -Path $d | Out-Null }
$svcAcl = $null
if ($ServiceAccount) { $svcAcl = $ServiceAccount }
Grant-Modify $DataDir $svcAcl
Set-RestrictedAcl $CertDir $svcAcl
Write-Ok "Data: $DataPath   Logs: $LogDir   Certificates: $CertDir"

# ============================================================================ 6. configuration
$CertFile = Join-Path $CertDir "cert.pem"
$KeyFile = Join-Path $CertDir "key.pem"
$scheme = "https"
if ($Https -eq "None") { $scheme = "http" }
Write-Step "6/11 Configuration"
$EnvFile = Join-Path $AppDir ".env"
$old = Read-EnvFile $EnvFile
$secret = $old["VERISHIELD_SECRET_KEY"]
if (-not $secret) { $secret = New-RandomString 48; Write-Ok "Generated a new secret key" } else { Write-Ok "Keeping the existing secret key" }
$envName = "production"
$seedUsers = "false"
if ($Mode -eq "Pilot") { $envName = "development"; $seedUsers = "true" }
$mfa = "true"
if ($DisableMfa -or $Mode -eq "Pilot") { $mfa = "false" }
$seedDemo = "true"
if ($NoDemoData) { $seedDemo = "false" }
$lines = @(
    "# VeriShield configuration - written by Install-VeriShield.ps1 on $(Get-Date -Format s)",
    "# Readable only by SYSTEM and Administrators. Re-running the installer keeps the secret key.",
    "VERISHIELD_ENV=$envName",
    "VERISHIELD_HOST=0.0.0.0",
    "VERISHIELD_PORT=$Port",
    "VERISHIELD_DATA_DIR=$DataPath",
    "VERISHIELD_LOG_FILE=$(Join-Path $LogDir 'verishield.log')",
    "VERISHIELD_SECRET_KEY=$secret",
    "VERISHIELD_TENANT_CODE=$TenantCode",
    "VERISHIELD_TENANT_NAME=$TenantName",
    "VERISHIELD_TENANT_CURRENCY=$Currency",
    "VERISHIELD_TENANT_TIMEZONE=$TimeZoneName",
    "VERISHIELD_SEED_DEMO=$seedDemo",
    "VERISHIELD_SEED_DEMO_USERS=$seedUsers",
    "VERISHIELD_SHOW_DEV_ACCOUNTS=false",
    "VERISHIELD_REQUIRE_MFA=$mfa",
    "VERISHIELD_DEMO_TCP_PORT=9009",
    "VERISHIELD_DEMO_HTTP_PORT=9010"
)
if ($scheme -eq "https") { $lines += "VERISHIELD_SSL_CERTFILE=$CertFile"; $lines += "VERISHIELD_SSL_KEYFILE=$KeyFile" }
if ($DatabaseUrl) { $lines += "DATABASE_URL=$DatabaseUrl" }
elseif ($old["DATABASE_URL"]) { $lines += "DATABASE_URL=" + $old["DATABASE_URL"] }
Set-Content -Path $EnvFile -Value $lines -Encoding UTF8
Set-RestrictedAcl $EnvFile $svcAcl
Write-Ok "Configuration written to $EnvFile (secrets protected)"

# ============================================================================ 7. host names + HTTPS
Write-Step "7/11 HTTPS certificate"
$names = New-Object System.Collections.Generic.List[string]
foreach ($n in $HostNames) { if ($n) { $names.Add($n) } }
$names.Add($env:COMPUTERNAME)
try { $fqdn = [System.Net.Dns]::GetHostEntry("").HostName; if ($fqdn) { $names.Add($fqdn) } } catch { }
$ips = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } | Select-Object -ExpandProperty IPAddress
foreach ($ip in $ips) { $names.Add($ip) }
$names.Add("localhost")
$names = @($names | Where-Object { $_ } | Select-Object -Unique)
Push-Location $AppDir
try {
    switch ($Https) {
        "None" { Write-Note "HTTPS disabled - only acceptable for an isolated test network." }
        "SelfSigned" {
            if ((Test-Path $CertFile) -and -not $RenewCertificate) { Write-Ok "Keeping the existing certificate ($CertFile)" }
            else {
                Invoke-Native $VenvPy @("-m", "verishield", "make-cert", "--self-signed", "--hosts", ($names -join ","), "--out", $CertDir) "Creating the certificate"
            }
        }
        "Pfx" {
            if (-not $PfxPath -or -not (Test-Path $PfxPath)) { Fail "-PfxPath must point to the certificate file (.pfx / .p12)." }
            $env:VS_PFX_PASSWORD = ConvertTo-Plain $PfxPassword
            try { Invoke-Native $VenvPy @("-m", "verishield", "make-cert", "--pfx", $PfxPath, "--password-env", "VS_PFX_PASSWORD", "--out", $CertDir) "Converting the PFX certificate" }
            finally { Remove-Item Env:\VS_PFX_PASSWORD -ErrorAction SilentlyContinue }
        }
        "Store" {
            if (-not $CertThumbprint) { Fail "-CertThumbprint is required with -Https Store." }
            $cert = Get-Item ("Cert:\LocalMachine\My\" + $CertThumbprint.Replace(" ", "")) -ErrorAction SilentlyContinue
            if (-not $cert) { Fail "Certificate $CertThumbprint not found in Cert:\LocalMachine\My." }
            if (-not $cert.HasPrivateKey) { Fail "The certificate has no private key." }
            $tmpPfx = Join-Path $CertDir "store-export.pfx"
            $tmpPwdPlain = New-RandomString 24
            $tmpPwd = ConvertTo-SecureString $tmpPwdPlain -AsPlainText -Force
            Export-PfxCertificate -Cert $cert -FilePath $tmpPfx -Password $tmpPwd | Out-Null
            $env:VS_PFX_PASSWORD = $tmpPwdPlain
            try { Invoke-Native $VenvPy @("-m", "verishield", "make-cert", "--pfx", $tmpPfx, "--password-env", "VS_PFX_PASSWORD", "--out", $CertDir) "Converting the certificate" }
            finally { Remove-Item Env:\VS_PFX_PASSWORD -ErrorAction SilentlyContinue; Remove-Item $tmpPfx -Force -ErrorAction SilentlyContinue }
        }
    }
} finally { Pop-Location }
if ($scheme -eq "https") { Set-RestrictedAcl $CertDir $svcAcl; Write-Ok "Certificate: $CertFile" }

# ============================================================================ 8. database
Write-Step "8/11 Initialising the database"
$bootstrapPlain = ConvertTo-Plain $BootstrapPassword
if (-not $bootstrapPlain) {
    if ($Mode -eq "Pilot") { $bootstrapPlain = "Admin" } else { $bootstrapPlain = New-ReadablePassword; $generated = $true }
}
$env:VERISHIELD_BOOTSTRAP_PASSWORD = $bootstrapPlain
$env:VERISHIELD_BACKGROUND_JOBS = "false"
Push-Location $AppDir
try { Invoke-Native $VenvPy @("-m", "verishield", "init") "Database initialisation" }
finally {
    Pop-Location
    Remove-Item Env:\VERISHIELD_BOOTSTRAP_PASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:\VERISHIELD_BACKGROUND_JOBS -ErrorAction SilentlyContinue
}
Write-Ok "Database ready"

# ============================================================================ 9. automatic start
Write-Step "9/11 Registering VeriShield to start with Windows"
$action = New-ScheduledTaskAction -Execute $VenvPy -Argument ("`"" + (Join-Path $AppDir "run_verishield.py") + "`"") -WorkingDirectory $AppDir
$atStartup = New-ScheduledTaskTrigger -AtStartup
$keepAlive = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
$taskSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
if ($existingTask) { Unregister-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -Confirm:$false }
if (-not $ServiceAccount) {
    $taskPrincipal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    Register-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -Action $action -Trigger @($atStartup, $keepAlive) `
        -Settings $taskSettings -Principal $taskPrincipal -Description "VeriShield web application and decision service" | Out-Null
} elseif ($ServiceAccount.EndsWith("$")) {
    $taskPrincipal = New-ScheduledTaskPrincipal -UserId $ServiceAccount -LogonType Password -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -Action $action -Trigger @($atStartup, $keepAlive) `
        -Settings $taskSettings -Principal $taskPrincipal -Description "VeriShield web application and decision service" | Out-Null
} else {
    if (-not $ServicePassword) { $ServicePassword = Read-Host "Password for $ServiceAccount" -AsSecureString }
    Register-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -Action $action -Trigger @($atStartup, $keepAlive) `
        -Settings $taskSettings -User $ServiceAccount -Password (ConvertTo-Plain $ServicePassword) -RunLevel Limited `
        -Description "VeriShield web application and decision service" | Out-Null
}
$runAs = "SYSTEM"
if ($ServiceAccount) { $runAs = $ServiceAccount }
Write-Ok "Scheduled task $TaskPath$TaskName (runs as $runAs, starts at boot, checked every 5 minutes, restarts on failure)"

# ============================================================================ 10. firewall
Write-Step "10/11 Windows Firewall"
Get-NetFirewallRule -Group $FirewallGroup -ErrorAction SilentlyContinue | Remove-NetFirewallRule
$remote = @($AllowedClients.Split(",") | ForEach-Object { $_.Trim() } | Where-Object { $_ })
New-NetFirewallRule -DisplayName "VeriShield web and API (TCP $Port)" -Group $FirewallGroup -Direction Inbound -Protocol TCP `
    -LocalPort $Port -RemoteAddress $remote -Action Allow -Profile Any | Out-Null
if ($OpenChannelPorts) {
    New-NetFirewallRule -DisplayName "VeriShield data channels (TCP 9009-9010)" -Group $FirewallGroup -Direction Inbound -Protocol TCP `
        -LocalPort @(9009, 9010) -RemoteAddress $remote -Action Allow -Profile Any | Out-Null
}
Write-Ok "Inbound TCP $Port allowed from: $($remote -join ', ')"
if ($OpenChannelPorts) { Write-Ok "Inbound TCP 9009 (TCP feed) and 9010 (HTTP listener) allowed" }

# ============================================================================ 11. start + health check
$healthy = $false
if (-not $SkipStart) {
    Write-Step "11/11 Starting VeriShield"
    Start-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath
    if ($scheme -eq "https") {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        [Net.ServicePointManager]::ServerCertificateValidationCallback = { $true }   # local health check only
    }
    $url = "${scheme}://localhost:$Port/ready"
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Seconds 3
        try {
            $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 5
            if ($r.StatusCode -eq 200) { $healthy = $true; break }
        } catch { }
        Write-Host "    waiting for the service... ($(($i + 1) * 3) s)"
    }
    [Net.ServicePointManager]::ServerCertificateValidationCallback = $null
    if ($healthy) { Write-Ok "VeriShield is running and ready" }
    else { Write-Note "VeriShield did not answer yet. Check $LogDir\verishield.log and run: .\Manage-VeriShield.ps1 Status" }
}

# ============================================================================ summary
$urls = @($names | Where-Object { $_ -ne "localhost" } | ForEach-Object { "${scheme}://$($_):$Port" })
$summary = New-Object System.Collections.Generic.List[string]
$summary.Add("VeriShield installed $(Get-Date -Format s) - mode $Mode")
$summary.Add("")
$summary.Add("Open from any client PC:")
foreach ($u in $urls) { $summary.Add("    $u") }
$summary.AddRange([string[]]@(
    "API documentation: ${scheme}://$($env:COMPUTERNAME):$Port/api/docs",
    "",
    "First sign-in: username 1000 (IT Admin). The password must be changed at first sign-in.",
    "",
    "Install folder:  $InstallDir",
    "Data folder:     $DataPath",
    "Log file:        $(Join-Path $LogDir 'verishield.log')",
    "Configuration:   $EnvFile",
    "Manage:          $InstallDir\Manage-VeriShield.ps1  Status | Start | Stop | Restart | Logs | Health | Backup"
))
if ($scheme -eq "https" -and $Https -eq "SelfSigned") {
    $summary.Add("")
    $summary.Add("Self-signed certificate: browsers show a warning until clients trust it.")
    $summary.Add("  Distribute $CertDir\verishield.cer to client PCs (Group Policy > Trusted Root Certification Authorities),")
    $summary.Add("  or install a certificate from the bank's CA: re-run with -Https Store -CertThumbprint <thumbprint>.")
}
if ($Mode -eq "Pilot") {
    $summary.Add("")
    $summary.Add("Pilot test accounts (passwords stay valid): 1000 Admin, 2000 Manager#2026, 3001 Analyst1#2026, 3002 Analyst2#2026, 4000 Chief#2026, 5000 Ceo#2026 (CEO)")
    $summary.Add("Demo channels are stopped until the IT Admin starts them (Integrations & Channels).")
}
Set-Content -Path (Join-Path $DataDir "INSTALL-SUMMARY.txt") -Value $summary -Encoding UTF8
Write-Host ""
Write-Host "==================================================================================" -ForegroundColor Green
$summary | ForEach-Object { Write-Host "  $_" }
Write-Host "==================================================================================" -ForegroundColor Green
Write-Host "  Installation log: $transcript"
$installOk = $true
}
finally {
    Stop-Transcript | Out-Null
}
# Printed after the transcript stops, so the password is never written to the installation log.
if ($installOk -and $bootstrapPlain) {
    Write-Host ""
    if ($generated) {
        Write-Host "  One-time bootstrap password for user 1000:  $bootstrapPlain" -ForegroundColor Yellow
        Write-Host "  (shown only now and not stored anywhere - write it down; it must be changed at first sign-in)" -ForegroundColor Yellow
    } elseif ($Mode -eq "Pilot") {
        Write-Host "  Bootstrap password for user 1000: Admin (must be changed at first sign-in)" -ForegroundColor Yellow
    } else {
        Write-Host "  Bootstrap password for user 1000: the one you entered (must be changed at first sign-in)" -ForegroundColor Yellow
    }
    Write-Host "  Note: the bootstrap password is only used the first time the database is created." -ForegroundColor Yellow
}
