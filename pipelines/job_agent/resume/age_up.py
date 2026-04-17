"""Age-up profile transform for senior-audience resume and cover letter generation.

Contours the candidate profile to present the same factual experience in a way
that reads as mid-to-senior rather than early-career. No information is fabricated;
only presentation choices change:

  - Electric Boat (exp_eb): title drops the "Engineering Intern," prefix, presenting
    four years of engineering progression (Engineer I to II, 2019-2023). The
    eb_intern transition-note bullet is excluded since it describes the intern phase.

IMPORTANT: this function calls model_copy(deep=True) before any mutation. The
master profile is cached in memory (resume/profile.py _PROFILE_CACHE) and the
cached instance must not be modified in-place, or subsequent same-process calls
that expect the standard profile will receive the aged-up version instead.
"""

from __future__ import annotations

from pipelines.job_agent.models.resume_tailoring import MasterExperience, ResumeMasterProfile

_EB_EXPERIENCE_ID = "exp_eb"
_EB_AGE_UP_TITLE = "Engineer I, Engineer II"
_EB_EXCLUDE_BULLETS: frozenset[str] = frozenset({"eb_intern"})


def age_up_profile(profile: ResumeMasterProfile) -> ResumeMasterProfile:
    """Return a deep copy of *profile* with seniority-contoured EB entry.

    The returned object is independent of the cached master profile — safe to
    modify, pass to LLM formatters, and use as renderer input without affecting
    any other pipeline node in the same process.
    """
    copied = profile.model_copy(deep=True)
    new_experience: list[MasterExperience] = []
    for exp in copied.experience:
        if exp.id == _EB_EXPERIENCE_ID:
            exp.title = _EB_AGE_UP_TITLE
            exp.bullets = [b for b in exp.bullets if b.id not in _EB_EXCLUDE_BULLETS]
        new_experience.append(exp)
    copied.experience = new_experience
    return copied
