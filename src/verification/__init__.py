"""Official-website verification stage.

The only stage allowed to promote a record out of ``unverified``, and only from
evidence fetched from the product's **own** website. See
:mod:`src.verification.verifier`.
"""

from src.verification.runner import (
    VERIFICATION_STATE_FILENAME,
    CandidateVerificationRunner,
    completed_keys,
    summarize_verified_file,
    verification_key,
)
from src.verification.store import (
    VERIFICATION_REPORT_FILENAME,
    VERIFIED_FILENAME,
    discovery_provenance,
    persist_verification,
    verification_record,
)
from src.verification.verifier import (
    LivenessChecker,
    LivenessResult,
    OfficialPageEvidence,
    OfficialSiteVerifier,
    VerificationFailure,
    VerificationReport,
    VerificationResult,
    extract_official_evidence,
    resolve_conflict,
)

__all__ = [
    "CandidateVerificationRunner",
    "VERIFICATION_STATE_FILENAME",
    "completed_keys",
    "summarize_verified_file",
    "verification_key",
    "LivenessChecker",
    "LivenessResult",
    "OfficialPageEvidence",
    "OfficialSiteVerifier",
    "VerificationFailure",
    "VerificationReport",
    "VerificationResult",
    "VERIFICATION_REPORT_FILENAME",
    "VERIFIED_FILENAME",
    "discovery_provenance",
    "extract_official_evidence",
    "persist_verification",
    "resolve_conflict",
    "verification_record",
]
