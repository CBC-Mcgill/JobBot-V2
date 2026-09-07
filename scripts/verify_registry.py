"""Build a verified seed registry from public job-list links, without Discord access."""

import argparse
import asyncio
import json
import logging
from pathlib import Path

from jobbot.discovery import Discovery, board_from_url, source_links
from jobbot.http import PublicHTTP
from jobbot.models import Company, now
from jobbot.providers import Providers


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("config/companies.json"))
    args = parser.parse_args()
    sources = json.loads(Path("config/discovery.json").read_text())
    http = PublicHTTP()
    try:
        candidates = {}
        for source in sources:
            try:
                response = await http.get(source, max_bytes=100_000_000)
                for url, name in source_links(response.text, source):
                    company = board_from_url(url, name, source)
                    if company:
                        candidates.setdefault(company.key, company)
                print(f"{source}: {len(candidates)} cumulative candidate boards", flush=True)
            except Exception as exc:
                print(f"Source unavailable: {source} ({type(exc).__name__})", flush=True)
        openai = Company(
            "OpenAI",
            "ashby",
            "openai",
            career_url="https://openai.com/careers/",
            provenance="https://openai.com/careers/",
        )
        candidates[openai.key] = openai
        if args.output.exists():
            for item in json.loads(args.output.read_text()):
                c = Company(**item)
                candidates.setdefault(c.key, c)
        verified = await Discovery(http, Providers(http)).validate(list(candidates.values()))
        verified.sort(key=lambda c: (c.name.lower(), c.key))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps([c.to_dict() for c in verified], indent=2) + "\n")
        report = {
            "checked_at": now(),
            "candidates": len(candidates),
            "verified": len(verified),
            "providers": {
                p: sum(c.provider == p for c in verified) for p in ("ashby", "greenhouse", "lever")
            },
            "sources": sources,
        }
        args.output.with_name("verification.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2), flush=True)
        if len(verified) < 200:
            raise SystemExit("Fewer than 200 verified boards; expand sources before shipping")
    finally:
        await http.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(main())
