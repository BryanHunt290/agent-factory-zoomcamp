"""Evidence-based capability assessment: "can this installation do X, Y, and Z?" (docs/capabilities.md).

Requirements name capability IDs. A model may later help extract them from a job description, but the
answer comes only from the registry: what is registered, what is available now, what needs a person's
approval, and what proves it. A model's opinion is never evidence, and nothing here calls a model, a
network, or an external service. The output is deterministic: the same registry state and the same
requirements give the same report.

This is the boundary a future marketplace integration (for example Upwork) would call. It is a
read-only report: it submits, applies for, or promises nothing.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ..safety.permissions import Decision, PermissionPolicy
from .model import CAPABILITY_ID_RE, ApprovalRule, CapabilityError
from .registry import CapabilityRegistry

_REQUIREMENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
MAX_REQUIREMENTS = 100


@dataclass(frozen=True)
class Requirement:
    """One need, by ID. ``capability_id`` None means no capability covers it; ``human`` means a person must do it."""

    requirement_id: str
    capability_id: str | None = None
    description: str = ""
    human: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.requirement_id, str) or not _REQUIREMENT_ID_RE.fullmatch(self.requirement_id):
            raise CapabilityError(f"malformed requirement id {self.requirement_id!r}")
        if not isinstance(self.description, str) or len(self.description) > 200:
            raise CapabilityError("a requirement description is at most 200 characters")


@dataclass(frozen=True)
class AssessmentEntry:
    requirement_id: str
    capability_id: str | None
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"requirement": self.requirement_id, "capability": self.capability_id, "reason": self.reason}


@dataclass(frozen=True)
class CapabilityAssessment:
    supported: tuple[AssessmentEntry, ...]
    unavailable: tuple[AssessmentEntry, ...]
    missing: tuple[AssessmentEntry, ...]
    requires_approval: tuple[AssessmentEntry, ...]
    requires_human: tuple[AssessmentEntry, ...]
    #: Per capability referenced: the evidence behind the classification.
    evidence: tuple[tuple[str, tuple[dict[str, Any], ...]], ...]

    @property
    def fully_supported(self) -> bool:
        return not (self.unavailable or self.missing or self.requires_human)

    def to_dict(self) -> dict[str, Any]:
        return {
            "supported": [e.to_dict() for e in self.supported],
            "unavailable": [e.to_dict() for e in self.unavailable],
            "missing": [e.to_dict() for e in self.missing],
            "requires_approval": [e.to_dict() for e in self.requires_approval],
            "requires_human": [e.to_dict() for e in self.requires_human],
            "evidence": [{"capability": cid, "items": list(items)} for cid, items in self.evidence],
            "basis": "registry",  # never a model's opinion
        }


def assess(registry: CapabilityRegistry, requirements: Iterable[Requirement], *,
           policy: PermissionPolicy | None = None) -> CapabilityAssessment:  # fmt: skip
    """Classify each requirement against the registry. Unknown or malformed capability IDs are missing."""
    policy = policy or PermissionPolicy()
    reqs = list(requirements)
    if len(reqs) > MAX_REQUIREMENTS:
        raise CapabilityError(f"at most {MAX_REQUIREMENTS} requirements per assessment")
    if len({r.requirement_id for r in reqs}) != len(reqs):
        raise CapabilityError("requirement ids must be unique")
    buckets: dict[str, list[AssessmentEntry]] = {k: [] for k in ("supported", "unavailable", "missing",
                                                                 "requires_approval", "requires_human")}  # fmt: skip
    referenced: set[str] = set()
    for req in sorted(reqs, key=lambda r: r.requirement_id):
        cid = req.capability_id
        if req.human:
            buckets["requires_human"].append(AssessmentEntry(req.requirement_id, cid, "needs a person"))
            continue
        if cid is None:
            buckets["missing"].append(AssessmentEntry(req.requirement_id, None, "no capability covers it"))
            continue
        if not isinstance(cid, str) or not CAPABILITY_ID_RE.fullmatch(cid):
            buckets["missing"].append(AssessmentEntry(req.requirement_id, None, "malformed capability id"))
            continue
        cap = registry.get(cid)
        if cap is None:
            buckets["missing"].append(AssessmentEntry(req.requirement_id, cid, "not registered"))
            continue
        referenced.add(cid)
        rule = policy.category_rules.get(cap.category, Decision.DENY)
        availability = registry.availability(cid)
        if cap.approval is ApprovalRule.DENIED or rule is Decision.DENY:
            buckets["unavailable"].append(AssessmentEntry(req.requirement_id, cid, "denied by policy"))
        elif not availability.available:
            buckets["unavailable"].append(AssessmentEntry(req.requirement_id, cid, availability.status.value))
        elif registry.requires_approval(cap, policy):
            buckets["requires_approval"].append(AssessmentEntry(req.requirement_id, cid, "a person must approve each action"))
        else:
            buckets["supported"].append(AssessmentEntry(req.requirement_id, cid, "available"))
    evidence = tuple((cid, tuple(e.to_dict() for e in registry.evidence(cid, policy))) for cid in sorted(referenced))
    return CapabilityAssessment(**{k: tuple(v) for k, v in buckets.items()}, evidence=evidence)


__all__ = ["AssessmentEntry", "CapabilityAssessment", "MAX_REQUIREMENTS", "Requirement", "assess"]
