"""
NOVIQ Engine — Validation Rules Module
=======================================
Phase 2, Deliverable 2.3

Purpose:
    Validates a patient episode's ICD-10-AM diagnosis codes against the
    AR-DRG V11.0 DCL Exclusion Knowledge Base. Identifies codes that cannot
    receive a Diagnosis Complexity Level (DCL), flags upcoding risk, and
    returns a fully provenance-traced ValidationResult JSON.

Architecture:
    JSON-In / JSON-Out — zero UI coupling, zero DB calls.
    Accepts a PatientEpisode dict, returns a ValidationResult dict.
    All Knowledge Base data loaded from dcl_exclusions.json at init.
    Ready to wrap with FastAPI in 3 lines (see bottom of file).

Interface Contract:
    Input:  PatientEpisode  (see INPUT_SCHEMA)
    Output: ValidationResult (see OUTPUT_SCHEMA)

AR-DRG Reference:
    AR-DRG V11.0 Final Report, IHACPA, January 2023
    Section 3.7.1 — Diagnoses in-scope for receiving a DCL
    Appendix A, Table A5 — Codes newly excluded from V11.0 complexity model

Author: NOVIQ Engine Build — Phase 2
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MODULE_VERSION = "1.0.0"
AR_DRG_VERSION = "V11.0"

DEFAULT_KB_PATH = Path(__file__).parent / "dcl_exclusions.json"

# Validation status levels
STATUS_PASS = "PASS"
STATUS_WARN = "WARN"
STATUS_FAIL = "FAIL"

# Upcoding risk levels
RISK_HIGH   = "high"
RISK_MEDIUM = "medium"
RISK_LOW    = "low"


# ---------------------------------------------------------------------------
# Knowledge Base loader
# ---------------------------------------------------------------------------

class DCLExclusionKnowledgeBase:
    """
    Loads and indexes the dcl_exclusions.json Knowledge Base.
    Called once at module init — not per request.
    """

    def __init__(self, kb_path: Path = DEFAULT_KB_PATH) -> None:
        if not kb_path.exists():
            raise FileNotFoundError(
                f"DCL Exclusion Knowledge Base not found at: {kb_path}\n"
                f"Ensure dcl_exclusions.json is in the same directory as validation_rules.py"
            )

        with open(kb_path, encoding="utf-8") as f:
            raw = json.load(f)

        self._raw = raw
        self._unconditional: dict[str, dict] = {}
        self._conditional: dict[str, dict]   = {}
        self._covid_inclusions: dict[str, dict] = {}
        self._previously_excluded_ranges: list[str] = []

        self._build_unconditional_index(raw)
        self._build_conditional_index(raw)
        self._build_covid_index(raw)
        self._build_legacy_range_index(raw)

    # ------------------------------------------------------------------
    # Index builders
    # ------------------------------------------------------------------

    def _build_unconditional_index(self, raw: dict) -> None:
        section = raw.get("unconditional_exclusions", {})

        for entry in section.get("codes", []):
            code = entry["icd_code"].strip().upper()
            self._unconditional[code] = entry

        # Expand range entries (e.g. Z14–Z16) into individual codes
        # 1. Safe access using .get() to prevent KeyError if keys are missing
            start = range_entry.get("range_start")
            end   = range_entry.get("range_end")
            
            # 2. Validation: If core range data is missing, skip this entry instead of crashing
            if not start or not end:
                continue

            start = str(start).strip().upper()
            end   = str(end).strip().upper()
            
            # 3. Safe Expansion: Fallback to [start] if expansion list is missing
            for expanded_code in range_entry.get("expansion", [start]):
               if not expanded_code:
                    continue
                   
                expanded_code = str(expanded_code).strip().upper()
                
                # 4. Map the data into the unconditional index
                self._unconditional[expanded_code] = {
                    **range_entry,
                    "icd_code": expanded_code,
                    "_range_source": f"{start}–{end}"
                }
    def _build_conditional_index(self, raw: dict) -> None:
        for entry in raw.get("conditional_exclusions", {}).get("codes", []):
            code = entry["icd_code"].strip().upper()
            self._conditional[code] = entry

    def _build_covid_index(self, raw: dict) -> None:
        for entry in raw.get("covid19_dcl_inclusions", {}).get("codes", []):
            code = entry["icd_code"].strip().upper()
            self._covid_inclusions[code] = entry

    def _build_legacy_range_index(self, raw: dict) -> None:
        for entry in raw.get("previously_excluded_categories", {}).get("code_ranges", []):
            self._previously_excluded_ranges.append(entry["range"].strip().upper())

    # ------------------------------------------------------------------
    # Public lookup methods
    # ------------------------------------------------------------------

    def is_unconditionally_excluded(self, icd_code: str) -> bool:
        """Return True if the code is unconditionally excluded from DCL."""
        code = self._normalise(icd_code)
        if code in self._unconditional:
            return True
        # Check range prefix match for Z14–Z16 style exclusions
        for prefix in self._unconditional:
            if code.startswith(prefix) and len(prefix) <= len(code):
                return True
        return False

    def is_conditionally_excluded(self, icd_code: str, co_present_codes: list[str]) -> bool:
        """Return True if the code is conditionally excluded given the episode's other codes."""
        code = self._normalise(icd_code)
        if code not in self._conditional:
            return False
        entry   = self._conditional[code]
        trigger = entry.get("excluded_when", {})
        if trigger.get("condition_type") == "co_present":
            normalised_co = {self._normalise(c) for c in co_present_codes}
            required      = {self._normalise(c) for c in trigger.get("condition_codes", [])}
            return bool(required & normalised_co)
        return False

    def is_previously_excluded(self, icd_code: str) -> bool:
        """Return True if code falls in the pre-V11.0 socioeconomic exclusion ranges."""
        code = self._normalise(icd_code)
        for prefix in self._previously_excluded_ranges:
            if code.startswith(prefix):
                return True
        return False

    def get_exclusion_entry(self, icd_code: str) -> dict | None:
        """Return the full exclusion KB entry for a code, or None if not excluded."""
        code = self._normalise(icd_code)
        if code in self._unconditional:
            return self._unconditional[code]
        # Prefix match
        for prefix, entry in self._unconditional.items():
            if code.startswith(prefix):
                return entry
        return None

    def get_covid_entry(self, icd_code: str) -> dict | None:
        """Return COVID-19 routing entry if the code is a COVID inclusion code."""
        return self._covid_inclusions.get(self._normalise(icd_code))

    def is_dcl_eligible(self, icd_code: str, co_present_codes: list[str] | None = None) -> bool:
        """
        Master DCL eligibility check.
        Returns True if the code CAN receive a DCL (not excluded by any rule).
        """
        if co_present_codes is None:
            co_present_codes = []
        if self.is_unconditionally_excluded(icd_code):
            return False
        if self.is_conditionally_excluded(icd_code, co_present_codes):
            return False
        if self.is_previously_excluded(icd_code):
            return False
        return True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalise(code: str) -> str:
        """Normalise ICD-10-AM code: uppercase, strip whitespace, remove trailing dot."""
        return code.strip().upper().rstrip(".")

    @property
    def meta(self) -> dict:
        return self._raw.get("_meta", {})


# ---------------------------------------------------------------------------
# Core validation functions  (JSON-In / JSON-Out)
# ---------------------------------------------------------------------------

# Module-level KB instance — loaded once on import
_KB: DCLExclusionKnowledgeBase | None = None


def _get_kb(kb_path: Path | None = None) -> DCLExclusionKnowledgeBase:
    global _KB
    if _KB is None or kb_path is not None:
        _KB = DCLExclusionKnowledgeBase(kb_path or DEFAULT_KB_PATH)
    return _KB


# ---------------------------------------------------------------------------
# Public API — three functions matching the interface contract
# ---------------------------------------------------------------------------

def validate_episode(episode: dict, kb_path: Path | None = None) -> dict:
    """
    PRIMARY ENTRY POINT — JSON-In / JSON-Out.

    Validates all ICD-10-AM codes in a PatientEpisode against the DCL
    Exclusion Knowledge Base.  Returns a fully provenance-traced
    ValidationResult dict ready for JSON serialisation.

    Args:
        episode:  PatientEpisode dict (see INPUT_SCHEMA at bottom of file)
        kb_path:  Optional override path to dcl_exclusions.json

    Returns:
        ValidationResult dict (see OUTPUT_SCHEMA at bottom of file)

    FastAPI usage:
        @app.post("/validate")
        async def validate(episode: dict) -> dict:
            return validate_episode(episode)
    """
    kb = _get_kb(kb_path)

    episode_id      = episode.get("episode_id", "UNKNOWN")
    pdx             = (episode.get("pdx") or "").strip().upper()
    adx_raw         = [c.strip().upper() for c in episode.get("adx", []) if c]
    all_diag_codes  = ([pdx] if pdx else []) + adx_raw

    flags:          list[str] = []
    excluded_codes: list[dict] = []
    dcl_eligible_adx: list[dict] = []
    covid_routing:  dict = {"triggered": False, "code": None, "target_adrg": None}

    # ------------------------------------------------------------------
    # 1. Validate PDX
    # ------------------------------------------------------------------
    pdx_result: dict[str, Any] = {"code": pdx, "eligible": True, "dcl_assigned": 0}
    if pdx:
        pdx_eligible = kb.is_dcl_eligible(pdx, adx_raw)
        pdx_result["eligible"] = pdx_eligible
        if not pdx_eligible:
            reason = _build_exclusion_record(pdx, kb, all_diag_codes, is_pdx=True)
            excluded_codes.append(reason)
            flags.append(f"PDX {pdx} is excluded from DCL — review coding.")

        # COVID PDX routing check
        covid_entry = kb.get_covid_entry(pdx)
        if covid_entry:
            routing = covid_entry.get("pdx_routing", {})
            if routing.get("applies_when") == "used_as_pdx":
                covid_routing = {
                    "triggered":   True,
                    "code":        pdx,
                    "target_adrg": routing.get("routes_to_adrg"),
                    "adrg_description": routing.get("adrg_description")
                }

    # ------------------------------------------------------------------
    # 2. Validate additional diagnoses (ADX)
    # ------------------------------------------------------------------
    for adx_code in adx_raw:
        eligible = kb.is_dcl_eligible(adx_code, [c for c in all_diag_codes if c != adx_code])
        exclusion_reason = None
        upcoding_risk = False

        if not eligible:
            record = _build_exclusion_record(adx_code, kb, all_diag_codes)
            excluded_codes.append(record)
            exclusion_reason = record["exclusion_reason"]
            upcoding_risk    = record["upcoding_risk"]

            if upcoding_risk:
                flags.append(
                    f"ADX {adx_code} ({record['description']}) is excluded from complexity "
                    f"scoring — upcoding risk. Exclusion type: {record['exclusion_type']}."
                )

        dcl_eligible_adx.append({
            "code":             adx_code,
            "eligible":         eligible,
            "exclusion_reason": exclusion_reason,
            "upcoding_risk":    upcoding_risk
        })

    # ------------------------------------------------------------------
    # 3. Compute summary
    # ------------------------------------------------------------------
    total_excluded      = len(excluded_codes)
    upcoding_risk_count = sum(1 for e in excluded_codes if e.get("upcoding_risk"))
    total_adx_reviewed  = len(adx_raw)

    # Determine overall status
    if upcoding_risk_count > 0:
        status = STATUS_WARN
    elif total_excluded > 0:
        status = STATUS_WARN
    else:
        status = STATUS_PASS

    # ------------------------------------------------------------------
    # 4. Assemble result
    # ------------------------------------------------------------------
    return {
        "episode_id":        episode_id,
        "validation_status": status,
        "ar_drg_version":    AR_DRG_VERSION,
        "validated_at":      datetime.now(timezone.utc).isoformat(),
        "module_version":    MODULE_VERSION,

        "dcl_eligible_codes": {
            "pdx": pdx_result,
            "adx": dcl_eligible_adx
        },

        "excluded_codes": excluded_codes,

        "covid_routing": covid_routing,

        "flags": flags,

        "summary": {
            "total_adx_reviewed":  total_adx_reviewed,
            "total_excluded":      total_excluded,
            "upcoding_risk_count": upcoding_risk_count,
            "pass_to_grouper":     status != STATUS_FAIL
        }
    }


def validate_dcl_eligibility(icd_code: str, adrg: str | None = None,
                              kb_path: Path | None = None) -> dict:
    """
    Single-code DCL eligibility check — JSON-Out.

    Args:
        icd_code: ICD-10-AM code to check
        adrg:     Optional — ADRG context (reserved for future conditional logic)
        kb_path:  Optional KB path override

    Returns:
        dict with keys: code, eligible, exclusion_type, exclusion_reason, upcoding_risk
    """
    kb      = _get_kb(kb_path)
    code    = icd_code.strip().upper()
    eligible = kb.is_dcl_eligible(code)
    entry   = kb.get_exclusion_entry(code) if not eligible else None

    return {
        "code":             code,
        "adrg_context":     adrg,
        "eligible":         eligible,
        "exclusion_type":   _get_exclusion_type(code, kb) if not eligible else None,
        "exclusion_reason": entry.get("exclusion_reason") if entry else None,
        "upcoding_risk":    (entry.get("upcoding_risk_category") in (RISK_HIGH, RISK_MEDIUM))
                            if entry else False,
        "provenance":       entry.get("source") if entry else None
    }


def get_exclusion_reason(icd_code: str, kb_path: Path | None = None) -> dict:
    """
    Returns the plain-language exclusion reason for a code, or confirms eligibility.

    Args:
        icd_code: ICD-10-AM code to query
        kb_path:  Optional KB path override

    Returns:
        dict with keys: code, excluded, reason, source
    """
    kb    = _get_kb(kb_path)
    code  = icd_code.strip().upper()
    entry = kb.get_exclusion_entry(code)

    if entry:
        return {
            "code":     code,
            "excluded": True,
            "reason":   entry.get("exclusion_reason", "Excluded from AR-DRG V11.0 DCL model."),
            "source":   entry.get("source", "AR-DRG V11.0 Definitions Manual Appendix C")
        }

    if kb.is_previously_excluded(code):
        return {
            "code":     code,
            "excluded": True,
            "reason":   "Socioeconomic/psychosocial code — excluded from DCL since AR-DRG V8.0.",
            "source":   "AR-DRG V11.0 Section 3.9.2"
        }

    return {
        "code":     code,
        "excluded": False,
        "reason":   None,
        "source":   None
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_exclusion_record(code: str, kb: DCLExclusionKnowledgeBase,
                             all_codes: list[str], is_pdx: bool = False) -> dict:
    """Build a full exclusion record for the ValidationResult excluded_codes list."""
    entry          = kb.get_exclusion_entry(code)
    exclusion_type = _get_exclusion_type(code, kb)

    if entry:
        description      = entry.get("description", "")
        exclusion_reason = entry.get("exclusion_reason", "Excluded from AR-DRG V11.0 DCL model.")
        risk_cat         = entry.get("upcoding_risk_category", RISK_LOW)
        upcoding_risk    = risk_cat in (RISK_HIGH, RISK_MEDIUM)
        provenance       = entry.get("source", "AR-DRG V11.0 Appendix A Table A5")
    elif kb.is_previously_excluded(code):
        description      = "Socioeconomic/psychosocial factor"
        exclusion_reason = "Excluded from DCL since AR-DRG V8.0 — pre-disposing factor, not clinical complexity driver."
        upcoding_risk    = False
        provenance       = "AR-DRG V11.0 Section 3.9.2"
    else:
        description      = ""
        exclusion_reason = "Code excluded from AR-DRG V11.0 DCL model."
        upcoding_risk    = False
        provenance       = "AR-DRG V11.0 Definitions Manual Appendix C"

    return {
        "code":             code,
        "description":      description,
        "exclusion_type":   exclusion_type,
        "exclusion_reason": exclusion_reason,
        "upcoding_risk":    upcoding_risk,
        "is_pdx":           is_pdx,
        "provenance":       provenance
    }


def _get_exclusion_type(code: str, kb: DCLExclusionKnowledgeBase) -> str:
    """Return 'unconditional', 'conditional', 'socioeconomic', or 'unknown'."""
    if kb.is_unconditionally_excluded(code):
        return "unconditional"
    if kb.is_previously_excluded(code):
        return "socioeconomic"
    return "conditional"


# ---------------------------------------------------------------------------
# Input / Output schemas (documentation)
# ---------------------------------------------------------------------------

INPUT_SCHEMA = {
    "episode_id":       "string — unique episode identifier (echoed in output)",
    "patient_age":      "integer — age in years at admission",
    "patient_sex":      "string — Male | Female | Other | Unknown",
    "admission_weight": "integer | null — grams, neonates only",
    "same_day":         "boolean",
    "separation_mode":  "string — discharge_home | transfer | death | ...",
    "pdx":              "string — principal ICD-10-AM diagnosis code",
    "adx":              ["list of additional ICD-10-AM diagnosis codes"],
    "achi_codes":       ["list of ACHI intervention codes"],
    "los_days":         "integer — length of stay in days"
}

OUTPUT_SCHEMA = {
    "episode_id":        "string",
    "validation_status": "PASS | WARN | FAIL",
    "ar_drg_version":    "string",
    "validated_at":      "ISO 8601 UTC timestamp",
    "module_version":    "string",
    "dcl_eligible_codes": {
        "pdx": {"code": "str", "eligible": "bool", "dcl_assigned": "int"},
        "adx": [{"code": "str", "eligible": "bool",
                 "exclusion_reason": "str|null", "upcoding_risk": "bool"}]
    },
    "excluded_codes": [{
        "code":             "string",
        "description":      "string",
        "exclusion_type":   "unconditional | conditional | socioeconomic",
        "exclusion_reason": "string",
        "upcoding_risk":    "boolean",
        "is_pdx":           "boolean",
        "provenance":       "string"
    }],
    "covid_routing": {
        "triggered":    "boolean",
        "code":         "string | null",
        "target_adrg":  "string | null"
    },
    "flags":   ["list of plain-language warning strings"],
    "summary": {
        "total_adx_reviewed":  "integer",
        "total_excluded":      "integer",
        "upcoding_risk_count": "integer",
        "pass_to_grouper":     "boolean"
    }
}


# ---------------------------------------------------------------------------
# ECC Model pipeline — confirmed from AR-DRG V11.0 Technical Specifications
# (free public PDF, IHACPA, Section 4.5 for ECCS; Section 4.4 for DCL)
# ---------------------------------------------------------------------------
#
# STEP 1  Diagnosis exclusions (runtime)
#         Remove unconditional (Appendix C, Table C1) and apply conditional
#         (Table C2) exclusions.  Handled by DCLExclusionKnowledgeBase above.
#         Result: ~11,065 in-scope codes in V11.0.
#
# STEP 2  Geometric mean cost model (development-time — not runtime)
#         Multiplicative per-ADRG model on log-transformed national cost data.
#         Predicts expected cost from diagnosis COUNT (no specific codes yet).
#
# STEP 3  DCL derivation (development-time — pre-computed, not runtime)
#         Per (diagnosis x ADRG): log cost differential actual vs predicted,
#         standardised, stabilised vs V10.0 (min ±0.2 to change), rounded 0-5.
#         Principal diagnosis IS included. ADRG-specific values.
#         DCL = 0 → no incremental resource impact in this ADRG.
#         Full DCL table: embedded in licensed grouper software ONLY.
#
# STEP 4  ECCS calculation (runtime) — implemented as compute_eccs() below
#         Official formula (Technical Specifications Section 4.5):
#         ECCS(e) = SUM[ DCL(x_i, A) × (0.86)^(i-1) ] for i = 1..n
#         DCLs sorted descending. Principal diagnosis included.
#
# STEP 5  ADRG splitting (runtime)
#         ECCS compared to per-ADRG thresholds → final DRG (A/B/C/D/Z).
#         Thresholds in AR-DRG V11.0 Definitions Manual (purchased).
# ---------------------------------------------------------------------------

ECCS_DECAY_FACTOR = 0.86
# Global constant — confirmed from Technical Specifications Section 4.5.
# Selected via nonlinear regression across all ADRGs, range tested: 0.83-0.88.
# Best overall statistical fit. Same value in V10.0 and V11.0.
# Single GLOBAL value — NOT ADRG-specific.


class KnowledgeBaseIncompleteError(Exception):
    """
    Raised when the Knowledge Base is missing a required value for production use.
    Example: F25 ECCS threshold is null — grouper must not assign F25A/F25B until resolved.
    """
    pass


def compute_eccs(dcl_values: list) -> float:
    """
    Compute Episode Clinical Complexity Score (ECCS) from a list of DCL values.

    Official formula — AR-DRG V11.0 Technical Specifications, Section 4.5:
        ECCS(e) = SUM[ DCL(x_i, A) × (0.86)^(i-1) ]  for i = 1..n
        where DCLs are sorted descending before summation.

    Args:
        dcl_values: List of integer DCL values (0–5). Include both PDX and ADX.
                    DCL = 0 values contribute nothing and may be included or omitted.
                    Sorting is handled internally.

    Returns:
        ECCS as a float, rounded to 4 decimal places.
        Typical range: 0–10. Theoretical max ~32. Rarely exceeds 15.

    Confirmed example (from Technical Specifications):
        dcl_values = [4, 3, 2, 1, 0]
        ECCS = 4×(0.86)^0 + 3×(0.86)^1 + 2×(0.86)^2 + 1×(0.86)^3 + 0×(0.86)^4
             = 4 + 2.58 + 1.4792 + 0.636056 + 0
             = 8.695  ← matches official documentation exactly

    Note:
        DCL values are PRE-COMPUTED during classification development and stored
        in licensed grouper software DCL tables. This function receives them as
        input — it does NOT derive DCLs from diagnoses.
    """
    if not dcl_values:
        return 0.0
    sorted_dcls = sorted(dcl_values, reverse=True)
    eccs = sum(dcl * (ECCS_DECAY_FACTOR ** i) for i, dcl in enumerate(sorted_dcls))
    return round(eccs, 4)


def compute_eccs_with_trace(dcl_values: list) -> dict:
    """
    Compute ECCS and return a full step-by-step trace — useful for physician
    review output and audit provenance.

    Returns:
        dict with keys: eccs, sorted_dcls, steps, formula_string
    """
    if not dcl_values:
        return {"eccs": 0.0, "sorted_dcls": [], "steps": [], "formula_string": "ECCS = 0"}

    sorted_dcls = sorted([d for d in dcl_values if d > 0], reverse=True)
    steps = []
    total = 0.0
    parts = []

    for i, dcl in enumerate(sorted_dcls):
        decay    = ECCS_DECAY_FACTOR ** i
        contrib  = dcl * decay
        total   += contrib
        steps.append({
            "position":    i + 1,
            "dcl":         dcl,
            "decay_power": i,
            "decay_value": round(decay, 6),
            "contribution": round(contrib, 6)
        })
        parts.append(f"{dcl}×(0.86)^{i}")

    formula_string = " + ".join(parts) + f" = {round(total, 4)}"

    return {
        "eccs":          round(total, 4),
        "sorted_dcls":   sorted_dcls,
        "steps":         steps,
        "formula_string": formula_string
    }


def check_threshold(eccs: float, threshold_value, adrg_code: str) -> str:
    """
    Determine DRG suffix (A or B) based on ECCS vs threshold.
    Raises KnowledgeBaseIncompleteError if threshold is None (production gate).

    Args:
        eccs:            Computed ECCS value
        threshold_value: The A-class lower boundary from the Knowledge Base
        adrg_code:       ADRG code (for error messaging)

    Returns:
        "A" if eccs >= threshold_value, else "B"
    """
    if threshold_value is None:
        raise KnowledgeBaseIncompleteError(
            f"ECCS threshold for ADRG {adrg_code} is null in the Knowledge Base. "
            f"Populate eccs_threshold.value from the AR-DRG V11.0 Definitions Manual "
            f"before assigning DRG end-class. Purchase via Lane Print: "
            f"ar-drg.laneprint.com.au"
        )
    return "A" if eccs >= threshold_value else "B"


# ---------------------------------------------------------------------------
# FastAPI wrapper (3 lines — uncomment when deploying)
# ---------------------------------------------------------------------------
#
# from fastapi import FastAPI
# app = FastAPI(title="NOVIQ Engine — Validation Rules API")
#
# @app.post("/validate")
# async def validate(episode: dict) -> dict:
#     return validate_episode(episode)
#
# @app.get("/validate/code/{icd_code}")
# async def check_code(icd_code: str, adrg: str | None = None) -> dict:
#     return validate_dcl_eligibility(icd_code, adrg)
#
# @app.get("/validate/reason/{icd_code}")
# async def exclusion_reason(icd_code: str) -> dict:
#     return get_exclusion_reason(icd_code)


# ---------------------------------------------------------------------------
# Quick smoke test — run directly: python validation_rules.py
# ---------------------------------------------------------------------------

if __name__ == "__main__":

    TEST_EPISODE = {
        "episode_id":       "TEST-001",
        "patient_age":      67,
        "patient_sex":      "Female",
        "admission_weight": None,
        "same_day":         False,
        "separation_mode":  "discharge_home",
        "pdx":              "K80.20",
        "adx":              [
            "E61.1",    # Iron deficiency — EXCLUDED (high upcoding risk)
            "D89.82",   # Immunocompromised status — EXCLUDED (high upcoding risk)
            "Z59.0",    # Homelessness — EXCLUDED (socioeconomic, since V8.0)
            "E11.9",    # Type 2 diabetes — ELIGIBLE
            "I10",      # Hypertension — ELIGIBLE
        ],
        "achi_codes":       ["30445-00"],
        "los_days":         3
    }

    TEST_COVID_EPISODE = {
        "episode_id":       "TEST-002",
        "patient_age":      55,
        "patient_sex":      "Male",
        "admission_weight": None,
        "same_day":         False,
        "separation_mode":  "discharge_home",
        "pdx":              "U07.12",
        "adx":              ["J18.9", "E11.9"],
        "achi_codes":       [],
        "los_days":         5
    }

    print("=" * 60)
    print("NOVIQ Engine — validation_rules.py smoke test")
    print("=" * 60)

    for test in [TEST_EPISODE, TEST_COVID_EPISODE]:
        result = validate_episode(test, kb_path=Path("dcl_exclusions.json"))
        print(f"\nEpisode: {result['episode_id']}")
        print(f"  Status:          {result['validation_status']}")
        print(f"  Excluded codes:  {result['summary']['total_excluded']}")
        print(f"  Upcoding risk:   {result['summary']['upcoding_risk_count']}")
        print(f"  Pass to grouper: {result['summary']['pass_to_grouper']}")
        print(f"  COVID routing:   {result['covid_routing']}")
        if result["flags"]:
            print("  Flags:")
            for flag in result["flags"]:
                print(f"    - {flag}")
