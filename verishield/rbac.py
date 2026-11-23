"""Role-based access control (BRD section 6). Enforced on every backend endpoint."""
from __future__ import annotations

ALL_PERMISSIONS = {
    # technology
    "users.manage": "Create, disable, unlock and reset users",
    "connectors.manage": "Configure connections, schedules, retries and secrets",
    "connectors.view": "View connector configuration and health",
    "system.view": "View system health, logs and channel failures",
    "system.summary": "View executive/AML-impacting system health summary",
    "tenant.manage": "Institution identity, security policy and tenant-wide branding",
    "api_clients.manage": "Manage system-to-system API credentials",
    "demo.manage": "Enable/disable and reset Demo Mode",
    "audit.view": "View the audit trail",
    "models.train": "Run data imports and model training",
    "models.activate": "Promote / roll back model versions",
    "models.view": "View model status, segments and performance",
    # data
    "tx.view_full": "View transactions in full AML scope",
    "tx.view_masked": "View technical transaction metadata (masked)",
    "tx.view_assigned": "View transactions linked to assigned cases",
    "customers.view_full": "View customer profiles in full AML scope",
    "customers.view_masked": "View masked customer support view",
    "customers.view_assigned": "View customers linked to assigned cases",
    "decisions.test": "Send test transactions to the decision API from the UI",
    # AML
    "alerts.view_all": "View all AML alerts",
    "alerts.triage": "Triage / close alerts",
    "cases.view_all": "View all AML cases",
    "cases.view_assigned": "View assigned cases",
    "cases.create": "Create cases and referrals",
    "cases.assign": "Assign / reassign cases",
    "cases.note": "Add notes and evidence",
    "cases.recommend": "Submit investigation recommendation",
    "cases.dispose": "Record final Manager disposition",
    "policies.view": "View thresholds and policies",
    "policies.edit": "Edit thresholds/policies, simulate and publish",
    "policies.approve": "Approve policy changes (maker-checker)",
    "branding.reports": "Configure AML report header/footer wording",
    # reports
    "reports.system": "Generate system performance/failure reports",
    "reports.aml": "Generate AML, transaction and customer reports",
    "reports.assigned": "Generate assigned-case reports",
    "reports.executive": "Generate executive/compliance reports",
}

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "IT_ADMIN": {
        "users.manage", "connectors.manage", "connectors.view", "system.view", "system.summary", "tenant.manage",
        "api_clients.manage", "demo.manage", "audit.view", "models.train", "models.activate", "models.view",
        "tx.view_masked", "customers.view_masked", "decisions.test", "policies.view", "reports.system",
    },
    "AML_MANAGER": {
        "system.summary", "models.view", "models.activate", "tx.view_full", "customers.view_full", "decisions.test",
        "alerts.view_all", "alerts.triage", "cases.view_all", "cases.create", "cases.assign", "cases.note",
        "cases.dispose", "policies.view", "policies.edit", "branding.reports", "reports.aml",
        "reports.assigned",
    },
    "AML_EMPLOYEE": {
        "tx.view_assigned", "customers.view_assigned", "cases.view_assigned", "cases.note", "cases.recommend",
        "reports.assigned",
    },
    "CHIEF_AML": {
        "system.summary", "connectors.view", "models.view", "tx.view_full", "customers.view_full",
        "alerts.view_all", "cases.view_all", "policies.view", "audit.view", "reports.aml", "reports.executive",
        "reports.assigned",
    },
}

# Permissions an IT Admin may additionally grant to a Chief (explicit, audited).
GRANTABLE = {"CHIEF_AML": {"policies.approve", "cases.dispose"}}


def permissions_for(role: str, extra: list | None = None) -> set[str]:
    perms = set(ROLE_PERMISSIONS.get(role, set()))
    allowed_extra = GRANTABLE.get(role, set())
    perms |= {p for p in (extra or []) if p in allowed_extra}
    return perms


# Navigation (BRD 17.4) and the permission that unlocks each workspace.
NAVIGATION = [
    ("overview", "Overview", None),
    ("transactions", "Transactions", ("tx.view_full", "tx.view_masked", "tx.view_assigned")),
    ("customers", "Customers", ("customers.view_full", "customers.view_masked", "customers.view_assigned")),
    ("alerts", "Alerts", ("alerts.view_all",)),
    ("cases", "AML Cases", ("cases.view_all", "cases.view_assigned")),
    ("decision", "Decision Console", ("decisions.test",)),
    ("reports", "Reports", ("reports.system", "reports.aml", "reports.assigned", "reports.executive")),
    ("models", "Models & Segments", ("models.view",)),
    ("integrations", "Integrations & Channels", ("connectors.view", "connectors.manage")),
    ("policies", "Policies & Thresholds", ("policies.view",)),
    ("operations", "Operations & Audit", ("system.view", "audit.view")),
    ("settings", "Users & Settings", ("users.manage", "tenant.manage")),
]


def navigation_for(perms: set[str]) -> list[dict]:
    return [{"id": key, "label": label} for key, label, need in NAVIGATION if need is None or perms & set(need)]
