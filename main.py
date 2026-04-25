"""
NOVIQ Engine — FastAPI Backend v3
===================================
Fixes in this version:
  1. Correct import path: adds engine/ to sys.path BEFORE importing
  2. engine/__init__.py no longer needed
  3. DEFAULT paths in noviq_engine.py look for KB in engine/ parent = knowledge_base/
     → we pass explicit paths to override
  4. Episode sequence counter (EP-0001, EP-0002...)
  5. episode_dict returned in /process so dashboard renders patient info
  6. PDF text extraction with pdfplumber
  7. Persistent store to JSON file (survives worker restarts)
  8. /api/health returns full diagnostic
"""
from __future__ import annotations
import json, os, sys, uuid, warnings, re
from datetime import datetime, timezone
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent

# Engine: try engine/ subfolder first, then root
for _ep in [BASE_DIR / "engine", BASE_DIR]:
    if (_ep / "noviq_engine.py").exists():
        sys.path.insert(0, str(_ep))
        _ENGINE_SRC = _ep
        break
else:
    sys.path.insert(0, str(BASE_DIR))
    _ENGINE_SRC = BASE_DIR

sys.path.insert(0, str(BASE_DIR))

# KB: try knowledge_base/ subfolder first, then root
KB_DIR = next(
    (p for p in [BASE_DIR / "knowledge_base", BASE_DIR]
     if (p / "ar_drg_kb_seed_v11_new_adrgs.json").exists()),
    BASE_DIR
)

# Data directory for persistent store
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
warnings.filterwarnings("ignore")

# ── Engine import ─────────────────────────────────────────────────────────
ENGINE_AVAILABLE = False
ENGINE_ERROR = None
NOVIQEngine = None
KBIncompleteError = Exception

try:
    from noviq_engine import NOVIQEngine as _E
    from validation_rules import KnowledgeBaseIncompleteError as _KBE
    NOVIQEngine = _E
    KBIncompleteError = _KBE
    ENGINE_AVAILABLE = True
    print(f"[OK] Engine loaded from: {_ENGINE_SRC}")
except Exception as e:
    ENGINE_ERROR = str(e)
    import traceback
    print(f"[WARN] Engine import failed: {e}")
    traceback.print_exc()

# ── App ───────────────────────────────────────────────────────────────────
app = FastAPI(title="NOVIQ Engine API", version="3.0.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"],
    allow_methods=["*"], allow_headers=["*"]
)

# ── Episode counter ───────────────────────────────────────────────────────
_COUNTER_FILE = DATA_DIR / "counter.txt"

def _next_episode_id() -> str:
    n = 1
    if _COUNTER_FILE.exists():
        try: n = int(_COUNTER_FILE.read_text().strip()) + 1
        except: n = 1
    _COUNTER_FILE.write_text(str(n))
    return f"EP-{n:04d}"

# ── Persistent store ──────────────────────────────────────────────────────
_STORE_FILE = DATA_DIR / "episodes.json"

def _load() -> dict:
    if _STORE_FILE.exists():
        try: return json.loads(_STORE_FILE.read_text(encoding="utf-8"))
        except: return {}
    return {}

def _save(store: dict):
    _STORE_FILE.write_text(
        json.dumps(store, ensure_ascii=False, default=str),
        encoding="utf-8"
    )

STORE: dict = _load()

# ── Medical Logic KB ─────────────────────────────────────────────────────
ML_KB: dict = {}
for _n in ["keyword_dictionary_medical_logic_v3.json",
           "keyword_dictionary_medical_logic_v2.json",
           "keyword_dictionary_medical_logic_v1.json"]:
    _p = KB_DIR / _n
    if _p.exists():
        ML_KB = json.loads(_p.read_text(encoding="utf-8"))
        m = ML_KB.get("_meta", {})
        print(f"[OK] KB {m.get('version','?')} — "
              f"{m.get('procedure_counts',{}).get('total',0)} procs "
              f"| {m.get('intelligence_triggers',0)} triggers")
        break

# ── Engine singleton ──────────────────────────────────────────────────────
_engine = None

def get_engine():
    global _engine
    if _engine is None and ENGINE_AVAILABLE:
        try:
            _engine = NOVIQEngine(
                kb_path   = KB_DIR / "ar_drg_kb_seed_v11_new_adrgs.json",
                excl_path = KB_DIR / "dcl_exclusions.json",
            )
            # DCL table auto-loaded by grouper from knowledge_base/dcl_table_empirical.json
            print("[OK] NOVIQEngine initialised")
        except Exception as e:
            print(f"[WARN] Engine init error: {e}")
            import traceback; traceback.print_exc()
    return _engine

@app.on_event("startup")
async def _startup():
    get_engine()
    mode = "LIVE" if get_engine() else "DEMO"
    print(f"[OK] NOVIQ v3 ready — {mode} | KB={KB_DIR} | "
          f"Engine src={_ENGINE_SRC}")

# ── Dashboard ─────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root():
    for n in ["noviq_dashboard_v2.html", "noviq_dashboard.html"]:
        p = BASE_DIR / n
        if p.exists():
            return HTMLResponse(p.read_text(encoding="utf-8"))
    return HTMLResponse(
        "<h1>NOVIQ Engine v3</h1>"
        "<p>Dashboard HTML not found. Push noviq_dashboard.html to repo root.</p>"
    )

# ── Upload ────────────────────────────────────────────────────────────────
@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...)):
    episode_id = _next_episode_id()
    ep = _empty(episode_id)
    docs, warns = [], []

    for f in files:
        raw      = await f.read()
        filename = f.filename or ""
        ext      = Path(filename).suffix.lower()
        dtype    = _doc_type(filename)
        docs.append({"filename": filename, "doc_type": dtype,
                      "size_kb": round(len(raw)/1024, 1)})
        try:
            if ext == ".json":
                ep = _merge(ep, json.loads(raw.decode("utf-8")))
            elif ext == ".pdf":
                ep = _merge(ep, _extract_pdf(raw, ep, dtype))
            elif ext == ".hl7":
                _parse_hl7(ep, raw.decode("utf-8", errors="ignore"))
            elif ext == ".xml":
                _parse_fhir_xml(ep, raw.decode("utf-8", errors="ignore"))
            else:  # .txt .text .csv and anything else
                _extract_text(ep, raw.decode("utf-8", errors="ignore"), dtype)
        except Exception as e:
            warns.append(f"{filename}: {e}")

    ep["ehr_documents"] = [d["doc_type"] for d in docs]
    if not ep.get("los_days"):
        ep["los_days"] = 1

    STORE[episode_id] = {
        "episode_dict": ep, "status": "uploaded",
        "docs_read": docs, "created_at": _now()
    }
    _save(STORE)

    return {
        "episode_id":       episode_id,
        "episode_dict":     ep,
        "documents_read":   docs,
        "warnings":         warns,
        "ready_to_process": bool(ep.get("pdx")),
        "engine_mode":      "live" if ENGINE_AVAILABLE else "demo",
    }

# ── Process ───────────────────────────────────────────────────────────────
@app.post("/api/process/{episode_id}")
async def process(episode_id: str, request: Request):
    body = {}
    try: body = await request.json()
    except: pass

    store = _load()

    if body.get("episode_dict"):
        ep = body["episode_dict"]
        store.setdefault(episode_id, {})["episode_dict"] = ep
    elif episode_id in store:
        ep = store[episode_id]["episode_dict"]
    else:
        raise HTTPException(404, f"Episode {episode_id} not found. Upload a file first.")

    STORE.update(store)
    engine = get_engine()

    if engine is None:
        res = _demo(episode_id, ep)
    else:
        kb_flags, blocked = [], False
        try:
            sug = engine.process_episode(ep)
            res = sug.to_dict()
        except Exception as e:
            nm = type(e).__name__
            if "KnowledgeBaseIncomplete" in nm or "threshold" in str(e).lower():
                blocked = True
                res = _blocked(episode_id, ep, str(e))
                kb_flags.append({"type":"KB_BLOCKED","severity":"critical","message":str(e)})
            else:
                print(f"[ENGINE ERROR] {nm}: {e}")
                import traceback; traceback.print_exc()
                res = _demo(episode_id, ep)
                kb_flags.append({"type":"ENGINE_ERROR","severity":"warn","message":f"{nm}: {e}"})

        if not blocked:
            kb_flags.extend(_triggers(ep, res))

        STORE[episode_id].update({
            "suggestion":  res,
            "kb_flags":    kb_flags,
            "status":      "blocked" if blocked else "PENDING",
            "processed_at": _now(),
        })
        _save(STORE)

        return {
            "episode_id":   episode_id,
            "suggestion":   res,
            "episode_dict": ep,
            "kb_flags":     kb_flags,
            "blocked":      blocked,
            "processed_at": _now(),
            "engine_mode":  "live",
        }

    # demo path
    STORE[episode_id].update({
        "suggestion": res["suggestion"],
        "kb_flags": [],
        "status": "PENDING",
        "processed_at": _now()
    })
    _save(STORE)
    res["episode_dict"] = ep
    return res

# ── Approve ───────────────────────────────────────────────────────────────
@app.post("/api/approve/{episode_id}")
async def approve(episode_id: str, request: Request):
    store = _load()
    if episode_id not in store:
        raise HTTPException(404, "Episode not found")
    body = await request.json()
    pid    = body.get("physician_id","").strip()
    action = body.get("action","approve")
    reason = body.get("reason","")
    if not pid:
        raise HTTPException(400, "physician_id required")
    if action == "approve":
        store[episode_id].update({
            "status":"APPROVED","approved_by":pid,"approved_at":_now()})
    else:
        store[episode_id].update({
            "status":"REJECTED","approved_by":pid,
            "rejected_at":_now(),"reject_reason":reason})
    _save(store); STORE.update(store)
    return {"episode_id":episode_id,"status":store[episode_id]["status"],"approved_by":pid}

# ── Queue ─────────────────────────────────────────────────────────────────
@app.get("/api/queue")
async def queue():
    store = _load()
    rows = []
    for eid, d in store.items():
        ep = d.get("episode_dict", {})
        s  = d.get("suggestion", {})
        rows.append({
            "episode_id":   eid,
            "patient_name": ep.get("patient_name","—"),
            "patient_age":  ep.get("patient_age"),
            "patient_sex":  ep.get("patient_sex"),
            "pdx":          ep.get("pdx"),
            "ar_drg":       s.get("proposed_codes",{}).get("ar_drg","—") if isinstance(s,dict) else "—",
            "status":       d.get("status","PENDING"),
            "approved_by":  d.get("approved_by"),
            "flag_count":   len(d.get("kb_flags",[])),
            "processed_at": d.get("processed_at"),
        })
    rows.sort(key=lambda x: x.get("processed_at") or "", reverse=True)
    return {"queue": rows, "total": len(rows)}

@app.get("/api/episode/{episode_id}")
async def get_ep(episode_id: str):
    store = _load()
    if episode_id not in store:
        raise HTTPException(404)
    return store[episode_id]

@app.get("/api/kb/search")
async def kb_search(q: str = "", specialty: str = ""):
    q = q.lower(); res = []
    for sp, procs in ML_KB.get("procedures",{}).items():
        if specialty and specialty.lower() not in sp.lower(): continue
        for p in procs:
            if not q or q in p.get("procedure","").lower() or \
               any(q in k.lower() for k in p.get("keywords",[])):
                res.append(p)
    return {"query":q,"results":res[:50],"total":len(res)}

@app.get("/api/kb/status")
async def kb_status():
    ar = {}
    ar_path = KB_DIR / "ar_drg_kb_seed_v11_new_adrgs.json"
    if ar_path.exists():
        ar = json.loads(ar_path.read_text(encoding="utf-8"))
    adrgs = ar.get("adrgs",{})
    f25_t = None
    if "F25" in adrgs:
        ec = adrgs["F25"].get("split_profile",{}).get("end_classes",[])
        if ec: f25_t = ec[0].get("eccs_threshold",{}).get("value")
    ml = ML_KB.get("_meta",{}).get("procedure_counts",{})
    return {
        "ar_drg_version": ar.get("_meta",{}).get("versioning",{}).get("ar_drg_version","V11.0"),
        "engine_available": ENGINE_AVAILABLE,
        "engine_mode": "live" if ENGINE_AVAILABLE else "demo",
        "engine_src": str(_ENGINE_SRC),
        "kb_dir": str(KB_DIR),
        "adrgs_seeded": list(adrgs.keys()),
        "f25_blocked": f25_t is None,
        "medical_logic_kb": {
            "version": ML_KB.get("_meta",{}).get("version","unknown"),
            "total":   ml.get("total",0),
            "intelligence_triggers": len(ML_KB.get("intelligence_triggers",{})),
            **{k: ml.get(k,0) for k in
               ["general_surgery","hand_surgery","bariatric","breast","plastic","orthopaedic"]},
        },
    }

@app.get("/api/health")
async def health():
    return {
        "status":         "ok",
        "engine":         ENGINE_AVAILABLE,
        "engine_src":     str(_ENGINE_SRC),
        "engine_error":   ENGINE_ERROR,
        "kb_dir":         str(KB_DIR),
        "episodes_stored": len(_load()),
        "time":           _now(),
    }

# ── Helpers ───────────────────────────────────────────────────────────────
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _empty(eid: str) -> dict:
    return {
        "episode_id": eid, "patient_name": "", "patient_age": 0,
        "patient_sex": "Unknown", "pdx": "", "adx": [], "achi_codes": [],
        "los_days": 0, "same_day": False, "separation_mode": "discharge_home",
        "admission_weight": None, "hours_mech_vent": None, "care_type": "01",
        "acs_pdx_score": 0, "acs_adx_scores": [], "ehr_documents": [],
    }

def _doc_type(fn: str) -> str:
    f = fn.lower()
    if "initial" in f or "er" in f:       return "Initial Medical Report"
    if "admission" in f or "admit" in f:   return "Admission Report"
    if "progress" in f or "daily" in f:    return "Progress Notes"
    if "operation" in f or "op" in f or "surg" in f: return "Operation Notes"
    if "nursing" in f or "nurse" in f:     return "Nursing Notes"
    if "discharge" in f or "summary" in f: return "Discharge Summary"
    return "EHR Document"

def _merge(base: dict, incoming: dict) -> dict:
    for k in base:
        v = incoming.get(k)
        if v is None: continue
        if isinstance(v, list) and not v: continue
        if isinstance(v, str) and not v.strip(): continue
        base[k] = v
    return base

def _extract_pdf(raw: bytes, ep: dict, doc_type: str) -> dict:
    """Extract text from PDF using pdfplumber, fall back to raw decode."""
    text = ""
    try:
        import pdfplumber, io
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    except Exception:
        text = raw.decode("utf-8", errors="ignore")
    _extract_text(ep, text, doc_type)
    return ep

def _extract_text(ep: dict, text: str, doc_type: str) -> None:
    # Patient Name
    m = re.search(r'Patient\s*(?:Name)?:\s*([A-Z][a-zA-Z\s]{2,40})', text)
    if m and not ep.get("patient_name"):
        ep["patient_name"] = m.group(1).strip()

    # Age
    m = re.search(r'(\d{1,3})\s*(?:year[s]?\s*old|y/?o\b|Y\.O\.|years)', text, re.I)
    if m and not ep.get("patient_age"):
        ep["patient_age"] = int(m.group(1))

    # Sex
    if ep.get("patient_sex") == "Unknown":
        if re.search(r'\b(female|woman|she\b|her\b)\b', text, re.I):
            ep["patient_sex"] = "Female"
        elif re.search(r'\b(male|man|he\b|his\b)\b', text, re.I):
            ep["patient_sex"] = "Male"

    # ICD-10-AM codes
    icds = re.findall(r'\b([A-Z][0-9]{2}(?:\.[0-9A-Z]{1,2})?)\b', text)
    if icds and not ep["pdx"] and doc_type in (
            "Initial Medical Report", "Admission Report", "Discharge Summary"):
        ep["pdx"] = icds[0]
    for c in icds[1:]:
        if c not in ep["adx"] and c != ep["pdx"]:
            ep["adx"].append(c)

    # ACHI codes
    for c in re.findall(r'\b(\d{5}-\d{2})\b', text):
        if c not in ep["achi_codes"]:
            ep["achi_codes"].append(c)

    # LOS from dates
    if not ep.get("los_days"):
        adm = re.search(r'Admission\s*Date[:\s]+(\d{1,2}[-/]\d{1,2}[-/]\d{4})', text, re.I)
        dis = re.search(r'Discharge\s*Date[:\s]+(\d{1,2}[-/]\d{1,2}[-/]\d{4})', text, re.I)
        if adm and dis:
            try:
                for fmt in ["%d-%m-%Y", "%d/%m/%Y", "%m-%d-%Y", "%m/%d/%Y"]:
                    try:
                        d1 = datetime.strptime(adm.group(1), fmt)
                        d2 = datetime.strptime(dis.group(1), fmt)
                        ep["los_days"] = max(1, (d2 - d1).days)
                        break
                    except: pass
            except: pass
        # LOS in text
        m = re.search(r'(?:LOS|length of stay)[:\s]*(\d+)\s*day', text, re.I)
        if m: ep["los_days"] = int(m.group(1))

    # ACS score from text
    m = re.search(r'ACS.*?score[:\s]*(\d)', text, re.I)
    if m and not ep.get("acs_pdx_score"):
        ep["acs_pdx_score"] = int(m.group(1))

def _parse_hl7(ep: dict, text: str) -> None:
    for line in text.splitlines():
        p = line.split("|")
        if not p: continue
        if p[0] == "PID" and len(p) > 8:
            s = p[8].strip()
            if s == "F": ep["patient_sex"] = "Female"
            elif s == "M": ep["patient_sex"] = "Male"
        elif p[0] == "DG1" and len(p) > 3:
            c = p[3].strip().split("^")[0].upper()
            if re.match(r'[A-Z][0-9]{2}', c):
                if not ep["pdx"]: ep["pdx"] = c
                elif c not in ep["adx"]: ep["adx"].append(c)
        elif p[0] == "PR1" and len(p) > 3:
            c = p[3].strip()
            if re.match(r'\d{5}-\d{2}', c) and c not in ep["achi_codes"]:
                ep["achi_codes"].append(c)

def _parse_fhir_xml(ep: dict, text: str) -> None:
    for c in re.findall(r'<code value="([A-Z][0-9]{2}(?:\.[0-9A-Z]{1,2})?)"/>', text):
        if not ep["pdx"]: ep["pdx"] = c
        elif c not in ep["adx"]: ep["adx"].append(c)

def _triggers(ep: dict, sug: dict) -> list:
    flags = []
    t = ML_KB.get("intelligence_triggers", {})
    txt = " ".join([ep.get("pdx",""), " ".join(ep.get("adx",[])),
                    " ".join(ep.get("ehr_documents",[]))]).lower()
    for kw in t.get("exclusion_hunter",{}).get("keywords",[]):
        if kw.lower() in txt:
            flags.append({"trigger":"exclusion_hunter","severity":"critical",
                          "message":f"EXCLUSION HUNTER: '{kw}' detected — verify Mode of Injury."})
            break
    achi = set(sug.get("proposed_codes",{}).get("achi") or [])
    if achi & {"90645-00","90644-00","90643-00"}:
        flags.append({"trigger":"ncv_matcher","severity":"high",
                      "message":"NCV MATCHER: CTS surgery — verify NCV/EMG report attached."})
    if achi & {"47360-00","47330-00","47321-00","47480-00"}:
        if "comminuted" not in txt and "complex" not in txt:
            flags.append({"trigger":"hardware_auditor","severity":"medium",
                          "message":"HARDWARE AUDITOR: Plate & Screws — verify fracture is comminuted/complex."})
    if achi & {"48624-00","48624-01","48600-00","48603-00"}:
        flags.append({"trigger":"timing_checker","severity":"medium",
                      "message":"TIMING CHECKER: Tendon repair — verify time of injury. >24h = Delayed Repair."})
    return flags

def _demo(episode_id: str, ep: dict) -> dict:
    pdx  = ep.get("pdx","")
    achi = ep.get("achi_codes",[])
    # Map codes to DRG
    drg, desc = "960Z", "Ungroupable — no PDX or ACHI matched"
    if "96211-00" in achi or pdx == "C48.1":
        drg, desc = "G13Z", "Peritonectomy for Gastrointestinal Disorders"
    elif "35414-00" in achi:
        drg, desc = "B08B", "Endovascular Clot Retrieval, Minor Complexity"
    elif "38488-08" in achi or "38488-09" in achi:
        drg, desc = "F25—", "BLOCKED — F25 threshold requires Definitions Manual"
    elif "30515-00" in achi or pdx in ("K35.2","K35.8","K35.9"):
        drg, desc = "G01A", "Appendectomy, Minor Complexity"
    elif pdx.startswith("C") or pdx.startswith("D"):
        drg, desc = "G77Z", "Other Digestive System — Malignancy"
    elif pdx:
        drg, desc = "Y99Z", "Unclassified — PDX recognised but no ACHI matched"

    return {
        "episode_id": episode_id, "blocked": False, "demo_mode": True,
        "suggestion": {
            "episode_id":      episode_id,
            "suggestion_id":   str(uuid.uuid4()),
            "approval_status": "PENDING",
            "proposed_codes":  {
                "pdx": pdx, "adx": ep.get("adx",[]),
                "achi": achi, "ar_drg": drg, "ar_drg_desc": desc,
            },
            "acs_scores": {
                "pdx_score": ep.get("acs_pdx_score",5),
                "coding_justification": (
                    f"DEMO MODE — engine modules not loaded. "
                    f"PDX={pdx} → {drg}. "
                    f"Place engine .py files in engine/ folder for live coding."
                ),
            },
            "grouper_result": {
                "ar_drg_code": drg, "eccs": 0.0,
                "step_trace": [
                    f"[DEMO] Step 1: PDX={pdx or 'none'} | ACHI={achi}",
                    "[DEMO] Step 2: No Pre-MDC trigger",
                    "[DEMO] Step 3: MDC lookup — demo mode",
                    f"[DEMO] Step 4: Code mapping → {drg}",
                    "[DEMO] Step 5: Engine modules not loaded — place files in engine/",
                ],
            },
            "validation_result": {"summary":{"total_excluded":0,"upcoding_risk_count":0}},
            "provenance": {
                "ehr_documents_read": ep.get("ehr_documents",[]),
                "dcl_excluded_count": 0,
                "upcoding_risk_count": 0,
                "achi_trigger": f"ACHI {achi[0]} → {drg}" if achi else "No ACHI",
            },
            "flags": [f"⚠ DEMO MODE: Place noviq_engine.py, grouper.py, models.py, "
                      f"validation_rules.py, statistical_simulation.py in engine/ folder "
                      f"for live AR-DRG grouping."],
            "engine_version": "V11.0-DEMO",
        },
        "kb_flags": [],
    }

def _blocked(episode_id: str, ep: dict, error: str) -> dict:
    return {
        "episode_id":      episode_id,
        "suggestion_id":   str(uuid.uuid4()),
        "approval_status": "BLOCKED",
        "proposed_codes":  {
            "pdx": ep.get("pdx",""), "adx": ep.get("adx",[]),
            "achi": ep.get("achi_codes",[]),
            "ar_drg": "BLOCKED", "ar_drg_desc": "KB Incomplete — see flags",
        },
        "flags": [f"KB_BLOCKED: {error}"],
        "engine_version": "V11.0",
    }
