"""Strict data contracts used by the parallel_v4 pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class V4BlockStatus(str, Enum):
    PENDING = "pending"
    SCANNED = "scanned"
    READY = "ready"
    TRANSLATING = "translating"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    NEEDS_REVALIDATE = "needs_revalidate"
    INCOMPLETE_REQUIRES_HUMAN = "incomplete_requires_human"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScanMention(StrictModel):
    paragraph_id: str = Field(pattern=r"^P\d{3}$")
    source_form: str = Field(min_length=1, max_length=200)
    category: str = Field(
        default="concept",
        pattern=(
            r"^(person|place|organization|group|item|concept|unit|title|"
            r"event|species|technology|work)$"
        ),
    )
    suggested_target: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=600)
    discourse_function: str = Field(
        default="referential",
        pattern=r"^(referential|vocative|institutional|unknown)$",
    )
    evidence_quote: str = Field(min_length=1, max_length=500)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    candidate_id: str = Field(default="", max_length=80)
    canonical_form: str = Field(default="", max_length=200)


class ScanAmbiguity(StrictModel):
    paragraph_id: str = Field(pattern=r"^P\d{3}$")
    evidence_quote: str = Field(min_length=1, max_length=500)
    constraint: str = Field(min_length=1, max_length=600)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class ScanResponse(StrictModel):
    mentions: List[ScanMention] = Field(default_factory=list)
    ambiguities: List[ScanAmbiguity] = Field(default_factory=list)


class CandidateMentionDecision(StrictModel):
    candidate_id: str = Field(pattern=r"^C\d{3}$")
    category: str = Field(
        default="concept",
        pattern=(
            r"^(person|place|organization|group|item|concept|unit|title|"
            r"event|species|technology|work)$"
        ),
    )
    suggested_target: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=600)
    discourse_function: str = Field(
        default="referential",
        pattern=r"^(referential|vocative|institutional|unknown)$",
    )
    canonical_candidate_id: Optional[str] = Field(
        default=None,
        pattern=r"^C\d{3}$",
    )
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class CandidateScanResponse(StrictModel):
    mentions: List[CandidateMentionDecision] = Field(default_factory=list)


class VerificationResponse(StrictModel):
    verdict: str = Field(pattern=r"^(support|reject|uncertain)$")
    rationale: str = Field(min_length=1, max_length=1200)
    evidence_quotes: List[str] = Field(default_factory=list, max_length=10)


class RepairResponse(StrictModel):
    paragraphs: List[str] = Field(min_length=1)
    repair_notes: List[str] = Field(default_factory=list, max_length=20)


@dataclass(frozen=True)
class V4Block:
    id: str
    source_edition_id: int
    chapter_id: str
    chapter_index: int
    block_index: int
    global_index: int
    block_type: str
    source_text: str
    source_hash: str
    token_count: int
    status: str
    legacy_id: str = ""


@dataclass
class ScanOutcome:
    block: V4Block
    response: Optional[ScanResponse]
    raw_response: str = ""
    request_payload: Dict[str, Any] = field(default_factory=dict)
    attempts: int = 0
    elapsed_ms: int = 0
    error: Optional[str] = None
    audit_calls: List[Dict[str, Any]] = field(default_factory=list)
    lexical_candidates: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class ContextPacket:
    block_id: str
    knowledge_version: int
    rendered: str
    required_chars: int
    matched_concept_ids: List[str] = field(default_factory=list)
    matched_claim_ids: List[str] = field(default_factory=list)


@dataclass
class TranslationOutcome:
    block: V4Block
    knowledge_version: int
    status: str
    draft_translation: str = ""
    final_translation: str = ""
    analysis: str = ""
    semantic_obligations: str = ""
    memory_summary: str = ""
    warnings: List[str] = field(default_factory=list)
    term_proposals: List[Dict[str, Any]] = field(default_factory=list)
    relation_proposals: List[Dict[str, Any]] = field(default_factory=list)
    claim_dependencies: List[str] = field(default_factory=list)
    audit_calls: List[Dict[str, Any]] = field(default_factory=list)
    attempts: int = 1
    elapsed_ms: int = 0
    error: Optional[str] = None


@dataclass(frozen=True)
class Island:
    id: str
    blocks: List[V4Block]
