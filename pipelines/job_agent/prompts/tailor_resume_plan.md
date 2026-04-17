# Resume Tailoring Plan

Given the job analysis and candidate profile below, produce a tailoring plan that selects, orders, and (where needed) recasts resume bullets to land the role. The candidate's work-history spine is fixed; your job is to contour framing, not to curate which jobs are shown.

## Job Analysis
{job_analysis}

## Candidate Profile (bullet IDs in brackets)
{candidate_profile_structured}

## Section tiers and anchor bullets

Every experience and education section has a tier suffix on its header: `(PINNED)` or `(OPTIONAL)`. Bullets marked `(ANCHOR)` are load-bearing for their section.

- **PINNED sections MUST appear** in `experience_sections` / `education_sections`. Do not omit them, reorder them, or drop them in favor of optional sections. The applier will auto-insert any pinned section you forget, but you should include them explicitly so you can order their bullets for the role.
- **OPTIONAL sections MAY appear** when the role's domain tags align. Include them only when they genuinely strengthen the candidacy; otherwise omit.
- **ANCHOR bullets MUST appear** in their section's `bullet_order`. The applier will auto-prepend anchors you omit, but include them explicitly so you can place them where they have maximum effect.

## Bullet operations

For each bullet in each section, choose one operation in `bullet_ops`:

- **keep** — use the master bullet's `text` verbatim. Default choice when the bullet already lands for this role.
- **shorten** — use the `short variant` (if defined) for space savings. Falls back to master `text` when no variant exists.
- **recast** — compose new bullet text from the bullet's own `source_material` and `text`. Governed by strict constraints below.

`recast` replaces the deprecated `rewrite` op; they are treated identically by the applier, but use `recast` in new plans.

### Recast contract (strict)

Recast is for contouring framing, not invention. The applier validates every recast and rejects violations, falling back to `keep` on failure.

1. **Fact palette is the bullet's own `source_material` and `text`.** Do not introduce facts from other bullets, other experiences, or outside knowledge. If the role demands a fact the bullet does not carry, pick a different bullet.
2. **Preserve verbatim:** every dollar amount, percentage, integer, acronym (RADAR, DSP, HIL, MIL-STD, etc.), and multi-word capitalized proper noun (Electric Boat, Columbia-class, Lincoln Laboratory, LangGraph, etc.) must appear verbatim in your recast. Do not paraphrase "$100M" as "$100 million"; do not shorten "Electric Boat" to "EB".
3. **Length parity.** The recast's word count must not exceed the master bullet's word count by more than 20%. Contouring means reframing, not expanding.
4. **Surface the right angle.** Change verbs, adverbs, ordering, and emphasis to highlight aspects of the work that match the job's `priority_requirements`, `themes`, and `must_hit_keywords`. Keep the underlying claims identical.

### Example of acceptable recast

- Master text: "Led embedded software development for $100M Army RADAR modernization program across next-generation phased array systems"
- Role: staff software engineer emphasizing real-time embedded C++ firmware
- Acceptable recast: "Owned real-time embedded C++ firmware for $100M Army RADAR modernization program across next-generation phased array systems"
- Unacceptable recast (invented fact): "Led embedded C++17 firmware for $100M Army RADAR modernization program used by 3 combat deployments"  ← "C++17", "3 combat deployments" not in source
- Unacceptable recast (length): expansion that doubles the word count

## Supplementary projects

If the candidate profile includes a `SUPPLEMENTARY PROJECTS` block, you may surface any subset by listing their ids in `supplementary_project_ids`. They render under the Additional Information section, not Experience. Apply `recast`/`shorten`/`keep` ops to them the same way as bullets. Default: include when a project clearly strengthens fit; omit when it would be filler.

## Quantitative caps

- Experience: at most 5 bullets per section.
- Education: at most 3 bullets per section.
- Professional summary: one to two sentences, max 40 words, targeting this specific role.
- Skills: 8–12 most-relevant items from the candidate's skill list.
- Supplementary projects: at most 4.

## Style constraints

- Only reference bullet IDs and section IDs that appear in the candidate profile above.
- Do not use inline em dashes or en dashes in bullet text or summary prose — prefer semicolons, commas, or sentence splits.
- Summary: write in third-person or neutral voice suitable for a resume; not "I led…" phrasing.

## Page-fill target

The rendered resume should comfortably fill one full page and may run to a second page for senior roles.

- Include every pinned section (this is not optional).
- Include 2–3 bullets per education section, 3–5 bullets per experience section.
- When undecided between keeping one more relevant bullet or omitting it, keep it.

{positioning_rules}
