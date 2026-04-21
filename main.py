"""
NOVIQ Engine — FastAPI Backend
Railway Production-Ready
"""
from __future__ import annotations
import json, os, sys, uuid, warnings
from datetime import datetime, timezone
from pathlib import Path

# ── Path setup — works on Railway AND locally ─────────────────────────────
BASE_DIR = Path(__file__).parent

# Try engine\ subfolder first, then root (Railway flat deployment)
_engine_candidates = [BASE_DIR / "engine", BASE_DIR]
for _p in _engine_candidates:
    if (_p / "noviq_engine.py").exists():
        sys.path.insert(0, str(_p))
        break
sys.path.insert(0, str(BASE_DIR))

# Knowledge base — try knowledge_base\ subfolder, then root
_KB_CANDIDATES = [BASE_DIR / "knowledge_base", BASE_DIR]
KB_DIR = next((p for p in _KB_CANDIDATES if (p / "ar_drg_kb_seed_v11_new_adrgs.json").exists()), BASE_DIR)

from fastapi import FastAPI, File, HTTPException, Request, UploadFile, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
warnings.filterwarnings("ignore")

# ── Import engine ─────────────────────────────────────────────────────────
ENGINE_AVAILABLE = False
NOVIQEngine = None
KnowledgeBaseIncompleteError = Exception  # safe fallback

try:
    from noviq_engine import NOVIQEngine as _NOVIQEngine
    from models import APPROVAL_APPROVED, APPROVAL_PENDING, APPROVAL_REJECTED
    from validation_rules import KnowledgeBaseIncompleteError as _KBError
    NOVIQEngine = _NOVIQEngine
    KnowledgeBaseIncompleteError = _KBError
    ENGINE_AVAILABLE = True
    print("[OK] Engine modules loaded successfully")
except Exception as e:
    print(f"[WARN] Engine import failed: {e}")
    import traceback; traceback.print_exc()

# ── App setup ────────────────────────────────────────────────────────────
app = FastAPI(title="NOVIQ Engine API", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

EPISODE_STORE: dict[str, dict] = {}

# Daily episode counter for EP-YYYY-NNNN format
_episode_counter: dict[str, int] = {}

def _generate_episode_id() -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    year = datetime.now().strftime("%Y")
    if today not in _episode_counter:
        _episode_counter[today] = 0
    _episode_counter[today] += 1
    seq = _episode_counter[today]
    return f"EP-{year}-{seq:04d}"

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

# ── Engine singleton ──────────────────────────────────────────────────────
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
            import traceback; traceback.print_exc()
    return _engine

# Eager init on startup
@app.on_event("startup")
async def startup_event():
    get_engine()
    print(f"[OK] NOVIQ API ready — engine={'ON' if get_engine() else 'DEMO'} | KB={KB_DIR}")

# ── Dashboard ─────────────────────────────────────────────────────────────
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
    return HTMLResponse("<h1>NOVIQ Engine API</h1><p>Dashboard file not found in root folder.</p>")

# ── Create Episode ────────────────────────────────────────────────────────
@app.post("/api/episodes")
async def create_episode(
    patient_name: str = Form("Ahmed Al-Rashid"),
    patient_age: int = Form(58),
    patient_sex: str = Form("Female"),
    specialty: str = Form("General Surgery"),
):
    episode_id = _generate_episode_id()
    EPISODE_STORE[episode_id] = {
        "episode_id": episode_id,
        "patient_name": patient_name,
        "patient_age": patient_age,
        "patient_sex": patient_sex,
        "specialty": specialty,
        "status": "upload_pending",
        "created_at": _now(),
    }
    return {"episode_id": episode_id, "episode": EPISODE_STORE[episode_id]}

# ── Upload ────────────────────────────────────────────────────────────────
@app.post("/api/upload")
async def upload_ehr(files: list[UploadFile] = File(...)):
    episode_id   = _generate_episode_id()
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
            if ext == ".json":
                parsed = json.loads(content.decode("utf-8"))
                episode_dict = _merge(episode_dict, parsed)
            elif ext in (".txt", ".text", ".pdf"):
                _extract_text(episode_dict, content.decode("utf-8", errors="ignore"), doc_type)
            elif ext == ".hl7":
                _parse_hl7(episode_dict, content.decode("utf-8", errors="ignore"))
            elif ext == ".xml":
                _parse_fhir_xml(episode_dict, content.decode("utf-8", errors="ignore"))
            else:
                warn_list.append(f"Unsupported format: {ext}")
        except Exception as e:
            warn_list.append(f"Error parsing {filename}: {e}")

    episode_dict["ehr_documents"] = [d["doc_type"] for d in docs_read]
    EPISODE_STORE[episode_id] = {"episode_dict": episode_dict,
                                  "status": "uploaded", "docs_read": docs_read}

    return {"episode_id": episode_id, "episode_dict": episode_dict,
            "documents_read": docs_read, "warnings": warn_list,
            "ready_to_process": bool(episode_dict.get("pdx")),
            "engine_mode": "live" if ENGINE_AVAILABLE else "demo"}

# ── Process ───────────────────────────────────────────────────────────────
@app.post("/api/process/{episode_id}")
async def process_episode(episode_id: str, request: Request):
    body = {}
    try: body = await request.json()
    except: pass

    if body.get("episode_dict"):
        episode_dict = body["episode_dict"]
        EPISODE_STORE.setdefault(episode_id, {})["episode_dict"] = episode_dict
    elif episode_id in EPISODE_STORE:
        episode_dict = EPISODE_STORE[episode_id]["episode_dict"]
    else:
        raise HTTPException(404, f"Episode {episode_id} not found. Upload files first.")

    engine = get_engine()

    # ── DEMO mode ─────────────────────────────────────────────────────────
    if engine is None:
        result = _demo_result(episode_id, episode_dict)
        EPISODE_STORE[episode_id].update({
            "suggestion": result["suggestion"], "kb_flags": [],
            "status": "PENDING", "processed_at": _now()})
        return result

    # ── LIVE engine ───────────────────────────────────────────────────────
    kb_flags, blocked = [], False
    try:
        suggestion = engine.process_episode(episode_dict)
        result     = suggestion.to_dict()
    except Exception as e:
        if "KnowledgeBaseIncomplete" in type(e).__name__ or "threshold" in str(e).lower():
            blocked = True
            result  = _blocked_result(episode_id, episode_dict, str(e))
            kb_flags.append({"type": "KB_BLOCKED", "severity": "critical", "message": str(e)})
        else:
            # Don't crash — return a live result with error flag
            result  = _demo_result(episode_id, episode_dict)["suggestion"]
            kb_flags.append({"type": "ENGINE_ERROR", "severity": "warn", "message": str(e)})

    if not blocked:
        kb_flags.extend(_apply_triggers(episode_dict, result))

    EPISODE_STORE[episode_id].update({
        "suggestion": result, "kb_flags": kb_flags,
        "status": "blocked" if blocked else "PENDING",
        "processed_at": _now()})

    return {"episode_id": episode_id, "suggestion": result,
            "kb_flags": kb_flags, "blocked": blocked,
            "processed_at": _now(), "engine_mode": "live"}

# ── Approve ───────────────────────────────────────────────────────────────
@app.post("/api/approve/{episode_id}")
async def approve_episode(episode_id: str, request: Request):
    if episode_id not in EPISODE_STORE:
        raise HTTPException(404)
    body = await request.json()
    physician_id = body.get("physician_id", "").strip()
    action       = body.get("action", "approve")
    reason       = body.get("reason", "")
    if not physician_id:
        raise HTTPException(400, "physician_id required")
    store = EPISODE_STORE[episode_id]
    if action == "approve":
        store.update({"status": "APPROVED", "approved_by": physician_id, "approved_at": _now()})
        return {"episode_id": episode_id, "status": "APPROVED",
                "approved_by": physician_id, "message": "Claim approved. Ready for submission."}
    store.update({"status": "REJECTED", "approved_by": physician_id,
                  "rejected_at": _now(), "reject_reason": reason})
    return {"episode_id": episode_id, "status": "REJECTED", "reason": reason}

# ── Queue ─────────────────────────────────────────────────────────────────
@app.get("/api/queue")
async def get_queue():
    queue = []
    for ep_id, data in EPISODE_STORE.items():
        ep = data.get("episode_dict", {})
        r  = data.get("suggestion", {})
        queue.append({
            "episode_id":   ep_id,
            "patient_name": data.get("patient_name", ep.get("patient_name", "—")),
            "patient_age":  ep.get("patient_age"),
            "patient_sex":  ep.get("patient_sex"),
            "pdx":          ep.get("pdx"),
            "ar_drg":       r.get("proposed_codes", {}).get("ar_drg", "—") if isinstance(r, dict) else "—",
            "status":       data.get("status", "PENDING"),
            "approved_by":  data.get("approved_by"),
            "flag_count":   len(data.get("kb_flags", [])),
            "processed_at": data.get("processed_at"),
        })
    queue.sort(key=lambda x: x.get("processed_at") or "", reverse=True)
    return {"queue": queue, "total": len(queue)}

@app.get("/api/episode/{episode_id}")
async def get_episode(episode_id: str):
    if episode_id not in EPISODE_STORE:
        raise HTTPException(404)
    return EPISODE_STORE[episode_id]

@app.get("/api/kb/search")
async def search_kb(q: str = "", specialty: str = ""):
    q = q.lower()
    results = []
    for sp_key, procs in MEDICAL_LOGIC_KB.get("procedures", {}).items():
        if specialty and specialty.lower() not in sp_key.lower(): continue
        for proc in procs:
            if not q or q in proc.get("procedure","").lower() or \
               any(q in kw.lower() for kw in proc.get("keywords",[])):
                results.append(proc)
    return {"query": q, "results": results[:50], "total": len(results)}

@app.get("/api/kb/status")
async def kb_status():
    ar_kb_path = KB_DIR / "ar_drg_kb_seed_v11_new_adrgs.json"
    ar_kb = json.load(open(ar_kb_path)) if ar_kb_path.exists() else {}
    adrgs = ar_kb.get("adrgs", {})
    f25_threshold = None
    if "F25" in adrgs:
        classes = adrgs["F25"].get("split_profile",{}).get("end_classes",[])
        if classes: f25_threshold = classes[0].get("eccs_threshold",{}).get("value")
    ml = MEDICAL_LOGIC_KB.get("_meta",{}).get("procedure_counts",{})
    return {
        "ar_drg_version":   ar_kb.get("_meta",{}).get("versioning",{}).get("ar_drg_version","V11.0"),
        "engine_available": ENGINE_AVAILABLE,
        "engine_mode":      "live" if ENGINE_AVAILABLE else "demo",
        "adrgs_seeded":     list(adrgs.keys()),
        "f25_blocked":      f25_threshold is None,
        "kb_dir":           str(KB_DIR),
        "medical_logic_kb": {
            "version":   MEDICAL_LOGIC_KB.get("_meta",{}).get("version","unknown"),
            "total":     ml.get("total",0),
            "general_surgery": ml.get("general_surgery",0),
            "hand_surgery":    ml.get("hand_surgery",0),
            "bariatric":       ml.get("bariatric",0),
            "breast":          ml.get("breast",0),
            "plastic":         ml.get("plastic",0),
            "orthopaedic":     ml.get("orthopaedic",0),
            "intelligence_triggers": len(MEDICAL_LOGIC_KB.get("intelligence_triggers",{})),
        },
    }

@app.get("/api/health")
async def health():
    return {"status": "ok", "engine": ENGINE_AVAILABLE,
            "kb_dir": str(KB_DIR), "time": _now()}

# ── Helpers ───────────────────────────────────────────────────────────────
def _now(): return datetime.now(timezone.utc).isoformat()

def _empty_episode(eid):
    return {"episode_id": eid, "patient_age": 0, "patient_sex": "Unknown",
            "pdx": "", "adx": [], "achi_codes": [], "los_days": 0,
            "same_day": False, "separation_mode": "discharge_home",
            "admission_weight": None, "hours_mech_vent": None,
            "care_type": "01", "acs_pdx_score": 0,
            "acs_adx_scores": [], "ehr_documents": []}

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
        if v is None: continue
        if isinstance(v, list) and not v: continue
        if isinstance(v, str) and not v.strip(): continue
        base[k] = v
    return base

def _extract_text(ep, text, doc_type):
    import re
    m = re.search(r'(\d{1,3})\s*(?:year[s]?\s*old|y/?o)', text, re.I)
    if m and ep["patient_age"] == 0: ep["patient_age"] = int(m.group(1))
    if ep["patient_sex"] == "Unknown":
        if re.search(r'\b(female|woman|she|her)\b', text, re.I): ep["patient_sex"] = "Female"
        elif re.search(r'\b(male|man|he|his)\b', text, re.I):    ep["patient_sex"] = "Male"
    icd = re.findall(r'\b([A-Z][0-9]{2}(?:\.[0-9A-Z]{1,2})?)\b', text)
    if icd and not ep["pdx"] and doc_type in ("Initial Medical Report","Admission Report","Discharge Summary"):
        ep["pdx"] = icd[0]
    for c in icd[1:]:
        if c not in ep["adx"] and c != ep["pdx"]: ep["adx"].append(c)
    for c in re.findall(r'\b(\d{5}-\d{2})\b', text):
        if c not in ep["achi_codes"]: ep["achi_codes"].append(c)

def _parse_hl7(ep, text):
    import re
    for line in text.splitlines():
        parts = line.split("|")
        if not parts: continue
        if parts[0] == "PID" and len(parts) > 8:
            s = parts[8].strip()
            if s == "F": ep["patient_sex"] = "Female"
            elif s == "M": ep["patient_sex"] = "Male"
        elif parts[0] == "DG1" and len(parts) > 3:
            c = parts[3].strip().split("^")[0].upper()
            if re.match(r'[A-Z][0-9]{2}', c):
                if not ep["pdx"]: ep["pdx"] = c
                elif c not in ep["adx"]: ep["adx"].append(c)
        elif parts[0] == "PR1" and len(parts) > 3:
            c = parts[3].strip()
            if re.match(r'\d{5}-\d{2}', c) and c not in ep["achi_codes"]:
                ep["achi_codes"].append(c)

def _parse_fhir_xml(ep, text):
    import re
    for c in re.findall(r'<code value="([A-Z][0-9]{2}(?:\.[0-9A-Z]{1,2})?)"/>', text):
        if not ep["pdx"]: ep["pdx"] = c
        elif c not in ep["adx"]: ep["adx"].append(c)

def _apply_triggers(episode, suggestion):
    flags = []
    triggers = MEDICAL_LOGIC_KB.get("intelligence_triggers", {})
    all_text = " ".join([str(episode.get("pdx","")),
                         " ".join(episode.get("adx",[])),
                         " ".join(episode.get("ehr_documents",[]))]).lower()
    for kw in triggers.get("exclusion_hunter",{}).get("keywords",[]):
        if kw.lower() in all_text:
            flags.append({"trigger":"exclusion_hunter","severity":"critical",
                          "message":f"EXCLUSION HUNTER: '{kw}' — policy exclusion risk."})
            break
    achi = set(suggestion.get("proposed_codes",{}).get("achi") or [])
    if achi & {"90645-00","90644-00","90643-00"}:
        flags.append({"trigger":"ncv_matcher","severity":"high",
                      "message":"NCV MATCHER: CTS surgery — verify NCV/EMG report is attached."})
    if achi & {"47360-00","47330-00","47321-00","47480-00"} and \
       "comminuted" not in all_text and "complex" not in all_text:
        flags.append({"trigger":"hardware_auditor","severity":"medium",
                      "message":"HARDWARE AUDITOR: Plate & Screws — verify fracture is comminuted/complex."})
    if achi & {"48624-00","48624-01","48600-00","48603-00"}:
        flags.append({"trigger":"timing_checker","severity":"medium",
                      "message":"TIMING CHECKER: Tendon repair — verify time of injury. >24h = Delayed Repair."})
    return flags

def _demo_result(episode_id, ep):
    pdx   = ep.get("pdx","C48.1")
    adx   = ep.get("adx",[])
    achi  = ep.get("achi_codes",[])
    score = ep.get("acs_pdx_score",5)
    # Map known PDX/ACHI to expected DRG even in demo mode
    drg, desc = "G13Z", "Peritonectomy for Gastrointestinal Disorders"
    if "35414-00" in achi:   drg, desc = "B08B", "Endovascular Clot Retrieval, Minor Complexity"
    elif "38488-08" in achi: drg, desc = "F25—", "BLOCKED — F25 threshold requires Definitions Manual"
    return {
        "episode_id": episode_id, "blocked": False, "demo_mode": True,
        "suggestion": {
            "episode_id": episode_id,
            "suggestion_id": str(uuid.uuid4()),
            "approval_status": "PENDING",
            "proposed_codes": {"pdx":pdx,"adx":adx,"achi":achi,"ar_drg":drg,"ar_drg_desc":desc},
            "acs_scores": {"pdx_score":score,
                           "coding_justification": f"PDX {pdx} → AR-DRG {drg}. Engine running in demo mode — place engine .py files in engine/ folder."},
            "grouper_result": {"ar_drg_code":drg,"eccs":0.0,
                               "step_trace":["Step 1 PASS","Step 2: No Pre-MDC",
                                             f"Step 3: {pdx} → MDC mapped",
                                             f"Step 4: ACHI matched → ADRG",
                                             f"Step 5: → {drg}"]},
            "validation_result": {"summary":{"total_excluded":0,"upcoding_risk_count":0}},
            "provenance": {"ehr_documents_read":ep.get("ehr_documents",[]),
                           "achi_trigger": f"ACHI {achi[0]} → {drg}" if achi else "No ACHI",
                           "dcl_excluded_count":0,"upcoding_risk_count":0},
            "flags": ["ℹ DEMO MODE: Engine modules not loaded. Result based on code mapping."],
            "engine_version": "V11.0",
        },
        "kb_flags": [],
    }

def _blocked_result(episode_id, ep, error):
    return {
        "episode_id": episode_id,
        "suggestion_id": str(uuid.uuid4()),
        "approval_status": "BLOCKED",
        "proposed_codes": {"pdx":ep.get("pdx",""),"adx":ep.get("adx",[]),
                           "achi":ep.get("achi_codes",[]),
                           "ar_drg":"BLOCKED","ar_drg_desc":"KB Incomplete — purchase AR-DRG Definitions Manual"},
        "flags":[f"KB_BLOCKED: {error}"],"engine_version":"V11.0",
    }
