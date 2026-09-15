"""A small, version-independent robots.txt matcher (RFC 9309).

Why this module exists instead of ``urllib.robotparser``
--------------------------------------------------------
The stdlib parser silently changes its answers between Python versions, and on
Python <= 3.12 it gets a *very* common real-world robots.txt shape wrong. Both
defects were found by the Run #9 investigation of
``tests/fixtures/robots_allow_root.txt``::

    User-agent: *
    Allow: /
                      <- blank line
    Disallow: /api/
    Disallow: /_nuxt/
    ...

1. **Blank lines truncate a group (Python <= 3.12).** ``parse()`` treats an
   empty line as a group terminator, so the group above ends after ``Allow: /``
   and all four ``Disallow`` rules are silently discarded. RFC 9309 §2.2
   delimits groups by ``user-agent`` lines, *not* by blank lines. Result:
   ``/api/private`` was reported as crawlable. That is a genuine politeness
   bug, not a test artefact.

2. **First-match instead of longest-match (Python <= 3.12).**
   ``Entry.allowance`` returns the *first* matching rule, so ``Allow: /``
   written above ``Disallow: /api/`` wins for every path. RFC 9309 §2.2.2
   requires the **most specific** (longest) pattern to win, with ``allow``
   breaking a tie. Python 3.13 rewrote this to longest-match, which is why the
   same fixture answers differently on 3.13 than on 3.12.

Because the project must enforce genuine ``Disallow`` rules identically on
every interpreter, robots matching is implemented here rather than inherited
from whichever stdlib the run happens to use.

Deliberately preserved behaviour (Run #7 intent)
------------------------------------------------
This module only answers "what do these rules say". It is never the thing that
decides an *absent* robots.txt means "forbidden": a 4xx/5xx, a network error or
an unparseable body leaves the caller with no rules, which the transport treats
as "no restriction found". Only explicitly published rules can block.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

__all__ = ["RobotsRules"]


def _normalize(value: str) -> str:
    """Percent-decode once so pattern and path are compared on equal terms."""
    try:
        return unquote(value)
    except Exception:  # noqa: BLE001 - malformed escapes are compared verbatim
        return value


def _compile(pattern: str) -> re.Pattern[str]:
    """Translate a robots path pattern (``*`` wildcard, ``$`` anchor) to regex.

    RFC 9309 §2.2.3: ``*`` matches any sequence of characters and a trailing
    ``$`` anchors the match at the end of the path. Everything else is literal.
    """
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = "".join(".*" if part == "*" else re.escape(part) for part in re.split(r"(\*)", body))
    return re.compile(f"^{regex}$" if anchored else f"^{regex}")


@dataclass(frozen=True)
class _Rule:
    """One ``Allow``/``Disallow`` line."""

    pattern: str
    allow: bool
    regex: re.Pattern[str]

    @property
    def specificity(self) -> int:
        """Length of the pattern — RFC 9309's "most specific match" metric."""
        return len(self.pattern)

    def matches(self, path: str) -> bool:
        return self.regex.search(path) is not None


class RobotsRules:
    """Parsed robots.txt rules, queried with :meth:`can_fetch`."""

    def __init__(self, groups: dict[str, list[_Rule]] | None = None) -> None:
        #: Lower-cased product token -> its rules. ``*`` is the catch-all group.
        self._groups: dict[str, list[_Rule]] = groups or {}

    # ------------------------------------------------------------- parsing
    @classmethod
    def parse(cls, text: str) -> RobotsRules:
        """Parse robots.txt content.

        Groups are delimited by ``user-agent`` lines only; blank lines are
        insignificant. Consecutive ``user-agent`` lines share one rule set.
        """
        groups: dict[str, list[_Rule]] = {}
        current: list[str] = []
        # True once the active group has at least one rule, so the next
        # ``user-agent`` line is understood to open a *new* group.
        group_has_rules = False

        for raw in (text or "").splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            field, _, value = line.partition(":")
            field = field.strip().lower()
            value = value.strip()

            if field == "user-agent":
                if group_has_rules:
                    current = []
                    group_has_rules = False
                if value:
                    current.append(value.lower())
                    groups.setdefault(value.lower(), [])
                continue

            if field not in {"allow", "disallow"} or not current:
                continue

            group_has_rules = True
            if not value:
                # "Disallow:" with an empty value imposes no restriction
                # (RFC 9309 §2.2.2); an empty "Allow:" is likewise a no-op.
                continue

            rule = _Rule(
                pattern=_normalize(value),
                allow=field == "allow",
                regex=_compile(_normalize(value)),
            )
            for agent in current:
                groups[agent].append(rule)

        return cls(groups)

    # ------------------------------------------------------------ querying
    def _select_group(self, user_agent: str) -> list[_Rule] | None:
        """Pick the most specific matching group, else the ``*`` group.

        RFC 9309 §2.2.1: the crawler obeys the group whose product token is the
        most specific match for its user agent.
        """
        token = (user_agent or "").split("/")[0].strip().lower()
        best: tuple[int, list[_Rule]] | None = None
        for agent, rules in self._groups.items():
            if agent == "*":
                continue
            if token and agent in token and (best is None or len(agent) > best[0]):
                best = (len(agent), rules)
        if best is not None:
            return best[1]
        return self._groups.get("*")

    def can_fetch(self, user_agent: str, url: str) -> bool:
        """Return whether ``user_agent`` may fetch ``url``.

        Nothing published for this agent means nothing is forbidden. Otherwise
        the longest matching pattern decides, and ``Allow`` wins an exact tie
        (RFC 9309 §2.2.2).
        """
        rules = self._select_group(user_agent)
        if not rules:
            return True

        parts = urlsplit(url)
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        path = _normalize(path)

        decision = True
        best = -1
        for rule in rules:
            if not rule.matches(path):
                continue
            if rule.specificity > best:
                best, decision = rule.specificity, rule.allow
            elif rule.specificity == best and rule.allow:
                decision = True  # ties favour access
        return decision
