"""
Outreach + tailoring briefs.

This module deliberately does NOT write the email and does NOT send it.

Not writing it: the model reading this tool's output is a better writer than any
template. Handing it structured evidence (what the job asks for, what you have
that maps to it, what the gap is) produces a specific email. A mail-merge template
produces the thing recruiters delete.

Not sending it: sending is the irreversible step. It stays a human action, in your
own mail client, one message at a time. That is a deliverability decision as much
as anything else — cold volume from a personal domain is how you end up in spam
folders for the rest of your search.
"""

from __future__ import annotations

from typing import Any


def tailoring_brief(job: dict[str, Any], score: dict[str, Any],
                    profile: dict[str, Any]) -> dict[str, Any]:
    """Everything needed to tailor a resume to one posting — the gap, not the prose."""
    missing = score.get("missing_keywords", [])
    matched = score.get("matched_keywords", [])

    return {
        "job": {
            "job_id": job.get("job_id"),
            "title": job.get("title"),
            "company": job.get("company"),
            "location": job.get("location"),
            "salary": job.get("salary"),
            "apply_url": job.get("apply_url"),
        },
        "score": score.get("score"),
        "breakdown": score.get("breakdown"),
        "emphasize": matched[:20],
        "gaps": missing[:20],
        "flags": score.get("flags", []),
        "experience_note": score.get("experience_note"),
        "instructions": [
            "Reorder existing bullets so the terms under 'emphasize' appear in the "
            "top third of the resume — that is the part a human actually reads.",
            "For each item under 'gaps': include it ONLY if you have real experience "
            "with it. If you do, surface the bullet that already demonstrates it and "
            "use the job's vocabulary for it. If you don't, leave it out.",
            "Never add a skill, tool, metric, employer or date you cannot defend in "
            "an interview. A tailored resume changes emphasis and wording; it does "
            "not change facts.",
            "Keep the one-page LaTeX layout and styling intact.",
        ],
        "job_description": job.get("description", "")[:6000],
    }


def outreach_brief(job: dict[str, Any], score: dict[str, Any],
                   profile: dict[str, Any], contact: dict[str, Any] | None) -> dict[str, Any]:
    """Structured material for writing one specific cold email."""
    evidence = []
    for kw in score.get("matched_keywords", [])[:12]:
        evidence.append(kw)

    return {
        "job": {
            "title": job.get("title"),
            "company": job.get("company"),
            "apply_url": job.get("apply_url"),
            "department": job.get("department"),
        },
        "contact": contact or {"note": "no verified individual — use the company route"},
        "your_relevant_evidence": evidence,
        "profile_headline": {
            "years": profile.get("years_experience"),
            "degree": profile.get("degree"),
            "field": profile.get("field_of_study"),
            "top_skills": (profile.get("skills") or [])[:12],
        },
        "job_description_excerpt": (job.get("description", "") or "")[:2500],
        "writing_guidance": [
            "Under 150 words. Recruiters and EMs read on a phone.",
            "Open with the specific thing about THIS role or team — not 'I came "
            "across your posting'. If you can't name something specific, you have "
            "not researched enough to justify sending it.",
            "One concrete, quantified proof point that maps to their stated need.",
            "State plainly that you have already applied, and link the posting.",
            "One clear ask: a 15-minute call, or a pointer to the right person.",
            "If work authorisation matters for the role, say your status in one "
            "neutral clause rather than letting them guess.",
            "No attachments on a first cold email — they trip spam filters. Link "
            "instead.",
        ],
        "hard_rules": [
            "This tool drafts only. You review and send from your own mail client.",
            "One email per company, plus at most one follow-up after ~7 days.",
            "If they say no or don't reply twice, stop. That company is done.",
            "Never send to an address not marked 'verified'.",
        ],
    }
