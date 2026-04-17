"""Regression tests for schema v2: tiers, anchors, recast validation, supplementary projects.

These tests guard the architectural guarantees introduced in the resume
pipeline v2 redesign:

- Pinned sections are auto-inserted into the tailored document even when
  the LLM plan omits them (work-history spine is fixed).
- Anchor bullets are auto-prepended within their section even when the
  LLM plan omits them (load-bearing bullets always render).
- Optional sections only appear when selected by the plan (or filtered
  in via tag match in the LLM view).
- `recast` (and deprecated `rewrite`) ops are validated for length parity
  and entity grounding; violations silently fall back to `keep`.
- Supplementary projects render under Additional Information when the
  plan selects them, and never enter the experience block.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from pipelines.job_agent.models.resume_tailoring import (
    BulletOp,
    MasterBullet,
    MasterEducation,
    MasterExperience,
    MasterSkills,
    ResumeMasterProfile,
    ResumeTailoringPlan,
    SectionPlan,
    SupplementaryProject,
    TailoredResumeDocument,
)
from pipelines.job_agent.resume.applier import apply_tailoring_plan
from pipelines.job_agent.resume.profile import format_profile_for_llm, load_master_profile
from pipelines.job_agent.resume.renderer import render_resume_docx


# ── helpers ───────────────────────────────────────────────────────────


def _bullet(
    bid: str,
    text: str,
    *,
    tags: list[str] | None = None,
    anchor: bool = False,
    source_material: str = "",
    variants: dict[str, str] | None = None,
) -> MasterBullet:
    return MasterBullet(
        id=bid,
        text=text,
        tags=tags or [],
        anchor=anchor,
        source_material=source_material,
        variants=variants or {},
    )


def _experience(
    eid: str,
    company: str,
    *,
    title: str = "Test Role",
    dates: str = "2020-2024",
    tier: str = "pinned",
    bullets: list[MasterBullet] | None = None,
) -> MasterExperience:
    return MasterExperience(
        id=eid,
        company=company,
        title=title,
        dates=dates,
        tier=tier,  # type: ignore[arg-type]
        bullets=bullets or [],
    )


def _make_profile(
    experience: list[MasterExperience] | None = None,
    supplementary: list[SupplementaryProject] | None = None,
) -> ResumeMasterProfile:
    return ResumeMasterProfile(
        schema_version=2,
        name="Test Candidate",
        location="Boston, MA",
        email="t@example.com",
        phone="555-0100",
        linkedin="",
        github="",
        clearance="",
        education=[
            MasterEducation(
                id="edu_test",
                school="Test University",
                degree="BS Test",
                graduation="2020",
                bullets=[_bullet("edu_bullet_1", "Some coursework")],
            )
        ],
        experience=experience or [],
        skills=MasterSkills(languages=["Python"]),
        supplementary_projects=supplementary or [],
    )


# ── Tier enforcement ──────────────────────────────────────────────────


class TestPinnedSectionAutoInsert:
    """Pinned sections must always appear — even when the plan omits them."""

    def test_pinned_section_appears_when_plan_is_empty(self) -> None:
        """Plan with no experience_sections still gets pinned sections."""
        profile = _make_profile(
            experience=[
                _experience(
                    "exp_a",
                    "Pinned Co",
                    tier="pinned",
                    bullets=[_bullet("a1", "Work at Pinned Co", anchor=True)],
                ),
            ]
        )
        plan = ResumeTailoringPlan(
            summary="x",
            experience_sections=[],
            education_sections=[],
            bullet_ops=[],
            skills_to_highlight=[],
        )
        doc = apply_tailoring_plan(profile, plan)
        assert any(e.company == "Pinned Co" for e in doc.experience)

    def test_optional_section_absent_from_plan_is_excluded(self) -> None:
        profile = _make_profile(
            experience=[
                _experience(
                    "exp_opt",
                    "Optional Co",
                    tier="optional",
                    bullets=[_bullet("o1", "Work at Optional Co", tags=["embedded"])],
                ),
            ]
        )
        plan = ResumeTailoringPlan(
            summary="x",
            experience_sections=[],
            education_sections=[],
            bullet_ops=[],
            skills_to_highlight=[],
        )
        doc = apply_tailoring_plan(profile, plan)
        assert not any(e.company == "Optional Co" for e in doc.experience)

    def test_optional_section_present_in_plan_is_included(self) -> None:
        profile = _make_profile(
            experience=[
                _experience(
                    "exp_opt",
                    "Optional Co",
                    tier="optional",
                    bullets=[_bullet("o1", "Some optional work")],
                ),
            ]
        )
        plan = ResumeTailoringPlan(
            summary="x",
            experience_sections=[SectionPlan(section_id="exp_opt", bullet_order=["o1"])],
            education_sections=[],
            bullet_ops=[BulletOp(bullet_id="o1", op="keep")],
            skills_to_highlight=[],
        )
        doc = apply_tailoring_plan(profile, plan)
        assert any(e.company == "Optional Co" for e in doc.experience)

    def test_multiple_pinned_sections_appear_in_profile_order(self) -> None:
        """Profile-declared order is preserved regardless of plan order."""
        profile = _make_profile(
            experience=[
                _experience(
                    "exp_1",
                    "First Co",
                    tier="pinned",
                    bullets=[_bullet("b1", "Work 1")],
                ),
                _experience(
                    "exp_2",
                    "Second Co",
                    tier="pinned",
                    bullets=[_bullet("b2", "Work 2")],
                ),
            ]
        )
        plan = ResumeTailoringPlan(
            summary="x",
            # LLM listed them in reversed order — applier ignores that
            experience_sections=[
                SectionPlan(section_id="exp_2", bullet_order=["b2"]),
                SectionPlan(section_id="exp_1", bullet_order=["b1"]),
            ],
            education_sections=[],
            bullet_ops=[
                BulletOp(bullet_id="b2", op="keep"),
                BulletOp(bullet_id="b1", op="keep"),
            ],
            skills_to_highlight=[],
        )
        doc = apply_tailoring_plan(profile, plan)
        companies = [e.company for e in doc.experience]
        assert companies == ["First Co", "Second Co"]


# ── Anchor enforcement ────────────────────────────────────────────────


class TestAnchorBulletEnforcement:
    """Anchor bullets must always appear within their section."""

    def test_anchor_auto_prepended_when_plan_omits(self) -> None:
        """Plan forgets the anchor; applier inserts it at the front."""
        profile = _make_profile(
            experience=[
                _experience(
                    "exp_a",
                    "Acme",
                    tier="pinned",
                    bullets=[
                        _bullet("anchor_1", "Load-bearing fact", anchor=True),
                        _bullet("norm_1", "Other work"),
                    ],
                ),
            ]
        )
        plan = ResumeTailoringPlan(
            summary="x",
            experience_sections=[
                SectionPlan(section_id="exp_a", bullet_order=["norm_1"]),
            ],
            education_sections=[],
            bullet_ops=[BulletOp(bullet_id="norm_1", op="keep")],
            skills_to_highlight=[],
        )
        doc = apply_tailoring_plan(profile, plan)
        bullet_ids = [b.id for b in doc.experience[0].bullets]
        assert "anchor_1" in bullet_ids
        # Anchor comes first
        assert bullet_ids[0] == "anchor_1"

    def test_anchor_present_in_plan_respected(self) -> None:
        """If plan already includes anchor, applier does not duplicate it."""
        profile = _make_profile(
            experience=[
                _experience(
                    "exp_a",
                    "Acme",
                    tier="pinned",
                    bullets=[
                        _bullet("anchor_1", "Load-bearing fact", anchor=True),
                        _bullet("norm_1", "Other work"),
                    ],
                ),
            ]
        )
        plan = ResumeTailoringPlan(
            summary="x",
            experience_sections=[
                SectionPlan(section_id="exp_a", bullet_order=["anchor_1", "norm_1"]),
            ],
            education_sections=[],
            bullet_ops=[
                BulletOp(bullet_id="anchor_1", op="keep"),
                BulletOp(bullet_id="norm_1", op="keep"),
            ],
            skills_to_highlight=[],
        )
        doc = apply_tailoring_plan(profile, plan)
        bullet_ids = [b.id for b in doc.experience[0].bullets]
        assert bullet_ids == ["anchor_1", "norm_1"]
        assert len([b for b in bullet_ids if b == "anchor_1"]) == 1

    def test_auto_pinned_section_uses_anchors(self) -> None:
        """When a pinned section is absent from plan entirely, the
        auto-inserted version still shows its anchor bullets."""
        profile = _make_profile(
            experience=[
                _experience(
                    "exp_a",
                    "Acme",
                    tier="pinned",
                    bullets=[
                        _bullet("anchor_1", "Anchor fact", anchor=True),
                        _bullet("norm_1", "Non-anchor"),
                    ],
                ),
            ]
        )
        plan = ResumeTailoringPlan(
            summary="x",
            experience_sections=[],
            education_sections=[],
            bullet_ops=[],
            skills_to_highlight=[],
        )
        doc = apply_tailoring_plan(profile, plan)
        assert doc.experience[0].bullets
        ids = [b.id for b in doc.experience[0].bullets]
        assert "anchor_1" in ids


# ── Recast validation ─────────────────────────────────────────────────


class TestRecastValidation:
    """Recast / rewrite ops are validated; failures fall back to `keep`."""

    def test_recast_with_length_parity_accepted(self) -> None:
        profile = _make_profile(
            experience=[
                _experience(
                    "exp_a",
                    "Acme",
                    bullets=[
                        _bullet(
                            "b1",
                            "Led $100M Army RADAR program across firmware and DSP teams",
                            source_material="Program was $100M, RADAR focused, firmware and DSP work across teams.",
                        ),
                    ],
                ),
            ]
        )
        plan = ResumeTailoringPlan(
            summary="x",
            experience_sections=[SectionPlan(section_id="exp_a", bullet_order=["b1"])],
            education_sections=[],
            bullet_ops=[
                BulletOp(
                    bullet_id="b1",
                    op="recast",
                    rewrite_text="Owned firmware and DSP for $100M Army RADAR program across teams",
                ),
            ],
            skills_to_highlight=[],
        )
        doc = apply_tailoring_plan(profile, plan)
        assert "Owned firmware and DSP" in doc.experience[0].bullets[0].text

    def test_recast_exceeding_length_falls_back_to_keep(self) -> None:
        master = (
            "Led $100M Army RADAR program"  # 5 words
        )
        profile = _make_profile(
            experience=[
                _experience(
                    "exp_a",
                    "Acme",
                    bullets=[_bullet("b1", master, source_material=master)],
                ),
            ]
        )
        # 20% tolerance on 5 words → max 7. Propose 15.
        proposed = (
            "Led $100M Army RADAR program with extensive responsibilities and large team scope "
            "for the duration"
        )
        plan = ResumeTailoringPlan(
            summary="x",
            experience_sections=[SectionPlan(section_id="exp_a", bullet_order=["b1"])],
            education_sections=[],
            bullet_ops=[BulletOp(bullet_id="b1", op="recast", rewrite_text=proposed)],
            skills_to_highlight=[],
        )
        doc = apply_tailoring_plan(profile, plan)
        assert doc.experience[0].bullets[0].text == master

    def test_recast_with_ungrounded_entity_falls_back_to_keep(self) -> None:
        """Recast invents a new number not present in source — reject."""
        master = "Led $100M Army RADAR program across firmware and DSP teams"
        source = "Program was $100M, RADAR focused, firmware and DSP work."
        profile = _make_profile(
            experience=[
                _experience(
                    "exp_a",
                    "Acme",
                    bullets=[_bullet("b1", master, source_material=source)],
                ),
            ]
        )
        proposed = "Led $200M Army RADAR program across firmware and DSP teams"
        plan = ResumeTailoringPlan(
            summary="x",
            experience_sections=[SectionPlan(section_id="exp_a", bullet_order=["b1"])],
            education_sections=[],
            bullet_ops=[BulletOp(bullet_id="b1", op="recast", rewrite_text=proposed)],
            skills_to_highlight=[],
        )
        doc = apply_tailoring_plan(profile, plan)
        assert doc.experience[0].bullets[0].text == master

    def test_recast_with_ungrounded_proper_noun_falls_back_to_keep(self) -> None:
        """Recast invents a company/project name not in source — reject."""
        master = "Built $50M revenue pipeline at Acme Corp"
        source = "At Acme Corp, built pipeline generating $50M revenue."
        profile = _make_profile(
            experience=[
                _experience(
                    "exp_a",
                    "Acme Corp",
                    bullets=[_bullet("b1", master, source_material=source)],
                ),
            ]
        )
        proposed = "Built $50M revenue pipeline at Foobar Industries"
        plan = ResumeTailoringPlan(
            summary="x",
            experience_sections=[SectionPlan(section_id="exp_a", bullet_order=["b1"])],
            education_sections=[],
            bullet_ops=[BulletOp(bullet_id="b1", op="recast", rewrite_text=proposed)],
            skills_to_highlight=[],
        )
        doc = apply_tailoring_plan(profile, plan)
        assert doc.experience[0].bullets[0].text == master

    def test_rewrite_alias_uses_same_validation(self) -> None:
        """`rewrite` (deprecated) behaves identically to `recast` for validation."""
        master = "Led $100M program"
        source = "Program size was $100M."
        profile = _make_profile(
            experience=[
                _experience(
                    "exp_a",
                    "Acme",
                    bullets=[_bullet("b1", master, source_material=source)],
                ),
            ]
        )
        plan = ResumeTailoringPlan(
            summary="x",
            experience_sections=[SectionPlan(section_id="exp_a", bullet_order=["b1"])],
            education_sections=[],
            bullet_ops=[
                BulletOp(bullet_id="b1", op="rewrite", rewrite_text="Led $999M program"),
            ],
            skills_to_highlight=[],
        )
        doc = apply_tailoring_plan(profile, plan)
        # Ungrounded entity — falls back to master
        assert doc.experience[0].bullets[0].text == master


# ── Supplementary projects ────────────────────────────────────────────


class TestSupplementaryProjects:
    """Supplementary projects render under Additional Information only."""

    def test_selected_supplementary_project_renders(self) -> None:
        profile = _make_profile(
            experience=[],
            supplementary=[
                SupplementaryProject(
                    id="proj_x",
                    name="Side Project X",
                    url="github.com/candidate/x",
                    text="Personal project description",
                    tags=["ai"],
                )
            ],
        )
        plan = ResumeTailoringPlan(
            summary="x",
            experience_sections=[],
            education_sections=[],
            bullet_ops=[],
            skills_to_highlight=[],
            supplementary_project_ids=["proj_x"],
        )
        doc = apply_tailoring_plan(profile, plan)
        assert len(doc.supplementary_projects) == 1
        assert doc.supplementary_projects[0].name == "Side Project X"

    def test_unselected_supplementary_project_excluded(self) -> None:
        profile = _make_profile(
            experience=[],
            supplementary=[
                SupplementaryProject(
                    id="proj_x",
                    name="Side Project X",
                    text="Personal project",
                )
            ],
        )
        plan = ResumeTailoringPlan(
            summary="x",
            experience_sections=[],
            education_sections=[],
            bullet_ops=[],
            skills_to_highlight=[],
            supplementary_project_ids=[],
        )
        doc = apply_tailoring_plan(profile, plan)
        assert doc.supplementary_projects == []

    def test_supplementary_project_never_enters_experience(self) -> None:
        """A tier=supplementary experience entry is filtered out of Experience."""
        # Use a tier=supplementary experience entry (as would happen if
        # someone misplaced a project into experience). It must NOT render.
        profile = _make_profile(
            experience=[
                _experience(
                    "exp_supp",
                    "Should Not Render",
                    tier="supplementary",
                    bullets=[_bullet("s1", "x")],
                ),
                _experience(
                    "exp_real",
                    "Real Co",
                    tier="pinned",
                    bullets=[_bullet("r1", "Real work")],
                ),
            ]
        )
        plan = ResumeTailoringPlan(
            summary="x",
            experience_sections=[],
            education_sections=[],
            bullet_ops=[],
            skills_to_highlight=[],
        )
        doc = apply_tailoring_plan(profile, plan)
        companies = [e.company for e in doc.experience]
        assert "Should Not Render" not in companies
        assert "Real Co" in companies

    def test_supplementary_project_renders_in_additional_info_docx(self, tmp_path: Path) -> None:
        from docx import Document

        profile = _make_profile(
            experience=[],
            supplementary=[
                SupplementaryProject(
                    id="proj_x",
                    name="Kokomoor Platform",
                    url="github.com/kokomoor/kokomoor-platform",
                    text="Autonomous AI job-application pipeline",
                )
            ],
        )
        plan = ResumeTailoringPlan(
            summary="x",
            experience_sections=[],
            education_sections=[],
            bullet_ops=[],
            skills_to_highlight=[],
            supplementary_project_ids=["proj_x"],
        )
        doc = apply_tailoring_plan(profile, plan)
        out = tmp_path / "resume.docx"
        render_resume_docx(doc, out)

        rendered = Document(str(out))
        text = "\n".join(p.text for p in rendered.paragraphs)
        assert "Autonomous AI job-application pipeline" in text
        assert "github.com/kokomoor/kokomoor-platform" in text


# ── LLM view (format_profile_for_llm) ────────────────────────────────


class TestLLMProfileView:
    """The formatter surfaces tier and anchor markers to the LLM."""

    def test_tier_markers_in_llm_view(self) -> None:
        profile = _make_profile(
            experience=[
                _experience("exp_p", "Pinned Co", tier="pinned", bullets=[_bullet("b1", "x")]),
                _experience("exp_o", "Optional Co", tier="optional", bullets=[_bullet("b2", "y")]),
            ]
        )
        view = format_profile_for_llm(profile)
        assert "(PINNED)" in view
        assert "(OPTIONAL)" in view

    def test_anchor_marker_in_llm_view(self) -> None:
        profile = _make_profile(
            experience=[
                _experience(
                    "exp_a",
                    "Acme",
                    bullets=[
                        _bullet("anch", "Anchored bullet", anchor=True),
                        _bullet("norm", "Plain bullet"),
                    ],
                )
            ]
        )
        view = format_profile_for_llm(profile)
        # Anchor bullet has the marker; non-anchor does not
        anchor_line = next(line for line in view.splitlines() if "[anch]" in line)
        assert "(ANCHOR)" in anchor_line
        norm_line = next(line for line in view.splitlines() if "[norm]" in line)
        assert "(ANCHOR)" not in norm_line

    def test_source_material_inlined_when_present(self) -> None:
        profile = _make_profile(
            experience=[
                _experience(
                    "exp_a",
                    "Acme",
                    bullets=[
                        _bullet(
                            "b1",
                            "Short bullet",
                            source_material="Long prose describing the work in rich detail.",
                        ),
                    ],
                )
            ]
        )
        view = format_profile_for_llm(profile)
        assert "source_material" in view
        assert "Long prose describing the work in rich detail." in view

    def test_supplementary_projects_appear_in_view(self) -> None:
        profile = _make_profile(
            experience=[],
            supplementary=[
                SupplementaryProject(
                    id="proj_x",
                    name="Personal Thing",
                    text="Built a personal thing",
                    tags=["ai"],
                )
            ],
        )
        view = format_profile_for_llm(profile)
        assert "SUPPLEMENTARY PROJECTS" in view
        assert "[proj_x]" in view
        assert "Personal Thing" in view

    def test_optional_section_hidden_when_no_tag_match(self) -> None:
        profile = _make_profile(
            experience=[
                _experience(
                    "exp_opt",
                    "Optional Co",
                    tier="optional",
                    bullets=[_bullet("b1", "x", tags=["embedded"])],
                ),
            ]
        )
        view = format_profile_for_llm(profile, relevant_tags={"ml", "ai"})
        assert "Optional Co" not in view

    def test_pinned_section_visible_regardless_of_tags(self) -> None:
        profile = _make_profile(
            experience=[
                _experience(
                    "exp_p",
                    "Pinned Co",
                    tier="pinned",
                    bullets=[_bullet("b1", "x", tags=["embedded"])],
                ),
            ]
        )
        view = format_profile_for_llm(profile, relevant_tags={"ml", "ai"})
        assert "Pinned Co" in view


# ── Real candidate profile integration ───────────────────────────────


class TestRealCandidateProfile:
    """Sanity checks against the production candidate_profile.yaml."""

    def _load(self) -> ResumeMasterProfile:
        path = Path("pipelines/job_agent/context/candidate_profile.yaml")
        return load_master_profile(path)

    def test_profile_loads_and_has_expected_structure(self) -> None:
        profile = self._load()
        assert profile.schema_version == 2
        assert len(profile.experience) >= 5
        assert len(profile.supplementary_projects) >= 1

    def test_sigcom_is_optional(self) -> None:
        profile = self._load()
        sigcom = next(
            (e for e in profile.experience if "Signal Communications" in e.company),
            None,
        )
        assert sigcom is not None
        assert sigcom.tier == "optional"

    def test_core_employers_are_pinned(self) -> None:
        profile = self._load()
        pinned_names = {e.company for e in profile.experience if e.tier == "pinned"}
        # Load-bearing work history
        assert any("Lincoln" in n for n in pinned_names)
        assert any("Electric Boat" in n for n in pinned_names)
        assert any("Gauntlet" in n for n in pinned_names)

    def test_kokomoor_platform_is_supplementary(self) -> None:
        profile = self._load()
        proj = profile.get_supplementary_project("proj_kokomoor_platform")
        assert proj is not None
        assert proj.name == "Kokomoor Platform"

    def test_pinned_sections_have_anchor_bullets(self) -> None:
        """Every pinned experience has at least one anchor bullet."""
        profile = self._load()
        for exp in profile.experience:
            if exp.tier == "pinned":
                anchor_count = sum(1 for b in exp.bullets if b.anchor)
                assert anchor_count >= 1, f"{exp.company} pinned but has no anchor"

    def test_applying_empty_plan_still_produces_real_resume(self) -> None:
        """Regression: the tier-guarantee path must produce a usable resume
        even if the LLM catastrophically fails and returns an empty plan."""
        profile = self._load()
        plan = ResumeTailoringPlan(
            summary="Fallback summary",
            experience_sections=[],
            education_sections=[],
            bullet_ops=[],
            skills_to_highlight=[],
        )
        doc = apply_tailoring_plan(profile, plan)
        # All pinned sections surface with anchored bullets.
        pinned_expected = {e.company for e in profile.experience if e.tier == "pinned"}
        rendered = {e.company for e in doc.experience}
        assert pinned_expected.issubset(rendered)
        # Each pinned section contributes at least one bullet (the anchor).
        for e in doc.experience:
            assert len(e.bullets) >= 1, f"{e.company} rendered with no bullets"


# ── Backward compat ───────────────────────────────────────────────────


class TestSchemaV1BackwardCompat:
    """v1 YAML (no tier/anchor/source_material) must still load and work."""

    def test_v1_yaml_loads_with_default_tiers(self, tmp_path: Path) -> None:
        """v1 fixture has no tier fields; loader defaults them to 'pinned'."""
        v1 = {
            "schema_version": 1,
            "name": "V1 Candidate",
            "location": "Test City",
            "email": "t@t.com",
            "phone": "555",
            "linkedin": "",
            "github": "",
            "clearance": "",
            "education": [
                {
                    "id": "edu_1",
                    "school": "U1",
                    "degree": "BS",
                    "graduation": "2020",
                    "bullets": [{"id": "e1", "text": "Coursework", "tags": []}],
                }
            ],
            "experience": [
                {
                    "id": "exp_1",
                    "company": "Old Co",
                    "title": "Engineer",
                    "dates": "2020",
                    "bullets": [{"id": "b1", "text": "Some work", "tags": []}],
                }
            ],
            "skills": {"languages": ["Python"]},
        }
        path = tmp_path / "v1.yaml"
        path.write_text(yaml.safe_dump(v1))
        profile = load_master_profile(path)
        assert profile.experience[0].tier == "pinned"
        assert profile.experience[0].bullets[0].anchor is False
        assert profile.experience[0].bullets[0].source_material == ""
