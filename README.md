# jobsearch-mcp

A job search pipeline exposed to Claude as an MCP connector.

```
add_boards ─► sync_jobs ─► score_new_jobs ─► rank_jobs ─► get_job
                                                            │
                                          ┌─────────────────┤
                                          ▼                 ▼
                                  tailoring_brief    find_contacts
                                          │                 │
                                   (you tailor)      outreach_brief
                                          │                 │
                                          ▼          (you write + send)
                                    set_stage ◄──────  log_outreach
                                          │                 │
                                   pipeline_stats ◄─  mark_outreach
```

## Why not LinkedIn

There is no LinkedIn job-search API available to an individual developer. The
official jobs surface sits inside Talent Solutions, is partner-gated, and exists
for approved ATS vendors to *post* jobs — not to query the search index. LinkedIn
also lists job-search automation and data extraction as use cases it rejects, and
unofficial scraping violates the User Agreement and risks restriction of the
account you are job-searching from.

This server reads the public, no-auth JSON APIs the ATS vendors publish so that
companies' own postings can be syndicated. That is what those endpoints are for.
The data is better anyway: full descriptions, real apply URLs, frequently salary
bands, no rate-limit roulette — and LinkedIn is downstream of these boards.

| ATS | Endpoint | Auth |
|---|---|---|
| Greenhouse | `boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true` | none |
| Lever | `api.lever.co/v0/postings/{slug}?mode=json` | none |
| Ashby | `api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true` | none |
| SmartRecruiters | `api.smartrecruiters.com/v1/companies/{slug}/postings` | none |
| Recruitee | `{slug}.recruitee.com/api/offers/` | none |

Pass `greenhouse:stripe`, a careers URL, or a bare slug — the ATS is auto-detected
by probing. One dead slug never kills a multi-company run.

## Install

```bash
git clone <your-repo> && cd jobsearch-mcp
pip install -e .
```

## Connect to Claude

**Local (Claude Desktop / Claude Code)** — stdio, simplest, data stays on your machine:

```json
{
  "mcpServers": {
    "jobsearch": {
      "command": "python",
      "args": ["-m", "jobsearch_mcp.server"],
      "env": { "HUNTER_API_KEY": "optional" }
    }
  }
}
```

**Remote (claude.ai custom connector)** — Claude connects from Anthropic's cloud,
not from your laptop, so the server must be reachable on the public internet over
HTTPS. `localhost` will not work; use a tunnel for development.

```bash
TRANSPORT=http PORT=8080 python -m jobsearch_mcp.server
cloudflared tunnel --url http://localhost:8080     # or: ngrok http 8080
```

Then in Claude: **Settings → Connectors → Add custom connector**, URL
`https://<your-tunnel>/mcp`.

For anything permanent, deploy to Fly/Railway/Render and put auth in front of it.
The server is currently authless — fine behind a private tunnel you control, not
fine on a public URL, since anyone who finds it can read your pipeline.

## Configuration

| Env var | Purpose |
|---|---|
| `JOBSEARCH_DATA_DIR` | Where `pipeline.db` and `profile.json` live (default `~/.jobsearch-mcp`) |
| `HUNTER_API_KEY` | Optional. Enables verified contact lookup. Without it you still get the company route. |
| `TRANSPORT` | `stdio` (default) or `http` |

## Scoring, honestly

Weights are 45% keyword coverage / 25% experience alignment / 20% skill overlap /
10% education.

**This is a triage heuristic for your own ranking, not a simulation of an ATS.**
Real platforms do not compute a 0-100 match number and gate on it — they run
keyword and boolean filters and hand a ranked list to a recruiter. Any tool
claiming to output "your ATS score" is outputting its own opinion.

Consequences for how you use it:

- A genuinely strong match lands around **72-80** here, not 90+. The tokenizer is
  strict, so coverage of a job's full technical vocabulary is rarely above 60%.
  If you carry over a 90+ threshold from a looser scorer you will filter out
  everything.
- **Use rank, not an absolute cutoff.** Sort descending and work the top 10-15.
  That is the decision the number is actually good for.
- The `gaps` list is the highest-value output. It is the vocabulary the posting
  uses that your profile does not. Add a gap term **only** if you have real
  experience behind it.

## Contacts and outreach: the boundaries

The server **never sends email and never submits an application.** Irreversible
steps stay with you, in your own client, one at a time.

- **No pattern-guessing.** Guessing `first.last@company.com` and blasting it is
  the fastest way to wreck your sending domain. Every hard bounce is a spam
  signal, and once your domain is flagged, the recruiter replies and interview
  scheduling you actually need start landing in spam. `log_outreach` refuses an
  unverified personal address by design.
- **No profile scraping.** No LinkedIn, no GitHub email harvesting. Contact lookup
  goes through a provider (Hunter) that maintains its own opt-out and GDPR
  handling and returns a deliverability verdict.
- **Company route first.** `find_contacts` always returns the ATS apply URL and any
  recruiting alias published in the posting itself. These convert better than cold
  personal email because they enter the pipeline the hiring team reads. Cold email
  works as a *follow-up* to an application, not a replacement.
- **Volume discipline.** One email per company, one follow-up after ~7 days, then
  stop. Ten researched emails beat four hundred sprayed ones, and only the first
  is defensible under CAN-SPAM/GDPR without an unsubscribe mechanism.

`outreach_brief` deliberately returns *material* rather than a finished email —
matched evidence, the role, length and tone constraints — because Claude writing
from that produces something specific, while a mail-merge template produces the
thing recruiters delete.

## Notes

- Tool responses are capped by the connector token limit (~30k). `rank_jobs`
  returns compact rows; use `get_job` for one full description.
- Re-running `sync_jobs` is cheap and idempotent — dedup is on a stable
  `{ats}:{company}:{id}` key, so a scheduled run surfaces only genuinely new
  postings.
- Pair with a cron/GitHub Action calling `sync_jobs` daily; review the new-jobs
  preview rather than re-scoring the world.
