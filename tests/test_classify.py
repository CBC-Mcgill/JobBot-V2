from dataclasses import replace

import pytest

from jobbot.classify import classify
from jobbot.models import canonical_url


@pytest.mark.parametrize(
    ("title", "kind", "experience"),
    [
        ("Software Engineer Intern", "SWE", "Intern"),
        ("Backend Developer Co-op", "SWE", "Intern"),
        ("Software Engineer, New Grad 2027", "SWE", "New Grad"),
        ("Graduate Software Engineer", "SWE", "New Grad"),
        ("Entry-level Cloud Engineer", "SWE", "New Grad"),
        ("Quantitative Research Intern", "Quant", "Intern"),
        ("Graduate Trader", "Quant", "New Grad"),
        ("Machine Learning Intern", "AI/ML", "Intern"),
        ("AI Research Scientist - New Grad", "AI/ML", "New Grad"),
        ("Quantitative Machine Learning Intern", "Quant", "Intern"),
        ("Marketing Intern", None, None),
        ("Senior Software Engineer - New Grad Program Mentor", None, None),
        ("Staff ML Engineer", None, None),
        ("Software Engineer", None, None),
        ("Junior Software Developer", None, None),
        ("Product Management Intern - AI", None, None),
        ("Mechanical Engineering Intern", None, "Intern"),
        ("Sales Engineer Intern", None, None),
    ],
)
def test_classification(job, title, kind, experience):
    result = classify(replace(job, title=title))
    assert (result.kind, result.experience) == (kind, experience)


def test_description_evidence_and_boilerplate(job):
    job = replace(job, title="Software Engineer")
    assert classify(replace(job, description="We welcome recent graduates to apply.")).eligible
    assert not classify(replace(job, description="Mentor interns and recent graduates.")).eligible
    assert not classify(replace(job, description="Minimum two years of experience.")).eligible


def test_quant_company_does_not_force_software_into_quant(job):
    assert classify(replace(job, company="Quantitative Capital")).kind == "SWE"
    result = classify(job, {"kind": "Quant"})
    assert result.kind == "Quant"
    assert "company type override" in result.reasons
    assert not classify(job, {"exclude": True}).eligible


def test_canonical_application_url():
    assert (
        canonical_url("https://jobs.lever.co/acme/123/apply?utm_source=x&lever-source=y")
        == "https://jobs.lever.co/acme/123"
    )
    assert (
        canonical_url("https://example.com/apply?job_id=1&utm_campaign=abc")
        == "https://example.com/apply?job_id=1"
    )
