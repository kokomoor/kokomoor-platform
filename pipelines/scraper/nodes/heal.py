"""Human-gated scraper diagnosis and remediation handoff node.

**Stage 1: Diagnosis** — A flagship LLM analyzes the failing scraper
(validation report, fresh fixture, wrapper code, profile) and produces
a ``RemediationReport`` with step-by-step repair instructions.

Remediation execution is intentionally handled by a separate operator/agent
workflow after the signed "fix" email trigger is validated.

Safety guardrails:
- Signed trigger token required for remediation opt-in.
- Anti-loop: never attempt remediation if the same ``heal_id`` has
  already been attempted and failed.
- Human gate: the user must explicitly opt in to remediation.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from core.notifications.heal_auth import build_heal_trigger_token
from pipelines.scraper.models import (
    RemediationReport,
    RemediationStep,
    ValidationReport,
)

if TYPE_CHECKING:
    from core.llm.protocol import LLMClient
    from core.scraper.fixtures import FixtureStore
    from pipelines.scraper.models import SiteProfile

logger = structlog.get_logger(__name__)

_DIAGNOSIS_SYSTEM_PROMPT = """You are a senior software engineer specializing in web scraper
maintenance. A scraper has failed validation. Analyze the evidence and produce
a structured remediation report.

The report MUST include:
1. **Diagnosis**: What broke and why (DOM change, auth flow change, etc.)
2. **Root cause**: The specific change on the site that caused the failure
3. **Affected files**: Which code files need changes
4. **Steps**: Ordered remediation steps, each with:
   - file_path: which file to edit
   - action: edit|add|delete|rename
   - description: what to do
   - before_pattern: what to look for (if editing)
   - after_guidance: what the result should look like
   - rationale: why this change is needed
   - constraints: what NOT to do (stealth, modularity, etc.)
5. **Test plan**: How to verify the fix works

CRITICAL CONSTRAINTS:
- Preserve stealth characteristics — no changes that break anti-detection
- Preserve modularity — fixes go in wrapper code, not in core/
- Preserve rate limiting — never speed up or remove delays
- Changes must pass ruff, mypy, and pytest
- Be specific — line-by-line guidance, not vague instructions

Return valid JSON matching the RemediationReport schema."""


async def diagnose(
    validation_report: ValidationReport,
    profile: SiteProfile,
    *,
    llm: LLMClient,
    fixture_store: FixtureStore | None = None,
    wrapper_code: str = "",
    reports_dir: str | Path = "data/heal_reports",
) -> RemediationReport:
    """Stage 1: Diagnose a scraper failure and produce a remediation report.

    Does NOT make any code changes. The report is emailed to the user
    for review and optional approval.
    """
    t_start = time.monotonic()

    fresh_fixture_html = ""
    if fixture_store:
        html = fixture_store.load_fixture_html(profile.site_id)
        if html:
            fresh_fixture_html = html[:5000]

    evidence = {
        "validation_report": validation_report.model_dump(mode="json"),
        "site_profile": profile.model_dump(mode="json"),
        "fresh_fixture_html_preview": fresh_fixture_html,
        "wrapper_code": wrapper_code[:4000],
    }

    user_prompt = f"""A scraper for site '{profile.site_id}' has failed validation.

Validation summary: {validation_report.summary}
Drift detected: {validation_report.drift_detected}
Fingerprint similarity: {validation_report.fingerprint_similarity}
Schema violations: {len(validation_report.field_violations)}
Coverage met: {validation_report.coverage_met}

Evidence:
{json.dumps(evidence, indent=2, default=str)[:8000]}

Produce a detailed RemediationReport with step-by-step instructions to fix this scraper."""

    try:
        response_text: str = await llm.complete(
            prompt=user_prompt,
            system=_DIAGNOSIS_SYSTEM_PROMPT,
            max_tokens=4096,
        )

        text = response_text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]

        report_dict = json.loads(text)
        fixture_path = ""
        if fixture_store:
            latest_path = fixture_store.latest_fixture_path(profile.site_id)
            fixture_path = str(latest_path) if latest_path else ""

        report = RemediationReport(
            site_id=profile.site_id,
            diagnosis=report_dict.get("diagnosis", ""),
            severity=report_dict.get("severity", "medium"),
            confidence=report_dict.get("confidence", 0.5),
            root_cause=report_dict.get("root_cause", ""),
            affected_files=report_dict.get("affected_files", []),
            steps=[RemediationStep(**step) for step in report_dict.get("steps", [])],
            guardrails=report_dict.get("guardrails", []),
            test_plan=report_dict.get("test_plan", ""),
            estimated_tokens=len(response_text) // 4,
            fresh_fixture_path=fixture_path,
        )

    except Exception as exc:
        logger.error(
            "heal.diagnosis_failed",
            site_id=profile.site_id,
            error=str(exc)[:300],
        )
        report = RemediationReport(
            site_id=profile.site_id,
            diagnosis=f"Diagnosis failed: {str(exc)[:500]}",
            severity="high",
            confidence=0.0,
            root_cause="unknown",
            affected_files=[],
            steps=[],
        )

    reports_path = Path(reports_dir)
    reports_path.mkdir(parents=True, exist_ok=True)
    report_file = reports_path / f"{report.heal_id}_{profile.site_id}.json"
    report_file.write_text(
        report.model_dump_json(indent=2),
        encoding="utf-8",
    )

    elapsed_ms = (time.monotonic() - t_start) * 1000
    logger.info(
        "heal.diagnosis_complete",
        site_id=profile.site_id,
        heal_id=report.heal_id,
        severity=report.severity,
        steps=len(report.steps),
        elapsed_ms=round(elapsed_ms, 1),
    )
    return report


async def send_diagnosis_email(
    report: RemediationReport,
    *,
    to_email: str = "",
    from_email: str = "",
) -> bool:
    """Email the diagnosis report to the user.

    The email includes the diagnosis, root cause, and step list.
    The user can reply "fix" to trigger remediation.
    """
    from core.config import get_settings
    from core.notifications import send_notification

    settings = get_settings()
    to_addr = to_email or settings.notification_to_email
    from_addr = from_email or settings.notification_from_email

    if not to_addr or not from_addr:
        logger.warning("heal.no_email_configured")
        return False

    subject = f"[KP Heal] Scraper diagnosis for {report.site_id} — {report.severity}"
    try:
        trigger_token = build_heal_trigger_token(report.heal_id)
    except ValueError:
        logger.warning("heal.trigger_secret_missing", heal_id=report.heal_id)
        return False

    steps_text = "\n".join(
        f"  {s.order}. [{s.action}] {s.file_path}: {s.description}" for s in report.steps
    )

    body = f"""Scraper Diagnosis Report
========================
Heal ID: {report.heal_id}
Heal Token: {trigger_token}
Site: {report.site_id}
Severity: {report.severity}
Confidence: {report.confidence:.0%}
Created: {report.created_at.isoformat()}

Diagnosis:
{report.diagnosis}

Root Cause:
{report.root_cause}

Remediation Steps ({len(report.steps)}):
{steps_text}

Test Plan:
{report.test_plan}

Guardrails:
{chr(10).join(f"  - {g}" for g in report.guardrails)}

---
Reply with exactly:
fix

And include this token line in your reply body:
Heal Token: {trigger_token}

This signed token prevents spoofed or replayed remediation triggers.
Do not reply if you want to fix manually or ignore.
"""

    try:
        await send_notification(
            to_email=to_addr,
            subject=subject,
            body=body,
        )
        logger.info(
            "heal.email_sent",
            heal_id=report.heal_id,
            to=to_addr,
        )
        return True
    except Exception as exc:
        logger.error(
            "heal.email_failed",
            heal_id=report.heal_id,
            error=str(exc)[:200],
        )
        return False


def load_report(report_path: str | Path) -> RemediationReport:
    """Load a saved remediation report from disk."""
    path = Path(report_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    return RemediationReport(**data)


def was_already_attempted(heal_id: str, reports_dir: str | Path = "data/heal_reports") -> bool:
    """Check if this heal_id has already been attempted (anti-loop)."""
    path = Path(reports_dir) / f"{heal_id}_attempted.flag"
    return path.exists()


def mark_attempted(heal_id: str, reports_dir: str | Path = "data/heal_reports") -> None:
    """Mark a heal_id as attempted."""
    path = Path(reports_dir)
    path.mkdir(parents=True, exist_ok=True)
    (path / f"{heal_id}_attempted.flag").write_text(
        datetime.now(UTC).isoformat(),
        encoding="utf-8",
    )
