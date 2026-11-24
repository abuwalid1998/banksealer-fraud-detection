"""Sanctions & PEP screening (UN, OFAC, EU, local lists, PEP, internal lists).

Names are normalised (case, accents, punctuation, honorifics, Arabic script transliterated to Latin, common
spelling variants such as Mohammad / Mohammed / Muhammad, Abd al / Abdul) and compared token-by-token, so
Arabic and Latin spellings of the same name meet. Candidate entries are found through a token index, then
scored with a fuzzy similarity (0-1). Matches above the threshold open a ScreeningMatch and an alert; an analyst
marks each one TRUE_MATCH or FALSE_POSITIVE (false positives are remembered and not raised again).

List files are imported offline (the server needs no internet): the OFAC SDN CSV, the UN consolidated XML,
the EU consolidated XML or a simple CSV (name, aliases, type, country, birth_date, identifiers, list).
"""
from __future__ import annotations

import csv
import io
import re
import threading
import unicodedata
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher
from typing import Iterable, Optional

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..models import Alert, Customer, SanctionEntry, ScreeningMatch, utcnow

LIST_CODES = {"UN": "UN Security Council consolidated list", "OFAC": "US OFAC SDN list",
              "EU": "EU consolidated financial sanctions", "LOCAL": "Local / national list",
              "PEP": "Politically exposed persons", "INTERNAL": "Internal blacklist"}

AR_MAP = {"ا": "a", "أ": "a", "إ": "i", "آ": "a", "ب": "b", "ت": "t", "ث": "th", "ج": "j", "ح": "h", "خ": "kh",
          "د": "d", "ذ": "th", "ر": "r", "ز": "z", "س": "s", "ش": "sh", "ص": "s", "ض": "d", "ط": "t", "ظ": "z",
          "ع": "a", "غ": "gh", "ف": "f", "ق": "q", "ك": "k", "ل": "l", "م": "m", "ن": "n", "ه": "h", "ة": "a",
          "و": "w", "ي": "y", "ى": "a", "ء": "", "ئ": "y", "ؤ": "w", "ـ": ""}
VARIANTS = [(r"\b(mohammed|mohamed|muhammad|muhammed|mohamad|mohammad|mohd|mhmd)\b", "mohammad"),
            (r"\b(ahmed|ahmad|ahmd)\b", "ahmad"), (r"\b(yousef|yusuf|youssef|yousif|yusef)\b", "yousef"),
            (r"\b(abdel|abdul|abd al|abd el|abdal|abdol)\s*", "abd "), (r"\b(al|el)[\s\-]+", "al"),
            (r"\b(hussein|husain|hussain|hosein)\b", "hussein"), (r"\b(osama|usama)\b", "osama"),
            (r"\b(khaled|khalid)\b", "khaled"), (r"\b(omar|umar)\b", "omar"), (r"\b(ibrahim|ebrahim)\b", "ibrahim"),
            (r"\b(mahmoud|mahmud|mahmood)\b", "mahmoud"), (r"\b(hasan|hassan)\b", "hassan")]
STOP = {"mr", "mrs", "ms", "dr", "sheikh", "shaikh", "haj", "hajj", "bin", "ibn", "bint", "ben", "the", "of",
        "co", "company", "ltd", "limited", "llc", "inc", "corp", "corporation", "fze", "sa", "srl", "gmbh", "plc",
        "est", "establishment"}

_cache: dict[str, dict] = {}
_lock = threading.Lock()


# --------------------------------------------------------------------------- normalisation & matching
def normalise(name: str) -> str:
    if not name:
        return ""
    text = "".join(AR_MAP.get(ch, ch) for ch in str(name))
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = re.sub(r"[^a-z0-9\s\-']", " ", text).replace("'", "")
    for pattern, repl in VARIANTS:
        text = re.sub(pattern, repl, text)
    text = re.sub(r"[\-\s]+", " ", text)
    text = re.sub(r"(.)\1+", r"\1", text)              # double letters: Haddad / Hadad
    return " ".join(t for t in text.split() if t not in STOP).strip()


def _skeleton(name: str) -> str:
    """Consonant skeleton: Arabic script is written without short vowels (Rahman / Rhmn)."""
    return " ".join(t[0] + re.sub(r"[aeiouy]", "", t[1:]) for t in name.split())


def similarity(a: str, b: str) -> float:
    """0-1 similarity of two normalised names, robust to word order, vowels and missing middle names."""
    if not a or not b:
        return 0.0
    return max(_similarity(a, b), round(0.97 * _similarity(_skeleton(a), _skeleton(b)), 3))


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    ta, tb = a.split(), b.split()
    whole = SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio()
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    best = []
    for t in short:
        best.append(max(SequenceMatcher(None, t, u).ratio() for u in long_))
    tokens = sum(best) / len(best)
    coverage = len(short) / len(long_)
    # every token of the shorter name found well in the longer one -> strong match even if a middle name is missing
    score = max(whole, tokens * (0.85 + 0.15 * coverage))
    if len(short) == 1 and len(long_) > 1:
        score *= 0.75                                  # one-word names are weak evidence
    return round(min(score, 1.0), 3)


def _keys(norm: str) -> set:
    return {t[:3] for t in norm.split() if len(t) >= 3}


def _index(db: Session, tenant_id: str) -> dict:
    with _lock:
        cached = _cache.get(tenant_id)
    version = db.execute(select(func.count(SanctionEntry.id), func.max(SanctionEntry.loaded_at)).where(
        SanctionEntry.tenant_id == tenant_id, SanctionEntry.active.is_(True))).one()
    if cached and cached["version"] == tuple(version):
        return cached
    entries = db.execute(select(SanctionEntry.id, SanctionEntry.list_code, SanctionEntry.name,
                                SanctionEntry.norm_names, SanctionEntry.identifiers, SanctionEntry.entity_type)
                         .where(SanctionEntry.tenant_id == tenant_id, SanctionEntry.active.is_(True))).all()
    by_key: dict[str, set] = {}
    data, ids = {}, {}
    for e in entries:
        data[e.id] = e
        for n in e.norm_names or []:
            for k in _keys(n):
                by_key.setdefault(k, set()).add(e.id)
        for ident in e.identifiers or []:
            ids[re.sub(r"\s+", "", str(ident)).upper()] = e.id
    idx = {"version": tuple(version), "by_key": by_key, "data": data, "ids": ids}
    with _lock:
        _cache[tenant_id] = idx
    return idx


def invalidate(tenant_id: str) -> None:
    with _lock:
        _cache.pop(tenant_id, None)


def screen_name(db: Session, tenant_id: str, name: str, min_score: float = 0.85,
                identifiers: Iterable[str] = ()) -> list[dict]:
    idx = _index(db, tenant_id)
    out: dict[int, dict] = {}
    for ident in identifiers:
        key = re.sub(r"\s+", "", str(ident or "")).upper()
        if key and key in idx["ids"]:
            e = idx["data"][idx["ids"][key]]
            out[e.id] = {"entry_id": e.id, "list_code": e.list_code, "name": e.name, "score": 1.0,
                         "matched_on": "identifier"}
    norm = normalise(name)
    if norm:
        cands = set()
        for k in _keys(norm):
            cands |= idx["by_key"].get(k, set())
        for eid in cands:
            e = idx["data"][eid]
            s = max((similarity(norm, n) for n in e.norm_names or []), default=0.0)
            if s >= min_score and s > out.get(eid, {}).get("score", 0):
                out[eid] = {"entry_id": eid, "list_code": e.list_code, "name": e.name, "score": s,
                            "matched_on": "name"}
    return sorted(out.values(), key=lambda m: -m["score"])


# --------------------------------------------------------------------------- matches & alerts
def _known_false_positive(db: Session, tenant_id: str, subject_ref: str, entry_id: int) -> bool:
    return db.execute(select(ScreeningMatch.id).where(
        ScreeningMatch.tenant_id == tenant_id, ScreeningMatch.subject_ref == subject_ref,
        ScreeningMatch.entry_id == entry_id, ScreeningMatch.status.in_(("FALSE_POSITIVE", "OPEN", "TRUE_MATCH")))
        .limit(1)).scalar() is not None


def record_match(db: Session, tenant_id: str, space: str, subject_type: str, subject_ref: str, subject_name: str,
                 customer_ref: str, m: dict, tx_ref: Optional[str] = None) -> Optional[ScreeningMatch]:
    from .common import space_ref
    if _known_false_positive(db, tenant_id, subject_ref, m["entry_id"]):
        return None
    match = ScreeningMatch(tenant_id=tenant_id, space=space, subject_type=subject_type, subject_ref=subject_ref,
                           subject_name=subject_name, customer_ref=customer_ref, tx_ref=tx_ref,
                           entry_id=m["entry_id"], list_code=m["list_code"], matched_name=m["name"], score=m["score"])
    db.add(match)
    severity = "CRITICAL" if m["list_code"] in ("UN", "OFAC", "EU", "LOCAL") and m["score"] >= 0.95 else \
        "HIGH" if m["list_code"] != "PEP" else "MEDIUM"
    alert = Alert(id=space_ref(db, tenant_id, space, "ALR"), tenant_id=tenant_id, space=space, kind="SCREENING",
                  tx_ref=tx_ref, customer_ref=customer_ref or subject_ref, decision="REVIEW" if not tx_ref else "HOLD",
                  timing="PRE_EVENT" if tx_ref else "POST_EVENT", severity=severity, score=m["score"],
                  aml_signal_level="HIGH" if m["list_code"] != "PEP" else "ELEVATED",
                  reason_codes=["SANCTIONS_MATCH" if m["list_code"] != "PEP" else "PEP_MATCH"],
                  summary=f"{subject_type.title()} '{subject_name}' matches {LIST_CODES.get(m['list_code'], m['list_code'])}"
                          f" entry '{m['name']}' (similarity {m['score']:.2f}, {m['matched_on']})",
                  created_by="screening")
    db.add(alert)
    db.flush()
    match.alert_id = alert.id
    return match


def screen_customers(db: Session, tenant_id: str, space: str, min_score: float = 0.88) -> dict:
    customers = db.execute(select(Customer).where(Customer.tenant_id == tenant_id, Customer.space == space)
                           ).scalars().all()
    created = 0
    for c in customers:
        ids = [x for x in ((c.attributes or {}).get("national_id"), (c.attributes or {}).get("iban")) if x]
        for m in screen_name(db, tenant_id, c.display_name, min_score, ids):
            if record_match(db, tenant_id, space, "CUSTOMER", c.customer_ref, c.display_name, c.customer_ref, m):
                created += 1
    db.commit()
    return {"customers": len(customers), "new_matches": created}


def screen_payment(db: Session, tenant_id: str, space: str, customer_ref: str, tx_ref: str, name: str,
                   identifier: Optional[str], subject_type: str, min_score: float) -> list[ScreeningMatch]:
    out = []
    for m in screen_name(db, tenant_id, name or "", min_score, [identifier] if identifier else []):
        subject = identifier or name
        cleared = db.execute(select(ScreeningMatch.id).where(
            ScreeningMatch.tenant_id == tenant_id, ScreeningMatch.subject_ref == subject,
            ScreeningMatch.entry_id == m["entry_id"], ScreeningMatch.status == "FALSE_POSITIVE").limit(1)).scalar()
        if cleared:
            continue
        rec = record_match(db, tenant_id, space, subject_type, identifier or name, name or identifier or "",
                           customer_ref, m, tx_ref)
        out.append(rec or m)
    return out


# --------------------------------------------------------------------------- list import
def _store(db: Session, tenant_id: str, list_code: str, list_name: str, rows: list[dict], replace: bool) -> int:
    if replace:
        db.execute(delete(SanctionEntry).where(SanctionEntry.tenant_id == tenant_id,
                                               SanctionEntry.list_code == list_code))
    now = utcnow()
    for r in rows:
        names = [r["name"]] + [a for a in r.get("aliases", []) if a]
        db.add(SanctionEntry(tenant_id=tenant_id, list_code=list_code, list_name=list_name,
                             entry_ref=str(r.get("ref", ""))[:80], entity_type=r.get("type", "INDIVIDUAL")[:16],
                             name=r["name"][:300], aliases=r.get("aliases", [])[:30],
                             norm_names=sorted({normalise(n) for n in names if normalise(n)}),
                             country=str(r.get("country", ""))[:64], birth_date=str(r.get("birth_date", ""))[:32],
                             identifiers=r.get("identifiers", [])[:20], remarks=str(r.get("remarks", ""))[:2000],
                             loaded_at=now))
    db.commit()
    invalidate(tenant_id)
    return len(rows)


def parse_csv(content: bytes) -> list[dict]:
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for r in reader:
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
        name = r.get("name") or r.get("full_name") or r.get("sdn_name")
        if not name:
            continue
        rows.append({"name": name, "aliases": [a.strip() for a in re.split(r"[;|]", r.get("aliases", "")) if a.strip()],
                     "type": (r.get("type") or "INDIVIDUAL").upper(), "country": r.get("country", ""),
                     "birth_date": r.get("birth_date", ""), "ref": r.get("ref") or r.get("id", ""),
                     "identifiers": [a.strip() for a in re.split(r"[;|]", r.get("identifiers", "")) if a.strip()],
                     "remarks": r.get("remarks", "")})
    return rows


def parse_ofac_sdn(content: bytes) -> list[dict]:
    """OFAC sdn.csv: ent_num, SDN_Name, SDN_Type, Program, Title, Call_Sign, Vess_type, ..., Remarks (no header)."""
    rows = []
    for rec in csv.reader(io.StringIO(content.decode("latin-1"))):
        if len(rec) < 3 or not rec[0].strip().isdigit():
            continue
        name = rec[1].strip().strip('"')
        if "," in name and rec[2].strip().lower() == "individual":
            last, _, first = name.partition(",")
            name = f"{first.strip()} {last.strip()}"
        rows.append({"name": name, "type": "INDIVIDUAL" if rec[2].strip().lower() == "individual" else "ENTITY",
                     "ref": rec[0].strip(), "remarks": (rec[-1] if len(rec) > 11 else "").strip()[:500],
                     "country": ""})
    return rows


def parse_un_xml(content: bytes) -> list[dict]:
    root = ET.fromstring(content)
    rows = []
    for tag, typ in (("INDIVIDUAL", "INDIVIDUAL"), ("ENTITY", "ENTITY")):
        for el in root.iter(tag):
            parts = [(el.findtext(t) or "").strip() for t in ("FIRST_NAME", "SECOND_NAME", "THIRD_NAME", "FOURTH_NAME")]
            name = " ".join(p for p in parts if p)
            if not name:
                continue
            aliases = [(a.findtext("ALIAS_NAME") or "").strip() for a in el.iter(f"{tag}_ALIAS")]
            rows.append({"name": name, "aliases": [a for a in aliases if a], "type": typ,
                         "ref": (el.findtext("REFERENCE_NUMBER") or "").strip(),
                         "country": (el.findtext("NATIONALITY/VALUE") or "").strip(),
                         "birth_date": (el.findtext("INDIVIDUAL_DATE_OF_BIRTH/DATE") or
                                        el.findtext("INDIVIDUAL_DATE_OF_BIRTH/YEAR") or "").strip(),
                         "remarks": (el.findtext("COMMENTS1") or "").strip()[:500]})
    return rows


def parse_eu_xml(content: bytes) -> list[dict]:
    root = ET.fromstring(content)
    rows = []
    for ent in root.iter():
        if not ent.tag.endswith("sanctionEntity"):
            continue
        names = [a.attrib.get("wholeName", "").strip() for a in ent.iter() if a.tag.endswith("nameAlias")]
        names = [n for n in names if n]
        if not names:
            continue
        subject = next((s.attrib.get("code", "") for s in ent.iter() if s.tag.endswith("subjectType")), "")
        rows.append({"name": names[0], "aliases": names[1:], "ref": ent.attrib.get("euReferenceNumber", ""),
                     "type": "INDIVIDUAL" if subject == "person" else "ENTITY", "country": ""})
    return rows


def import_list(db: Session, tenant_id: str, list_code: str, fmt: str, content: bytes, list_name: str = "",
                replace: bool = True) -> dict:
    if list_code not in LIST_CODES:
        raise ValueError(f"list must be one of {', '.join(LIST_CODES)}")
    parser = {"csv": parse_csv, "ofac_sdn": parse_ofac_sdn, "un_xml": parse_un_xml, "eu_xml": parse_eu_xml}.get(fmt)
    if parser is None:
        raise ValueError("format must be csv, ofac_sdn, un_xml or eu_xml")
    try:
        rows = parser(content)
    except ET.ParseError as exc:
        raise ValueError(f"the file is not valid XML: {exc}") from exc
    if not rows:
        raise ValueError("no names were found in the file - check the format")
    n = _store(db, tenant_id, list_code, list_name or LIST_CODES[list_code], rows, replace)
    return {"list": list_code, "entries": n}


# Clearly fictional entries so Demo Mode can show screening end to end; no real person or entity is listed.
DEMO_LIST = [
    ("UN", "Viktor Draganov", "INDIVIDUAL", ["Victor Draganoff"], "XX", "Fictional demo entry"),
    ("OFAC", "Global Trade FZE", "ENTITY", ["Global Trading FZE"], "AE", "Fictional demo entry"),
    ("OFAC", "Atlas Imports Ltd", "ENTITY", [], "CY", "Fictional demo entry"),
    ("EU", "Nova Services SRL", "ENTITY", [], "RO", "Fictional demo entry"),
    ("EU", "Marat Petrovsky", "INDIVIDUAL", ["M. Petrov"], "RU", "Fictional demo entry"),
    ("LOCAL", "Sunrise Holdings", "ENTITY", ["Sunrise Holding Co"], "XX", "Fictional demo entry"),
    ("INTERNAL", "Darkline Exchange", "ENTITY", [], "XX", "Fictional demo entry - internal blacklist"),
]


def seed_demo(db: Session, tenant_id: str) -> int:
    if db.execute(select(SanctionEntry.id).where(SanctionEntry.tenant_id == tenant_id).limit(1)).scalar():
        return 0
    from mock_channel.generator import PS_FIRST, PS_LAST  # noqa: F401 - PEP names come from demo customers
    rows: dict[str, list] = {}
    for code, name, typ, aliases, country, remark in DEMO_LIST:
        rows.setdefault(code, []).append({"name": name, "type": typ, "aliases": aliases, "country": country,
                                          "remarks": remark})
    # a few demo customers are politically exposed persons (synthetic)
    peps = db.execute(select(Customer.display_name).where(Customer.tenant_id == tenant_id, Customer.space == "DEMO",
                                                          Customer.customer_type == "INDIVIDUAL")
                      .order_by(Customer.customer_ref).limit(60)).scalars().all()[::20]
    rows["PEP"] = [{"name": n, "type": "INDIVIDUAL", "country": "PS", "remarks": "Synthetic PEP (demo)"} for n in peps]
    total = 0
    for code, items in rows.items():
        total += _store(db, tenant_id, code, f"{LIST_CODES[code]} (DEMO - fictional)", items, replace=True)
    return total
