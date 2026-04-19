"""
NOVIQ Engine — MVP Backend (Enhanced Version)
===========================================
- Unified Episode Handling
- Medical Logic KB V3 Integration
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
from typing import Any, List

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

# ── Setup Paths ───────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
KB_DIR = BASE_DIR / "knowledge_base"
warnings.filterwarnings("ignore")

app = FastAPI(title="NOVIQ Engine API", version="1.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Memory Store ──────────────────────────────────────────────────────────
EPISODE_STORE: dict[str, dict] = {}

# ── Load Medical Logic KB V3 ──────────────────────────────────────────────
MEDICAL_LOGIC_KB: dict = {}
KB_FILE_NAME = "keyword_dictionary_medical_logic_v3.json"
kb_path = KB_DIR / KB_FILE_NAME

if kb_path.exists():
    try:
        with open(kb_path, encoding="utf-8") as f:
            MEDICAL_LOGIC_KB = json.load(f)
        _meta = MEDICAL_LOGIC_KB.get("_meta", {})
        print(f"[OK] NOVIQ Knowledge Base Loaded: v{_meta.get('version')} | {len(MEDICAL_LOGIC_KB.get('procedures', {}))} Specialties detected.")
    except Exception as e:
        print(f"[ERROR] Failed to parse KB JSON: {e}")
else:
    print(f"[WARN] KB File not found at {kb_path}")

# ══════════════════════════════════════════════════════════════════════════
# ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    dash_path = BASE_DIR / "noviq_dashboard.html"
    if dash_path.exists():
        return HTMLResponse(dash_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>NOVIQ Engine</h1><p>Dashboard not found.</p>")

@app.post("/api/upload")
async def upload_ehr(files: List[UploadFile] = File(...)):
    """يرفع مجموعة ملفات ويدمجها في حالة مريض واحدة (Episode)"""
    # 1. إنشاء معرف موحد لهذه المجموعة
    episode_id = f"EP-{datetime.now().strftime('%Y%m%d')}-{str(uuid.uuid4())[:6].upper()}"
    episode_dict = _build_empty_episode(episode_id)
    docs_info = []

    for upload in files:
        content = await upload.read()
        filename = upload.filename or "unknown.txt"
        doc_type = _infer_doc_type(filename)
        
        # تحويل المحتوى لنص
        text = content.decode("utf-8", errors="ignore")
        
        # استخراج البيانات الأساسية والأكواد
        _extract_from_text(episode_dict, text, doc_type)
        
        # مطابقة المنطق الطبي من الـ KB الموحد
        _match_medical_logic(episode_dict, text)

        docs_info.append({"filename": filename, "type": doc_type})

    # تخزين الحالة في الذاكرة
    EPISODE_STORE[episode_id] = {
        "episode_dict": episode_dict,
        "status": "uploaded",
        "docs": docs_info
    }

    return {
        "episode_id": episode_id,
        "episode_dict": episode_dict,
        "files_processed": len(files),
        "ready": bool(episode_dict["pdx"])
    }

# ══════════════════════════════════════════════════════════════════════════
# HELPERS (Logic & Extraction)
# ══════════════════════════════════════════════════════════════════════════

def _build_empty_episode(eid: str) -> dict:
    return {
        "episode_id": eid,
        "patient_age": 0,
        "patient_sex": "Unknown",
        "pdx": "", # Diagnosis
        "adx": [], # Additional Diagnoses
        "achi_codes": [], # Procedures
        "detected_logic": [], # القواعد الطبية المستخرجة
        "ehr_documents": []
    }

def _match_medical_logic(episode: dict, text: str):
    """يبحث عن العمليات والقواعد في ملف الـ JSON الموحد"""
    text_lower = text.lower()
    procedures_db = MEDICAL_LOGIC_KB.get("procedures", {})
    
    for specialty, procs in procedures_db.items():
        for p in procs:
            keywords = p.get("keywords", [])
            if any(kw.lower() in text_lower for kw in keywords):
                logic_entry = {
                    "procedure": p.get("procedure"),
                    "specialty": specialty,
                    "coding_rule": p.get("medical_coding_logic"),
                    "ai_trigger": p.get("ai_triggers")
                }
                if logic_entry not in episode["detected_logic"]:
                    episode["detected_logic"].append(logic_entry)

def _extract_from_text(episode: dict, text: str, doc_type: str):
    """استخراج الأكواد والبيانات باستخدام Regex"""
    # استخراج ICD-10 (مثل K35.8)
    icds = re.findall(r'\b([A-Z][0-9]{2}(?:\.[0-9A-Z]{1,2})?)\b', text)
    if icds:
        if not episode["pdx"]: episode["pdx"] = icds[0]
        episode["adx"].extend([c for c in icds[1:] if c not in episode["adx"]])

    # استخراج ACHI (مثل 30645-00)
    achis = re.findall(r'\b(\d{5}-\d{2})\b', text)
    episode["achi_codes"].extend([a for a in achis if a not in episode["achi_codes"]])

    # استخراج العمر
    age_m = re.search(r'(\d{1,3})\s*(?:year|y/o|yo)', text, re.I)
    if age_m and episode["patient_age"] == 0:
        episode["patient_age"] = int(age_m.group(1))

def _infer_doc_type(filename: str) -> str:
    f = filename.lower()
    if "op" in f or "surg" in f: return "Operation Notes"
    if "dis" in f or "sum" in f: return "Discharge Summary"
    if "init" in f or "admit" in f: return "Admission Report"
    return "Clinical Note"
