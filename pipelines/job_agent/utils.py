"""Shared utilities for the job-agent pipeline."""

from __future__ import annotations

_ALWAYS_RELEVANT_TAGS = {"leadership", "technical", "general", "management", "software"}

_TAG_EXPANSION: dict[str, list[str]] = {
    "military": ["defense", "naval"],
    "government": ["defense"],
    "aerospace": ["defense"],
    "robotics": ["technical", "hardware", "software"],
    "data": ["ml", "ai"],
    "machine learning": ["ml", "ai"],
    "fintech": ["finance"],
    "trading": ["finance"],
    "quant": ["finance", "math"],
    "nuclear": ["energy"],
    "clean": ["energy"],
    "climate": ["energy"],
    "product": ["startup"],
    "growth": ["startup"],
}


def safe_filename(company: str, title: str, dedup_key: str) -> str:
    """Build a filesystem-safe filename from listing fields."""
    raw = f"{company}_{title}".replace(" ", "_")
    safe = "".join(c for c in raw if c.isalnum() or c in ("_", "-"))
    return f"{safe[:50]}_{dedup_key[:8]}"


def expand_domain_tags(domain_tags: list[str]) -> set[str]:
    """Build the set of profile tags relevant to a job's domain."""
    tags = {t.lower() for t in domain_tags}
    expanded = set(tags)
    for tag in tags:
        expanded.update(_TAG_EXPANSION.get(tag, []))
    expanded.update(_ALWAYS_RELEVANT_TAGS)
    return expanded


def positioning_rules(domain_tags: list[str]) -> str:
    """Select positioning guidance based on job domain tags."""
    tags = {t.lower() for t in domain_tags}
    rules: list[str] = []

    if tags & {"defense", "military", "government", "aerospace"}:
        rules.append("- For defense roles: lead with clearance, Lincoln Lab, Electric Boat.")
    if tags & {"tech", "software", "engineering", "saas"}:
        rules.append("- For tech roles: lead with technical depth, startup, MIT Sloan.")
    if tags & {"energy", "nuclear", "clean", "climate"}:
        rules.append("- For energy roles: lead with nuclear coursework, systems engineering.")
    if tags & {"quant", "finance", "trading", "fintech"}:
        rules.append("- For quant roles: lead with math, probability, FinTech ML.")
    if tags & {"ai", "ml", "data", "machine learning"}:
        rules.append("- For AI/ML roles: lead with GenAI Lab, Spyglass pipeline, ML coursework.")
    if tags & {"startup", "product", "growth"}:
        rules.append("- For startup/product roles: lead with Gauntlet-42, MIT Co-ops, MBA.")

    if not rules:
        rules.append("- Position the candidate's strongest and most relevant experience first.")

    return "\n".join(rules)
