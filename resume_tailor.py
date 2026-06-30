from pathlib import Path
from build_master_resume import build_resume

def _detect_role_type(title: str, description: str) -> str:
    combined = f"{title} {description}".lower()
    if any(x in combined for x in ("machine learning", "ml engineer", "ai engineer", "nlp", "pytorch", "llm")):
        return "ml"
    if any(x in combined for x in ("data engineer", "data analyst", "etl", "pipeline", "hadoop", "hive", "spark", "databricks", "sql")):
        return "data"
    if any(x in combined for x in ("software", "backend", "api", "full stack", "frontend", "java", "python developer")):
        return "software"
    return "master"


def tailor_resume(job_title: str, company: str, description: str, output_path: str) -> str:
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    variant = _detect_role_type(job_title, description)
    build_resume(variant=variant, output_path=output_path)
    return output_path
