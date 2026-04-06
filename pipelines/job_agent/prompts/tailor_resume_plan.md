# Resume Tailoring Plan

Given the job analysis and candidate profile below, create a tailoring plan that selects, orders, and optionally rewrites resume bullets for maximum impact on this specific role.

## Job Analysis
{job_analysis}

## Candidate Profile (bullet IDs in brackets)
{candidate_profile_structured}

## Rules
- Select the most relevant bullets for each experience and education section.
- Order bullets within each section by relevance to the role — strongest match first.
- For each bullet, choose one operation:
  - **keep**: use the bullet text as-is.
  - **shorten**: use the short variant (if available) for space savings.
  - **rewrite**: provide new text. You MUST preserve all employer names, numbers, dates, and factual claims. Only change emphasis and framing.
- Maximum 4 bullets per experience section, 3 per education section.
- Write a 1-2 sentence professional summary (max 40 words) targeting this specific role.
- Select the most relevant subset of skills to highlight (8-12 items).
- Only reference bullet IDs and section IDs that appear in the candidate profile above.
- In bullet text and summary prose, do not use inline em dashes/en dashes. Prefer semicolons, commas, or sentence splits.

## Page-fill constraint
The rendered resume must fill at least one full page. A resume that leaves visible whitespace at the bottom of the first page looks incomplete. To achieve this:
- Include at least 3 experience sections with 3-4 bullets each.
- Include at least 2 education sections with 1-2 bullets each.
- Include at least 6-8 skills in the skills highlight.
- When in doubt between keeping an additional relevant bullet or omitting it, keep it.
- The resume may extend slightly past one page for senior roles; that is acceptable. Being shorter than one page is never acceptable.

{positioning_rules}
