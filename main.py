"""
NOVIQ Engine — FastAPI Backend v4
===================================
Phase 5 Enhancement — Intent Agent (Optional Mode)

New Features:
  1. Intent Agent (optional) — uses Claude API if ANTHROPIC_API_KEY is set
  2. Enhanced regex extraction — uses KB v4 EHR protocol as fallback
  3. Auto ACS scoring from Progress Notes
  4. Document sequence protocol enforced
  5. ACHI code assembly (Base + Modifier detection)
  6. Medical Logic KB v4 + v3 merged intelligence

Environment Variables (optional):
  ANTHROPIC_API_KEY — if set, enables Intent Agent for NLP extraction
  
Fallback Mode:
  If no API key → uses enhanced regex + KB v4 keyword matching
"""
from __future__ import annotations
import json, os, sys, uuid, warnings, re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

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

# ── Intent Agent (Optional) ───────────────────────────────────────────────
INTENT_AGENT_AVAILABLE = False
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

if ANTHROPIC_API_KEY:
    try:
        from anthropic import Anthropic
        _anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)
        INTENT_AGENT_AVAILABLE = True
        print("[OK] Intent Agent ENABLED — Claude API available")
    except Exception as e:
        print(f"[WARN] Claude API import failed: {e}")
        print("[INFO] Falling back to regex extraction")
else:
    print("[INFO] ANTHROPIC_API_KEY not set — using regex extraction")

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
app = FastAPI(title="NOVIQ Engine API", version="4.0.0")
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

def _load_kb_merged(kb_dir: Path) -> dict:
    """Load v4 (Excel-built) merged with v3 (logic + triggers). v4 wins on conflict."""
    result = {}

    # Try v3 first as base (has intelligence_triggers + medical_logic)
    for vname in ["keyword_dictionary_medical_logic_v3.json",
                  "keyword_dictionary_medical_logic_v2.json",
                  "keyword_dictionary_medical_logic_v1.json"]:
        p = kb_dir / vname
        if p.exists():
            result = json.loads(p.read_text(encoding="utf-8"))
            print(f"[OK] Base KB loaded: {vname}")
            break

    # Overlay v4 (Excel-built) — richer procedure data + EHR extraction protocol
    v4_path = kb_dir / "keyword_dictionary_medical_logic_v4.json"
    if v4_path.exists():
        v4 = json.loads(v4_path.read_text(encoding="utf-8"))
        # Merge: v4 procedures_by_specialty overrides v3 procedures
        if "procedures_by_specialty" in v4:
            result["procedures_by_specialty"] = v4["procedures_by_specialty"]
            result["procedure_index"] = v4.get("procedure_index", {})
        if "ehr_extraction_protocol" in v4:
            result["ehr_extraction_protocol"] = v4["ehr_extraction_protocol"]
        if "coding_integrity_rules" in v4:
            result["coding_integrity_rules"] = v4["coding_integrity_rules"]
        # Keep v3 intelligence_triggers and medical_logic (not in v4)
        print(f"[OK] v4 overlay applied: {len(v4.get('procedure_index',{}))} procedures + EHR protocol")

    return result

ML_KB = _load_kb_merged(KB_DIR)
_m = ML_KB.get("_meta", {})
_total = (_m.get("procedure_counts",{}).get("total",0)
          or len(ML_KB.get("procedure_index",{})))
print(f"[OK] Medical Logic KB ready — {_total} procedures | "
      f"{len(ML_KB.get('intelligence_triggers',{}))} triggers | "
      f"EHR protocol: {'yes' if 'ehr_extraction_protocol' in ML_KB else 'no'}")

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
            print("[OK] NOVIQEngine initialised")
        except Exception as e:
            print(f"[WARN] Engine init error: {e}")
            import traceback; traceback.print_exc()
    return _engine

@app.on_event("startup")
async def _startup():
    get_engine()
    mode = "LIVE" if get_engine() else "DEMO"
    extract_mode = "NLP (Claude API)" if INTENT_AGENT_AVAILABLE else "Regex + KB"
    print(f"[OK] NOVIQ v4 ready — {mode} | Extraction: {extract_mode} | KB={KB_DIR}")

# ── Dashboard ─────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root():
    for n in ["noviq_dashboard_v2.html", "noviq_dashboard.html"]:
        p = BASE_DIR / n
        if p.exists():
            return HTMLResponse(p.read_text(encoding="utf-8"))
    return HTMLResponse(
        "<h1>NOVIQ Engine v4</h1>"
        "<p>Dashboard HTML not found. Push noviq_dashboard.html to repo root.</p>"
    )

# ══════════════════════════════════════════════════════════════════════════
# INTENT AGENT — NLP Extraction (Optional)
# ══════════════════════════════════════════════════════════════════════════

def _build_intent_prompt(text: str, doc_type: str, ep: dict) -> str:
    """
    Build a document-specific extraction prompt using KB v4 EHR protocol.
    Each document type has its own extraction targets and audit flags.
    """
    # Pull per-document rules from KB v4
    ehr_proto  = ML_KB.get("ehr_extraction_protocol", {})
    doc_rules  = ehr_proto.get("extraction_by_document", {}).get(doc_type, {})
    extract    = doc_rules.get("extract", [])
    targets    = doc_rules.get("coding_targets", [])
    audit      = doc_rules.get("audit_flags", [])
    hint       = doc_rules.get("ai_prompt_hint", "")

    # Build procedure keyword context from KB v4
    proc_context = ""
    if doc_type == "Operation Notes":
        # Flatten all primary keywords from all specialties for matching
        all_procs = []
        for sp, procs in ML_KB.get("procedures_by_specialty", {}).items():
            for p in procs:
                kws  = p.get("primary_keywords", [])
                excl = p.get("exclusion_keywords", [])
                hint_p = p.get("coding_hint", "")
                all_procs.append(
                    f"  • {p['procedure']} → ACHI hint: {hint_p} | "
                    f"Keywords: {', '.join(kws[:3])} | "
                    f"Exclusions: {', '.join(excl[:2])}"
                )
        proc_context = "Known Procedure Library (match against document):\n" + "\n".join(all_procs[:60])

    # Build ACS scoring context from KB for Progress Notes
    acs_context = ""
    if doc_type == "Progress Notes":
        acs_context = """ACS 0002 Scoring Rules — apply per diagnosis found:
  C1 (+3 pts): Therapeutic treatment altered (new medication, dose change, IV therapy, insulin sliding scale)
  C2 (+3 pts): Diagnostic procedure performed (CT, MRI, blood tests, ECG, biopsy, culture)
  C3 (+2 pts): Increased level of clinical care (ICU transfer, hourly obs, continuous monitoring)
  
  Score per diagnosis:
  ≥ 5 → action: "code"
  3-4 → action: "review"  
  < 3 → action: "do_not_code"
  
  IMPORTANT: Score EACH diagnosis separately based on its OWN evidence in this document."""

    # Build PDX context for Initial/Admission/Discharge
    pdx_context = ""
    if doc_type in ("Initial Medical Report", "Admission Report", "Discharge Summary"):
        pdx_context = """PDX Selection Rules (ACS 0001):
  - PDX = the condition established AFTER study to be chiefly responsible for admission
  - NOT the presenting complaint if a different condition is found to be the cause
  - NOT a symptom if an underlying condition is found
  - If multiple conditions: select the one that consumed most resources"""

    # Already extracted data context (avoid duplication)
    existing = []
    if ep.get("pdx"): existing.append(f"PDX already found: {ep['pdx']}")
    if ep.get("adx"): existing.append(f"ADX already found: {', '.join(ep['adx'])}")
    if ep.get("achi_codes"): existing.append(f"ACHI already found: {', '.join(ep['achi_codes'])}")
    existing_ctx = "\n".join(existing) if existing else "None yet"

    prompt = f"""You are an expert Australian hospital clinical coder using ICD-10-AM (11th Ed), ACHI (12th Ed), and AR-DRG V11.0.

=== DOCUMENT TYPE: {doc_type} ===
{hint}

=== WHAT TO EXTRACT FROM THIS DOCUMENT ===
{chr(10).join(f"  • {e}" for e in extract) if extract else "  • All clinically relevant information"}

=== CODING TARGETS ===
{chr(10).join(f"  • {t}" for t in targets) if targets else "  • Apply standard ICD-10-AM coding rules"}

=== AUDIT FLAGS — Check These ===
{chr(10).join(f"  ⚠ {a}" for a in audit) if audit else "  • Standard audit"}

{pdx_context}
{acs_context}
{proc_context}

=== PREVIOUSLY EXTRACTED (do not duplicate) ===
{existing_ctx}

=== DOCUMENT TEXT ===
{text[:10000]}

=== OUTPUT INSTRUCTIONS ===
Return ONLY a valid JSON object. No markdown, no explanation, no preamble.

For "acs_adx_scores": score EACH additional diagnosis INDIVIDUALLY based on its own evidence.
For "achi_codes": format must be "00000-00" (5 digits, dash, 2 digits).
For ICD-10-AM codes: format "A00.0" — verify the code matches the documented diagnosis.
If a field has no evidence, use null or [].

{{
  "patient_age": null,
  "patient_sex": "Unknown",
  "pdx": null,
  "pdx_justification": "why this is the PDX",
  "adx": [],
  "achi_codes": [],
  "achi_assembly": [
    {{
      "procedure_name": "Laparoscopic Cholecystectomy",
      "approach": "laparoscopic",
      "base_code": "30455",
      "modifier": "00",
      "full_code": "30455-00",
      "justification": "documented in OT notes as laparoscopic approach"
    }}
  ],
  "acs_adx_scores": [
    {{
      "code": "E11.9",
      "diagnosis_name": "Type 2 Diabetes",
      "score": 5,
      "action": "code",
      "c1_therapeutic": "insulin sliding scale commenced",
      "c2_diagnostic": "HbA1c ordered",
      "c3_care": null,
      "justification": "ACS 0002: C1(3) + C2(2) = 5 — code as ADX"
    }}
  ],
  "los_days": null,
  "separation_mode": null,
  "complications": [],
  "audit_flags": [],
  "extraction_confidence": "high"
}}"""

    return prompt


def _extract_with_intent_agent(text: str, doc_type: str, ep: dict) -> dict:
    """
    Use Claude API to extract structured medical data from EHR documents.
    Uses KB v4 document-specific extraction protocol.
    Falls back gracefully on any error.
    """
    if not INTENT_AGENT_AVAILABLE:
        return ep

    try:
        prompt = _build_intent_prompt(text, doc_type, ep)

        response = _anthropic_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}]
        )

        raw = response.content[0].text.strip()

        # Strip markdown fences if present
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1] if len(parts) > 1 else raw
            if raw.startswith("json"):
                raw = raw[4:].strip()

        extracted = json.loads(raw)

        # ── Merge demographics (only if not yet found) ───────────────
        if extracted.get("patient_age") and not ep.get("patient_age"):
            ep["patient_age"] = extracted["patient_age"]
        sex = extracted.get("patient_sex", "Unknown")
        if sex != "Unknown" and ep.get("patient_sex") == "Unknown":
            ep["patient_sex"] = sex

        # ── PDX (only set from authoritative documents) ───────────────
        authoritative = ("Admission Report", "Discharge Summary", "Initial Medical Report")
        if extracted.get("pdx") and not ep.get("pdx"):
            ep["pdx"] = extracted["pdx"]
        elif extracted.get("pdx") and doc_type in authoritative:
            # Discharge Summary overrides earlier documents
            if doc_type == "Discharge Summary":
                ep["pdx"] = extracted["pdx"]
        if extracted.get("pdx_justification"):
            ep["_pdx_justification"] = extracted["pdx_justification"]

        # ── ADX — merge avoiding duplicates ───────────────────────────
        for code in extracted.get("adx", []):
            if code and code not in ep["adx"] and code != ep.get("pdx"):
                ep["adx"].append(code)

        # ── ACHI — prefer assembled codes, fallback to raw list ───────
        for asm in extracted.get("achi_assembly", []):
            full = asm.get("full_code", "")
            if full and re.match(r'\d{5}-\d{2}', full) and full not in ep["achi_codes"]:
                ep["achi_codes"].append(full)
                # Store assembly provenance
                ep.setdefault("_achi_assembly", []).append(asm)

        for code in extracted.get("achi_codes", []):
            if code and re.match(r'\d{5}-\d{2}', code) and code not in ep["achi_codes"]:
                ep["achi_codes"].append(code)

        # ── ACS Scores — per diagnosis ─────────────────────────────────
        for score_entry in extracted.get("acs_adx_scores", []):
            code = score_entry.get("code", "")
            if not code:
                continue
            # Add to ADX if not already there
            if code != ep.get("pdx") and code not in ep["adx"]:
                ep["adx"].append(code)
            # Check if already scored
            existing = next((s for s in ep.get("acs_adx_scores", [])
                             if s.get("code") == code), None)
            if not existing:
                ep.setdefault("acs_adx_scores", []).append({
                    "code":           code,
                    "score":          score_entry.get("score", 0),
                    "is_principal":   False,
                    "action":         score_entry.get("action", "review"),
                    "justification":  score_entry.get("justification", ""),
                    "score_breakdown": {
                        "therapeutic_treatment": 3 if score_entry.get("c1_therapeutic") else 0,
                        "diagnostic_procedure":  2 if score_entry.get("c2_diagnostic")  else 0,
                        "increased_clinical_care": 2 if score_entry.get("c3_care")       else 0,
                    },
                    "c1_evidence": score_entry.get("c1_therapeutic"),
                    "c2_evidence": score_entry.get("c2_diagnostic"),
                    "c3_evidence": score_entry.get("c3_care"),
                })

        # ── LOS and separation ─────────────────────────────────────────
        if extracted.get("los_days") and not ep.get("los_days"):
            ep["los_days"] = extracted["los_days"]
        if extracted.get("separation_mode") and not ep.get("separation_mode"):
            ep["separation_mode"] = extracted["separation_mode"]

        # ── Audit flags ────────────────────────────────────────────────
        for flag in extracted.get("audit_flags", []):
            ep.setdefault("_audit_flags", []).append({
                "source_doc": doc_type,
                "flag": flag,
            })

        print(f"[Intent Agent] {doc_type} → PDX:{extracted.get('pdx')} "
              f"ADX:{len(extracted.get('adx',[]))} "
              f"ACHI:{len(extracted.get('achi_codes',[]))} "
              f"ACS:{len(extracted.get('acs_adx_scores',[]))}")

        return ep

    except json.JSONDecodeError as e:
        print(f"[WARN] Intent Agent JSON parse error ({doc_type}): {e}")
        return ep
    except Exception as e:
        print(f"[WARN] Intent Agent failed ({doc_type}): {e}")
        return ep

# ══════════════════════════════════════════════════════════════════════════
# ENHANCED REGEX EXTRACTION — Uses KB v4 Protocol
# ══════════════════════════════════════════════════════════════════════════

def _extract_with_regex(ep: dict, text: str, doc_type: str) -> None:
    """Enhanced regex extraction using KB v4 EHR protocol."""
    
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

    # ACHI codes — look for both complete and partial
    for c in re.findall(r'\b(\d{5}-\d{2})\b', text):
        if c not in ep["achi_codes"]:
            ep["achi_codes"].append(c)
    
    # ACHI base codes (without modifier) — match with KB
    base_codes = re.findall(r'\b(\d{5})\b', text)
    if base_codes and doc_type == "Operation Notes":
        # Try to match with procedure index
        proc_index = ML_KB.get("procedure_index", {})
        for base in base_codes:
            # Look for keywords around the base code to determine modifier
            context = text[max(0, text.find(base)-200):text.find(base)+200].lower()
            
            # Search procedure index for this base code
            for proc_name, proc_data in proc_index.items():
                if not isinstance(proc_data, dict):
                    continue
                achi_list = proc_data.get("achi_codes", [])
                if not isinstance(achi_list, list):
                    continue
                    
                for achi_full in achi_list:
                    if achi_full.startswith(base):
                        # Check if keywords match
                        keywords = proc_data.get("primary_keywords", [])
                        exclusions = proc_data.get("exclusion_keywords", [])
                        
                        # If primary keyword found and no exclusions
                        has_primary = any(kw.lower() in context for kw in keywords) if keywords else False
                        has_exclusion = any(ex.lower() in context for ex in exclusions) if exclusions else False
                        
                        if has_primary and not has_exclusion:
                            if achi_full not in ep["achi_codes"]:
                                ep["achi_codes"].append(achi_full)
                            break

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

    # ACS evidence detection for Progress Notes
    if doc_type == "Progress Notes":
        ep.setdefault("_acs_evidence", {})
        
        # C1: Therapeutic treatment altered (3 points)
        therapeutic_patterns = [
            r'(?:commenced|started|initiated|added)\s+(?:on\s+)?(\w+)',  # New medication
            r'(?:increased|decreased|adjusted)\s+(?:dose|dosage)',       # Dose change
            r'(?:changed|switched)\s+(?:to|from)',                       # Treatment change
            r'insulin\s+(?:sliding\s+scale|infusion)',                   # Insulin management
            r'(?:iv|intravenous)\s+(?:fluids|antibiotics)',             # IV therapy
        ]
        for pattern in therapeutic_patterns:
            matches = re.findall(pattern, text, re.I)
            if matches:
                ep["_acs_evidence"].setdefault("therapeutic_treatment", []).extend(
                    [f"Treatment altered: {m}" if isinstance(m, str) else f"Treatment altered" for m in matches[:2]]
                )
        
        # C2: Diagnostic procedure (3 points)
        diagnostic_patterns = [
            r'(?:ct|mri|ultrasound|xray|x-ray)\s+(?:scan|performed|ordered)',
            r'(?:blood|lab)\s+(?:test|results|investigation)',
            r'(?:ecg|echo|ekg)\s+(?:performed|shows|ordered)',
            r'(?:biopsy|culture|pathology)\s+(?:sent|taken|performed)',
        ]
        for pattern in diagnostic_patterns:
            matches = re.findall(pattern, text, re.I)
            if matches:
                ep["_acs_evidence"].setdefault("diagnostic_procedures", []).extend(
                    [f"Investigation: {m}" if isinstance(m, str) else f"Investigation performed" for m in matches[:2]]
                )
        
        # C3: Increased clinical care (2 points)
        care_patterns = [
            r'(?:icu|intensive\s+care)\s+(?:admission|transfer)',
            r'(?:increased|frequent|continuous)\s+(?:monitoring|observations)',
            r'(?:hourly|q1h|q2h)\s+(?:obs|observations|vitals)',
        ]
        for pattern in care_patterns:
            if re.search(pattern, text, re.I):
                ep["_acs_evidence"].setdefault("increased_clinical_care", []).append(
                    "Increased level of clinical care documented"
                )
                break

# ══════════════════════════════════════════════════════════════════════════
# AUTO ACS SCORING — From collected evidence
# ══════════════════════════════════════════════════════════════════════════

def _auto_score_acs(ep: dict) -> None:
    """
    Finalise ACS scoring after all documents are processed.
    - PDX: set acs_pdx_score if not already set
    - ADX: any ADX without a score gets scored from global evidence (regex fallback)
    - Intent Agent scores (per-diagnosis) take priority
    """
    # PDX
    if ep.get("pdx") and not ep.get("acs_pdx_score"):
        ep["acs_pdx_score"] = 5

    # ADX — only score those without an existing score
    global_evidence = ep.get("_acs_evidence", {})
    scored_codes = {s["code"] for s in ep.get("acs_adx_scores", [])}

    for adx_code in ep.get("adx", []):
        if adx_code in scored_codes:
            continue  # Intent Agent already scored this

        # Fallback: score from global regex evidence
        score, breakdown, parts = 0, {}, []

        if global_evidence.get("therapeutic_treatment"):
            score += 3
            breakdown["therapeutic_treatment"] = 3
            parts.append("therapeutic treatment altered")
        if global_evidence.get("diagnostic_procedures"):
            score += 2
            breakdown["diagnostic_procedure"] = 2
            parts.append("diagnostic investigation performed")
        if global_evidence.get("increased_clinical_care"):
            score += 2
            breakdown["increased_clinical_care"] = 2
            parts.append("increased level of care")

        if score >= 5:   action = "code"
        elif score >= 3: action = "review"
        else:            action = "do_not_code"

        ep.setdefault("acs_adx_scores", []).append({
            "code":           adx_code,
            "score":          score,
            "is_principal":   False,
            "action":         action,
            "justification":  f"ACS 0002 (regex fallback) score {score}/8 — {', '.join(parts)}" if parts else f"Score {score}/8 — insufficient evidence",
            "score_breakdown": breakdown,
        })

# ══════════════════════════════════════════════════════════════════════════
# UPLOAD ENDPOINT — Enhanced with Intent Agent
# ══════════════════════════════════════════════════════════════════════════

@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...)):
    episode_id = _next_episode_id()
    ep = _empty(episode_id)
    docs, warns = [], []
    
    # Document sequence for proper reading order
    doc_sequence = {
        "Initial Medical Report": 1,
        "Admission Report": 2,
        "Progress Notes": 3,
        "Operation Notes": 4,
        "Nursing Notes": 5,
        "Discharge Summary": 6,
    }
    
    # Collect all files with metadata
    file_data = []
    for f in files:
        raw = await f.read()
        filename = f.filename or ""
        ext = Path(filename).suffix.lower()
        dtype = _doc_type(filename)
        file_data.append({
            "raw": raw,
            "filename": filename,
            "ext": ext,
            "doc_type": dtype,
            "sequence": doc_sequence.get(dtype, 99),
        })
        docs.append({"filename": filename, "doc_type": dtype,
                      "size_kb": round(len(raw)/1024, 1)})
    
    # Sort by document sequence
    file_data.sort(key=lambda x: x["sequence"])
    
    # Process in sequence
    for fd in file_data:
        try:
            text = None
            
            # Extract text based on file type
            if fd["ext"] == ".json":
                ep = _merge(ep, json.loads(fd["raw"].decode("utf-8")))
                continue
            elif fd["ext"] == ".pdf":
                text = _extract_pdf_text(fd["raw"])
            elif fd["ext"] == ".hl7":
                _parse_hl7(ep, fd["raw"].decode("utf-8", errors="ignore"))
                continue
            elif fd["ext"] == ".xml":
                _parse_fhir_xml(ep, fd["raw"].decode("utf-8", errors="ignore"))
                continue
            else:  # .txt .text .csv
                text = fd["raw"].decode("utf-8", errors="ignore")
            
            # Extract using Intent Agent if available, fallback to regex
            if text and INTENT_AGENT_AVAILABLE:
                ep = _extract_with_intent_agent(text, fd["doc_type"], ep)
            
            # Always run regex as backup/supplement
            if text:
                _extract_with_regex(ep, text, fd["doc_type"])
                
        except Exception as e:
            warns.append(f"{fd['filename']}: {e}")
            import traceback
            traceback.print_exc()

    # Auto-score ACS if evidence was collected
    _auto_score_acs(ep)
    
    # Finalize
    ep["ehr_documents"] = [d["doc_type"] for d in docs]
    if not ep.get("los_days"):
        ep["los_days"] = 1

    STORE[episode_id] = {
        "episode_dict": ep, "status": "uploaded",
        "docs_read": docs, "created_at": _now(),
        "extraction_mode": "NLP" if INTENT_AGENT_AVAILABLE else "Regex+KB"
    }
    _save(STORE)

    return {
        "episode_id":       episode_id,
        "episode_dict":     ep,
        "documents_read":   docs,
        "warnings":         warns,
        "ready_to_process": bool(ep.get("pdx")),
        "engine_mode":      "live" if ENGINE_AVAILABLE else "demo",
        "extraction_mode":  "NLP (Claude API)" if INTENT_AGENT_AVAILABLE else "Regex + KB",
    }

# ── Continue with existing endpoints (process, approve, queue, etc.) ──────

@app.post("/api/process/{episode_id}")
async def process(episode_id: str, request: Request):
    """Process episode through NOVIQ Engine pipeline."""
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
    res["episode_id"] = episode_id
    res["engine_mode"] = "demo"
    return res

@app.post("/api/approve/{episode_id}")
async def approve(episode_id: str, request: Request):
    """Physician approval gate."""
    if episode_id not in STORE:
        raise HTTPException(404, f"Episode {episode_id} not found")

    body = await request.json()
    physician_id = body.get("physician_id", "").strip()
    action = body.get("action", "approve")
    reason = body.get("reason", "")

    if not physician_id:
        raise HTTPException(400, "physician_id is required")

    if action == "approve":
        STORE[episode_id]["status"] = "APPROVED"
        STORE[episode_id]["approved_by"] = physician_id
        STORE[episode_id]["approved_at"] = _now()
        _save(STORE)
        return {
            "episode_id": episode_id,
            "status": "APPROVED",
            "approved_by": physician_id,
            "message": "Claim approved. Ready for submission.",
        }
    elif action == "reject":
        STORE[episode_id]["status"] = "REJECTED"
        STORE[episode_id]["rejected_by"] = physician_id
        STORE[episode_id]["rejected_at"] = _now()
        STORE[episode_id]["reject_reason"] = reason
        _save(STORE)
        return {
            "episode_id": episode_id,
            "status": "REJECTED",
            "reason": reason,
            "message": "Suggestion rejected. Returned for recoding.",
        }
    raise HTTPException(400, "action must be 'approve' or 'reject'")

@app.get("/api/queue")
async def get_queue():
    """Return all episodes with status."""
    queue = []
    for ep_id, data in STORE.items():
        ep = data.get("episode_dict", {})
        sug = data.get("suggestion", {})
        queue.append({
            "episode_id": ep_id,
            "patient_age": ep.get("patient_age"),
            "patient_sex": ep.get("patient_sex"),
            "pdx": ep.get("pdx"),
            "ar_drg": sug.get("proposed_codes", {}).get("ar_drg", "—"),
            "status": data.get("status", "uploaded"),
            "approved_by": data.get("approved_by"),
            "flag_count": len(data.get("kb_flags", [])),
            "processed_at": data.get("processed_at"),
            "extraction_mode": data.get("extraction_mode", "unknown"),
        })
    queue.sort(key=lambda x: x.get("processed_at") or "", reverse=True)
    return {"queue": queue, "total": len(queue)}

@app.get("/api/episode/{episode_id}")
async def get_episode(episode_id: str):
    """Return full episode detail."""
    if episode_id not in STORE:
        raise HTTPException(404, f"Episode {episode_id} not found")
    return STORE[episode_id]

@app.get("/api/kb/status")
async def kb_status():
    """Return KB health status."""
    ar_kb_path = KB_DIR / "ar_drg_kb_seed_v11_new_adrgs.json"
    ar_kb = {}
    if ar_kb_path.exists():
        with open(ar_kb_path) as f:
            ar_kb = json.load(f)

    adrgs = ar_kb.get("adrgs", {})
    
    procedure_count = len(ML_KB.get("procedure_index", {}))
    
    return {
        "ar_drg_version": ar_kb.get("_meta", {}).get("versioning", {}).get("ar_drg_version", "V11.0"),
        "adrgs_seeded": list(adrgs.keys()),
        "engine_mode": "live" if ENGINE_AVAILABLE else "demo",
        "medical_logic_kb": {
            "version": ML_KB.get("_meta", {}).get("version", "v4"),
            "total_procedures": procedure_count,
            "total": procedure_count,  # Dashboard compatibility
            "intelligence_triggers": len(ML_KB.get("intelligence_triggers", {})),
            "ehr_protocol": "yes" if ML_KB.get("ehr_extraction_protocol") else "no",
        },
        "engine_available": ENGINE_AVAILABLE,
        "intent_agent": "enabled" if INTENT_AGENT_AVAILABLE else "disabled (using regex)",
    }

@app.get("/api/health")
async def health():
    """Diagnostic endpoint with full environment info."""
    # Check KB files
    kb_files_status = {}
    critical_files = [
        "ar_drg_kb_seed_v11_new_adrgs.json",
        "dcl_exclusions.json",
        "keyword_dictionary_medical_logic_v3.json",
        "keyword_dictionary_medical_logic_v4.json",
    ]
    for fname in critical_files:
        fpath = KB_DIR / fname
        kb_files_status[fname] = {
            "exists": fpath.exists(),
            "size_kb": round(fpath.stat().st_size / 1024, 1) if fpath.exists() else 0,
        }
    
    # Check engine files
    engine_files = {}
    for fname in ["noviq_engine.py", "models.py", "validation_rules.py", "grouper.py"]:
        fpath = _ENGINE_SRC / fname
        engine_files[fname] = fpath.exists()
    
    return {
        "status": "ok",
        "base_dir": str(BASE_DIR.absolute()),
        "kb_dir": str(KB_DIR.absolute()),
        "kb_files": kb_files_status,
        "engine": {
            "available": ENGINE_AVAILABLE,
            "error": ENGINE_ERROR if not ENGINE_AVAILABLE else None,
            "source": str(_ENGINE_SRC.absolute()),
            "files": engine_files,
        },
        "intent_agent": "enabled" if INTENT_AGENT_AVAILABLE else "disabled",
        "medical_logic_kb": {
            "loaded": bool(ML_KB),
            "procedures": len(ML_KB.get("procedure_index", {})),
            "triggers": len(ML_KB.get("intelligence_triggers", {})),
        },
        "episodes_in_store": len(STORE),
        "data_dir_exists": DATA_DIR.exists(),
    }

# ══════════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _empty(episode_id: str) -> dict:
    return {
        "episode_id": episode_id,
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
        "ehr_documents": [],
    }

def _doc_type(filename: str) -> str:
    f = filename.lower()
    if "initial" in f or "er" in f: return "Initial Medical Report"
    if "admission" in f or "admit" in f: return "Admission Report"
    if "progress" in f or "daily" in f: return "Progress Notes"
    if "operation" in f or "op" in f or "surgical" in f: return "Operation Notes"
    if "nursing" in f or "nurse" in f: return "Nursing Notes"
    if "discharge" in f or "dc" in f or "summary" in f: return "Discharge Summary"
    return "EHR Document"

def _merge(base: dict, incoming: dict) -> dict:
    for k in base:
        v = incoming.get(k)
        if v is None: continue
        if isinstance(v, list) and not v: continue
        if isinstance(v, str) and not v.strip(): continue
        base[k] = v
    return base

def _extract_pdf_text(raw: bytes) -> str:
    """Extract text from PDF using pdfplumber."""
    try:
        import pdfplumber, io
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            return "\n".join(p.extract_text() or "" for p in pdf.pages)
    except Exception:
        return raw.decode("utf-8", errors="ignore")

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
    """Apply intelligence triggers from KB v3."""
    flags = []
    triggers = ML_KB.get("intelligence_triggers", {})
    
    all_text = " ".join([
        str(ep.get("pdx", "")),
        " ".join(ep.get("adx", [])),
        " ".join(ep.get("ehr_documents", [])),
    ]).lower()
    
    # Exclusion Hunter
    excl = triggers.get("exclusion_hunter", {})
    for kw in excl.get("keywords", []):
        if kw.lower() in all_text:
            flags.append({
                "trigger": "exclusion_hunter",
                "severity": "critical",
                "action": excl.get("action", "flag"),
                "message": f"EXCLUSION HUNTER: keyword '{kw}' detected — policy exclusion risk.",
            })
            break
    
    # NCV Matcher
    ncv = triggers.get("ncv_matcher", {})
    proposed_achi = sug.get("proposed_codes", {}).get("achi", [])
    cts_achi = {"90645-00", "90644-00", "90643-00"}
    if any(a in cts_achi for a in proposed_achi):
        flags.append({
            "trigger": "ncv_matcher",
            "severity": "high",
            "action": ncv.get("action", "verify"),
            "message": "NCV MATCHER: Carpal Tunnel surgery. Verify NCV/EMG Test attached.",
        })
    
    # Hardware Auditor
    hw = triggers.get("hardware_auditor", {})
    hw_achi = {"47360-00", "47330-00", "47321-00", "47480-00"}
    if any(a in hw_achi for a in proposed_achi):
        if "comminuted" not in all_text and "complex" not in all_text:
            flags.append({
                "trigger": "hardware_auditor",
                "severity": "medium",
                "action": hw.get("action", "verify"),
                "message": "HARDWARE AUDITOR: Plate fixation detected. Verify fracture complexity.",
            })
    
    return flags

def _demo(episode_id: str, ep: dict) -> dict:
    """Demo result when engine unavailable."""
    return {
        "episode_id": episode_id,
        "suggestion": {
            "episode_id": episode_id,
            "suggestion_id": str(uuid.uuid4()),
            "approval_status": "PENDING",
            "proposed_codes": {
                "pdx": ep.get("pdx", "—"),
                "adx": ep.get("adx", []),
                "achi": ep.get("achi_codes", []),
                "ar_drg": "DEMO",
                "ar_drg_desc": "Demo mode — engine not available",
            },
            "acs_scores": {
                "pdx_score": ep.get("acs_pdx_score", 0),
                "adx_scores": ep.get("acs_adx_scores", []),
            },
            "flags": ["Demo mode: NOVIQ engine not loaded"],
            "engine_version": "V11.0",
        },
    }

def _blocked(episode_id: str, ep: dict, error: str) -> dict:
    """Blocked result for KB gates."""
    return {
        "episode_id": episode_id,
        "suggestion_id": str(uuid.uuid4()),
        "approval_status": "BLOCKED",
        "proposed_codes": {
            "pdx": ep.get("pdx", ""),
            "adx": ep.get("adx", []),
            "achi": ep.get("achi_codes", []),
            "ar_drg": "BLOCKED",
            "ar_drg_desc": "Knowledge Base Incomplete",
        },
        "flags": [f"KB_BLOCKED: {error}"],
        "engine_version": "V11.0",
    }
