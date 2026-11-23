"""AML case management endpoints (BRD 10, 15.2)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..deps import Principal, require
from ..models import Case, EvidenceReference
from ..services import cases as svc

router = APIRouter(prefix="/api/v1/cases", tags=["cases"])


def _wrap(fn):
    try:
        return fn()
    except svc.CaseError as exc:
        raise HTTPException(exc.status, {"code": "CASE_ERROR", "message": exc.message}) from exc


def _case(db: Session, p: Principal, case_id: str) -> Case:
    return _wrap(lambda: svc.get_case(db, p, case_id))


@router.get("")
def list_cases(status: str = "", priority: str = "", assignee_id: str = "", search: str = "", open_only: bool = False,
               page: int = 1, page_size: int = Query(50, le=500),
               p: Principal = Depends(require("cases.view_all", "cases.view_assigned")), db: Session = Depends(get_db)):
    q = svc.case_query(p)
    if status:
        q = q.where(Case.status == status)
    if open_only:
        q = q.where(Case.status != "CLOSED")
    if priority:
        q = q.where(Case.priority == priority)
    if assignee_id:
        q = q.where(Case.assignee_id == assignee_id)
    if search:
        like = f"%{search}%"
        q = q.where(Case.id.like(like) | Case.title.like(like))
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar()
    rows = db.execute(q.order_by(Case.updated_at.desc()).offset((page - 1) * page_size).limit(page_size))\
        .scalars().all()
    return {"total": total, "items": [svc.serialize(db, c) for c in rows], "statuses": svc.STATUSES,
            "priorities": svc.PRIORITIES}


@router.post("", status_code=201)
def create_case(body: dict, p: Principal = Depends(require("cases.create")), db: Session = Depends(get_db)):
    case = _wrap(lambda: svc.create_case(db, p, body))
    return svc.serialize(db, case, detail=True, p=p)


@router.get("/{case_id}")
def get_case(case_id: str, p: Principal = Depends(require("cases.view_all", "cases.view_assigned")),
             db: Session = Depends(get_db)):
    case = _case(db, p, case_id)
    data = svc.serialize(db, case, detail=True, p=p)
    data["permissions"] = {
        "assign": p.can("cases.assign") and case.status != "CLOSED",
        "work": case.status not in ("CLOSED", "APPROVED_DISPOSITION") and
        (p.role == "AML_MANAGER" or (p.role == "AML_EMPLOYEE" and case.assignee_id == p.user_id)),
        "recommend": p.role == "AML_EMPLOYEE" and case.assignee_id == p.user_id and
        case.status in ("ASSIGNED", "IN_PROGRESS", "RETURNED_FOR_MORE_INFO"),
        "dispose": p.can("cases.dispose") and case.status not in ("CLOSED", "APPROVED_DISPOSITION"),
        "close": p.can("cases.dispose") and case.status == "APPROVED_DISPOSITION",
        "reopen": p.can("cases.dispose") and case.status == "CLOSED",
        "review": p.can("cases.dispose") and case.status == "RECOMMENDATION_SUBMITTED",
    }
    return data


@router.patch("/{case_id}")
def update_case(case_id: str, body: dict, p: Principal = Depends(require("cases.assign")),
                db: Session = Depends(get_db)):
    case = _case(db, p, case_id)
    _wrap(lambda: svc.update_case(db, p, case, body))
    return svc.serialize(db, case, detail=True, p=p)


@router.post("/{case_id}/assign")
def assign(case_id: str, body: dict, p: Principal = Depends(require("cases.assign")), db: Session = Depends(get_db)):
    case = _case(db, p, case_id)
    _wrap(lambda: svc.assign(db, p, case, body.get("assignee_id", ""), body.get("note", "")))
    return svc.serialize(db, case, detail=True, p=p)


@router.post("/{case_id}/start")
def start(case_id: str, p: Principal = Depends(require("cases.note")), db: Session = Depends(get_db)):
    case = _case(db, p, case_id)
    _wrap(lambda: svc.start(db, p, case))
    return svc.serialize(db, case, detail=True, p=p)


@router.post("/{case_id}/notes", status_code=201)
def add_note(case_id: str, body: dict, p: Principal = Depends(require("cases.note")), db: Session = Depends(get_db)):
    case = _case(db, p, case_id)
    _wrap(lambda: svc.add_note(db, p, case, body.get("body", ""), body.get("kind", "NOTE"),
                               bool(body.get("restricted"))))
    return svc.serialize(db, case, detail=True, p=p)


@router.put("/{case_id}/checklist")
def checklist(case_id: str, body: dict, p: Principal = Depends(require("cases.note")), db: Session = Depends(get_db)):
    case = _case(db, p, case_id)
    _wrap(lambda: svc.update_checklist(db, p, case, body.get("checklist", [])))
    return svc.serialize(db, case, detail=True, p=p)


@router.post("/{case_id}/evidence", status_code=201)
async def add_evidence(case_id: str, file: Optional[UploadFile] = File(None), description: str = Form(""),
                       external_reference: str = Form(""), p: Principal = Depends(require("cases.note")),
                       db: Session = Depends(get_db)):
    case = _case(db, p, case_id)
    content = await file.read(svc.MAX_EVIDENCE_BYTES + 1) if file is not None else b""
    _wrap(lambda: svc.add_evidence(db, p, case, file.filename if file else "", content, description,
                                   external_reference, file.content_type if file else ""))
    return svc.serialize(db, case, detail=True, p=p)


@router.get("/{case_id}/evidence/{evidence_id}")
def download_evidence(case_id: str, evidence_id: str,
                      p: Principal = Depends(require("cases.view_all", "cases.view_assigned")),
                      db: Session = Depends(get_db)):
    case = _case(db, p, case_id)
    ev = db.get(EvidenceReference, evidence_id)
    if ev is None or ev.case_id != case.id:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "evidence not found"})
    path = svc.evidence_path(ev)
    if path is None or not path.exists():
        raise HTTPException(404, {"code": "NO_FILE", "message": "this evidence is an external reference"})
    audit.record(db, p.tenant_id, "case.evidence_download", actor=p, resource_type="case", resource_id=case.id,
                 after={"evidence": ev.id, "file": ev.filename})
    return FileResponse(path, filename=ev.filename, media_type=ev.content_type)


@router.post("/{case_id}/recommendation")
def recommend(case_id: str, body: dict, p: Principal = Depends(require("cases.recommend")),
              db: Session = Depends(get_db)):
    case = _case(db, p, case_id)
    _wrap(lambda: svc.recommend(db, p, case, body.get("value", ""), body.get("rationale", ""),
                                body.get("findings", "")))
    return svc.serialize(db, case, detail=True, p=p)


@router.post("/{case_id}/review")
def review(case_id: str, p: Principal = Depends(require("cases.dispose")), db: Session = Depends(get_db)):
    case = _case(db, p, case_id)
    _wrap(lambda: svc.begin_review(db, p, case))
    return svc.serialize(db, case, detail=True, p=p)


@router.post("/{case_id}/disposition")
def disposition(case_id: str, body: dict, p: Principal = Depends(require("cases.dispose")),
                db: Session = Depends(get_db)):
    case = _case(db, p, case_id)
    res = _wrap(lambda: svc.dispose(db, p, case, body.get("value", ""), body.get("rationale", ""),
                                    bool(body.get("close", True))))
    data = svc.serialize(db, case, detail=True, p=p)
    data["execution"] = res["execution"]
    return data


@router.post("/{case_id}/close")
def close(case_id: str, p: Principal = Depends(require("cases.dispose")), db: Session = Depends(get_db)):
    case = _case(db, p, case_id)
    _wrap(lambda: svc.close(db, p, case))
    return svc.serialize(db, case, detail=True, p=p)


@router.post("/{case_id}/reopen")
def reopen(case_id: str, body: dict, p: Principal = Depends(require("cases.dispose")), db: Session = Depends(get_db)):
    case = _case(db, p, case_id)
    _wrap(lambda: svc.reopen(db, p, case, body.get("reason", "")))
    return svc.serialize(db, case, detail=True, p=p)
