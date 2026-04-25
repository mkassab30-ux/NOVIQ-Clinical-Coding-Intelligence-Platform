"""
NOVIQ Engine — FastAPI Backend v3.1 (FINAL FIX)
===============================================
Fixes:
  - Added @app.post("/api/process") without {episode_id} to fix "Method Not Allowed"
  - Better episode_dict handling from body or store
  - Improved error messages and logging
  - Supports both /api/process and /api/process/{id}
"""

from __future__ import annotations
import json, os, sys, uuid, warnings, re
from datetime import datetime, timezone
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent

# Add engine/ to path
for _ep in [BASE_DIR / "engine", BASE_DIR]:
    if (_ep / "noviq_engine.py").exists():
        sys.path.insert(0, str(_ep))
        _ENGINE_SRC = _ep
        break
else:
    sys.path.insert(0, str(BASE_DIR))
    _ENGINE_SRC = BASE_DIR

# KB directory
KB_DIR = next(
    (p for p in [BASE_DIR / "knowledge_base", BASE_DIR]
     if (p / "ar_drg_kb_seed_v11_new_adrgs.json").exists()),
    BASE_DIR
)

# Data directory
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
warnings.filterwarnings("ignore")

# ── Engine Import ────────────────────────────────────────────────────────
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
app = FastAPI(title="NOVIQ Engine API", version="3.1.0")
app.add_middleware(
    CORSMiddleware, 
    allow_origins=["*"],
    allow_methods=["*"], 
    allow_headers=["*"]
)

# ── Episode Counter & Persistent Store ───────────────────────────────────
_COUNTER_FILE = DATA_DIR / "counter.txt"
_STORE_FILE = DATA_DIR / "episodes.json"

def _next_episode_id() -> str:
    n = 1
    if _COUNTER_FILE.exists():
        try:
            n = int(_COUNTER_FILE.read_text().strip()) + 1
        except:
            n = 1
    _COUNTER_FILE.write_text(str(n))
    return f"EP-{n:04d}"

def _load() -> dict:
    if _STORE_FILE.exists():
        try:
            return json.loads(_STORE_FILE.read_text(encoding="utf-8"))
        except:
            return {}
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
        print(f"[OK] Medical Logic KB {m.get('version','?')} loaded — "
              f"{m.get('procedure_counts',{}).get('total',0)} procedures")
        break

# ── Engine Singleton ─────────────────────────────────────────────────────
_engine = None

def get_engine():
    global _engine
    if _engine is None and ENGINE_AVAILABLE:
        try:
            _engine = NOVIQEngine(
                kb_path=KB_DIR / "ar_drg_kb_seed_v11_new_adrgs.json",
                excl_path=KB_DIR / "dcl_exclusions.json",
            )
            print("[OK] NOVIQEngine initialized successfully")
        except Exception as e:
            print(f"[WARN] Engine init failed: {e}")
    return _engine

@app.on_event("startup")
async def _startup():
    get_engine()
    mode = "LIVE" if ENGINE_AVAILABLE else "DEMO"
    print(f"[OK] NOVIQ Engine v3.1 ready — {mode} mode | KB={KB_DIR}")

# ── Dashboard ─────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root():
    for name in ["noviq_dashboard_v2.html", "noviq_dashboard.html"]:
        p = BASE_DIR / name
        if p.exists():
            return HTMLResponse(p.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>NOVIQ Engine v3.1</h1><p>Dashboard HTML not found.</p>")

# ── Upload ────────────────────────────────────────────────────────────────
@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...)):
    episode_id = _next_episode_id()
    ep = _empty(episode_id)
    docs, warns = [], []

    for f in files:
        raw = await f.read()
        filename = f.filename or ""
        ext = Path(filename).suffix.lower()
        dtype = _doc_type(filename)
        docs.append({"filename": filename, "doc_type": dtype, "size_kb": round(len(raw)/1024, 1)})

        try:
            if ext == ".json":
                ep = _merge(ep, json.loads(raw.decode("utf-8")))
            elif ext == ".pdf":
                ep = _merge(ep, _extract_pdf(raw, ep, dtype))
            elif ext == ".hl7":
                _parse_hl7(ep, raw.decode("utf-8", errors="ignore"))
            elif ext == ".xml":
                _parse_fhir_xml(ep, raw.decode("utf-8", errors="ignore"))
            else:
                _extract_text(ep, raw.decode("utf-8", errors="ignore"), dtype)
        except Exception as e:
            warns.append(f"{filename}: {str(e)}")

    ep["ehr_documents"] = [d["doc_type"] for d in docs]
    if not ep.get("los_days"):
        ep["los_days"] = 1

    STORE[episode_id] = {
        "episode_dict": ep,
        "status": "uploaded",
        "docs_read": docs,
        "created_at": _now()
    }
    _save(STORE)

    return {
        "episode_id": episode_id,
        "episode_dict": ep,
        "documents_read": docs,
        "warnings": warns,
        "ready_to_process": bool(ep.get("pdx")),
        "engine_mode": "live" if ENGINE_AVAILABLE else "demo"
    }

# ── FIXED PROCESS ENDPOINT ───────────────────────────────────────────────
@app.post("/api/process")
@app.post("/api/process/{episode_id}")
async def process_episode(episode_id: str = None, request: Request = None):
    """Fixed endpoint that works with both /api/process and /api/process/{id}"""
    body = {}
    try:
        body = await request.json()
    except:
        pass

    # Get episode_id from URL or body
    if not episode_id and body.get("episode_id"):
        episode_id = body.get("episode_id")

    # Get episode data
    if body.get("episode_dict"):
        episode_dict = body["episode_dict"]
    elif episode_id and episode_id in STORE:
        episode_dict = STORE[episode_id].get("episode_dict", {})
    else:
        episode_dict = body  # fallback: whole body is the episode

    if not episode_dict or not episode_dict.get("pdx"):
        raise HTTPException(400, "Missing 'pdx' field. Upload EHR files first.")

    engine = get_engine()
    if engine is None:
        result = _demo(episode_id or "EP-DEMO", episode_dict)
        return {"episode_id": episode_id, "suggestion": result["suggestion"], "demo_mode": True}

    # Live processing
    kb_flags = []
    blocked = False
    try:
        suggestion = engine.process_episode(episode_dict)
        result = suggestion.to_dict()
        kb_flags = _triggers(episode_dict, result)
    except Exception as e:
        blocked = True
        result = _blocked(episode_id or "EP-ERROR", episode_dict, str(e))
        kb_flags.append({"type": "ENGINE_ERROR", "severity": "critical", "message": str(e)})

    # Save
    eid = episode_id or _next_episode_id()
    STORE[eid] = {
        "episode_dict": episode_dict,
        "suggestion": result,
        "kb_flags": kb_flags,
        "status": "blocked" if blocked else "PENDING",
        "processed_at": _now()
    }
    _save(STORE)

    return {
        "episode_id": eid,
        "suggestion": result,
        "episode_dict": episode_dict,
        "kb_flags": kb_flags,
        "blocked": blocked,
        "processed_at": _now(),
        "engine_mode": "live"
    }

# ── Approve ───────────────────────────────────────────────────────────────
@app.post("/api/approve/{episode_id}")
async def approve(episode_id: str, request: Request):
    store = _load()
    if episode_id not in store:
        raise HTTPException(404, "Episode not found")
    body = await request.json()
    pid = body.get("physician_id", "").strip()
    action = body.get("action", "approve")
    reason = body.get("reason", "")
    if not pid:
        raise HTTPException(400, "physician_id required")
    if action == "approve":
        store[episode_id].update({"status": "APPROVED", "approved_by": pid, "approved_at": _now()})
    else:
        store[episode_id].update({"status": "REJECTED", "approved_by": pid,
                                  "rejected_at": _now(), "reject_reason": reason})
    _save(store)
    STORE.update(store)
    return {"episode_id": episode_id, "status": store[episode_id]["status"], "approved_by": pid}

# باقي الـ endpoints (queue, episode, kb/status, health) تبقى زي ما هي
# (انسخها من الكود القديم بتاعك)

# ── Helpers (keep the rest of your helper functions) ─────────────────────
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
