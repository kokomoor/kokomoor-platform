"""Resume tailoring subsystem.

Provides profile loading, plan application, and .docx rendering
for the multi-phase resume tailoring node.
"""

from pipelines.job_agent.resume.applier import apply_tailoring_plan
from pipelines.job_agent.resume.profile import format_profile_for_llm, load_master_profile
from pipelines.job_agent.resume.renderer import render_resume_docx

__all__ = [
    "apply_tailoring_plan",
    "format_profile_for_llm",
    "load_master_profile",
    "render_resume_docx",
]
