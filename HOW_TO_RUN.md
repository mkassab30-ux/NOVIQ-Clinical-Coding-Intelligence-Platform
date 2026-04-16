# NOVIQ Engine — How to Run the MVP Locally

## Prerequisites
- Python 3.10 or higher
- Git (repo already cloned)

---

## Step 1 — Set up the folder structure

Your repo should look like this after the GitHub push:

```
NOVIQ-Clinical-Coding-Intelligence-Platform/
├── engine/
│   ├── noviq_engine.py
│   ├── grouper.py
│   ├── validation_rules.py
│   ├── models.py
│   └── statistical_simulation.py
├── knowledge_base/
│   ├── ar_drg_kb_seed_v11_new_adrgs.json
│   ├── dcl_exclusions.json
│   ├── keyword_dictionary_v11_new_adrgs.json
│   └── keyword_dictionary_medical_logic_v1.json
├── tests/
│   ├── test_grouper.py
│   └── test_pipeline.py
├── docs/
│   └── GROUPER_PSEUDOCODE.md
├── main.py                  ← FastAPI backend (NEW)
├── noviq_dashboard.html     ← Dashboard UI (NEW)
├── sample_episode.json      ← Test episode (NEW)
├── requirements.txt         ← Dependencies (NEW)
└── README.md
```

---

## Step 2 — Install dependencies

```bash
# In the project root folder
pip install -r requirements.txt
```

This installs: fastapi, uvicorn, python-multipart, aiofiles, pandas, openpyxl, pdfplumber

---

## Step 3 — Run the server

```bash
uvicorn main:app --reload --port 8000
```

You will see:
```
INFO:     Started server process
INFO:     Uvicorn running on http://127.0.0.1:8000
[OK] Medical Logic KB loaded: 41 GS + 38 HS procedures
[OK] NOVIQEngine initialised
```

---

## Step 4 — Open the dashboard

Open your browser and go to:
```
http://localhost:8000
```

The NOVIQ dashboard will open.

---

## Step 5 — Test with the sample episode

1. Click **Upload EHR** in the sidebar
2. Click **Choose Files** and select `sample_episode.json`
3. Click **▶ Run NOVIQ Engine**
4. Watch the 4 agents process the episode
5. Review the Coding Suggestion (G13Z expected)
6. Click **✓ Approve & Submit** — enter your physician ID

---

## Step 6 — Test with real EHR files

The engine accepts:

| Format | What it reads |
|--------|--------------|
| `.json` | Pre-structured EpisodeRecord (like sample_episode.json) |
| `.txt`  | Free clinical text — extracts ICD codes, ACHI codes, patient age/sex |
| `.hl7`  | HL7 v2 messages — reads PID, DG1, PR1 segments |
| `.xml`  | FHIR R4 XML — reads Condition and Procedure resources |
| `.pdf`  | Extracts text then applies keyword matching |

You can upload multiple files at once (e.g., admission.txt + opnotes.txt + discharge.pdf).

---

## API Endpoints (for testing or HMIS integration)

| Method | Endpoint | What it does |
|--------|----------|-------------|
| GET  | `/` | Serves the dashboard |
| POST | `/api/upload` | Upload EHR files → returns PatientEpisode JSON |
| POST | `/api/process/{episode_id}` | Run full NOVIQ pipeline |
| POST | `/api/approve/{episode_id}` | Physician approve/reject |
| GET  | `/api/queue` | All episodes with status |
| GET  | `/api/episode/{id}` | Full episode detail |
| GET  | `/api/kb/search?q=cholecystectomy` | Search Medical Logic KB |
| GET  | `/api/kb/status` | Knowledge Base health check |

### Quick test with curl:

```bash
# Upload a file
curl -X POST http://localhost:8000/api/upload \
  -F "files=@sample_episode.json"

# Process the returned episode_id
curl -X POST http://localhost:8000/api/process/EP-2026-SAMPLE-001

# Approve
curl -X POST http://localhost:8000/api/approve/EP-2026-SAMPLE-001 \
  -H "Content-Type: application/json" \
  -d '{"physician_id": "DR-KASSAB-001", "action": "approve"}'

# Check queue
curl http://localhost:8000/api/queue

# Search KB
curl "http://localhost:8000/api/kb/search?q=cholecystectomy"
```

---

## Known limitations (MVP)

1. **F25 ECCS threshold is null** — episodes with TAVI/TAVR will raise `KnowledgeBaseIncompleteError`. This is by design (production gate). Fix: purchase AR-DRG Definitions Manual from Lane Print.

2. **DCL table is stub** — all ECCS values = 0.0. DRG suffix defaults to lowest complexity (B08B not B08A, etc.). Fix: license grouper software.

3. **EHR text extraction is keyword-based** — works for structured text and JSON. Phase 5 Intent Agent (LLM-based) will replace this for unstructured free text.

4. **In-memory storage** — episodes are lost on server restart. For production, replace `EPISODE_STORE` dict in `main.py` with a PostgreSQL database.

---

## Next steps to full production

1. Purchase AR-DRG Definitions Manual → unblock F25 + populate DCL table
2. Deploy to cloud (Docker → AWS/Azure) → share URL with hospital pilot
3. Build Phase 5 Intelligence Layer → LLM-powered Intent Agent
4. Add FHIR R4 proper adapter → auto-connect to any HMIS
5. Replace in-memory store → PostgreSQL

---

*NOVIQ Engine — AR-DRG V11.0 · 4,804 lines · 63 tests · All green*
*Dr. Mohamed Kassab / Noviq Health 2026*
