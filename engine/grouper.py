"""
NOVIQ Engine — AR-DRG V11.0 Grouper
=====================================
Phase 3, Deliverable 3.2

Architecture:  JSON-In / JSON-Out — zero UI coupling, zero DB calls.
Entry point:   ARDRGGrouper.group_episode(episode_dict) → dict
Versioning:    V11.0, Errata 1 (2023-04-01)
FHIR:          Output schema compatible with Encounter/Observation extension

Chain of Truth (runtime steps implemented here):
  Step 4 — Appendix C exclusions (unconditional, conditional, socioeconomic)
  Step 5 — DCL lookup (injected pre-computed table)
  Step 6 — ECCS = SUM[DCL_i × 0.86^(i-1)], sorted descending
  Step 7 — Threshold comparison → final DRG suffix

Source authority:
  AR-DRG V11.0 Final Report, IHACPA, January 2023
  AR-DRG V11.0 Technical Specifications, IHACPA
  Pseudocode: GROUPER_PSEUDOCODE.md (Deliverable 3.1, approved)

FastAPI wrapper (3 lines — uncomment when deploying):
  from fastapi import FastAPI
  app = FastAPI(title="NOVIQ Engine — AR-DRG V11.0 Grouper")
  @app.post("/group") async def group(episode: dict) -> dict: return grouper.group_episode(episode)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from validation_rules import (
    DCLExclusionKnowledgeBase,
    KnowledgeBaseIncompleteError,
    compute_eccs,
    compute_eccs_with_trace,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AR_DRG_VERSION   = "V11.0"
ERRATA_APPLIED   = ["Errata1_2023-04-01"]
MODULE_VERSION   = "1.0.0"

# The only PDX in V11.0 still requiring sex as a routing variable
# All others resolved in ICD-10-AM Twelfth Edition (Section 3.5.1)
SEX_ROUTING_PDX  = "R10.2"

# Error DRG codes (Step 1 exits only)
ERROR_DRG_UNGROUPABLE    = "960Z"
ERROR_DRG_BAD_PDX        = "961Z"
ERROR_DRG_NEONATAL       = "963Z"

DEFAULT_KB_PATH          = Path(__file__).parent / "ar_drg_kb_seed_v11_new_adrgs.json"
DEFAULT_EXCL_PATH        = Path(__file__).parent / "dcl_exclusions.json"


# ---------------------------------------------------------------------------
# Data models (plain dicts for JSON-In/JSON-Out compliance)
# ---------------------------------------------------------------------------

def _dcl_entry(code: str, dcl: int, is_pdx: bool,
               excluded: bool, excl_type: str | None,
               excl_reason: str | None) -> dict:
    return {
        "diagnosis_code":  code,
        "dcl_value":       dcl,
        "is_principal":    is_pdx,
        "is_excluded":     excluded,
        "exclusion_type":  excl_type,
        "exclusion_reason": excl_reason,
    }


def _error_result(episode_id: str, drg_code: str,
                  reason: str, trace: list[str]) -> dict:
    return {
        "episode_id":         episode_id,
        "ar_drg_version":     AR_DRG_VERSION,
        "ar_drg_code":        drg_code,
        "ar_drg_description": _error_description(drg_code),
        "adrg_code":          drg_code[:3],
        "mdc":                None,
        "partition":          "error",
        "grouping_status":    "ERROR",
        "eccs":               0.0,
        "dcl_contributions":  [],
        "threshold_used":     None,
        "edit_flags":         [],
        "error_code":         drg_code,
        "errata_applied":     ERRATA_APPLIED,
        "step_trace":         trace + [f"TERMINAL: {reason}"],
        "grouped_at":         _now(),
        "module_version":     MODULE_VERSION,
    }


def _error_description(drg_code: str) -> str:
    return {
        ERROR_DRG_UNGROUPABLE: "Ungroupable",
        ERROR_DRG_BAD_PDX:     "Unacceptable Principal Diagnosis",
        ERROR_DRG_NEONATAL:    "Neonatal Diagnosis Not Consistent with Age/Weight",
    }.get(drg_code, "Error")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Knowledge Base loader
# ---------------------------------------------------------------------------

class GrouperKnowledgeBase:
    """
    Loads and indexes the AR-DRG V11.0 Knowledge Base JSON.
    Injected into ARDRGGrouper at init — swappable for V12.0.
    """

    def __init__(self, kb_path: Path = DEFAULT_KB_PATH) -> None:
        if not kb_path.exists():
            raise FileNotFoundError(f"KB not found: {kb_path}")
        with open(kb_path, encoding="utf-8") as f:
            self._raw = json.load(f)
        self._adrgs: dict[str, dict] = self._raw.get("adrgs", {})
        self._version = self._raw.get("_meta", {}).get(
            "versioning", {}).get("ar_drg_version", "UNKNOWN")
        # MDC lookup loaded from KB — populated in fixed KB
        self._mdc_pdx_lookup: dict[str, str] = self._raw.get("mdc_pdx_lookup", {})

    def get_adrg(self, adrg_code: str) -> dict | None:
        return self._adrgs.get(adrg_code.upper())

    def get_description(self, drg_code: str) -> str:
        """Return DRG description from KB if available."""
        adrg_code = drg_code[:3].upper()
        suffix    = drg_code[3:].upper() if len(drg_code) > 3 else ""
        adrg      = self._adrgs.get(adrg_code)
        if not adrg:
            return drg_code
        for ec in adrg.get("split_profile", {}).get("end_classes", []):
            if ec.get("suffix", "").upper() == suffix:
                return ec.get("drg_description", drg_code)
        return adrg.get("adrg_description", drg_code)

    @property
    def version(self) -> str:
        return self._version


# ---------------------------------------------------------------------------
# DCL Table — injected dependency
# ---------------------------------------------------------------------------

class DCLTable:
    """
    Pre-computed Diagnosis Complexity Level lookup table.
    Source: Licensed grouper software (Appendix B aggregation — development-time).
    This class provides the interface; the actual table must be populated
    from licensed software or the AR-DRG V11.0 Definitions Manual.

    For testing: returns 0 for all lookups (safe default — no complexity inflation).
    For production: load from a populated dcl_table.json once licensed data is available.
    """

    PRODUCTION_READY = False  # Set True only when full table is loaded

    def __init__(self, table_path: Path | None = None) -> None:
        self._table: dict[tuple[str, str], int] = {}
        if table_path and table_path.exists():
            with open(table_path, encoding="utf-8") as f:
                raw = json.load(f)
            # Expected format: {"B08:K80.20": 3, "B08:E11.9": 2, ...}
            for key, val in raw.items():
                parts = key.split(":", 1)
                if len(parts) == 2:
                    self._table[(parts[1].upper(), parts[0].upper())] = int(val)
            self.PRODUCTION_READY = True

    def lookup(self, diagnosis_code: str, adrg_code: str) -> int:
        """
        Return pre-computed DCL (0-5) for (diagnosis, ADRG) pair.
        
        Lookup priority:
          1. ADRG-specific: ("E11.9", "G13") 
          2. Global fallback: ("E11.9", "_GLOBAL")
          3. Default: 0 (safe — no complexity inflation)
          
        DCL=0 diagnoses contribute nothing to ECCS.
        """
        dx   = diagnosis_code.strip().upper()
        adrg = adrg_code.strip().upper()
        # Priority 1: ADRG-specific
        v = self._table.get((dx, adrg))
        if v is not None:
            return v
        # Priority 2: Global fallback
        v = self._table.get((dx, "_GLOBAL"))
        if v is not None:
            return v
        return 0


# ---------------------------------------------------------------------------
# Step implementations
# ---------------------------------------------------------------------------

def _step1_edits(episode: dict) -> tuple[bool, str | None, list[str], dict]:
    """
    Step 1 — Demographic & Clinical Edits.

    Returns: (passed, error_drg_or_none, edit_flags, cleaned_episode)

    V11.0 changes:
    - Sex conflict TEST removed → warning FLAG only (Section 3.5.2)
    - Invalid ADX/ACHI stripped silently (non-fatal)
    - Error DRGs 960Z, 961Z, 963Z exit only from this step
    """
    flags = []
    ep    = dict(episode)  # work on a copy

    # 1a. Sex validation
    valid_sex = {"Male", "Female", "Other", "Unknown"}
    if ep.get("patient_sex") not in valid_sex:
        return False, ERROR_DRG_UNGROUPABLE, [], ep

    # 1b. PDX must be present
    pdx = (ep.get("pdx") or "").strip().upper()
    if not pdx:
        return False, ERROR_DRG_UNGROUPABLE, [], ep
    ep["pdx"] = pdx

    # 1c. PDX acceptability check
    # NOTE: Full valid-PDX list requires ICD-10-AM Twelfth Edition code list.
    # In production, load from licensed code list. Here we perform basic
    # format validation (alphanumeric, reasonable length).
    if not _is_plausible_icd_code(pdx):
        return False, ERROR_DRG_BAD_PDX, [], ep

    # 1d. Neonatal consistency
    if _is_neonatal_pdx(pdx):
        age = ep.get("patient_age", 0) or 0
        if age > 0:
            return False, ERROR_DRG_NEONATAL, [], ep
        weight = ep.get("admission_weight")
        if weight is not None and not _weight_consistent(pdx, weight):
            return False, ERROR_DRG_NEONATAL, [], ep

    # 1e. Strip invalid ADX (non-fatal)
    clean_adx = []
    for code in (ep.get("adx") or []):
        code = code.strip().upper()
        if _is_plausible_icd_code(code):
            clean_adx.append(code)
        else:
            flags.append(f"ADX_STRIPPED: {code} — invalid code removed")
    ep["adx"] = clean_adx

    # 1f. Strip invalid ACHI (non-fatal)
    clean_achi = []
    for code in (ep.get("achi_codes") or []):
        code = code.strip().upper()
        if _is_plausible_achi_code(code):
            clean_achi.append(code)
        else:
            flags.append(f"ACHI_STRIPPED: {code} — invalid code removed")
    ep["achi_codes"] = clean_achi

    # 1g. Sex conflict FLAG — non-blocking (V11.0: test removed, flag retained)
    for code in clean_adx + clean_achi:
        if _sex_conflicts(ep.get("patient_sex", ""), code):
            flags.append(
                f"SEX_CONFLICT_WARN: {code} conflicts with "
                f"sex={ep['patient_sex']} "
                f"[warning only — does not affect grouping in V11.0]"
            )

    # 1h. Age conflict FLAG — non-blocking
    for code in ([pdx] + clean_adx):
        if _age_conflicts(ep.get("patient_age", 0) or 0, code):
            flags.append(f"AGE_CONFLICT_WARN: {code} conflicts with age={ep.get('patient_age')}")

    return True, None, flags, ep


def _step2_pre_mdc(episode: dict,
                   pre_mdc_list: list[dict]) -> tuple[bool, str | None]:
    """
    Step 2 — Pre-MDC Override Check.

    Returns: (triggered, matched_adrg_code_or_none)

    If triggered: Step 3 is bypassed entirely.
    B08, F25, G13 are NOT Pre-MDC (confirmed V11.0 Final Report).
    """
    achi_codes = set(episode.get("achi_codes") or [])
    for adrg in sorted(pre_mdc_list, key=lambda a: a.get("hierarchy_position", 99)):
        for trigger in adrg.get("trigger_codes", []):
            if trigger.upper() in achi_codes:
                return True, adrg["adrg_code"]
    return False, None


def _step3_mdc(episode: dict,
               mdc_pdx_lookup: dict[str, str]) -> tuple[str | None, str | None]:
    """
    Step 3 — MDC Assignment via PDX.

    Returns: (mdc_or_none, error_drg_or_none)

    V11.0: R10.2 is the ONLY remaining sex-routing PDX (Section 3.5.1).
    All other sex-dependent PDX routing resolved in ICD-10-AM Twelfth Edition.
    """
    pdx = episode["pdx"]

    # Special case: R10.2 — only remaining sex-routing PDX in V11.0
    if pdx == SEX_ROUTING_PDX:
        sex = episode.get("patient_sex", "Unknown")
        if sex in ("Male", "Other", "Unknown"):
            return "12", None   # Diseases and Disorders of Male Reproductive System
        else:
            return "13", None   # Diseases and Disorders of Female Reproductive System

    # Standard MDC lookup — exact match first
    mdc = mdc_pdx_lookup.get(pdx.upper())

    # Prefix fallback: try progressively shorter prefixes
    if mdc is None:
        for length in [5, 4, 3, 2]:
            mdc = mdc_pdx_lookup.get(pdx[:length].upper())
            if mdc is not None:
                break

    if mdc is None:
        return None, ERROR_DRG_UNGROUPABLE
    return mdc, None


def _step4_adrg(episode: dict,
                mdc: str,
                kb: GrouperKnowledgeBase,
                mdc_adrg_registry: dict) -> tuple[str, str]:
    """
    Step 4 — ADRG Assignment via Intervention Hierarchy.

    Returns: (adrg_code, partition)

    Hierarchy is positional — lower number = higher priority.
    First ACHI trigger match wins. Modifier codes do NOT trigger ADRG.
    Falls back to medical partition, then ADRG 801.

    Hierarchy positions confirmed (V11.0 Final Report Table 3):
      MDC 01: B02=pos1 > B08=pos2
      MDC 05: F25=pos13
      MDC 06: G13=pos1
    """
    achi_codes = set(episode.get("achi_codes") or [])

    # Get all ADRGs for this MDC from registry (ordered list)
    mdc_adrgs = mdc_adrg_registry.get(mdc, [])
    intervention_adrgs = sorted(
        [a for a in mdc_adrgs if a.get("partition") == "intervention"],
        key=lambda a: a.get("hierarchy_position", 99)
    )

    # Walk intervention hierarchy — first ACHI match wins
    for adrg_def in intervention_adrgs:
        for trigger in adrg_def.get("trigger_codes", []):
            if trigger.upper() in achi_codes:
                return adrg_def["adrg_code"], "intervention"

    # No intervention match — medical partition via PDX
    medical_adrgs = [a for a in mdc_adrgs if a.get("partition") == "medical"]
    pdx = episode["pdx"]
    for adrg_def in medical_adrgs:
        for pdx_range in adrg_def.get("pdx_ranges", []):
            if _pdx_in_range(pdx, pdx_range):
                return adrg_def["adrg_code"], "medical"

    # Final fallback — ADRG 801 (General Interventions Unrelated to PDX)
    return "801", "medical"


def _step5_drg(episode: dict,
               adrg_code: str,
               kb: GrouperKnowledgeBase,
               excl_kb: DCLExclusionKnowledgeBase,
               dcl_table: DCLTable) -> dict:
    """
    Step 5 — DRG Assignment via ECCS.

    Implements Chain of Truth runtime steps:
      5A: Appendix C exclusions (unconditional → conditional → socioeconomic)
      5B: DCL pre-computed table lookup
      5C: ECCS = SUM[DCL_i × 0.86^(i-1)], sorted descending
      5D: Threshold comparison → DRG suffix

    Returns: step5_result dict with drg_code, eccs, dcl_contributions, threshold_used, trace
    """
    adrg = kb.get_adrg(adrg_code)
    if adrg is None:
        # Unknown ADRG (e.g. 801) — return Z suffix
        return {
            "drg_code": adrg_code + "Z",
            "eccs": 0.0,
            "dcl_contributions": [],
            "threshold_used": None,
            "trace": f"Step 5: ADRG {adrg_code} not in KB → {adrg_code}Z"
        }

    all_diagnoses = [episode["pdx"]] + list(episode.get("adx") or [])
    dcl_entries   = []

    # ── 5A: Apply Appendix C exclusions ─────────────────────────────────
    # ORDERING: unconditional → conditional → socioeconomic
    # CRITICAL: evaluate FULL diagnosis list together for conditional logic

    for diagnosis in all_diagnoses:
        is_pdx    = (diagnosis == episode["pdx"])
        co_others = [d for d in all_diagnoses if d != diagnosis]

        # Table C1 — unconditional exclusion (always DCL=0)
        if excl_kb.is_unconditionally_excluded(diagnosis):
            reason = excl_kb.get_exclusion_entry(diagnosis)
            dcl_entries.append(_dcl_entry(
                diagnosis, 0, is_pdx, True, "unconditional",
                reason.get("exclusion_reason", "Appendix C Table C1")
                if reason else "Appendix C Table C1 — unconditional exclusion"
            ))
            continue

        # Table C2 — conditional exclusion (DCL=0 only when related Dx present)
        if excl_kb.is_conditionally_excluded(diagnosis, co_others):
            dcl_entries.append(_dcl_entry(
                diagnosis, 0, is_pdx, True, "conditional",
                "Appendix C Table C2 — definitive diagnosis co-present in episode"
            ))
            continue

        # Socioeconomic / psychosocial (Z55-Z65, Z74, Z76 — excluded since V8.0)
        if excl_kb.is_previously_excluded(diagnosis):
            dcl_entries.append(_dcl_entry(
                diagnosis, 0, is_pdx, True, "socioeconomic",
                "Socioeconomic code excluded from ECC Model since AR-DRG V8.0"
            ))
            continue

        # ── 5B: DCL lookup ───────────────────────────────────────────────
        # Pre-computed (diagnosis × ADRG) → integer 0-5
        # Default 0 if not in table (no complexity inflation)
        dcl = dcl_table.lookup(diagnosis, adrg_code)
        dcl_entries.append(_dcl_entry(
            diagnosis, dcl, is_pdx, False, None, None
        ))

    # ── 5C: Compute ECCS ────────────────────────────────────────────────
    # Only non-excluded, non-zero DCL values contribute
    eligible_dcls = [
        e["dcl_value"] for e in dcl_entries
        if not e["is_excluded"] and e["dcl_value"] > 0
    ]
    eccs = compute_eccs(eligible_dcls)

    # ── 5D: Assign DRG suffix ────────────────────────────────────────────
    split_profile = adrg.get("split_profile", {})

    # Case 1: Unsplit ADRG (Z suffix) — G13 in current KB
    if split_profile.get("profile") == "Z":
        drg_code = adrg_code + "Z"
        return {
            "drg_code":       drg_code,
            "eccs":           eccs,
            "dcl_contributions": dcl_entries,
            "threshold_used": None,
            "trace": (
                f"Step 5: ADRG {adrg_code} is unsplit → {drg_code} "
                f"(ECCS={eccs} computed for clinical reporting)"
            )
        }

    # Case 2: Administrative split (LOS, age, separation mode)
    # Small number of ADRGs use admin variables instead of / in addition to ECCS
    if split_profile.get("has_administrative_split"):
        drg_code = _apply_administrative_split(episode, adrg, adrg_code)
        if drg_code:
            return {
                "drg_code":       drg_code,
                "eccs":           eccs,
                "dcl_contributions": dcl_entries,
                "threshold_used": None,
                "trace": f"Step 5: Administrative split → {drg_code}"
            }

    # Case 3: ECCS-based split (standard case for A/B, A/B/C, A/B/C/D)
    # Walk end classes from highest complexity (lowest cost_rank) downward
    end_classes = sorted(
        split_profile.get("end_classes", []),
        key=lambda ec: ec.get("cost_rank", 99)
    )

    for end_class in end_classes:
        threshold_obj = end_class.get("eccs_threshold") or {}
        threshold_val = threshold_obj.get("value")

        # Production gate — null threshold raises hard error
        # Fires for F25 until Definitions Manual is purchased
        if threshold_val is None and end_class.get("suffix") != end_classes[-1].get("suffix"):
            raise KnowledgeBaseIncompleteError(
                f"ECCS threshold for {adrg_code} "
                f"(suffix {end_class.get('suffix')}) is null in Knowledge Base. "
                f"Populate from AR-DRG V11.0 Definitions Manual "
                f"(Lane Print: ar-drg.laneprint.com.au). "
                f"NOVIQ Engine cannot assign {adrg_code} DRG until resolved."
            )

        if threshold_val is not None and eccs >= threshold_val:
            drg_code = adrg_code + end_class["suffix"]
            return {
                "drg_code":       drg_code,
                "eccs":           eccs,
                "dcl_contributions": dcl_entries,
                "threshold_used": threshold_val,
                "trace": (
                    f"Step 5: ECCS={eccs} >= threshold {threshold_val} "
                    f"→ {drg_code}"
                )
            }

    # Fallback — lowest complexity suffix (no lower bound condition)
    lowest   = end_classes[-1]
    drg_code = adrg_code + lowest["suffix"]
    return {
        "drg_code":       drg_code,
        "eccs":           eccs,
        "dcl_contributions": dcl_entries,
        "threshold_used": lowest.get("eccs_threshold", {}).get("value"),
        "trace": (
            f"Step 5: ECCS={eccs} below all thresholds "
            f"→ {drg_code} (lowest complexity)"
        )
    }


# ---------------------------------------------------------------------------
# Administrative split helper (stub — expand per Definitions Manual)
# ---------------------------------------------------------------------------

def _apply_administrative_split(episode: dict, adrg: dict,
                                 adrg_code: str) -> str | None:
    """
    Apply administrative DRG split based on LOS, age, or separation mode.
    Used by a small number of ADRGs (e.g. B70D — transferred < 5 days).
    Expand per Definitions Manual per-ADRG definition sections.
    """
    admin_rules = adrg.get("split_profile", {}).get("administrative_rules", [])
    for rule in admin_rules:
        variable = rule.get("variable")
        if variable == "los_days":
            if episode.get("los_days", 0) < rule.get("threshold"):
                return adrg_code + rule.get("suffix")
        elif variable == "separation_mode":
            if episode.get("separation_mode") == rule.get("value"):
                return adrg_code + rule.get("suffix")
        elif variable == "age":
            if episode.get("patient_age", 0) < rule.get("threshold"):
                return adrg_code + rule.get("suffix")
    return None


# ---------------------------------------------------------------------------
# Validation helpers (stubs — replace with ICD-10-AM code list in production)
# ---------------------------------------------------------------------------

def _is_plausible_icd_code(code: str) -> bool:
    """Basic format check. Production: validate against ICD-10-AM Twelfth Edition list."""
    if not code or len(code) < 3 or len(code) > 8:
        return False
    return code[0].isalpha() and code[1:3].isdigit()


def _is_plausible_achi_code(code: str) -> bool:
    """Basic format check. Production: validate against ACHI Twelfth Edition list."""
    if not code:
        return False
    # ACHI format: NNNNN-NN [NNNN] e.g. 35414-00 or 38488-08
    return any(c.isdigit() for c in code)


def _is_neonatal_pdx(pdx: str) -> bool:
    """True if PDX is a neonatal code (P00-P99 or Z38.x)."""
    return pdx.startswith("P") or pdx.startswith("Z38")


def _weight_consistent(pdx: str, weight_grams: int) -> bool:
    """Basic neonatal weight consistency. Expand per Appendix D."""
    return weight_grams > 0


def _sex_conflicts(sex: str, code: str) -> bool:
    """
    Sex conflict check — FLAG only in V11.0 (test removed per Section 3.5.2).
    Minimal implementation: production requires Appendix D sex conflict tables.
    """
    return False  # Stub — populate from Definitions Manual Appendix D


def _age_conflicts(age: int, code: str) -> bool:
    """
    Age conflict check — FLAG only, non-blocking.
    Production requires aligned age edits from ICD-10-AM ECLs (Section 3.6).
    """
    return False  # Stub — populate from Definitions Manual Appendix D


def _pdx_in_range(pdx: str, pdx_range: str) -> bool:
    """Check if PDX falls within a range string e.g. 'K80-K87'."""
    if "-" in pdx_range:
        start, end = pdx_range.split("-", 1)
        return start.upper() <= pdx.upper() <= end.upper() + "Z"
    return pdx.upper().startswith(pdx_range.upper())


# ---------------------------------------------------------------------------
# Main grouper class
# ---------------------------------------------------------------------------

class ARDRGGrouper:
    """
    AR-DRG V11.0 Grouper — JSON-In / JSON-Out.

    Usage:
        grouper = ARDRGGrouper()
        result  = grouper.group_episode(episode_dict)

    Injected dependencies (swappable for testing or V12.0):
        kb          — GrouperKnowledgeBase  (AR-DRG seed JSON)
        excl_kb     — DCLExclusionKnowledgeBase (dcl_exclusions.json)
        dcl_table   — DCLTable (pre-computed DCL lookup)
        pre_mdc_list — list of Pre-MDC ADRG definitions
        mdc_pdx_lookup — dict mapping ICD-10-AM code → MDC number
        mdc_adrg_registry — dict mapping MDC → list of ADRG definitions
    """

    def __init__(
        self,
        kb_path:    Path = DEFAULT_KB_PATH,
        excl_path:  Path = DEFAULT_EXCL_PATH,
        dcl_table:  DCLTable | None = None,
        pre_mdc_list: list[dict] | None = None,
        mdc_pdx_lookup: dict[str, str] | None = None,
        mdc_adrg_registry: dict[str, list] | None = None,
    ) -> None:

        self.kb       = GrouperKnowledgeBase(kb_path)
        self.excl_kb  = DCLExclusionKnowledgeBase(excl_path)
        # Auto-load empirical DCL table if no explicit table provided
        if dcl_table is not None:
            self.dcl_table = dcl_table
        else:
            # Try empirical table first, then fall back to empty
            _emp_paths = [
                kb_path.parent / "dcl_table_empirical.json",
                kb_path.parent.parent / "knowledge_base" / "dcl_table_empirical.json",
                kb_path.parent.parent / "dcl_table_empirical.json",
            ]
            _loaded = False
            for _p in _emp_paths:
                if _p.exists():
                    self.dcl_table = DCLTable(_p)
                    if self.dcl_table.PRODUCTION_READY:
                        print(f"[OK] Empirical DCL table loaded: {_p.name}")
                        _loaded = True
                        break
            if not _loaded:
                self.dcl_table = DCLTable()

        # These must be populated from Definitions Manual for production use.
        # Stubs here allow engine to run for the ADRGs seeded in the KB.
        self.pre_mdc_list      = pre_mdc_list or []
        # Auto-load MDC lookup from KB JSON if not explicitly provided
        self.mdc_pdx_lookup    = mdc_pdx_lookup or self.kb._mdc_pdx_lookup or {}
        self.mdc_adrg_registry = mdc_adrg_registry or self._build_registry_from_kb()

    def _build_registry_from_kb(self) -> dict[str, list]:
        """
        Build a minimal MDC→ADRG registry from the seeded KB ADRGs.
        Production: replace with full Definitions Manual Appendix A data.
        """
        registry: dict[str, list] = {}
        for adrg_code, adrg_def in self.kb._adrgs.items():
            mdc = adrg_def.get("mdc")
            if not mdc:
                continue
            if mdc not in registry:
                registry[mdc] = []
            registry[mdc].append({
                "adrg_code":        adrg_code,
                "partition":        adrg_def.get("partition", "intervention"),
                "hierarchy_position": adrg_def.get("hierarchy", {}).get("position", 99),
                "trigger_codes":    [
                    t["achi_code"]
                    for t in adrg_def.get("trigger_codes", [])
                    if t.get("role") == "trigger"
                ],
                "pdx_ranges":       adrg_def.get("pdx_ranges", []),
            })
        return registry

    # ------------------------------------------------------------------
    # Primary entry point
    # ------------------------------------------------------------------

    def group_episode(self, episode: dict) -> dict:
        """
        PRIMARY ENTRY POINT — JSON-In / JSON-Out.

        Args:
            episode: EpisodeRecord dict (see INPUT_SCHEMA)

        Returns:
            GrouperResult dict (FHIR-compatible, see OUTPUT_SCHEMA)

        Raises:
            KnowledgeBaseIncompleteError: if required KB value is null (e.g. F25 threshold)
        """
        episode_id = episode.get("episode_id", "UNKNOWN")
        trace: list[str] = []

        # ── STEP 1: Demographic & Clinical Edits ─────────────────────────
        passed, err_drg, edit_flags, episode = _step1_edits(episode)
        if not passed:
            return _error_result(episode_id, err_drg,
                                 f"Step 1 edit failure → {err_drg}", trace)
        trace.append(
            f"Step 1 PASS — PDX={episode['pdx']}, "
            f"{len(episode.get('adx',[]))} ADX, "
            f"{len(episode.get('achi_codes',[]))} ACHI codes validated"
        )

        # ── STEP 2: Pre-MDC Override ──────────────────────────────────────
        pre_triggered, pre_adrg = _step2_pre_mdc(episode, self.pre_mdc_list)
        if pre_triggered:
            trace.append(
                f"Step 2: Pre-MDC trigger → ADRG {pre_adrg} "
                f"— MDC assignment bypassed"
            )
            r5 = _step5_drg(episode, pre_adrg, self.kb,
                            self.excl_kb, self.dcl_table)
            trace.append(r5["trace"])
            return self._build_result(
                episode_id, r5, mdc=None,
                partition="pre_mdc", edit_flags=edit_flags, trace=trace
            )
        trace.append("Step 2: No Pre-MDC trigger → MDC assignment proceeds")

        # ── STEP 3: MDC Assignment ────────────────────────────────────────
        mdc, err_drg = _step3_mdc(episode, self.mdc_pdx_lookup)
        if err_drg:
            return _error_result(episode_id, err_drg,
                                 f"Step 3 MDC lookup failed for PDX={episode['pdx']}",
                                 trace)

        # R10.2 sex-routing note in trace
        if episode["pdx"] == SEX_ROUTING_PDX:
            trace.append(
                f"Step 3: R10.2 sex-routing → "
                f"sex={episode.get('patient_sex')} → MDC {mdc}"
            )
        else:
            trace.append(f"Step 3: PDX={episode['pdx']} → MDC {mdc}")

        # ── STEP 4: ADRG Assignment ───────────────────────────────────────
        adrg_code, partition = _step4_adrg(
            episode, mdc, self.kb, self.mdc_adrg_registry
        )
        trace.append(
            f"Step 4: ADRG={adrg_code} ({partition})"
        )

        # ── STEP 5: DRG Assignment ────────────────────────────────────────
        r5 = _step5_drg(episode, adrg_code, self.kb,
                        self.excl_kb, self.dcl_table)
        trace.append(r5["trace"])

        return self._build_result(
            episode_id, r5, mdc=mdc,
            partition=partition, edit_flags=edit_flags, trace=trace
        )

    # ------------------------------------------------------------------
    # Result builder
    # ------------------------------------------------------------------

    def _build_result(self, episode_id: str, r5: dict,
                      mdc: str | None, partition: str,
                      edit_flags: list[str], trace: list[str]) -> dict:
        drg_code = r5["drg_code"]
        return {
            "episode_id":         episode_id,
            "ar_drg_version":     AR_DRG_VERSION,
            "ar_drg_code":        drg_code,
            "ar_drg_description": self.kb.get_description(drg_code),
            "adrg_code":          drg_code[:3],
            "mdc":                mdc,
            "partition":          partition,
            "grouping_status":    "SUCCESS",
            "eccs":               r5["eccs"],
            "dcl_contributions":  r5["dcl_contributions"],
            "threshold_used":     r5["threshold_used"],
            "edit_flags":         edit_flags,
            "error_code":         None,
            "errata_applied":     ERRATA_APPLIED,
            "step_trace":         trace,
            "grouped_at":         _now(),
            "module_version":     MODULE_VERSION,
        }


# ---------------------------------------------------------------------------
# Input / Output schemas (documentation)
# ---------------------------------------------------------------------------

INPUT_SCHEMA = {
    "episode_id":       "string",
    "patient_age":      "integer — years",
    "patient_sex":      "Male | Female | Other | Unknown",
    "admission_weight": "integer | null — grams, neonates only",
    "same_day":         "boolean",
    "separation_mode":  "string",
    "los_days":         "integer",
    "pdx":              "string — principal ICD-10-AM code",
    "adx":              ["list of additional ICD-10-AM codes"],
    "achi_codes":       ["list of ACHI codes"],
    "hours_mech_vent":  "integer | null",
    "care_type":        "string — 01=Acute 07=Newborn 11=MentalHealth",
}

OUTPUT_SCHEMA = {
    "episode_id":         "string",
    "ar_drg_version":     "V11.0",
    "ar_drg_code":        "string — e.g. B08A, G13Z",
    "ar_drg_description": "string",
    "adrg_code":          "string — e.g. B08",
    "mdc":                "string | null",
    "partition":          "intervention | medical | pre_mdc | error",
    "grouping_status":    "SUCCESS | ERROR | WARNING",
    "eccs":               "float",
    "dcl_contributions":  [{"diagnosis_code": "str", "dcl_value": "int",
                            "is_principal": "bool", "is_excluded": "bool",
                            "exclusion_type": "str|null",
                            "exclusion_reason": "str|null"}],
    "threshold_used":     "float | null",
    "edit_flags":         ["list of warning strings"],
    "error_code":         "string | null",
    "errata_applied":     ["list of errata identifiers"],
    "step_trace":         ["full audit trail — one entry per step"],
    "grouped_at":         "ISO 8601 UTC timestamp",
    "module_version":     "string",
}


# ---------------------------------------------------------------------------
# Smoke test — run directly: python grouper.py
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json

    grouper = ARDRGGrouper()

    # ── Test A: G13Z — Peritonectomy (unsplit, no ECCS threshold needed) ─
    test_g13 = {
        "episode_id":    "TEST-G13-001",
        "patient_age":   58,
        "patient_sex":   "Female",
        "admission_weight": None,
        "same_day":      False,
        "separation_mode": "discharge_home",
        "los_days":      12,
        "pdx":           "C48.1",
        "adx":           ["E11.9", "I10", "E61.1", "Z59.0"],
        "achi_codes":    ["96211-00"],   # Peritonectomy — G13 trigger
        "hours_mech_vent": None,
        "care_type":     "01",
    }

    # ── Test B: B08 — Endovascular Clot Retrieval ─────────────────────────
    # Manually inject MDC lookup and ADRG registry for B08
    mdc_lookup   = {"I63.3": "01", "I63.4": "01", "C48.1": "06"}
    mdc_registry = {
        "01": [{
            "adrg_code": "B08",
            "partition": "intervention",
            "hierarchy_position": 2,
            "trigger_codes": ["35414-00"],
            "pdx_ranges": [],
        }],
        "06": [{
            "adrg_code": "G13",
            "partition": "intervention",
            "hierarchy_position": 1,
            "trigger_codes": ["96211-00"],
            "pdx_ranges": [],
        }]
    }

    grouper_with_lookup = ARDRGGrouper(
        mdc_pdx_lookup    = mdc_lookup,
        mdc_adrg_registry = mdc_registry,
    )

    test_b08 = {
        "episode_id":    "TEST-B08-001",
        "patient_age":   72,
        "patient_sex":   "Male",
        "admission_weight": None,
        "same_day":      False,
        "separation_mode": "discharge_home",
        "los_days":      4,
        "pdx":           "I63.3",
        "adx":           ["I10", "E11.9"],
        "achi_codes":    ["35414-00"],   # ECR — B08 trigger
        "hours_mech_vent": None,
        "care_type":     "01",
    }

    test_g13_full = {**test_g13}

    print("=" * 60)
    print("NOVIQ Engine — grouper.py smoke test")
    print("=" * 60)

    for label, episode, grpr in [
        ("G13Z — Peritonectomy (unsplit)",  test_g13_full,  grouper_with_lookup),
        ("B08  — Endovascular Clot Retrieval", test_b08,    grouper_with_lookup),
    ]:
        result = grpr.group_episode(episode)
        print(f"\nTest: {label}")
        print(f"  DRG:        {result['ar_drg_code']}")
        print(f"  ECCS:       {result['eccs']}")
        print(f"  Threshold:  {result['threshold_used']}")
        print(f"  Status:     {result['grouping_status']}")
        print(f"  DCL entries: {len(result['dcl_contributions'])}")
        excl = [e for e in result['dcl_contributions'] if e['is_excluded']]
        print(f"  Excluded:   {len(excl)} ({[e['diagnosis_code'] for e in excl]})")
        print(f"  Trace:")
        for t in result['step_trace']:
            print(f"    → {t}")

    # ── Test C: 961Z — Bad PDX ───────────────────────────────────────────
    test_bad_pdx = {
        "episode_id": "TEST-961Z",
        "patient_age": 45, "patient_sex": "Female",
        "admission_weight": None, "same_day": False,
        "separation_mode": "discharge_home", "los_days": 2,
        "pdx": "INVALID!!", "adx": [], "achi_codes": [],
        "hours_mech_vent": None, "care_type": "01",
    }
    result_bad = grouper.group_episode(test_bad_pdx)
    print(f"\nTest: 961Z — Invalid PDX")
    print(f"  DRG:    {result_bad['ar_drg_code']}")
    print(f"  Status: {result_bad['grouping_status']}")
    print(f"  Error:  {result_bad['error_code']}")

    # ── Test D: R10.2 sex-routing ─────────────────────────────────────────
    test_r102_male = {
        "episode_id": "TEST-R102-M",
        "patient_age": 35, "patient_sex": "Male",
        "admission_weight": None, "same_day": False,
        "separation_mode": "discharge_home", "los_days": 1,
        "pdx": "R10.2", "adx": [], "achi_codes": [],
        "hours_mech_vent": None, "care_type": "01",
    }
    grp_r102 = ARDRGGrouper(mdc_pdx_lookup={"R10.2": "12"})
    result_r102 = grp_r102.group_episode(test_r102_male)
    print(f"\nTest: R10.2 sex-routing (Male)")
    print(f"  MDC assigned: {result_r102['mdc']} (expected: 12)")
    print(f"  Trace: {result_r102['step_trace'][2]}")

    print("\n✓ All smoke tests complete")
