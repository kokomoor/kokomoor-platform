"""Layered job-page extraction for direct listing URLs.

Transport is shared infrastructure: ``core.fetch.HttpFetcher`` and
``core.fetch.BrowserFetcher`` implement ``ContentFetcher``; this module only
contains job-domain parsing (selectors, JSON-LD JobPosting mapping, scoring).

Extraction order is deliberately resilient:
1. Structured metadata (JSON-LD, meta tags)
2. Provider-aware selectors (LinkedIn, Indeed, Greenhouse, Lever, Workday, Ashby)
3. Generic main-content block scoring
4. Full-page text fallback

If plain HTTP extraction is weak (often JS-rendered pages), the module retries
with ``BrowserFetcher`` (Playwright via ``BrowserManager``) and re-runs parsing.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from html import unescape
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import structlog
from bs4 import BeautifulSoup, Tag

from core.fetch import BrowserFetcher, HttpFetcher, iter_json_ld_objects_from_soup
from pipelines.job_agent.models import JobSource

logger = structlog.get_logger(__name__)

_MIN_DESCRIPTION_CHARS = 500
_MAX_SUMMARY_CHARS = 280
_MIN_EXTRACTION_SCORE_FOR_HTTP_ONLY = 1300

_KEEP_QUERY_KEYS = {
    "gh_jid",
    "jk",
    "job",
    "jobid",
    "jid",
    "reqid",
    "lever-via",
}
_DROP_QUERY_PREFIXES = ("utm_", "fbclid", "gclid", "trk", "mc_")

_NOISE_PHRASES = {
    "cookie preferences",
    "privacy policy",
    "sign in",
    "join now",
    "create account",
    "related jobs",
    "people also viewed",
    "report this job",
}

_PROVIDER_SELECTORS: dict[str, list[str]] = {
    "linkedin": [
        ".show-more-less-html__markup",
        ".description__text",
        ".jobs-description-content__text",
        "#job-details",
    ],
    "indeed": [
        "#jobDescriptionText",
        ".jobsearch-JobComponent-description",
    ],
    "greenhouse": [
        "#content .content",
        ".opening .content",
        "#app-body",
    ],
    "lever": [
        ".posting-page .section-wrapper",
        ".posting-page .content",
        ".content-wrapper",
    ],
    "workday": [
        "[data-automation-id='jobPostingDescription']",
        "[data-automation-id='jobDescription']",
        ".jobDescriptionText",
    ],
    "ashby": [
        "[data-testid='job-posting-description']",
        ".job-posting-description",
    ],
    "amazon": [
        ".job-detail-body-container",
        "#job-detail-body",
    ],
}

_SALARY_RE = re.compile(
    r"\$?\s*([1-9]\d{1,2}(?:,\d{3})+|[1-9]\d{4,6})"
    r"(?:\s*(?:-|to)\s*\$?\s*([1-9]\d{1,2}(?:,\d{3})+|[1-9]\d{4,6}))?",
    re.IGNORECASE,
)


@dataclass
class ExtractedJobData:
    """Canonical extraction result before conversion to ``JobListing``.

    Description fields:
    - ``raw_description``: faithful block text as extracted from the winning
      candidate source (before normalization).
    - ``cleaned_description``: normalized canonical text used downstream by
      ``JobListing.description`` and the analysis/tailoring nodes.
    """

    title: str
    company: str
    location: str
    canonical_url: str
    source: JobSource
    raw_description: str
    cleaned_description: str
    salary_min: int | None = None
    salary_max: int | None = None
    remote: bool | None = None
    employment_type: str = ""
    role_summary: str = ""
    metadata: dict[str, object] = field(default_factory=dict)

    @property
    def normalized_description(self) -> str:
        """Backward-compatible alias for cleaned canonical description."""
        return self.cleaned_description


@dataclass
class _StructuredFields:
    title: str = ""
    company: str = ""
    location: str = ""
    description: str = ""
    salary_min: int | None = None
    salary_max: int | None = None
    remote_mode: str = ""
    employment_type: str = ""


@dataclass
class _DescriptionCandidate:
    source: str
    raw_text: str
    cleaned_text: str
    score: int


def generate_dedup_key(company: str, title: str, url: str) -> str:
    """Generate a deterministic dedup key from listing identifiers."""
    raw = f"{company.lower().strip()}|{title.lower().strip()}|{url.strip()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


async def extract_job_data_from_url(url: str) -> ExtractedJobData:
    """Fetch and extract normalized job data from a direct listing URL.

    Fetches with the original URL to preserve all query parameters, then
    canonicalizes the final resolved URL for provider detection and dedup.
    """
    http_fetcher = HttpFetcher()
    http_result = await http_fetcher.fetch(url.strip())
    http_final_url = canonicalize_job_url(http_result.final_url)
    http_provider = detect_provider(http_final_url)
    http_source = map_provider_to_source(http_provider)
    best = _extract_from_html(
        http_result.html,
        http_final_url,
        http_provider,
        http_source,
        extraction_method="http",
    )
    best.metadata["browser_fallback_used"] = False

    needs_browser_fallback = (
        _extraction_quality_score(best, http_result.html) < _MIN_EXTRACTION_SCORE_FOR_HTTP_ONLY
        or len(best.cleaned_description) < _MIN_DESCRIPTION_CHARS
        or not best.title
        or not best.company
    )

    if needs_browser_fallback:
        logger.info(
            "manual_extract.browser_fallback",
            provider=http_provider,
            reason="low_quality_or_js_blocked",
            chars=len(best.cleaned_description),
        )
        try:
            browser_fetcher = BrowserFetcher()
            browser_result = await browser_fetcher.fetch(http_final_url)
            rendered_url = canonicalize_job_url(browser_result.final_url)
            rendered_provider = detect_provider(rendered_url)
            rendered_source = map_provider_to_source(rendered_provider)
            rendered = _extract_from_html(
                browser_result.html,
                rendered_url,
                rendered_provider,
                rendered_source,
                extraction_method="browser",
            )
            best = _pick_better_extraction(
                http_data=best,
                browser_data=rendered,
                http_html=http_result.html,
                browser_html=browser_result.html,
            )
            best.metadata["browser_fallback_used"] = True
        except Exception as exc:
            logger.warning(
                "manual_extract.browser_fallback_failed",
                provider=http_provider,
                error=str(exc)[:200],
            )

    if not best.cleaned_description:
        msg = "No extractable job description found"
        raise ValueError(msg)

    return best


def canonicalize_job_url(url: str) -> str:
    """Normalize URL for stable dedup and source handling."""
    parsed = urlparse(url.strip())
    if not parsed.scheme:
        parsed = parsed._replace(scheme="https")
    if not parsed.netloc and parsed.path:
        parsed = urlparse(f"https://{parsed.path}")

    query_pairs = parse_qsl(parsed.query, keep_blank_values=False)
    kept: list[tuple[str, str]] = []
    for key, value in query_pairs:
        key_lower = key.lower()
        if key_lower in _KEEP_QUERY_KEYS or "job" in key_lower:
            kept.append((key, value))
            continue
        if key_lower.startswith(_DROP_QUERY_PREFIXES):
            continue
    normalized_query = urlencode(sorted(kept), doseq=True)
    normalized = parsed._replace(fragment="", query=normalized_query)
    return urlunparse(normalized)


def detect_provider(url: str) -> str:
    """Infer provider name from URL host."""
    host = urlparse(url).netloc.lower()
    if "linkedin." in host:
        return "linkedin"
    if "indeed." in host:
        return "indeed"
    if "greenhouse." in host:
        return "greenhouse"
    if "lever.co" in host:
        return "lever"
    if "myworkdayjobs." in host or "workday." in host:
        return "workday"
    if "ashbyhq." in host:
        return "ashby"
    if "amazon.jobs" in host:
        return "amazon"
    return "company_site"


def map_provider_to_source(provider: str) -> JobSource:
    """Map provider label to ``JobSource`` enum."""
    return {
        "linkedin": JobSource.LINKEDIN,
        "indeed": JobSource.INDEED,
        "greenhouse": JobSource.GREENHOUSE,
        "lever": JobSource.LEVER,
        "workday": JobSource.WORKDAY,
        "amazon": JobSource.COMPANY_SITE,
        "company_site": JobSource.COMPANY_SITE,
    }.get(provider, JobSource.OTHER)


def _extract_from_html(
    html: str,
    final_url: str,
    provider: str,
    source: JobSource,
    *,
    extraction_method: str,
) -> ExtractedJobData:
    soup = BeautifulSoup(html, "html.parser")
    structured = _extract_structured_fields(soup)

    provider_block = _extract_provider_description(soup, provider)
    generic_block = _extract_generic_main_block(soup)
    fallback_block = _extract_full_page_text(soup)
    best_description = _select_best_description_candidate(
        structured=structured.description,
        provider=provider_block,
        generic=generic_block,
        fallback=fallback_block,
    )

    title = normalize_line(structured.title) or _extract_title(soup)
    company = normalize_line(structured.company) or _extract_company(
        soup,
        provider=provider,
        title=title,
        final_url=final_url,
    )
    location = normalize_line(structured.location) or _extract_location(soup)

    salary_min = structured.salary_min
    salary_max = structured.salary_max
    if salary_min is None and salary_max is None:
        salary_min, salary_max = _infer_salary(best_description.cleaned_text)

    remote_mode = structured.remote_mode or infer_remote_mode(
        title,
        location,
        best_description.cleaned_text,
    )
    remote: bool | None
    if remote_mode == "remote":
        remote = True
    elif remote_mode == "onsite":
        remote = False
    else:
        remote = None

    employment_type = normalize_line(structured.employment_type) or infer_employment_type(
        best_description.cleaned_text
    )

    return ExtractedJobData(
        title=title,
        company=company,
        location=location,
        canonical_url=final_url,
        source=source,
        raw_description=best_description.raw_text,
        cleaned_description=best_description.cleaned_text,
        salary_min=salary_min,
        salary_max=salary_max,
        remote=remote,
        employment_type=employment_type,
        role_summary=build_role_summary(best_description.cleaned_text),
        metadata={
            "resolved_provider": provider,
            "extraction_method": extraction_method,
            "extraction_mode": best_description.source,
            "extraction_score": best_description.score,
            "source": source.value,
            "remote_mode": remote_mode,
            "description_chars_raw": len(best_description.raw_text),
            "description_chars_cleaned": len(best_description.cleaned_text),
        },
    )


def _extract_structured_fields(soup: BeautifulSoup) -> _StructuredFields:
    merged = _StructuredFields()

    for obj in iter_json_ld_objects_from_soup(soup):
        posting = _extract_jobposting_object(obj)
        if posting is None:
            continue
        extracted = _parse_jobposting(posting)
        merged = _merge_structured(merged, extracted)

    title_meta = _meta_content(soup, "og:title") or _meta_name_content(soup, "title")
    if title_meta and not merged.title:
        merged.title = normalize_line(title_meta)
    description_meta = _meta_content(soup, "og:description")
    if description_meta and not merged.description:
        merged.description = description_meta

    return merged


def _merge_structured(base: _StructuredFields, incoming: _StructuredFields) -> _StructuredFields:
    return _StructuredFields(
        title=base.title or incoming.title,
        company=base.company or incoming.company,
        location=base.location or incoming.location,
        description=base.description or incoming.description,
        salary_min=base.salary_min if base.salary_min is not None else incoming.salary_min,
        salary_max=base.salary_max if base.salary_max is not None else incoming.salary_max,
        remote_mode=base.remote_mode or incoming.remote_mode,
        employment_type=base.employment_type or incoming.employment_type,
    )


def _select_best_description_candidate(
    *,
    structured: str,
    provider: str,
    generic: str,
    fallback: str,
) -> _DescriptionCandidate:
    candidates = _build_description_candidates(
        structured=structured,
        provider=provider,
        generic=generic,
        fallback=fallback,
    )
    if not candidates:
        return _DescriptionCandidate(source="empty", raw_text="", cleaned_text="", score=0)
    return max(candidates, key=lambda c: c.score)


def _build_description_candidates(
    *,
    structured: str,
    provider: str,
    generic: str,
    fallback: str,
) -> list[_DescriptionCandidate]:
    raw_candidates = {
        "structured": structured,
        "provider": provider,
        "generic": generic,
        "fallback": fallback,
    }
    candidates: list[_DescriptionCandidate] = []
    for source, raw_text in raw_candidates.items():
        if not raw_text or not raw_text.strip():
            continue
        cleaned = normalize_description(raw_text)
        if not cleaned:
            continue
        candidates.append(
            _DescriptionCandidate(
                source=source,
                raw_text=raw_text.strip(),
                cleaned_text=cleaned,
                score=_score_description_candidate(cleaned, source=source),
            )
        )
    return candidates


def _score_description_candidate(text: str, *, source: str) -> int:
    lowered = text.lower()
    score = len(text)
    score += lowered.count("responsibilit") * 120
    score += lowered.count("qualification") * 180
    score += lowered.count("requirements") * 180
    score += lowered.count("preferred") * 80
    score += lowered.count("experience") * 60
    score += lowered.count("- ") * 30
    if source == "provider":
        score += 180
    elif source == "generic":
        score += 120
    elif source == "structured":
        score += 80
    score -= lowered.count("apply now") * 100
    score -= lowered.count("cookie") * 100
    return score


def _extract_jobposting_object(obj: object) -> dict[str, object] | None:
    if not isinstance(obj, dict):
        return None

    at_type = obj.get("@type")
    if isinstance(at_type, str) and at_type.lower() == "jobposting":
        return obj
    if isinstance(at_type, list) and any(str(t).lower() == "jobposting" for t in at_type):
        return obj

    graph = obj.get("@graph")
    if isinstance(graph, list):
        for item in graph:
            nested = _extract_jobposting_object(item)
            if nested is not None:
                return nested
    return None


def _parse_jobposting(posting: dict[str, object]) -> _StructuredFields:
    title = normalize_line(str(posting.get("title", "")))
    description = _html_to_text(str(posting.get("description", "")))

    company = ""
    org = posting.get("hiringOrganization")
    if isinstance(org, dict):
        company = normalize_line(str(org.get("name", "")))

    location = ""
    loc = posting.get("jobLocation")
    if isinstance(loc, dict):
        location = _extract_location_from_structured(loc)
    elif isinstance(loc, list):
        for loc_item in loc:
            if isinstance(loc_item, dict):
                location = _extract_location_from_structured(loc_item)
                if location:
                    break

    salary_min, salary_max = _extract_salary_from_structured(posting.get("baseSalary"))
    remote_mode = ""
    location_type = str(posting.get("jobLocationType", "")).lower()
    if "telecommute" in location_type or "remote" in location_type:
        remote_mode = "remote"

    employment_type = normalize_line(str(posting.get("employmentType", "")))
    return _StructuredFields(
        title=title,
        company=company,
        location=location,
        description=description,
        salary_min=salary_min,
        salary_max=salary_max,
        remote_mode=remote_mode,
        employment_type=employment_type,
    )


def _extract_salary_from_structured(base_salary: object) -> tuple[int | None, int | None]:
    if not isinstance(base_salary, dict):
        return None, None
    value = base_salary.get("value")
    if isinstance(value, dict):
        min_value = _to_int(value.get("minValue"))
        max_value = _to_int(value.get("maxValue"))
        single = _to_int(value.get("value"))
        if min_value is None and max_value is None and single is not None:
            return single, single
        return min_value, max_value
    return None, None


def _to_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        cleaned = re.sub(r"[^\d]", "", value)
        if cleaned:
            return int(cleaned)
    return None


def _extract_provider_description(soup: BeautifulSoup, provider: str) -> str:
    selectors = _PROVIDER_SELECTORS.get(provider, [])
    for selector in selectors:
        nodes = soup.select(selector)
        if not nodes:
            continue
        parts: list[str] = []
        for node in nodes:
            if isinstance(node, Tag):
                text = _node_to_raw_text(node)
                if text:
                    parts.append(text)
        merged = "\n\n".join(parts)
        if len(normalize_description(merged)) >= 120:
            return merged
    return ""


def _extract_generic_main_block(soup: BeautifulSoup) -> str:
    """Find the richest content region, merging sibling sections that look job-related."""
    cleaned = _clone_without_noise(soup)
    candidates = cleaned.select("main, article, section, div")

    best_node: Tag | None = None
    best_score = -1
    for node in candidates:
        if not isinstance(node, Tag):
            continue
        raw_text = _node_to_raw_text(node)
        cleaned_text = normalize_description(raw_text)
        if len(cleaned_text) < 120:
            continue
        score = _score_description_candidate(cleaned_text, source="_internal")
        if score > best_score:
            best_score = score
            best_node = node

    if best_node is None:
        return ""

    parts = [_node_to_raw_text(best_node)]
    for sibling in best_node.find_next_siblings():
        if not isinstance(sibling, Tag):
            continue
        sib_text = _node_to_raw_text(sibling)
        sib_cleaned = normalize_description(sib_text)
        if len(sib_cleaned) < 40:
            continue
        if _score_description_candidate(sib_cleaned, source="_internal") > 0:
            parts.append(sib_text)
    return "\n\n".join(parts)


def _extract_full_page_text(soup: BeautifulSoup) -> str:
    cleaned = _clone_without_noise(soup)
    return cleaned.get_text("\n", strip=True)


def _clone_without_noise(soup: BeautifulSoup) -> BeautifulSoup:
    cloned = BeautifulSoup(str(soup), "html.parser")
    for node in cloned.select(
        "script, style, nav, header, footer, form, button, iframe, noscript, aside, svg"
    ):
        node.decompose()
    for node in cloned.find_all(True):
        if not isinstance(node, Tag):
            continue
        # Broken real-world HTML (e.g. some LinkedIn responses) can yield ``Tag.attrs is None``.
        raw_attrs = getattr(node, "attrs", None)
        mapping: dict[str, object] = raw_attrs if isinstance(raw_attrs, dict) else {}
        attr_parts: list[str] = []
        for attr in ("id", "class", "role", "aria-label"):
            val = mapping.get(attr, "")
            if isinstance(val, list):
                attr_parts.append(" ".join(str(v) for v in val))
            else:
                attr_parts.append(str(val))
        attrs = " ".join(attr_parts).lower()
        if any(
            token in attrs
            for token in ("cookie", "consent", "related", "newsletter", "breadcrumb", "share")
        ):
            node.decompose()
    return cloned


def _extract_title(soup: BeautifulSoup) -> str:
    for selector in ("h1", "meta[property='og:title']", "title"):
        node = soup.select_one(selector)
        if isinstance(node, Tag):
            if node.name == "meta":
                content = node.get("content")
                if isinstance(content, str) and content.strip():
                    return normalize_line(content)
            text = node.get_text(" ", strip=True)
            if text:
                return normalize_line(text)
    return ""


def _extract_company(
    soup: BeautifulSoup,
    *,
    provider: str,
    title: str,
    final_url: str,
) -> str:
    company_selectors = {
        "linkedin": [
            ".job-details-jobs-unified-top-card__company-name a",
            ".topcard__org-name-link",
        ],
        "indeed": [".jobsearch-CompanyInfoWithoutHeaderImage div"],
        "greenhouse": [".company-name", "meta[property='og:site_name']"],
        "lever": [".posting-headline .company", ".main-header-text"],
        "workday": ["[data-automation-id='company']"],
        "ashby": [".ashby-job-posting-company"],
        "amazon": [".job-company-name", "meta[property='og:site_name']"],
    }
    for selector in company_selectors.get(provider, []):
        node = soup.select_one(selector)
        if isinstance(node, Tag):
            text = node.get_text(" ", strip=True)
            if text:
                return normalize_line(text)
    generic_visible = _extract_generic_company_visible_text(soup)
    if generic_visible:
        return generic_visible
    title_company = _extract_company_from_title(title)
    if title_company:
        return title_company
    site_name = _meta_content(soup, "og:site_name")
    if site_name:
        return normalize_line(site_name)
    host = urlparse(final_url).netloc
    if host:
        host_label = host.lower().replace("www.", "").split(".")[0].replace("-", " ").strip()
        if host_label:
            return " ".join(part.capitalize() for part in host_label.split())
    return "Unknown Company"


def _extract_generic_company_visible_text(soup: BeautifulSoup) -> str:
    selectors = [
        "[itemprop='hiringOrganization']",
        "[data-company]",
        "[aria-label*='company' i]",
        ".company",
        ".company-name",
        ".employer",
    ]
    for selector in selectors:
        node = soup.select_one(selector)
        if isinstance(node, Tag):
            text = normalize_line(node.get_text(" ", strip=True))
            if _looks_like_company_name(text):
                return text

    title_node = soup.find("h1")
    if isinstance(title_node, Tag):
        container = title_node.parent if isinstance(title_node.parent, Tag) else None
        nearby: list[Tag] = []
        if container is not None:
            nearby.extend(container.find_all(["h2", "h3", "p", "span"], limit=6))
        for node in nearby:
            text = normalize_line(node.get_text(" ", strip=True))
            if _looks_like_company_name(text):
                return text
    return ""


def _extract_company_from_title(title: str) -> str:
    clean = normalize_line(title)
    if not clean:
        return ""
    patterns = (
        r"^(?P<role>.+?)\s+(?:at|@\s*)\s+(?P<company>[A-Z][A-Za-z0-9& .-]{1,80})$",
        r"^(?P<role>.+?)\s+[-|]\s+(?P<company>[A-Z][A-Za-z0-9& .-]{1,80})$",
        r"^(?P<company>[A-Z][A-Za-z0-9& .-]{1,80})\s+is hiring\s+(?P<role>.+)$",
    )
    for pattern in patterns:
        match = re.match(pattern, clean, flags=re.IGNORECASE)
        if not match:
            continue
        company = normalize_line(match.group("company"))
        if _looks_like_company_name(company):
            return company
    return ""


def _looks_like_company_name(value: str) -> bool:
    if not value:
        return False
    lowered = value.lower()
    if lowered in {"company", "employer", "careers"}:
        return False
    if any(token in lowered for token in ("apply", "job description", "responsibilities")):
        return False
    return len(value) <= 100


def _extract_location(soup: BeautifulSoup) -> str:
    location_selectors = [
        "[data-automation-id='locations']",
        "[data-qa='job-location']",
        ".job-details-jobs-unified-top-card__bullet",
        ".location",
        ".location-icon + span",
        ".location-and-id .location",
    ]
    for selector in location_selectors:
        node = soup.select_one(selector)
        if isinstance(node, Tag):
            text = normalize_line(node.get_text(" ", strip=True))
            if _looks_like_location(text):
                return text
    body_text = soup.get_text(" ", strip=True)
    for pattern in (
        r"\b(Remote|Hybrid|On[- ]?site)\b",
        r"\b[A-Z][a-z]+,\s*[A-Z]{2}\b",
    ):
        match = re.search(pattern, body_text)
        if match:
            return normalize_line(match.group(0))
    return ""


def _looks_like_location(text: str) -> bool:
    if not text:
        return False
    return bool(
        re.search(r"\b(remote|hybrid|on[- ]?site)\b", text, re.IGNORECASE)
        or re.search(r"\b[A-Z][a-z]+,\s*[A-Z]{2}\b", text)
    )


def _extract_location_from_structured(loc: dict[str, object]) -> str:
    address = loc.get("address")
    if not isinstance(address, dict):
        return ""
    city = normalize_line(str(address.get("addressLocality", "")))
    region = normalize_line(str(address.get("addressRegion", "")))
    if city and region:
        return f"{city}, {region}"
    return city or region


def _meta_content(soup: BeautifulSoup, prop: str) -> str:
    node = soup.find("meta", attrs={"property": prop})
    if isinstance(node, Tag):
        content = node.get("content")
        if isinstance(content, str):
            return content.strip()
    return ""


def _meta_name_content(soup: BeautifulSoup, name: str) -> str:
    node = soup.find("meta", attrs={"name": name})
    if isinstance(node, Tag):
        content = node.get("content")
        if isinstance(content, str):
            return content.strip()
    return ""


def _node_to_raw_text(node: Tag) -> str:
    return node.get_text("\n", strip=True)


def _html_to_text(value: str) -> str:
    if "<" not in value and ">" not in value:
        return unescape(value).strip()
    soup = BeautifulSoup(value, "html.parser")
    return soup.get_text("\n", strip=True)


def _extraction_quality_score(data: ExtractedJobData, html: str) -> int:
    raw_score = data.metadata.get("extraction_score", 0)
    if isinstance(raw_score, int):
        score = raw_score
    elif isinstance(raw_score, float):
        score = int(raw_score)
    else:
        score = 0
    if data.title and data.title != "Unknown Role":
        score += 180
    if data.company and data.company != "Unknown Company":
        score += 180
    if data.location:
        score += 80
    if data.source in {
        JobSource.GREENHOUSE,
        JobSource.LEVER,
        JobSource.WORKDAY,
        JobSource.LINKEDIN,
        JobSource.INDEED,
    }:
        score += 80
    cleaned = data.cleaned_description.lower()
    if "basic qualifications" in cleaned:
        score += 120
    if "preferred qualifications" in cleaned:
        score += 120
    if "responsibilities" in cleaned or "requirements" in cleaned:
        score += 120
    if _looks_js_blocked(html):
        score -= 300
    return score


def _pick_better_extraction(
    *,
    http_data: ExtractedJobData,
    browser_data: ExtractedJobData,
    http_html: str,
    browser_html: str,
) -> ExtractedJobData:
    http_score = _extraction_quality_score(http_data, http_html)
    browser_score = _extraction_quality_score(browser_data, browser_html)
    if browser_score > http_score:
        browser_data.metadata["extraction_winner"] = "browser"
        browser_data.metadata["http_score"] = http_score
        browser_data.metadata["browser_score"] = browser_score
        return browser_data
    http_data.metadata["extraction_winner"] = "http"
    http_data.metadata["http_score"] = http_score
    http_data.metadata["browser_score"] = browser_score
    return http_data


def _infer_salary(text: str) -> tuple[int | None, int | None]:
    candidates: list[tuple[int | None, int | None]] = []
    for match in _SALARY_RE.finditer(text):
        first = _to_int(match.group(1))
        second = _to_int(match.group(2))
        if first is None:
            continue
        if second is None:
            candidates.append((first, first))
        else:
            lo = min(first, second)
            hi = max(first, second)
            candidates.append((lo, hi))

    if not candidates:
        return None, None
    candidates.sort(key=lambda pair: pair[1] or 0, reverse=True)
    return candidates[0]


def infer_remote_mode(title: str, location: str, description: str) -> str:
    """Infer remote/hybrid/onsite mode from listing text."""
    haystack = f"{title}\n{location}\n{description}".lower()
    if "hybrid" in haystack:
        return "hybrid"
    if re.search(r"\b(remote|work from home|wfh)\b", haystack):
        return "remote"
    if "on-site" in haystack or "onsite" in haystack:
        return "onsite"
    return ""


def infer_employment_type(description: str) -> str:
    lowered = description.lower()
    for token in ("full-time", "part-time", "contract", "internship", "temporary"):
        if token in lowered:
            return token
    return ""


def normalize_line(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def normalize_description(text: str) -> str:
    """Normalize text while preserving structure needed by tailoring."""
    raw_lines = [normalize_line(unescape(line)) for line in text.splitlines()]
    lines: list[str] = []
    seen: set[str] = set()
    for line in raw_lines:
        if not line:
            continue
        lowered = line.lower()
        if any(phrase in lowered for phrase in _NOISE_PHRASES):
            continue
        line = re.sub(r"^[\u2022\u2023\u25E6\u2043\u2219•]\s*", "- ", line)
        key = lowered
        if key in seen:
            continue
        seen.add(key)
        lines.append(line)
    return "\n".join(lines).strip()


def build_role_summary(description: str) -> str:
    """Build a concise summary from the first informative lines."""
    parts: list[str] = []
    for line in description.splitlines():
        trimmed = line.strip("- ").strip()
        if len(trimmed) < 20:
            continue
        if trimmed.endswith(":"):
            continue
        parts.append(trimmed)
        if len(" ".join(parts)) >= _MAX_SUMMARY_CHARS:
            break
    summary = " ".join(parts).strip()
    if len(summary) > _MAX_SUMMARY_CHARS:
        summary = summary[: _MAX_SUMMARY_CHARS - 3].rstrip() + "..."
    return summary


def _looks_js_blocked(html: str) -> bool:
    lowered = html.lower()
    hints = (
        "enable javascript",
        "javascript is disabled",
        "please turn on javascript",
        "captcha",
    )
    return any(hint in lowered for hint in hints)


def extract_job_data_from_html(url: str, html: str) -> ExtractedJobData:
    """Extract job data from HTML content (test and fixture entry point)."""
    canonical_url = canonicalize_job_url(url)
    provider = detect_provider(canonical_url)
    source = map_provider_to_source(provider)
    return _extract_from_html(
        html,
        canonical_url,
        provider,
        source,
        extraction_method="fixture",
    )
