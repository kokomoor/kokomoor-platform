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
