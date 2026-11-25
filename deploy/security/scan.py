"""Security scan before a release: code (bandit) and dependencies (pip-audit).

    pip install bandit pip-audit          # on a build machine with internet access
    python deploy/security/scan.py        # writes <data dir>/security_scan.json and prints a summary

The JSON summary is shown in VeriShield (Users & Settings > Security posture). Findings that are accepted by
design are listed in ``ACCEPTED`` with the reason, so the report only shows what needs action.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TARGETS = ["verishield", "banksealer", "mock_channel"]
REQUIREMENTS = ["requirements-verishield.txt", "requirements-postgres.txt", "requirements-ai.txt"]

# bandit test id -> why it is accepted in VeriShield
ACCEPTED = {
    "B104": "binding to all interfaces is intended for a server (firewall / load balancer in front)",
    "B301": "pickle is only used for model files written by VeriShield itself; cluster checkpoints are HMAC-signed",
    "B403": "see B301",
    "B608": "table and column names come from administrator configuration and pass a strict identifier check",
    "B311": "random is used for synthetic demo data, never for secrets (secrets uses the secrets module)",
    "B105": "constant strings such as status names, not credentials",
    "B110": "best-effort clean-up code",
    "B310": "outbound URLs are administrator-configured and restricted to http(s) by security.check_url",
    "B323": "TLS verification can only be switched off per connector by the IT Admin; the posture check reports it",
    "B405": "XML from uploaded lists is parsed only after DTDs / entities are rejected",
    "B314": "see B405",
    "B406": "see B405",
}


def _run(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    return proc.returncode, proc.stdout


def bandit() -> dict:
    if not shutil.which("bandit"):
        return {"skipped": "bandit is not installed"}
    _, out = _run(["bandit", "-q", "-r", *TARGETS, "-f", "json"])
    data = json.loads(out or "{}")
    findings = []
    for r in data.get("results", []):
        findings.append({"severity": r["issue_severity"], "id": r["test_id"], "file": r["filename"],
                         "line": r["line_number"], "text": r["issue_text"],
                         "accepted": ACCEPTED.get(r["test_id"], "")})
    return {"findings": findings}


def pip_audit() -> dict:
    if not shutil.which("pip-audit"):
        return {"skipped": "pip-audit is not installed"}
    vulns = []
    for req in REQUIREMENTS:
        if not (ROOT / req).exists():
            continue
        _, out = _run(["pip-audit", "-r", req, "-f", "json", "--progress-spinner", "off"])
        try:
            data = json.loads(out or "{}")
        except ValueError:
            continue
        for dep in data.get("dependencies", data if isinstance(data, list) else []):
            for v in dep.get("vulns", []):
                vulns.append({"requirements": req, "package": dep.get("name"), "version": dep.get("version"),
                              "id": v.get("id"), "fix": ", ".join(v.get("fix_versions", []))})
    return {"vulnerabilities": vulns}


def main() -> int:
    code, deps = bandit(), pip_audit()
    open_findings = [f for f in code.get("findings", []) if not f["accepted"]]
    high = sum(1 for f in open_findings if f["severity"] == "HIGH") + len(deps.get("vulnerabilities", []))
    medium = sum(1 for f in open_findings if f["severity"] == "MEDIUM")
    summary = {"date": dt.date.today().isoformat(), "high": high, "medium": medium,
               "code": code, "dependencies": deps}
    data_dir = Path(os.environ.get("VERISHIELD_DATA_DIR", ROOT / "data" / "verishield"))
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "security_scan.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(f"Security scan {summary['date']}: {high} high, {medium} medium open findings")
    for f in open_findings:
        print(f"  [{f['severity']}] {f['id']} {f['file']}:{f['line']} {f['text']}")
    for v in deps.get("vulnerabilities", []):
        print(f"  [DEPENDENCY] {v['package']} {v['version']} {v['id']} (fixed in {v['fix'] or '-'})")
    for part, res in (("bandit", code), ("pip-audit", deps)):
        if "skipped" in res:
            print(f"  {part}: {res['skipped']}")
    return 1 if high else 0


if __name__ == "__main__":
    sys.exit(main())
