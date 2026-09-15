"""Candidate processing layer.

Sits between discovery (``data/raw/discovery/``) and normalization. It owns
everything that must happen to an *unverified* discovery candidate before the
pipeline is allowed to treat it as a tool record:

* :mod:`src.candidates.store` — resilient (re)loading of persisted raw
  candidates, with an auditable report of what was skipped and why.

Nothing in this layer verifies a candidate: verification against the official
website happens later, in :mod:`src.verification`.
"""
