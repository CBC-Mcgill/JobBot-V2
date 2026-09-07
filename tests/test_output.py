import json
from dataclasses import replace
from pathlib import Path

import pytest

from jobbot.discord_output import make_message
from jobbot.discovery import board_from_url
from jobbot.models import Company


@pytest.mark.parametrize(
    "route",
    [
        "SWE_Intern",
        "SWE_New Grad",
        "Quant_Intern",
        "Quant_New Grad",
        "AI/ML_Intern",
        "AI/ML_New Grad",
    ],
)
def test_routes_and_embed_limits(job, settings, route):
    expanded = replace(
        job,
        title="x" * 500,
        description="@everyone **" * 1000,
        compensation="USD 100 per hour " * 100,
        location="x" * 2000,
    )
    delivery = {
        "id": "abc",
        "kind": "job",
        "route": route,
        "payload": json.dumps(expanded.to_dict()),
    }
    message = make_message(delivery, settings)
    embed = message["embed"]
    assert len(embed.title) <= 256
    assert len(embed) < 6000
    assert all(len(field.value) <= 1024 for field in embed.fields)
    assert "jobbot:abc" in embed.footer.text
    assert expanded.title[:100] in embed.title
    assert f"{route.replace('_', ' · ')}" in embed.footer.text
    assert "**Example**" in embed.description
    assert "📍 " in embed.description
    assert "💰 " in embed.description
    assert "[Apply now ↗]" in embed.description
    assert message["allowed_mentions"].to_dict()["parse"] == []
    assert f"<#{settings.channels[route]}>" in embed.description
    assert not embed.fields
    assert settings.destination(route) == settings.test_channel
    assert replace(settings, debug=False).destination(route) == settings.channels[route]


def test_summary_pings_only_production_roles(settings):
    delivery = {
        "id": "abc",
        "kind": "summary",
        "payload": json.dumps(
            {
                "counts": {"SWE_Intern": 2},
                "backlog": 7,
            }
        ),
    }
    debug = make_message(delivery, settings)
    assert debug["content"] is None
    assert debug["allowed_mentions"].to_dict()["parse"] == []
    prod = make_message(delivery, replace(settings, debug=False))
    assert prod["content"] == "<@&400> <@&401>"
    assert prod["allowed_mentions"].to_dict() == {"roles": [400, 401], "parse": []}


def test_absent_compensation_keeps_embed_compact(job, settings):
    message = make_message(
        {"id": "abc", "kind": "job", "route": "SWE_Intern", "payload": json.dumps(job.to_dict())},
        settings,
    )
    embed = message["embed"]
    assert "Not listed" not in embed.description
    assert not embed.fields


def heading_for(job, settings, company):
    delivery = {
        "id": "abc",
        "kind": "job",
        "route": "SWE_New Grad",
        "payload": json.dumps(replace(job, company=company).to_dict()),
    }
    return make_message(delivery, settings)["embed"].description.split("\n")[0]


def test_padded_company_name_is_normalised(job, settings):
    # Discovery lifts names from third-party listing tables, so they arrive padded.
    assert Company("  Cottingham & Butler ", "greenhouse", "x").name == "Cottingham & Butler"
    assert heading_for(job, settings, "  Cottingham & Butler ") == "**Cottingham & Butler**"


def test_padding_in_a_payload_queued_earlier_is_still_cleaned(job, settings):
    """A delivery snapshots job.company when it is queued, so normalising Company cannot
    reach rows already in the outbox. The render boundary has to strip too."""
    assert heading_for(job, settings, "TEGNA ") == "**TEGNA**"


def test_blank_scraped_name_falls_back_to_the_board(job, settings):
    """A whitespace-only name is truthy, so it would skip the board fallback and then
    strip to nothing. Empty is the one value that renders as four literal asterisks."""
    assert board_from_url("https://jobs.ashbyhq.com/acme", name="   ").name == "acme"
    assert heading_for(job, settings, "") == "****"


def test_registry_names_are_already_normalised():
    path = Path(__file__).resolve().parent.parent / "config" / "companies.json"
    registry = json.loads(path.read_text())
    assert [c["name"] for c in registry if c["name"] != c["name"].strip()] == []
