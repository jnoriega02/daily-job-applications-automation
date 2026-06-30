"""
filters.py — Shared filtering and scoring logic for job application automation.
All other scripts import from this module.
"""

import re
from typing import Optional

# ---------------------------------------------------------------------------
# Staffing / recruiting agency blacklist
# ---------------------------------------------------------------------------
AGENCY_BLACKLIST: set[str] = {
    # Major national agencies
    "robert half", "randstad", "teksystems", "insight global", "manpower",
    "staffmark", "apex systems", "kforce", "hays", "modis", "aerotek",
    "cybercoder", "cybercoders", "motion recruitment", "mastech",
    "emonics", "ams staffing", "contractstaffingrecruiters",
    "tenth revolution group", "trg", "ltm", "riccione resources",
    # Other common agencies
    "infosys bpm", "wipro", "cognizant staffing", "tata consultancy",
    "hcl technologies staffing", "genesis10", "collabera", "igate",
    "stefanini", "softpath system", "diverse lynx", "nityo infotech",
    "persistent systems staffing", "tek systems", "actalent", "proliant",
    "experis", "kelly services", "adecco", "spherion", "staffing solutions",
    "staffing agency", "recruiting firm", "talent acquisition partner",
    "global employment", "employment solutions", "staffbridge",
    "strategic staffing", "beacon hill", "volt information sciences",
    "volt workforce", "pomeroy", "ntt data staffing", "mvp staffing",
    "resource informatics", "srintech", "vinsys", "hirect",
    "glocomms", "harnham", "burtch works", "sci systems",
}

# ---------------------------------------------------------------------------
# Keyword sets for filtering
# ---------------------------------------------------------------------------
_CONTRACT_TITLE_KEYWORDS = {
    "contract", "contractor", "c2c", "corp to corp", "corp-to-corp",
    "1099", "temp", "temporary", "contingent", "freelance", "interim",
    "contract-to-hire", "c2h",
}

_CONTRACT_DESC_KEYWORDS = {
    "c2c only", "corp to corp only", "1099 only", "w2 or c2c",
    "contract position", "contract role", "temporary position",
    "contract to hire", "right to represent",
    "we are looking for contractors", "open to c2c",
}

_FAKE_TITLE_PATTERNS = [
    r"^(job|career|opportunity|hiring|position|role|opening)\s*$",
    r"^\s*(urgent|immediate|asap)\s*$",
    r"various roles",
    r"multiple positions",
]

_SKILL_KEYWORDS = {
    # Core languages / tools Juviny has
    "python", "sql", "hql", "java", "javascript", "typescript",
    "react", "angular", "fastapi", "rest", "api",
    # Data engineering stack
    "hadoop", "hive", "cloudera", "spark", "databricks", "etl",
    "pipeline", "data pipeline", "kafka", "airflow", "dbt",
    "data warehouse", "data lake", "s3", "redshift", "snowflake",
    # ML / AI / NLP
    "machine learning", "ml", "ai", "nlp", "natural language",
    "pytorch", "tensorflow", "scikit", "llm", "deep learning",
    "computer vision", "spacy",
    # Cloud / DevOps
    "aws", "gcp", "azure", "docker", "kubernetes", "git",
    # Roles Juviny targets
    "software engineer", "data engineer", "backend", "full stack",
    "fullstack", "ml engineer", "ai engineer", "technical product manager",
    "tpm", "platform engineer", "analytics engineer",
}

_EXPERIENCE_LEVEL_GOOD = {
    "junior", "entry level", "entry-level", "0-2 years", "1-3 years",
    "0-3 years", "new grad", "new graduate", "associate", "associate level",
    "early career",
}

_EXPERIENCE_LEVEL_BAD = {
    "senior", "staff", "principal", "lead", "director", "manager",
    "5+ years", "7+ years", "8+ years", "10+ years", "15+ years",
    "vp", "vice president", "head of", "architect",
}

_DFW_KEYWORDS = {
    "dallas", "fort worth", "dfw", "irving", "plano", "frisco",
    "richardson", "allen", "mckinney", "addison", "carrollton",
    "lewisville", "arlington", "garland", "mesquite", "grand prairie",
}


# ---------------------------------------------------------------------------
# Core filter functions
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    """Lowercase + collapse whitespace for consistent matching."""
    return re.sub(r"\s+", " ", (text or "").lower().strip())


def is_contractor_role(title: str, description: str) -> bool:
    """
    Return True if the role is a contract/C2C/1099/temp position.
    Checks title first (fast path), then scans description.
    """
    title_norm = _normalize(title)
    for kw in _CONTRACT_TITLE_KEYWORDS:
        if kw in title_norm:
            return True

    desc_norm = _normalize(description)
    for kw in _CONTRACT_DESC_KEYWORDS:
        if kw in desc_norm:
            return True
    return False


def is_agency_role(company_name: str) -> bool:
    """
    Return True if company_name matches a known staffing/recruiting agency.
    Uses substring match so 'Robert Half International' still triggers.
    """
    company_norm = _normalize(company_name)
    for agency in AGENCY_BLACKLIST:
        if agency in company_norm:
            return True
    return False


def is_fake_posting(title: str, company: str, description: str) -> bool:
    """
    Return True for red-flag postings:
      - Missing or confidential company
      - Vague/generic title matching known patterns
      - Description under 100 chars
      - Suspiciously inflated salary (>$250k base)
    """
    if not company or _normalize(company) in {"", "n/a", "confidential", "unknown"}:
        return True

    title_norm = _normalize(title)
    for pattern in _FAKE_TITLE_PATTERNS:
        if re.search(pattern, title_norm):
            return True

    desc_norm = _normalize(description)
    # Only flag short descriptions if we actually have some content (not empty/unloaded)
    if desc_norm and len(desc_norm) < 100:
        return True

    # Detect unrealistically high pay (per-week disguised as annual, etc.)
    salary_match = re.search(
        r"\$\s*([0-9][0-9,]+)\s*(k|,000)?\s*(per\s*year|\/yr|annually|\/year)?",
        desc_norm,
    )
    if salary_match:
        try:
            raw = salary_match.group(1).replace(",", "")
            value = int(raw)
            if salary_match.group(2) and "k" in salary_match.group(2):
                value *= 1000
            if value > 250_000:
                return True
        except ValueError:
            pass

    return False


def score_job(
    title: str,
    company: str,
    location: str,
    description: str,
    work_type: str,
) -> float:
    """
    Score a job 0–10 based on Juviny's preferences.

    Breakdown (max points):
      DFW hybrid/on-site location      → 3.0  (highest weight)
      Skill keyword matches             → 3.0
      Entry-level / realistic XP level → 2.0
      Not agency + not contractor       → 1.0
      Recency (24h filter applied)      → 1.0
                                          ----
                                          10.0

    Callers should use threshold ≥ 6.0 to qualify a job.
    """
    score = 0.0
    title_norm = _normalize(title)
    desc_norm = _normalize(description)
    loc_norm = _normalize(location)
    wt_norm = _normalize(work_type)
    combined = f"{title_norm} {desc_norm}"

    # --- Location / work-type preference (0–3) ---
    in_dfw = any(kw in loc_norm for kw in _DFW_KEYWORDS)
    is_hybrid = "hybrid" in wt_norm or "hybrid" in desc_norm
    is_onsite = any(x in wt_norm for x in ("on-site", "onsite", "on site"))
    is_remote = "remote" in wt_norm or "remote" in loc_norm

    if in_dfw and (is_hybrid or is_onsite):
        score += 3.0   # Best: DFW + hybrid/onsite
    elif in_dfw and is_remote:
        score += 2.0   # DFW remote
    elif is_remote:
        score += 1.0   # US remote (acceptable fallback)
    # Out-of-area onsite → 0 points

    # --- Skill keyword matches (0–3) ---
    matched = sum(1 for kw in _SKILL_KEYWORDS if kw in combined)
    score += min(matched / 4.0, 1.0) * 3.0

    # --- Experience level fit (0–2) ---
    has_good = any(kw in combined for kw in _EXPERIENCE_LEVEL_GOOD)
    has_bad = any(kw in combined for kw in _EXPERIENCE_LEVEL_BAD)
    if has_good and not has_bad:
        score += 2.0
    elif not has_bad:
        score += 1.0   # No explicit level → neutral
    # Senior-only → 0

    # --- Clean company + title (0–1) ---
    if not is_agency_role(company) and not is_contractor_role(title, description):
        score += 1.0

    # --- Recency (search filters ensure last 24h; award the point) ---
    score += 1.0

    return round(min(score, 10.0), 2)
