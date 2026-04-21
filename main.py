"""
NOVIQ Engine — FastAPI Backend (Production-Fixed v2)
======================================================
Fixes:
  1. EPISODE_STORE persisted to JSON file (fixes multi-worker 404)
  2. Engine import with detailed error logging
  3. Graceful demo fallback with full episode_dict return
  4. CORS enabled for all origins
"""
from __future__ import annotations
import json, os, sys, uuid, warnings, re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── Paths ──────────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent
ENGINE_DIR = BASE_DIR / "engine"
KB_DIR     = BASE_DIR / "knowledge_base"
DATA_DIR   = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(ENGINE_DIR))

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
warnings.filterwarnings("ignore")

# ── Import engine ──────────────────────────────────────────────────────────
ENGINE_AVAILABLE = False
ENGINE_IMPORT_ERROR = None
NOVIQEngine = None
KnowledgeBaseIncompleteError = Exception

try:
    from noviq_engine import NOVIQEngine as _NOVIQEngine
    from models import APPROVAL_APPROVED, APPROVAL_PENDING, APPROVAL_REJECTED
    from validation_rules import KnowledgeBaseIncompleteError as _KBError
    NOVIQEngine = _NOVIQEngine
    KnowledgeBaseIncompleteError = _KBError
    ENGINE_AVAILABLE = True
    print("[OK] Engine modules loaded successfully")
except Exception as e:
    ENGINE_IMPORT_ERROR = str(e)
    print(f"[WARN] Engine import failed: {e}")
    import traceback
    traceback.print_exc()

# ── App ────────────────────────────────────────────────────────────────────
app = FastAPI(title="NOVIQ Engine API", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Persistent Episode Store ───────────────────────────────────────────────
EPISODE_STORE_PATH = DATA_DIR / "episodes.json"

def _load_store() -> dict:
    if EPISODE_STORE_PATH.exists():
        try:
            with open(EPISODE_STORE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return {}
    return {}

def _save_store(store: dict):
    with open(EPISODE_STORE_PATH, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, default=str)

EPISODE_STORE: dict = _load_store()

# ── Medical Logic KB ─────────────────────────────────────────────────────
MEDICAL_LOGIC_KB: dict = {}
for _ml_name in ["keyword_dictionary_medical_logic_v3.json",
                  "keyword_dictionary_medical_logic_v2.json",
                  "keyword_dictionary_medical_logic_v1.json"]:
    _ml_path = KB_DIR / _ml_name
    if _ml_path.exists():
        with open(_ml_path, encoding="utf-8") as f:
            MEDICAL_LOGIC_KB = json.load(f)
        meta = MEDICAL_LOGIC_KB.get("_meta", {})
        total = meta.get("procedure_counts", {}).get("total", 0)
        trigs = meta.get("intelligence_triggers", 0)
        print(f"[OK] Medical Logic KB v{meta.get('version','?')} — {total} procedures | {trigs} triggers")
        break
else:
    print(f"[WARN] No Medical Logic KB found in {KB_DIR}")

# ── Engine singleton ─────────────────────────────────────────────────────
_engine = None

def get_engine():
    global _engine
    if _engine is None and ENGINE_AVAILABLE and NOVIQEngine is not None:
        try:
            kb_path   = KB_DIR / "ar_drg_kb_seed_v11_new_adrgs.json"
            excl_path = KB_DIR / "dcl_exclusions.json"
            _engine = NOVIQEngine(kb_path=kb_path, excl_path=excl_path)
            print("[OK] NOVIQEngine initialised")
        except Exception as e:
            print(f"[WARN] Engine init failed: {e}")
            import traceback
            traceback.print_exc()
    return _engine

@app.on_event("startup")
async def startup_event():
    get_engine()
    mode = "LIVE" if get_engine() else "DEMO"
    print(f"[OK] NOVIQ API ready — engine={mode} | KB={KB_DIR}")
    if ENGINE_IMPORT_ERROR:
        print(f"[INFO] Engine import error: {ENGINE_IMPORT_ERROR[:200]}")

# ── Dashboard ────────────────────────────────────────────────────────────
def _find_dashboard():
    for name in ["noviq_dashboard_v2.html", "noviq_dashboard.html"]:
        p = BASE_DIR / name
        if p.exists():
            return p
    return None

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    p = _find_dashboard()
    if p:
        return HTMLResponse(p.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>NOVIQ Engine API</h1><p>Dashboard file not found.</p>")

# ── Upload ───────────────────────────────────────────────────────────────
@app.post("/api/upload")
async def upload_ehr(files: list[UploadFile] = File(...)):
    episode_id   = f"EP-{datetime.now().strftime('%Y%m%d')}-{str(uuid.uuid4())[:6].upper()}"
    episode_dict = _empty_episode(episode_id)
    docs_read, warn_list = [], []

    for upload in files:
        content  = await upload.read()
        filename = upload.filename or ""
        ext      = Path(filename).suffix.lower()
        doc_type = _infer_doc_type(filename)
        docs_read.append({"filename": filename, "doc_type": doc_type,
                           "size_kb": round(len(content)/1024, 1)})
        try:
            text = content.decode("utf-8", errors="ignore")
            if ext == ".json":
                parsed = json.loads(text)
                episode_dict = _merge(episode_dict, parsed)
            elif ext in (".txt", ".text", ".pdf"):
                _extract_text(episode_dict, text, doc_type)
            elif ext == ".hl7":
                _parse_hl7(episode_dict, text)
            elif ext == ".xml":
                _parse_fhir_xml(episode_dict, text)
            else:
                warn_list.append(f"Unsupported format: {ext}")
        except Exception as e:
            warn_list.append(f"Error parsing {filename}: {e}")

    # Apply keyword mapping after all files processed
    _keyword_map(episode_dict)
    
    # Calculate LOS if dates found
    _calc_los(episode_dict)

    episode_dict["ehr_documents"] = [d["doc_type"] for d in docs_read]
    
    # Save to persistent store
    EPISODE_STORE[episode_id] = {
        "episode_dict": episode_dict,
        "status": "uploaded",
        "docs_read": docs_read,
        "patient_name": episode_dict.get("patient_name", "Unknown"),
        "patient_age": episode_dict.get("patient_age", 0),
        "patient_sex": episode_dict.get("patient_sex", "Unknown"),
    }
    _save_store(EPISODE_STORE)

    return {
        "episode_id": episode_id,
        "episode_dict": episode_dict,
        "documents_read": docs_read,
        "warnings": warn_list,
        "ready_to_process": bool(episode_dict.get("pdx")),
        "engine_mode": "live" if ENGINE_AVAILABLE else "demo"
    }

# ── Process ──────────────────────────────────────────────────────────────
@app.post("/api/process/{episode_id}")
async def process_episode(episode_id: str, request: Request):
    body = {}
    try: 
        body = await request.json()
    except: 
        pass

    # Reload store in case another worker saved it
    store = _load_store()
    
    if body.get("episode_dict"):
        episode_dict = body["episode_dict"]
        # Update store
        if episode_id in store:
            store[episode_id]["episode_dict"] = episode_dict
        else:
            store[episode_id] = {"episode_dict": episode_dict, "status": "uploaded"}
        _save_store(store)
        EPISODE_STORE.update(store)
    elif episode_id in store:
        episode_dict = store[episode_id]["episode_dict"]
        EPISODE_STORE.update(store)
    else:
        raise HTTPException(404, detail=f"Episode {episode_id} not found. Upload files first.")

    engine = get_engine()

    # DEMO mode
    if engine is None:
        result = _demo_result(episode_id, episode_dict)
        EPISODE_STORE[episode_id].update({
            "suggestion": result["suggestion"],
            "kb_flags": [],
            "status": "PENDING",
            "processed_at": _now()
        })
        _save_store(EPISODE_STORE)
        result["episode_dict"] = episode_dict
        return result

    # LIVE engine
    kb_flags, blocked = [], False
    try:
        suggestion = engine.process_episode(episode_dict)
        result = suggestion.to_dict()
    except Exception as e:
        error_name = type(e).__name__
        if "KnowledgeBaseIncomplete" in error_name or "threshold" in str(e).lower():
            blocked = True
            result = _blocked_result(episode_id, episode_dict, str(e))
            kb_flags.append({"type": "KB_BLOCKED", "severity": "critical", "message": str(e)})
        else:
            result = _demo_result(episode_id, episode_dict)["suggestion"]
            kb_flags.append({"type": "ENGINE_ERROR", "severity": "warn", "message": f"{error_name}: {str(e)}"})

    if not blocked:
        kb_flags.extend(_apply_triggers(episode_dict, result))

    EPISODE_STORE[episode_id].update({
        "suggestion": result,
        "kb_flags": kb_flags,
        "status": "blocked" if blocked else "PENDING",
        "processed_at": _now()
    })
    _save_store(EPISODE_STORE)

    return {
        "episode_id": episode_id,
        "suggestion": result,
        "episode_dict": episode_dict,
        "kb_flags": kb_flags,
        "blocked": blocked,
        "processed_at": _now(),
        "engine_mode": "live"
    }

# ── Approve ──────────────────────────────────────────────────────────────
@app.post("/api/approve/{episode_id}")
async def approve_episode(episode_id: str, request: Request):
    store = _load_store()
    if episode_id not in store:
        raise HTTPException(404, detail="Episode not found")
    
    body = await request.json()
    physician_id = body.get("physician_id", "").strip()
    action = body.get("action", "approve")
    reason = body.get("reason", "")
    
    if not physician_id:
        raise HTTPException(400, detail="physician_id required")
    
    if action == "approve":
        store[episode_id].update({
            "status": "APPROVED",
            "approved_by": physician_id,
            "approved_at": _now()
        })
    elif action == "reject":
        store[episode_id].update({
            "status": "REJECTED",
            "approved_by": physician_id,
            "rejected_at": _now(),
            "reject_reason": reason
        })
    else:
        raise HTTPException(400, detail="action must be approve or reject")
    
    _save_store(store)
    EPISODE_STORE.update(store)
    
    return {
        "episode_id": episode_id,
        "status": store[episode_id]["status"],
        "approved_by": physician_id
    }

# ── Queue ────────────────────────────────────────────────────────────────
@app.get("/api/queue")
async def get_queue():
    store = _load_store()
    queue = []
    for ep_id, data in store.items():
        ep = data.get("episode_dict", {})
        r = data.get("suggestion", {})
        queue.append({
            "episode_id": ep_id,
            "patient_name": data.get("patient_name", ep.get("patient_name", "—")),
            "patient_age": ep.get("patient_age"),
            "patient_sex": ep.get("patient_sex"),
            "pdx": ep.get("pdx"),
            "ar_drg": r.get("proposed_codes", {}).get("ar_drg", "—") if isinstance(r, dict) else "—",
            "status": data.get("status", "PENDING"),
            "approved_by": data.get("approved_by"),
            "flag_count": len(data.get("kb_flags", [])),
            "processed_at": data.get("processed_at"),
        })
    queue.sort(key=lambda x: x.get("processed_at") or "", reverse=True)
    return {"queue": queue, "total": len(queue)}

@app.get("/api/episode/{episode_id}")
async def get_episode(episode_id: str):
    store = _load_store()
    if episode_id not in store:
        raise HTTPException(404, detail="Episode not found")
    return store[episode_id]

@app.get("/api/kb/search")
async def search_kb(q: str = "", specialty: str = ""):
    q = q.lower()
    results = []
    for sp_key, procs in MEDICAL_LOGIC_KB.get("procedures", {}).items():
        if specialty and specialty.lower() not in sp_key.lower():
            continue
        for proc in procs:
            if not q or q in proc.get("procedure", "").lower() or \
               any(q in kw.lower() for kw in proc.get("keywords", [])):
                results.append(proc)
    return {"query": q, "results": results[:50], "total": len(results)}

@app.get("/api/kb/status")
async def kb_status():
    ar_kb_path = KB_DIR / "ar_drg_kb_seed_v11_new_adrgs.json"
    ar_kb = {}
    if ar_kb_path.exists():
        with open(ar_kb_path, encoding="utf-8") as f:
            ar_kb = json.load(f)
    adrgs = ar_kb.get("adrgs", {})
    f25_threshold = None
    if "F25" in adrgs:
        classes = adrgs["F25"].get("split_profile", {}).get("end_classes", [])
        if classes:
            f25_threshold = classes[0].get("eccs_threshold", {}).get("value")
    ml = MEDICAL_LOGIC_KB.get("_meta", {}).get("procedure_counts", {})
    return {
        "ar_drg_version": ar_kb.get("_meta", {}).get("versioning", {}).get("ar_drg_version", "V11.0"),
        "engine_available": ENGINE_AVAILABLE,
        "engine_mode": "live" if ENGINE_AVAILABLE else "demo",
        "adrgs_seeded": list(adrgs.keys()),
        "f25_blocked": f25_threshold is None,
        "kb_dir": str(KB_DIR),
        "medical_logic_kb": {
            "version": MEDICAL_LOGIC_KB.get("_meta", {}).get("version", "unknown"),
            "total": ml.get("total", 0),
            "general_surgery": ml.get("general_surgery", 0),
            "hand_surgery": ml.get("hand_surgery", 0),
            "bariatric": ml.get("bariatric", 0),
            "breast": ml.get("breast", 0),
            "plastic": ml.get("plastic", 0),
            "orthopaedic": ml.get("orthopaedic", 0),
            "intelligence_triggers": len(MEDICAL_LOGIC_KB.get("intelligence_triggers", {})),
        },
    }

@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "engine": ENGINE_AVAILABLE,
        "engine_import_error": ENGINE_IMPORT_ERROR,
        "kb_dir": str(KB_DIR),
        "time": _now()
    }

# ── Helpers ──────────────────────────────────────────────────────────────
def _now(): 
    return datetime.now(timezone.utc).isoformat()

def _empty_episode(eid):
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

def _infer_doc_type(fn):
    f = fn.lower()
    if "initial" in f or "er" in f:        return "Initial Medical Report"
    if "admission" in f or "admit" in f:    return "Admission Report"
    if "progress" in f or "daily" in f:     return "Progress Notes"
    if "operation" in f or "op" in f:       return "Operation Notes"
    if "nursing" in f or "nurse" in f:      return "Nursing Notes"
    if "discharge" in f or "summary" in f:  return "Discharge Summary"
    return "EHR Document"

def _merge(base, incoming):
    for k in base:
        v = incoming.get(k)
        if v is None: 
            continue
        if isinstance(v, list) and not v: 
            continue
        if isinstance(v, str) and not v.strip(): 
            continue
        base[k] = v
    return base

def _extract_text(ep, text, doc_type):
    # Patient Name
    m = re.search(r'Patient\s*Name:\s*([^\n\r]+)', text, re.I)
    if m and not ep.get("patient_name"):
        ep["patient_name"] = m.group(1).strip()
    
    # Age
    m = re.search(r'(\d{1,3})\s*(?:year[s]?\s*old|y/?o|Years|yrs)', text, re.I)
    if m and ep.get("patient_age", 0) == 0:
        ep["patient_age"] = int(m.group(1))
    
    # Sex
    if ep.get("patient_sex") == "Unknown":
        if re.search(r'\b(female|woman|she|her)\b', text, re.I):
            ep["patient_sex"] = "Female"
        elif re.search(r'\b(male|man|he|his)\b', text, re.I):
            ep["patient_sex"] = "Male"
    
    # Explicit ICD codes
    icd = re.findall(r'\b([A-Z][0-9]{2}(?:\.[0-9A-Z]{1,2})?)\b', text)
    if icd and not ep["pdx"] and doc_type in ("Initial Medical Report", "Admission Report", "Discharge Summary"):
        ep["pdx"] = icd[0]
    for c in icd[1:]:
        if c not in ep["adx"] and c != ep["pdx"]:
            ep["adx"].append(c)
    
    # Explicit ACHI codes
    for c in re.findall(r'\b(\d{5}-\d{2})\b', text):
        if c not in ep["achi_codes"]:
            ep["achi_codes"].append(c)
    
    # Admission / Discharge dates for LOS
    if doc_type == "Discharge Summary":
        adm = re.search(r'Admission\s*Date:\s*\[?(\d{1,2}-\d{1,2}-\d{4})\]?', text, re.I)
        dis = re.search(r'Discharge\s*Date:\s*\[?(\d{1,2}-\d{1,2}-\d{4})\]?', text, re.I)
        if adm and dis and ep.get("los_days", 0) == 0:
            try:
                d1 = datetime.strptime(adm.group(1), "%d-%m-%Y")
                d2 = datetime.strptime(dis.group(1), "%d-%m-%Y")
                ep["los_days"] = max(1, (d2 - d1).days)
            except:
                pass

def _keyword_map(ep):
    """Map common diagnosis/procedure keywords to codes when explicit codes missing."""
    docs = [d.lower() for d in ep.get("ehr_documents", [])]
    text = " ".join(docs)
    
    if not ep.get("pdx"):
        if any("append" in d for d in docs):
            ep["pdx"] = "K35.8"
            ep["acs_pdx_score"] = 7
        elif any("peritonectomy" in d for d in docs):
            ep["pdx"] = "C48.1"
            ep["acs_pdx_score"] = 6
    
    if not ep.get("achi_codes"):
        if any("operation" in d or "surgical" in d for d in docs):
            if ep.get("pdx") == "K35.8":
                ep["achi_codes"] = ["30515-00"]  # Appendectomy
            elif ep.get("pdx") == "C48.1":
                ep["achi_codes"] = ["96211-00"]  # Peritonectomy

def _calc_los(ep):
    """Ensure LOS has a reasonable default."""
    if ep.get("los_days", 0) == 0:
        ep["los_days"] = 2  # Default short stay

def _parse_hl7(ep, text):
    for line in text.splitlines():
        parts = line.split("|")
        # TODO: HL7 parsing implementation
        pass

def _parse_fhir_xml(ep, text):
    # TODO: FHIR XML parsing implementation
    pass

def _apply_triggers(episode, suggestion):
    """Apply medical logic triggers."""
    flags = []
    triggers = MEDICAL_LOGIC_KB.get("intelligence_triggers", {})
    all_text = " ".join([
        str(episode.get("pdx", "")),
        " ".join(episode.get("adx", [])),
        " ".join(episode.get("ehr_documents", []))
    ]).lower()
    
    excl = triggers.get("exclusion_hunter", {})
    for kw in excl.get("keywords", []):
        if kw.lower() in all_text:
            flags.append({
                "trigger": "exclusion_hunter",
                "severity": "critical",
                "action": excl.get("action", "AUTO_REJECT"),
                "message": f"EXCLUSION HUNTER: '{kw}' detected — policy exclusion risk."
            })
            break
    
    proposed_achi = set(suggestion.get("proposed_codes", {}).get("achi") or [])
    cts_achi = {"90645-00", "90644-00", "90643-00"}
    if proposed_achi & cts_achi:
        ncv = triggers.get("ncv_matcher", {})
        flags.append({
            "trigger": "ncv_matcher",
            "severity": "high",
            "action": ncv.get("action", "FLAG"),
            "message": "NCV MATCHER: CTS surgery — verify NCV/EMG report is attached."
        })
    
    return flags

def _demo_result(episode_id, episode_dict):
    """Generate demo result when engine is not available."""
    pdx = episode_dict.get("pdx", "")
    achi = episode_dict.get("achi_codes", [])
    
    # Map to demo DRG based on codes
    drg_map = {
        "K35.8": ("G01A", "Appendectomy Minor Complexity"),
        "C48.1": ("G13Z", "Peritonectomy"),
    }
    
    drg_code, drg_desc = drg_map.get(pdx, ("960Z", "Ungroupable"))
    
    if pdx == "K35.8" and "30515-00" in achi:
        drg_code = "G01A"
        drg_desc = "Appendectomy Minor Complexity"
    elif pdx == "C48.1" and "96211-00" in achi:
        drg_code = "G13Z"
        drg_desc = "Peritonectomy"
    
    return {
        "episode_id": episode_id,
        "suggestion": {
            "episode_id": episode_id,
            "suggestion_id": str(uuid.uuid4()),
            "approval_status": "PENDING",
            "proposed_codes": {
                "ar_drg": drg_code,
                "ar_drg_desc": drg_desc,
                "pdx": pdx,
                "adx": episode_dict.get("adx", []),
                "achi": achi,
            },
            "acs_scores": {
                "pdx_score": episode_dict.get("acs_pdx_score", 5),
                "coding_justification": "Demo mode — engine modules not loaded. Result based on code mapping.",
            },
            "grouper_result": {
                "ar_drg_code": drg_code,
                "eccs": 0.0,
                "step_trace": [
                    "Step 1: Demo mode - no engine available",
                    "Step 2: Code mapping used",
                    f"Step 3: PDX={pdx} mapped to {drg_code}",
                ],
            },
            "validation_result": {"summary": {"total_excluded": 0, "upcoding_risk_count": 0}},
            "provenance": {
                "ehr_documents_read": episode_dict.get("ehr_documents", []),
                "engine_version": "DEMO",
            },
            "flags": ["⚠ Demo mode: Engine modules not loaded. Result based on code mapping."],
        },
        "kb_flags": [],
    }

def _blocked_result(episode_id, episode_dict, error):
    """Generate blocked result when KB is incomplete."""
    return {
        "episode_id": episode_id,
        "suggestion": {
            "episode_id": episode_id,
            "suggestion_id": str(uuid.uuid4()),
            "approval_status": "BLOCKED",
            "proposed_codes": {
                "pdx": episode_dict.get("pdx", ""),
                "adx": episode_dict.get("adx", []),
                "achi": episode_dict.get("achi_codes", []),
                "ar_drg": "BLOCKED",
                "ar_drg_desc": "KB Incomplete — purchase AR-DRG Definitions Manual",
            },
            "acs_scores": {"pdx_score": 0, "coding_justification": "Blocked due to incomplete KB"},
            "grouper_result": {"ar_drg_code": "BLOCKED", "eccs": 0.0, "step_trace": ["BLOCKED: " + error]},
            "validation_result": {"summary": {"total_excluded": 0, "upcoding_risk_count": 0}},
            "provenance": {"ehr_documents_read": [], "engine_version": "BLOCKED"},
            "flags": [f"KB_BLOCKED: {error}"],
        },
        "kb_flags": [{"type": "KB_BLOCKED", "severity": "critical", "message": error}],
    }
