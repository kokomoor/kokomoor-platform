"""Ranking node — pick the highest-fit listings for expensive tailoring.

Ranking is deliberately decoupled from LLM calls. We score each
listing using the structured ``JobAnalysisResult`` that was already
computed in the upstream job-analysis node, cross-referenced against
the candidate's master profile (skills + bullet text). The result is
a cheap, deterministic fit score in ``[0.0, 1.0]``.

Scoring (weights tuned so any one signal can still reach ~0.5):

    fit = 0.55 * basic_coverage
        + 0.25 * preferred_coverage
        + 0.20 * keyword_coverage

where ``*_coverage`` is the fraction of the analysis's requirement
phrases whose substantive tokens appear anywhere in the candidate
corpus. A requirement with no evaluable tokens is skipped so an
empty or boilerplate JD cannot artificially drag the score down.

Selection rules:

1. Listings scoring below ``ranking_min_fit_score`` are marked
   SKIPPED — they will not consume Sonnet tailoring budget.
2. Remaining listings are sorted by fit score descending; posted
   salary is used only as a tiebreaker. Salary data is known to be
   unreliable upstream, so it is never the primary signal.
3. ``tailoring_max_listings`` still caps the final selection.

This replaces the previous salary-first ranking that let listings
with fabricated/duplicated compensation figures win.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from core.config import get_settings
from pipelines.job_agent.models import ApplicationStatus
from pipelines.job_agent.resume.profile import load_master_profile
from pipelines.job_agent.state import JobAgentState, PipelinePhase

if TYPE_CHECKING:
    from pipelines.job_agent.models import JobListing
    from pipelines.job_agent.models.resume_tailoring import (
        JobAnalysisResult,
        ResumeMasterProfile,
    )

logger = structlog.get_logger(__name__)


_TOKEN_RE = re.compile(r"[a-zA-Z0-9+/#.]+")
_STOPWORDS = frozenset(
    {
        "with",
        "from",
        "this",
        "that",
        "have",
        "will",
        "your",
        "their",
        "they",
        "about",
        "into",
        "using",
        "years",
        "year",
        "experience",
        "ability",
        "able",
        "strong",
        "good",
        "great",
        "working",
        "work",
        "team",
        "role",
        "skills",
        "skill",
        "knowledge",
        "including",
        "include",
        "must",
        "should",
        "plus",
        "preferred",
        "required",
        "requirements",
        "responsibilities",
        "environment",
        "ideal",
        "demonstrated",
        "understanding",
        "proficiency",
        "familiarity",
        "background",
        "degree",
    }
)


@dataclass(frozen=True)
class _ListingScore:
    listing: JobListing
    fit: float
    basic_coverage: float
    preferred_coverage: float
    keyword_coverage: float


def _candidate_corpus(profile: ResumeMasterProfile) -> frozenset[str]:
    """Flatten the profile into a lower-cased token set for matching."""
    tokens: set[str] = set()
    tokens.update(tok.lower() for tok in profile.skills.all_skills())
    for exp in profile.experience:
        tokens.update(tok.lower() for tok in (exp.company, exp.title, exp.subtitle) if tok)
        for bullet in exp.bullets:
            tokens.update(_extract_tokens(bullet.text))
            tokens.update(tag.lower() for tag in bullet.tags)
    for edu in profile.education:
        tokens.update(tok.lower() for tok in (edu.school, edu.degree) if tok)
        for bullet in edu.bullets:
            tokens.update(_extract_tokens(bullet.text))
            tokens.update(tag.lower() for tag in bullet.tags)
    # Skill strings may be multi-word ("Machine Learning"); expand into
    # individual substantive tokens so a JD requirement of "machine"
    # can still match.
    expanded: set[str] = set()
    for tok in list(tokens):
        expanded.update(_extract_tokens(tok))
    return frozenset(tokens | expanded)


def _extract_tokens(text: str) -> set[str]:
    """Return substantive lower-cased tokens from a free-text field."""
    out: set[str] = set()
    for match in _TOKEN_RE.findall(text or ""):
        lower = match.lower().strip(".,;:")
        if not lower or lower in _STOPWORDS:
            continue
        if len(lower) < 3:
            continue
        out.add(lower)
    return out


def _phrase_tokens(phrase: str) -> set[str]:
    """Substantive tokens for one requirement phrase.

    Empty/boilerplate phrases return an empty set so we can skip them
    rather than letting them count against coverage.
    """
    return _extract_tokens(phrase)


def _coverage(items: list[str], corpus: frozenset[str]) -> float:
    """Fraction of requirement items whose tokens appear in ``corpus``.

    A requirement counts as matched if any of its substantive tokens is
    present in the candidate corpus. Requirements with zero evaluable
    tokens are skipped entirely (neither numerator nor denominator).
    """
    evaluable = 0
    matched = 0
    for item in items:
        tokens = _phrase_tokens(item)
        if not tokens:
            continue
        evaluable += 1
        if tokens & corpus:
            matched += 1
    if evaluable == 0:
        return 0.0
    return matched / evaluable


def _score_listing(
    listing: JobListing,
    analysis: JobAnalysisResult,
    corpus: frozenset[str],
) -> _ListingScore:
    basic_cov = _coverage(analysis.basic_qualifications, corpus)
    preferred_cov = _coverage(analysis.preferred_qualifications, corpus)
    keyword_cov = _coverage(analysis.must_hit_keywords, corpus)
    fit = (0.55 * basic_cov) + (0.25 * preferred_cov) + (0.20 * keyword_cov)
    return _ListingScore(
        listing=listing,
        fit=round(fit, 4),
        basic_coverage=round(basic_cov, 4),
        preferred_coverage=round(preferred_cov, 4),
        keyword_coverage=round(keyword_cov, 4),
    )


def _tiebreak_key(scored: _ListingScore) -> tuple[float, int, int]:
    """Sort descending: fit, then salary_max, then salary_min."""
    return (
        -scored.fit,
        -(scored.listing.salary_max or 0),
        -(scored.listing.salary_min or 0),
    )


async def ranking_node(state: JobAgentState) -> JobAgentState:
    """Score and select listings for expensive tailoring.

    Uses the already-computed ``state.job_analyses`` plus the master
    profile to compute a deterministic fit score per listing, drops
    listings that fall below the configured floor, and returns the
    top-N by fit (salary is a tiebreaker only).
    """
    state.phase = PipelinePhase.RANKING
    settings = get_settings()
    cap = settings.tailoring_max_listings
    floor = settings.ranking_min_fit_score

    if not state.qualified_listings:
        logger.info("ranking.empty")
        return state

    profile = load_master_profile(Path(settings.resume_master_profile_path))
    corpus = _candidate_corpus(profile)

    scored: list[_ListingScore] = []
    missing_analysis: list[str] = []
    for listing in state.qualified_listings:
        analysis = state.job_analyses.get(listing.dedup_key)
        if analysis is None:
            missing_analysis.append(listing.dedup_key)
            scored.append(
                _ListingScore(
                    listing=listing,
                    fit=0.0,
                    basic_coverage=0.0,
                    preferred_coverage=0.0,
                    keyword_coverage=0.0,
                )
            )
            continue
        scored.append(_score_listing(listing, analysis, corpus))

    scored.sort(key=_tiebreak_key)

    above_floor = [s for s in scored if s.fit >= floor]
    below_floor = [s for s in scored if s.fit < floor]

    if cap:
        selected = above_floor[:cap]
        skipped_scored = above_floor[cap:] + below_floor
    else:
        selected = above_floor
        skipped_scored = below_floor

    for entry in skipped_scored:
        entry.listing.status = ApplicationStatus.SKIPPED

    state.qualified_listings = [entry.listing for entry in selected]

    logger.info(
        "ranking.complete",
        cap=cap,
        floor=floor,
        input=len(scored),
        selected=len(selected),
        skipped_below_floor=len(below_floor),
        skipped_over_cap=len(above_floor) - len(selected) if cap else 0,
        missing_analysis=len(missing_analysis),
        top=[
            {
                "company": s.listing.company,
                "title": s.listing.title,
                "fit": s.fit,
                "basic": s.basic_coverage,
                "preferred": s.preferred_coverage,
                "keywords": s.keyword_coverage,
            }
            for s in selected[:5]
        ],
    )
    return state
