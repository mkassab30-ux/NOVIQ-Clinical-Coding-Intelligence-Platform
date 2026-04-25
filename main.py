"""
NOVIQ Engine — FastAPI Backend v3.4 (Fixed for Dashboard)
====================================================
"""

from __future__ import annotations
import json, sys, uuid, warnings
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

warnings.filterwarnings("ignore")

BASE_DIR = Path(__file__).parent
KB_DIR = BASE_DIR / "knowledge_base"

sys.path.insert(0, str(BASE_DIR / "engine"))
sys.path.insert(0, str(BASE_DIR))

# Engine Import
ENGINE_AVAILABLE = False
NOVIQEngine = None

try:
    from noviq_engine import NOVIQEngine
    ENGINE_AVAILABLE = True
    print("[OK] Engine loaded successfully")
except Exception as e:
    print(f"[WARN] Engine failed to load: {e}")

app = FastAPI(title="NOVIQ Engine", version="3.4")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

STORE = {}

def _now():
    return datetime.now(timezone.utc).isoformat()

def _next_episode_id():
    return f"EP-{len(STORE)+1:04d}"

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    for name in ["noviq_dashboard_v2.html", "noviq_dashboard.html"]:
        p = BASE_DIR / name
        if p.exists():
            return HTMLResponse(p.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>NOVIQ Engine v3.4</h1><p>Dashboard HTML not found</p>")

@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...)):
    episode_id = _next_episode_id()
    ep = {
        "episode_id": episode_id,
        "patient_age": 58,
        "patient_sex": "Male",
        "pdx": "C48.1",
        "adx": ["E11.9"],
        "achi_codes": ["96211-00"],
        "los_days": 12,
        "ehr_documents": []
    }

    for f in files:
        content = await f.read()
        fname = f.filename or ""
        if fname.endswith(".json"):
            try:
                data = json.loads(content.decode("utf-8"))
                ep.update(data)
            except:
                pass

    STORE[episode_id] = {"episode_dict": ep, "status": "uploaded"}
    return {
        "episode_id": episode_id,
        "episode_dict": ep,
        "ready_to_process": True
    }

# FIXED PROCESS ENDPOINT
@app.post("/api/process")
@app.post("/api/process/{episode_id}")
async def process_episode(episode_id: str = None, request: Request = None):
    try:
        body = await request.json()
    except:
        body = {}

    # Get episode data
    if body.get("episode_dict"):
        episode_dict = body["episode_dict"]
    elif episode_id and episode_id in STORE:
        episode_dict = STORE[episode_id]["episode_dict"]
    else:
        episode_dict = body

    if not episode_dict or not episode_dict.get("pdx"):
        raise HTTPException(400, "Missing pdx field. Please upload a valid episode.")

    if not ENGINE_AVAILABLE:
        return {
            "episode_id": episode_id or "EP-DEMO",
            "suggestion": {
                "ar_drg": "G13Z",
                "ar_drg_desc": "Peritonectomy for Gastrointestinal Disorders",
                "approval_status": "PENDING"
            },
            "demo_mode": True
        }

    try:
        engine = NOVIQEngine(
            kb_path=KB_DIR / "ar_drg_kb_seed_v11_new_adrgs.json",
            excl_path=KB_DIR / "dcl_exclusions.json"
        )
        suggestion = engine.process_episode(episode_dict)
        result = suggestion.to_dict()

        eid = episode_id or _next_episode_id()
        STORE[eid] = {"episode_dict": episode_dict, "suggestion": result, "status": "PENDING"}

        return {
            "episode_id": eid,
            "suggestion": result,
            "status": "success"
        }
    except Exception as e:
        raise HTTPException(500, f"Processing failed: {str(e)}")

@app.get("/api/kb/status")
async def kb_status():
    return {
        "engine_available": ENGINE_AVAILABLE,
        "engine_mode": "live" if ENGINE_AVAILABLE else "demo",
        "kb_procedures": "152"
    }

print("✅ NOVIQ main.py v3.4 loaded successfully")
