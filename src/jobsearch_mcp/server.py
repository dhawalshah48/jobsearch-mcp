"""
jobsearch-mcp — a job pipeline as an MCP connector.

Pipeline: track boards -> sync postings -> score against your profile ->
tailor -> apply -> find contacts -> draft outreach -> track replies.

Every tool is read-or-local-write. Nothing in this server sends an email, submits
an application, or posts anything to the outside world. The irreversible steps stay
with you.

Run:
    python -m jobsearch_mcp.server                 # stdio (Claude Desktop / Code)
    TRANSPORT=http python -m jobsearch_mcp.server  # streamable-http (custom connector)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from . import contacts as contacts_mod
from . import outreach as outreach_mod
from . import sources
from .scoring import Profile, score_job
from .store import STAGES, Store

DATA_DIR = Path(os.getenv("JOBSEARCH_DATA_DIR", Path.home() / ".jobsearch-mcp"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "pipeline.db"
PROFILE_PATH = DATA_DIR / "profile.json"

store = Store(DB_PATH)

mcp = MCPServer(
    name="jobsearch",
    version="1.0.0",
    instructions=(
        "Job search pipeline over public ATS job-board APIs (Greenhouse, Lever, "
        "Ashby, SmartRecruiters, Recruitee).\n\n"
        "Typical flow: add_boards -> sync_jobs -> score_new_jobs -> rank_jobs -> "
        "get_job -> tailoring_brief -> (user tailors and applies) -> set_stage -> "
        "find_contacts -> outreach_brief -> (user sends) -> log_outreach.\n\n"
        "This server never sends email and never submits applications. When a draft "
        "is ready, hand it to the user to send themselves."
    ),
)

RO = ToolAnnotations(readOnlyHint=True)
RW = ToolAnnotations(readOnlyHint=False, destructiveHint=False)


def _load_profile() -> Profile:
    if PROFILE_PATH.exists():
        return Profile.load(PROFILE_PATH)
    return Profile()


# --------------------------------------------------------------------------- #
# Profile                                                                      #
# --------------------------------------------------------------------------- #

@mcp.tool(annotations=RW, description="Create or update the candidate profile used for scoring. Pass only the fields you want to change.")
async def set_profile(
    name: Annotated[str | None, Field(description="Full name")] = None,
    titles: Annotated[list[str] | None, Field(description="Target job titles, e.g. ['ML Engineer','AI Engineer']")] = None,
    years_experience: Annotated[float | None, Field(description="Years of relevant industry experience")] = None,
    degree: Annotated[str | None, Field(description="Highest degree: bs | ms | phd")] = None,
    field_of_study: Annotated[str | None, Field(description="e.g. 'Computer Science (AI)'")] = None,
    skills: Annotated[list[str] | None, Field(description="Concrete skills and tools you can defend in an interview")] = None,
    keywords: Annotated[list[str] | None, Field(description="Extra domain terms to credit during matching")] = None,
    exclude_terms: Annotated[list[str] | None, Field(description="Terms that disqualify a posting, e.g. ['clearance','on-site only']")] = None,
    min_salary: Annotated[int | None, Field(description="Minimum acceptable base salary")] = None,
    locations: Annotated[list[str] | None, Field(description="Acceptable locations")] = None,
    remote_only: Annotated[bool | None, Field(description="Only consider remote roles")] = None,
    needs_sponsorship: Annotated[bool | None, Field(description="Flag postings that rule out visa sponsorship")] = None,
) -> dict[str, Any]:
    p = _load_profile()
    for key, val in locals().items():
        if key in ("p",) or val is None:
            continue
        if hasattr(p, key):
            setattr(p, key, val)
    p.save(PROFILE_PATH)
    return {"saved": True, "profile": json.loads(PROFILE_PATH.read_text())}


@mcp.tool(annotations=RO, description="Return the stored candidate profile.")
async def get_profile() -> dict[str, Any]:
    p = _load_profile()
    return {"profile": json.loads(json.dumps(p.__dict__)),
            "configured": PROFILE_PATH.exists()}


# --------------------------------------------------------------------------- #
# Boards                                                                       #
# --------------------------------------------------------------------------- #

@mcp.tool(annotations=RW, description="Track company job boards. Accepts 'greenhouse:stripe', a careers URL like 'https://jobs.lever.co/spotify', or a bare company slug (the ATS is auto-detected).")
async def add_boards(
    targets: Annotated[list[str], Field(description="Board identifiers, URLs, or slugs")],
    label: Annotated[str, Field(description="Optional group label, e.g. 'yc-h1b'")] = "",
) -> dict[str, Any]:
    added = store.add_boards(targets, label)
    return {"added": added, "skipped_duplicates": len(targets) - added,
            "total_tracked": len(store.list_boards())}


@mcp.tool(annotations=RO, description="List every tracked company board with its last sync result.")
async def list_boards() -> dict[str, Any]:
    boards = store.list_boards()
    return {"count": len(boards), "boards": boards}


# --------------------------------------------------------------------------- #
# Sync                                                                         #
# --------------------------------------------------------------------------- #

@mcp.tool(annotations=RW, description="Fetch current postings from tracked boards (or specific targets) and store any new ones. Returns counts plus a preview of what is new.")
async def sync_jobs(
    targets: Annotated[list[str] | None, Field(description="Specific boards to sync; omit to sync everything tracked")] = None,
    title_filter: Annotated[str | None, Field(description="Case-insensitive substring the title must contain, e.g. 'engineer'")] = None,
) -> dict[str, Any]:
    todo = targets or [b["target"] for b in store.list_boards()]
    if not todo:
        return {"error": "no boards tracked — call add_boards first"}

    jobs, errors = await sources.fetch_many(todo)

    if title_filter:
        needle = title_filter.lower()
        jobs = [j for j in jobs if needle in j.title.lower()]

    records = [j.compact() | {"description": j.description} for j in jobs]
    result = store.upsert_jobs(records)

    for t in todo:
        store.mark_board_run(t, errors.get(t))

    new_preview = store.query_jobs(new_only=True, limit=25)
    return {
        "fetched": len(jobs),
        **result,
        "board_errors": errors,
        "new_jobs_preview": [
            {k: r[k] for k in ("job_id", "title", "company", "location", "posted_at")}
            for r in new_preview
        ],
        "next_step": "call score_new_jobs to rank these against your profile",
    }


# --------------------------------------------------------------------------- #
# Scoring                                                                      #
# --------------------------------------------------------------------------- #

@mcp.tool(annotations=RW, description="Score every unscored posting against the stored profile (45% keywords / 25% experience / 20% skills / 10% education). Returns the top results.")
async def score_new_jobs(
    limit: Annotated[int, Field(description="Max postings to score in this pass", ge=1, le=500)] = 200,
) -> dict[str, Any]:
    profile = _load_profile()
    if not profile.skills:
        return {"error": "profile has no skills — call set_profile first"}

    pending = store.query_jobs(unscored=True, limit=limit)
    scored = []
    for job in pending:
        detail = score_job(profile, job)
        store.set_score(job["job_id"], detail["score"], detail)
        scored.append({"job_id": job["job_id"], "title": job["title"],
                       "company": job["company"], "score": detail["score"],
                       "flags": detail["flags"]})

    scored.sort(key=lambda r: -r["score"])
    return {
        "scored": len(scored),
        "top": scored[:25],
        "note": ("Score is a triage heuristic for your own ranking, not a real ATS "
                 "verdict — production ATS platforms run keyword filters and hand a "
                 "ranked list to a human, they do not compute a 0-100 gate. Use it to "
                 "decide where to spend tailoring time."),
    }


@mcp.tool(annotations=RO, description="List stored postings filtered by score, stage or company, best first. Descriptions are truncated — use get_job for the full text.")
async def rank_jobs(
    min_score: Annotated[float | None, Field(description="Only return postings at or above this score", ge=0, le=100)] = None,
    stage: Annotated[str | None, Field(description=f"Pipeline stage filter, one of: {', '.join(STAGES)}")] = None,
    company: Annotated[str | None, Field(description="Company substring filter")] = None,
    limit: Annotated[int, Field(description="Max rows", ge=1, le=100)] = 25,
) -> dict[str, Any]:
    rows = store.query_jobs(min_score=min_score, stage=stage, company=company, limit=limit)
    out = []
    for r in rows:
        detail = json.loads(r["score_detail"]) if r.get("score_detail") else {}
        out.append({
            "job_id": r["job_id"], "title": r["title"], "company": r["company"],
            "location": r["location"], "remote": bool(r["remote"]),
            "salary": r["salary"], "score": r["score"], "stage": r["stage"],
            "posted_at": r["posted_at"], "apply_url": r["apply_url"],
            "flags": detail.get("flags", []),
            "gaps": detail.get("missing_keywords", [])[:8],
        })
    return {"count": len(out), "jobs": out}


@mcp.tool(annotations=RO, description="Full record for one posting, including the complete job description and score breakdown.")
async def get_job(
    job_id: Annotated[str, Field(description="Job id from rank_jobs or sync_jobs")],
) -> dict[str, Any]:
    job = store.get_job(job_id)
    if not job:
        return {"error": f"no such job_id: {job_id}"}
    job["score_detail"] = json.loads(job["score_detail"]) if job.get("score_detail") else None
    return job


# --------------------------------------------------------------------------- #
# Tailoring                                                                    #
# --------------------------------------------------------------------------- #

@mcp.tool(annotations=RO, description="Gap analysis for tailoring a resume to one posting: what to emphasise, what is missing, and the rules for staying truthful.")
async def tailoring_brief(
    job_id: Annotated[str, Field(description="Job id to tailor against")],
) -> dict[str, Any]:
    job = store.get_job(job_id)
    if not job:
        return {"error": f"no such job_id: {job_id}"}
    profile = _load_profile()
    detail = json.loads(job["score_detail"]) if job.get("score_detail") else score_job(profile, job)
    return outreach_mod.tailoring_brief(job, detail, profile.__dict__)


# --------------------------------------------------------------------------- #
# Pipeline                                                                     #
# --------------------------------------------------------------------------- #

@mcp.tool(annotations=RW, description="Move a posting to a new pipeline stage with an optional note.")
async def set_stage(
    job_id: Annotated[str, Field(description="Job id")],
    stage: Annotated[
        Literal["new", "shortlisted", "tailored", "applied", "outreach_sent",
                "responded", "interviewing", "rejected", "closed"],
        Field(description="New stage"),
    ],
    note: Annotated[str, Field(description="Optional note appended to the job's history")] = "",
) -> dict[str, Any]:
    ok = store.set_stage(job_id, stage, note)
    return {"updated": ok, "job_id": job_id, "stage": stage}


@mcp.tool(annotations=RO, description="Pipeline summary: totals by stage, outreach volume and reply rate.")
async def pipeline_stats() -> dict[str, Any]:
    return store.stats()


# --------------------------------------------------------------------------- #
# Contacts + outreach                                                          #
# --------------------------------------------------------------------------- #

@mcp.tool(annotations=RO, description="Find contact routes for a posting: the ATS application link, any recruiting alias published in the posting, and (if HUNTER_API_KEY is set) verified hiring-relevant addresses at the company domain. Only deliverable addresses are returned.")
async def find_contacts(
    job_id: Annotated[str, Field(description="Job id")],
    domain: Annotated[str | None, Field(description="Company domain, e.g. 'stripe.com'. Supply this when the apply URL points at the ATS rather than the company.")] = None,
    limit: Annotated[int, Field(description="Max individual contacts", ge=1, le=15)] = 6,
) -> dict[str, Any]:
    job = store.get_job(job_id)
    if not job:
        return {"error": f"no such job_id: {job_id}"}
    return await contacts_mod.find_contacts(job, domain=domain, limit=limit)


@mcp.tool(annotations=RO, description="Structured material for writing one cold email: the role, the contact, your matching evidence, and the length/tone constraints. Returns a brief for you to write from — it does not write or send the email.")
async def outreach_brief(
    job_id: Annotated[str, Field(description="Job id")],
    contact_email: Annotated[str | None, Field(description="Verified recipient address, if you have one")] = None,
    contact_name: Annotated[str | None, Field(description="Recipient name")] = None,
    contact_role: Annotated[str | None, Field(description="Recipient's role")] = None,
) -> dict[str, Any]:
    job = store.get_job(job_id)
    if not job:
        return {"error": f"no such job_id: {job_id}"}
    profile = _load_profile()
    detail = json.loads(job["score_detail"]) if job.get("score_detail") else score_job(profile, job)
    contact = None
    if contact_email:
        contact = {"name": contact_name or "", "role": contact_role or "",
                   "email": contact_email}
    return outreach_mod.outreach_brief(job, detail, profile.__dict__, contact)


@mcp.tool(annotations=RW, description="Record an outreach draft in the pipeline. Does NOT send it — you send from your own mail client, then call mark_outreach.")
async def log_outreach(
    job_id: Annotated[str, Field(description="Job id")],
    subject: Annotated[str, Field(description="Email subject line")],
    body: Annotated[str, Field(description="Email body as it will be sent")],
    contact_email: Annotated[str, Field(description="Recipient address")] = "",
    contact_name: Annotated[str, Field(description="Recipient name")] = "",
    contact_role: Annotated[str, Field(description="Recipient role")] = "",
    email_status: Annotated[
        Literal["verified", "company_alias", "unverified"],
        Field(description="Verification status of the address"),
    ] = "unverified",
) -> dict[str, Any]:
    job = store.get_job(job_id)
    if not job:
        return {"error": f"no such job_id: {job_id}"}
    if email_status == "unverified" and contact_email:
        return {"error": "refusing to log an unverified personal address — verify it "
                         "first (find_contacts) or use the company route. Bounces "
                         "damage the sending domain you need for recruiter replies."}
    oid = store.record_draft(
        job_id=job_id, company=job["company"], contact_name=contact_name,
        contact_role=contact_role, contact_email=contact_email,
        email_status=email_status, subject=subject, body=body,
    )
    return {"outreach_id": oid, "logged": True,
            "reminder": "Review and send this yourself. One email per company, "
                        "one follow-up after ~7 days, then stop."}


@mcp.tool(annotations=RW, description="Mark a logged outreach as sent or replied, for reply-rate tracking.")
async def mark_outreach(
    outreach_id: Annotated[int, Field(description="Id returned by log_outreach")],
    event: Annotated[Literal["sent", "replied"], Field(description="What happened")],
) -> dict[str, Any]:
    ok = store.mark_outreach(outreach_id, sent=(event == "sent"),
                             replied=(event == "replied"))
    return {"updated": ok, "outreach_id": outreach_id, "event": event}


@mcp.tool(annotations=RO, description="Recent outreach history with sent/replied timestamps.")
async def outreach_log(
    limit: Annotated[int, Field(description="Max rows", ge=1, le=100)] = 25,
) -> dict[str, Any]:
    rows = store.outreach_log(limit)
    for r in rows:
        r["body"] = (r.get("body") or "")[:400]
    return {"count": len(rows), "outreach": rows}


def main() -> None:
    transport = os.getenv("TRANSPORT", "stdio")
    if transport == "http":
        mcp.settings.host = os.getenv("HOST", "0.0.0.0")
        mcp.settings.port = int(os.getenv("PORT", "8080"))
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
