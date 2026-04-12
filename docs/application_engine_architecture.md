# Application engine — architectural design document

## What this is

This document specifies the complete architecture for an automated job
application engine: a new pipeline stage that takes a job listing with
tailored resume and cover letter, navigates to the application page in a
real browser, fills out every field, uploads documents, answers screening
questions, and submits the application. It handles the full spectrum of
application experiences — from simple single-page Greenhouse forms to
multi-step Workday wizards with account creation, custom dropdowns, and
cross-domain redirects.

The engine sits in the pipeline immediately after resume and cover letter
tailoring:

```
... → ranking → tailoring → cover_letter_tailoring
                                      ↓
                              APPLICATION ENGINE  ← this document
                                      ↓
                              tracking → notification → END
```

At the point the application engine receives a listing, all of this
exists on `state`:

- `listing.url` — the original job listing URL
- `listing.tailored_resume_path` — path to the `.docx` resume on disk
- `listing.tailored_cover_letter_path` — path to the `.docx` cover letter
- `state.job_analyses[listing.dedup_key]` — structured `JobAnalysisResult`
- The candidate profile YAML (loaded from disk via `load_master_profile`)

The engine's job is to convert all of that into a completed application.

---

## Design principles

1. **API-first, browser-fallback.** Greenhouse and Lever have documented
   public APIs for application submission. Use them. Don't open a browser
   when an HTTP POST does the same thing faster, cheaper, and more
   reliably. Browser automation is reserved for platforms that force it
   (Workday, iCIMS, LinkedIn Easy Apply, Taleo, unknown employer sites).

2. **Deterministic answers don't touch the LLM.** The candidate's name,
   email, phone number, LinkedIn URL, GitHub URL, work authorization
   status, and demographic responses are all known constants from the
   candidate profile. Routing these through an LLM call wastes tokens and
   adds latency. A deterministic field mapper handles them; the LLM is
   reserved for open-ended questions the mapper can't answer.

3. **Never auto-submit without human approval.** The engine fills
   everything, then pauses. A human reviews the populated form (via
   screenshot or browser preview) and confirms. Only then does the engine
   click Submit. This is non-negotiable for the initial implementation.

4. **Fail loud, fail one.** A failure on one listing must not block or
   crash the others. Each application attempt is isolated. Failures
   produce structured error records with screenshots and page state for
   debugging.

5. **Reuse existing infrastructure.** The codebase already has
   `BrowserManager` (stealth, sessions), `BrowserActions` (human-behavior
   interaction), `PageObserver` (structured page state extraction),
   `WebAgentController` (LLM observe-decide-act loop), `HumanBehavior`
   (realistic mouse/typing/scrolling), `SessionStore`, CAPTCHA detection,
   rate limiting, and debug capture. The application engine composes these
   — it doesn't rebuild them.

---

## Existing infrastructure inventory

The codebase provides a production-grade browser automation stack. The
application engine builds on all of it:

| Module | What it provides | Used by application engine |
|--------|------------------|--------------------------|
| `core/browser/` `BrowserManager` | Managed Playwright lifecycle, stealth context, rate-limited navigation | Browser session for each application |
| `core/browser/stealth.py` | UA rotation, viewport randomization, JS anti-detection (webdriver, plugins, WebGL, canvas, audio, fonts, WebRTC) | Every browser-based application |
| `core/browser/human_behavior.py` | Reading pauses, Bezier mouse movement, typing cadence with typos, natural scrolling | All form interaction |
| `core/browser/actions.py` | Stealth-wrapped `fill()`, `click()`, `select_option()`, `upload_file()`, `check()`, `scroll()` | Direct field filling |
| `core/browser/observer.py` | `PageObserver` → `PageState` with indexed elements, form extraction, error detection, progress indicators | LLM agent observation |
| `core/browser/session.py` | `SessionStore` — persist/restore browser storage states per source | LinkedIn session reuse |
| `core/browser/captcha.py` | CAPTCHA detection (reCAPTCHA, hCaptcha, Cloudflare), strategy dispatch | Detect and handle CAPTCHAs during application |
| `core/browser/rate_limiter.py` | Adaptive per-domain rate limiting with backoff | Pace application submissions |
| `core/browser/debug_capture.py` | Screenshot + HTML + metadata capture on failure | Debugging failed applications |
| `core/web_agent/controller.py` | `WebAgentController` — LLM observe→decide→act loop with human-approval gates | Open-ended form navigation |
| `core/web_agent/protocol.py` | `AgentAction`, `AgentGoal`, `AgentResult`, `AgentStep` | Action vocabulary for LLM agent |
| `core/web_agent/context.py` | `AgentContextManager` — history compression, prompt assembly | Context window management in long form flows |
| `core/fetch/http_client.py` | `HttpFetcher` with retries and realistic headers | API-based submissions (Greenhouse, Lever) |
| `pipelines/job_agent/application/form_workflow.py` | Skeleton `fill_application()` — wires `WebAgentController` to a form goal | Starting point for browser-based flow |
| `pipelines/job_agent/application/qa_answerer.py` | `answer_form_field()` — LLM-based field answering with confidence scores | Answering open-ended questions |
| `pipelines/job_agent/application/node.py` | Skeleton `application_node()` | Starting point for pipeline node |

---

## Architecture overview

```
                    ┌───────────────────────────────────┐
                    │       application_node             │
                    │  (LangGraph node, orchestrator)    │
                    └──────────────┬────────────────────┘
                                   │
                    for each listing in state.tailored_listings:
                                   │
                    ┌──────────────▼────────────────────┐
                    │       ApplicationRouter            │
                    │  Detects ATS platform from URL,    │
                    │  selects submission strategy        │
                    └──────────────┬────────────────────┘
                                   │
              ┌────────────────────┼────────────────────┐
              │                    │                     │
    ┌─────────▼──────┐  ┌─────────▼──────┐  ┌──────────▼─────────┐
    │  API Submitter  │  │  Template Filler│  │  LLM Agent Filler  │
    │  (Greenhouse,   │  │  (known ATS     │  │  (unknown sites,   │
    │   Lever)        │  │   with known    │  │   complex wizards, │
    │                 │  │   field layout) │  │   custom layouts)  │
    └─────────┬──────┘  └─────────┬──────┘  └──────────┬─────────┘
              │                    │                     │
              └────────────────────┼────────────────────┘
                                   │
                    ┌──────────────▼────────────────────┐
                    │       ApplicationResult            │
                    │  status, screenshots, errors       │
                    └──────────────┬────────────────────┘
                                   │
                    ┌──────────────▼────────────────────┐
                    │  Human review gate (if submitted   │
                    │  in browser: screenshot + pause)   │
                    └───────────────────────────────────┘
```

Three submission strategies, tried in order of preference:

1. **API Submitter** — HTTP POST to ATS API. No browser. Fastest, most
   reliable, cheapest. Used for Greenhouse and Lever.

2. **Template Filler** — Browser automation with known selectors for a
   specific ATS platform. Deterministic field mapping handles
   standard fields; LLM handles only custom questions. Used for
   LinkedIn Easy Apply, Ashby, and potentially SmartRecruiters.

3. **LLM Agent Filler** — Full `WebAgentController` observe-decide-act
   loop. The LLM reads the page state and decides every action. Used
   for Workday, iCIMS, Taleo, and any unknown employer career site.
   This is the most expensive strategy but handles arbitrary form
   layouts.

---

## Component 1: Candidate application profile

Before anything can fill a form, we need a flat, machine-readable
representation of every answer the candidate could be asked for. This
is distinct from the resume master profile (which structures experience
bullets for tailoring). The application profile is about form fields.

**New file:** `pipelines/job_agent/context/candidate_application.yaml`

```yaml
schema_version: 1

# --- Personal information ---
personal:
  first_name: Samuel
  last_name: Kokomoor
  preferred_name: Sam
  email: kokomoor@mit.edu
  phone: "+18603895347"
  phone_formatted: "(860) 389-5347"
  linkedin_url: "https://www.linkedin.com/in/sam-kokomoor-b90629247/"
  github_url: "https://github.com/kokomoor"
  portfolio_url: ""
  website_url: ""

# --- Address ---
address:
  street: ""  # fill in
  city: Boston
  state: MA
  zip: ""     # fill in
  country: United States

# --- Work authorization ---
authorization:
  authorized_us: true
  require_sponsorship: false
  citizenship: "US Citizen"
  clearance: "DoD Final Secret (active)"

# --- Demographics (EEO voluntary) ---
# These are US federal EEOC categories. All are voluntary.
# Set to "decline" to select "Decline to self-identify" where offered.
demographics:
  gender: "Male"
  race_ethnicity: "White"
  veteran_status: "I am not a protected veteran"
  disability_status: "I do not have a disability"

# --- Education (structured for form filling) ---
education:
  highest_degree: "MBA"
  school: "MIT Sloan School of Management"
  graduation_year: "2026"
  gpa: ""
  field_of_study: "Business Administration"
  additional_degrees:
    - degree: "BS Computer Engineering"
      school: "University of Connecticut"
      year: "2021"
    - degree: "BS Electrical Engineering"
      school: "University of Connecticut"
      year: "2021"
    - degree: "BA Computer Science"
      school: "University of Connecticut"
      year: "2021"

# --- Common screening answers ---
screening:
  years_experience: "5"
  willing_to_relocate: true
  desired_salary: "200000"
  earliest_start_date: ""  # leave blank unless specific
  how_did_you_hear: "Online job search"
  referral_name: ""
  languages_spoken:
    - language: English
      proficiency: Native

# --- Source tracking ---
source:
  default: "Online job search"
  linkedin: "LinkedIn"
  greenhouse: "Company website"
  lever: "Company website"
  indeed: "Indeed"
```

**Pydantic model:** `CandidateApplicationProfile` in
`pipelines/job_agent/models/application.py`, loaded once per run and
cached the same way as `ResumeMasterProfile`.

---

## Component 2: Deterministic field mapper

The field mapper is a pure-Python function — no LLM, no network. Given
a field label, type, and options, it returns a value from the candidate
application profile. It handles 80-90% of form fields.

**New file:** `pipelines/job_agent/application/field_mapper.py`

```python
@dataclass(frozen=True)
class FieldMapping:
    """Result of deterministic field mapping."""
    value: str
    confidence: float  # 1.0 = certain, 0.0 = unmapped
    source: str        # which profile section

def map_field(
    label: str,
    field_type: str,
    options: list[str] | None,
    profile: CandidateApplicationProfile,
) -> FieldMapping:
    """Map a form field to a candidate profile value.

    Returns confidence=0.0 if no deterministic mapping exists,
    signaling the caller to fall back to the LLM QA answerer.
    """
```

The mapper works by normalized label matching:

```python
_FIELD_PATTERNS: dict[str, Callable[[CandidateApplicationProfile], str]] = {
    # Personal
    "first name": lambda p: p.personal.first_name,
    "last name": lambda p: p.personal.last_name,
    "full name": lambda p: f"{p.personal.first_name} {p.personal.last_name}",
    "name": lambda p: f"{p.personal.first_name} {p.personal.last_name}",
    "email": lambda p: p.personal.email,
    "phone": lambda p: p.personal.phone_formatted,
    "linkedin": lambda p: p.personal.linkedin_url,
    "github": lambda p: p.personal.github_url,
    "portfolio": lambda p: p.personal.portfolio_url,
    "website": lambda p: p.personal.website_url,

    # Address
    "city": lambda p: p.address.city,
    "state": lambda p: p.address.state,
    "zip": lambda p: p.address.zip,
    "country": lambda p: p.address.country,

    # Authorization
    "authorized to work": lambda p: "Yes" if p.authorization.authorized_us else "No",
    "work authorization": lambda p: "Yes" if p.authorization.authorized_us else "No",
    "sponsorship": lambda p: "No" if not p.authorization.require_sponsorship else "Yes",
    "require sponsorship": lambda p: "No" if not p.authorization.require_sponsorship else "Yes",
    "visa": lambda p: "No" if not p.authorization.require_sponsorship else "Yes",

    # Education
    "degree": lambda p: p.education.highest_degree,
    "school": lambda p: p.education.school,
    "university": lambda p: p.education.school,
    "graduation": lambda p: p.education.graduation_year,
    "gpa": lambda p: p.education.gpa,
    "field of study": lambda p: p.education.field_of_study,
    "major": lambda p: p.education.field_of_study,

    # Screening
    "years of experience": lambda p: p.screening.years_experience,
    "years experience": lambda p: p.screening.years_experience,
    "relocate": lambda p: "Yes" if p.screening.willing_to_relocate else "No",
    "salary": lambda p: p.screening.desired_salary,
    "compensation": lambda p: p.screening.desired_salary,
    "how did you hear": lambda p: p.source.default,
    "how did you find": lambda p: p.source.default,

    # Demographics
    "gender": lambda p: p.demographics.gender,
    "race": lambda p: p.demographics.race_ethnicity,
    "ethnicity": lambda p: p.demographics.race_ethnicity,
    "veteran": lambda p: p.demographics.veteran_status,
    "disability": lambda p: p.demographics.disability_status,
}
```

The matching algorithm:

1. Normalize the label: lowercase, strip punctuation, collapse whitespace.
2. Check for exact key match in `_FIELD_PATTERNS`.
3. Check for substring match (e.g., "Your phone number *" matches "phone").
4. For select/radio fields with options: if the deterministic value is
   in the options list (fuzzy match), return it. Otherwise return the
   closest match by edit distance.
5. If no match: return `confidence=0.0` → caller routes to LLM.

**Key detail:** For select fields where the options don't match the
expected values (e.g., EEO race options vary by employer), the mapper
uses fuzzy matching with a minimum threshold. If no option is close
enough, it looks for "Decline" / "Prefer not to say" / "I don't wish
to answer" and selects that as a safe fallback.

**Performance:** Zero LLM calls, microsecond execution. Handles first
name, last name, email, phone, LinkedIn, address, work authorization,
sponsorship, education, years of experience, salary expectations, and
all EEO fields deterministically.

---

## Component 3: LLM question answerer (enhanced)

The existing `qa_answerer.py` handles open-ended questions. It needs
enhancements for the application engine:

### Enhancement 1: Job-aware context

The current QA answerer receives only the candidate profile. For
application questions like "Why are you interested in this role?" or
"Describe your experience with distributed systems," the LLM needs
the job context too.

```python
async def answer_application_question(
    llm: LLMClient,
    *,
    field_label: str,
    field_type: str,
    field_options: list[str] | None = None,
    candidate_profile: str,
    job_context: str,       # NEW: job title, company, description excerpt
    cover_letter_text: str, # NEW: the tailored cover letter for tone consistency
    run_id: str = "",
) -> FormFieldAnswer:
```

The system prompt becomes:

```
You are answering a question on a job application form. The candidate
is applying for {job_title} at {company}.

Rules:
1. Answer in the candidate's voice — direct, confident, specific.
2. Reference specific experience from the profile, not generalities.
3. Keep answers concise — most form fields have character limits.
4. For "Why this role/company?" questions, draw from the cover letter
   themes (provided below) for consistency.
5. If the question is a yes/no or multiple choice, give only the answer.
6. If the profile genuinely doesn't contain relevant info, say so via
   low confidence — don't fabricate.
```

### Enhancement 2: Answer caching

Many applications ask the same questions ("Are you authorized to work
in the US?", "Do you require sponsorship?", "How did you hear about
us?"). Cache LLM answers by normalized question text so the same
question across different applications doesn't trigger repeated calls.

```python
# Cache key: normalized(field_label) + field_type
# Cache TTL: entire run (answers are profile-dependent, not listing-dependent
# for generic questions, but listing-dependent for "why this role" questions)
_QA_CACHE: dict[str, FormFieldAnswer] = {}

def _is_generic_question(label: str) -> bool:
    """Questions whose answer doesn't change per listing."""
    generic_patterns = [
        "authorized to work", "sponsorship", "visa", "how did you hear",
        "gender", "race", "veteran", "disability", "relocate",
        "years of experience", "salary", "start date",
    ]
    label_lower = label.lower()
    return any(p in label_lower for p in generic_patterns)
```

### Enhancement 3: Character limit awareness

Many form fields have maxlength constraints. The observer should extract
`maxlength` from the DOM. The QA answerer receives it and the prompt
instructs the LLM to stay within the limit:

```
Maximum character count for this field: {maxlength}
```

---

## Component 4: Application router

The router examines the listing URL (and optionally the page after
initial navigation) and selects the submission strategy.

**New file:** `pipelines/job_agent/application/router.py`

```python
class SubmissionStrategy(StrEnum):
    API_GREENHOUSE = "api_greenhouse"
    API_LEVER = "api_lever"
    TEMPLATE_LINKEDIN_EASY_APPLY = "template_linkedin_easy_apply"
    TEMPLATE_ASHBY = "template_ashby"
    AGENT_WORKDAY = "agent_workday"
    AGENT_GENERIC = "agent_generic"

@dataclass(frozen=True)
class RouteDecision:
    strategy: SubmissionStrategy
    application_url: str  # may differ from listing.url after redirect
    ats_platform: str     # detected ATS name or "unknown"
    requires_browser: bool
    requires_account: bool

def detect_ats_platform(url: str) -> str | None:
    """Detect ATS platform from URL pattern."""
    domain = urlparse(url).netloc.lower()
    path = urlparse(url).path.lower()

    if "greenhouse" in domain or "boards.greenhouse.io" in domain:
        return "greenhouse"
    if "lever" in domain or "jobs.lever.co" in domain:
        return "lever"
    if "myworkdayjobs" in domain or "myworkday" in domain:
        return "workday"
    if "icims" in domain:
        return "icims"
    if "taleo" in domain or "taleo.net" in domain:
        return "taleo"
    if "ashbyhq" in domain or "jobs.ashbyhq.com" in domain:
        return "ashby"
    if "smartrecruiters" in domain:
        return "smartrecruiters"
    if "bamboohr" in domain:
        return "bamboohr"
    if "linkedin.com" in domain and "/jobs/" in path:
        return "linkedin"
    return None

async def route_application(
    listing: JobListing,
    *,
    page: Page | None = None,
) -> RouteDecision:
    """Determine the best submission strategy for a listing.

    First checks the listing URL. If that's a job board page (LinkedIn,
    Indeed) rather than a direct application URL, navigate and follow
    the "Apply" link to discover the actual ATS.
    """
```

The routing logic:

```
1. Detect ATS from listing.url
   → greenhouse → API_GREENHOUSE
   → lever → API_LEVER
   → ashby → TEMPLATE_ASHBY
   → workday → AGENT_WORKDAY (requires_account=True)
   → icims → AGENT_GENERIC (requires_account=True)
   → taleo → AGENT_GENERIC (requires_account=True)
   → linkedin → TEMPLATE_LINKEDIN_EASY_APPLY

2. If listing.url is a job board page (LinkedIn, Indeed):
   → Navigate to the page in browser
   → Find and click "Apply" / "Easy Apply" button
   → Follow redirect chain
   → Re-detect ATS from final URL
   → Route based on detected ATS

3. If ATS is unknown:
   → Navigate to listing.url in browser
   → Find application link/button
   → Follow redirects
   → AGENT_GENERIC (the LLM figures it out)
```

### Redirect chain following

When a user clicks "Apply" on LinkedIn or Indeed, the browser typically
opens a new tab or redirects through tracking URLs to the employer's ATS.
The router must follow this chain:

```python
async def _follow_apply_link(page: Page) -> str:
    """Click the Apply button and follow redirects to the actual form."""
    # Listen for new page (LinkedIn opens new tab)
    async with page.context.expect_page() as new_page_info:
        apply_btn = await page.query_selector(
            "button:has-text('Apply'), "
            "a:has-text('Apply'), "
            "[data-control-name*='apply'], "
            ".jobs-apply-button"
        )
        if apply_btn:
            await apply_btn.click()

    new_page = await new_page_info.value
    await new_page.wait_for_load_state("domcontentloaded")
    return new_page.url
```

---

## Component 5: API submitters

### Greenhouse API submitter

Greenhouse has a documented, public Job Board API. No browser needed.

**Endpoint:** `POST https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}`

**New file:** `pipelines/job_agent/application/submitters/greenhouse_api.py`

```python
async def submit_greenhouse_application(
    listing: JobListing,
    profile: CandidateApplicationProfile,
    resume_path: Path,
    cover_letter_path: Path | None,
    *,
    run_id: str = "",
) -> ApplicationResult:
    """Submit application via Greenhouse Job Board API.

    Steps:
    1. GET /v1/boards/{slug}/jobs/{job_id} to fetch the question set.
    2. Map each question to a profile value (deterministic mapper first,
       LLM for custom questions).
    3. POST multipart form with personal info, resume, cover letter,
       and question answers.
    4. Return success/failure with response details.
    """
```

The Greenhouse API flow:

```
1. Extract board slug and job ID from listing URL:
   https://boards.greenhouse.io/{slug}/jobs/{job_id}

2. GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}
   Response includes:
   - questions[] with id, label, type (short_text, long_text, multi_select, etc.)
   - required flag per question
   - location[] for location-specific questions

3. For each question:
   a. Try field_mapper.map_field(question.label, question.type, question.options)
   b. If confidence < 0.8 → call answer_application_question()
   c. Build answer dict: {question_id: answer_value}

4. POST https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}
   Content-Type: multipart/form-data
   Fields:
   - first_name, last_name, email, phone
   - resume (file)
   - cover_letter (file, optional)
   - mapped_url_X for LinkedIn, GitHub, portfolio
   - question answers by ID

5. Parse response:
   - 200 → success
   - 422 → validation errors (return them for human review)
   - 429 → rate limited (backoff and retry)
```

**Cost:** Zero LLM calls for standard fields. One Haiku call per custom
question (typically 0-3 per application). Total: ~$0.001-0.003 per
application.

### Lever API submitter

Same pattern as Greenhouse. Lever's public Postings API accepts
application submissions.

**Endpoint:** `POST https://api.lever.co/v0/postings/{company}/{posting_id}`

**New file:** `pipelines/job_agent/application/submitters/lever_api.py`

```python
async def submit_lever_application(
    listing: JobListing,
    profile: CandidateApplicationProfile,
    resume_path: Path,
    cover_letter_path: Path | None,
    *,
    run_id: str = "",
) -> ApplicationResult:
    """Submit application via Lever Postings API."""
```

Lever API flow:

```
1. Extract company slug and posting UUID from listing URL:
   https://jobs.lever.co/{slug}/{uuid}

2. POST https://api.lever.co/v0/postings/{slug}/{uuid}
   Content-Type: multipart/form-data
   Fields:
   - name (full name)
   - email, phone, org (current company)
   - urls[LinkedIn], urls[GitHub], urls[Portfolio]
   - resume (file)
   - comments (cover letter text or "See attached cover letter")
   - Custom question answers in cards[] format

3. Parse response.
```

---

## Component 6: Template fillers

Template fillers use browser automation with known selectors for a
specific ATS. They are a middle ground between API submission (no
browser) and full LLM agent (every action decided by LLM). The template
knows the form layout and fills fields deterministically; only custom
questions trigger an LLM call.

### LinkedIn Easy Apply template

**New file:** `pipelines/job_agent/application/templates/linkedin_easy_apply.py`

LinkedIn Easy Apply is a modal wizard that appears on the job detail
page. It requires an authenticated LinkedIn session.

```python
async def fill_linkedin_easy_apply(
    page: Page,
    listing: JobListing,
    profile: CandidateApplicationProfile,
    resume_path: Path,
    llm: LLMClient,
    *,
    behavior: HumanBehavior,
    run_id: str = "",
) -> ApplicationResult:
    """Fill LinkedIn Easy Apply modal wizard.

    Flow:
    1. Navigate to job detail page (listing.url).
    2. Click "Easy Apply" button.
    3. Wait for modal to appear.
    4. For each wizard step:
       a. Extract form fields from modal.
       b. Map deterministic fields via field_mapper.
       c. For custom questions: use LLM QA answerer.
       d. Click "Next" / "Continue" / "Review" / "Submit".
    5. At the final step: pause for human review.
    """
```

The Easy Apply wizard step handler:

```python
async def _handle_easy_apply_step(
    page: Page,
    modal: ElementHandle,
    profile: CandidateApplicationProfile,
    resume_path: Path,
    llm: LLMClient,
    behavior: HumanBehavior,
) -> StepResult:
    """Process one step of the Easy Apply wizard."""

    # 1. Find all form fields in the modal
    fields = await modal.query_selector_all(
        "input:not([type=hidden]), select, textarea"
    )

    for field_el in fields:
        label = await _get_field_label(field_el, page)
        field_type = await field_el.get_attribute("type") or "text"
        tag = await field_el.evaluate("el => el.tagName.toLowerCase()")

        # 2. Try deterministic mapping
        if tag == "select":
            options = await _get_select_options(field_el)
            mapping = map_field(label, "select", options, profile)
        else:
            mapping = map_field(label, field_type, None, profile)

        # 3. If high confidence, fill deterministically
        if mapping.confidence >= 0.8:
            if tag == "select":
                await _select_option_fuzzy(field_el, mapping.value, behavior)
            elif field_type == "file":
                await field_el.set_input_files(str(resume_path))
            else:
                await behavior.human_click(page, field_el)
                await field_el.evaluate("el => el.value = ''")
                await behavior.type_with_cadence(field_el, mapping.value)
        # 4. Otherwise, ask the LLM
        else:
            answer = await answer_application_question(
                llm, field_label=label, field_type=field_type,
                candidate_profile=..., job_context=...,
            )
            # fill with answer.answer

    # 5. Click Next/Continue/Review
    next_btn = await modal.query_selector(
        "button[aria-label='Continue to next step'], "
        "button[aria-label='Review your application'], "
        "button[aria-label='Submit application'], "
        "button.artdeco-button--primary"
    )
    if next_btn:
        btn_text = (await next_btn.text_content() or "").strip().lower()
        if "submit" in btn_text:
            return StepResult(action="submit_ready")
        await behavior.human_click(page, next_btn)
        await behavior.between_actions_pause()

    return StepResult(action="next")
```

**Key selectors for LinkedIn Easy Apply:**

| Element | Selectors (with fallbacks) |
|---------|---------------------------|
| Easy Apply button | `button.jobs-apply-button`, `[data-control-name*="inapply"]` |
| Modal container | `.jobs-easy-apply-modal`, `div[data-test-modal-id="easy-apply-modal"]` |
| Next button | `button[aria-label="Continue to next step"]`, `.artdeco-button--primary` (inside modal) |
| Review button | `button[aria-label="Review your application"]` |
| Submit button | `button[aria-label="Submit application"]` |
| File upload | `input[type="file"]` inside modal |
| Close/dismiss | `button[aria-label="Dismiss"]` inside modal |

**Rate limiting:** No more than 25 Easy Apply submissions per day per
account. The engine tracks submission count per session and stops when
approaching the limit. The rate limiter enforces 60-120 second gaps
between applications to avoid triggering LinkedIn's velocity detection.

### Ashby template

Ashby has the cleanest form structure of any modern ATS. Single-page
form with standard HTML elements, no iframes, no shadow DOM.

**New file:** `pipelines/job_agent/application/templates/ashby.py`

Similar structure to LinkedIn Easy Apply but simpler — single page,
standard form elements, `input[name="firstName"]` / `input[name="email"]`
etc. Detection: `jobs.ashbyhq.com` in URL.

---

## Component 7: LLM agent filler

For unknown sites, Workday, iCIMS, Taleo, and any form that doesn't
have a template, the engine falls back to the full `WebAgentController`
observe-decide-act loop.

The existing `form_workflow.py` is the starting point, but it needs
significant enhancement.

**Rewritten file:** `pipelines/job_agent/application/agent_filler.py`

### Enhanced system prompt

The current system prompt is too generic. The enhanced version includes:

```python
def _build_agent_system_prompt(
    profile: CandidateApplicationProfile,
    listing: JobListing,
    analysis: JobAnalysisResult | None,
    ats_platform: str | None,
    resume_path: str,
    cover_letter_path: str | None,
) -> str:
    """Build a detailed system prompt for the form-filling agent."""
    parts = [
        "You are filling out a job application form for the candidate.",
        "",
        "## Candidate Information",
        f"Name: {profile.personal.first_name} {profile.personal.last_name}",
        f"Email: {profile.personal.email}",
        f"Phone: {profile.personal.phone_formatted}",
        f"LinkedIn: {profile.personal.linkedin_url}",
        f"GitHub: {profile.personal.github_url}",
        f"Location: {profile.address.city}, {profile.address.state}",
        f"Work authorized in US: Yes",
        f"Requires sponsorship: No",
        f"Clearance: {profile.authorization.clearance}",
        "",
        f"## Job Details",
        f"Title: {listing.title}",
        f"Company: {listing.company}",
    ]

    if analysis:
        parts.append(f"Themes: {', '.join(analysis.themes[:5])}")
        parts.append(f"Seniority: {analysis.seniority}")

    parts.extend([
        "",
        "## Files",
        f"Resume file path (for upload fields): {resume_path}",
    ])
    if cover_letter_path:
        parts.append(f"Cover letter file path: {cover_letter_path}")

    parts.extend([
        "",
        "## Rules",
        "1. Fill every field you can identify. Use the candidate info above.",
        "2. For file upload fields, use the upload action with the file path.",
        "3. For select/dropdown fields: click the dropdown trigger first, wait "
        "   for options to appear, then click the matching option. Many forms "
        "   use custom (non-native) dropdowns — do NOT assume select_option works.",
        "4. For EEO/demographic questions: Gender=Male, Race=White, "
        "   Veteran=Not a protected veteran, Disability=No disability. "
        "   If 'Decline' is offered, select that instead.",
        "5. Navigate through all form pages using Next/Continue buttons.",
        "6. If you encounter a login wall or account creation requirement, "
        "   use action='stuck' with details.",
        "7. If you encounter a CAPTCHA, use action='stuck'.",
        "8. When you reach the final Submit/Apply button, use action='done'. "
        "   Do NOT click Submit — the human will do that.",
        "9. If a field asks an open-ended question you cannot answer from "
        "   the info above, fill it with a brief, professional response "
        "   drawing on the job themes and candidate background.",
    ])

    if ats_platform:
        parts.extend(_ats_specific_hints(ats_platform))

    return "\n".join(parts)
```

### ATS-specific hints

When the router detects a known ATS, inject platform-specific guidance
into the agent prompt so it knows what to expect:

```python
def _ats_specific_hints(platform: str) -> list[str]:
    hints = {
        "workday": [
            "",
            "## Workday-specific guidance",
            "- This is a multi-step wizard. Click the Next button "
            "  (data-automation-id='bottom-navigation-next-button') to advance.",
            "- Dropdowns are CUSTOM — not native <select>. Click the dropdown, "
            "  wait for the listbox ([role='listbox']), then click the option.",
            "- File upload uses data-automation-id='file-upload-input-ref'.",
            "- Workday may try to parse your resume and pre-fill fields. "
            "  Verify pre-filled data matches the candidate info above.",
            "- If you see an account creation/sign-in page, report 'stuck'.",
        ],
        "icims": [
            "",
            "## iCIMS-specific guidance",
            "- The form may be inside an iframe. If you see very few form "
            "  fields, look for an iframe and try interacting within it.",
            "- Account creation may be required. If prompted, report 'stuck'.",
            "- Forms are multi-step. Look for Continue/Next/Save buttons.",
        ],
        "taleo": [
            "",
            "## Taleo-specific guidance",
            "- This is an older-style multi-step wizard. Pages load slowly.",
            "- Field IDs are dynamically generated. Use labels, not IDs.",
            "- Sessions expire after ~15 minutes. Work efficiently.",
            "- Account creation is almost always required. Report 'stuck' if prompted.",
        ],
    }
    return hints.get(platform, [])
```

### Enhanced PageObserver for application forms

The current `PageObserver` has gaps that matter for form filling:

1. **No iframe awareness.** Greenhouse and iCIMS embed forms in iframes.
   The observer only queries the top-level page.
2. **No `data-automation-id` / `data-testid` extraction.** These are the
   most stable selectors on Workday and other React-based ATS platforms.
3. **No `maxlength` extraction.** Needed for character-limit-aware
   LLM answers.
4. **No custom dropdown detection.** The observer classifies `<select>`
   as combobox but doesn't detect div-based dropdown triggers.

Enhancements to `PageObserver`:

```python
# In _element_to_info, add to the evaluate function:
const automationId = el.getAttribute('data-automation-id') || '';
const testId = el.getAttribute('data-testid') || el.getAttribute('data-test') || '';
const maxLength = el.getAttribute('maxlength') || '';
const ariaExpanded = el.getAttribute('aria-expanded');
const ariaHasPopup = el.getAttribute('aria-haspopup') || '';

// Better selector priority:
let selector = '';
if (automationId) selector = `[data-automation-id="${automationId}"]`;
else if (testId) selector = `[data-testid="${testId}"]`;
else if (id) selector = '#' + CSS.escape(id);
else if (name) selector = tag + '[name="' + name + '"]';
```

Add iframe form extraction:

```python
async def _extract_forms(self, page: Page, *, max_elements: int) -> list[FormInfo]:
    forms = []

    # Main page forms
    forms.extend(await self._extract_forms_from_frame(page, max_elements))

    # Iframe forms (check known ATS domains)
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        frame_url = frame.url or ""
        if any(ats in frame_url for ats in [
            "greenhouse", "lever", "icims", "ashby", "smartrecruiters"
        ]):
            forms.extend(
                await self._extract_forms_from_frame(frame, max_elements)
            )

    return forms
```

### Robust file upload

The current `_execute_upload` only handles `set_input_files()` on a
visible element. Add fallback strategies:

```python
async def _execute_upload(self, action: AgentAction) -> ActionResult:
    file_path = action.value
    if not file_path:
        return ActionResult(success=False, error="No file path for upload")

    # Strategy 1: Direct set_input_files on indexed element
    if action.element_index is not None:
        el = await self._observer.get_element_by_index(action.element_index)
        if el:
            try:
                await el.set_input_files(file_path)
                await self._actions._behavior.between_actions_pause()
                return ActionResult(success=True)
            except Exception:
                pass  # fall through to other strategies

    # Strategy 2: Find any file input on the page (even hidden)
    file_input = await self._page.query_selector("input[type='file']")
    if file_input:
        try:
            await file_input.set_input_files(file_path)
            await self._actions._behavior.between_actions_pause()
            return ActionResult(success=True)
        except Exception:
            pass

    # Strategy 3: Find file input in iframes
    for frame in self._page.frames:
        if frame == self._page.main_frame:
            continue
        file_input = await frame.query_selector("input[type='file']")
        if file_input:
            try:
                await file_input.set_input_files(file_path)
                await self._actions._behavior.between_actions_pause()
                return ActionResult(success=True)
            except Exception:
                pass

    # Strategy 4: Click upload trigger and use file chooser
    upload_triggers = [
        "text=Upload", "text=Choose file", "text=Browse",
        "text=Attach", "[class*='upload']", "[class*='drop']",
        "[data-automation-id='file-upload-input-ref']",
    ]
    for trigger in upload_triggers:
        try:
            el = await self._page.query_selector(trigger)
            if el and await el.is_visible():
                async with self._page.expect_file_chooser(timeout=5000) as fc:
                    await el.click()
                chooser = await fc.value
                await chooser.set_files(file_path)
                return ActionResult(success=True)
        except Exception:
            continue

    return ActionResult(success=False, error="All upload strategies failed")
```

### Custom dropdown handling

Add a new action type or make `select` smart enough to handle both
native and custom dropdowns:

```python
async def _execute_select(self, action: AgentAction) -> ActionResult:
    el = await self._observer.get_element_by_index(action.element_index)
    if el is None:
        return ActionResult(success=False, error="Element not found")

    tag = await el.evaluate("el => el.tagName.toLowerCase()")

    # Native <select>: use Playwright's select_option
    if tag == "select":
        try:
            await el.select_option(label=action.value)
            return ActionResult(success=True)
        except Exception:
            # Try by value
            try:
                await el.select_option(value=action.value)
                return ActionResult(success=True)
            except Exception as exc:
                return ActionResult(success=False, error=str(exc)[:300])

    # Custom dropdown: click trigger → wait for listbox → click option
    try:
        await self._actions._behavior.human_click(self._page, el)
        await self._page.wait_for_selector(
            "[role='listbox'], [role='option'], .select-menu, "
            "[data-automation-id*='selectWidget']",
            timeout=3000,
        )
        # Find and click the matching option
        options = await self._page.query_selector_all("[role='option']")
        target = (action.value or "").lower()
        for opt in options:
            text = (await opt.text_content() or "").strip()
            if target in text.lower() or text.lower() in target:
                await self._actions._behavior.human_click(self._page, opt)
                return ActionResult(success=True)
        return ActionResult(
            success=False,
            error=f"Option '{action.value}' not found in dropdown",
        )
    except Exception as exc:
        return ActionResult(success=False, error=str(exc)[:300])
```

---

## Component 8: Application node (orchestrator)

The `application_node` is the LangGraph node that ties everything
together.

**Rewritten file:** `pipelines/job_agent/application/node.py`

```python
async def application_node(
    state: JobAgentState,
    *,
    llm_client: LLMClient | None = None,
) -> JobAgentState:
    """Submit applications for all tailored listings.

    For each listing in state.tailored_listings:
    1. Load candidate application profile.
    2. Route to the appropriate submission strategy.
    3. Execute the submission (API, template, or LLM agent).
    4. Record the result.
    5. Update listing status.
    """
    state.phase = PipelinePhase.APPLICATION
    settings = get_settings()

    if state.dry_run:
        logger.info("application.skip_dry_run")
        return state

    if not state.tailored_listings:
        logger.info("application.no_listings")
        return state

    profile = load_application_profile(
        Path(settings.candidate_application_profile_path)
    )
    master_profile = load_master_profile(
        Path(settings.resume_master_profile_path)
    )

    results: list[ApplicationAttempt] = []

    for listing in state.tailored_listings:
        if listing.status == ApplicationStatus.ERRORED:
            continue  # skip listings that failed earlier

        try:
            result = await _apply_to_listing(
                listing=listing,
                profile=profile,
                master_profile=master_profile,
                analysis=state.job_analyses.get(listing.dedup_key),
                llm_client=llm_client,
                settings=settings,
                run_id=state.run_id,
            )
            results.append(result)

            if result.status == "submitted":
                listing.status = ApplicationStatus.APPLIED
                listing.applied_at = datetime.now(tz=UTC)
            elif result.status == "awaiting_review":
                listing.status = ApplicationStatus.PENDING_REVIEW
            elif result.status == "stuck":
                listing.status = ApplicationStatus.ERRORED
                state.errors.append({
                    "node": "application",
                    "dedup_key": listing.dedup_key,
                    "message": result.summary[:500],
                })
        except Exception as exc:
            listing.status = ApplicationStatus.ERRORED
            state.errors.append({
                "node": "application",
                "dedup_key": listing.dedup_key,
                "message": str(exc)[:500],
            })
            results.append(ApplicationAttempt(
                dedup_key=listing.dedup_key,
                status="error",
                summary=str(exc)[:500],
            ))

    state.application_results = results
    logger.info(
        "application.complete",
        total=len(state.tailored_listings),
        submitted=sum(1 for r in results if r.status == "submitted"),
        pending_review=sum(1 for r in results if r.status == "awaiting_review"),
        stuck=sum(1 for r in results if r.status == "stuck"),
        errors=sum(1 for r in results if r.status == "error"),
    )
    return state
```

The per-listing handler:

```python
async def _apply_to_listing(
    listing: JobListing,
    profile: CandidateApplicationProfile,
    master_profile: ResumeMasterProfile,
    analysis: JobAnalysisResult | None,
    llm_client: LLMClient | None,
    settings: Settings,
    run_id: str,
) -> ApplicationAttempt:
    """Attempt to submit one application."""

    # Route
    route = await route_application(listing)

    # API strategies (no browser)
    if route.strategy == SubmissionStrategy.API_GREENHOUSE:
        return await submit_greenhouse_application(
            listing, profile,
            Path(listing.tailored_resume_path),
            Path(listing.tailored_cover_letter_path) if listing.tailored_cover_letter_path else None,
            run_id=run_id,
        )

    if route.strategy == SubmissionStrategy.API_LEVER:
        return await submit_lever_application(...)

    # Browser strategies
    async with BrowserManager(
        headless=settings.browser_headless,
        storage_state=_load_session(route.ats_platform, settings),
    ) as browser:
        page = await browser.new_page()

        if route.strategy == SubmissionStrategy.TEMPLATE_LINKEDIN_EASY_APPLY:
            return await fill_linkedin_easy_apply(
                page, listing, profile,
                Path(listing.tailored_resume_path),
                llm_client,
                behavior=HumanBehavior(),
                run_id=run_id,
            )

        # LLM agent filler (Workday, iCIMS, unknown)
        return await fill_with_agent(
            page, listing, profile, analysis,
            resume_path=Path(listing.tailored_resume_path),
            cover_letter_path=...,
            llm_client=llm_client,
            ats_platform=route.ats_platform,
            run_id=run_id,
        )
```

---

## Component 9: State and model additions

### New state fields

```python
@dataclass
class JobAgentState:
    # ... existing fields ...

    # NEW: application results per listing
    application_results: list[ApplicationAttempt] = field(default_factory=list)
```

### New models

**New file:** `pipelines/job_agent/models/application.py`

```python
class ApplicationAttempt(BaseModel):
    """Result of one application submission attempt."""
    dedup_key: str
    status: Literal["submitted", "awaiting_review", "stuck", "error"]
    strategy: str = ""      # api_greenhouse, template_linkedin, agent_workday, etc.
    summary: str = ""
    steps_taken: int = 0
    screenshot_path: str = "" # path to final-state screenshot
    errors: list[str] = Field(default_factory=list)
    fields_filled: int = 0
    llm_calls_made: int = 0


class CandidateApplicationProfile(BaseModel):
    """Flat representation of candidate data for form filling."""
    schema_version: int = 1

    personal: PersonalInfo
    address: AddressInfo
    authorization: AuthorizationInfo
    demographics: DemographicInfo
    education: EducationInfo
    screening: ScreeningInfo
    source: SourceTracking
```

### New config fields

```python
# core/config.py additions
candidate_application_profile_path: str = Field(
    default=str(_PROJECT_ROOT / "pipelines" / "job_agent" / "context" / "candidate_application.yaml"),
    description="Path to the candidate application profile YAML.",
)
application_max_per_run: int = Field(
    default=5, ge=0,
    description="Max applications to submit per pipeline run. 0 = no cap.",
)
application_require_human_review: bool = Field(
    default=True,
    description="Pause before final submission for human review.",
)
application_linkedin_daily_cap: int = Field(
    default=25, ge=1,
    description="Max LinkedIn Easy Apply submissions per day per account.",
)
application_min_delay_seconds: float = Field(
    default=60.0, ge=10.0,
    description="Minimum seconds between application submissions.",
)
```

### Graph update

```python
# In build_graph():
graph.add_node(
    "application",
    cast(Any, _llm_node_wrapper(application_node, llm_client=llm_client)),
)
# Insert between cover_letter_tailoring and tracking:
graph.add_edge("cover_letter_tailoring", "application")
graph.add_edge("application", "tracking")
# Remove the old direct edge:
# graph.add_edge("cover_letter_tailoring", "tracking")  # DELETE
```

---

## File layout

```
pipelines/job_agent/application/
    __init__.py
    node.py                       # LangGraph node (orchestrator)
    router.py                     # ATS detection + strategy selection
    field_mapper.py               # Deterministic field → value mapping
    qa_answerer.py                # Enhanced LLM question answerer (existing, upgraded)
    form_workflow.py              # Generic browser form workflow (existing, upgraded)
    agent_filler.py               # Full LLM agent filler for unknown forms
    models.py                     # ApplicationAttempt, CandidateApplicationProfile
    submitters/
        __init__.py
        greenhouse_api.py         # Greenhouse Job Board API submission
        lever_api.py              # Lever Postings API submission
    templates/
        __init__.py
        linkedin_easy_apply.py    # LinkedIn Easy Apply modal wizard
        ashby.py                  # Ashby single-page form
```

---

## Implementation plan — build order

### Phase 1: Foundation (build first)

**Goal:** One working API submission path. No browser.

1. Create `candidate_application.yaml` with Sam's data.
2. Create `CandidateApplicationProfile` Pydantic model and loader.
3. Implement `field_mapper.py` with the deterministic mapping table.
4. Implement `greenhouse_api.py` — fetch questions, map answers, POST.
5. Implement `router.py` with Greenhouse detection.
6. Wire `application_node` to call the Greenhouse submitter.
7. Update `graph.py` to insert the application node.
8. Test end-to-end with a real Greenhouse listing in dry-run mode
   (fetch questions, map answers, log what would be submitted, don't
   actually POST).

**Verification:** Run the pipeline with a Greenhouse listing. The node
should log all field mappings, identify any custom questions that need
LLM answers, and produce a complete submission payload without actually
submitting.

### Phase 2: Second API path + LLM questions

**Goal:** Two API paths, LLM handles custom questions.

1. Implement `lever_api.py`.
2. Enhance `qa_answerer.py` with job context, character limits, caching.
3. Wire the LLM answerer into both API submitters for custom questions.
4. Test with a Lever listing that has custom questions.

**Verification:** Custom questions like "Why are you interested in this
role?" get coherent, job-specific LLM answers. Generic questions like
"Are you authorized to work?" are answered deterministically without
LLM calls.

### Phase 3: LinkedIn Easy Apply

**Goal:** First browser-based submission path.

1. Implement `linkedin_easy_apply.py` template.
2. Add LinkedIn session reuse (already exists in discovery).
3. Add Easy Apply detection to the router.
4. Implement rate limiting (daily cap, inter-application delay).
5. Add human-review gate: take screenshot of filled form, pause.
6. Test with a real LinkedIn Easy Apply listing.

**Verification:** The engine logs into LinkedIn using the existing
session, opens a job page, fills the Easy Apply wizard, and pauses at
the submit step with a screenshot for review.

### Phase 4: LLM agent for unknown forms

**Goal:** The fallback path that handles anything.

1. Enhance `PageObserver` with iframe awareness, `data-automation-id`,
   `maxlength`, custom dropdown detection.
2. Enhance `WebAgentController` with robust upload, custom dropdown
   handling, ATS-specific hints.
3. Implement `agent_filler.py` with the enhanced system prompt.
4. Add redirect-chain following to the router.
5. Test with a company career site that redirects to an unknown ATS.

**Verification:** The LLM agent navigates an unfamiliar form, fills
fields using the candidate info from the system prompt, uploads the
resume, and pauses before submit.

### Phase 5: Workday specialist

**Goal:** Handle the hardest common ATS.

1. Add Workday-specific hints to the agent prompt.
2. Handle custom dropdown interaction pattern (click → listbox → option).
3. Handle the multi-step wizard navigation.
4. Handle resume parsing verification (Workday pre-fills from uploaded
   resume — the agent verifies pre-filled data is correct).
5. Account creation detection: if Workday requires an account, report
   `stuck` and log the details for manual handling.

**Verification:** The engine navigates a Workday application through
multiple wizard steps, handles custom dropdowns, uploads resume, and
reaches the review page.

### Phase 6: Hardening

**Goal:** Production-grade reliability.

1. Debug capture on every failure (screenshot + page HTML + metadata).
2. Retry logic: if a browser strategy fails, log the failure, capture
   debug artifacts, and try once more with a fresh browser context.
3. Application deduplication: don't re-apply to the same listing. Track
   submitted applications in the database.
4. Notification integration: email/log summary of applications submitted
   vs. failed vs. awaiting review.
5. Metrics: track success rates by ATS platform to identify which
   templates need improvement.

---

## Cost analysis

| Strategy | LLM cost per application | Browser cost | Speed |
|----------|-------------------------|-------------|-------|
| API (Greenhouse/Lever) | $0.001-0.003 (0-3 custom Q's via Haiku) | None | ~2 seconds |
| Template (LinkedIn Easy Apply) | $0.005-0.01 (1-5 custom Q's via Haiku) | One Playwright page | ~60-90 seconds |
| Template (Ashby) | $0.003-0.005 | One Playwright page | ~30-45 seconds |
| LLM Agent (generic) | $0.05-0.15 (20-50 Haiku observe-act cycles) | One Playwright page | ~3-8 minutes |
| LLM Agent (Workday) | $0.10-0.25 (30-80 cycles for multi-step wizard) | One Playwright page | ~5-15 minutes |

For 5 applications per run (the default `tailoring_max_listings`), worst
case is 5 Workday applications at $1.25 total. Typical case with a mix
of Greenhouse + LinkedIn + one unknown site: ~$0.20-0.50 per run.

---

## Things to not do

- **Don't auto-submit without human review in Phase 1-3.** The
  `application_require_human_review` flag defaults to True. Let the
  engine prove itself on 50+ reviewed applications before considering
  auto-submit for high-confidence API paths.

- **Don't build Workday account creation.** Most Workday sites require
  creating an account. Automating account creation triggers email
  verification, potentially CAPTCHAs, and is the kind of behavior most
  likely to get flagged. For now, report `stuck` on account-wall
  Workday sites. The user can pre-create accounts manually for
  high-priority companies.

- **Don't use Sonnet for form-filling observe-act cycles.** Haiku is
  sufficient for "what field is this, what should I type." Each cycle
  is a simple classification task, not a creative generation task.
  Reserve Sonnet for the QA answerer on open-ended questions where
  answer quality directly affects interview chances.

- **Don't hardcode selectors without fallbacks.** Even the template
  fillers should have 2-3 fallback selectors per element. ATS
  platforms update their UIs regularly. The LinkedIn Easy Apply modal
  has changed class names three times in the last year.

- **Don't submit more than 25 LinkedIn Easy Apply per day.** LinkedIn
  tracks application velocity and will restrict the account. The daily
  cap is a hard safety rail, not a suggestion.

- **Don't build a visual/screenshot-based agent yet.** The DOM-based
  `PageObserver` is 10-50x cheaper per step than sending screenshots
  to a vision model. It handles 95%+ of form fields. Add vision as a
  targeted fallback for shadow DOM / canvas-based forms only after
  Phase 4 proves the DOM approach's limits.

- **Don't skip the deterministic field mapper.** Routing every field
  through the LLM (even "First Name") wastes $0.001 per field and
  adds 1-2 seconds of latency. The mapper handles 80-90% of fields
  at zero cost in microseconds.

- **Don't try to handle every ATS in Phase 1.** Greenhouse and Lever
  cover ~30-40% of applications at top-tier tech companies (the
  candidate's target list). LinkedIn Easy Apply covers another 20-30%.
  That's 50-70% with just three strategies. The LLM agent fallback
  catches the rest imperfectly, and that's fine — it improves over
  time.

---

## Success criteria

The application engine is working when:

1. A Greenhouse listing goes from "tailored" to "submitted" with zero
   human intervention beyond the review gate.
2. A LinkedIn Easy Apply listing gets fully populated in the browser,
   with a screenshot ready for human review, in under 90 seconds.
3. An unknown employer site gets filled as far as the LLM agent can
   manage, with clear `stuck` reporting when it hits a wall (account
   creation, CAPTCHA, incomprehensible form).
4. Failed applications produce debug captures (screenshot + HTML +
   metadata) that make it possible to diagnose and fix the issue.
5. The whole thing runs within the existing pipeline — `python -m
   pipelines.job_agent` goes from discovery to application submission
   in one invocation.

---

## Implementation prompts — sequential build plan for a coding model

Everything below is a **ready-to-execute prompt sequence** for a flagship
coding model (Claude Code Opus, Codex, etc.). Each prompt is
self-contained: it names the files to read, the files to write, the
invariants to respect, and the CI gates that must stay green. A model
with internet access and this repository (including every `AGENTS.md`)
should be able to execute these in order and land the complete
application engine.

### How to use these prompts

1. Feed the prompts **in numbered order**. Each one builds on the
   previous. Do not skip ahead — later prompts depend on types,
   fixtures, and config flags introduced earlier.
2. Between prompts, run the full CI gate locally:

   ```bash
   ruff check core/ pipelines/
   ruff format --check core/ pipelines/
   mypy core/ pipelines/ --ignore-missing-imports
   pytest --cov=core --cov=pipelines --cov-report=term-missing -x
   ```

   If any check fails, fix it before continuing. **Do not** skip
   hooks or silence mypy with `# type: ignore` unless the type error
   is genuinely in third-party code; `warn_unused_ignores = true` is
   set in `pyproject.toml` and will punish stale ignores.
3. Each prompt should land as **one focused commit** (or PR, if
   working on a branch). The commit message should name the component
   and reference the prompt number, e.g. `feat(application): prompt 03
   — greenhouse API submitter`.
4. Invariants that hold for **every** prompt (do not violate, do not
   mention being reminded of them):
   - Never auto-submit applications without `application_require_human_review=False`
     being an explicit, logged decision. Defaults must pause at the
     submit button.
   - Never hit a real employer endpoint from a unit test. All network
     and browser interactions in tests must be mocked or use the
     fixture pages shipped under `pipelines/job_agent/tests/fixtures/`.
   - Never store API keys, session cookies, or PII in source control.
     Session stores live under `data/sessions/` and are gitignored.
   - Never rebuild infrastructure the repo already provides. Compose
     `BrowserManager`, `BrowserActions`, `PageObserver`,
     `WebAgentController`, `HumanBehavior`, `SessionStore`,
     `HttpFetcher`, `DedupEngine`, `structured_complete`, and
     `AnthropicClient` — do not fork them.
   - Never widen public types to `Any` to escape mypy. Add a precise
     type or a targeted `cast`.
   - All new user-facing fields must go through `core.config.Settings`
     (prefixed `KP_`), not module-level constants.
5. Before any prompt touches a file in `core/`, re-read
   `core/AGENTS.md` and the relevant subdirectory's `AGENTS.md`
   (`core/browser/AGENTS.md`, `core/web_agent/AGENTS.md`). Before
   touching `pipelines/job_agent/`, re-read
   `pipelines/job_agent/AGENTS.md`. Those files encode conventions the
   architecture doc assumes but does not restate.

---

### Prompt 00 — Orient and establish a baseline

**Goal:** Read the architecture doc, understand the existing
infrastructure, and prove the baseline CI is green before touching
anything.

**Read first:**
- `docs/application_engine_architecture.md` (this document, in full)
- `core/AGENTS.md`, `core/browser/AGENTS.md`, `core/web_agent/AGENTS.md`,
  `pipelines/job_agent/AGENTS.md`
- `pipelines/job_agent/graph.py`, `pipelines/job_agent/state.py`,
  `pipelines/job_agent/models/__init__.py`
- `pipelines/job_agent/application/` — every file
- `core/web_agent/controller.py`, `core/web_agent/protocol.py`,
  `core/web_agent/context.py`
- `core/browser/observer.py`, `core/browser/actions.py`,
  `core/browser/human_behavior.py`, `core/browser/session.py`
- `core/llm/structured.py`, `core/llm/anthropic.py`, `core/llm/protocol.py`
- `pipelines/job_agent/context/candidate_profile.yaml`
- `core/config.py`
- `.github/workflows/ci.yml`
- `pyproject.toml` (especially `[tool.mypy]`, `[tool.ruff]`,
  `[tool.pytest.ini_options]`, and dependencies)

**What to produce:**
- A short summary (≤ 300 words) in your scratch output — do NOT
  commit it — covering: (a) which infrastructure components the
  application engine can compose, (b) which files today are skeleton
  stubs that must be rewritten vs. enhanced, (c) what the existing
  CI gate checks. This is a *pre-flight*, not a deliverable.
- Run the full CI gate (`ruff check`, `ruff format --check`, `mypy`,
  `pytest -x`) and confirm it is green against `main`. If it is not
  green, **stop and fix** before starting Prompt 01.

**Out of scope:** Any code changes. This prompt is pure orientation.

---

### Prompt 01 — Candidate application profile schema + data

**Goal:** Add a structured YAML profile covering every form field the
engine might need, with a Pydantic model and a cached loader.

**Read first:**
- `pipelines/job_agent/context/candidate_profile.yaml` (for style)
- `pipelines/job_agent/models/resume_tailoring.py` (for Pydantic model
  patterns already in use)
- "Component 1" section of this document (lines ~155-245)

**What to build:**
1. `pipelines/job_agent/context/candidate_application.yaml` — use the
   schema in Component 1 of this document verbatim as the starting
   point. Pre-populate all fields the user's data supports; leave
   unknowns as empty strings (never `null`), with a `# fill in`
   comment so a human can finish them. Commit this file; it is
   personal data and should NOT be gitignored (it is the canonical
   fixture for CI tests). Offer a sibling `candidate_application.example.yaml`
   with scrubbed placeholder values.
2. `pipelines/job_agent/models/application.py`:
   - `PersonalInfo`, `AddressInfo`, `AuthorizationInfo`,
     `DemographicInfo`, `EducationInfo`, `ScreeningInfo`,
     `SourceTracking` as Pydantic v2 `BaseModel` subclasses with full
     type annotations and `Field(description=...)` on every field.
   - `CandidateApplicationProfile` composing them with a
     `schema_version: int = 1`.
   - `load_application_profile(path: Path) -> CandidateApplicationProfile`
     using `functools.lru_cache` keyed on the resolved path string.
     Raise `FileNotFoundError` with a clear message if missing.
3. Export the public names from `pipelines/job_agent/models/__init__.py`.

**Tests (new file `pipelines/job_agent/tests/test_application_profile.py`):**
- Load `candidate_application.example.yaml` via `load_application_profile`.
- Assert every nested section parses.
- Assert caching: two loads on the same path return the same object
  (identity check).
- Assert an invalid YAML (missing `personal.email`) raises
  `ValidationError`.

**Acceptance:**
- CI green on the four-check suite.
- `load_application_profile` is importable from
  `pipelines.job_agent.models`.

**Out of scope:** field mapping, LLM prompting, any form filling.

---

### Prompt 02 — Deterministic field mapper

**Goal:** Pure-Python, zero-LLM mapping from form field labels to
profile values, with fuzzy option matching and safe fallbacks.

**Read first:**
- "Component 2" section of this document (lines ~249-354)
- The new `pipelines/job_agent/models/application.py` from Prompt 01

**What to build:**
1. `pipelines/job_agent/application/field_mapper.py`:
   - `FieldMapping` frozen dataclass: `value: str`, `confidence: float`,
     `source: str`.
   - Module-level `_FIELD_PATTERNS: dict[str, Callable[[CandidateApplicationProfile], str]]`
     populated per Component 2, with entries for every label token
     listed in the doc.
   - `_normalize_label(label: str) -> str` — lowercase, strip
     punctuation, collapse whitespace.
   - `_fuzzy_match_option(value: str, options: Sequence[str]) -> str | None`
     using `difflib.SequenceMatcher` with a min ratio of 0.6. If no
     option clears the threshold, check for decline-style options
     (`"Decline"`, `"Prefer not"`, `"I don't wish"`) and return one.
   - `map_field(label, field_type, options, profile) -> FieldMapping`:
     exact key match → substring match → option fuzzy match → fallback
     to `FieldMapping(value="", confidence=0.0, source="unmapped")`.
   - No LLM calls. No network. No async.
2. Keep the module under 300 lines. If it grows bigger, you're
   over-engineering — strip speculative features.

**Tests (`pipelines/job_agent/tests/test_application_field_mapper.py`):**
- Parametrized table of (label, expected profile source) for every
  deterministic entry in Component 2.
- Select-field case with exact option list match.
- Select-field case with fuzzy match (e.g. "Male/Man" → "Male").
- Select-field case with no good match → returns "Decline to
  self-identify" when available.
- Unknown label → `confidence=0.0`.
- Case insensitivity (`"FIRST NAME *"` → first name).

**Acceptance:**
- Every field listed in `_FIELD_PATTERNS` has at least one test.
- `ruff check` passes with zero errors; no `# noqa` added.
- Coverage for `field_mapper.py` ≥ 95 %.

**Out of scope:** LLM fallback path, API submission, browser interaction.

---

### Prompt 03 — Greenhouse API submitter

**Goal:** Ship the first end-to-end submission strategy, hitting the
public Greenhouse Job Board API directly (no browser).

**Read first:**
- "Component 5 — Greenhouse" (lines ~551-617)
- `core/fetch/http_client.py` for `HttpFetcher` retry/header conventions
- Current Greenhouse public API docs (via WebFetch; confirm endpoint
  paths, question-set schema, and multipart field names — the doc
  snippets are authoritative but version-pinned)

**What to build:**
1. `pipelines/job_agent/application/models.py` (new) — contains
   `ApplicationAttempt` per Component 9 of this document. Include
   `strategy` as a string, `screenshot_path` default `""`, and
   `fields_filled` / `llm_calls_made` counters.
2. `pipelines/job_agent/application/submitters/__init__.py`
3. `pipelines/job_agent/application/submitters/greenhouse_api.py`:
   - `_parse_greenhouse_url(url: str) -> tuple[str, str]` → `(board_slug, job_id)`.
     Accept both `boards.greenhouse.io/<slug>/jobs/<id>` and the
     embedded-iframe form. Raise `ValueError` on non-Greenhouse URLs.
   - `_fetch_question_set(fetcher, slug, job_id) -> GreenhouseJobSchema`.
     Parse `questions[]`, `required`, `type`, `label`, `options`.
   - `submit_greenhouse_application(...)` signature per Component 5:
     `(listing, profile, resume_path, cover_letter_path, *, llm, run_id)`.
     Uses `field_mapper.map_field` first; calls the LLM question
     answerer (from Prompt 07) only for `confidence < 0.8`. In this
     prompt, stub the LLM path with a `NotImplementedError` so the
     code path is wired but reserves the interface — Prompt 07 fills
     it in.
   - Multipart payload construction, 422 error parsing, 429 backoff
     respecting `Retry-After`.
   - **Dry-run mode**: if `dry_run=True`, do not POST — return an
     `ApplicationAttempt` with `status="awaiting_review"` and a
     `summary` containing the JSON payload that would have been sent.
     This is the default for CI and for the first live runs.
3. Update `pyproject.toml` if you need any extra dependency (you
   shouldn't; `httpx` and `pydantic` already cover this).

**Tests (`pipelines/job_agent/tests/test_greenhouse_submitter.py`):**
- `_parse_greenhouse_url` happy path + error path.
- Question-set parsing from a captured JSON fixture in
  `pipelines/job_agent/tests/fixtures/greenhouse_job_schema.json`
  (synthesize a plausible payload covering `short_text`, `long_text`,
  `multi_select`, `single_select`, and `attachment`).
- End-to-end dry-run: pass a fake `HttpFetcher` that returns the
  fixture, assert that every deterministic field maps to the profile
  value, and that custom questions raise `NotImplementedError` (to be
  removed in Prompt 07).
- 422 response translates into `ApplicationAttempt(status="error", ...)`.

**Acceptance:**
- Zero real network I/O in tests (patch the fetcher).
- CI gate green.
- `submit_greenhouse_application` has full type hints including
  return type.

**Out of scope:** LLM question answering, router, node wiring,
screenshots.

---

### Prompt 04 — Application router (URL → strategy)

**Goal:** A single function that, given a `JobListing`, decides which
submitter strategy to use, without opening a browser yet.

**Read first:**
- "Component 4 — Application router" (lines ~436-547)
- `pipelines/job_agent/models/__init__.py` for `JobListing`

**What to build:**
1. `pipelines/job_agent/application/router.py`:
   - `SubmissionStrategy` (`StrEnum`) and `RouteDecision` (frozen
     dataclass) per Component 4.
   - `detect_ats_platform(url: str) -> str | None` covering
     greenhouse, lever, workday, icims, taleo, ashby, smartrecruiters,
     bamboohr, linkedin. Handle `http`/`https`, subdomains, trailing
     slashes, and query strings.
   - `route_application(listing, *, page=None) -> RouteDecision`:
     URL-only inference in this prompt. Do not actually navigate the
     browser; the `page` parameter is accepted but unused. Return
     `SubmissionStrategy.AGENT_GENERIC` with `requires_browser=True`
     and `ats_platform="unknown"` for any URL where detection fails.
     Redirect-chain following is **Prompt 13**.
2. Update `pipelines/job_agent/application/__init__.py` to export
   the router publicly.

**Tests (`pipelines/job_agent/tests/test_application_router.py`):**
- One parametrized case per ATS with at least two URL variants.
- `route_application` returns the expected strategy for each case.
- Unknown domain returns `AGENT_GENERIC` with `ats_platform="unknown"`.
- Garbage URLs (empty string, missing scheme) raise or return
  `AGENT_GENERIC` — pick one and test it; do not silently return `None`.

**Acceptance:**
- `detect_ats_platform` has no regex catastrophic-backtracking risks
  — use `in` checks or anchored patterns.
- CI gate green.

**Out of scope:** Browser navigation, redirect following, account
detection.

---

### Prompt 05 — Application node (Greenhouse-only) + graph wiring + config + state

**Goal:** Stand up the LangGraph node that runs the Greenhouse path
end-to-end, wire it into the graph between `cover_letter_tailoring`
and `tracking`, and expose new config flags.

**Read first:**
- "Component 8" and "Component 9" (lines ~1092-1330)
- `pipelines/job_agent/state.py` — observe the dataclass style
- `pipelines/job_agent/graph.py` — note the existing `_llm_node_wrapper`
- `core/config.py` — note `Settings` conventions (`KP_` prefix, `Field`
  with `description`, validation)

**What to build:**
1. Extend `pipelines/job_agent/state.py`:
   - Add `application_results: list[ApplicationAttempt] = field(default_factory=list)`.
   - Import `ApplicationAttempt` under a `TYPE_CHECKING` guard to
     avoid circular imports; use `from __future__ import annotations`
     if not already present.
2. Extend `core/config.py` with every field in Component 9 (state
   additions), including `candidate_application_profile_path`,
   `application_max_per_run`, `application_require_human_review`,
   `application_linkedin_daily_cap`, `application_min_delay_seconds`.
   Each must have a `description=` and validated default.
3. **Rewrite** `pipelines/job_agent/application/node.py`:
   - Replace the skeleton with the orchestrator per Component 8.
   - Only the Greenhouse + generic-stuck path is implemented in this
     prompt. For non-Greenhouse strategies, return
     `ApplicationAttempt(status="stuck", summary=f"{strategy} not yet implemented")`
     and do NOT raise. Later prompts fill in each strategy.
   - Respect `state.dry_run`, `settings.application_max_per_run`,
     `settings.application_require_human_review`.
   - Between submissions, `await asyncio.sleep(settings.application_min_delay_seconds)`.
   - Use structured logging (`structlog`) with `run_id` bound.
4. Update `pipelines/job_agent/graph.py`:
   - Register `application` node (wrapped by `_llm_node_wrapper`
     because the eventual LLM paths need the client).
   - Replace `graph.add_edge("cover_letter_tailoring", "tracking")`
     with `cover_letter_tailoring → application → tracking`.
   - Apply the same change in `build_manual_graph`.
5. Update `pipelines/job_agent/nodes/tracking.py` to fold
   `state.application_results` into whatever summary it currently
   produces.

**Tests:**
- `pipelines/job_agent/tests/test_application_node.py`:
  - Dry-run with one Greenhouse listing produces exactly one
    `awaiting_review` attempt.
  - `application_max_per_run=1` with two listings processes only the
    first.
  - A listing whose status is already `ERRORED` is skipped.
  - Non-Greenhouse strategy returns `status="stuck"` without raising.
- Extend `pipelines/job_agent/tests/test_job_analysis.py` (or whichever
  covers graph wiring) to assert the new edge ordering.

**Acceptance:**
- `python -m pipelines.job_agent` boots without import errors (test
  via `python -c "from pipelines.job_agent.graph import build_graph; build_graph()"`).
- CI gate green.
- No `# type: ignore` added.

**Out of scope:** LLM answering, templates, browser strategies.

---

### Prompt 06 — Lever API submitter

**Goal:** Second API strategy, same pattern as Greenhouse.

**Read first:**
- "Component 5 — Lever" (lines ~619-657)
- `pipelines/job_agent/application/submitters/greenhouse_api.py` from
  Prompt 03 — match its shape and error-handling style.
- Current Lever Postings API docs (via WebFetch) to confirm multipart
  field names and `cards[]` structure.

**What to build:**
1. `pipelines/job_agent/application/submitters/lever_api.py` mirroring
   the Greenhouse submitter. Reuse `_build_multipart_headers` or
   whatever helper you extracted in Prompt 03 — if you didn't extract
   one, do it now in a `pipelines/job_agent/application/submitters/_common.py`.
2. `_parse_lever_url(url: str) -> tuple[str, str]` → `(slug, posting_uuid)`.
3. `submit_lever_application(...)` — same signature as the Greenhouse
   equivalent, same dry-run contract.
4. Update the router to dispatch `lever` → `API_LEVER`.
5. Update `node.py` to call the Lever submitter when
   `strategy == API_LEVER`.

**Tests:**
- `pipelines/job_agent/tests/test_lever_submitter.py`:
  - URL parsing happy path + error path.
  - Dry-run against a fixture `lever_posting_schema.json`.
  - Node-level test: listing with Lever URL → dispatched to Lever path.

**Acceptance:**
- Zero code duplication between the two submitters beyond what is
  structurally unavoidable. Shared helpers live in `_common.py`.
- CI gate green.

**Out of scope:** LLM custom-question answers (still stubbed).

---

### Prompt 07 — Enhanced LLM question answerer

**Goal:** Fill the NotImplementedError gap the API submitters left
for custom questions. Add job context, cover-letter voice matching,
character-limit awareness, and a run-scoped cache.

**Read first:**
- "Component 3" (lines ~356-434)
- Existing `pipelines/job_agent/application/qa_answerer.py`
- `core/llm/structured.py` for `structured_complete` usage
- `pipelines/job_agent/application/prompts/qa_system.md`

**What to build:**
1. **Rewrite** `pipelines/job_agent/application/qa_answerer.py`:
   - Keep the existing `FormFieldAnswer` pydantic model but add
     `used_cache: bool = False`.
   - New function `answer_application_question(...)` per Component 3
     with `job_context`, `cover_letter_text`, `maxlength`, `options`.
   - Backwards-compat: keep `answer_form_field` as a thin wrapper that
     calls the new function with empty job context (so existing
     imports don't break).
   - Move the system prompt into
     `pipelines/job_agent/application/prompts/qa_system.md` — do not
     hard-code it in Python. The file is read once and cached.
   - `_QA_CACHE: dict[str, FormFieldAnswer]` keyed on a tuple of
     `(normalized_label, field_type, maxlength, options_hash)`.
     Exposed via `clear_qa_cache()` for tests.
   - `_is_generic_question(label: str) -> bool` per Component 3.
     Generic questions are always cached; job-specific ones
     (containing the job title or company) bypass cache.
2. Update `greenhouse_api.py` and `lever_api.py` to call
   `answer_application_question` instead of raising. Increment
   `llm_calls_made` on the `ApplicationAttempt`.
3. The LLM client uses **Haiku** for this flow, not Sonnet. Pass
   `model=settings.anthropic_haiku_model` explicitly. Add that
   setting to `core/config.py` if it does not already exist.

**Tests (`pipelines/job_agent/tests/test_application_qa_answerer.py`):**
- Use `MockLLMClient` from `core/testing/`.
- Generic question ("Are you authorized to work?") hits the LLM once,
  then is served from cache on repeat with `used_cache=True`.
- Character-limit field appends the `maxlength` constraint to the
  prompt (assert via captured prompt).
- Cover-letter tone consistency: feed a known cover letter, assert
  the system prompt contains its themes.
- Low-confidence answer (`confidence < 0.5`) is still returned, not
  raised — the router decides what to do.

**Acceptance:**
- Greenhouse and Lever submitter tests from Prompts 03 and 06 now
  succeed with custom questions instead of `NotImplementedError`.
- CI gate green.

**Out of scope:** Browser/template logic.

---

### Prompt 08 — LinkedIn Easy Apply template

**Goal:** First browser-based submission strategy. Uses the existing
LinkedIn session, drives the Easy Apply modal wizard, fills each
step, pauses at submit for human review.

**Read first:**
- "Component 6 — LinkedIn Easy Apply" (lines ~666-782)
- `core/browser/session.py` — session reuse
- `core/browser/human_behavior.py` — `type_with_cadence`, `human_click`,
  `between_actions_pause`
- `pipelines/job_agent/discovery/providers/linkedin_provider.py` — how
  LinkedIn sessions are loaded today (reuse that code)
- `pipelines/job_agent/application/field_mapper.py` from Prompt 02

**What to build:**
1. `pipelines/job_agent/application/templates/__init__.py`
2. `pipelines/job_agent/application/templates/linkedin_easy_apply.py`:
   - `fill_linkedin_easy_apply(page, listing, profile, resume_path, llm, *, behavior, run_id)`
     per Component 6.
   - Use a module-level dict of **primary + fallback selectors** for
     each canonical element (Easy Apply button, modal, next button,
     submit, file upload, dismiss). The selector constants have
     been proven to drift; always try primary then each fallback.
   - Wizard step handler: enumerate fields, map deterministic ones,
     call the LLM for the rest, click Next/Review/Submit-ready.
   - On the submit step, take a full-page screenshot to
     `data/application_debug/{run_id}/{dedup_key}_linkedin_submit.png`
     and return `ApplicationAttempt(status="awaiting_review", ...)`.
     **Never click Submit.**
   - Daily cap enforcement: read a counter from
     `data/application_state/linkedin_daily_count.json` (UTC-dated).
     If the cap is reached, return `status="stuck"` with a
     diagnostic summary and do not open the modal.
3. Hook into `node.py` — when `strategy == TEMPLATE_LINKEDIN_EASY_APPLY`,
   open a `BrowserManager` with the LinkedIn storage state and call
   the template.

**Tests (`pipelines/job_agent/tests/test_linkedin_easy_apply.py`):**
- Use Playwright's offline mode pointed at a local fixture HTML page
  (shipped under `pipelines/job_agent/tests/fixtures/linkedin_easy_apply/`
  — hand-author a single-step modal clone that is good enough to
  exercise the field-discovery + next-button path).
- Daily cap: artificially set the counter to the cap, assert
  `status="stuck"`.
- Screenshot path exists after a dry-run submit flow.
- No real linkedin.com access in tests; `conftest.py` should block
  the network except for the fixture server.

**Acceptance:**
- CI gate green.
- No secrets in repo. Sessions path stays under `data/sessions/`
  (gitignored).

**Out of scope:** Ashby, Workday, agent filler.

---

### Prompt 09 — Ashby template

**Goal:** Second template filler, much simpler than LinkedIn.

**Read first:**
- "Component 6 — Ashby template" (lines ~784-793)
- Prompt 08's implementation for structure reuse

**What to build:**
1. `pipelines/job_agent/application/templates/ashby.py` — single-page
   form. Reuse field-enumeration and select-fuzzy-match helpers from
   the LinkedIn template; if those helpers exist in `linkedin_easy_apply.py`
   inline, pull them into `pipelines/job_agent/application/templates/_common.py`.
2. Router updated to dispatch `ashby` → `TEMPLATE_ASHBY`.
3. Node dispatches `TEMPLATE_ASHBY` to the new filler.
4. Same human-review invariant: screenshot + pause, never click
   Submit.

**Tests (`pipelines/job_agent/tests/test_ashby_template.py`):**
- Fixture HTML at `pipelines/job_agent/tests/fixtures/ashby_form.html`.
- Assert every standard field gets filled deterministically.
- Assert a custom question field triggers one LLM call.

**Acceptance:** CI gate green.

**Out of scope:** Workday, agent filler, unknown sites.

---

### Prompt 10 — PageObserver enhancements

**Goal:** Add the observer capabilities the LLM agent filler needs:
iframe awareness, `data-automation-id`/`data-testid` selectors,
`maxlength` extraction, and custom-dropdown detection.

**Read first:**
- "Component 7 — Enhanced PageObserver" (lines ~920-974)
- `core/browser/observer.py` in full
- `core/tests/test_browser_stealth.py` for existing observer test patterns
- `core/browser/AGENTS.md`

**What to build:**
1. Edit `core/browser/observer.py`:
   - Extend `_element_to_info` to capture `data-automation-id`,
     `data-testid`, `data-test`, `maxlength`, `aria-expanded`,
     `aria-haspopup`. Add these to `ElementInfo` / `PageState`.
   - Selector-priority logic per Component 7: automation-id → testid
     → id → name-with-tag → xpath fallback.
   - Custom dropdown classification: an element with
     `aria-haspopup="listbox"` or `role="combobox"` that is NOT a
     `<select>` is classified as `field_kind="custom_select"`.
   - `_extract_forms` iterates both the main frame and any iframe
     whose URL matches a known ATS domain list (greenhouse, lever,
     icims, ashby, smartrecruiters). Skip other frames to avoid
     noise.
2. Bump any JSON-schema or snapshot fixtures that test observer
   output.
3. Update `core/web_agent/controller.py` downstream consumers that
   rely on `ElementInfo` fields — mypy will catch anything missing.

**Tests:**
- Extend existing observer tests with HTML fixtures that exercise
  each new code path: a field with `data-automation-id`, a field
  with `maxlength`, a custom dropdown (`<div role="combobox">`),
  and a form inside an iframe.

**Acceptance:**
- No regression in existing observer tests.
- CI gate green.
- Selector priority is unit-tested and stable.

**Out of scope:** Agent controller action changes — that's Prompt 11.

---

### Prompt 11 — WebAgentController: robust upload + custom dropdown actions

**Goal:** Make the existing agent controller actually reliable on
ATS forms. Upload now has 4 fallback strategies. Select handles
both native and custom dropdowns.

**Read first:**
- "Component 7 — Robust file upload" and "Custom dropdown handling"
  (lines ~976-1090)
- `core/web_agent/controller.py` — `_execute_upload`, `_execute_select`
- `core/web_agent/AGENTS.md`

**What to build:**
1. Rewrite `_execute_upload` with all four strategies per Component 7.
   Each strategy logs its attempt at debug level and captures the
   exception into the `ActionResult.error` string on total failure.
2. Rewrite `_execute_select` to branch on `tag == "select"` vs. custom.
   Custom branch: click trigger → `wait_for_selector` on listbox →
   fuzzy match option text → click. Respect `human_behavior` delays.
3. Add `_wait_for_listbox(timeout_ms: int = 3000)` helper that returns
   the found selector so logs show which one matched.
4. Never swallow exceptions without logging at WARN level. Never
   bypass `human_behavior` — that's what makes the session look
   human.

**Tests (`core/tests/test_web_agent_actions.py`):**
- Mock `Page` / `ElementHandle` with `unittest.mock.AsyncMock`.
- Upload strategies: strategy 1 success, strategy 1 fail → strategy
  2 success, all strategies fail → `ActionResult(success=False)`.
- Select: native select path calls `select_option`; custom path
  clicks, waits, matches "Male" in a list of options.

**Acceptance:**
- Existing agent tests stay green.
- CI gate green.
- No `broad-except` lint warnings.

**Out of scope:** Agent prompt changes.

---

### Prompt 12 — Agent filler with enhanced system prompt

**Goal:** The fallback path that handles anything the templates
don't. A single entrypoint the node calls when the router says
`AGENT_GENERIC` or `AGENT_WORKDAY`.

**Read first:**
- "Component 7 — Enhanced system prompt" and "ATS-specific hints"
  (lines ~805-919)
- Existing `pipelines/job_agent/application/form_workflow.py`
- `core/web_agent/context.py`

**What to build:**
1. `pipelines/job_agent/application/agent_filler.py`:
   - `_build_agent_system_prompt(...)` per Component 7 verbatim.
     Load the base template from
     `pipelines/job_agent/application/prompts/form_agent_system.md`
     and interpolate the structured sections in Python.
   - `_ats_specific_hints(platform: str) -> list[str]` with Workday,
     iCIMS, Taleo, (and later) Ashby hint blocks.
   - `fill_with_agent(page, listing, profile, analysis, *, resume_path, cover_letter_path, llm_client, ats_platform, run_id) -> ApplicationAttempt`:
     - Build `AgentGoal` with `max_steps=60`, success/failure signals.
     - Instantiate `WebAgentController` with the new system prompt.
     - Run the loop. Never pass `require_human_approval_before=["submit"]`
       — instead, instruct the LLM to report `done` before clicking
       Submit, and return `awaiting_review` status with a screenshot.
2. Update `pipelines/job_agent/application/form_workflow.py` to
   delegate to `fill_with_agent` (keep the old function for a short
   backwards-compat period; a follow-up prompt removes it).
3. Node dispatches `AGENT_GENERIC`/`AGENT_WORKDAY` to `fill_with_agent`.

**Tests (`pipelines/job_agent/tests/test_agent_filler.py`):**
- Use `MockLLMClient` to pre-script a 5-step trajectory: click Apply
  → fill email → upload resume → fill cover letter → report done.
- Serve HTML from `pipelines/job_agent/tests/fixtures/agent_generic_form.html`.
- Assert the final `ApplicationAttempt.status == "awaiting_review"`
  and `steps_taken == 5`.
- ATS hint injection: assert a Workday-flagged run includes the
  "Workday-specific guidance" block in the system prompt.

**Acceptance:**
- CI gate green.
- `fill_with_agent` is the only place `WebAgentController` is
  constructed for application flows.

**Out of scope:** Redirect-chain following, debug capture, retry.

---

### Prompt 13 — Router redirect-chain following

**Goal:** When the listing URL is a LinkedIn/Indeed job board page
rather than an actual ATS form, follow the "Apply" button to the
real destination and re-detect the ATS there.

**Read first:**
- "Component 4 — Redirect chain following" (lines ~524-547)
- `core/browser/actions.py` for `goto`
- `pipelines/job_agent/application/router.py` from Prompt 04

**What to build:**
1. Extend `router.py` with `_follow_apply_link(page) -> str` per
   Component 4. Handle:
   - New-tab opens (`context.expect_page()`).
   - Same-tab redirects.
   - 3-second wait-for-load-state with a fallback to `networkidle`
     capped at 8 seconds.
2. Update `route_application` to accept `page` and call
   `_follow_apply_link` when the input URL is a LinkedIn job board
   page (`linkedin.com/jobs/view/...`) or Indeed (`indeed.com/viewjob`).
   Re-run `detect_ats_platform` on the resulting URL.
3. Node is updated to open a browser *only* for browser strategies
   or when the router signals redirect-chain needed. API strategies
   still skip the browser.

**Tests (`pipelines/job_agent/tests/test_application_router_redirect.py`):**
- Mock Playwright page: `query_selector` returns a fake apply button,
  `context.expect_page()` yields a new page whose `url` is a
  Greenhouse URL. Assert final strategy is `API_GREENHOUSE`.
- Same case but same-tab: assert strategy resolves from the redirected
  URL.
- No apply button found: falls through to `AGENT_GENERIC` without
  raising.

**Acceptance:**
- CI gate green.
- No real linkedin.com / indeed.com access in tests.

**Out of scope:** Account creation handling.

---

### Prompt 14 — Workday specialization

**Goal:** Handle the hardest common ATS. Custom dropdowns, multi-step
wizard, resume pre-fill verification, account-wall detection.

**Read first:**
- "Phase 5: Workday specialist" (lines ~1425-1440)
- `_ats_specific_hints` "workday" block in Component 7 of this doc
- `pipelines/job_agent/application/agent_filler.py` from Prompt 12

**What to build:**
1. Extend `_ats_specific_hints` with the comprehensive Workday block
   if Prompt 12 left it minimal.
2. `pipelines/job_agent/application/workday_helpers.py`:
   - `detect_workday_account_wall(page) -> bool` — checks for the
     sign-in form container (`div[data-automation-id="signInForm"]`
     or similar). If found, the agent should short-circuit with
     `stuck`.
   - `verify_workday_prefill(page, profile) -> list[str]` — after
     Workday parses the uploaded resume and pre-fills fields, compare
     `personal.email`, `personal.phone_formatted`, name fields. Return
     a list of mismatches so the agent can correct them.
3. Wire these helpers into `fill_with_agent` via a Workday-only
   pre-pass that runs before the generic observe-act loop.

**Tests (`pipelines/job_agent/tests/test_workday_helpers.py`):**
- Account wall detection: page with sign-in form → `True`, without
  → `False`.
- Pre-fill verification: all-correct → empty list; mismatched phone
  → list containing "phone".
- Agent-filler integration test: Workday fixture form → pre-pass
  runs, generic loop proceeds, `ApplicationAttempt.strategy="agent_workday"`.

**Acceptance:**
- Account-wall path reports `status="stuck"` with a summary naming
  the wall, not a generic "something went wrong".
- CI gate green.

**Out of scope:** Account creation (explicitly forbidden — see "Things
to not do").

---

### Prompt 15 — Debug capture for every failure path

**Goal:** Every `status="error"` or `status="stuck"` result leaves a
diagnosable artifact on disk.

**Read first:**
- `core/browser/debug_capture.py`
- "Phase 6: Hardening" (lines ~1441-1453)

**What to build:**
1. Add `_capture_failure_bundle(page, listing, run_id, reason)` helper
   in `pipelines/job_agent/application/_debug.py`. Produces:
   - Full-page screenshot
   - Page HTML (pretty-printed, secrets scrubbed — reuse redaction
     from `core/web_agent/controller.py`)
   - JSON metadata: URL, user agent, viewport, cookies-count (never
     cookie values), timestamp, error summary
   - All three saved under
     `data/application_debug/{run_id}/{dedup_key}_{strategy}_{UTC-timestamp}.{png,html,json}`
2. Every error/stuck return path in `fill_linkedin_easy_apply`,
   `ashby.py`, `fill_with_agent`, and `node.py`'s exception handler
   calls `_capture_failure_bundle`. The returned `ApplicationAttempt`
   gets `screenshot_path` populated.
3. Error-capture paths must never themselves raise. If the capture
   fails, log at WARN level and continue.

**Tests (`pipelines/job_agent/tests/test_application_debug_capture.py`):**
- Fake `Page` + fake filesystem → bundle written with all three
  artifacts.
- Capture raising → original error still returned, capture failure
  logged.

**Acceptance:**
- CI gate green.
- Every error path in the application engine produces a bundle OR
  explains why it did not (e.g., API submitter has no page).

**Out of scope:** Retry logic.

---

### Prompt 16 — Application deduplication + retry

**Goal:** Never re-apply to the same listing. On transient browser
failures, retry once with a fresh context.

**Read first:**
- `core/scraper/dedup.py` — model the application dedup on this
- `pipelines/job_agent/application/node.py` from Prompt 05

**What to build:**
1. `pipelines/job_agent/application/applied_store.py`:
   - SQLite-backed store at `data/applied_applications.db`.
   - Schema: `(dedup_key TEXT PRIMARY KEY, company TEXT, title TEXT,
     strategy TEXT, submitted_at REAL, status TEXT, artifact_dir TEXT)`.
   - Functions: `is_already_applied(dedup_key) -> bool`,
     `record_application(attempt)`, `list_recent(days=30)`.
   - Do NOT reimplement Bloom filters; the volume is small enough
     for direct SQLite queries.
2. Node checks `is_already_applied` before routing. If true, skip
   with `status="skipped"` and a clear summary.
3. Wrap the per-listing execution in a retry helper: one retry
   allowed for any browser strategy that fails with a
   `PlaywrightTimeoutError` or `ElementHandleError`. API strategies
   do NOT retry at this layer — the HTTP client already has retries.
4. The retry uses a **fresh** `BrowserManager` (new context). Never
   reuse the failed context.

**Tests:**
- `pipelines/job_agent/tests/test_applied_store.py` — CRUD on a
  temp DB path.
- `test_application_node.py` — already-applied listing skipped;
  transient failure retried once; double failure propagates.

**Acceptance:**
- CI gate green.
- `applied_applications.db` path is configurable via `Settings`.

**Out of scope:** Notifications, metrics.

---

### Prompt 17 — Notification integration

**Goal:** The notification node summarizes application outcomes.

**Read first:**
- `pipelines/job_agent/nodes/notification.py`
- `core/notifications/` — whatever transport currently exists

**What to build:**
1. Extend the notification node's render function to group
   `state.application_results` by `status` and produce a concise
   summary block. Example:

   ```
   Applications:
   - Submitted (API): 3
   - Awaiting human review (browser): 2
     * TechCorp — Workday — link to screenshot
     * DataCo — LinkedIn — link to screenshot
   - Stuck: 1
     * Old-Ent — Taleo — account wall detected
   - Errors: 0
   ```

2. Include relative paths to the screenshot bundles so a human can
   click through. Do not attach files; just reference them.
3. Respect the existing quiet-hours / per-run-throttle logic in the
   notification node.

**Tests:**
- `test_notification_application_summary.py` — snapshot test on the
  rendered summary for each `status` mix.

**Acceptance:** CI gate green.

**Out of scope:** New transports.

---

### Prompt 18 — Per-ATS metrics

**Goal:** Track success rates by ATS so the team knows which
templates need love.

**Read first:**
- `core/observability/` — existing metrics scaffolding
- Prompt 16's `applied_store.py`

**What to build:**
1. `pipelines/job_agent/application/metrics.py`:
   - `record_attempt(attempt: ApplicationAttempt)` — writes a row
     to a time-series table in `applied_applications.db`.
   - `compute_success_rates(window_days: int = 30) -> dict[str, float]`
     — per-ATS submission success rate (submitted / total).
2. CLI reporting subcommand:
   `python -m pipelines.job_agent.application.metrics report`
   prints a table of the last-30-day success rates by strategy.

**Tests:**
- `test_application_metrics.py` — seed some attempts, assert the
  computed rate, assert the CLI output contains each strategy.

**Acceptance:**
- CI gate green.
- No new third-party dependencies.

**Out of scope:** Dashboards, alerting.

---

### Prompt 19 — End-to-end smoke test + CI gate

**Goal:** One test that boots the whole graph against a canned
listing and asserts the application node produces the expected
`ApplicationAttempt`. No real network. No real LLM.

**Read first:**
- `pipelines/job_agent/tests/test_cover_letter_tailoring.py` for the
  end-to-end style that already exists
- `core/testing/` for `MockLLMClient`

**What to build:**
1. `pipelines/job_agent/tests/test_application_e2e.py`:
   - Seed a `JobAgentState` with one Greenhouse listing and one
     "unknown" listing.
   - Use `MockLLMClient` to pre-script all LLM answers.
   - Patch the HTTP fetcher to return fixture payloads.
   - Patch `BrowserManager` to return a fake `Page` that serves a
     fixture HTML form.
   - Run `graph.ainvoke(state)`.
   - Assert:
     - Exactly two entries in `state.application_results`.
     - The Greenhouse one has `status="awaiting_review"` and
       `strategy="api_greenhouse"`.
     - The unknown one has `status="awaiting_review"` and
       `strategy="agent_generic"`.
     - The applied store contains both.
     - The notification summary includes both.
2. Mark the test with `@pytest.mark.slow` if it runs longer than
   0.5 s wall-clock; CI already runs slow-marked tests.

**Acceptance:**
- Running `pytest pipelines/job_agent/tests/test_application_e2e.py -x`
  passes in under 5 seconds on the CI runner.
- CI gate green on the full suite.

**Out of scope:** Live network probes. Any real employer URL.

---

### Prompt 20 — Documentation + shipping checklist

**Goal:** Close the loop. Document the feature from a user's
perspective, verify every invariant, open the PR.

**Read first:**
- Every file touched since Prompt 01.
- `README.md`
- `pipelines/job_agent/README.md`

**What to build:**
1. Update `pipelines/job_agent/README.md` with a new "Application
   engine" section explaining: how to set the candidate application
   profile, how to enable/disable the engine
   (`KP_APPLICATION_MAX_PER_RUN`, `KP_APPLICATION_REQUIRE_HUMAN_REVIEW`),
   how dry-run works, where debug bundles land, how to check metrics.
2. Add a one-page operator runbook at
   `docs/application_engine_runbook.md` covering: safe first-run
   procedure, how to inspect a pending-review screenshot, how to
   actually click Submit after review (the explicit step that is
   NOT automated), how to recover from a LinkedIn daily-cap trip,
   how to re-try a stuck listing.
3. Final pre-PR checklist — tick each off before opening:
   - [ ] Every new file has a module-level docstring explaining what
         it is and what it composes.
   - [ ] No `# type: ignore` added without a one-line comment
         explaining why.
   - [ ] No `Any` in public function signatures.
   - [ ] No hardcoded secrets, cookies, or PII in source control.
   - [ ] `ruff check` and `ruff format --check` clean.
   - [ ] `mypy core/ pipelines/ --ignore-missing-imports` clean.
   - [ ] `pytest --cov=core --cov=pipelines --cov-report=term-missing -x`
         clean, coverage on `pipelines/job_agent/application/` ≥ 85 %.
   - [ ] End-to-end smoke test (Prompt 19) passes.
   - [ ] Manual run: `python -m pipelines.job_agent --dry-run` against
         a seeded Greenhouse listing produces an `awaiting_review`
         attempt with a complete payload and zero errors in
         `state.errors`.
   - [ ] Graph boot: `python -c "from pipelines.job_agent.graph import
         build_graph; build_graph()"` exits 0.
   - [ ] Human-review invariant: grep for `click.*submit` /
         `submit_application` in new code → every hit is behind a
         flag or explicit approval path. Zero unconditional Submit
         clicks.
   - [ ] LinkedIn daily cap is enforced (test in place).
   - [ ] Debug capture writes all three artifacts on a forced failure.
4. Open the PR titled `feat(application): application engine (prompts 01-20)`.
   Body lists the components, references this architecture doc, and
   links the Phase 1-6 rollout plan above.

**Acceptance:**
- CI green on the PR.
- All checklist boxes ticked in the PR description.
- The PR diff stays focused on the application engine plus the
  unavoidable wiring into `state.py`, `graph.py`, `core/config.py`,
  and `nodes/tracking.py`. Drive-by refactors belong in separate
  PRs.

**Out of scope:** Shipping auto-submit for any strategy. That is a
separate decision gated on at least 50 reviewed-and-approved
applications across each strategy that would become auto-submit.

---

### Audit checklist for anyone reading these prompts later

If you are revisiting this plan and want to confirm it still meets
the bar ("a flagship coding model can implement the engine from
scratch with only the repo, internet, and these prompts"), verify:

- [ ] Every prompt names **specific files** it reads and writes.
- [ ] Every prompt names **tests** with a concrete file path.
- [ ] Every prompt has an explicit **acceptance** block that names
      the CI commands.
- [ ] No prompt assumes state from a conversation that is not in
      the prompt itself or in a file the prompt reads.
- [ ] No prompt depends on a later prompt except through the "Out
      of scope" marker (so a model that stops halfway still ends
      up with a coherent, green build).
- [ ] The last prompt closes the loop on docs + CI + PR.
- [ ] The invariants in "How to use these prompts" are restated or
      re-linkable without requiring tribal knowledge.

If any of those check boxes fails, rewrite the failing prompt and
re-run Prompt 00 to re-baseline.
