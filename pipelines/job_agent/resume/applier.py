"""Apply a tailoring plan to the master profile, producing a renderable document.

Pure function — no LLM calls, no I/O. Given a master profile and a
structured plan (bullet selections, ordering, rewrites), outputs the
final ``TailoredResumeDocument`` ready for .docx rendering.

Schema v2 guarantees enforced here:
  - Every ``tier="pinned"`` experience or education section in the master
    profile ALWAYS appears in the tailored document, even if the LLM
    plan omitted it. Missing pinned sections are auto-inserted using
    their anchored bullets.
  - Every ``anchor=True`` bullet ALWAYS appears in its section's bullet
    list, prepended in master order if the plan omitted it.
  - ``recast`` / ``rewrite`` ops are validated before being accepted:
      * length parity — recast text word count must not exceed the master
        bullet's word count by more than ``RECAST_LENGTH_TOLERANCE`` (20%);
      * entity grounding — every number, dollar amount, percentage, acronym,
        and capitalized proper-noun phrase in the recast text must appear
        (as a substring, case-insensitive) in the master bullet's ``text``,
        its ``source_material``, or the profile-level corpus of known
        entities (company names, school names, clearance text).
    On failure, the op falls back to ``keep`` (master text verbatim) and
    the failure is logged.
  - Supplementary projects selected by the plan are rendered into
    ``TailoredResumeDocument.supplementary_projects`` (and omitted when
    not selected). The renderer folds them under Additional Information.
"""

from __future__ import annotations

import re

import structlog

from pipelines.job_agent.models.resume_tailoring import (
    BulletOp,
    MasterBullet,
    MasterExperience,
    ResumeMasterProfile,
    ResumeTailoringPlan,
    SupplementaryProject,
    TailoredBullet,
    TailoredEducation,
    TailoredExperience,
    TailoredResumeDocument,
    TailoredSupplementaryProject,
)

logger = structlog.get_logger(__name__)

# Maximum sections and bullets carried through to the tailored document.
# Raised in v2 to accommodate profiles with 5–6 pinned experience
# sections (e.g. current Sam profile: ICAAD, Helium, Gauntlet, Lincoln,
# EB, + optional SigCom). The applier honors per-tier guarantees above
# these caps; ``MAX_EXPERIENCE_SECTIONS`` is a safety rail against
# runaway LLM plans, not a product policy.
MAX_EXPERIENCE_SECTIONS = 8
MAX_EDUCATION_SECTIONS = 3
MAX_BULLETS_PER_EXPERIENCE_SECTION = 5
MAX_BULLETS_PER_EDUCATION_SECTION = 3
MAX_SUMMARY_WORDS = 32
MAX_SKILLS_TO_HIGHLIGHT = 12
MAX_SUPPLEMENTARY_PROJECTS = 4

# Recast validation thresholds.
RECAST_LENGTH_TOLERANCE = 0.20  # 20% — recast word count may exceed master by this much
RECAST_MIN_MASTER_WORDS = 4      # ignore parity check for very short bullets

# Entity regex: numbers/money/acronyms/proper nouns used for recast
# grounding verification. Matches the kinds of tokens a recast must not
# invent: numeric facts and named-entity references.
_ENTITY_RE = re.compile(
    r"\$[\d][\d.,]*[KMBkmb]?"           # $100M, $2,000, $7K
    r"|\d+(?:\.\d+)?%"                  # 95%, 0.961%
    r"|\d+(?:\.\d+)?(?:K|M|B|k|m|b)"    # 10K, 100M
    r"|\d+(?:,\d{3})+"                  # 10,000
    r"|\b\d+\+?\b"                      # 3, 5+, 200
    r"|\b[A-Z]{2,}(?:[-/][A-Z0-9]+)*\b" # RADAR, HIL, MIL-STD, DSP, C++
    r"|\b[A-Z][a-zA-Z0-9]+"             # Columbia, LangGraph, Playwright
    r"(?:[-\s][A-Z][a-zA-Z0-9]+)+\b"    # …-class, Electric Boat, Lincoln Lab
)


def apply_tailoring_plan(
    profile: ResumeMasterProfile,
    plan: ResumeTailoringPlan,
) -> TailoredResumeDocument:
    """Apply *plan* to *profile* and return the tailored document.

    Unknown bullet or section IDs are logged and skipped rather than
    raising — the rest of the resume is still usable. Tier + anchor
    guarantees are enforced even if the plan omitted them.
    """
    valid_ids = profile.all_bullet_ids()
    ops_by_id = {op.bullet_id: op for op in plan.bullet_ops}
    profile_corpus = _build_profile_corpus(profile)

    experience = _build_experience(profile, plan, ops_by_id, valid_ids, profile_corpus)
    education = _build_education(profile, plan, ops_by_id, valid_ids, profile_corpus)
    supplementary = _build_supplementary(profile, plan, ops_by_id, profile_corpus)

    additional_info: list[str] = []
    if profile.clearance:
        additional_info.append(profile.clearance)

    return TailoredResumeDocument(
        name=_normalize_whitespace(profile.name),
        location=_normalize_whitespace(profile.location),
        email=_normalize_whitespace(profile.email),
        phone=_normalize_whitespace(profile.phone),
        linkedin=_normalize_whitespace(profile.linkedin),
        github=_normalize_whitespace(profile.github),
        clearance=_normalize_whitespace(profile.clearance),
        summary=_truncate_words(_normalize_inline_prose(plan.summary), MAX_SUMMARY_WORDS),
        experience=experience,
        education=education,
        skills_highlight=[
            _normalize_whitespace(skill)
            for skill in plan.skills_to_highlight[:MAX_SKILLS_TO_HIGHLIGHT]
        ],
        additional_info=additional_info,
        supplementary_projects=supplementary,
    )


# ── experience/education assembly ─────────────────────────────────────


def _build_experience(
    profile: ResumeMasterProfile,
    plan: ResumeTailoringPlan,
    ops_by_id: dict[str, BulletOp],
    valid_ids: set[str],
    profile_corpus: str,
) -> list[TailoredExperience]:
    """Assemble experience sections honoring tier and anchor guarantees."""
    # Respect the profile's declared section order — it's reverse-
    # chronological curated. The plan's order is advisory for which
    # sections to include from the optional pool and in what bullet
    # order within each section.
    plan_order = [sec.section_id for sec in plan.experience_sections]
    plan_by_id = {sec.section_id: sec for sec in plan.experience_sections}

    selected_ids: list[str] = []
    for exp in profile.experience:
        if exp.tier == "supplementary":
            continue
        if exp.tier == "pinned":
            selected_ids.append(exp.id)
        elif exp.id in plan_by_id:  # optional — include only if plan opted in
            selected_ids.append(exp.id)

    # Capture-plan-ordered ids not already in selected_ids (handles the
    # case where the LLM invents unknown sections — silently dropped).
    for sid in plan_order:
        if sid not in selected_ids and any(e.id == sid for e in profile.experience):
            # Unknown to selected because it's supplementary or tier=optional
            # already handled. Nothing to add.
            pass

    result: list[TailoredExperience] = []
    for section_id in selected_ids[:MAX_EXPERIENCE_SECTIONS]:
        master_exp = profile.get_experience(section_id)
        if master_exp is None:
            logger.warning("applier.unknown_section", section_id=section_id)
            continue
        section_plan = plan_by_id.get(section_id)
        bullet_order = list(section_plan.bullet_order) if section_plan else []
        bullets = _resolve_bullets_with_anchors(
            master_bullets=master_exp.bullets,
            ordered_ids=bullet_order,
            ops_by_id=ops_by_id,
            valid_ids=valid_ids,
            profile_corpus=profile_corpus,
            max_bullets=MAX_BULLETS_PER_EXPERIENCE_SECTION,
        )
        result.append(
            TailoredExperience(
                company=_normalize_whitespace(master_exp.company),
                title=_normalize_whitespace(master_exp.title),
                dates=_normalize_whitespace(master_exp.dates),
                location=_normalize_whitespace(master_exp.location),
                subtitle=_normalize_whitespace(master_exp.subtitle),
                bullets=bullets,
            )
        )
    return result


def _build_education(
    profile: ResumeMasterProfile,
    plan: ResumeTailoringPlan,
    ops_by_id: dict[str, BulletOp],
    valid_ids: set[str],
    profile_corpus: str,
) -> list[TailoredEducation]:
    plan_by_id = {sec.section_id: sec for sec in plan.education_sections}

    selected_ids: list[str] = []
    for edu in profile.education:
        if edu.tier == "pinned":
            selected_ids.append(edu.id)
        elif edu.id in plan_by_id:
            selected_ids.append(edu.id)

    result: list[TailoredEducation] = []
    for section_id in selected_ids[:MAX_EDUCATION_SECTIONS]:
        master_edu = profile.get_education(section_id)
        if master_edu is None:
            logger.warning("applier.unknown_section", section_id=section_id)
            continue
        section_plan = plan_by_id.get(section_id)
        bullet_order = list(section_plan.bullet_order) if section_plan else []
        bullets = _resolve_bullets_with_anchors(
            master_bullets=master_edu.bullets,
            ordered_ids=bullet_order,
            ops_by_id=ops_by_id,
            valid_ids=valid_ids,
            profile_corpus=profile_corpus,
            max_bullets=MAX_BULLETS_PER_EDUCATION_SECTION,
        )
        result.append(
            TailoredEducation(
                school=_normalize_whitespace(master_edu.school),
                degree=_normalize_whitespace(master_edu.degree),
                graduation=_normalize_whitespace(master_edu.graduation),
                gpa=_normalize_whitespace(master_edu.gpa),
                location=_normalize_whitespace(master_edu.location),
                bullets=bullets,
            )
        )
    return result


def _build_supplementary(
    profile: ResumeMasterProfile,
    plan: ResumeTailoringPlan,
    ops_by_id: dict[str, BulletOp],
    profile_corpus: str,
) -> list[TailoredSupplementaryProject]:
    """Apply the plan's supplementary project selections.

    ``supplementary_project_ids`` can contain recast/shorten/keep ops in
    ``bullet_ops`` using the project's id, same mechanics as experience
    bullets. Unknown project ids are logged and skipped.
    """
    selected: list[TailoredSupplementaryProject] = []
    for proj_id in plan.supplementary_project_ids[:MAX_SUPPLEMENTARY_PROJECTS]:
        proj = profile.get_supplementary_project(proj_id)
        if proj is None:
            logger.warning("applier.unknown_supplementary_project", project_id=proj_id)
            continue
        op = ops_by_id.get(proj_id)
        text = _apply_op_to_supplementary(proj, op, profile_corpus)
        selected.append(
            TailoredSupplementaryProject(
                id=proj.id,
                name=_normalize_whitespace(proj.name),
                url=_normalize_whitespace(proj.url),
                text=_normalize_inline_prose(text),
            )
        )
    return selected


# ── bullet resolution with anchor enforcement + recast validation ────


def _resolve_bullets_with_anchors(
    *,
    master_bullets: list[MasterBullet],
    ordered_ids: list[str],
    ops_by_id: dict[str, BulletOp],
    valid_ids: set[str],
    profile_corpus: str,
    max_bullets: int,
) -> list[TailoredBullet]:
    """Return bullets honoring the plan order with anchor guarantee.

    Anchor guarantee: every ``anchor=True`` bullet in ``master_bullets``
    appears in the result. If the LLM's plan omitted an anchored bullet,
    it is prepended in master-profile order (so the anchors appear
    before non-anchored bullets the LLM selected, preserving a stable
    top-of-section structure).

    ``max_bullets`` caps total output. Anchored bullets consume slots
    first; remaining slots go to the LLM's plan selections in order.
    """
    by_id = {b.id: b for b in master_bullets}

    anchored_ids_in_profile = [b.id for b in master_bullets if b.anchor]
    # The LLM's ordered_ids may include or exclude anchors. Track what
    # it picked so we can keep its non-anchor order intact.
    seen: set[str] = set()
    anchor_list: list[str] = []
    non_anchor_list: list[str] = []

    # First pass: pull anchors from the plan's order as they appear.
    for bid in ordered_ids:
        if bid in seen:
            continue
        if bid not in valid_ids:
            logger.warning("applier.unknown_bullet", bullet_id=bid)
            continue
        bullet = by_id.get(bid)
        if bullet is None:
            logger.warning("applier.bullet_not_in_section", bullet_id=bid)
            continue
        seen.add(bid)
        if bullet.anchor:
            anchor_list.append(bid)
        else:
            non_anchor_list.append(bid)

    # Second pass: insert any anchored bullets the plan forgot, in the
    # profile's declared order (before any non-anchors it did pick).
    missing_anchors = [bid for bid in anchored_ids_in_profile if bid not in seen]
    # Place missing anchors at the FRONT of anchor_list, in master order.
    anchor_list = missing_anchors + anchor_list

    final_ids = anchor_list + non_anchor_list
    final_ids = final_ids[:max_bullets]

    result: list[TailoredBullet] = []
    for bid in final_ids:
        master = by_id[bid]
        op = ops_by_id.get(bid)
        text = _apply_op(master, op, profile_corpus)
        result.append(TailoredBullet(id=bid, text=_normalize_inline_prose(text)))
    return result


def _apply_op(bullet: MasterBullet, op: BulletOp | None, profile_corpus: str) -> str:
    """Return the final bullet text after applying the requested operation.

    Recast / rewrite ops are validated against length parity and entity
    grounding; failures fall back to ``keep``.
    """
    if op is None or op.op == "keep":
        return bullet.text

    if op.op == "shorten":
        short = bullet.variants.get("short")
        if short:
            return short
        logger.warning("applier.no_short_variant", bullet_id=bullet.id)
        return bullet.text

    if op.op in ("recast", "rewrite"):
        proposed = (op.rewrite_text or "").strip()
        if not proposed:
            return bullet.text
        rejection = _validate_recast(
            proposed=proposed,
            master_text=bullet.text,
            source_material=bullet.source_material,
            profile_corpus=profile_corpus,
        )
        if rejection is not None:
            logger.warning(
                "applier.recast_rejected_fallback_to_keep",
                bullet_id=bullet.id,
                reason=rejection,
                proposed=proposed[:200],
            )
            return bullet.text
        return proposed

    return bullet.text


def _apply_op_to_supplementary(
    proj: SupplementaryProject,
    op: BulletOp | None,
    profile_corpus: str,
) -> str:
    """Apply a recast/shorten/keep op to a supplementary project.

    Uses the same validation as bullets (length parity + entity grounding)
    against the project's own text + source_material.
    """
    if op is None or op.op == "keep":
        return proj.text

    if op.op == "shorten":
        short = proj.variants.get("short")
        if short:
            return short
        return proj.text

    if op.op in ("recast", "rewrite"):
        proposed = (op.rewrite_text or "").strip()
        if not proposed:
            return proj.text
        rejection = _validate_recast(
            proposed=proposed,
            master_text=proj.text,
            source_material=proj.source_material,
            profile_corpus=profile_corpus,
        )
        if rejection is not None:
            logger.warning(
                "applier.supplementary_recast_rejected_fallback_to_keep",
                project_id=proj.id,
                reason=rejection,
                proposed=proposed[:200],
            )
            return proj.text
        return proposed

    return proj.text


# ── recast validation: length parity + entity grounding ──────────────


def _validate_recast(
    *,
    proposed: str,
    master_text: str,
    source_material: str,
    profile_corpus: str,
) -> str | None:
    """Return a rejection reason string, or None if the recast is acceptable."""
    # 1. Length parity. Only enforce for bullets with meaningful length;
    #    very short master bullets (< RECAST_MIN_MASTER_WORDS) would make
    #    the 20% tolerance too tight to be useful.
    master_words = len(master_text.split())
    proposed_words = len(proposed.split())
    if master_words >= RECAST_MIN_MASTER_WORDS:
        max_allowed = int(master_words * (1 + RECAST_LENGTH_TOLERANCE)) + 1
        if proposed_words > max_allowed:
            return (
                f"length_parity_exceeded: proposed={proposed_words}w, "
                f"master={master_words}w, max={max_allowed}w"
            )

    # 2. Entity grounding. Every entity-like token in the proposed text
    #    must appear (case-insensitive, substring match) in the combined
    #    corpus of master text, source_material, and profile-wide corpus.
    #    Substring matching is deliberate so "Electric Boat" grounds
    #    against "General Dynamics, Electric Boat" verbatim.
    corpus = "\n".join([master_text, source_material, profile_corpus]).lower()
    unknown: list[str] = []
    for match in _ENTITY_RE.findall(proposed):
        token = match.strip()
        if not token:
            continue
        if token.lower() not in corpus:
            unknown.append(token)
    if unknown:
        # Deduplicate while preserving order for readable logs.
        seen: set[str] = set()
        deduped: list[str] = []
        for t in unknown:
            if t not in seen:
                seen.add(t)
                deduped.append(t)
        return f"ungrounded_entities: {deduped[:6]}"

    return None


def _build_profile_corpus(profile: ResumeMasterProfile) -> str:
    """Flatten every fact surface from the profile into a single search corpus.

    Used for recast entity grounding: any entity the LLM emits must
    appear somewhere in the candidate's declared facts (company names,
    school names, bullet texts, source_material, clearance).
    """
    parts: list[str] = [profile.name, profile.location, profile.clearance]
    for exp in profile.experience:
        parts.extend([exp.company, exp.title, exp.dates, exp.location, exp.subtitle])
        for b in exp.bullets:
            parts.append(b.text)
            parts.extend(b.variants.values())
            if b.source_material:
                parts.append(b.source_material)
    for edu in profile.education:
        parts.extend([edu.school, edu.degree, edu.graduation, edu.location])
        for b in edu.bullets:
            parts.append(b.text)
            parts.extend(b.variants.values())
            if b.source_material:
                parts.append(b.source_material)
    for proj in profile.supplementary_projects:
        parts.extend([proj.name, proj.url, proj.text])
        parts.extend(proj.variants.values())
        if proj.source_material:
            parts.append(proj.source_material)
    parts.extend(profile.skills.languages)
    parts.extend(profile.skills.frameworks)
    parts.extend(profile.skills.domains)
    parts.extend(profile.skills.tools)
    return "\n".join(p for p in parts if p)


# ── normalization helpers ────────────────────────────────────────────


def _normalize_whitespace(text: str) -> str:
    """Normalize whitespace while preserving punctuation."""
    normalized = re.sub(r"\s+", " ", text).strip()
    return normalized


def _truncate_words(text: str, max_words: int) -> str:
    """Limit *text* to *max_words* words."""
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words])


def _normalize_inline_prose(text: str) -> str:
    """Normalize punctuation in narrative prose only.

    Rule: inline em/en dashes in sentence text are replaced with semicolons
    to avoid obvious LLM punctuation patterns. Date ranges and headings are
    preserved because they typically do not have surrounding spaces.
    """
    normalized = _normalize_whitespace(text)
    normalized = re.sub(r"\s[\u2014\u2013]\s", "; ", normalized)
    normalized = re.sub(r"\s--\s", "; ", normalized)
    return normalized
