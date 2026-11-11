"""
Job sources.

Every endpoint here is a *public, documented, no-auth* job-board API published by
the ATS vendor for exactly this purpose (syndicating a company's own postings).
No scraping, no proxies, no login walls, no ToS grey area.

Adding a source = write one `_fetch_*` coroutine returning list[Job] and register
it in ADAPTERS.
"""

from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Iterable

import httpx

USER_AGENT = "jobsearch-mcp/1.0 (personal job search; contact: set OWNER_EMAIL)"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_NL_RE = re.compile(r"\n{3,}")


def _clean(raw: str | None) -> str:
    """ATS descriptions arrive as HTML, sometimes double-escaped. Normalise to text."""
    if not raw:
        return ""
    txt = html.unescape(html.unescape(raw))
    txt = re.sub(r"<\s*(br|/p|/div|/li)\s*/?>", "\n", txt, flags=re.I)
    txt = re.sub(r"<\s*li[^>]*>", "\n- ", txt, flags=re.I)
    txt = _TAG_RE.sub("", txt)
    txt = _WS_RE.sub(" ", txt)
    txt = _NL_RE.sub("\n\n", txt)
    return txt.strip()


def _iso(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, (int, float)):  # Lever uses epoch millis
        try:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
        except (ValueError, OSError, OverflowError):
            return None
    return str(value)


@dataclass
class Job:
    job_id: str                 # stable: "{ats}:{company}:{native_id}"
    source: str
    company: str
    title: str
    location: str = ""
    remote: bool | None = None
    department: str = ""
    employment_type: str = ""
    salary: str = ""
    posted_at: str | None = None
    apply_url: str = ""
    description: str = ""
    raw_company_slug: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def compact(self) -> dict[str, Any]:
        """Small record for list responses — keeps us under the connector token cap."""
        d = asdict(self)
        d.pop("extra", None)
        d["description"] = d["description"][:280]
        return d


# --------------------------------------------------------------------------- #
# Adapters                                                                     #
# --------------------------------------------------------------------------- #

async def _fetch_greenhouse(client: httpx.AsyncClient, slug: str) -> list[Job]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    r = await client.get(url)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        loc = (j.get("location") or {}).get("name", "") or ""
        out.append(Job(
            job_id=f"greenhouse:{slug}:{j['id']}",
            source="greenhouse",
            company=slug,
            raw_company_slug=slug,
            title=j.get("title", ""),
            location=loc,
            remote="remote" in loc.lower(),
            department=", ".join(
                d.get("name", "") for d in (j.get("departments") or [])
            ),
            posted_at=_iso(j.get("updated_at") or j.get("first_published")),
            apply_url=j.get("absolute_url", ""),
            description=_clean(j.get("content")),
        ))
    return out


async def _fetch_lever(client: httpx.AsyncClient, slug: str) -> list[Job]:
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    r = await client.get(url)
    r.raise_for_status()
    out = []
    for j in r.json():
        cats = j.get("categories") or {}
        wp = (j.get("workplaceType") or "").lower()
        out.append(Job(
            job_id=f"lever:{slug}:{j.get('id')}",
            source="lever",
            company=slug,
            raw_company_slug=slug,
            title=j.get("text", ""),
            location=cats.get("location", "") or "",
            remote=wp == "remote" or "remote" in (cats.get("location") or "").lower(),
            department=cats.get("team", "") or cats.get("department", "") or "",
            employment_type=cats.get("commitment", "") or "",
            salary=(j.get("salaryRange") or {}).get("currency", "") and str(j.get("salaryRange")) or "",
            posted_at=_iso(j.get("createdAt")),
            apply_url=j.get("hostedUrl", ""),
            description=j.get("descriptionPlain") or _clean(j.get("description")),
        ))
    return out


async def _fetch_ashby(client: httpx.AsyncClient, slug: str) -> list[Job]:
    url = (f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
           f"?includeCompensation=true")
    r = await client.get(url)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        comp = j.get("compensation") or {}
        summary = comp.get("compensationTierSummary") or ""
        out.append(Job(
            job_id=f"ashby:{slug}:{j.get('jobUrl', j.get('title'))}",
            source="ashby",
            company=slug,
            raw_company_slug=slug,
            title=j.get("title", ""),
            location=j.get("location", "") or "",
            remote=bool(j.get("isRemote")),
            department=j.get("department", "") or j.get("team", "") or "",
            employment_type=j.get("employmentType", "") or "",
            salary=summary,
            posted_at=_iso(j.get("publishedAt")),
            apply_url=j.get("applyUrl") or j.get("jobUrl", ""),
            description=j.get("descriptionPlain") or _clean(j.get("descriptionHtml")),
        ))
    return out


async def _fetch_smartrecruiters(client: httpx.AsyncClient, slug: str) -> list[Job]:
    out, offset = [], 0
    while True:
        url = (f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
               f"?limit=100&offset={offset}")
        r = await client.get(url)
        r.raise_for_status()
        data = r.json()
        items = data.get("content", [])
        for j in items:
            loc = j.get("location") or {}
            loc_str = ", ".join(
                filter(None, [loc.get("city"), loc.get("region"), loc.get("country")])
            )
            out.append(Job(
                job_id=f"smartrecruiters:{slug}:{j.get('id')}",
                source="smartrecruiters",
                company=slug,
                raw_company_slug=slug,
                title=j.get("name", ""),
                location=loc_str,
                remote=bool(loc.get("remote")),
                department=(j.get("department") or {}).get("label", ""),
                employment_type=(j.get("typeOfEmployment") or {}).get("label", ""),
                posted_at=_iso(j.get("releasedDate")),
                apply_url=j.get("applyUrl") or j.get("ref", ""),
                description="",  # detail endpoint required; filled by fetch_job_detail
            ))
        offset += len(items)
        if len(items) < 100 or offset >= data.get("totalFound", offset):
            break
    return out


async def _fetch_recruitee(client: httpx.AsyncClient, slug: str) -> list[Job]:
    url = f"https://{slug}.recruitee.com/api/offers/"
    r = await client.get(url)
    r.raise_for_status()
    out = []
    for j in r.json().get("offers", []):
        out.append(Job(
            job_id=f"recruitee:{slug}:{j.get('id')}",
            source="recruitee",
            company=slug,
            raw_company_slug=slug,
            title=j.get("title", ""),
            location=j.get("location", "") or "",
            remote=(j.get("remote") is True),
            department=j.get("department", "") or "",
            employment_type=j.get("employment_type_code", "") or "",
            posted_at=_iso(j.get("published_at")),
            apply_url=j.get("careers_url") or j.get("careers_apply_url", ""),
            description=_clean(j.get("description")),
        ))
    return out


ADAPTERS = {
    "greenhouse": _fetch_greenhouse,
    "lever": _fetch_lever,
    "ashby": _fetch_ashby,
    "smartrecruiters": _fetch_smartrecruiters,
    "recruitee": _fetch_recruitee,
}

# Careers-URL shapes used for ATS auto-detection.
_URL_PATTERNS = [
    (re.compile(r"boards\.greenhouse\.io/([\w-]+)"), "greenhouse"),
    (re.compile(r"job-boards\.greenhouse\.io/([\w-]+)"), "greenhouse"),
    (re.compile(r"jobs\.lever\.co/([\w-]+)"), "lever"),
    (re.compile(r"jobs\.ashbyhq\.com/([\w.-]+)"), "ashby"),
    (re.compile(r"careers\.smartrecruiters\.com/([\w-]+)"), "smartrecruiters"),
    (re.compile(r"([\w-]+)\.recruitee\.com"), "recruitee"),
]


def parse_target(target: str) -> tuple[str | None, str]:
    """
    Accepts 'greenhouse:stripe', a careers URL, or a bare slug.
    Returns (ats_or_None, slug). None means 'probe every ATS'.
    """
    target = target.strip()
    if ":" in target and not target.startswith("http"):
        ats, _, slug = target.partition(":")
        if ats in ADAPTERS:
            return ats, slug
    for pat, ats in _URL_PATTERNS:
        m = pat.search(target)
        if m:
            return ats, m.group(1)
    return None, target.rstrip("/").split("/")[-1]


async def fetch_company(
    client: httpx.AsyncClient, target: str
) -> tuple[list[Job], str | None]:
    """Fetch one company's board. Probes every ATS when the platform is unknown."""
    ats, slug = parse_target(target)
    candidates = [ats] if ats else list(ADAPTERS)
    last_err = None
    for name in candidates:
        try:
            jobs = await ADAPTERS[name](client, slug)
            if jobs:
                return jobs, None
        except Exception as exc:  # one dead slug must not kill a 200-company run
            last_err = f"{name}: {type(exc).__name__}"
    return [], last_err or "no postings found"


async def fetch_many(
    targets: Iterable[str], concurrency: int = 8
) -> tuple[list[Job], dict[str, str]]:
    sem = asyncio.Semaphore(concurrency)
    errors: dict[str, str] = {}
    all_jobs: list[Job] = []

    async with httpx.AsyncClient(
        timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=True
    ) as client:
        async def one(t: str):
            async with sem:
                jobs, err = await fetch_company(client, t)
                if err:
                    errors[t] = err
                all_jobs.extend(jobs)

        await asyncio.gather(*(one(t) for t in targets), return_exceptions=True)

    seen, deduped = set(), []
    for j in all_jobs:
        if j.job_id not in seen:
            seen.add(j.job_id)
            deduped.append(j)
    return deduped, errors
