"""Official-website verification stage.

The only stage allowed to promote a record out of ``unverified``, and only from
evidence fetched from the product's **own** website. See
:mod:`src.verification.verifier`.
"""

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
    "LivenessChecker",
    "LivenessResult",
    "OfficialPageEvidence",
    "OfficialSiteVerifier",
    "VerificationFailure",
    "VerificationReport",
    "VerificationResult",
    "extract_official_evidence",
    "resolve_conflict",
]
