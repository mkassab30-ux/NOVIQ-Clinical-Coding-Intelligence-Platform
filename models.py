"""
NOVIQ Engine — Data Models
===========================
Phase 4, Deliverable 4.1

Defines the agreed data contracts between all engine modules:
  EpisodeRecord     — JSON-In: what every module receives
  ACSScore          — ACS 0001/0002 scoring result per diagnosis
  CodingSuggestion  — final output with provenance + physician gate

All models are plain dataclasses with to_dict() for JSON serialisation.
JSON-In / JSON-Out — zero UI coupling throughout.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
import uuid


# ---------------------------------------------------------------------------
# ACS thresholds — confirmed from ACS 0001 and ACS 0002
# ---------------------------------------------------------------------------

ACS_THRESHOLD_CODE   = 5    # score >= 5 → code
ACS_THRESHOLD_REVIEW = 3    # score 3-4 → physician review required
                             # score < 3 → do not code

ACS_PDX_MAX_SCORE    = 7    # ACS 0001 principal diagnosis max
ACS_ADX_MAX_SCORE    = 8    # ACS 0002 additional diagnosis max


# ---------------------------------------------------------------------------
# EpisodeRecord — universal input contract
# ---------------------------------------------------------------------------

@dataclass
class EpisodeRecord:
    """
    Standardised patient episode. Accepted by every NOVIQ Engine module.
    Sourced from EHR via FHIR R4 or HL7 v2 adapter.
    """
    episode_id:        str
    patient_age:       int
    patient_sex:       str                     # Male | Female | Other | Unknown
    pdx:               str                     # principal ICD-10-AM code
    adx:               list[str] = field(default_factory=list)
    achi_codes:        list[str] = field(default_factory=list)
    los_days:          int       = 0
    same_day:          bool      = False
    separation_mode:   str       = "discharge_home"
    admission_weight:  Any       = None        # grams, neonates only
    hours_mech_vent:   Any       = None
    care_type:         str       = "01"        # 01=Acute 07=Newborn 11=MentalHealth

    # Populated by ACS Scoring Engine
    acs_pdx_score:     int        = 0          # ACS 0001 score (0–7)
    acs_adx_scores:    list[dict] = field(default_factory=list)

    # EHR provenance — which documents were read
    ehr_documents:     list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> EpisodeRecord:
        return cls(
            episode_id       = str(d.get("episode_id", "")),
            patient_age      = int(d.get("patient_age", 0) or 0),
            patient_sex      = str(d.get("patient_sex", "Unknown")),
            pdx              = str(d.get("pdx", "")).strip().upper(),
            adx              = [c.strip().upper() for c in (d.get("adx") or []) if c],
            achi_codes       = [c.strip().upper() for c in (d.get("achi_codes") or []) if c],
            los_days         = int(d.get("los_days", 0) or 0),
            same_day         = bool(d.get("same_day", False)),
            separation_mode  = str(d.get("separation_mode", "discharge_home")),
            admission_weight = d.get("admission_weight"),
            hours_mech_vent  = d.get("hours_mech_vent"),
            care_type        = str(d.get("care_type", "01")),
            acs_pdx_score    = int(d.get("acs_pdx_score", 0) or 0),
            acs_adx_scores   = list(d.get("acs_adx_scores") or []),
            ehr_documents    = list(d.get("ehr_documents") or []),
        )

    def to_dict(self) -> dict:
        return {
            "episode_id":       self.episode_id,
            "patient_age":      self.patient_age,
            "patient_sex":      self.patient_sex,
            "pdx":              self.pdx,
            "adx":              self.adx,
            "achi_codes":       self.achi_codes,
            "los_days":         self.los_days,
            "same_day":         self.same_day,
            "separation_mode":  self.separation_mode,
            "admission_weight": self.admission_weight,
            "hours_mech_vent":  self.hours_mech_vent,
            "care_type":        self.care_type,
            "acs_pdx_score":    self.acs_pdx_score,
            "acs_adx_scores":   self.acs_adx_scores,
            "ehr_documents":    self.ehr_documents,
        }

    def to_grouper_input(self) -> dict:
        """Subset dict accepted by ARDRGGrouper.group_episode()."""
        return {
            "episode_id":       self.episode_id,
            "patient_age":      self.patient_age,
            "patient_sex":      self.patient_sex,
            "pdx":              self.pdx,
            "adx":              self.adx,
            "achi_codes":       self.achi_codes,
            "los_days":         self.los_days,
            "same_day":         self.same_day,
            "separation_mode":  self.separation_mode,
            "admission_weight": self.admission_weight,
            "hours_mech_vent":  self.hours_mech_vent,
            "care_type":        self.care_type,
        }


# ---------------------------------------------------------------------------
# ACSScore — per-diagnosis ACS scoring result
# ---------------------------------------------------------------------------

@dataclass
class ACSScore:
    """
    ACS scoring result for a single diagnosis.

    ACS 0001 — Principal Diagnosis (max 7 pts):
      +3 confirmed by investigation
      +2 documented by physician
      +2 reason for admission

    ACS 0002 — Additional Diagnoses (max 8 pts):
      +3 therapeutic treatment altered
      +3 diagnostic procedure ordered
      +2 increased clinical care
    """
    code:            str
    score:           int
    is_principal:    bool
    action:          str       # "code" | "review" | "do_not_code"
    justification:   str
    score_breakdown: dict

    @classmethod
    def from_score(cls, code: str, score: int, is_pdx: bool,
                   breakdown: dict | None = None,
                   justification: str = "") -> ACSScore:
        if score >= ACS_THRESHOLD_CODE:
            action = "code"
        elif score >= ACS_THRESHOLD_REVIEW:
            action = "review"
        else:
            action = "do_not_code"
        return cls(
            code            = code,
            score           = score,
            is_principal    = is_pdx,
            action          = action,
            justification   = justification,
            score_breakdown = breakdown or {},
        )

    def to_dict(self) -> dict:
        return {
            "code":            self.code,
            "score":           self.score,
            "is_principal":    self.is_principal,
            "action":          self.action,
            "justification":   self.justification,
            "score_breakdown": self.score_breakdown,
        }


# ---------------------------------------------------------------------------
# CodingSuggestion — physician-facing output with approval gate
# ---------------------------------------------------------------------------

APPROVAL_PENDING  = "PENDING"
APPROVAL_APPROVED = "APPROVED"
APPROVAL_REJECTED = "REJECTED"


@dataclass
class CodingSuggestion:
    """
    Final NOVIQ Engine output — GrouperResult + full provenance chain.

    Non-negotiable gate: no claim can be submitted until
    approval_status == APPROVED and approved_by is set.

    Call assert_approved() immediately before any claim submission.
    """
    episode_id:          str
    suggestion_id:       str = field(default_factory=lambda: str(uuid.uuid4()))
    approval_status:     str = APPROVAL_PENDING
    approved_by:         Any = None
    approved_at:         Any = None

    # Proposed codes
    proposed_pdx:        str       = ""
    proposed_adx:        list[str] = field(default_factory=list)
    proposed_achi:       list[str] = field(default_factory=list)
    ar_drg_code:         str       = ""
    ar_drg_description:  str       = ""

    # ACS scoring
    acs_pdx_score:       int        = 0
    acs_adx_scores:      list[dict] = field(default_factory=list)
    coding_justification: str       = ""

    # Full module outputs — complete audit trail
    grouper_result:      dict = field(default_factory=dict)
    validation_result:   dict = field(default_factory=dict)

    # Provenance chain
    ehr_documents_read:    list[str] = field(default_factory=list)
    acs_trigger:           str       = ""
    achi_trigger:          str       = ""
    dcl_excluded_count:    int       = 0
    upcoding_risk_count:   int       = 0

    # Physician-facing flags
    flags:               list[str] = field(default_factory=list)

    # Metadata
    created_at:          str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    engine_version:      str = "V11.0"

    # ------------------------------------------------------------------
    # Physician approval gate
    # ------------------------------------------------------------------

    def approve(self, physician_id: str) -> None:
        """
        Physician approves the coding suggestion.
        Must be called before assert_approved() allows claim submission.
        """
        if not physician_id or not physician_id.strip():
            raise ValueError("physician_id is required to approve.")
        self.approval_status = APPROVAL_APPROVED
        self.approved_by     = physician_id.strip()
        self.approved_at     = datetime.now(timezone.utc).isoformat()

    def reject(self, physician_id: str, reason: str = "") -> None:
        """Physician rejects the suggestion — flags for recoding."""
        if not physician_id or not physician_id.strip():
            raise ValueError("physician_id is required to reject.")
        self.approval_status = APPROVAL_REJECTED
        self.approved_by     = physician_id.strip()
        self.approved_at     = datetime.now(timezone.utc).isoformat()
        if reason:
            self.flags.append(f"REJECTED by {physician_id}: {reason}")

    def assert_approved(self) -> None:
        """
        Non-negotiable gate — call immediately before claim submission.
        Raises PermissionError if not APPROVED.
        """
        if self.approval_status != APPROVAL_APPROVED:
            raise PermissionError(
                f"Claim blocked: suggestion {self.suggestion_id} "
                f"status='{self.approval_status}'. "
                f"Physician approval required before submission."
            )
        if not self.approved_by:
            raise PermissionError(
                f"Claim blocked: approved_by not set on {self.suggestion_id}."
            )

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "episode_id":      self.episode_id,
            "suggestion_id":   self.suggestion_id,
            "approval_status": self.approval_status,
            "approved_by":     self.approved_by,
            "approved_at":     self.approved_at,
            "proposed_codes": {
                "pdx":         self.proposed_pdx,
                "adx":         self.proposed_adx,
                "achi":        self.proposed_achi,
                "ar_drg":      self.ar_drg_code,
                "ar_drg_desc": self.ar_drg_description,
            },
            "acs_scores": {
                "pdx_score":            self.acs_pdx_score,
                "adx_scores":           self.acs_adx_scores,
                "coding_justification": self.coding_justification,
            },
            "grouper_result":    self.grouper_result,
            "validation_result": self.validation_result,
            "provenance": {
                "ehr_documents_read":  self.ehr_documents_read,
                "acs_trigger":         self.acs_trigger,
                "achi_trigger":        self.achi_trigger,
                "dcl_excluded_count":  self.dcl_excluded_count,
                "upcoding_risk_count": self.upcoding_risk_count,
            },
            "flags":          self.flags,
            "created_at":     self.created_at,
            "engine_version": self.engine_version,
        }

    @classmethod
    def from_pipeline_results(
        cls,
        episode:           EpisodeRecord,
        grouper_result:    dict,
        validation_result: dict,
    ) -> CodingSuggestion:
        """
        Build a CodingSuggestion from the full pipeline outputs.
        Called by NOVIQEngine after all modules have run.
        """
        # Extract ACHI trigger from grouper step trace
        achi_trigger = next(
            (s for s in grouper_result.get("step_trace", []) if "Step 4:" in s),
            ""
        )

        # Extract counts from validation summary
        summary        = validation_result.get("summary", {})
        excl_count     = summary.get("total_excluded", 0)
        upcoding_count = summary.get("upcoding_risk_count", 0)

        # Build physician-facing flags from upcoding risks
        flags = [
            f"UPCODING RISK: {e['code']} ({e.get('description','')}) — "
            f"{e.get('exclusion_reason','excluded from complexity scoring')}"
            for e in validation_result.get("excluded_codes", [])
            if e.get("upcoding_risk")
        ]

        # Add review flags for ACS scores in the review band
        for adx in episode.acs_adx_scores:
            s = adx.get("score", 0)
            if ACS_THRESHOLD_REVIEW <= s < ACS_THRESHOLD_CODE:
                flags.append(
                    f"ACS REVIEW: {adx.get('code','')} score={s} — "
                    f"physician review required before coding"
                )

        # Plain-language justification
        justification = _build_justification(episode, grouper_result)

        acs_trigger = (
            f"ACS 0001 PDX score={episode.acs_pdx_score}/7 "
            f"({'code' if episode.acs_pdx_score >= ACS_THRESHOLD_CODE else 'review'})"
        )

        return cls(
            episode_id            = episode.episode_id,
            proposed_pdx          = episode.pdx,
            proposed_adx          = episode.adx,
            proposed_achi         = episode.achi_codes,
            ar_drg_code           = grouper_result.get("ar_drg_code", ""),
            ar_drg_description    = grouper_result.get("ar_drg_description", ""),
            acs_pdx_score         = episode.acs_pdx_score,
            acs_adx_scores        = episode.acs_adx_scores,
            coding_justification  = justification,
            grouper_result        = grouper_result,
            validation_result     = validation_result,
            ehr_documents_read    = episode.ehr_documents,
            acs_trigger           = acs_trigger,
            achi_trigger          = achi_trigger,
            dcl_excluded_count    = excl_count,
            upcoding_risk_count   = upcoding_count,
            flags                 = flags,
        )


def _build_justification(episode: EpisodeRecord, grouper_result: dict) -> str:
    drg  = grouper_result.get("ar_drg_code", "")
    eccs = grouper_result.get("eccs", 0.0)
    parts = [f"PDX {episode.pdx} → AR-DRG {drg}."]
    if episode.achi_codes:
        parts.append(f"Procedure(s): {', '.join(episode.achi_codes)}.")
    if eccs > 0:
        parts.append(f"ECCS: {eccs:.4f}.")
    if episode.acs_pdx_score >= ACS_THRESHOLD_CODE:
        parts.append(
            f"ACS 0001 score {episode.acs_pdx_score}/7 — "
            f"meets coding threshold (≥{ACS_THRESHOLD_CODE})."
        )
    elif episode.acs_pdx_score >= ACS_THRESHOLD_REVIEW:
        parts.append(
            f"ACS 0001 score {episode.acs_pdx_score}/7 — review band."
        )
    return " ".join(parts)
