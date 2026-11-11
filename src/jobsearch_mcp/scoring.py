"""
Fit scoring.

Weights match the ATS rubric already in use: 45% keyword coverage, 25% experience
alignment, 20% skill overlap, 10% education.

Important framing: this score is a *ranking heuristic for your own triage*. Real ATS
platforms (Greenhouse, Lever, Workday) do not compute a 0-100 match number and gate
on it — most run keyword/boolean filters and hand a ranked list to a human. Treat 90+
as "this is worth an hour of tailoring", not "this will pass a machine".
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

WEIGHTS = {"keywords": 0.45, "experience": 0.25, "skills": 0.20, "education": 0.10}

_STOP = set("""
a an and are as at be by for from has have in is it its of on or that the to with
we you your our their will can may must who what which этой role team work working
company please apply job position candidate candidates ability strong excellent
years year experience including etc using use used help across new
""".split())

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+#.\-/]*[a-z0-9+#]|[a-z0-9]{2,}")

# Multi-word skills that must be matched as phrases, not split into tokens.
_PHRASES = [
    "machine learning", "deep learning", "computer vision", "natural language processing",
    "large language model", "model serving", "feature store", "vector database",
    "distributed training", "model compression", "knowledge distillation",
    "reinforcement learning", "time series", "data pipeline", "ci/cd", "a/b testing",
    "infrastructure as code", "model registry", "experiment tracking", "prompt engineering",
    "retrieval augmented generation", "fine tuning", "quantization aware training",
]

_SENIORITY = {
    "intern": 0, "internship": 0,
    "junior": 1, "entry level": 1, "new grad": 1, "associate": 1,
    "mid": 3, "intermediate": 3,
    "senior": 5, "sr.": 5, "sr ": 5,
    "staff": 8, "lead": 7, "principal": 10, "architect": 8,
    "manager": 8, "director": 12, "head of": 12, "vp": 15,
}

_DEGREE_RANK = {"phd": 4, "ph.d": 4, "doctorate": 4, "master": 3, "ms": 3,
                "msc": 3, "bachelor": 2, "bs": 2, "bsc": 2, "associate degree": 1}


@dataclass
class Profile:
    name: str = ""
    titles: list[str] = field(default_factory=list)
    years_experience: float = 0.0
    degree: str = ""              # "ms" | "phd" | "bs"
    field_of_study: str = ""
    skills: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)   # extra terms to credit
    exclude_terms: list[str] = field(default_factory=list)  # hard disqualifiers
    min_salary: int | None = None
    locations: list[str] = field(default_factory=list)
    remote_only: bool = False
    needs_sponsorship: bool = False

    @classmethod
    def load(cls, path: str | Path) -> "Profile":
        data = json.loads(Path(path).read_text())
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(asdict(self), indent=2))


def tokenize(text: str) -> set[str]:
    low = text.lower()
    found = {p for p in _PHRASES if p in low}
    toks = {t for t in _TOKEN_RE.findall(low) if t not in _STOP and len(t) > 1}
    return found | toks


def _extract_years(text: str) -> float | None:
    """Pull the minimum years-of-experience requirement out of a description."""
    low = text.lower()
    best = None
    for m in re.finditer(r"(\d{1,2})\s*\+?\s*(?:-\s*\d{1,2}\s*)?(?:years?|yrs?)", low):
        window = low[max(0, m.start() - 60): m.end() + 60]
        if "experience" not in window:
            continue
        val = float(m.group(1))
        if 0 < val <= 25:
            best = val if best is None else min(best, val)
    return best


def _seniority_of(title: str) -> int | None:
    low = f" {title.lower()} "
    for word, level in sorted(_SENIORITY.items(), key=lambda kv: -len(kv[0])):
        if word in low:
            return level
    return None


def _score_keywords(profile: Profile, job_tokens: set[str]) -> tuple[float, list[str], list[str]]:
    """Coverage of the job's *salient* terms by the profile's vocabulary."""
    mine = set()
    for s in profile.skills + profile.keywords + profile.titles:
        mine |= tokenize(s)

    # Salient = terms in the job that look like requirements, not prose.
    salient = {t for t in job_tokens if len(t) > 2}
    if not salient:
        return 0.0, [], []

    hit = sorted(salient & mine)
    # Missing terms that read like tech nouns are the actionable gap.
    miss = sorted(
        t for t in (salient - mine)
        if _looks_technical(t) and not _is_noise(t)
    )[:40]

    # Coverage of the technical subset carries the weight; raw prose overlap is noise.
    tech_salient = {t for t in salient if _looks_technical(t)}
    if tech_salient:
        cov = len(tech_salient & mine) / len(tech_salient)
    else:
        cov = len(hit) / len(salient)
    # Gentle curve: 60% coverage of a job's tech stack is already a strong match.
    return min(1.0, math.sqrt(cov) * 1.15), hit[:40], miss


# Short tokens must match EXACTLY — as substrings they fire on ordinary prose
# ("email" contains "ai", "goals" contains "go", "enormous" contains "orm").
_TECH_EXACT = {
    "py", "js", "ts", "sql", "aws", "gcp", "ml", "ai", "api", "sdk", "orm", "etl",
    "elt", "llm", "llms", "gpu", "gpus", "cpu", "cv", "nlp", "rl", "k8s", "ci", "cd",
    "go", "r", "c", "s3", "ec2", "rds", "eks", "ecs", "iam", "vpc", "rag", "moe",
    "vlm", "ocr", "grpc", "rest", "json", "yaml", "http", "tls", "ssh", "sre",
}
_TECH_SUBSTR = re.compile(
    r"(azure|kubernet|kube|docker|terraform|ansible|spark|kafka|airflow|dagster|"
    r"torch|tensorflow|tensor|jax|numpy|pandas|sklearn|scikit|hugging|transformer|"
    r"fastapi|flask|django|express|react|angular|vue|node|rust|golang|java|scala|"
    r"kotlin|swift|bash|linux|git|jenkins|gitlab|github|postgres|mysql|mongo|redis|"
    r"elastic|snowflake|databricks|bigquery|redshift|kinesis|lambda|sagemaker|"
    r"vertex|bedrock|triton|vllm|onnx|cuda|nccl|deepspeed|megatron|ray|dask|"
    r"prometheus|grafana|datadog|opentelemetry|pinecone|weaviate|qdrant|faiss|milvus|"
    r"langchain|llamaindex|mlflow|wandb|kubeflow|quantiz|distill|prun|lora|peft)"
)


def _matches_tech(tok: str) -> bool:
    return tok in _TECH_EXACT or bool(_TECH_SUBSTR.search(tok))


_PROSE_SUFFIX = re.compile(r"(tion|ment|ness|ance|ence|ship|ities|ility|ing|ely|ally)$")


def _looks_technical(tok: str) -> bool:
    """
    Heuristic: is this token a tool/technology worth listing as a gap?

    Kept deliberately tight. A loose version fills the gap list with ordinary prose,
    which both wastes your reading time and dilutes the keyword coverage score.
    """
    if " " in tok:                      # multi-word phrases are pre-vetted
        return True
    if len(tok) < 2 or len(tok) > 24:
        return False
    if _matches_tech(tok):
        return True
    if _PROSE_SUFFIX.search(tok):       # "deployment", "engineering", "reliability"
        return False
    if tok in _PROSE_SLASH:             # "and/or", "he/she"
        return False
    # Internal punctuation or digits: c++, ci/cd, gpt-4, node.js, k8s.
    return bool(re.search(r"[+#/]|\d", tok) or re.search(r"\w\.\w", tok))


_PROSE_SLASH = {"and/or", "he/she", "his/her", "s/he", "w/", "24/7", "n/a"}

_URLISH = re.compile(r"(\.com|\.io|\.org|\.ai$|\.co$|www\.|https?)")
_BOILERPLATE = {
    "legitimate", "trustworthy", "equal", "veteran", "disability", "eeo",
    "e-verify", "401k", "401", "usd", "salary", "compensation", "commissions/sales",
}


def _is_noise(tok: str) -> bool:
    """Tokens that pass the tech filter but are never an actionable resume gap."""
    if tok in _BOILERPLATE:
        return True
    if _URLISH.search(tok):
        return True
    if re.fullmatch(r"[\d,.\-]+", tok):     # bare figures from salary bands
        return True
    return False


def _score_experience(profile: Profile, title: str, text: str) -> tuple[float, str]:
    req = _extract_years(text)
    sen = _seniority_of(title)
    mine = profile.years_experience

    if req is None and sen is None:
        return 0.7, "no explicit requirement"

    target = req if req is not None else float(sen or 3)
    if mine >= target:
        over = mine - target
        # Being wildly overqualified is also a mismatch signal.
        score = 1.0 if over <= 4 else max(0.55, 1.0 - (over - 4) * 0.08)
        return score, f"you {mine:g}y vs ~{target:g}y required"
    gap = target - mine
    return max(0.0, 1.0 - gap * 0.22), f"you {mine:g}y vs ~{target:g}y required (short {gap:g}y)"


def _score_skills(profile: Profile, job_tokens: set[str]) -> tuple[float, list[str]]:
    mine = set()
    for s in profile.skills:
        mine |= tokenize(s)
    if not mine:
        return 0.0, []
    overlap = sorted(mine & job_tokens)
    return min(1.0, len(overlap) / max(6, len(mine) * 0.35)), overlap[:30]


def _score_education(profile: Profile, text: str) -> tuple[float, str]:
    low = text.lower()
    required = 0
    for word, rank in _DEGREE_RANK.items():
        if re.search(rf"\b{re.escape(word)}\b", low):
            required = max(required, rank)
    mine = _DEGREE_RANK.get(profile.degree.lower().strip(".").replace(".", ""), 0)
    if required == 0:
        return 0.85, "no degree requirement stated"
    if mine >= required:
        return 1.0, "degree meets or exceeds requirement"
    return max(0.3, 1.0 - (required - mine) * 0.3), "degree below stated requirement"


def score_job(profile: Profile, job: dict[str, Any]) -> dict[str, Any]:
    text = f"{job.get('title','')}\n{job.get('description','')}"
    tokens = tokenize(text)
    low = text.lower()

    kw, hits, misses = _score_keywords(profile, tokens)
    exp, exp_note = _score_experience(profile, job.get("title", ""), text)
    sk, sk_hits = _score_skills(profile, tokens)
    edu, edu_note = _score_education(profile, text)

    total = 100 * (
        kw * WEIGHTS["keywords"] + exp * WEIGHTS["experience"]
        + sk * WEIGHTS["skills"] + edu * WEIGHTS["education"]
    )

    flags = []
    for term in profile.exclude_terms:
        if term.lower() in low:
            flags.append(f"excluded term present: {term}")
            total *= 0.5
    if profile.needs_sponsorship:
        for phrase in ("no sponsorship", "not sponsor", "unable to sponsor",
                       "without sponsorship", "us citizen", "security clearance",
                       "citizenship required", "green card holder"):
            if phrase in low:
                flags.append(f"sponsorship blocker: '{phrase}'")
                total = min(total, 35)
                break
    if profile.remote_only and job.get("remote") is False:
        flags.append("not remote")

    return {
        "job_id": job.get("job_id"),
        "score": round(total, 1),
        "breakdown": {
            "keywords": round(kw * 100, 1),
            "experience": round(exp * 100, 1),
            "skills": round(sk * 100, 1),
            "education": round(edu * 100, 1),
        },
        "matched_keywords": hits,
        "missing_keywords": misses,
        "matched_skills": sk_hits,
        "experience_note": exp_note,
        "education_note": edu_note,
        "flags": flags,
    }
