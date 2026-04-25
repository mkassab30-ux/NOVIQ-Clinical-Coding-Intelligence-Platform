"""
NOVIQ Engine — Validation Rules & DCL Exclusions
==================================================
Phase 2 — Production-grade, Railway-safe

JSON-In / JSON-Out. Zero DB calls. Zero UI coupling.
FastAPI-wrappable in 3 lines.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# ── Default paths ──────────────────────────────────────────────────────────
DEFAULT_KB_PATH = Path(__file__).parent / "dcl_exclusions.json"

# ── Errors ─────────────────────────────────────────────────────────────────
class KnowledgeBaseIncompleteError(ValueError):
    """Raised when a required KB value is null (e.g. F25 ECCS threshold)."""
    pass


# ══════════════════════════════════════════════════════════════════════════
# DCL Exclusion Knowledge Base
# ══════════════════════════════════════════════════════════════════════════

class DCLExclusionKnowledgeBase:
    """
    Loads dcl_exclusions.json and provides fast lookup for:
    - Unconditional exclusions (always DCL=0)
    - Conditional exclusions (DCL=0 only when related diagnosis co-present)
    - Previously excluded Z-code ranges (socioeconomic)
    """

    def __init__(self, kb_path: Path = DEFAULT_KB_PATH) -> None:
        # Graceful: if file missing, engine still runs with no exclusions
        self._unconditional: dict[str, dict] = {}
        self._conditional:   dict[str, dict] = {}
        self._prev_excluded: set[str]        = set()

        if not kb_path.exists():
            print(f"[WARN] DCL exclusions KB not found: {kb_path}")
            return

        try:
            with open(kb_path, encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            print(f"[WARN] DCL exclusions KB failed to load: {e}")
            return

        self._build_unconditional(raw)
        self._build_conditional(raw)
        self._build_prev_excluded(raw)

    # ── Builders ─────────────────────────────────────────────────────────

    def _build_unconditional(self, raw: dict) -> None:
        """Load unconditional exclusion codes and code ranges."""
        section = raw.get("unconditional_exclusions", {})

        # Individual codes
        for entry in section.get("codes", []):
            try:
                code = entry["icd_code"].strip().upper()
                self._unconditional[code] = entry
            except (KeyError, AttributeError):
                continue

        # Code ranges (e.g. Z14–Z16 expanded to Z14, Z15, Z16)
        for range_entry in section.get("code_ranges", []):
            try:
                # Support both "range_start/range_end" and "range" (single code)
                if "range_start" in range_entry and "range_end" in range_entry:
                    expansions = range_entry.get(
                        "expansion",
                        [range_entry["range_start"], range_entry["range_end"]]
                    )
                elif "range" in range_entry:
                    # Single code listed as range
                    expansions = range_entry.get("expansion", [range_entry["range"]])
                else:
                    continue

                for code in expansions:
                    code = str(code).strip().upper()
                    self._unconditional[code] = {**range_entry, "icd_code": code}
            except Exception:
                continue

    def _build_conditional(self, raw: dict) -> None:
        """Load conditional exclusion codes."""
        for entry in raw.get("conditional_exclusions", {}).get("codes", []):
            try:
                code = entry["icd_code"].strip().upper()
                self._conditional[code] = entry
            except (KeyError, AttributeError):
                continue

    def _build_prev_excluded(self, raw: dict) -> None:
        """Load previously excluded Z-code ranges (socioeconomic Z55–Z65)."""
        for entry in raw.get("previously_excluded_categories", {}).get("code_ranges", []):
            try:
                code = entry.get("range", "").strip().upper()
                if code:
                    self._prev_excluded.add(code)
            except Exception:
                continue

    # ── Public lookups ───────────────────────────────────────────────────

    def is_unconditionally_excluded(self, icd_code: str) -> bool:
        code = icd_code.strip().upper()
        return code in self._unconditional

    def is_conditionally_excluded(
        self,
        icd_code: str,
        co_present_codes: list[str],
    ) -> bool:
        code = icd_code.strip().upper()
        entry = self._conditional.get(code)
        if not entry:
            return False
        try:
            trigger_codes = entry.get("excluded_when", {}).get("condition_codes", [])
            co_upper = {c.strip().upper() for c in co_present_codes}
            return bool(set(trigger_codes) & co_upper)
        except Exception:
            return False

    def is_previously_excluded(self, icd_code: str) -> bool:
        """Check Z-code ranges like Z55-Z65 (socioeconomic)."""
        code = icd_code.strip().upper()
        # Check direct membership
        if code in self._prev_excluded:
            return True
        # Check by prefix (e.g. Z59.0 starts with Z59)
        if len(code) >= 3:
            prefix = code[:3]
            if prefix in self._prev_excluded:
                return True
        return False

    def is_dcl_eligible(
        self,
        icd_code: str,
        co_present_codes: list[str] | None = None,
    ) -> bool:
        """Return True if code can receive a non-zero DCL."""
        code = icd_code.strip().upper()
        if self.is_unconditionally_excluded(code):
            return False
        if self.is_previously_excluded(code):
            return False
        if co_present_codes and self.is_conditionally_excluded(code, co_present_codes):
            return False
        return True

    def get_exclusion_entry(self, icd_code: str) -> dict | None:
        code = icd_code.strip().upper()
        return (self._unconditional.get(code)
                or self._conditional.get(code))


# ══════════════════════════════════════════════════════════════════════════
# ECCS Utilities
# ══════════════════════════════════════════════════════════════════════════

ECCS_DECAY = 0.86  # Confirmed: AR-DRG V11.0 Technical Specifications, Section 4.5


def compute_eccs(dcl_values: list) -> float:
    """
    ECCS = Σ [ DCL_i × 0.86^(i-1) ] — DCLs sorted descending.
    
    Only non-zero DCLs contribute. Returns 0.0 if no eligible diagnoses.
    """
    eligible = sorted([int(v) for v in dcl_values if v and int(v) > 0], reverse=True)
    if not eligible:
        return 0.0
    return round(
        sum(dcl * (ECCS_DECAY ** i) for i, dcl in enumerate(eligible)),
        6,
    )


def compute_eccs_with_trace(dcl_values: list) -> dict:
    """ECCS computation with full audit trail."""
    eligible = sorted([int(v) for v in dcl_values if v and int(v) > 0], reverse=True)
    contributions = []
    total = 0.0

    for i, dcl in enumerate(eligible):
        weight    = ECCS_DECAY ** i
        contrib   = dcl * weight
        total    += contrib
        contributions.append({
            "position":    i + 1,
            "dcl":         dcl,
            "decay_weight": round(weight, 6),
            "contribution": round(contrib, 6),
            "running_eccs": round(total, 6),
        })

    return {
        "eccs":          round(total, 6),
        "decay_factor":  ECCS_DECAY,
        "n_eligible":    len(eligible),
        "eligible_dcls": eligible,
        "contributions": contributions,
    }


def check_threshold(eccs: float, threshold_value, adrg_code: str) -> str:
    """
    Compare ECCS against threshold.
    Returns "A" (major) or "B" (minor) suffix.
    Raises KnowledgeBaseIncompleteError if threshold is None.
    """
    if threshold_value is None:
        raise KnowledgeBaseIncompleteError(
            f"ECCS threshold for ADRG {adrg_code} is null in Knowledge Base. "
            f"Purchase AR-DRG V11.0 Definitions Manual (Lane Print: ar-drg.laneprint.com.au) "
            f"to resolve. NOVIQ Engine cannot assign {adrg_code} DRG until populated."
        )
    return "A" if eccs >= float(threshold_value) else "B"


# ══════════════════════════════════════════════════════════════════════════
# Episode Validation
# ══════════════════════════════════════════════════════════════════════════

def validate_episode(episode: dict, kb_path: Path | None = None) -> dict:
    """
    Validate episode and compute DCL eligibility for all diagnoses.

    Returns a validation_result dict with:
      - dcl_eligible: list of eligible diagnosis codes
      - excluded: list of excluded codes with reasons
      - upcoding_risks: list of flagged codes
      - summary: counts
    """
    kb = DCLExclusionKnowledgeBase(kb_path or DEFAULT_KB_PATH)

    pdx      = (episode.get("pdx") or "").strip().upper()
    adx_list = [c.strip().upper() for c in (episode.get("adx") or []) if c]
    all_dx   = ([pdx] if pdx else []) + adx_list

    eligible:        list[dict] = []
    excluded:        list[dict] = []
    upcoding_risks:  list[dict] = []

    for code in all_dx:
        if not code:
            continue

        is_pdx    = (code == pdx)
        co_others = [c for c in all_dx if c != code]

        # Check exclusions
        if kb.is_unconditionally_excluded(code):
            entry  = kb.get_exclusion_entry(code) or {}
            excl_r = entry.get("exclusion_reason", "Appendix C Table C1 — unconditional exclusion")
            excluded.append({
                "icd_code":       code,
                "is_principal":   is_pdx,
                "exclusion_type": "unconditional",
                "reason":         excl_r,
                "upcoding_risk":  entry.get("upcoding_risk", "unknown"),
            })
            if entry.get("upcoding_risk") in ("high", "medium"):
                upcoding_risks.append({
                    "icd_code":  code,
                    "risk_level": entry.get("upcoding_risk"),
                    "reason":    excl_r,
                })
            continue

        if kb.is_previously_excluded(code):
            excluded.append({
                "icd_code":       code,
                "is_principal":   is_pdx,
                "exclusion_type": "socioeconomic",
                "reason":         "Socioeconomic code — excluded from ECC Model since AR-DRG V8.0",
                "upcoding_risk":  "high",
            })
            upcoding_risks.append({
                "icd_code":   code,
                "risk_level": "high",
                "reason":     "Socioeconomic code (Z55–Z65) — not eligible for DCL",
            })
            continue

        if kb.is_conditionally_excluded(code, co_others):
            excluded.append({
                "icd_code":       code,
                "is_principal":   is_pdx,
                "exclusion_type": "conditional",
                "reason":         "Appendix C Table C2 — definitive diagnosis co-present in episode",
                "upcoding_risk":  "medium",
            })
            continue

        # Eligible
        eligible.append({
            "icd_code":     code,
            "is_principal": is_pdx,
        })

    # ACS score validation
    acs_pdx_score = episode.get("acs_pdx_score", 0) or 0
    acs_violations = []
    if acs_pdx_score < 5 and pdx:
        acs_violations.append({
            "code":   pdx,
            "type":   "pdx_below_threshold",
            "score":  acs_pdx_score,
            "reason": f"PDX ACS score {acs_pdx_score}/7 — below coding threshold of 5",
        })

    for adx_score in (episode.get("acs_adx_scores") or []):
        if isinstance(adx_score, dict):
            code  = adx_score.get("code", "")
            score = adx_score.get("score", 0) or 0
            if score < 3 and adx_score.get("action") == "code":
                acs_violations.append({
                    "code":   code,
                    "type":   "adx_below_threshold",
                    "score":  score,
                    "reason": f"ADX {code} ACS score {score}/8 — below minimum of 3",
                })

    return {
        "dcl_eligible":     eligible,
        "excluded":         excluded,
        "upcoding_risks":   upcoding_risks,
        "acs_violations":   acs_violations,
        "summary": {
            "total_diagnoses":    len(all_dx),
            "total_eligible":     len(eligible),
            "total_excluded":     len(excluded),
            "upcoding_risk_count": len(upcoding_risks),
            "acs_violations":     len(acs_violations),
        },
    }
