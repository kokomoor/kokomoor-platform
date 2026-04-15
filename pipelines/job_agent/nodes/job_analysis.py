"""Job analysis node — extract structured understanding from full job descriptions.

Dedicated LangGraph node that sits between extraction and tailoring.
Reads the full ``JobListing.description`` (no truncation), produces a
``JobAnalysisResult`` per listing, and stores results on
``state.job_analyses`` keyed by ``dedup_key``.

The tailoring node then consumes these pre-computed analyses instead of
running its own embedded LLM pass.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from core.config import Settings, get_settings
from core.workflows import StructuredAnalysisEngine, StructuredAnalysisSpec
from pipelines.job_agent.models import ApplicationStatus
from pipelines.job_agent.models.resume_tailoring import JobAnalysisResult
from pipelines.job_agent.state import JobAgentState, PipelinePhase

if TYPE_CHECKING:
    from core.llm.protocol import LLMClient
    from pipelines.job_agent.models import JobListing

logger = structlog.get_logger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_ENGINE: StructuredAnalysisEngine[JobAgentState, JobListing, JobAnalysisResult, _Runtime] = (
    StructuredAnalysisEngine()
)


@dataclass(frozen=True)
class _Runtime:
    prompt_template: str
    settings: Settings


def _build_spec() -> StructuredAnalysisSpec[JobAgentState, JobListing, JobAnalysisResult, _Runtime]:
    return StructuredAnalysisSpec(
        name="job_analysis",
        response_model=JobAnalysisResult,
        prepare=_prepare_runtime,
        get_items=lambda state: state.qualified_listings,
        should_skip=lambda state: state.dry_run or not state.qualified_listings,
        on_skip=_on_skip,
        build_prompt=_build_prompt,
        get_run_id=lambda state: state.run_id,
        get_model=lambda _state, runtime: runtime.settings.job_analysis_model or None,
        get_max_tokens=lambda _state, runtime: runtime.settings.job_analysis_max_tokens,
        get_cache_key=lambda _state, item, _runtime: _analysis_cache_key(item),
        get_cached_result=_get_cached_result,
        cache_result=_cache_result,
        on_item_start=_on_item_start,
        on_item_result=_on_item_result,
        on_item_error=_on_item_error,
        on_complete=_on_complete,
        # The instructions block of the analysis prompt is identical
        # across listings; mark it as the cacheable system prefix so the
        # JobAnalysisResult schema (folded in by structured_complete when
        # cache_system=True) plus these instructions form a stable cache
        # prefix. Without this every Haiku call paid full prefill cost.
        build_cached_system=lambda _state, runtime: _ANALYSIS_SYSTEM,
        concurrency=get_settings().llm_max_concurrency,
    )


_ANALYSIS_SYSTEM = """You analyse job listings and produce a structured JobAnalysisResult.

Your job is to read an entire job description (role pitch, responsibilities,
qualifications, preferred qualifications, company blurb) and distil it into a
compact, structured understanding that downstream resume- and cover-letter-
tailoring nodes can consume without re-reading the raw JD. Your output drives
both the ranker (via keywords, basic_qualifications, preferred_qualifications)
and the tailoring prompts (via themes, positioning angles, domain tags), so
the fidelity of every field matters. Treat the JD as the ground truth: never
invent requirements, themes, or domains that are not supported by the text.

Read the full description — including any "what we're looking for", "ideal
candidate", "about the team", and "responsibilities" sections. Employers often
bury the must-haves in prose rather than in a bulleted list; your job is to
surface them. Many listings also carry a long legal disclaimer, EEO statement,
or benefits blurb at the end that contains no role content; do not let that
content pollute the analysis.

Follow every rule below.

## themes (3-5 items)

Identify the 3-5 themes the employer cares about most. A theme is a phrase
(not a keyword) that captures a capability or outcome the role is ultimately
evaluated on. Examples:
- "autonomous systems safety case ownership"
- "cross-functional technical program leadership"
- "data platform migration and governance"
- "customer-facing executive storytelling"
- "continuous improvement culture transformation"

Prefer specificity over generality. "Leadership" is too broad; "leading a
distributed program across hardware, firmware, and cloud teams" is right.
Themes should be distinct — if two themes share more than 60% of their
substantive words, collapse them. If the JD has fewer than three clearly
distinct themes, emit only what is supported rather than padding.

A theme must be defensible from the JD text. If you cannot point to a
specific paragraph that motivates the theme, drop it. Themes are ordered
by importance: the first theme must be the single most load-bearing
capability the role is evaluated on; subsequent themes in descending
priority.

## seniority (one of: junior, mid, mid-senior, senior, lead, staff, director)

Map from concrete signals in the JD, not from vibes.

- junior: 0-2 YoE asked for; title contains "associate", "entry", "I" suffix,
  or "new grad". Scope is "contribute to features under supervision". Base
  salary bands typically below $120K in US tech.
- mid: 2-5 YoE; title has no seniority modifier; scope is "own features
  end to end". Receives code review but drives implementation independently.
- mid-senior: 3-6 YoE; title is "Senior II" or equivalent; scope is
  "own a workstream". May mentor one or two juniors but does not set
  technical direction for the team.
- senior: 5-8 YoE; title contains "Senior", "Sr."; scope is "own a product
  area or critical service". Sets technical direction within their scope
  and leads design reviews.
- lead: 7-10 YoE; title contains "Lead" or "Tech Lead"; scope includes
  hands-on plus mentorship, but does not formally manage people. Owns
  quarterly planning for a small team.
- staff: 8-12 YoE; title contains "Staff" or "Principal"; scope is
  cross-team technical strategy or major architectural ownership without
  direct reports. Influences roadmaps across multiple teams.
- director: 10+ YoE; title contains "Director", "Head of", "VP"; scope is
  multi-team budget, headcount, and strategic accountability. Hires and
  fires; owns org-wide OKRs.

Seniority disambiguation rules:
1. If title says "Senior" but responsibilities read like Staff (cross-team
   strategy, architectural ownership without reports), choose `staff` and
   call out the mismatch in positioning_angles.
2. If the JD asks for both "5+ years" and "team lead experience", use
   `lead` even if the title says "Senior Engineer".
3. "Principal" in some companies (especially consulting) means director;
   in engineering organisations it means staff. Decide from responsibilities.
4. "Engineering Manager" is not a seniority — it is an IC-vs-manager
   distinction. Map to `senior` or `lead` based on YoE asked, and add a
   domain tag `people-management` so downstream tailoring knows.

When the JD is ambiguous (e.g. a "Senior TPM" that reads like a Staff-level
strategic role), pick the label that matches the RESPONSIBILITIES, not the
title. Note the discrepancy in positioning_angles so downstream tailoring
can address it.

## domain_tags (3-10 short tags)

A domain tag describes the role's industry, technology domain, or business
context. Use short lowercase tags with hyphens, no spaces. Examples:
- Industries: "defense", "aerospace", "energy", "fintech", "healthcare",
  "biotech", "robotics", "semiconductors", "automotive", "saas",
  "enterprise", "consumer", "gaming", "media", "govtech", "climate",
  "logistics", "ecommerce", "insurance", "legal-tech", "ed-tech".
- Technical domains: "ml", "llm", "genai", "data-platform", "devops",
  "security", "embedded", "firmware", "autonomy", "perception",
  "control-systems", "hardware", "networking", "distributed-systems",
  "mobile", "web", "graphics", "search", "observability", "storage",
  "payments", "identity", "compliance-eng", "platform-eng".
- Business contexts: "startup", "growth-stage", "public-co", "hyperscaler",
  "nonprofit", "research-lab", "open-source", "pre-ipo", "series-a",
  "series-b", "series-c", "remote-first", "hybrid", "in-office".
- Role archetype modifiers: "ic", "people-management", "customer-facing",
  "internal-tools", "greenfield", "migration", "zero-to-one".

Prefer tags that exist in the list above when applicable; coin new tags
sparingly and only when nothing in the vocabulary fits. Limit to the most
salient 3-7 tags. Avoid tags that describe every job ("software",
"collaboration") — they add no signal. If the JD is explicit about
company stage (e.g. "Series B, 60 employees"), always include a stage tag.

## keywords (5-10 ATS must-hit terms)

Extract the concrete tools, frameworks, languages, methodologies, and
credentials that an ATS keyword filter would key on. Each keyword should
appear verbatim (or be strongly implied) in the JD. Examples:
- Languages / frameworks: "Python", "TypeScript", "React", "Rust",
  "CUDA", "FastAPI", "Django", "TensorFlow", "PyTorch", "LangChain",
  "LangGraph", "JAX", "SQL".
- Methodologies: "SAFe", "Scrum", "Lean", "Kanban", "Kaizen", "Six Sigma",
  "OKRs", "V-model", "MBSE", "DO-178C".
- Credentials: "PMP", "PMI-ACP", "CSM", "CISSP", "Secret clearance",
  "TS/SCI", "ITAR", "CMMC".
- Platforms: "AWS", "GCP", "Azure", "Databricks", "Snowflake", "Kubernetes",
  "Airflow", "dbt", "Spark", "Kafka", "Postgres".
- Domains: "RF design", "GPU kernels", "SLAM", "computer vision", "NLP",
  "MLOps", "RAG", "fine-tuning", "evals".

Do NOT include generic soft skills ("communication", "teamwork") or vague
attributes ("fast-paced", "passionate"). The ranker scores keyword overlap
against the candidate's resume; noise here pollutes the score. Do not
include the job title itself as a keyword — that is never what an ATS
filter keys on.

Prefer the exact capitalisation used in the JD (React, not react; Python,
not python). For acronyms that the JD spells out (e.g. "Large Language
Models (LLMs)"), emit both the acronym and the expansion only if both
appear frequently; otherwise just the more common form.

## priority_requirements (3-8 items)

The must-meet criteria from the JD, lightly paraphrased. Each should be
short (one line) and should match what a recruiter would use to reject a
resume in the first pass. Include degree requirements, years of experience,
clearance, specific technologies, and geographic constraints. Skip nice-to-
haves here — those belong in preferred_qualifications.

A priority requirement is a reject criterion, not just a positive signal.
Ask yourself: "would a recruiter scanning 200 resumes in 20 minutes throw
this one away if they didn't see this?" If yes, it is a priority
requirement. If no, it is a preferred qualification or a non-requirement.

## basic_qualifications and preferred_qualifications

- basic_qualifications: the minimum bar. Include degree, YoE, required
  tools, required clearances, required location. If the listing labels a
  section "Minimum Qualifications" or "What you'll bring", use that
  content verbatim. If not, infer from the "Requirements" section.
- preferred_qualifications: the nice-to-haves. Include advanced degrees,
  extra YoE beyond minimum, adjacent tools, prior industry exposure.
  If the JD does not distinguish basic from preferred, leave
  preferred_qualifications empty — do NOT duplicate the basic list.

Keep each qualification entry under ~30 words. One entry per distinct
requirement — do not merge "5+ years Python AND Java" into one line if the
JD treats them separately.

Watch for implicit qualifications. A JD that says "you will be the
technical owner of the deployment pipeline" implicitly requires CI/CD
experience even if no section lists it. Surface these in basic_qualifications
when the responsibility is clearly load-bearing.

## positioning_angles (3-5 strategic framings)

Each angle is a short argument the candidate should advance in their
resume and cover letter to land the role. Angles are NOT requirements —
they are interpretations of how the candidate should tell their story.

Examples:
- "Lead with autonomous-systems safety case experience; de-emphasise
  pure web-stack work."
- "Frame MBA + defense background as the bridge between engineering rigor
  and customer storytelling this role needs."
- "Highlight the two public-cloud migrations as direct evidence for the
  data-platform-consolidation scope."

Angles should be specific to THIS job, not generic career advice. They
are the most valuable output field — downstream tailoring hinges on them.

Good positioning angles do three things: (1) name the specific capability
to foreground, (2) name the specific background element to de-emphasise,
(3) imply the framing that ties them together. A weak angle like "emphasise
leadership experience" satisfies none of these. Strong angles are
directive and asymmetric.

## Calibration examples

The following are abbreviated illustrations of the shape of correct output.
They are deliberately incomplete — use them to calibrate specificity and
shape, not to copy field values.

Example A — "Staff Program Manager, Autonomous Delivery" at a robotics
startup (Series B, 80 employees, explicitly looking for candidates who
have shipped autonomous hardware to production):
- themes[0]: "autonomous-delivery program ownership from prototype to
  production". (Not "program management".)
- seniority: "staff". (Despite the "Staff" title, the JD talks about
  cross-team coordination across perception, motion planning, and
  hardware — that confirms staff.)
- domain_tags: ["robotics", "autonomy", "startup", "series-b", "hardware",
  "zero-to-one"].
- keywords: ["ROS", "Python", "C++", "Git", "JIRA", "Kubernetes",
  "simulation"].
- positioning_angles[0]: "Lead with the one-cycle prototype-to-production
  case study; the JD says explicitly they've been burned by PMs who've
  only operated in simulation."

Example B — "Senior Technical Program Manager" at a hyperscaler, data
platform org, migration from legacy Hadoop to Snowflake:
- themes[0]: "multi-petabyte data-platform migration from Hadoop to
  Snowflake". (Not "data infrastructure".)
- seniority: "senior". (Title and YoE both say senior; no ambiguity.)
- domain_tags: ["data-platform", "migration", "hyperscaler", "saas"].
- keywords: ["Snowflake", "Hadoop", "Spark", "Airflow", "dbt", "SQL"].
- positioning_angles[0]: "Open with the 'we moved 12PB off Hive' story;
  the JD is explicitly searching for someone who has 'been in the room
  when a migration went wrong'."

Example C — "Engineering Manager, Compliance Data" at a payments company,
building an evidence engine for SOC2/PCI auditors:
- themes[0]: "compliance evidence platform ownership with external
  auditor touch points". (Not "engineering management".)
- seniority: "senior" or "lead" depending on team size. Add
  "people-management" domain tag.
- domain_tags: ["fintech", "payments", "compliance-eng",
  "people-management", "enterprise"].
- keywords: ["Python", "PostgreSQL", "AWS", "SOC2", "PCI-DSS", "Kafka",
  "Terraform"].
- positioning_angles[0]: "Lead with the compliance audit ownership story;
  de-emphasise feature work that doesn't involve auditor-facing deliverables."

## Anti-patterns you must avoid

1. Do not fabricate themes, requirements, or domains not in the JD text.
2. Do not pad lists to hit the minimum length. If only two themes exist,
   return two.
3. Do not copy recruiter fluff verbatim ("fast-paced environment",
   "work-life balance") into any field.
4. Do not return empty arrays for themes, seniority, keywords, or
   basic_qualifications — those four fields are always extractable from
   any real JD.
5. Do not emit markdown, code fences, or commentary. Respond with raw
   JSON matching the schema only.
6. Do not use the company's marketing language verbatim. "World-class
   engineering culture" is marketing, not signal. Translate it to what
   the company actually does ("pair programming, trunk-based development,
   continuous deploy") only if the JD says so.
7. Do not conflate the role title with a theme. "Senior Product Manager"
   is the title; the theme is what the senior PM actually owns.
8. Do not emit US-state-specific compensation disclaimers in any field.
   They are regulatory padding, not signal.
9. Do not let the company's "About us" section drive themes. Themes come
   from the role, not the company.
10. Do not return identical values for basic_qualifications and
    priority_requirements — the fields have different purposes. The
    priority_requirements are the reject criteria; basic_qualifications
    are the full minimum bar, which may include items that the recruiter
    would NOT reject on (e.g. "authorized to work in the US" is basic
    but rarely a reject criterion in a US listing).

## Common JD patterns to recognise

Most job descriptions follow one of a few templates. Recognising the
template accelerates accurate extraction:

- "Responsibilities / Requirements / Preferred / Benefits" — the classic
  four-section template. The `priority_requirements` come from the
  first half of Requirements; anything after "nice to have" is preferred.
- "About us / About the role / What you'll do / What you'll bring /
  What we offer" — common at startups. "What you'll bring" contains both
  basic and preferred, usually with the word "bonus" or "plus" marking
  preferred.
- "Day in the life / Core skills / Stretch skills" — stretched-narrative
  template. Core = basic, stretch = preferred.
- Bullet-free narrative — some listings (often from engineering-led
  companies) describe the role entirely in prose. Read the whole thing
  twice; the requirements are buried in sentences like "you have shipped
  production systems that handle 10k QPS".
"""


async def job_analysis_node(
    state: JobAgentState,
    *,
    llm_client: LLMClient | None = None,
) -> JobAgentState:
    """Analyse every listing in ``qualified_listings`` and populate ``job_analyses``.

    Skips listings that already have a cached analysis (by ``dedup_key``).
    """
    state.phase = PipelinePhase.JOB_ANALYSIS

    if llm_client is None:
        from core.llm import AnthropicClient

        llm_client = AnthropicClient()

    return await _ENGINE.run(state, llm_client=llm_client, spec=JOB_ANALYSIS_SPEC)


def _prepare_runtime(_state: JobAgentState) -> _Runtime:
    settings = get_settings()
    prompt_template = (_PROMPTS_DIR / "tailor_job_analysis.md").read_text(encoding="utf-8")
    return _Runtime(prompt_template=prompt_template, settings=settings)


def _on_skip(state: JobAgentState) -> None:
    if state.dry_run:
        logger.info("job_analysis.skip_dry_run")
    elif not state.qualified_listings:
        logger.info("job_analysis.skip_no_listings")


def _analysis_cache_key(listing: JobListing) -> str:
    """Cache key for analysis includes dedup key + description fingerprint."""
    desc_hash = hashlib.sha256((listing.description or "").encode("utf-8")).hexdigest()[:16]
    return f"{listing.dedup_key}:{desc_hash}"


def _get_cached_result(
    state: JobAgentState, cache_key: str, runtime: _Runtime
) -> JobAnalysisResult | None:
    if not runtime.settings.job_analysis_enable_cache:
        return None
    return state.job_analysis_cache.get(cache_key)


def _cache_result(
    state: JobAgentState, cache_key: str, result: JobAnalysisResult, runtime: _Runtime
) -> None:
    if runtime.settings.job_analysis_enable_cache:
        state.job_analysis_cache[cache_key] = result


def _on_item_start(_state: JobAgentState, listing: JobListing, _runtime: _Runtime) -> None:
    if not listing.description:
        msg = f"Listing {listing.dedup_key} has empty description"
        raise ValueError(msg)
    listing.status = ApplicationStatus.ANALYZING


def _build_prompt(state: JobAgentState, listing: JobListing, runtime: _Runtime) -> str:
    jd_text = listing.description[: runtime.settings.job_analysis_max_input_chars]
    prompt = runtime.prompt_template.format(
        job_title=listing.title,
        company=listing.company,
        job_description=jd_text,
    )
    logger.debug(
        "job_analysis.prompt_built",
        dedup_key=listing.dedup_key,
        model=runtime.settings.job_analysis_model or "default",
        input_chars=len(jd_text),
        run_id=state.run_id,
    )
    return prompt


def _on_item_result(
    state: JobAgentState,
    listing: JobListing,
    result: JobAnalysisResult,
    runtime: _Runtime,
) -> None:
    state.job_analyses[listing.dedup_key] = result
    listing.status = ApplicationStatus.ANALYZED
    logger.info(
        "job_analysis.analysed",
        dedup_key=listing.dedup_key,
        themes=result.themes[:3],
        seniority=result.seniority,
        basic_quals=len(result.basic_qualifications),
        preferred_quals=len(result.preferred_qualifications),
        model_used=runtime.settings.job_analysis_model or "default",
        input_chars=min(len(listing.description), runtime.settings.job_analysis_max_input_chars),
    )


def _on_item_error(
    state: JobAgentState, listing: JobListing, exc: Exception, _runtime: _Runtime
) -> None:
    listing.status = ApplicationStatus.ERRORED
    state.errors.append(
        {
            "node": "job_analysis",
            "dedup_key": listing.dedup_key,
            "message": str(exc)[:500],
        }
    )
    logger.warning(
        "job_analysis.failed",
        dedup_key=listing.dedup_key,
        error=str(exc)[:200],
    )


def _on_complete(state: JobAgentState, _runtime: _Runtime) -> None:
    logger.info(
        "job_analysis.complete",
        total=len(state.qualified_listings),
        analysed=len(state.job_analyses),
        errors=len(state.errors),
    )


JOB_ANALYSIS_SPEC = _build_spec()
