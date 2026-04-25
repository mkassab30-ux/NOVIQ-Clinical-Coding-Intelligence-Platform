"""
NOVIQ Engine — FastAPI Backend v3.3 (Clean & Stable)
===================================================
Optimized for Railway + Dashboard
"""

from __future__ import annotations
import json, sys, uuid, warnings
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

warnings.filterwarnings("ignore")

# ====================== PATHS ======================
BASE_DIR = Path(__file__).parent
KB_DIR = BASE_DIR / "knowledge_base"
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# Add engine to path
sys.path.insert(0, str(BASE_DIR / "engine"))
sys.path.insert(0, str(BASE_DIR))

# ====================== IMPORT ENGINE ======================
ENGINE_AVAILABLE = False
NOVIQEngine = None

try:
    from noviq_engine import NOVIQEngine
    ENGINE_AVAILABLE = True
    print("[OK] NOVIQ Engine imported successfully")
except Exception as e:
    print(f"[WARN] Engine import failed: {e}")

# ====================== APP ======================
app = FastAPI(title="NOVIQ Engine", version="3.3")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

STORE = {}
def _next_id(): 
    n = len(STORE) + 1
    return f"EP-{n:04d}"

def _now():
    return datetime.now(timezone.utc).isoformat()

# ====================== HELPERS ======================
def _empty_episode(eid):
    return {
        "episode_id": eid, "patient_age": 0, "patient_sex": "Unknown",
        "pdx": "", "adx": [], "achi_codes": [], "los_days": 0,
        "ehr_documents": []
    }

def _doc_type(filename: str):
    f = filename.lower()
    if "initial" in f or "er" in f: return "Initial Medical Report"
    if "admission" in f: return "Admission Report"
    if "progress" in f: return "Progress Notes"
    if "operation" in f or "op" in f: return "Operation Notes"
    if "nursing" in f: return "Nursing Notes"
    if "discharge" in f: return "Discharge Summary"
    return "EHR Document"

# ====================== ROUTES ======================
@app.get("/", response_class=HTMLResponse)
async def dashboard():
    for name in ["noviq_dashboard_v2.html", "noviq_dashboard.html"]:
        p = BASE_DIR / name
        if p.exists():
            return HTMLResponse(p.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>NOVIQ Engine</h1><p>Dashboard not found</p>")

@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...)):
    episode_id = _next_id()
    ep = _empty_episode(episode_id)

    for f in files:
        content = await f.read()
        fname = f.filename or ""
        ext = Path(fname).suffix.lower()
        dtype = _doc_type(fname)

        try:
            if ext == ".json":
                data = json.loads(content.decode("utf-8"))
                ep.update({k: v for k, v in data.items() if v})
            else:
                text = content.decode("utf-8", errors="ignore")
                # simple extraction
                if not ep.get("pdx"):
                    import re
                    codes = re.findall(r'\b([A-Z]\d{2}(?:\.\d+)?)\b', text)
                    if codes:
                        ep["pdx"] = codes[0]
        except:
            pass

    STORE[episode_id] = {"episode_dict": ep, "status": "uploaded"}
    return {"episode_id": episode_id, "episode_dict": ep, "ready_to_process": bool(ep.get("pdx"))}

@app.post("/api/process")
@app.post("/api/process/{episode_id}")
async def process(episode_id: str = None, request: Request = None):
    body = await request.json() if request else {}

    if body.get("episode_dict"):
        ep = body["episode_dict"]
    elif episode_id and episode_id in STORE:
        ep = STORE[episode_id]["episode_dict"]
    else:
        ep = body

    if not ep or not ep.get("pdx"):
        raise HTTPException(400, "Missing pdx. Upload first.")

    if not ENGINE_AVAILABLE:
        return {"episode_id": episode_id, "suggestion": {"ar_drg": "G13Z", "status": "DEMO"}, "demo_mode": True}

    try:
        engine = NOVIQEngine(
            kb_path=KB_DIR / "ar_drg_kb_seed_v11_new_adrgs.json",
            excl_path=KB_DIR / "dcl_exclusions.json"
        )
        suggestion = engine.process_episode(ep)
        result = suggestion.to_dict()
        return {"episode_id": episode_id or "EP-0001", "suggestion": result}
    except Exception as e:
        raise HTTPException(500, f"Processing error: {str(e)}")

@app.get("/api/kb/status")
async def kb_status():
    return {
        "engine_available": ENGINE_AVAILABLE,
        "engine_mode": "live" if ENGINE_AVAILABLE else "demo",
        "message": "Ready"
    }

print("✅ main.py loaded successfully - v3.3 Clean Version")
