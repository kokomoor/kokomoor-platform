# Ranking architecture — design notes

## The two questions

The ranking system must answer two distinct questions for every listing:

1. **Desire fit** — Would Sam actually want this job? Is it at a
   frontier company doing interesting work in a domain he cares about?
   Is it the kind of role where he'd feel like he's in the middle of
   the technical revolution, not watching it from the sidelines?

2. **Qualification stretch** — Given that he wants it, can we build
   a credible narrative from his background (MIT Sloan, Lincoln Lab,
   Electric Boat, Gauntlet-42, ML research, defense clearance) that
   makes him look like a strong candidate?

Both must pass for a listing to reach tailoring. Desire fit is the
**primary** ranking signal. Qualification stretch is the **gate**.

The previous architecture only addressed question 2 — it computed how
well the candidate's token vocabulary overlapped with the job's stated
requirements. That's necessary but not sufficient: a TPM role at Deloitte
with perfectly matching quals would rank above a stretch role at Anthropic,
which is exactly wrong.

---

## What "desire fit" actually means

Sam's job preferences decompose into five weighted dimensions. These are
not abstract — they come directly from his stated values:

| Dimension | Weight | What it captures |
|-----------|--------|------------------|
| **Company tier** | 0.30 | Is this a dream company, a frontier startup, or a generic services firm? |
| **Domain excitement** | 0.25 | Is the work in AI, nuclear, robotics, drones, autonomous vehicles, semiconductors, space, or tech-enabled finance? |
| **Role growth potential** | 0.20 | Does this role involve creative problem solving, technical depth, and a path to expertise — or is it process management and slide decks? |
| **Impact & mission** | 0.15 | Is the company building something that matters, or shuffling paper? |
| **Compensation signal** | 0.10 | Is the posted (or inferred) compensation competitive? |

These weights are tunable. They should live in a YAML file alongside the
candidate profile so they can be adjusted without code changes.

---

## Candidate Desire Profile

The desire profile is a new structured document that encodes Sam's
preferences. It lives at
`pipelines/job_agent/context/candidate_desires.yaml` and is loaded
alongside the master resume profile. It is the input to the desire-fit
scoring system the same way `candidate_profile.yaml` is the input to the
qualification-fit scorer.

```yaml
schema_version: 1

# --- Company classification ---
# Tier 1: dream companies — automatic high desire signal
# Tier 2: strong interest — known frontier/prestigious companies
# Tier 3: conditional interest — depends on role and domain
# Companies not in any tier are scored by domain/role signals only
# Anti-tier: explicit rejects — score floors to zero regardless of role

company_tiers:
  tier_1:
    - Anthropic
    - OpenAI
    - SpaceX
    - Commonwealth Fusion Systems
    - NuScale
    - NVIDIA
    - Boston Dynamics
    - Waymo
    - Zoox
    - Apple
    - Google DeepMind

  tier_2:
    - AMD
    - Google
    - Tesla
    - Anduril
    - Palantir
    - Scale AI
    - Databricks
    - Stripe
    - Figma
    - Notion
    - Cruise
    - Aurora
    - Shield AI
    - Skydio
    - Saronic
    - Hermeus
    - Relativity Space
    - Rocket Lab
    - Astra
    - Cerebras
    - SambaNova
    - Groq
    - D.E. Shaw
    - Citadel
    - Two Sigma
    - Jane Street
    - Renaissance Technologies
    - Bridgewater
    - Point72
    - HRT (Hudson River Trading)
    - Jump Trading
    - Cohere
    - Mistral
    - Inflection AI
    - xAI
    - Meta (FAIR)
    - Microsoft Research
    - Amazon (Lab126 / Robotics)

  tier_3:
    - Airbnb
    - Robinhood
    - Coinbase
    - Rippling
    - Ramp
    - Brex
    - Reddit
    - Instacart
    - DoorDash
    - Plaid
    - Gusto
    - Affirm
    - Chime
    - Mercury
    - Vanta
    - Retool
    - Vercel
    - Linear
    - Replit

  anti_tier:
    - Deloitte
    - Accenture
    - McKinsey
    - BCG
    - Bain
    - KPMG
    - PwC
    - EY
    - Booz Allen Hamilton
    - Capgemini
    - Infosys
    - Wipro
    - TCS
    - Cognizant
    - CGI
    - SAIC
    - Leidos
    - Peraton
    - ManTech

# --- Domain excitement ---
# Keywords/phrases that signal a domain Sam cares about.
# Matched against job description, title, and company description.

excited_domains:
  ai_ml:
    weight: 1.0
    signals:
      - artificial intelligence
      - machine learning
      - deep learning
      - large language model
      - LLM
      - foundation model
      - neural network
      - transformer
      - generative AI
      - computer vision
      - NLP
      - reinforcement learning
      - MLOps
      - model training
      - inference
      - AI safety
      - alignment

  robotics_autonomy:
    weight: 0.95
    signals:
      - robotics
      - autonomous
      - self-driving
      - drone
      - UAV
      - UAS
      - unmanned
      - perception
      - motion planning
      - SLAM
      - manipulation
      - legged robot
      - humanoid

  nuclear_energy:
    weight: 0.90
    signals:
      - nuclear
      - fusion
      - fission
      - reactor
      - tokamak
      - stellarator
      - advanced reactor
      - small modular reactor
      - SMR
      - nuclear engineering
      - plasma physics
      - energy storage

  semiconductors_hardware:
    weight: 0.90
    signals:
      - semiconductor
      - chip design
      - ASIC
      - FPGA
      - GPU architecture
      - AI accelerator
      - silicon
      - fab
      - lithography
      - EUV
      - compute hardware
      - TPU
      - neural processing unit

  space:
    weight: 0.85
    signals:
      - spacecraft
      - satellite
      - launch vehicle
      - orbital
      - propulsion
      - avionics
      - space systems
      - mission operations

  frontier_defense:
    weight: 0.80
    signals:
      - electronic warfare
      - radar
      - sensor fusion
      - C4ISR
      - hypersonic
      - directed energy
      - ISR
      - SIGINT
      - autonomous weapons
      - counter-UAS

  quant_finance:
    weight: 0.75
    signals:
      - quantitative
      - trading
      - hedge fund
      - systematic
      - alpha generation
      - portfolio
      - derivatives
      - market making
      - risk modeling
      - fintech infrastructure

# --- Role growth signals ---
# Phrases in the JD that indicate the role involves creative problem
# solving, technical depth, and growth — vs. pure process management.

role_growth_positive:
  - build from scratch
  - greenfield
  - architect
  - design systems
  - first hire
  - founding team
  - technical leadership
  - hands-on
  - full stack
  - end-to-end ownership
  - prototype
  - R&D
  - research
  - novel
  - cutting-edge
  - state-of-the-art
  - publish
  - open source
  - patent
  - technical depth
  - systems design
  - infrastructure
  - platform
  - scale
  - distributed systems
  - high performance
  - low latency
  - real-time
  - mission critical

role_growth_negative:
  - maintain existing
  - support tickets
  - documentation only
  - process compliance
  - audit
  - vendor management
  - staffing
  - resource allocation
  - travel 50%
  - travel 75%
  - on-call rotation
  - shift work
  - legacy system

# --- Impact signals ---
impact_positive:
  - climate
  - clean energy
  - national security
  - public safety
  - healthcare
  - save lives
  - open source
  - democratize
  - frontier
  - breakthrough
  - first-of-its-kind
  - transformative
  - moonshot

impact_negative:
  - advertising
  - ad tech
  - engagement optimization
  - click-through
  - SEO
  - affiliate
  - gambling
  - payday loan

# --- Title signals ---
# Some titles are strong positive/negative indicators regardless of
# the rest of the listing.

title_boost:
  - technical program manager
  - TPM
  - senior engineer
  - staff engineer
  - principal engineer
  - engineering manager
  - product engineer
  - ML engineer
  - research engineer
  - robotics engineer
  - systems engineer
  - platform engineer
  - founding engineer
  - head of engineering
  - VP engineering
  - CTO
  - technical director
  - program manager
  - chief of staff

title_penalty:
  - recruiter
  - sales
  - account manager
  - customer success
  - support engineer
  - QA analyst
  - manual tester
  - data entry
  - administrative
  - coordinator
  - receptionist
  - marketing
  - content writer
  - social media
  - HR
  - payroll
```

This YAML is the machine-readable encoding of the conversation Sam would
have with a model about what he wants. The "conversational elicitation"
idea is a great future enhancement (Phase 3 below), but the structured
YAML is the right starting point because:

1. It's deterministic and debuggable — you can see exactly why a listing
   scored high or low.
2. It's fast — no LLM call needed for the base desire-fit score.
3. It's easy to iterate — edit the YAML, rerun, see different results.
4. It composes cleanly with the existing qualification-fit scorer.

---

## Architecture: the ranking pipeline

The new ranking system replaces the current single-signal scorer with a
two-stage pipeline inside the existing `ranking_node`:

```
                    ┌──────────────────────┐
                    │  Job Analysis Result  │
                    │  (already computed)   │
                    └──────────┬───────────┘
                               │
                    ┌──────────▼───────────┐
                    │  Stage 1: Desire Fit │ ← candidate_desires.yaml
                    │  "Would he want it?" │
                    └──────────┬───────────┘
                               │
                         desire_score
                               │
                    ┌──────────▼───────────┐
                    │  Stage 2: Qual Stretch│ ← candidate_profile.yaml
                    │  "Can we make a case?"│
                    └──────────┬───────────┘
                               │
                     qualification_score
                               │
                    ┌──────────▼───────────┐
                    │   Combined ranking   │
                    │   + hard gates       │
                    └──────────┬───────────┘
                               │
                    top-N to tailoring
```

**Combined score:**

```
rank_score = 0.65 * desire_score + 0.35 * qualification_score
```

Desire is weighted nearly 2:1 over qualification. A listing Sam would
love but that's a stretch still outranks a listing that matches his
resume perfectly but he'd hate working there.

**Hard gates (either kills the listing):**

- `desire_score < 0.15` — anti-tier company or completely irrelevant
  domain. No amount of qualification match saves this.
- `qualification_score < 0.15` — even with generous stretching, there's
  no credible narrative. A nuclear reactor operator certification
  requirement with no engineering degree path, for example.

---

## Phase 0 — Deterministic desire scoring (no LLM)

**Cost:** zero. **Effort:** ~1 day. **This is what to build first.**

Load `candidate_desires.yaml` alongside the master profile. Score each
listing on five dimensions using the `JobAnalysisResult` that already
exists plus the listing's metadata:

### Company tier score (weight: 0.30)

```python
def _company_tier_score(company: str, tiers: CompanyTiers) -> float:
    """Score based on which tier the company falls in."""
    normalized = company.strip().lower()
    if _fuzzy_match(normalized, tiers.anti_tier):
        return 0.0    # hard zero — kills the listing via the gate
    if _fuzzy_match(normalized, tiers.tier_1):
        return 1.0
    if _fuzzy_match(normalized, tiers.tier_2):
        return 0.75
    if _fuzzy_match(normalized, tiers.tier_3):
        return 0.50
    return 0.30  # unknown company — neutral, scored by other signals
```

The fuzzy match is important: "Google DeepMind" should match a listing
from "Google" that mentions "DeepMind" in the description, and "NVIDIA
Corporation" should match "NVIDIA". Use normalized substring matching
with a short alias table, not strict equality.

### Domain excitement score (weight: 0.25)

```python
def _domain_score(
    description: str,
    analysis: JobAnalysisResult,
    domains: dict[str, DomainConfig],
) -> float:
    """Score based on how exciting the domain is."""
    # Check both the raw description and the structured analysis
    text = f"{description} {' '.join(analysis.themes)} {' '.join(analysis.domain_tags)}"
    text_lower = text.lower()

    best_match = 0.0
    for domain in domains.values():
        hits = sum(1 for signal in domain.signals if signal.lower() in text_lower)
        if hits > 0:
            # More hits = higher confidence this is actually in that domain
            confidence = min(hits / 3, 1.0)  # saturate at 3 hits
            score = domain.weight * confidence
            best_match = max(best_match, score)

    return best_match
```

This checks the full job description text plus the structured themes and
domain tags from the job analysis. A listing that mentions "large language
model" once gets partial credit; one that mentions LLM, transformer,
foundation model, and AI safety gets full credit for the AI domain.

### Role growth score (weight: 0.20)

```python
def _role_growth_score(
    description: str,
    title: str,
    positive_signals: list[str],
    negative_signals: list[str],
    title_boost: list[str],
    title_penalty: list[str],
) -> float:
    """Score based on role's growth/creativity potential."""
    text_lower = description.lower()
    title_lower = title.lower()

    score = 0.5  # neutral baseline

    # Title signals
    if any(t.lower() in title_lower for t in title_boost):
        score += 0.20
    if any(t.lower() in title_lower for t in title_penalty):
        score -= 0.40

    # Description signals
    pos_hits = sum(1 for s in positive_signals if s.lower() in text_lower)
    neg_hits = sum(1 for s in negative_signals if s.lower() in text_lower)
    score += min(pos_hits * 0.05, 0.30)
    score -= min(neg_hits * 0.10, 0.40)

    return max(0.0, min(1.0, score))
```

### Impact score (weight: 0.15)

Same structure as role growth — keyword match against impact positive
and negative signals, with a neutral baseline of 0.5 so unknown impact
doesn't penalize.

### Compensation score (weight: 0.10)

```python
def _compensation_score(listing: JobListing) -> float:
    """Score based on posted salary signals."""
    if listing.salary_max is None and listing.salary_min is None:
        return 0.5  # unknown — neutral
    best = listing.salary_max or listing.salary_min or 0
    if best >= 250_000:
        return 1.0
    if best >= 200_000:
        return 0.80
    if best >= 170_000:
        return 0.60
    if best >= 140_000:
        return 0.40
    return 0.20
```

### Qualification stretch (replacing the old fit score)

The existing `_coverage()` function stays, but its interpretation changes.
Instead of "does the candidate meet the requirements," it becomes "is
there enough overlap to build a narrative." The threshold drops: a
coverage score of 0.25 is enough to pass the gate, because the tailoring
system's entire job is to *stretch* the narrative.

```python
def _qualification_stretch_score(
    analysis: JobAnalysisResult,
    corpus: frozenset[str],
) -> float:
    """How stretchable is the candidate's background for this role?"""
    basic_cov = _coverage(analysis.basic_qualifications, corpus)
    preferred_cov = _coverage(analysis.preferred_qualifications, corpus)
    keyword_cov = _coverage(analysis.must_hit_keywords, corpus)

    # Weight preferred quals higher than before — if the candidate
    # matches preferred quals well, that's strong narrative material
    # even if basic quals are a stretch.
    stretch = (0.40 * basic_cov) + (0.35 * preferred_cov) + (0.25 * keyword_cov)

    # Seniority adjustment: the job analysis already extracts seniority.
    # A "junior" role is a poor stretch for someone with Sam's background
    # (overqualified → won't get it, and wouldn't want it).
    # A "director" or "VP" role is aspirational but possible with MBA.
    # No adjustment for mid/senior/staff — those are the sweet spot.
    return stretch
```

### The combined ranker

```python
desire = (
    0.30 * company_tier
    + 0.25 * domain_excitement
    + 0.20 * role_growth
    + 0.15 * impact
    + 0.10 * compensation
)

qualification = qualification_stretch_score

# Hard gates
if desire < 0.15 or qualification < 0.15:
    listing.status = SKIPPED

rank_score = 0.65 * desire + 0.35 * qualification
```

**What this buys immediately:** A listing at Anthropic for a TPM role
mentioning LLMs gets `company_tier=1.0, domain=1.0, role_growth~0.7,
impact~0.7, comp=0.5` → `desire~0.82`. Even if qualification stretch is
only 0.35, the combined score is `0.65*0.82 + 0.35*0.35 = 0.66`.

A listing at Deloitte for a perfectly matching TPM gets
`company_tier=0.0` → `desire < 0.15` → hard-gated out regardless of
qualification match.

A listing at an unknown company doing AI robotics gets
`company_tier=0.30, domain~0.90, role_growth~0.6, impact~0.5, comp=0.5`
→ `desire~0.55`. If quals stretch to 0.40, combined is `0.50` — it makes
it through and gets ranked in the middle, which is correct: interesting
work at an unknown company, moderate match.

---

## Phase 1 — Enriched company intelligence (low-cost LLM)

**Cost:** one Haiku call per *unknown* company (~$0.001 each, cached
across runs). **Effort:** ~1 day.

Phase 0's company tier is a lookup table. It handles the 50+ explicitly
listed companies well, but discovery returns hundreds of companies that
aren't on any list. Phase 0 gives them all 0.30 (neutral), which means
the domain/role/impact signals do all the work. That's often fine, but
it misses cases like:

- A startup called "Aether Machines" that builds autonomous drones —
  should be tier 2, but isn't in the list.
- A company called "Cerebral" that's a telehealth company — sounds
  techy, but isn't frontier.

Phase 1 adds a one-time Haiku lookup per unknown company:

```
Prompt: "Given this company name and job listing, classify the company:
- What industry/domain is this company in? (1-3 words)
- Is this company working on frontier/cutting-edge technology? (yes/no/unclear)
- Company prestige tier: (frontier_tech | established_tech | startup_interesting |
  startup_generic | services_consulting | traditional_industry | unclear)
Return JSON."
```

Results are cached in a persistent company intelligence store (SQLite or
JSON file alongside `dedup_seen.json`). The cache key is the normalized
company name. Cache TTL: 90 days (companies don't change industries
often).

The Haiku classification maps to a tier score:

| Classification | Score |
|---|---|
| frontier_tech | 0.85 |
| established_tech | 0.60 |
| startup_interesting | 0.70 |
| startup_generic | 0.35 |
| services_consulting | 0.05 |
| traditional_industry | 0.20 |
| unclear | 0.30 |

Cost ceiling: at most ~200 unique unknown companies per month × $0.001 =
$0.20/month. Negligible.

**When to build this:** when you see good listings from unknown companies
consistently ranking in the middle (desire ~0.5) and you're manually
thinking "I'd actually want that one" or "I'd never want that one."

---

## Phase 2 — LLM desire judge per listing

**Cost:** one Haiku call per listing (~$0.001 each). **Effort:** ~1 day.

Same shape as the old Phase 3 (LLM-as-judge), but the prompt is
fundamentally different. Instead of "score the fit," it's:

```
You are evaluating whether a specific job seeker would WANT this job.

The candidate values:
1. Frontier, bleeding-edge technology work (AI, nuclear, robotics,
   autonomy, semiconductors, space, quant finance)
2. Prestigious, high-status employers where the work is meaningful
3. Creative problem solving and technical depth — not process management
4. Good compensation, but secondary to the above
5. Being in the middle of the technical revolution, not watching it

The candidate's background: MIT Sloan MBA, MIT Lincoln Lab (RADAR),
General Dynamics Electric Boat (submarines), PropTech startup cofounder,
ML research, DoD Secret clearance.

Given this job listing, score:
- desire_score (0-10): How excited would this candidate be about this role?
- stretch_feasibility (0-10): How credible a case can be made from their
  background?
- reasoning: One sentence on why.
- red_flags: Anything that would make this a bad fit despite surface appeal.
```

The LLM judge handles nuance that keyword matching can't:

- "This role says 'autonomous systems' but it's actually about
  autonomous accounting workflows, not robots."
- "This is technically a TPM role but the JD reads like a project
  coordinator with no technical depth."
- "The company is unknown but the JD describes building a novel
  fusion reactor control system — strong desire fit."

**Hybrid scoring:**

```
final_desire = 0.5 * deterministic_desire + 0.5 * llm_desire / 10
final_qual = 0.5 * deterministic_qual + 0.5 * llm_stretch / 10
rank = 0.65 * final_desire + 0.35 * final_qual
```

**When to build this:** when the deterministic scorer is regularly
producing rankings where the top 5 include a listing you'd skip
manually, or excludes one you'd pick. The LLM judge catches semantic
nuance that keyword matching fundamentally cannot.

---

## Phase 3 — Conversational desire elicitation

**Cost:** one Sonnet conversation per profile update (rare). **Effort:**
~2-3 days.

This is the idea you raised: have a conversation with a model about what
you want, and it produces the `candidate_desires.yaml` automatically.

Implementation:

1. A standalone script (`scripts/elicit_desires.py`) launches a
   multi-turn Sonnet conversation.
2. The model asks targeted questions:
   - "What companies make you excited when you see their name?"
   - "What kind of work do you find boring?"
   - "If you could pick any job in the world right now, what would it be?"
   - "What would make you turn down a high-paying offer?"
   - "Rate these domains 1-10: AI, nuclear, robotics, ..."
3. After 5-10 exchanges, the model synthesizes the conversation into a
   `candidate_desires.yaml` with all the tier lists, domain weights,
   growth signals, etc.
4. The user reviews and edits the YAML, then it's used by the pipeline.

The value here is *elicitation*, not *runtime scoring*. The conversation
happens once (or when preferences change), not per pipeline run.
This is the right tool for turning "I want something frontier and
bleeding edge" into a structured preference model, because:

- A human writing the YAML from scratch will miss signals they care
  about but haven't thought to list.
- The conversational model can probe edge cases: "You said you like
  defense — would you work at a defense contractor that primarily does
  IT services?"
- It produces a structured output that the deterministic scorer can use
  without any per-run LLM cost.

**When to build this:** after Phase 0 is running and you've manually
edited the desires YAML a few times. At that point you'll know what
dimensions matter and what the conversation needs to surface.

---

## Phase 4 — Pairwise desire comparison

**Cost:** O(N log N) Haiku calls per run. **Effort:** ~2-3 days.

Same concept as the old pairwise tournament, but the comparison prompt
is desire-focused:

```
"Which of these two jobs would the candidate be more excited about, and
why? Consider: company prestige, domain excitement, role growth
potential, impact, and compensation."
```

Pairwise forces discrimination that absolute scoring can't. "Both are 7s"
becomes "listing A is better because the autonomous drone work is exactly
what the candidate wants, while listing B is a good company but the role
is operational."

**When to build:** once you're consistently seeing the top 5 and
thinking "the ordering is wrong" rather than "the wrong listings made
it."

---

## Phase 5 — Feedback-trained desire model

**Cost:** zero per-call. **Effort:** ~1 week.

Log `(listing, desire_score, qual_score, was_opened, was_applied,
got_interview, user_reaction)` tuples. After 50-100 labelled
examples, train a lightweight model on the features from earlier phases.
This is the only phase that actually learns what Sam wants from his
*behaviour*, not his stated preferences.

The critical addition over the old Phase 5 design: track
`user_reaction` — did Sam look at the tailored output and say "yes,
apply" or "no, skip this one"? That's the direct signal. Interview
outcomes are lagging indicators; the immediate reaction is what
personalizes desire fit.

**When to build:** after months of running with labelled outcomes.

---

## Interaction with existing pipeline stages

### Prefilter (discovery node)

The prefilter runs on `ListingRef` — it has title, company, and location
but NOT the full description. It should get a lightweight desire check:

```python
def score_listing_ref(ref: ListingRef, criteria: SearchCriteria) -> float:
    # ... existing role/keyword/company/location scoring ...

    # NEW: company tier boost
    if _fuzzy_match(ref.company, desires.tier_1):
        score += 0.50  # tier 1 companies always survive prefilter
    elif _fuzzy_match(ref.company, desires.tier_2):
        score += 0.35
    elif _fuzzy_match(ref.company, desires.anti_tier):
        score -= 0.80  # anti-tier hard-reject at prefilter

    # NEW: title desire signals
    if any(t.lower() in ref.title.lower() for t in desires.title_boost):
        score += 0.15
    if any(t.lower() in ref.title.lower() for t in desires.title_penalty):
        score -= 0.30

    return max(0.0, min(1.0, score))
```

This means a listing from Anthropic survives prefilter even if it doesn't
match a target role keyword. And a listing from Deloitte gets killed
before it wastes a bulk extraction fetch.

### Filtering node

No change needed. Salary floor is orthogonal to desire/qualification
ranking.

### Job analysis node

The `JobAnalysisResult` already extracts `themes`, `domain_tags`,
`seniority`, `basic_qualifications`, `preferred_qualifications`,
`must_hit_keywords`, and `angles`. These are exactly the inputs the
desire scorer needs. No schema change required.

One potential enhancement: add a `company_description` field to
`JobAnalysisResult` that extracts a one-line description of what the
company does from the JD. Many JDs open with "About [Company]" — this
gives the domain scorer better signal than just keyword matching on the
full description. This is optional and can be added later.

### Ranking node

This is where the bulk of the new logic lives. The current
`ranking_node` computes qualification coverage and sorts by fit. The new
version computes both desire and qualification scores, applies hard
gates, and sorts by the combined signal.

The `_ListingScore` dataclass grows:

```python
@dataclass(frozen=True)
class _ListingScore:
    listing: JobListing
    desire: float
    qualification: float
    combined: float
    # Breakdown for logging
    company_tier: float
    domain_excitement: float
    role_growth: float
    impact: float
    compensation: float
    basic_coverage: float
    preferred_coverage: float
    keyword_coverage: float
```

### Tailoring and cover letter nodes

No changes needed to the tailoring pipeline. It already receives
`JobAnalysisResult` and the master profile. The desire score is a
ranking signal, not a tailoring input.

One future consideration: the cover letter could reference *why* the
candidate wants the role (desire signals), not just *why* they're
qualified. That would use the desire profile's `excited_domains` and
`impact_positive` signals to inject genuine enthusiasm into the letter.
But that's a tailoring enhancement, not a ranking one.

---

## State field additions

```python
@dataclass
class JobAgentState:
    # ... existing fields ...

    # NEW: desire scores per listing (keyed by dedup_key)
    desire_scores: dict[str, float] = field(default_factory=dict)
```

The `JobListing` model may also want a `desire_score` field parallel to
the existing implicit fit score, for persistence and debugging.

---

## Configuration additions

```python
# core/config.py additions
candidate_desires_path: str = Field(
    default=str(_PROJECT_ROOT / "pipelines" / "job_agent" / "context" / "candidate_desires.yaml"),
    description="Path to the candidate desires YAML.",
)
ranking_desire_weight: float = Field(
    default=0.65, ge=0.0, le=1.0,
    description="Weight of desire score in combined ranking (qual = 1 - this).",
)
ranking_min_desire_score: float = Field(
    default=0.15, ge=0.0, le=1.0,
    description="Hard floor for desire score — listings below this are always skipped.",
)
ranking_min_qualification_score: float = Field(
    default=0.15, ge=0.0, le=1.0,
    description="Hard floor for qualification stretch — listings below are always skipped.",
)
```

---

## Decision matrix

| Phase | LLM cost/run | Handles known companies | Handles unknown companies | Catches semantic nuance | Personalizes over time |
|-------|-------------|------------------------|--------------------------|------------------------|----------------------|
| 0 (deterministic) | $0 | yes (tier list) | partially (domain keywords) | no | no |
| 1 (company intel) | ~$0.20/month | yes | yes | no | no |
| 2 (LLM judge) | ~$0.10/run | yes | yes | yes | no |
| 3 (elicitation) | ~$0.50/update | yes | yes | partially (better YAML) | no |
| 4 (pairwise) | ~$0.15/run | yes | yes | yes | no |
| 5 (trained) | ~$0.01/run | yes | yes | yes | yes |

---

## What to build now vs. later

**Now (Phase 0):**
1. Create `candidate_desires.yaml` with the tier lists, domain signals,
   and role/impact/title signals from the specification above.
2. Add a `CandidateDesires` Pydantic model and loader (parallel to
   `ResumeMasterProfile`).
3. Rewrite `ranking_node` to compute both desire and qualification
   scores, gate on both, and rank by the combined signal.
4. Add lightweight desire signals to the prefilter.
5. Add config knobs for desire weight and floor thresholds.

**Later (when Phase 0's limits show up):**
- Phase 1 when unknown companies are a consistent ranking problem.
- Phase 2 when keyword matching misclassifies roles you'd want/skip.
- Phase 3 when you're tired of manually editing the YAML.

---

## Things to not do

- **Don't put desire scoring in the job analysis node.** Job analysis
  is objective: "what does this job require?" Desire scoring is
  subjective: "would this specific person want it?" Mixing them in one
  LLM call muddles both signals and makes the analysis non-reusable.

- **Don't make desire scoring depend on the full description at
  prefilter time.** The prefilter runs before bulk extraction — it only
  has title, company, and location. The full desire score runs in the
  ranking node after job analysis, when the full description and
  structured analysis are available.

- **Don't hardcode the desire weights in Python.** They should live in
  `candidate_desires.yaml` so they're tunable without code changes.
  The config knobs in `core/config.py` are for pipeline-level controls
  (desire vs. qualification weight, floor thresholds); the per-dimension
  weights are profile-level.

- **Don't use Sonnet for per-listing desire scoring.** Haiku is more
  than good enough for classification tasks. Save Sonnet budget for
  cover letters and the one-time conversational elicitation.

- **Don't build the feedback loop (Phase 5) before you have data.**
  Same warning as the old architecture doc. 10 labelled examples will
  overfit. Wait for 50-100.

- **Don't remove the qualification scorer.** It's still the gate that
  prevents wasting tailoring budget on roles where there's genuinely no
  narrative to build. The desire scorer identifies what Sam wants; the
  qualification scorer identifies what's feasible. Both are load-bearing.
