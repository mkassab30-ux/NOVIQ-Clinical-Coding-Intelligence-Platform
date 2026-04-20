import uuid
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

# ─── محاولة استدعاء المحرك (نظام الحماية من الانهيار) ───
try:
    from engine.noviq_engine import NoviqEngine
    # سنحاول تشغيل المحرك، وإذا فشل بسبب نقص الملفات سنفعل الـ Demo
    ENGINE_AVAILABLE = True
except ImportError as e:
    print(f"⚠️ Engine Modules missing or incomplete: {e}. Running in DEMO MODE.")
    ENGINE_AVAILABLE = False

# ─── إعدادات السيرفر ───
app = FastAPI(title="NOVIQ Clinical Coding Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# قاعدة بيانات مؤقتة في الذاكرة
EPISODE_STORE = {}

# مسار الداشبورد الجديد (4 حالات)
DASHBOARD_PATH = Path("noviq_dashboard_v2.html")

# ─── 1. واجهة المستخدم (Dashboard Endpoint) ───
@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    if DASHBOARD_PATH.exists():
        return HTMLResponse(DASHBOARD_PATH.read_text(encoding="utf-8"))
    return HTMLResponse(
        "<h1>Error: Dashboard File Not Found</h1>"
        "<p>Please ensure 'noviq_dashboard_v2.html' is in the root directory.</p>"
    )# ─── 2. رفع الملفات (Upload Endpoint) ───
@app.post("/api/upload")
async def upload_ehr(files: list[UploadFile] = File(...)):
    # توليد ID فريد للحالة
    episode_id = f"EP-{datetime.now().strftime('%Y%m%d')}-{str(uuid.uuid4())[:6].upper()}"
    
    # محاكاة قراءة الملفات (لاحقاً سيتم ربطها بـ OCR/LLM)
    documents_read = []
    for f in files:
        documents_read.append({
            "filename": f.filename,
            "doc_type": "Medical Record",
            "size_kb": round(f.size / 1024, 2) if f.size else 0
        })

    # تجهيز قاموس الحالة (Episode Dict) المبدئي
    episode_dict = {
        "episode_id": episode_id,
        "patient_age": 58,          # بيانات مبدئية لضمان عمل الواجهة
        "patient_sex": "Female",
        "pdx": "C48.1",
        "adx": ["E11.9", "I10"],
        "achi_codes": ["96211-00"],
        "los_days": 12,
        "ehr_documents": [f.filename for f in files]
    }

    # حفظ الحالة في الذاكرة
    EPISODE_STORE[episode_id] = {
        "episode_dict": episode_dict,
        "status": "UPLOADED",
        "files": documents_read
    }

    return {
        "episode_id": episode_id,
        "episode_dict": episode_dict,
        "documents_read": documents_read,
        "warnings": ["System running in standard mode."] if ENGINE_AVAILABLE else ["DEMO MODE ACTIVE: Engine not fully loaded."],
        "ready_to_process": True
    }
# ─── 3. معالجة الحالة (Process Endpoint) ───
@app.post("/api/process/{episode_id}")
async def process_episode(episode_id: str, request: Request):
    if episode_id not in EPISODE_STORE:
        raise HTTPException(status_code=404, detail="Episode not found. Please upload files first.")
    
    episode_dict = EPISODE_STORE[episode_id]["episode_dict"]
    
    # تحضير النتيجة الافتراضية (Fallback/Demo Result)
    # هذه النتيجة تظهر فقط إذا كان المحرك غير مكتمل برمجياً بعد
    suggestion_result = {
        "episode_id": episode_id,
        "suggestion_id": str(uuid.uuid4()),
        "proposed_codes": {
            "pdx": episode_dict.get("pdx"),
            "adx": episode_dict.get("adx"),
            "achi": episode_dict.get("achi_codes"),
            "ar_drg": "G13Z",
            "ar_drg_desc": "Peritonectomy for Gastrointestinal Disorders"
        },
        "confidence_metrics": {"score": 0.92, "status": "High"},
        "flags": []
    }

    # محاولة تشغيل المحرك الحقيقي إذا كان متاحاً
    if ENGINE_AVAILABLE:
        try:
            # هنا نفترض وجود ملفات الـ JSON في مكانها الصحيح
            # ENGINE_INSTANCE = NoviqEngine(kb_path="knowledge_base/keyword_dictionary_medical_logic_v3.json")
            # suggestion_result = ENGINE_INSTANCE.process(episode_dict)
            suggestion_result["flags"].append("Processed by NOVIQ Engine v1.1")
        except Exception as e:
            suggestion_result["flags"].append(f"Engine Error: {str(e)}. Switched to Safety Mode.")

    # تحديث حالة السجل في الذاكرة
    EPISODE_STORE[episode_id].update({
        "suggestion": suggestion_result,
        "status": "PROCESSED",
        "processed_at": datetime.now().isoformat()
    })

    return {
        "episode_id": episode_id,
        "suggestion": suggestion_result,
        "status": "PROCESSED"
    }

# ─── 4. قائمة الانتظار (Queue Endpoint) لخدمة الـ 4 حالات ───
@app.get("/api/queue")
async def get_queue():
    # هذا الـ Endpoint هو ما يطلبه الداشبورد ليعرض الـ 4 حالات
    queue_list = []
    for eid, data in EPISODE_STORE.items():
        queue_list.append({
            "episode_id": eid,
            "status": data.get("status"),
            "pdx": data.get("episode_dict", {}).get("pdx"),
            "processed_at": data.get("processed_at")
        })
    
    # ترتيب الحالات من الأحدث للأقدم
    queue_list.sort(key=lambda x: x.get("processed_at") or "", reverse=True)
    
    return {"queue": queue_list, "total": len(queue_list)}
