# VeriShield on Windows Server – offline installation

For servers **without internet access**. Everything the server needs (Python, all libraries, the
application) is prepared once on a computer that has internet, carried to the server as one ZIP, and
installed with one script. Client PCs only need a web browser.

```
 PC with internet                         Windows Server (no internet)                Client PCs
 ─────────────────                        ────────────────────────────                ──────────
 build_offline_bundle.py  ── ZIP ──►      INSTALL.cmd / Install-VeriShield.ps1  ◄──── https://SERVER:8443
 (Python installer + Windows wheels)      Python, libraries, service, firewall, HTTPS
```

## Requirements

| | Build PC (internet) | Server (offline) |
|---|---|---|
| OS | Windows, Linux or macOS | Windows Server 2016 / 2019 / 2022 / 2025, 64-bit |
| Software | Python 3.9+ with pip | nothing – Python is installed from the bundle |
| Network | access to pypi.org and python.org (proxy supported) | inbound TCP 8443 from client PCs |
| Disk | ~150 MB | ~1 GB free (2 GB checked) |
| Rights | normal user | local Administrator |

## Step 1 – build the bundle (PC with internet)

From the repository folder (branch `VERISHIELD`):

```powershell
python deploy\windows\build_offline_bundle.py
```

Result: `dist\VeriShield-Windows-Offline-<date>.zip` (≈ 90 MB) with the application, the Python 3.12
Windows installer, all libraries as Windows wheels for Python 3.12 and 3.13, the scripts and `SHA256SUMS.txt`.

Behind a corporate proxy: `set HTTPS_PROXY=http://proxy.bank.local:8080` first. With an internal
PyPI mirror (Artifactory / Nexus): add `--index-url https://mirror/simple`. If python.org is blocked, download
`python-3.12.10-amd64.exe` another way and add `--python-installer C:\path\python-3.12.10-amd64.exe`.

## Step 2 – copy to the server

Copy the ZIP with the bank's approved method (secure file transfer, USB). Optionally verify it: the build
prints the ZIP's SHA-256; on the server run `Get-FileHash .\VeriShield-Windows-Offline-*.zip`.
Extract it, for example to `C:\Install\VeriShield-Windows-Offline-<date>\`.

## Step 3 – install (server, as Administrator)

Double-click **`INSTALL.cmd`** (it asks for Administrator rights), or in an elevated PowerShell:

```powershell
cd C:\Install\VeriShield-Windows-Offline-<date>
powershell -ExecutionPolicy Bypass -File .\Install-VeriShield.ps1
```

The installer (≈ 2–4 minutes) performs 11 steps:

1. checks the server (64-bit, disk, port free; stops a previous version when upgrading)
2. installs the bundled Python **privately** in `C:\VeriShield\python` (no PATH changes, nothing else affected)
3. copies the application to `C:\VeriShield\app`
4. creates `C:\VeriShield\venv` and installs every library **from the bundle** (`pip --no-index`), then verifies them
5. creates `C:\ProgramData\VeriShield\{data,logs,certs,backups}` with restricted permissions
6. writes the configuration `C:\VeriShield\app\.env` with a generated secret key (readable only by SYSTEM / Administrators)
7. creates the HTTPS certificate
8. creates the database and the bootstrap IT Admin **1000**
9. registers the scheduled task `\VeriShield\VeriShield`: starts at boot, runs as SYSTEM, restarts on failure, keep-alive check every 5 minutes
10. opens Windows Firewall TCP 8443 (rule group "VeriShield")
11. starts VeriShield and waits until `/ready` answers

At the end it prints the addresses for client PCs, e.g. `https://VSSRV01:8443`, `https://vssrv01.bop.local:8443`,
`https://10.10.5.20:8443`, and the **one-time password for user 1000** (shown once, never written to disk or to the log).
A summary without passwords is saved in `C:\ProgramData\VeriShield\INSTALL-SUMMARY.txt`.

### Installer options

| Option | Default | Meaning |
|---|---|---|
| `-Mode Production\|Pilot` | Production | Production: MFA required, no development accounts, random bootstrap password. Pilot: also creates 2000 / 3001 / 3002 / 4000 with temporary passwords, MFA optional, bootstrap password `Admin` |
| `-Port 8443` | 8443 | port for browsers and the API |
| `-Https SelfSigned\|Store\|Pfx\|None` | SelfSigned | certificate source (see below) |
| `-CertThumbprint <thumbprint>` | | with `-Https Store`: certificate in `LocalMachine\My` (key must be exportable) |
| `-PfxPath file.pfx -PfxPassword (Read-Host -AsSecureString)` | | with `-Https Pfx` |
| `-HostNames vs.bop.local,vs` | | extra DNS names to put in the self-signed certificate |
| `-AllowedClients "10.10.0.0/16,10.20.5.0/24"` | Any | which client networks may connect (firewall) |
| `-DatabaseUrl "postgresql://user:pass@dbhost:5432/verishield"` | SQLite | use the bank's PostgreSQL instead of the built-in SQLite file |
| `-ServiceAccount "BOP\gmsa_verishield$"` | SYSTEM | run under a (group) managed service account; for a normal account add `-ServicePassword` (needs "Log on as a batch job") |
| `-BootstrapPassword (Read-Host -AsSecureString)` | random | choose the first password of user 1000 |
| `-OpenChannelPorts` | off | also open TCP 9009 / 9010 (Demo live TCP feed / HTTP listener) to other machines |
| `-NoDemoData` | off | do not create the Demo workspace |
| `-DisableMfa` | off | do not require MFA in Production mode |
| `-InstallDir`, `-DataDir` | `C:\VeriShield`, `C:\ProgramData\VeriShield` | locations |
| `-RenewCertificate` | off | regenerate the self-signed certificate |

Examples:

```powershell
# pilot with test accounts, only the AML team's subnet can connect
.\Install-VeriShield.ps1 -Mode Pilot -AllowedClients "10.10.12.0/24"

# production with a certificate issued by the bank's CA and PostgreSQL
.\Install-VeriShield.ps1 -Https Store -CertThumbprint 3F2A9C...  -DatabaseUrl "postgresql://vs:***@pgsrv01:5432/verishield"
```

## Step 4 – open it from any client PC

Browse to `https://<server-name>:8443` and sign in with **1000** and the password printed by the installer;
you will set a new password, your name and e-mail, and (Production) enrol an authenticator app.
Then create the AML Manager, Employees and Chief under **Users & Settings**.

**Certificate warning:** a self-signed certificate is not trusted by browsers until it is distributed.
Either publish `C:\ProgramData\VeriShield\certs\verishield.cer` to client PCs through Group Policy
(*Computer Configuration › Windows Settings › Security Settings › Public Key Policies › Trusted Root
Certification Authorities*), or – recommended – request a server certificate from the bank's CA, import it
into the server's `LocalMachine\My` store and re-run the installer with `-Https Store -CertThumbprint …`.
For a friendly address such as `https://verishield.bop.local:8443`, ask the DNS team for a record that points
to the server and include that name in the certificate.

## Operating it

Run in an elevated PowerShell in `C:\VeriShield`:

```powershell
.\Manage-VeriShield.ps1 Status     # task, process, port and readiness
.\Manage-VeriShield.ps1 Restart
.\Manage-VeriShield.ps1 Logs       # follow C:\ProgramData\VeriShield\logs\verishield.log
.\Manage-VeriShield.ps1 Backup     # consistent online backup of the database, files and settings
.\Manage-VeriShield.ps1 Feed       # stream test transactions into the Demo live TCP channel
```

* **Upgrade:** build a new bundle from the updated code, extract it on the server and run the installer again.
  Data, settings, certificate and secret key are kept.
* **Backups:** `Backup` writes to `C:\ProgramData\VeriShield\backups\<date>` (restricted to Administrators).
  Include that folder in the server backup. The `env.backup` file contains the secret key – without it,
  stored connector credentials and MFA enrolments cannot be decrypted after a restore.
* **Monitoring:** `https://<server>:8443/health` (alive), `/ready` (database reachable), `/metrics` (Prometheus).
* **Uninstall:** `.\Uninstall-VeriShield.ps1` (keeps files and data); add `-RemoveApplication` and/or `-RemoveData`.

## Feeding live transactions from bank systems

* **Decision / events API:** create an API key in *Users & Settings › API clients* (in the workspace that should
  receive the data) and call `POST https://<server>:8443/api/v1/decisions/evaluate` or `/api/v1/transactions/events`.
* **Demo live channels:** the Demo workspace listens on TCP 9009 (JSON lines) and HTTP 9010 (`/transactions`)
  and watches `C:\ProgramData\VeriShield\data\incoming\transactions.csv`. Use `-OpenChannelPorts` if other
  machines must send to 9009 / 9010.
* Other channels (database replica, REST pull, file drop) are configured in *Integrations & Channels*.

## Troubleshooting

| Symptom | Check |
|---|---|
| Installer stops at step 2 | `%TEMP%\Python*.log`; or install `python-3.12.x-amd64.exe` manually ("Install for all users") and re-run |
| Step 4 "No matching distribution" | the bundle was built for other Python versions – rebuild with `--targets 3.12,3.13` |
| "Port 8443 is already used" | run with `-Port 9443` (or another free port) |
| Clients cannot connect | `Manage-VeriShield.ps1 Status`; firewall rule group "VeriShield"; network firewalls between the client VLAN and the server |
| Browser certificate warning | distribute `verishield.cer` or use a CA certificate (see above) |
| Service not running | `C:\ProgramData\VeriShield\logs\verishield.log`; Task Scheduler › VeriShield › History |
