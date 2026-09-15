"""Candidate processing layer.

Sits between discovery (``data/raw/discovery/``) and normalization. It owns
everything that must happen to an *unverified* discovery candidate before the
pipeline is allowed to treat it as a tool record:

* :mod:`src.candidates.store` — resilient (re)loading of persisted raw
  candidates, with an auditable report of what was skipped and why;
* :mod:`src.candidates.prepare` — normalization + required-identity validation
  producing stable, explicitly **unverified** ``PreparedCandidate`` records
  under ``data/interim/``.

Nothing in this layer verifies a candidate: verification against the official
website happens later, in :mod:`src.verification`.
"""
