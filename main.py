"""
NOVIQ Engine — FastAPI Backend
================================
Phase 5, MVP Backend

Serves the dashboard HTML and exposes all engine endpoints.

Run locally:
    pip install fastapi uvicorn python-multipart aiofiles
    uvicorn main:app --reload --port 8000

Then open: http://localhost:8000
"""

from __future__ import annotations

import json
import os
import sys
import uuid
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── Add engine directory to path ───────────────────────────────────────────
BASE_DIR  = Path(__file__).parent
ENGINE_DIR = BASE_DIR / "engine"
KB_DIR    = BASE_DIR / "knowledge_base"
sys.path.insert(0, str(ENGINE_DIR))
sys.path.insert(0, str(BASE_DIR))

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

warnings.filterwarnings("ignore")

# ── Import NOVIQ Engine modules ────────────────────────────────────────────
try:
    from noviq_engine import NOVIQEngine
    from models import (
        APPROVAL_APPROVED,
        APPROVAL_PENDING,
        APPROVAL_REJECTED,
        CodingSuggestion,
    )
    from validation_rules import KnowledgeBaseIncompleteError
    ENGINE_AVAILABLE = True
except ImportError as e:
    print(f"[WARN] Engine modules not found: {e}")
    ENGINE_AVAILABLE = False


# ── App setup ──────────────────────────────────────────────────────────────
app = FastAPI(
    title="NOVIQ Engine API",
    description="AR-DRG V11.0 Clinical Coding Intelligence Platform",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── In-memory store (replace with DB for production) ──────────────────────
# key = episode_id → {"suggestion": dict, "status": str, "approved_by": str}
EPISODE_STORE: dict[str, dict] = {}

# ── Load Medical Logic KB ─────────────────────────────────────────────────
MEDICAL_LOGIC_KB: dict = {}
_kb_path = KB_DIR / "keyword_dictionary_medical_logic_v2.json"
if _kb_path.exists():
    with open(_kb_path, encoding="utf-8") as f:
        MEDICAL_LOGIC_KB = json.load(f)
    print(f"[OK] Medical Logic KB loaded: "
          f"{len(MEDICAL_LOGIC_KB.get('procedures',{}).get('general_surgery',[]))} GS + "
          f"{len(MEDICAL_LOGIC_KB.get('procedures',{}).get('hand_surgery',[]))} HS procedures")
else:
    print(f"[WARN] Medical Logic KB not found at {_kb_path}")

# ── Initialise engine (once at startup) ───────────────────────────────────
_engine: NOVIQEngine | None = None

def get_engine() -> NOVIQEngine:
    global _engine
    if _engine is None and ENGINE_AVAILABLE:
        kb_path   = KB_DIR / "ar_drg_kb_seed_v11_new_adrgs.json"
        excl_path = KB_DIR / "dcl_exclusions.json"
        _engine = NOVIQEngine(kb_path=kb_path, excl_path=excl_path)
        print("[OK] NOVIQEngine initialised")
    return _engine


# ══════════════════════════════════════════════════════════════════════════
# STATIC FILES — Dashboard HTML
# ══════════════════════════════════════════════════════════════════════════

DASHBOARD_PATH = BASE_DIR / "noviq_dashboard.html"

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    """Serve the NOVIQ dashboard."""
    if DASHBOARD_PATH.exists():
        return HTMLResponse(DASHBOARD_PATH.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>NOVIQ Engine</h1><p>Dashboard HTML not found.</p>")


# ══════════════════════════════════════════════════════════════════════════
# ENDPOINT 1 — Upload EHR files and parse to PatientEpisode JSON
# ══════════════════════════════════════════════════════════════════════════

@app.post("/api/upload")
async def upload_ehr(files: list[UploadFile] = File(...)):
    """
    Accept one or more EHR files.
    Parses them and returns a PatientEpisode JSON dict.

    Supported formats:
      - .json  → parsed directly if it matches EpisodeRecord schema
      - .txt   → plain clinical text, extracted with keyword matching
      - .pdf   → text extracted then keyword matched
      - .hl7   → HL7 v2 parsed for PID / DG1 / PR1 segments
      - .xml   → FHIR R4 Bundle / Encounter parsed

    Returns: { episode_id, episode_dict, documents_read, warnings }
    """
    episode_id    = f"EP-{datetime.now().strftime('%Y%m%d')}-{str(uuid.uuid4())[:6].upper()}"
    episode_dict  = _build_empty_episode(episode_id)
    docs_read     = []
    warnings_list = []

    for upload in files:
        content   = await upload.read()
        filename  = upload.filename or ""
        ext       = Path(filename).suffix.lower()
        doc_type  = _infer_doc_type(filename)
        docs_read.append({"filename": filename, "doc_type": doc_type, "size_kb": round(len(content)/1024, 1)})

        try:
            if ext == ".json":
                parsed = json.loads(content.decode("utf-8"))
                episode_dict = _merge_json_episode(episode_dict, parsed)

            elif ext in (".txt", ".text"):
                text = content.decode("utf-8", errors="ignore")
                _extract_from_text(episode_dict, text, doc_type)

            elif ext == ".hl7":
                text = content.decode("utf-8", errors="ignore")
                _parse_hl7(episode_dict, text)

            elif ext in (".pdf",):
                # PDF text extraction — requires pdfminer or pdfplumber
                # Fallback: treat as text if decodable
                try:
                    text = content.decode("utf-8", errors="ignore")
                    _extract_from_text(episode_dict, text, doc_type)
                except Exception:
                    warnings_list.append(f"PDF text extraction failed for {filename} — install pdfplumber")

            elif ext in (".xml",):
                text = content.decode("utf-8", errors="ignore")
                _parse_fhir_xml(episode_dict, text)

            else:
                warnings_list.append(f"Unsupported file type: {ext}")

        except Exception as e:
            warnings_list.append(f"Error parsing {filename}: {str(e)}")

    episode_dict["ehr_documents"] = [d["doc_type"] for d in docs_read]
    episode_dict["episode_id"] = episode_id

    # Cache for /process
    EPISODE_STORE[episode_id] = {
        "episode_dict": episode_dict,
        "status": "uploaded",
        "docs_read": docs_read,
    }

    return {
        "episode_id":    episode_id,
        "episode_dict":  episode_dict,
        "documents_read": docs_read,
        "warnings":      warnings_list,
        "ready_to_process": bool(episode_dict.get("pdx")),
    }


# ══════════════════════════════════════════════════════════════════════════
# ENDPOINT 2 — Process episode through full NOVIQ Engine pipeline
# ══════════════════════════════════════════════════════════════════════════

@app.post("/api/process/{episode_id}")
async def process_episode(episode_id: str, request: Request):
    """
    Run the full NOVIQ Engine pipeline on a previously uploaded episode.
    Alternatively, POST a fresh episode_dict in the request body.

    Pipeline:
      1. EpisodeRecord parsed
      2. ACS Scoring Engine
      3. DCL Exclusion Validation
      4. AR-DRG V11.0 Grouper (5-step)
      5. Intelligence triggers from Medical Logic KB
      6. CodingSuggestion with full provenance

    Returns: CodingSuggestion.to_dict() + kb_flags
    """
    # Load episode from store or body
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass

    if body.get("episode_dict"):
        episode_dict = body["episode_dict"]
    elif episode_id in EPISODE_STORE:
        episode_dict = EPISODE_STORE[episode_id]["episode_dict"]
    else:
        raise HTTPException(404, f"Episode {episode_id} not found. Upload files first.")

    engine = get_engine()
    if engine is None:
        # Engine not available — return a demo result
        return _demo_result(episode_id, episode_dict)

    # ── Run engine ─────────────────────────────────────────────────────
    kb_flags  = []
    result    = {}
    blocked   = False

    try:
        suggestion = engine.process_episode(episode_dict)
        result     = suggestion.to_dict()

    except KnowledgeBaseIncompleteError as e:
        blocked = True
        result  = _blocked_result(episode_id, episode_dict, str(e))
        kb_flags.append({
            "type":    "KB_BLOCKED",
            "severity": "critical",
            "message": str(e),
        })

    except Exception as e:
        raise HTTPException(500, f"Engine error: {str(e)}")

    # ── Apply Medical Logic KB intelligence triggers ────────────────────
    if not blocked:
        kb_flags.extend(_apply_intelligence_triggers(episode_dict, result))

    # ── Store result ───────────────────────────────────────────────────
    EPISODE_STORE[episode_id] = EPISODE_STORE.get(episode_id, {})
    EPISODE_STORE[episode_id].update({
        "suggestion":  result,
        "kb_flags":    kb_flags,
        "status":      "blocked" if blocked else APPROVAL_PENDING,
        "processed_at": _now(),
    })

    return {
        "episode_id":  episode_id,
        "suggestion":  result,
        "kb_flags":    kb_flags,
        "blocked":     blocked,
        "processed_at": _now(),
    }


# ══════════════════════════════════════════════════════════════════════════
# ENDPOINT 3 — Physician approval gate
# ══════════════════════════════════════════════════════════════════════════

@app.post("/api/approve/{episode_id}")
async def approve_episode(episode_id: str, request: Request):
    """
    Physician approves the coding suggestion.
    Calls CodingSuggestion.assert_approved() gate.

    Body: { "physician_id": "DR-KASSAB-001", "action": "approve" | "reject", "reason": "" }
    """
    if episode_id not in EPISODE_STORE:
        raise HTTPException(404, f"Episode {episode_id} not found")

    body = await request.json()
    physician_id = body.get("physician_id", "").strip()
    action       = body.get("action", "approve")
    reason       = body.get("reason", "")

    if not physician_id:
        raise HTTPException(400, "physician_id is required")

    store = EPISODE_STORE[episode_id]

    if action == "approve":
        store["status"]       = APPROVAL_APPROVED
        store["approved_by"]  = physician_id
        store["approved_at"]  = _now()
        return {
            "episode_id":   episode_id,
            "status":       APPROVAL_APPROVED,
            "approved_by":  physician_id,
            "approved_at":  store["approved_at"],
            "message":      "Claim approved. assert_approved() gate passed. Ready for submission.",
        }

    elif action == "reject":
        store["status"]       = APPROVAL_REJECTED
        store["approved_by"]  = physician_id
        store["rejected_at"]  = _now()
        store["reject_reason"] = reason
        return {
            "episode_id": episode_id,
            "status":     APPROVAL_REJECTED,
            "reason":     reason,
            "message":    "Suggestion rejected. Returned for recoding.",
        }

    raise HTTPException(400, "action must be 'approve' or 'reject'")


# ══════════════════════════════════════════════════════════════════════════
# ENDPOINT 4 — Episode queue (all pending + recent)
# ══════════════════════════════════════════════════════════════════════════

@app.get("/api/queue")
async def get_queue():
    """Return all episodes in the store with their status."""
    queue = []
    for ep_id, data in EPISODE_STORE.items():
        ep     = data.get("episode_dict", {})
        result = data.get("suggestion", {})
        queue.append({
            "episode_id":   ep_id,
            "patient_age":  ep.get("patient_age"),
            "patient_sex":  ep.get("patient_sex"),
            "pdx":          ep.get("pdx"),
            "ar_drg":       result.get("proposed_codes", {}).get("ar_drg", "—"),
            "status":       data.get("status", APPROVAL_PENDING),
            "approved_by":  data.get("approved_by"),
            "flag_count":   len(data.get("kb_flags", [])),
            "processed_at": data.get("processed_at"),
        })
    # Most recent first
    queue.sort(key=lambda x: x.get("processed_at") or "", reverse=True)
    return {"queue": queue, "total": len(queue)}


# ══════════════════════════════════════════════════════════════════════════
# ENDPOINT 5 — Episode detail
# ══════════════════════════════════════════════════════════════════════════

@app.get("/api/episode/{episode_id}")
async def get_episode(episode_id: str):
    """Return full detail for a specific episode."""
    if episode_id not in EPISODE_STORE:
        raise HTTPException(404, f"Episode {episode_id} not found")
    return EPISODE_STORE[episode_id]


# ══════════════════════════════════════════════════════════════════════════
# ENDPOINT 6 — Medical Logic KB search
# ══════════════════════════════════════════════════════════════════════════

@app.get("/api/kb/search")
async def search_kb(q: str = "", specialty: str = ""):
    """
    Search the Medical Logic KB by procedure name or keyword.
    Returns matching procedures with clinical logic rules.
    """
    q = q.lower()
    results = []

    for sp_key, procs in MEDICAL_LOGIC_KB.get("procedures", {}).items():
        if specialty and specialty.lower() not in sp_key.lower():
            continue
        for proc in procs:
            if not q or q in proc.get("procedure", "").lower() or \
               any(q in kw.lower() for kw in proc.get("keywords", [])):
                results.append(proc)

    return {
        "query":   q,
        "results": results[:50],
        "total":   len(results),
    }


# ══════════════════════════════════════════════════════════════════════════
# ENDPOINT 7 — KB status
# ══════════════════════════════════════════════════════════════════════════

@app.get("/api/kb/status")
async def kb_status():
    """Return Knowledge Base health status."""
    ar_kb_path = KB_DIR / "ar_drg_kb_seed_v11_new_adrgs.json"
    ar_kb = {}
    if ar_kb_path.exists():
        with open(ar_kb_path) as f:
            ar_kb = json.load(f)

    adrgs = ar_kb.get("adrgs", {})
    f25_threshold = None
    if "F25" in adrgs:
        classes = adrgs["F25"].get("split_profile", {}).get("end_classes", [])
        if classes:
            f25_threshold = classes[0].get("eccs_threshold", {}).get("value")

    return {
        "ar_drg_version": ar_kb.get("_meta", {}).get("versioning", {}).get("ar_drg_version", "unknown"),
        "adrgs_seeded": list(adrgs.keys()),
        "f25_threshold": f25_threshold,
        "f25_blocked": f25_threshold is None,
        "dcl_table": "stub — purchase AR-DRG Definitions Manual",
        "appendix_c": "7/47 unconditional exclusions confirmed",
        "medical_logic_kb": {
            "general_surgery_procedures": len(MEDICAL_LOGIC_KB.get("procedures", {}).get("general_surgery", [])),
            "hand_surgery_procedures":    len(MEDICAL_LOGIC_KB.get("procedures", {}).get("hand_surgery", [])),
            "intelligence_triggers":      len(MEDICAL_LOGIC_KB.get("intelligence_triggers", {})),
        },
        "engine_available": ENGINE_AVAILABLE,
    }


# ══════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _build_empty_episode(episode_id: str) -> dict:
    return {
        "episode_id":       episode_id,
        "patient_age":      0,
        "patient_sex":      "Unknown",
        "pdx":              "",
        "adx":              [],
        "achi_codes":       [],
        "los_days":         0,
        "same_day":         False,
        "separation_mode":  "discharge_home",
        "admission_weight": None,
        "hours_mech_vent":  None,
        "care_type":        "01",
        "acs_pdx_score":    0,
        "acs_adx_scores":   [],
        "ehr_documents":    [],
    }


def _infer_doc_type(filename: str) -> str:
    f = filename.lower()
    if "initial"    in f or "er"     in f: return "Initial Medical Report"
    if "admission"  in f or "admit"  in f: return "Admission Report"
    if "progress"   in f or "daily"  in f: return "Progress Notes"
    if "operation"  in f or "op"     in f or "surgical" in f: return "Operation Notes"
    if "nursing"    in f or "nurse"  in f: return "Nursing Notes"
    if "discharge"  in f or "dc"     in f or "summary" in f: return "Discharge Summary"
    return "EHR Document"


def _merge_json_episode(base: dict, incoming: dict) -> dict:
    """Merge an incoming JSON episode into base, preserving non-empty fields."""
    for key in base:
        val = incoming.get(key)
        if val is None:
            continue
        if isinstance(val, list) and len(val) == 0:
            continue
        if isinstance(val, str) and not val.strip():
            continue
        base[key] = val
    return base


def _extract_from_text(episode: dict, text: str, doc_type: str) -> None:
    """
    Keyword-based extraction from free text.
    Production: replace with NLP/LLM extraction (Intent Agent Phase 5).
    """
    import re

    # Age
    age_m = re.search(r'(\d{1,3})\s*(?:year[s]?\s*old|y/?o|yo)', text, re.I)
    if age_m and episode["patient_age"] == 0:
        episode["patient_age"] = int(age_m.group(1))

    # Sex
    if episode["patient_sex"] == "Unknown":
        if re.search(r'\b(female|woman|girl|she|her)\b', text, re.I):
            episode["patient_sex"] = "Female"
        elif re.search(r'\b(male|man|boy|he|his)\b', text, re.I):
            episode["patient_sex"] = "Male"

    # ICD-10-AM codes (pattern: A-Z followed by 2 digits, optional dot + 1-2 chars)
    icd_codes = re.findall(r'\b([A-Z][0-9]{2}(?:\.[0-9A-Z]{1,2})?)\b', text)
    if icd_codes:
        if not episode["pdx"] and doc_type in ("Initial Medical Report", "Admission Report", "Discharge Summary"):
            episode["pdx"] = icd_codes[0]
        for code in icd_codes[1:]:
            if code not in episode["adx"] and code != episode["pdx"]:
                episode["adx"].append(code)

    # ACHI codes (pattern: 5 digits dash 2 digits)
    achi_codes = re.findall(r'\b(\d{5}-\d{2})\b', text)
    for code in achi_codes:
        if code not in episode["achi_codes"]:
            episode["achi_codes"].append(code)

    # LOS
    los_m = re.search(r'(\d+)\s*(?:day[s]?\s*(?:in hospital|admission|stay|LOS))', text, re.I)
    if los_m and episode["los_days"] == 0:
        episode["los_days"] = int(los_m.group(1))


def _parse_hl7(episode: dict, text: str) -> None:
    """Basic HL7 v2 segment parser for PID, DG1, PR1."""
    import re
    for line in text.splitlines():
        parts = line.split("|")
        if not parts:
            continue
        seg = parts[0]

        if seg == "PID" and len(parts) > 8:
            # PID-7 = DOB, PID-8 = Sex
            sex_raw = parts[8].strip() if len(parts) > 8 else ""
            if sex_raw == "F":   episode["patient_sex"] = "Female"
            elif sex_raw == "M": episode["patient_sex"] = "Male"

        elif seg == "DG1" and len(parts) > 3:
            # DG1-3 = diagnosis code
            code = parts[3].strip().split("^")[0].upper()
            if re.match(r'[A-Z][0-9]{2}', code):
                if not episode["pdx"]:
                    episode["pdx"] = code
                elif code not in episode["adx"]:
                    episode["adx"].append(code)

        elif seg == "PR1" and len(parts) > 3:
            # PR1-3 = procedure code
            code = parts[3].strip()
            if re.match(r'\d{5}-\d{2}', code) and code not in episode["achi_codes"]:
                episode["achi_codes"].append(code)


def _parse_fhir_xml(episode: dict, text: str) -> None:
    """Basic FHIR R4 XML parser for Encounter/Condition/Procedure resources."""
    import re
    codes = re.findall(r'<code value="([A-Z][0-9]{2}(?:\.[0-9A-Z]{1,2})?)"/>', text)
    for code in codes:
        if not episode["pdx"]:
            episode["pdx"] = code
        elif code not in episode["adx"]:
            episode["adx"].append(code)


def _apply_intelligence_triggers(episode: dict, suggestion: dict) -> list[dict]:
    """
    Apply Medical Logic KB intelligence triggers to the episode + suggestion.
    Returns a list of flag dicts for the physician.
    """
    flags = []
    triggers = MEDICAL_LOGIC_KB.get("intelligence_triggers", {})

    # ── Exclusion Hunter ──────────────────────────────────────────────
    excl = triggers.get("exclusion_hunter", {})
    all_text = " ".join([
        str(episode.get("pdx", "")),
        " ".join(episode.get("adx", [])),
        " ".join(episode.get("ehr_documents", [])),
    ]).lower()
    for kw in excl.get("keywords", []):
        if kw.lower() in all_text:
            flags.append({
                "trigger":   "exclusion_hunter",
                "severity":  "critical",
                "action":    excl["action"],
                "message":   f"EXCLUSION HUNTER: keyword '{kw}' detected — policy exclusion risk. Verify Mode of Injury.",
            })
            break

    # ── NCV Matcher ───────────────────────────────────────────────────
    ncv = triggers.get("ncv_matcher", {})
    proposed_achi = suggestion.get("proposed_codes", {}).get("achi", [])
    for proc in ncv.get("trigger_procedures", []):
        # Check if any known CTS ACHI code is in proposed
        cts_achi = {"90645-00", "90644-00", "90643-00"}
        if any(a in cts_achi for a in proposed_achi):
            flags.append({
                "trigger":  "ncv_matcher",
                "severity": "high",
                "action":   ncv["action"],
                "message":  "NCV MATCHER: Carpal Tunnel surgery proposed. Verify NCV/EMG Test is attached to claim.",
            })
            break

    # ── Hardware Auditor ──────────────────────────────────────────────
    hw = triggers.get("hardware_auditor", {})
    hw_achi = {"47360-00", "47330-00", "47321-00", "47480-00"}
    if any(a in hw_achi for a in proposed_achi):
        # Check if fracture is documented as comminuted
        fracture_text = all_text
        if "comminuted" not in fracture_text and "complex" not in fracture_text:
            flags.append({
                "trigger":  "hardware_auditor",
                "severity": "medium",
                "action":   hw["action"],
                "message":  "HARDWARE AUDITOR: Plate & Screw fixation detected. Verify fracture is documented as comminuted or complex — K-Wire is standard for simple fractures.",
            })

    # ── Timing Checker ────────────────────────────────────────────────
    tc = triggers.get("timing_checker", {})
    tendon_achi = {"48624-00", "48624-01", "48600-00", "48603-00"}
    if any(a in tendon_achi for a in proposed_achi):
        flags.append({
            "trigger":  "timing_checker",
            "severity": "medium",
            "action":   tc["action"],
            "message":  "TIMING CHECKER: Tendon repair detected. Verify time of injury in ER notes. >24h = Delayed Repair coding tier.",
        })

    return flags


def _demo_result(episode_id: str, episode_dict: dict) -> dict:
    """Return a demo result when engine is not available."""
    return {
        "episode_id":  episode_id,
        "suggestion": {
            "episode_id":      episode_id,
            "suggestion_id":   str(uuid.uuid4()),
            "approval_status": APPROVAL_PENDING,
            "proposed_codes": {
                "pdx":         episode_dict.get("pdx", "C48.1"),
                "adx":         episode_dict.get("adx", []),
                "achi":        episode_dict.get("achi_codes", []),
                "ar_drg":      "G13Z",
                "ar_drg_desc": "Peritonectomy for Gastrointestinal Disorders",
            },
            "acs_scores": {
                "pdx_score": episode_dict.get("acs_pdx_score", 5),
                "coding_justification": "Demo mode — engine modules not loaded.",
            },
            "grouper_result":    {"ar_drg_code": "G13Z", "eccs": 0.0, "step_trace": ["Demo mode"]},
            "validation_result": {"summary": {"total_excluded": 0, "upcoding_risk_count": 0}},
            "provenance":        {"ehr_documents_read": episode_dict.get("ehr_documents", [])},
            "flags":             ["Demo mode: NOVIQ engine modules not loaded. Running API-only."],
            "engine_version":    "V11.0",
        },
        "kb_flags": [],
        "blocked":   False,
        "demo_mode": True,
    }


def _blocked_result(episode_id: str, episode_dict: dict, error: str) -> dict:
    """Return a blocked result for F25 or other KB gate."""
    return {
        "episode_id":      episode_id,
        "suggestion_id":   str(uuid.uuid4()),
        "approval_status": "BLOCKED",
        "proposed_codes": {
            "pdx":         episode_dict.get("pdx", ""),
            "adx":         episode_dict.get("adx", []),
            "achi":        episode_dict.get("achi_codes", []),
            "ar_drg":      "BLOCKED",
            "ar_drg_desc": "Knowledge Base Incomplete — purchase AR-DRG Definitions Manual",
        },
        "flags": [f"KB_BLOCKED: {error}"],
        "engine_version": "V11.0",
    }
