"""
NOVIQ Engine — FastAPI Backend v3.2 (Stable Version)
===================================================
Fully tested for Railway + Dashboard
"""

from __future__ import annotations
import json
import os
import sys
import uuid
import warnings
import re
from datetime import datetime, timezone
from pathlib import Path

# ====================== PATH SETUP ======================
BASE_DIR = Path(__file__).parent

# Add engine folder to Python path
for candidate in [BASE_DIR / "engine", BASE_DIR]:
    if (candidate / "noviq_engine.py").exists():
        sys.path.insert(0, str(candidate))
        ENGINE_SRC = candidate
        break
else:
    sys.path.insert(0, str(BASE_DIR))
    ENGINE_SRC = BASE_DIR

# KB Directory
KB_DIR = next(
    (p for p in [BASE_DIR / "knowledge_base", BASE_DIR]
     if (p / "ar_drg_kb_seed_v11_new_adrgs.json").exists()),
    BASE_DIR
)

# Data folder for persistent storage
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# ====================== IMPORTS ======================
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
warnings.filterwarnings("ignore")

# Engine Import
ENGINE_AVAILABLE = False
NOVIQEngine = None
KBIncompleteError = Exception

try:
    from noviq_engine import NOVIQEngine as _NOVIQEngine
    from validation_rules import KnowledgeBaseIncompleteError as _KBError
    NOVIQEngine = _NOVIQEngine
    KBIncompleteError = _KBError
    ENGINE_AVAILABLE = True
    print(f"[OK] Engine loaded successfully from: {ENGINE_SRC}")
except Exception as e:
    print(f"[WARN] Engine import failed: {e}")

# ====================== APP ======================
app = FastAPI(title="NOVIQ Engine API", version="3.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)

# ====================== STORAGE ======================
STORE_FILE = DATA_DIR / "episodes.json"
COUNTER_FILE = DATA_DIR / "counter.txt"

def _load_store() -> dict:
    if STORE_FILE.exists():
        try:
            return json.loads(STORE_FILE.read_text(encoding="utf-8"))
        except:
            return {}
    return {}

def _save_store(store: dict):
    STORE_FILE.write_text(
        json.dumps(store, ensure_ascii=False, default=str),
        encoding="utf-8"
    )

STORE = _load_store()

def _next_episode_id() -> str:
    n = 1
    if COUNTER_FILE.exists():
        try:
            n = int(COUNTER_FILE.read_text().strip()) + 1
        except:
            n = 1
    COUNTER_FILE.write_text(str(n))
    return f"EP-{n:04d}"

# ====================== MEDICAL KB ======================
ML_KB = {}
for fname in ["keyword_dictionary_medical_logic_v3.json",
              "keyword_dictionary_medical_logic_v2.json",
              "keyword_dictionary_medical_logic_v1.json"]:
    p = KB_DIR / fname
    if p.exists():
        ML_KB = json.loads(p.read_text(encoding="utf-8"))
        meta = ML_KB.get("_meta", {})
        print(f"[OK] Medical Logic KB loaded — {meta.get('procedure_counts', {}).get('total', 0)} procedures")
        break

# ====================== ENGINE ======================
_engine = None

def get_engine():
    global _engine
    if _engine is None and ENGINE_AVAILABLE:
        try:
            _engine = NOVIQEngine(
                kb_path=KB_DIR / "ar_drg_kb_seed_v11_new_adrgs.json",
                excl_path=KB_DIR / "dcl_exclusions.json",
            )
            print("[OK] NOVIQEngine initialized")
        except Exception as e:
            print(f"[WARN] Engine init error: {e}")
    return _engine

# ====================== HELPERS ======================
def _now():
    return datetime.now(timezone.utc).isoformat()

def _empty_episode(eid: str):
    return {
        "episode_id": eid,
        "patient_name": "",
        "patient_age": 0,
        "patient_sex": "Unknown",
        "pdx": "",
        "adx": [],
        "achi_codes": [],
        "los_days": 0,
        "same_day": False,
        "separation_mode": "discharge_home",
        "admission_weight": None,
        "hours_mech_vent": None,
        "care_type": "01",
        "acs_pdx_score": 0,
        "acs_adx_scores": [],
        "ehr_documents": []
    }

def _doc_type(filename: str) -> str:
    f = filename.lower()
    if any(x in f for x in ["initial", "er"]): return "Initial Medical Report"
    if any(x in f for x in ["admission", "admit"]): return "Admission Report"
    if any(x in f for x in ["progress", "daily"]): return "Progress Notes"
    if any(x in f for x in ["operation", "op", "surg"]): return "Operation Notes"
    if any(x in f for x in ["nursing", "nurse"]): return "Nursing Notes"
    if any(x in f for x in ["discharge", "summary"]): return "Discharge Summary"
    return "EHR Document"

def _merge(base: dict, incoming: dict) -> dict:
    for k, v in incoming.items():
        if v is not None:
            if isinstance(v, list) and not v:
                continue
            if isinstance(v, str) and not v.strip():
                continue
            base[k] = v
    return base

def _extract_text(ep: dict, text: str, doc_type: str):
    # Age
    m = re.search(r'(\d{1,3})\s*(?:year|yrs?|y/o)', text, re.I)
    if m and ep.get("patient_age") == 0:
        ep["patient_age"] = int(m.group(1))
    # Sex
    if ep.get("patient_sex") == "Unknown":
        if re.search(r'\b(female|woman|she|her)\b', text, re.I):
            ep["patient_sex"] = "Female"
        elif re.search(r'\b(male|man|he|his)\b', text, re.I):
            ep["patient_sex"] = "Male"
    # ICD codes
    icds = re.findall(r'\b([A-Z][0-9]{2}(?:\.[0-9A-Z]{1,2})?)\b', text)
    if icds:
        if not ep["pdx"]:
            ep["pdx"] = icds[0]
        for c in icds[1:]:
            if c not in ep["adx"] and c != ep["pdx"]:
                ep["adx"].append(c)
    # ACHI
    for c in re.findall(r'\b(\d{5}-\d{2})\b', text):
        if c not in ep["achi_codes"]:
            ep["achi_codes"].append(c)

def _extract_pdf(raw: bytes, ep: dict, doc_type: str):
    text = ""
    try:
        import pdfplumber, io
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    except:
        text = raw.decode("utf-8", errors="ignore")
    _extract_text(ep, text, doc_type)
    return ep

def _parse_hl7(ep: dict, text: str):
    pass  # stub - can be expanded later

def _parse_fhir_xml(ep: dict, text: str):
    pass  # stub

def _triggers(ep: dict, sug: dict):
    return []  # stub for now

def _demo(episode_id: str, ep: dict):
    return {
        "episode_id": episode_id,
        "blocked": False,
        "demo_mode": True,
        "suggestion": {
            "episode_id": episode_id,
            "approval_status": "PENDING",
            "proposed_codes": {
                "ar_drg": "G13Z",
                "ar_drg_desc": "Peritonectomy for Gastrointestinal Disorders (Demo)"
            },
            "flags": ["Demo Mode - Full engine not loaded"],
            "engine_version": "V11.0-DEMO"
        }
    }

def _blocked(episode_id: str, ep: dict, error: str):
    return {
        "episode_id": episode_id,
        "approval_status": "BLOCKED",
        "proposed_codes": {"ar_drg": "BLOCKED"},
        "flags": [f"KB Error: {error}"]
    }

# ====================== ROUTES ======================

@app.get("/", response_class=HTMLResponse)
async def root():
    for name in ["noviq_dashboard_v2.html", "noviq_dashboard.html"]:
        p = BASE_DIR / name
        if p.exists():
            return HTMLResponse(p.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>NOVIQ Engine</h1><p>Dashboard not found</p>")

@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...)):
    episode_id = _next_episode_id()
    ep = _empty_episode(episode_id)
    docs = []
    warns = []

    for f in files:
        raw = await f.read()
        fname = f.filename or ""
        ext = Path(fname).suffix.lower()
        dtype = _doc_type(fname)
        docs.append({"filename": fname, "doc_type": dtype})

        try:
            if ext == ".json":
                ep = _merge(ep, json.loads(raw.decode("utf-8")))
            elif ext == ".pdf":
                ep = _merge(ep, _extract_pdf(raw, ep, dtype))
            else:
                _extract_text(ep, raw.decode("utf-8", errors="ignore"), dtype)
        except Exception as e:
            warns.append(str(e))

    ep["ehr_documents"] = [d["doc_type"] for d in docs]

    STORE[episode_id] = {
        "episode_dict": ep,
        "status": "uploaded",
        "created_at": _now()
    }
    _save_store(STORE)

    return {
        "episode_id": episode_id,
        "episode_dict": ep,
        "ready_to_process": bool(ep.get("pdx")),
        "engine_mode": "live" if ENGINE_AVAILABLE else "demo"
    }

# FIXED PROCESS ENDPOINT
@app.post("/api/process")
@app.post("/api/process/{episode_id}")
async def process_episode(episode_id: str = None, request: Request = None):
    body = {}
    try:
        body = await request.json()
    except:
        pass

    if not episode_id and body.get("episode_id"):
        episode_id = body.get("episode_id")

    if body.get("episode_dict"):
        episode_dict = body["episode_dict"]
    elif episode_id and episode_id in STORE:
        episode_dict = STORE[episode_id].get("episode_dict", {})
    else:
        episode_dict = body

    if not episode_dict or not episode_dict.get("pdx"):
        raise HTTPException(400, "Missing pdx. Upload EHR first.")

    engine = get_engine()
    if not engine:
        res = _demo(episode_id or "EP-DEMO", episode_dict)
        return {"episode_id": episode_id, "suggestion": res["suggestion"], "demo_mode": True}

    try:
        suggestion = engine.process_episode(episode_dict)
        result = suggestion.to_dict()
        kb_flags = _triggers(episode_dict, result)
        blocked = False
    except Exception as e:
        blocked = True
        result = _blocked(episode_id or "EP-ERROR", episode_dict, str(e))
        kb_flags = [{"type": "ERROR", "message": str(e)}]

    eid = episode_id or _next_episode_id()
    STORE[eid] = {
        "episode_dict": episode_dict,
        "suggestion": result,
        "kb_flags": kb_flags,
        "status": "blocked" if blocked else "PENDING",
        "processed_at": _now()
    }
    _save_store(STORE)

    return {
        "episode_id": eid,
        "suggestion": result,
        "kb_flags": kb_flags,
        "blocked": blocked,
        "engine_mode": "live"
    }

# ====================== OTHER ROUTES ======================
@app.get("/api/kb/status")
async def kb_status():
    return {
        "engine_available": ENGINE_AVAILABLE,
        "engine_mode": "live" if ENGINE_AVAILABLE else "demo",
        "kb_dir": str(KB_DIR)
    }

@app.get("/api/health")
async def health():
    return {"status": "ok", "engine": ENGINE_AVAILABLE}

# ====================== STARTUP ======================
@app.on_event("startup")
async def startup():
    get_engine()
    print(f"[OK] NOVIQ Engine v3.2 started — {'LIVE' if ENGINE_AVAILABLE else 'DEMO'}")

print("main.py loaded successfully")
