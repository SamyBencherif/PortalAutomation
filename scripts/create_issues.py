#!/usr/bin/env python3
"""Mirror ROADMAP.md into the GitHub issue tracker.

The roadmap file is the source; this only projects it. Parsing the markdown
rather than keeping a second copy of the items here is the whole point -- two
hand-maintained lists of the same work drift within a week, and the tracker
version is the one people then stop trusting.

Safe to re-run: an item whose title already exists as an issue (open *or*
closed) is skipped, so this never duplicates and never reopens something
someone deliberately closed.

    export GITHUB_TOKEN=ghp_...          # needs `repo` scope
    python scripts/create_issues.py                 # dry run, prints the plan
    python scripts/create_issues.py --create        # actually creates

Requires only httpx, which is already a dependency.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import httpx

API = "https://api.github.com"
ROADMAP = Path(__file__).resolve().parent.parent / "ROADMAP.md"

MILESTONE = re.compile(r"^## (M\d+) — (.+)$")
ITEM = re.compile(r"^### (\d+)\. (.+)$")
# Sections that describe state rather than work to be done.
NOT_WORK = ("Shipped", "Known limits")


@dataclass
class Item:
    number: int
    title: str
    milestone: str
    milestone_title: str
    body: list[str] = field(default_factory=list)

    @property
    def issue_title(self) -> str:
        return self.title

    def issue_body(self, milestone_desc: str) -> str:
        text = "\n".join(self.body).strip()
        return (
            f"{text}\n\n---\n"
            f"*{self.milestone} — {milestone_desc}. "
            f"Tracked in [`ROADMAP.md`](../blob/main/ROADMAP.md); edit there, not here.*\n"
        )


def parse(path: Path) -> list[Item]:
    items: list[Item] = []
    milestone = milestone_title = ""
    skipping = False
    current: Item | None = None

    for line in path.read_text().splitlines():
        m = MILESTONE.match(line)
        if m:
            milestone, milestone_title = m.group(1), m.group(2)
            skipping = False
            current = None
            continue
        if line.startswith("## "):
            skipping = any(s in line for s in NOT_WORK)
            current = None
            continue
        if skipping:
            continue

        m = ITEM.match(line)
        if m:
            current = Item(number=int(m.group(1)), title=m.group(2),
                           milestone=milestone, milestone_title=milestone_title)
            items.append(current)
            continue
        if current is not None:
            current.body.append(line)

    return items


def repo_slug() -> str:
    """owner/name, from the git remote."""
    url = subprocess.run(["git", "remote", "get-url", "origin"],
                         capture_output=True, text=True, check=True).stdout.strip()
    m = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?$", url)
    if not m:
        raise SystemExit(f"could not parse a repo from remote {url!r}")
    return m.group(1)


class GitHub:
    def __init__(self, token: str, slug: str) -> None:
        self.slug = slug
        self.http = httpx.Client(
            base_url=f"{API}/repos/{slug}",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
            timeout=30.0,
        )

    def _all(self, path: str, **params) -> list[dict]:
        out, page = [], 1
        while True:
            r = self.http.get(path, params={**params, "per_page": 100, "page": page})
            r.raise_for_status()
            batch = r.json()
            out.extend(batch)
            if len(batch) < 100:
                return out
            page += 1

    def existing_titles(self) -> set[str]:
        # state=all so a closed item is not silently recreated.
        return {i["title"] for i in self._all("/issues", state="all")
                if "pull_request" not in i}

    def milestones(self) -> dict[str, int]:
        return {m["title"]: m["number"] for m in self._all("/milestones", state="all")}

    def create_milestone(self, title: str, description: str) -> int:
        r = self.http.post("/milestones", json={"title": title,
                                                "description": description})
        r.raise_for_status()
        return r.json()["number"]

    def create_issue(self, title: str, body: str, milestone: int | None,
                     labels: list[str]) -> str:
        payload = {"title": title, "body": body, "labels": labels}
        if milestone is not None:
            payload["milestone"] = milestone
        r = self.http.post("/issues", json=payload)
        r.raise_for_status()
        return r.json()["html_url"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--create", action="store_true",
                    help="actually create issues (default is a dry run)")
    ap.add_argument("--repo", help="owner/name; default from the git remote")
    ap.add_argument("--label", action="append", default=["roadmap"])
    args = ap.parse_args()

    items = parse(ROADMAP)
    if not items:
        print("no roadmap items parsed -- has ROADMAP.md changed shape?", file=sys.stderr)
        return 1

    slug = args.repo or repo_slug()
    print(f"{len(items)} roadmap items -> {slug}\n")

    if not args.create:
        for it in items:
            print(f"  [{it.milestone}] {it.title}")
        print("\nDry run. Set GITHUB_TOKEN and pass --create to make these.")
        return 0

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        print("GITHUB_TOKEN is not set (needs `repo` scope).", file=sys.stderr)
        return 2

    gh = GitHub(token, slug)
    try:
        existing = gh.existing_titles()
        milestones = gh.milestones()
    except httpx.HTTPStatusError as e:
        print(f"GitHub API rejected the request: {e.response.status_code} "
              f"{e.response.text[:200]}", file=sys.stderr)
        return 2

    created = skipped = 0
    for it in items:
        if it.issue_title in existing:
            print(f"  skip   {it.title}  (already an issue)")
            skipped += 1
            continue

        number = milestones.get(it.milestone)
        if number is None:
            number = gh.create_milestone(it.milestone, it.milestone_title)
            milestones[it.milestone] = number
            print(f"  +milestone {it.milestone} — {it.milestone_title}")

        url = gh.create_issue(it.issue_title, it.issue_body(it.milestone_title),
                              number, args.label)
        print(f"  create {it.title}\n         {url}")
        created += 1

    print(f"\n{created} created, {skipped} already present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
