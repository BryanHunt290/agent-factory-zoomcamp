"""Compose runtime capability evidence; no business scoring or pricing."""

from dataclasses import dataclass
import re

from ...capabilities.assessment import Requirement, assess
from ...capabilities.model import CAPABILITY_ID_RE
from .contracts import digest, require


@dataclass(frozen=True)
class RequirementMapping:
    """Trusted caller input, never extracted from marketplace claims by U1.

Each mapping binds to the exact normalized observation, not merely its job ID.
This is a technical mapping, not an assertion of professional qualifications.
"""
    observation_digest: str
    requirements: tuple[Requirement, ...]

    def __post_init__(self):
        require(type(self.observation_digest) is str and re.fullmatch(r"[0-9a-f]{64}", self.observation_digest) is not None)
        require(type(self.requirements) is tuple and 1 <= len(self.requirements) <= 30)
        require(all(type(r) is Requirement for r in self.requirements))
        require(len({r.requirement_id for r in self.requirements}) == len(self.requirements))
        for r in self.requirements:
            require(type(r.human) is bool)
            require(r.capability_id is None or (type(r.capability_id) is str and CAPABILITY_ID_RE.fullmatch(r.capability_id)))
            # Descriptions are neither needed nor copied into reports.
            require(r.description == "")
            require(re.fullmatch(r"r[1-9][0-9]{0,2}", r.requirement_id) is not None)

    def signature(self):
        return digest([self.observation_digest, sorted(
            (r.requirement_id, r.capability_id, r.human) for r in self.requirements)])


def review_batch(batch, mappings, registry, policy):
    require(type(mappings) is tuple and len(mappings) <= 3)
    require(all(type(m) is RequirementMapping for m in mappings))
    by_digest = {m.observation_digest: m for m in mappings}
    require(len(by_digest) == len(mappings))
    facts = batch.facts()
    require(set(by_digest) <= {j["observation_digest"] for j in facts})
    results = []
    for job in facts:
        mapping = by_digest.get(job["observation_digest"])
        report = assess(registry, mapping.requirements, policy=policy).to_dict() if mapping else None
        results.append({**job, "capability_assessment": report,
                        "mapping_digest": mapping.signature() if mapping else None,
                        "mapping_status": "provided_by_operator" if mapping else "missing",
                        "application_ready": False, "execution_authorized": False})
    return {"schema_version": "opportunity-review-v1", "status": "REVIEW_READY",
            "source_mode": "offline_fake", "batch_digest": batch.fingerprint,
            "jobs": results, "ranking": None, "pricing": None,
            "application_ready": False, "execution_authorized": False,
            "live_transport_enabled": False}
