"""Conservative, explainable rules; company overrides remain explicit opt-ins."""

import re

from .models import Classification, Job

SENIOR = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|manager|director|head|vp|vice president)\b", re.I
)
INTERN = re.compile(r"\b(intern(?:ship)?s?|co[ -]?op|industrial placement)\b", re.I)
GRAD = re.compile(
    r"\b(new[ -]?grad(?:uate)?s?|recent graduates?|university graduates?|"
    r"entry[ -]?level|graduate (?:software|engineer|developer|analyst|trader|quant|program|role)|"
    r"campus hire|early careers?)\b",
    re.I,
)
QUANT = re.compile(
    r"\b(quant(?:itative)?|trader|trading|systematic (?:research|investing)|"
    r"algorithmic (?:research|execution))\b",
    re.I,
)
AI = re.compile(
    r"\b(ai|ml|machine learning|deep learning|artificial intelligence|"
    r"computer vision|nlp|natural language processing|data scien(?:ce|tist))\b",
    re.I,
)
SOFTWARE = re.compile(
    r"\b(software|swe|sde|developer|programmer|devops|sre|site reliability|"
    r"(?:front|back)[ -]?end|full[ -]?stack|firmware|embedded|"
    r"(?:platform|cloud|data|security|infrastructure|systems) engineer)\b",
    re.I,
)
NONTECH = re.compile(
    r"\b(recruit(?:er|ing|ment)|sales|marketing|account(?:ant|ing)|"
    r"business development|human resources|product manag(?:er|ement)|"
    r"project manag(?:er|ement)|legal|counsel|customer success|"
    r"talent acquisition|administrative)\b",
    re.I,
)


def classify(job: Job, overrides: dict | None = None) -> Classification:
    overrides = overrides or {}
    reasons = []
    if overrides.get("exclude"):
        return Classification(None, None, ("company exclusion",))
    title = job.title.replace("–", "-").replace("—", "-")
    description = job.description
    if SENIOR.search(title):
        return Classification(None, None, ("senior title",))
    if NONTECH.search(title):
        return Classification(None, None, ("unrelated occupation",))

    # Use role-level evidence rather than incidental benefits or company boilerplate.
    experience = None
    if INTERN.search(title) or job.employment_type.lower() in {"intern", "internship"}:
        experience = "Intern"
        reasons.append("internship title or employment type")
    elif GRAD.search(title):
        experience = "New Grad"
        reasons.append("explicit graduate/entry-level title")
    elif re.search(r"\bgraduate\b", title, re.I) and not re.search(
        r"\b(?:post[ -]?graduate|graduate student|graduate degree)\b", title, re.I
    ):
        experience = "New Grad"
        reasons.append("explicit graduate title")
    elif re.search(
        r"\b(?:this (?:role|position|opportunity) is|we (?:are seeking|welcome|encourage)|"
        r"designed for|open to|seeking|looking for)\b[^.!?\n]{0,90}"
        r"\b(?:new[ -]?grad(?:uate)?s?|recent graduates?|entry[ -]?level)\b",
        description,
        re.I,
    ):
        experience = "New Grad"
        reasons.append("explicit graduate eligibility in description")
    if "experience" in overrides:
        experience = overrides["experience"]
        reasons.append("company experience override")
    if not experience:
        return Classification(None, None, ("no explicit early-career evidence",))

    # Quant firms do not make every software position a quantitative role.
    context = f"{title} {job.department}"
    kind = None
    if QUANT.search(context):
        kind = "Quant"
        reasons.append("quantitative/trading role")
    elif AI.search(context):
        kind = "AI/ML"
        reasons.append("AI/ML role")
    elif SOFTWARE.search(title):
        kind = "SWE"
        reasons.append("software role")
    if "kind" in overrides:
        kind = overrides["kind"]
        reasons.append("company type override")
    if not kind:
        reasons.append("unrelated or ambiguous occupation")
    return Classification(kind, experience, tuple(reasons))
