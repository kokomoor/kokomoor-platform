# Ranking architecture — design notes

This document is the design space for the job-agent's ranking node beyond
today's deterministic fit scorer. It is *not* a commitment to build any of
these phases — it is a set of options the user can pull off the shelf when
the cheap floor (the rule-based scorer just landed in `ranking.py`) stops
being good enough.

The fit scorer landed today is intentionally a *floor*, not a ceiling. It
exists to prevent the bug where Sonnet cover-letter calls were spent on the
single salaried listing in the discovery batch instead of on the listings
that actually matched the candidate's profile. It does not understand
seniority mismatches, doesn't notice that "Senior MLE" and "Staff Research
Engineer" are talking about the same role at different companies, can't
tell that the requirement "deep familiarity with PyTorch" is satisfied by a
bullet that says "wrote a custom autograd kernel for ResNet-50 training,"
and has no model of how the candidate weighs autonomy vs. compensation vs.
mission.

Each phase below addresses a class of failure mode the cheap floor leaves
on the table. Phases are *additive*, not exclusive — phase N+1 sits on top
of phase N.

---

## Where we are today (Phase 0)

```
fit = 0.55 * basic_coverage + 0.25 * preferred_coverage + 0.20 * keyword_coverage
```

`*_coverage` = fraction of requirement phrases (from `JobAnalysisResult`)
whose substantive tokens (3+ chars, non-stopword) appear in the candidate
corpus (skills + bullet text). Sub-floor listings drop; the rest are picked
top-N by fit, salary as tiebreaker only.

**What this is good at:** kicking out listings whose requirement vocabulary
has no overlap with the candidate's resume vocabulary at all. Fast (no LLM
call), deterministic, debuggable, reproducible across runs.

**What this is bad at:**

- Synonyms — "PyTorch" matches "pytorch" but not "torch" or "deep
  learning frameworks."
- Negation — "no degree required" is treated the same as "degree required."
- Seniority — a Staff role and a Junior role with the same skill list score
  identically.
- Quality of evidence — a single passing mention of "Kubernetes" in a 2018
  bullet matches the same as production-scale K8s ownership.
- Cross-listing comparison — every listing is scored in isolation, so
  there's no way to express "this listing is much better than that one,"
  only "this listing scored 0.71."

These are the exact failure modes the next phases address.

---

## Phase 1 — Token expansion + section weighting

**Cost:** zero LLM, one new YAML file. **Effort:** ~half a day.

The simplest improvement that doesn't require a model call. Two changes:

1. **Synonym groups in the candidate corpus.** Add a small
   `pipelines/job_agent/context/skill_synonyms.yaml` mapping canonical skill
   tokens to their common aliases (`pytorch -> torch, deep-learning,
   neural-network`; `kubernetes -> k8s, eks, gke, aks`; `llm -> language
   model, large language model, gpt, claude, sonnet`). The corpus extractor
   expands every match through the synonym table at load time.
2. **Bullet-section weights.** A token that appears in the candidate's most
   recent role (top of the experience array) is worth more than the same
   token in a 2017 internship bullet. Weight tokens by their source bullet's
   position in the experience array, with a soft decay (`0.95^n`).

This still produces a deterministic fit score. It mostly addresses the
*synonyms* and *quality of evidence* gaps, partially addresses *seniority*
(by accident — junior bullets contribute less weight), and doesn't touch
*negation* or *cross-listing comparison* at all.

**When to skip directly to Phase 2:** if the synonyms file becomes the
dominant maintenance burden (i.e. the candidate or job market shifts faster
than the file can be updated by hand). At that point an embedding lookup
buys the same thing without the maintenance.

---

## Phase 2 — Embedding similarity per requirement

**Cost:** one embedding call per listing (~$0.0001 each), one call per
candidate-bullet at startup (cacheable for the run). **Effort:** ~1 day.

Replace the bag-of-tokens corpus check with cosine similarity between
embeddings of (a) each requirement phrase from `JobAnalysisResult` and
(b) each candidate bullet. The fit score becomes:

```
basic_coverage = mean(max_similarity(requirement, candidate_bullets) for requirement in basic_qualifications)
```

Where `max_similarity` is the cosine similarity between the requirement
embedding and the best-matching candidate bullet embedding. This collapses
to "how strongly does *some* bullet on this resume actually evidence this
requirement," which is exactly the question the rule-based scorer is
trying to approximate.

Embeddings handle synonyms and paraphrase natively, so the synonyms file
from Phase 1 either becomes optional or stays as a small targeted override
list. They still don't handle negation ("must NOT have managed people"
embeds the same as "must have managed people") or cross-listing comparison.

**Implementation notes:**

- Use `voyage-3` or `text-embedding-3-small` — both fit comfortably in
  the cost envelope, both have reasonable open documentation. Either
  can be swapped in via a config knob.
- Embed the candidate corpus *once per run* and pass the resulting
  numpy array down to the ranking node via the master-profile cache
  (the file we just added in remediation A5).
- Listing-side embeddings can be cached in the same `state.job_analyses`
  dict that already caches `JobAnalysisResult` per `dedup_key`.

**Failure mode this opens up:** embeddings can confidently match a
requirement to a totally unrelated bullet if they share surface vocabulary
("Python" requirements match "Python the snake conservation work" if the
candidate has weird hobbies). Mitigate by capping per-requirement
similarity at 0.9 and requiring at least one match above 0.5 for a
requirement to count as "covered."

---

## Phase 3 — LLM-as-judge per listing

**Cost:** one Haiku call per listing (~$0.001 each, ~$0.10 for 100
listings). **Effort:** ~1 day.

Run a structured Haiku call per listing with the prompt: "Given this job
analysis and this candidate profile, score the fit on a 0-10 scale. Return
JSON: { score: int, reasoning: string, blocking_concerns: string[] }."
Combine with the deterministic floor score for a hybrid:

```
final_fit = 0.4 * deterministic_fit + 0.6 * llm_score / 10
```

This is the first phase that handles *negation*, *seniority*, and *quality
of evidence* properly — Haiku can read "5+ years required" and notice that
the candidate has 3, or read "must have shipped a production LLM
application" and notice that the candidate has only research experience.

It still doesn't handle *cross-listing comparison*: each call is
independent, so the model can't say "listing A is a much better fit than
listing B" — only "listing A is a 7" and "listing B is a 7." Haiku
graders are notoriously sticky around the middle of their scale, and
nothing in the prompt forces them to discriminate.

**Cost ceiling:** at 100 listings/run × $0.001/call × 4 runs/day × 30 days
= $12/month. The same envelope as the existing job-analysis Haiku spend.
Cheap enough to leave on by default once it lands.

**When this is the right next step:** once the user is regularly seeing
listings reach tailoring where a human reviewer would say "you should not
apply for this — wrong seniority / wrong domain / requires something I
don't have." That's the failure mode Phase 3 is built to catch, and the
deterministic floor cannot.

---

## Phase 4 — Pairwise comparison + tournament ranking

**Cost:** O(N log N) Haiku calls per run. For 30 listings ≈ 150 calls ≈
$0.15/run. **Effort:** ~2-3 days.

Once Phase 3 is in place and the user trusts the per-listing scores enough
that the *only* remaining complaint is "the top 5 should be ordered
differently," switch to a tournament ranker:

1. Score every listing once with Phase 3.
2. Take the top 2 × `tailoring_max_listings` candidates by score.
3. Run Haiku pairwise prompts ("which of these two listings is a better
   fit and why?") in a single-elimination bracket.
4. The bracket winner is the top pick; reseed and repeat for the runner-up
   slots.

Pairwise prompts force the model to discriminate — it cannot answer
"both equal." Pairwise outputs also produce *much* more useful reasoning
than absolute scores ("listing A is better because the candidate's CFS
work is a closer match than the unrelated robotics bullets in listing B").

Pairwise is overkill for current discovery volume (`tailoring_max_listings=5`)
but becomes valuable once the funnel widens (e.g. discovery returns 200+
listings/day, top-of-funnel gets noisy, and the user is investing real
time in reading tailored output).

---

## Phase 5 — User-feedback-trained ranker

**Cost:** zero per-call (linear model on cached features), training is
amortised. **Effort:** ~1 week, once the dataset exists.

The most expensive phase to set up but the only one that *actually
personalises* to this candidate. Steps:

1. Log every (listing, fit_score, llm_judge_score, was_applied,
   got_response, got_interview, got_offer) tuple to a feedback database.
2. After ~50-100 labelled outcomes, fit a logistic regression (or
   gradient-boosted tree, depending on dataset size) on the features
   above plus the deterministic and embedding-based intermediates from
   earlier phases.
3. The trained model becomes the ranker for the next run, with the
   pre-trained signals as inputs.

This is the only phase that closes the loop on *whether the candidate
actually wanted what was selected*. Phases 1-4 all proxy for fit using
the candidate's profile and the job's requirements; Phase 5 asks the
candidate's *behaviour* directly.

**Pre-requisite:** the user has to be willing to label outcomes (mark
listings as "applied / not applied / interviewed / hired / no") with
enough discipline to produce a training set. The labelling UX is a
separate design problem and probably the gating concern, not the model.

**When to consider:** after the user has been running the agent in
production for a few months and has accumulated outcome data. Don't
build this until the labelled dataset exists — there's no way to
validate the model without it.

---

## Decision matrix

| Phase | LLM cost / run | Closes synonym gap | Closes seniority gap | Closes cross-listing gap | Personalises |
|-------|----------------|--------------------|----------------------|---------------------------|--------------|
| 0 (today) | $0 | partially | no | no | no |
| 1 (synonyms + weights) | $0 | mostly | partially | no | no |
| 2 (embeddings) | ~$0.01 | yes | partially | no | no |
| 3 (Haiku judge) | ~$0.10 | yes | yes | no | no |
| 4 (pairwise) | ~$0.15 | yes | yes | yes | no |
| 5 (trained) | ~$0.01 | yes | yes | yes | yes |

The plan is to ship phases in order, evaluate each one against actual
listings, and only move on when the previous phase's failure modes show
up reliably enough to justify the cost of the next one.

## Things to not do

- **Don't chain multiple LLM calls per listing in Phase 3.** A single
  structured call with a clear schema is cheaper, faster, and easier to
  debug than a multi-step "first extract, then compare, then score"
  prompt. Save multi-step for Phase 4.
- **Don't use Sonnet for grading.** Haiku is more than good enough for
  grading work and one twentieth the cost. Reserve Sonnet for tailoring,
  where output quality directly hits a recipient.
- **Don't precompute embeddings for the entire candidate corpus on
  every run.** Cache them in the master-profile loader (the cache that
  already exists after remediation A5). Re-embed only when the master
  profile mtime changes.
- **Don't build Phase 5 (trained ranker) without an outcomes dataset.**
  No matter how clean the model code is, it will silently overfit on a
  dataset of ten labelled examples. Wait.
