"""
Contact discovery for outreach.

Design rules, and the reasoning behind each — these are not decoration, they are
what keeps your sending domain alive:

1. NO PATTERN-GUESS-AND-SEND. Guessing first.last@company.com and blasting it is
   the single fastest way to destroy your domain reputation. Every hard bounce is
   a spam signal; a handful of them and your *real* mail (recruiter replies,
   interview scheduling) starts landing in spam. This module will only return an
   email address that a verification provider marks deliverable.

2. NO PROFILE SCRAPING. We do not scrape LinkedIn, GitHub or anywhere else for
   personal contact details. Compiling personal data on individuals without a
   lawful basis is a different activity from applying for a job, and it is also
   not what actually gets replies.

3. COMPANY ROUTE FIRST. The ATS apply URL and a published careers/recruiting
   alias convert better than a cold personal email, because they put you into the
   pipeline the hiring team actually reads. This module always returns those.

4. LOW VOLUME, HIGH SPECIFICITY. Ten researched emails beat four hundred sprayed
   ones, and only the first approach is legal to do at scale under CAN-SPAM /
   GDPR without an unsubscribe mechanism and a lawful basis.

Providers are optional. With no API key configured you still get the company route,
which is the part that matters most.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, asdict
from typing import Any
from urllib.parse import urlparse

import httpx

HUNTER_KEY = os.getenv("HUNTER_API_KEY", "")
TIMEOUT = httpx.Timeout(20.0, connect=10.0)

# Roles worth approaching for an IC engineering role, in priority order.
RELEVANT_SENIORITY = ("senior", "executive")
RELEVANT_DEPARTMENTS = ("engineering", "it", "executive", "hr")

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_ALIAS_HINTS = ("careers", "jobs", "recruiting", "talent", "hiring", "people")


@dataclass
class Contact:
    name: str = ""
    role: str = ""
    email: str = ""
    status: str = "unverified"     # verified | risky | unverified | company_alias
    confidence: int | None = None
    source: str = ""
    note: str = ""

    def dict(self) -> dict[str, Any]:
        return asdict(self)


def domain_from(job: dict[str, Any]) -> str | None:
    """Best-effort company domain from an apply URL. ATS domains are not the company."""
    url = job.get("apply_url") or ""
    host = urlparse(url).netloc.lower()
    ats_hosts = ("greenhouse.io", "lever.co", "ashbyhq.com", "smartrecruiters.com",
                 "recruitee.com", "workable.com", "myworkdayjobs.com")
    if host and not any(a in host for a in ats_hosts):
        return host.removeprefix("www.")
    return None


async def company_route(job: dict[str, Any]) -> list[Contact]:
    """Zero-risk contact surfaces that always exist. Try these before cold email."""
    out = [Contact(
        role="Application (ATS)",
        email="",
        status="company_alias",
        source=job.get("apply_url", ""),
        note="Primary route. Applying here puts you in the pipeline the hiring "
             "team actually reviews; cold email works best as a *follow-up* to "
             "this, not a replacement for it.",
    )]
    # Careers aliases published in the posting itself are fair game — the company
    # put them there to be contacted.
    for m in _EMAIL_RE.finditer(job.get("description", "") or ""):
        addr = m.group(0).lower()
        if any(h in addr.split("@")[0] for h in _ALIAS_HINTS):
            out.append(Contact(
                role="Published recruiting alias",
                email=addr,
                status="company_alias",
                source="job description",
                note="Listed in the posting itself.",
            ))
    return out


async def hunter_contacts(domain: str, limit: int = 8) -> tuple[list[Contact], str | None]:
    """
    Query Hunter.io domain-search, filtered to hiring-relevant roles.

    Hunter is used rather than a scraper because it maintains its own opt-out and
    GDPR handling, and — the practical part — it returns a deliverability verdict,
    so we can drop addresses that would bounce.
    """
    if not HUNTER_KEY:
        return [], ("no HUNTER_API_KEY configured — set one to enable verified "
                    "contact lookup, or use the company route above")

    params = {
        "domain": domain,
        "api_key": HUNTER_KEY,
        "limit": str(limit * 3),
        "department": ",".join(RELEVANT_DEPARTMENTS),
    }
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        r = await client.get("https://api.hunter.io/v2/domain-search", params=params)
        if r.status_code == 401:
            return [], "Hunter rejected the API key"
        if r.status_code == 429:
            return [], "Hunter rate limit reached"
        r.raise_for_status()
        data = r.json().get("data", {})

    contacts: list[Contact] = []
    for e in data.get("emails", []):
        status = (e.get("verification") or {}).get("status") or e.get("status") or "unknown"
        # Rule 1: only deliverable addresses leave this function.
        if status not in ("valid", "deliverable", "accept_all"):
            continue
        name = " ".join(filter(None, [e.get("first_name"), e.get("last_name")])).strip()
        contacts.append(Contact(
            name=name,
            role=e.get("position") or e.get("department") or "",
            email=e.get("value", ""),
            status="verified" if status in ("valid", "deliverable") else "risky",
            confidence=e.get("confidence"),
            source="hunter.io",
            note="accept-all domain: deliverable but unconfirmed recipient"
                 if status == "accept_all" else "",
        ))

    def rank(c: Contact) -> tuple[int, int]:
        role = c.role.lower()
        pri = 3
        if any(k in role for k in ("engineering manager", "head of engineering",
                                   "director of engineering", "vp engineering",
                                   "hiring manager")):
            pri = 0
        elif any(k in role for k in ("machine learning", "ml ", "ai ", "data science",
                                     "staff engineer", "principal engineer", "cto")):
            pri = 1
        elif any(k in role for k in ("recruit", "talent", "people", "hr")):
            pri = 2
        return (pri, -(c.confidence or 0))

    contacts.sort(key=rank)
    return contacts[:limit], None


async def find_contacts(job: dict[str, Any], domain: str | None = None,
                        limit: int = 8) -> dict[str, Any]:
    route = await company_route(job)
    dom = domain or domain_from(job)
    people: list[Contact] = []
    warning = None

    if dom:
        try:
            people, warning = await hunter_contacts(dom, limit=limit)
        except Exception as exc:
            warning = f"contact lookup failed: {type(exc).__name__}"
    else:
        warning = ("could not resolve a company domain from the apply URL "
                   "(it points at the ATS) — pass domain= explicitly")

    return {
        "company": job.get("company"),
        "domain": dom,
        "company_route": [c.dict() for c in route],
        "people": [c.dict() for c in people],
        "warning": warning,
        "guidance": (
            "Apply through the ATS link first. Send at most one cold email per "
            "company and one follow-up a week later. Never email an address that "
            "is not marked verified — bounces damage your sending domain, which "
            "you need working for the recruiter replies you actually want."
        ),
    }
