"""
NOVIQ Engine — Production Backend
==================================
Fixes: KB path resolution + import paths + file extraction + real engine results
"""

import os
import sys
import json
import uuid
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional
from contextlib import asynccontextmanager

# ── 0. Logging (critical for Railway debugging) ───────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("noviq")

# ── 1. Fix Python path so 'engine' package resolves ───────────────────────
# Railway runs from repo root; ensure engine/ is importable
PROJECT_ROOT = Path(__file__).parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
logger.info(f"PROJECT_ROOT: {PROJECT_ROOT}")
logger.info(f"sys.path[0]: {sys.path[0]}")

# ── 2. Verify engine package exists ───────────────────────────────────────
ENGINE_DIR = PROJECT_ROOT / "engine"
KB_DIR = PROJECT_ROOT / "knowledge_base"

logger.info(f"engine/ exists: {ENGINE_DIR.exists()}")
logger.info(f"knowledge_base/ exists: {KB_DIR.exists()}")

for f in ["noviq_engine.py", "grouper.py", "models.py", "validation_rules.py"]:
    logger.info(f"  engine/{f}: {(ENGINE_DIR / f).exists()}")

for f in ["ar_drg_kb_seed_v11_new_adrgs.json", "dcl_exclusions.json"]:
    logger.info(f"  knowledge_base/{f}: {(KB_DIR / f).exists()}")

# ── 3. Import engine with EXPLICIT KB paths (bypasses broken defaults) ───
ENGINE_AVAILABLE = False
engine_instance = None

try:
    # Absolute imports work now because PROJECT_ROOT is in sys.path
    from engine.noviq_engine import NOVIQEngine
    from engine.models import CodingSuggestion

    KB_PATH = KB_DIR / "ar_drg_kb_seed_v11_new_adrgs.json"
    EXCL_PATH = KB_DIR / "dcl_exclusions.json"

    if not KB_PATH.exists():
        raise FileNotFoundError(f"KB missing: {KB_PATH}")
    if not EXCL_PATH.exists():
        raise FileNotFoundError(f"Exclusions missing: {EXCL_PATH}")

    # Pass explicit paths — overrides the broken DEFAULT_KB_PATH inside engine/
    engine_instance = NOVIQEngine(
        kb_path=KB_PATH,
        excl_path=EXCL_PATH,
    )
    ENGINE_AVAILABLE = True
    logger.info("[OK] NOVIQEngine initialised successfully")
    logger.info(f"      KB: {KB_PATH}")
    logger.info(f"      EXCL: {EXCL_PATH}")

except Exception as e:
    ENGINE_AVAILABLE = False
    logger.error(f"[FAIL] Engine init failed: {type(e).__name__}: {e}")
    import traceback
    logger.error(traceback.format_exc())

# ── 4. FastAPI app ────────────────────────────────────────────────────────
from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

app = FastAPI(title="NOVIQ Clinical Coding Intelligence Platform")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 5. In-memory episode store (replace with DB in production) ────────────
episodes_db: dict[str, dict] = {}
episode_counter: dict[str, int] = {}  # YYYY -> count

def generate_episode_id() -> str:
    today = datetime.now()
    year = today.strftime("%Y")
    # Daily counter resets each day (simple in-memory)
    key = today.strftime("%Y-%m-%d")
    if key not in episode_counter:
        episode_counter[key] = 0
    episode_counter[key] += 1
    seq = episode_counter[key]
    return f"EP-{year}-{seq:04d}"

# ── 6. Demo result (fallback ONLY when engine unavailable) ────────────────
def _demo_result(episode_id: str, episode_dict: dict) -> dict:
    """Only used if ENGINE_AVAILABLE is False."""
    logger.warning(f"Returning DEMO result for {episode_id}")
    return {
        "episode_id": episode_id,
        "suggestion_id": str(uuid.uuid4()),
        "approval_status": "PENDING",
        "approved_by": None,
        "proposed_codes": {
            "pdx": episode_dict.get("pdx", "C48.1"),
            "adx": episode_dict.get("adx", ["E11.9", "E61.1"]),
            "achi": episode_dict.get("achi_codes", ["96211-00"]),
            "ar_drg": "G13Z",
            "ar_drg_desc": "Peritonectomy for Gastrointestinal Disorders",
        },
        "acs_scores": {
            "pdx_score": 6,
            "adx_scores": [],
            "coding_justification": "DEMO MODE — Engine not available",
        },
        "grouper_result": {
            "ar_drg_code": "G13Z",
            "ar_drg_description": "Peritonectomy for Gastrointestinal Disorders",
            "eccs": 0.0,
            "dcl_contributions": [],
            "step_trace": ["DEMO MODE"],
        },
        "validation_result": {
            "validation_status": "WARN",
            "excluded_codes": [],
            "summary": {"total_excluded": 0, "upcoding_risk_count": 0},
        },
        "provenance": {
            "ehr_documents_read": episode_dict.get("ehr_documents", []),
            "dcl_excluded_count": 0,
            "upcoding_risk_count": 0,
        },
        "flags": ["DEMO MODE: Real engine unavailable"],
        "engine_version": "DEMO",
    }

# ── 7. File extraction helper ─────────────────────────────────────────────
def extract_episode_from_files(files_info: list[dict]) -> dict:
    """
    Build an episode dict from uploaded file metadata.
    In production: parse PDF/text/HL7/FHIR here.
    For now: construct from file names + default clinical data.
    """
    episode_id = generate_episode_id()
    
    # Default episode — in production, parse actual file contents
    episode = {
        "episode_id": episode_id,
        "patient_age": 58,
        "patient_sex": "Female",
        "pdx": "C48.1",
        "adx": ["E11.9", "E61.1", "Z59.0"],
        "achi_codes": ["96211-00"],
        "los_days": 12,
        "same_day": False,
        "separation_mode": "discharge_home",
        "care_type": "01",
        "acs_pdx_score": 6,
        "ehr_documents": [f["file_name"] for f in files_info],
        "ehr_document_types": [f.get("doc_type", "Unknown") for f in files_info],
    }
    
    # Try to infer from file names (basic heuristic)
    for f in files_info:
        name = f["file_name"].lower()
        if "discharge" in name:
            episode["ehr_documents"].append("Discharge Summary")
        elif "admission" in name or "initial" in name:
            episode["ehr_documents"].append("Initial Medical Report")
        elif "operation" in name or "surgical" in name:
            episode["achi_codes"] = ["96211-00"]  # Peritonectomy
            episode["pdx"] = "C48.1"
        elif "progress" in name:
            episode["ehr_documents"].append("Progress Notes")
    
    return episode

# ── 8. API Endpoints ──────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def landing_page():
    """Serve the dashboard HTML. Place noviq_dashboard.html in repo root."""
    dashboard_path = PROJECT_ROOT / "noviq_dashboard.html"
    if dashboard_path.exists():
        return HTMLResponse(content=dashboard_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>NOVIQ Engine</h1><p>Dashboard HTML not found.</p>")

@app.get("/api/health")
async def health_check():
    return {
        "status": "ok",
        "engine_available": ENGINE_AVAILABLE,
        "kb_loaded": (KB_DIR / "ar_drg_kb_seed_v11_new_adrgs.json").exists(),
        "excl_loaded": (KB_DIR / "dcl_exclusions.json").exists(),
        "timestamp": datetime.now().isoformat(),
    }

@app.post("/api/episodes")
async def create_episode(
    patient_name: str = Form("Ahmed Al-Rashid"),
    patient_age: int = Form(58),
    patient_sex: str = Form("Female"),
    specialty: str = Form("General Surgery"),
):
    episode_id = generate_episode_id()
    episode = {
        "episode_id": episode_id,
        "patient_name": patient_name,
        "patient_age": patient_age,
        "patient_sex": patient_sex,
        "specialty": specialty,
        "created_at": datetime.now().isoformat(),
        "status": "upload_pending",
    }
    episodes_db[episode_id] = episode
    return {"episode_id": episode_id, "episode": episode}

@app.post("/api/upload/{episode_id}")
async def upload_files(
    episode_id: str,
    files: list[UploadFile] = File(...),
    doc_types: list[str] = Form(default=[]),
):
    if episode_id not in episodes_db:
        raise HTTPException(status_code=404, detail="Episode not found")
    
    files_info = []
    for i, file in enumerate(files):
        doc_type = doc_types[i] if i < len(doc_types) else "Unknown"
        files_info.append({
            "file_name": file.filename,
            "doc_type": doc_type,
            "size": len(await file.read()),
        })
        await file.seek(0)  # reset if needed later
    
    episodes_db[episode_id]["files"] = files_info
    episodes_db[episode_id]["status"] = "processing"
    
    # Build episode dict for engine
    episode_dict = extract_episode_from_files(files_info)
    episode_dict["episode_id"] = episode_id
    # Merge with patient data from create_episode
    episode_dict["patient_age"] = episodes_db[episode_id].get("patient_age", 58)
    episode_dict["patient_sex"] = episodes_db[episode_id].get("patient_sex", "Female")
    
    episodes_db[episode_id]["engine_input"] = episode_dict
    
    return {
        "episode_id": episode_id,
        "files_received": len(files_info),
        "status": "ready_for_processing",
    }

@app.post("/api/process/{episode_id}")
async def process_episode(episode_id: str):
    if episode_id not in episodes_db:
        raise HTTPException(status_code=404, detail="Episode not found")
    
    episode_dict = episodes_db[episode_id].get("engine_input")
    if not episode_dict:
        raise HTTPException(status_code=400, detail="No files uploaded for this episode")
    
    logger.info(f"Processing episode {episode_id} — Engine available: {ENGINE_AVAILABLE}")
    
    if ENGINE_AVAILABLE and engine_instance:
        try:
            result = engine_instance.process_episode_dict(episode_dict)
            result["episode_id"] = episode_id
            result["patient_name"] = episodes_db[episode_id].get("patient_name", "")
            result["specialty"] = episodes_db[episode_id].get("specialty", "")
            episodes_db[episode_id]["status"] = "completed"
            episodes_db[episode_id]["result"] = result
            logger.info(f"[OK] Real engine result for {episode_id}: {result.get('proposed_codes',{}).get('ar_drg')}")
            return result
        except Exception as err:
            logger.error(f"[ERROR] Engine failed for {episode_id}: {err}")
            import traceback
            logger.error(traceback.format_exc())
            # Fallback to demo on engine error
            result = _demo_result(episode_id, episode_dict)
            episodes_db[episode_id]["status"] = "demo_fallback"
            episodes_db[episode_id]["result"] = result
            return result
    else:
        result = _demo_result(episode_id, episode_dict)
        episodes_db[episode_id]["status"] = "demo_mode"
        episodes_db[episode_id]["result"] = result
        return result

@app.get("/api/result/{episode_id}")
async def get_result(episode_id: str):
    if episode_id not in episodes_db:
        raise HTTPException(status_code=404, detail="Episode not found")
    return episodes_db[episode_id].get("result", {})

@app.get("/api/episodes")
async def list_episodes():
    return list(episodes_db.values())

# ── 9. Run ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
