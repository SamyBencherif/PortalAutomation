"""Where capabilities live.

A directory of JSON files, one per version, named `<id>@<version>.json`. That
is not a placeholder for "a real database" -- it is the right storage for this
object. Capabilities are reviewed, diffed and approved by humans, and a text
file in git gives you review, history, blame and rollback for free. A row in
Postgres gives you none of those without building them.

Versions are kept, never overwritten. An artifact that is replaying in
production is a contract with a running caller; editing it in place would
change that caller's behaviour with no diff and no way back.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from cua.artifact.schema import Capability

FILENAME = re.compile(r"^(?P<id>[a-z0-9_.]+)@(?P<version>\d+\.\d+\.\d+)\.json$")


class CapabilityNotFound(KeyError):
    pass


class CapabilityExists(FileExistsError):
    """Refusing to overwrite a published version. Bump instead."""


class Store:
    def __init__(self, root: str | Path = "capabilities") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, cap_id: str, version: str) -> Path:
        return self.root / f"{cap_id}@{version}.json"

    def save(self, cap: Capability, overwrite: bool = False) -> Path:
        path = self.path_for(cap.id, cap.version)
        if path.exists() and not overwrite:
            raise CapabilityExists(
                f"{cap.ref} already exists. Bump the version rather than "
                f"editing a published capability in place."
            )
        # Sorted keys and a trailing newline: this file is reviewed as a diff,
        # so a stable serialisation is part of its job.
        path.write_text(
            json.dumps(cap.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
        )
        return path

    def load(self, ref: str) -> Capability:
        """Load by `id@version`, or by bare `id` for the newest version."""
        if "@" in ref:
            cap_id, _, version = ref.partition("@")
            path = self.path_for(cap_id, version)
            if not path.exists():
                raise CapabilityNotFound(ref)
            return Capability.model_validate_json(path.read_text())

        versions = self.versions(ref)
        if not versions:
            raise CapabilityNotFound(ref)
        return self.load(f"{ref}@{versions[-1]}")

    def versions(self, cap_id: str) -> list[str]:
        found = []
        for path in self.root.glob(f"{cap_id}@*.json"):
            m = FILENAME.match(path.name)
            if m and m.group("id") == cap_id:
                found.append(m.group("version"))
        return sorted(found, key=_semver_key)

    def list(self) -> list[Capability]:
        """Every capability, newest version of each.

        This is the catalogue an agent would browse to discover what it can
        invoke, which is why it returns the objects rather than filenames.
        """
        latest: dict[str, str] = {}
        for path in self.root.glob("*.json"):
            m = FILENAME.match(path.name)
            if not m:
                continue
            cap_id, version = m.group("id"), m.group("version")
            if cap_id not in latest or _semver_key(version) > _semver_key(latest[cap_id]):
                latest[cap_id] = version
        return [self.load(f"{i}@{v}") for i, v in sorted(latest.items())]


def _semver_key(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split("."))


def bump(version: str, part: str = "patch") -> str:
    major, minor, patch = (int(p) for p in version.split("."))
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


__all__ = ["Store", "CapabilityNotFound", "CapabilityExists", "bump"]
