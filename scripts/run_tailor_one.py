"""One-off: tailor a resume for a single job listing.

This mirrors what the LangGraph pipeline passes into ``tailoring_node`` after
filtering: ``JobAgentState`` with ``qualified_listings`` populated (same
``JobListing`` model as discovery/filtering).

Run from repo root with the venv active::

    python scripts/run_tailor_one.py

Requires ``KP_ANTHROPIC_API_KEY`` and a master profile at
``KP_RESUME_MASTER_PROFILE_PATH`` (default: pipelines/job_agent/context/candidate_profile.yaml).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Allow ``python /path/to/run_tailor_one.py`` when the package isn't on PYTHONPATH
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.observability import setup_logging  # noqa: E402
from pipelines.job_agent.models import JobListing, JobSource, SearchCriteria  # noqa: E402
from pipelines.job_agent.nodes.tailoring import tailoring_node  # noqa: E402
from pipelines.job_agent.state import JobAgentState  # noqa: E402

_BOSTON_DYNAMICS_JD = """
The Head of Strategic Programs & Business Operations PMO owns and evolves the company's enterprise program management capability, ensuring the most critical cross-functional business initiatives are prioritized, structured, and executed with discipline.

This role leads enterprise programs that shape how the company operates, spanning new business initiatives, operating model shifts, major tech stack projects, and the redesign of critical cross-functional workflows that underpin scalable growth.

This leader builds and runs a PMO that translates strategy into coordinated execution across GTM, Finance, Product, Manufacturing, Supply Chain, Services, and IT. The role remains hands-on, personally leading the highest-impact, highest-complexity cross-functional programs.

Key Responsibilities

BizOps PMO Strategy & Operating Model
- Own the Business Operations PMO charter, operating model, and delivery standards.
- Define how cross-functional initiatives are intake-managed, prioritized, staffed, and governed across the company.
- Clarify roles across Program Management, Business Users, Business Process, Enablement, Analytics, and IT to ensure coordinated delivery.
- Scale the PMO operating model as the company grows, maintaining clarity, rigor, and accountability.

Portfolio Management & Prioritization
- Own the portfolio of BizOps-led initiatives, assigning programs to PMO team members while retaining direct ownership of select, high-complexity initiatives.
- Lead cross-functional prioritization processes, balancing business impact, risk, urgency, and delivery capacity.
- Provide executive visibility into initiative status, dependencies, and tradeoffs, particularly where process decisions drive system scope.

Program & Initiative Execution (Direct Ownership)
- Personally lead complex, cross-functional programs, driving end-to-end execution across operating model design, policy definition, process architecture, system requirements, build, testing, change management, and rollout.
- Ensure programs have clear scope, success metrics, governance, and change management plans.
- Partner closely with Business Process Analysts to ensure operating models and workflows are clearly defined prior to downstream enablement or system build.
- Partner with Finance and Internal Controls to ensure programs support audit readiness and enterprise governance standards.
- Partner with IT and Enterprise Applications to ensure technology solutions align to intended business outcomes.

PMO Team Leadership & Development
- Build, lead, coach, and develop the Business Operations Program Management team.
- Set expectations for managing enterprise-scale, cross-functional programs with high organizational visibility and risk.
- Build capability in structured dependency management, risk mitigation, stakeholder alignment, and executive communication.
- Review program plans and execution quality across the team's portfolio to ensure rigor and consistency.
- Support hiring, onboarding, performance management, and career development for PMO team members.
- Continuously raise the bar for execution maturity, predictability, and governance across BizOps initiatives.

Governance, Cadence & Executive Communication
- Establish and run operating cadences for enterprise program reviews, cross-functional checkpoints, and executive steering committees.
- Ensure programs adhere to governance standards, decision rights, approval processes, and compliance requirements, particularly for initiatives tied to financial controls or IPO readiness.
- Create concise, executive-ready communication that highlights progress against business outcomes, risk exposure, structural capacity constraints, and decisions required.
- Drive accountability through clear ownership, transparent reporting, and disciplined follow-through.

Qualifications
- Bachelor's degree in business, operations, engineering, or a related field.
- 8-12+ years of experience in program management, business operations, systems implementation, or transformation roles.
- Demonstrated experience standing up new business units and leading enterprise-wide initiatives.
- Demonstrated experience managing and developing program management teams.
- Proven track record of personally leading complex, cross-functional initiatives from concept through adoption.
- Strong understanding of operating model design, business process architecture, and how these translate into scalable systems and governance frameworks.
- Ability to influence senior stakeholders and drive decisions without direct authority.
- Excellent organizational, communication, and executive presentation skills.
- Sound judgment in balancing rigor with pragmatism.
- Experience leading enterprise system implementations (e.g., CRM, ERP, PLM, MRP).
- Familiarity with internal controls and audit-driven programs.
""".strip()


async def main() -> None:
    setup_logging()

    listing = JobListing(
        title="Head of Strategic Programs & Business Operations PMO",
        company="Boston Dynamics",
        location="Waltham, MA",
        url="https://www.linkedin.com/jobs/view/4383883967/",
        source=JobSource.LINKEDIN,
        description=_BOSTON_DYNAMICS_JD,
        salary_min=None,
        salary_max=None,
        remote=None,
        dedup_key="linkedin_4383883967",
    )

    state = JobAgentState(
        search_criteria=SearchCriteria(),
        qualified_listings=[listing],
        run_id="one-job-run",
        dry_run=False,
    )

    out = await tailoring_node(state)

    if out.errors:
        print("Errors:", out.errors, file=sys.stderr)
        sys.exit(1)

    path = out.qualified_listings[0].tailored_resume_path
    print("Wrote:", path)


if __name__ == "__main__":
    asyncio.run(main())
