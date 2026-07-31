"""
Output-quality measurement for the Grant Analyser.

This module turns a finished analysis into numbers. It is the single place
where "is the output any good?" is defined, so that the app, the eval harness
and the fortnightly review cycle all answer that question the same way.

Design constraints, in order of importance:

1.  **Pure.** No network, no database, no API key, no imports from `analyzer`.
    It scores dictionaries. That makes it unit-testable, cheap to import, and
    runnable in environments (like a review session's sandbox) that cannot
    reach the outside world.
2.  **Honest naming.** Recall and exclusion accuracy are measured against
    ground truth and only exist for benchmark companies. Real user runs have
    no ground truth, so they get *structural* measures instead — and those are
    named `structural_*` so nobody mistakes them for the real thing.
3.  **Consistency is a diagnostic, never a target.** `convergence()` exists to
    detect near-random retrieval, per §9a of the launch plan. Nothing in this
    module, or anything built on it, should be used to make output stable by
    caching or pinning; that re-serves a poor answer and destroys the live-
    search differentiator.

The quality properties referred to below (Q1–Q8) are defined in §9a, "What good
output looks like", of `GRANT_ANALYZER_LAUNCH_PLAN.md`.
"""

from __future__ import annotations

import re
from itertools import combinations
from typing import Any, Iterable, Sequence

# ---------------------------------------------------------------------------
# Name normalisation
#
# Programme names arrive in many surface forms for the same thing: "EIC
# Accelerator Open 2025", "EIC Accelerator (2025-2026 cut-offs)", "EIC
# Accelerator — full application". Comparing raw strings across runs therefore
# understates agreement, and comparing only loosely overstates it. We compute
# both, and treat the gap between them as its own finding: a wide gap means
# programme identity is not normalised anywhere in the pipeline, which is what
# defeats de-duplication.
#
# Nothing here names a funder or a programme. The token lists below describe
# *classes* of word — programme scaffolding, legal suffixes, date fragments —
# so the behaviour generalises to funders never seen in testing.
# ---------------------------------------------------------------------------

# Words that describe the *shape* of a funding instrument rather than identify
# it. Two names differing only in these words are the same programme.
_FILLER_WORDS = {
    "a", "an", "and", "the", "of", "for", "to", "in", "on", "with",
    "programme", "program", "programmes", "programs",
    "fund", "funds", "funding",
    "grant", "grants", "grantee",
    "scheme", "schemes",
    "call", "calls", "competition", "competitions",
    "round", "rounds", "cycle", "cycles", "window", "windows",
    "phase", "phases", "stage", "stages", "step", "steps",
    "open", "opens", "opening", "closed", "closing", "close",
    "application", "applications", "apply", "submission", "submissions",
    "deadline", "deadlines", "cutoff", "cutoffs", "cut", "off", "offs",
    "full", "short", "first", "second", "third",
    "award", "awards", "prize", "prizes",
    "initiative", "initiatives", "programmeme",
    "support", "supported", "supporting",
    "new", "current", "upcoming", "latest", "next", "annual",
    "general", "standard", "core", "main",
}

# Legal / organisational suffixes that carry no identifying information.
_BODY_SUFFIXES = {
    "ltd", "limited", "plc", "llc", "inc", "gmbh", "sa", "nv", "bv", "ab",
    "agency", "agencies", "authority", "authorities", "department",
    "ministry", "office", "council", "councils", "commission", "committee",
    "board", "board's", "executive", "government", "govt",
    "organisation", "organization", "association", "foundation", "trust",
    "institute", "institution", "centre", "center", "network",
}

_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_ROMAN_RE = re.compile(r"^[ivxlcdm]+$")
_PARENS_RE = re.compile(r"\([^)]*\)")
_NON_WORD_RE = re.compile(r"[^a-z0-9\s]+")
_WS_RE = re.compile(r"\s+")


def normalise_name(name: str) -> str:
    """Lowercase, strip parentheticals, punctuation, years and filler words.

    Used for the *exact* comparison level: two names that normalise to the same
    string are unambiguously the same programme.
    """
    text = (name or "").lower()
    text = _PARENS_RE.sub(" ", text)
    text = _YEAR_RE.sub(" ", text)
    text = _NON_WORD_RE.sub(" ", text)
    tokens = [t for t in _WS_RE.split(text) if t]
    keep = [
        t for t in tokens
        if t not in _FILLER_WORDS
        and not t.isdigit()
        and not _ROMAN_RE.match(t)
    ]
    return " ".join(keep).strip()


def _raw_body_tokens(managing_body: str) -> list[str]:
    """Word tokens of the funder name, before legal scaffolding is removed.

    Only the part before the first '/' is used: bodies are often recorded as
    "European Innovation Council (EIC) / European Commission", where the text
    after the slash is the parent department rather than the funder itself.
    """
    text = (managing_body or "").lower().split("/")[0]
    text = _PARENS_RE.sub(" ", text)
    text = _NON_WORD_RE.sub(" ", text)
    tokens = [t for t in _WS_RE.split(text) if t]
    return [t for t in tokens if t not in _FILLER_WORDS and not t.isdigit()]


def _body_identity(managing_body: str) -> tuple[list[str], set[str]]:
    """Return (significant funder tokens, every string that stands for the funder).

    The second value includes the funder's initials, so a programme titled with
    the funder's acronym is recognised as repeating the funder's own name even
    when the body field spells it out in full.
    """
    raw = _raw_body_tokens(managing_body)
    significant = [t for t in raw if t not in _BODY_SUFFIXES and len(t) > 1]
    aliases = set(raw) | set(significant)
    if len(raw) > 1:
        aliases.add("".join(t[0] for t in raw))
    if len(significant) > 1:
        aliases.add("".join(t[0] for t in significant))
    return significant, aliases


# Abbreviations that appear inside programme titles but identify a category, a
# geography or a metric rather than the programme itself. Generic by design —
# no funder or scheme is named here.
_GENERIC_ACRONYMS = {
    "SME", "SMES", "RD", "TRL", "IP", "AI", "ML", "VAT", "PLC", "LTD", "LLC",
    "GMBH", "CO2", "CO", "GHG", "ESG", "KPI", "MW", "KW", "GW", "TWH", "KWH",
    "EUR", "GBP", "USD", "NZD", "AUD", "TBC", "TBD", "NA", "FAQ", "PDF",
    "UK", "EU", "US", "USA", "NZ", "AU", "DE", "FR", "IE", "GB",
}

_ACRONYM_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,}\b")


def _abbreviates(acronym: str, tokens: Sequence[str]) -> bool:
    """True when the acronym is the initials of consecutive words in the title.

    This is the definition of an abbreviation, and using it as a test is what
    stops an umbrella abbreviation being mistaken for a programme's identity.
    A title reading "<Umbrella> Accelerator" carries the umbrella's letters but
    those letters spell nothing in the title, so the umbrella is rejected and
    the programme keeps its descriptive identity — which matters, because
    several quite different programmes sit under one umbrella and merging them
    would inflate agreement.
    """
    letters = acronym.lower()
    initials = [t[0] for t in tokens]
    for start in range(len(initials) - len(letters) + 1):
        if "".join(initials[start:start + len(letters)]) == letters:
            return True
    return False


def _identifying_acronym(name: str, body_aliases: set[str]) -> str:
    """The abbreviation in a programme title that identifies *this* programme.

    A parenthetical or inline abbreviation is the most stable part of a
    programme's name across runs: the surrounding words get rewritten,
    reordered and re-punctuated, but the abbreviation survives. Funder
    abbreviations and generic business terms are removed first, and what
    remains must actually abbreviate words in the title — see `_abbreviates`.

    Where a title carries more than one qualifying abbreviation, the
    **shortest** wins. True abbreviations are short; a long all-capitals token
    is usually an ordinary word being shouted, and shouting is exactly the kind
    of styling that varies between runs.
    """
    tokens = _title_tokens(name, body_aliases)
    keep = [
        a for a in _ACRONYM_RE.findall(name or "")
        if a not in _GENERIC_ACRONYMS
        and a.lower() not in body_aliases
        and not a.isdigit()
        and _abbreviates(a, tokens)
    ]
    return min(sorted(set(keep)), key=len) if keep else ""


def _title_tokens(name: str, body_aliases: set[str]) -> list[str]:
    """Words of the main title, funder words removed, scaffolding words kept.

    Scaffolding ("fund", "programme") is retained here because an abbreviation
    is built from every word it stands for — "Strategic Innovation Fund" gives
    SIF, not SI.
    """
    text = _PARENS_RE.sub(" ", (name or "").lower())
    text = _YEAR_RE.sub(" ", text)
    text = _NON_WORD_RE.sub(" ", text)
    tokens = [t for t in _WS_RE.split(text) if t]
    return [
        t for t in tokens
        if t not in body_aliases
        and not t.isdigit()
        and not _ROMAN_RE.match(t)
        and t not in {"a", "an", "the", "of", "for", "to", "in", "on", "and", "with"}
    ]




def family_key(name: str, managing_body: str = "", depth: int = 2) -> str:
    """A coarse identity for a funding programme: what distinguishes it, only.

    Two signals, in order:

    1.  **Its own abbreviation**, if it has one that isn't the funder's or a
        generic business term. This is the part of a title that survives
        rewording, so it is the most reliable identity available.
    2.  Otherwise, the programme name with the funder's own words (and
        initials) removed, so that "<Funder> Smart Grants" and "Smart Grants"
        from the same funder collapse to one identity while two genuinely
        different programmes from that funder stay apart.

    Used on its own this is context-free and therefore conservative: a run that
    gives a programme's abbreviation and one that spells it out will not meet.
    `FamilyResolver` closes that gap by learning the pairing from the runs
    being compared, and is what `convergence()` uses. Both are heuristics and
    are meant to be read as such: treat the exact level as the conservative
    floor and the family level as the optimistic ceiling, and read the truth as
    lying between them.

    **The funder is deliberately not part of the key.** The same programme is
    routinely recorded with different body strings between runs — spelled out,
    abbreviated, or attributed to the parent department — so including the body
    would split one programme into several identities and understate agreement,
    which is the exact opposite of what this measure is for. The cost is a small
    chance that two funders' similarly-named programmes collide; that is the
    right trade for a measure used to compare repeat runs of one company, and
    the exact level is always reported alongside as a check.

    `depth` is how many distinguishing tokens to keep. Two is deliberate: one
    over-merges sibling programmes, three splits on cosmetic qualifiers.
    """
    significant, aliases = _body_identity(managing_body)

    acronym = _identifying_acronym(name, aliases)
    if acronym:
        return acronym.lower()
    return _descriptive_key(name, aliases, significant, depth)


def _descriptive_key(
    name: str, aliases: set[str], significant: Sequence[str], depth: int
) -> str:
    """The words-only identity: the title minus the funder's own words."""
    residual = [t for t in normalise_name(name).split() if t and t not in aliases]
    if residual:
        return " ".join(residual[:depth])
    # The name says nothing the funder didn't: identity falls back to the funder.
    return " ".join(significant[:depth]) or normalise_name(name)


class FamilyResolver:
    """Family keys that learn a corpus's abbreviations instead of guessing them.

    The context-free `family_key()` cannot tell that "Knowledge Transfer
    Partnerships" and "Knowledge Transfer Partnership (KTP) Round 2" are one
    programme: one has an abbreviation to key on, the other doesn't. Guessing
    an abbreviation from a title's initials was tried and is worse than
    useless — it fires on titles nobody would ever abbreviate and invents
    differences that aren't there.

    So instead of guessing, this reads the pairing off the corpus. Any item
    that carries *both* an abbreviation and a description teaches that those
    two are the same thing, and every other item is then resolved through what
    was learned. Nothing is hard-coded, so it works for funders and programmes
    never seen before.

    An alias is only learned when it is unambiguous: if one description is seen
    with two different abbreviations, the pairing is dropped rather than
    guessed at, because a wrong merge inflates agreement and inflated agreement
    is precisely the reassurance this measure exists to withhold.
    """

    def __init__(self, items: Iterable[dict], depth: int = 2) -> None:
        self.depth = depth
        candidates: dict[str, set[str]] = {}
        for item in items:
            name = item.get("name") or ""
            body = item.get("managing_body") or ""
            significant, aliases = _body_identity(body)
            acronym = _identifying_acronym(name, aliases)
            if not acronym:
                continue
            described = _descriptive_key(name, aliases, significant, depth)
            if described and described != acronym.lower():
                candidates.setdefault(described, set()).add(acronym.lower())
        self.aliases = {k: next(iter(v)) for k, v in candidates.items() if len(v) == 1}
        self.ambiguous = {k: sorted(v) for k, v in candidates.items() if len(v) > 1}

    def key(self, item: dict) -> str:
        name = item.get("name") or ""
        body = item.get("managing_body") or ""
        significant, aliases = _body_identity(body)
        acronym = _identifying_acronym(name, aliases)
        if acronym:
            return acronym.lower()
        described = _descriptive_key(name, aliases, significant, self.depth)
        return self.aliases.get(described, described)


# ---------------------------------------------------------------------------
# Reading an analysis
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Link authority (Q3)
#
# A link that loads is not the same as a link that can be trusted. A shortlist
# is a claim about where money is; the citation should point at whoever holds
# it. A Facebook post, a consultancy's summary, or a commercial grant-listing
# site may all resolve perfectly and still tell a reader that the shortlist was
# assembled by scraping — which is the "no better than a chatbot" judgement in
# §9a, and the one that costs most, because the person reading is being invited
# to consider hiring the author.
#
# Everything below describes *classes* of domain. No funder or programme is
# named, so the behaviour generalises to funders never seen in testing.
# ---------------------------------------------------------------------------

#: Domain *labels* that mark a host as public sector wherever they appear.
#: Matching on the label rather than a fixed suffix list is what makes this
#: work for gov.uk, gov.wales, govt.nz, gouv.fr and the next country nobody
#: thought to enumerate.
_PUBLIC_SECTOR_LABELS = {"gov", "govt", "gouv", "gob", "govern", "nhs", "mil"}

#: Suffixes for supranational and academic estates, where there is no single
#: label to key on.
_OFFICIAL_SUFFIXES = (
    ".europa.eu", ".int", ".un.org", ".who.int",
    ".ac.uk", ".ac.nz", ".edu", ".edu.au", ".edu.sg", ".gc.ca",
)

#: Platforms that host other people's writing. Authoritative for the author,
#: never for a funding programme.
_PUBLISHING_PLATFORMS = {
    "facebook.com", "m.facebook.com", "twitter.com", "x.com", "instagram.com",
    "linkedin.com", "tiktok.com", "youtube.com", "youtu.be", "reddit.com",
    "medium.com", "substack.com", "wordpress.com", "blogspot.com",
    "blogger.com", "tumblr.com", "wixsite.com", "notion.site",
    "docs.google.com", "drive.google.com", "dropbox.com",
}

#: Words so common across this sector that a domain containing one proves
#: nothing about who runs the programme. Used only to restrain substring
#: matching; an exact token match on these still counts.
_COMMON_SECTOR_TOKENS = {
    "energy", "climate", "green", "clean", "carbon", "sustainability",
    "sustainable", "innovation", "innovate", "research", "technology",
    "science", "future", "global", "national", "european", "europe",
    "international", "development", "enterprise", "business", "industry",
    "industrial", "digital", "environment", "environmental", "impact",
    "growth", "startup", "venture", "capital", "partnership", "network",
}

_GENERIC_DOMAIN_TOKENS = {
    "www", "com", "org", "net", "eu", "int", "co", "uk", "de", "fr", "nl",
    "info", "portal", "apply", "funding", "grants", "grant", "fund", "en",
    "gov", "the", "and", "for", "of",
}


def _acronym_variants(tokens: Sequence[str]) -> set[str]:
    """The short forms an organisation might be known by.

    Initialisms don't always take one letter per word: a short word is often
    kept whole, so a two-word body can be known both by its two initials and
    by the first initial followed by the short word. Both are generated,
    because a funder's own domain may use either.
    """
    if len(tokens) < 2:
        return set()
    variants = {"".join(t[0] for t in tokens)}
    variants.add(tokens[0][0] + "".join(t for t in tokens[1:] if len(t) <= 3))
    variants.add("".join(t if len(t) <= 3 else t[0] for t in tokens))
    return {v for v in variants if len(v) > 1}


def _registrable_domain(url: str) -> str:
    """Host of a URL, lowercased, with any leading www. removed."""
    text = (url or "").strip().lower()
    for prefix in ("https://", "http://"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    host = text.split("/")[0].split("?")[0].split("#")[0].split("@")[-1]
    return host[4:] if host.startswith("www.") else host


def _domain_tokens(host: str) -> set[str]:
    return {
        t for t in re.split(r"[.\-_]", host)
        if t and t not in _GENERIC_DOMAIN_TOKENS and len(t) > 2
    }


def link_authority(url: str, name: str = "", managing_body: str = "") -> str:
    """Classify who a recommendation's link actually points at.

    Returns one of:

    * ``"funder"`` — an official public-sector or academic domain, or a domain
      that shares identifying words (or an acronym) with the programme or the
      body said to run it. Application portals often live on a domain that
      matches neither exactly, so token overlap is tested against the
      programme name too, not just the funder.
    * ``"third_party"`` — a publishing or social platform. Never a funder.
    * ``"unknown"`` — resolves somewhere unrelated: an aggregator, a
      consultancy, a trade blog. Not proof of a bad link, but not a citation a
      reader can lean on either.

    Deliberately generous about what counts as a funder. A false "unknown" on a
    legitimate programme costs a demotion; a false "funder" lets the defect
    through unmeasured, and the whole point is to be able to see it.
    """
    host = _registrable_domain(url)
    if not host or "." not in host:
        return "unknown"

    base = host[4:] if host.startswith("www.") else host
    if base in _PUBLISHING_PLATFORMS or any(
        base.endswith("." + p) or base == p for p in _PUBLISHING_PLATFORMS
    ):
        return "third_party"

    labels = host.split(".")
    if any(label in _PUBLIC_SECTOR_LABELS for label in labels):
        return "funder"
    if any(host == suffix.lstrip(".") or host.endswith(suffix)
           for suffix in _OFFICIAL_SUFFIXES):
        return "funder"

    host_tokens = _domain_tokens(host)
    if not host_tokens:
        return "unknown"

    # Every organisation named in the body counts, not just the first. Bodies
    # are routinely written as "Delivery Agency / Parent Department", and a
    # link to either one is a link to the funder.
    significant: list[str] = []
    aliases: set[str] = set()
    for part in re.split(r"[/,;&]| and ", managing_body or ""):
        part_significant, part_aliases = _body_identity(part)
        significant.extend(part_significant)
        aliases |= part_aliases
        aliases |= _acronym_variants(part_significant)

    claim_tokens = {t for t in normalise_name(name).split() if len(t) > 2}
    claim_tokens |= {t for t in significant if len(t) > 2}
    claim_tokens |= {a for a in aliases if len(a) > 2}
    acronym = _identifying_acronym(name, aliases)
    if acronym:
        claim_tokens.add(acronym.lower())

    if host_tokens & claim_tokens:
        return "funder"

    # A domain that is itself an acronym of the funder — "abcd.org" for
    # "A B C D Agency" — carries the funder's identity without sharing a word.
    joined = "".join(significant)
    if any(t == joined or t in aliases for t in host_tokens):
        return "funder"

    # Organisations routinely run their words together in a domain, so a
    # funder's own site can share no *token* with its name while obviously
    # belonging to it. Substring matching recovers those — but only on
    # distinctive words: half the climate funding world has "energy" or
    # "innovation" in its title, and matching on those would wave through a
    # retailer's blog on the strength of a coincidence.
    distinctive = {
        t for t in claim_tokens
        if len(t) >= 5 and t not in _COMMON_SECTOR_TOKENS
    }
    if any(d in host_token for host_token in host_tokens for d in distinctive):
        return "funder"

    return "unknown"


def unwrap(result: dict) -> dict:
    """Accept either a raw analysis or a stored eval file that wraps one.

    Stored eval results are `{"result": {...}, "findings": {...}}`; the app
    stores the analysis itself. Both are common inputs, so accept both rather
    than making every caller remember which it has.
    """
    if not isinstance(result, dict):
        return {}
    if "opportunities" not in result and isinstance(result.get("result"), dict):
        return result["result"]
    return result


def items_of(result: dict) -> tuple[list[dict], list[dict]]:
    """Return (main recommendations, strategic watchlist) as lists of dicts."""
    data = unwrap(result)
    main = [i for i in (data.get("opportunities") or []) if isinstance(i, dict)]
    watch = [i for i in (data.get("strategic_watchlist") or []) if isinstance(i, dict)]
    return main, watch


def _matches(item: dict, pattern: str) -> bool:
    """Ground-truth patterns match on name OR managing body, case-insensitively.

    Matching the body as well is what lets a pattern naming a funder catch a
    programme of theirs whose title doesn't repeat the funder's name.
    """
    haystack = f"{item.get('name', '')} {item.get('managing_body', '')}".lower()
    return (pattern or "").lower() in haystack


# ---------------------------------------------------------------------------
# Ground-truth measures (benchmark companies only)
# ---------------------------------------------------------------------------

def score_against_ground_truth(result: dict, case: dict) -> dict:
    """Recall and exclusion accuracy for one run of a known company.

    Two separate measures, because they fail for different reasons:

    * **recall** — of the programmes we know should surface, how many did?
      Misses point at discovery (Q6): the search didn't find it, or the
      longlist dropped it.
    * **exclusion accuracy** — of the programmes we know are wrong for this
      company, how many stayed out? A miss here is more serious: the pipeline
      recommended something confirmed ineligible (Q1), which is the failure a
      reader notices first.

    `must_not_appear_in_main` violations and `must_not_appear_anywhere`
    violations are counted separately: surfacing an ineligible programme on the
    watchlist as a partner route is a judgement call, recommending it directly
    is a defect.
    """
    main, watch = items_of(result)
    everywhere = main + watch

    recall_checks: list[dict] = []
    for pattern in case.get("must_appear_in_main", []) or []:
        hit = next((i for i in main if _matches(i, pattern)), None)
        recall_checks.append({
            "rule": "must_appear_in_main",
            "pattern": pattern,
            "passed": hit is not None,
            "matched": hit.get("name") if hit else None,
        })
    for pattern in case.get("must_appear_in_watchlist_or_main", []) or []:
        hit = next((i for i in everywhere if _matches(i, pattern)), None)
        recall_checks.append({
            "rule": "must_appear_in_watchlist_or_main",
            "pattern": pattern,
            "passed": hit is not None,
            "matched": hit.get("name") if hit else None,
        })

    exclusion_checks: list[dict] = []
    for pattern in case.get("must_not_appear_in_main", []) or []:
        hit = next((i for i in main if _matches(i, pattern)), None)
        exclusion_checks.append({
            "rule": "must_not_appear_in_main",
            "pattern": pattern,
            "passed": hit is None,
            "matched": hit.get("name") if hit else None,
        })
    for pattern in case.get("must_not_appear_anywhere", []) or []:
        hit = next((i for i in everywhere if _matches(i, pattern)), None)
        exclusion_checks.append({
            "rule": "must_not_appear_anywhere",
            "pattern": pattern,
            "passed": hit is None,
            "matched": hit.get("name") if hit else None,
        })

    def _rate(checks: list[dict]) -> float | None:
        return round(sum(c["passed"] for c in checks) / len(checks), 3) if checks else None

    violations = [c for c in exclusion_checks if not c["passed"]]
    return {
        "case_id": case.get("id"),
        "recall": _rate(recall_checks),
        "recall_checks": recall_checks,
        "misses": [c["pattern"] for c in recall_checks if not c["passed"]],
        "exclusion_accuracy": _rate(exclusion_checks),
        "exclusion_checks": exclusion_checks,
        "violations": [
            {"pattern": c["pattern"], "rule": c["rule"], "matched": c["matched"]}
            for c in violations
        ],
        "must_not_violations": len(violations),
    }


# ---------------------------------------------------------------------------
# Structural measures (work on any run, ground truth or not)
#
# Real user runs are analyses of companies nobody wrote ground truth for, so
# they cannot be scored for correctness. They can still be scored for defects
# that are visible without knowing the right answer — an unresolvable link, a
# main recommendation the company cannot legally lead, a shortlist that is a
# third of the advertised size. §9a warns that the two worst failure modes are
# invisible in usage data; these checks are how they become visible.
# ---------------------------------------------------------------------------

# Each entry: (check name, predicate returning True when the item FAILS,
#              the §9a property it harms).
_DEFENSIBILITY_CHECKS: list[tuple[str, Any, str]] = [
    ("no_application_process",
     lambda i: i.get("has_application_process") is False,
     "Q2"),
    ("not_directly_applicable",
     lambda i: (i.get("applicant_type_match") or "direct") not in ("direct", "unknown"),
     "Q1"),
    ("trl_mismatch",
     lambda i: i.get("trl_match") is False,
     "Q1"),
    ("geography_mismatch",
     lambda i: i.get("geography_match") is False,
     "Q1"),
    ("no_usable_link",
     lambda i: not str(i.get("application_link") or "").startswith("http"),
     "Q3"),
    ("broken_link",
     lambda i: i.get("link_status") == "broken",
     "Q3"),
    ("homepage_only_link",
     lambda i: i.get("link_type") == "funder_homepage",
     "Q3"),
    ("non_authoritative_link",
     lambda i: str(i.get("application_link") or "").startswith("http")
     and link_authority(i.get("application_link") or "",
                        i.get("name") or "",
                        i.get("managing_body") or "") != "funder",
     "Q3"),
    ("deadline_passed",
     lambda i: str(i.get("application_timing") or "").lower() == "passed",
     "Q2"),
]


def defensibility(result: dict) -> dict:
    """How many main recommendations survive every structural check.

    This is the closest thing to precision that exists for a company with no
    ground truth. It is deliberately conservative: it only counts a defect when
    the pipeline's *own* recorded fields say something is wrong (a TRL mismatch
    it flagged, a link it verified as broken). It cannot see a programme that
    is simply a poor fit, so treat it as a floor on the defect rate, never a
    ceiling on quality.
    """
    main, _ = items_of(result)
    failures: dict[str, int] = {name: 0 for name, _, _ in _DEFENSIBILITY_CHECKS}
    offenders: list[dict] = []

    for item in main:
        failed = [name for name, predicate, _ in _DEFENSIBILITY_CHECKS if predicate(item)]
        for name in failed:
            failures[name] += 1
        if failed:
            offenders.append({"name": item.get("name"), "failed": failed})

    clean = len(main) - len(offenders)
    return {
        "main_count": len(main),
        "defensible": clean,
        "structural_precision": round(clean / len(main), 3) if main else None,
        "failures": {k: v for k, v in failures.items() if v},
        "offenders": offenders,
        "properties": {name: prop for name, _, prop in _DEFENSIBILITY_CHECKS},
    }


def tier_spread(result: dict) -> dict:
    """Q4 — does the rubric discriminate, or does everything land in one tier?

    `concentration` is the share of main recommendations sitting in the single
    most common tier. At 1.0 the ranking is decoration.
    """
    main, _ = items_of(result)
    counts: dict[str, int] = {}
    for item in main:
        tier = str(item.get("priority_tier") or "unknown")
        counts[tier] = counts.get(tier, 0) + 1
    scores = [
        float(i["priority_score"]) for i in main
        if isinstance(i.get("priority_score"), (int, float))
    ]
    return {
        "counts": counts,
        "distinct_tiers": len(counts),
        "concentration": round(max(counts.values()) / len(main), 3) if main else None,
        "score_range": [round(min(scores), 2), round(max(scores), 2)] if scores else None,
        "score_spread": round(max(scores) - min(scores), 2) if len(scores) > 1 else None,
    }


def link_quality(result: dict) -> dict:
    """Q3 — can a reader verify any claim in one click?"""
    main, watch = items_of(result)
    everything = main + watch
    by_type: dict[str, int] = {}
    by_status: dict[str, int] = {}
    by_authority: dict[str, int] = {}
    no_url = 0
    for item in everything:
        authority = link_authority(
            item.get("application_link") or "",
            item.get("name") or "",
            item.get("managing_body") or "",
        )
        by_authority[authority] = by_authority.get(authority, 0) + 1
        by_type[str(item.get("link_type") or "unknown")] = \
            by_type.get(str(item.get("link_type") or "unknown"), 0) + 1
        by_status[str(item.get("link_status") or "unverified")] = \
            by_status.get(str(item.get("link_status") or "unverified"), 0) + 1
        if not str(item.get("application_link") or "").startswith("http"):
            no_url += 1
    total = len(everything)
    specific = by_type.get("application_portal", 0) + by_type.get("programme_page", 0)
    return {
        "total": total,
        "by_type": by_type,
        "by_status": by_status,
        "no_url": no_url,
        "specific_link_rate": round(specific / total, 3) if total else None,
        "broken_rate": round(by_status.get("broken", 0) / total, 3) if total else None,
        "by_authority": by_authority,
        "link_authority_rate": (round(by_authority.get("funder", 0) / total, 3)
                                if total else None),
        "third_party_links": by_authority.get("third_party", 0),
    }


def shape(result: dict, promised_main: int = 10) -> dict:
    """Q8 — did the run deliver the advertised volume and structure?

    `promised_main` defaults to the pipeline's own shortlist target; pass the
    live value from `analyzer.SHORTLIST_SIZE` where it is importable, so this
    never silently drifts from what the app actually promises.
    """
    main, watch = items_of(result)
    data = unwrap(result)
    return {
        "main": len(main),
        "watchlist": len(watch),
        "promised_main": promised_main,
        "shortfall": max(0, promised_main - len(main)),
        "watchlist_ratio": round(len(watch) / len(main), 2) if main else None,
        "has_summary": bool(str(data.get("executive_summary") or "").strip()),
        "has_recommendations": bool(data.get("strategic_recommendations")),
    }


def assess_run(result: dict, promised_main: int = 10) -> dict:
    """The full ground-truth-free quality vector for a single analysis."""
    return {
        "shape": shape(result, promised_main),
        "defensibility": defensibility(result),
        "tier_spread": tier_spread(result),
        "links": link_quality(result),
    }


# ---------------------------------------------------------------------------
# Convergence (Q5) — a diagnostic, never a target
# ---------------------------------------------------------------------------

def _jaccard(a: set[str], b: set[str]) -> float:
    union = a | b
    return round(len(a & b) / len(union), 3) if union else 0.0


def convergence(results: Sequence[dict], scope: str = "main") -> dict:
    """Agreement between repeat runs of the same company on the same commit.

    Measured at two levels because they diagnose different faults:

    * **exact** — identical programme names after normalisation.
    * **family** — the same programme however it was titled.

    The *gap* between them is itself the finding. A wide gap means the pipeline
    is returning one programme under several identities, which is a
    de-duplication defect. Low agreement persisting at family level means
    genuine retrieval churn — the search is near-random, per §9a.

    Read a low number as an alarm about retrieval and scoring. Never respond to
    it by caching or pinning results.
    """
    runs = [r for r in results if r]
    if len(runs) < 2:
        return {
            "runs": len(runs),
            "comparable": False,
            "note": "Convergence needs at least two runs of the same company.",
        }

    def _items(r: dict) -> list[dict]:
        main, watch = items_of(r)
        return main if scope == "main" else main + watch

    resolver = FamilyResolver([i for r in runs for i in _items(r)])

    def _sets(level: str) -> list[set[str]]:
        out = []
        for r in runs:
            items = [i for i in _items(r) if i.get("name")]
            if level == "exact":
                out.append({normalise_name(i.get("name", "")) for i in items})
            else:
                out.append({resolver.key(i) for i in items})
        return out

    summary: dict[str, Any] = {
        "runs": len(runs),
        "comparable": True,
        "scope": scope,
        "learned_aliases": len(resolver.aliases),
        "ambiguous_aliases": resolver.ambiguous,
    }
    for level in ("exact", "family"):
        sets = _sets(level)
        pairs = [_jaccard(a, b) for a, b in combinations(sets, 2)]
        common = set.intersection(*sets) if sets else set()
        union = set.union(*sets) if sets else set()
        summary[level] = {
            "mean_pairwise_jaccard": round(sum(pairs) / len(pairs), 3) if pairs else None,
            "min_pairwise_jaccard": round(min(pairs), 3) if pairs else None,
            "in_every_run": len(common),
            "distinct_across_runs": len(union),
            "core_share": round(len(common) / len(union), 3) if union else None,
            "sizes": [len(s) for s in sets],
        }
    exact_j = summary["exact"]["mean_pairwise_jaccard"]
    family_j = summary["family"]["mean_pairwise_jaccard"]
    summary["identity_gap"] = (
        round(family_j - exact_j, 3)
        if exact_j is not None and family_j is not None else None
    )
    return summary


def group_repeat_runs(runs: Iterable[dict], key: str = "company_url") -> dict[str, list[dict]]:
    """Group stored analyses by company so convergence has something to compare.

    Only groups of two or more are returned — a single run tells you nothing
    about agreement.
    """
    groups: dict[str, list[dict]] = {}
    for run in runs:
        k = str(run.get(key) or "").strip().lower().rstrip("/")
        if not k:
            continue
        groups.setdefault(k, []).append(run)
    return {k: v for k, v in groups.items() if len(v) > 1}


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _mean(values: Sequence[float | None]) -> float | None:
    present = [v for v in values if isinstance(v, (int, float))]
    return round(sum(present) / len(present), 3) if present else None


def summarise(assessments: Sequence[dict]) -> dict:
    """Roll per-run assessments into the figures a review cycle trends.

    Every figure carries its own sample size. A cycle that reports a number
    without saying how many runs produced it invites false confidence, and §9a
    is explicit that measures are read as trends, not snapshots.
    """
    if not assessments:
        return {"runs": 0}
    return {
        "runs": len(assessments),
        "structural_precision": _mean(
            [a["defensibility"]["structural_precision"] for a in assessments]
        ),
        "main_count": _mean([a["shape"]["main"] for a in assessments]),
        "main_count_range": [
            min(a["shape"]["main"] for a in assessments),
            max(a["shape"]["main"] for a in assessments),
        ],
        "watchlist_count": _mean([a["shape"]["watchlist"] for a in assessments]),
        "tier_concentration": _mean([a["tier_spread"]["concentration"] for a in assessments]),
        "distinct_tiers": _mean([a["tier_spread"]["distinct_tiers"] for a in assessments]),
        "specific_link_rate": _mean([a["links"]["specific_link_rate"] for a in assessments]),
        "broken_rate": _mean([a["links"]["broken_rate"] for a in assessments]),
        "link_authority_rate": _mean([a["links"]["link_authority_rate"] for a in assessments]),
        "third_party_links": sum(a["links"]["third_party_links"] for a in assessments),
        "defect_totals": _merge_counts(
            [a["defensibility"]["failures"] for a in assessments]
        ),
    }


def _merge_counts(dicts: Sequence[dict]) -> dict:
    out: dict[str, int] = {}
    for d in dicts:
        for k, v in (d or {}).items():
            out[k] = out.get(k, 0) + v
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))
