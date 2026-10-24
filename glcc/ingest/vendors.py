"""Vendor name normalization. Emails and TMS records spell vendor names many ways."""

from __future__ import annotations

import json
import re
from pathlib import Path


class VendorRegistry:
    def __init__(self, vendors: list[dict]):
        self._vendors = vendors
        self._lookup: dict[str, str] = {}
        for v in vendors:
            for alias in [v["name"], v["key"], *v.get("aliases", [])]:
                self._lookup[self._norm(alias)] = v["name"]

    @staticmethod
    def _norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()

    @classmethod
    def load(cls, path: Path) -> "VendorRegistry":
        return cls(json.loads(Path(path).read_text(encoding="utf-8"))["vendors"])

    def canonical(self, raw: str) -> str | None:
        """Canonical vendor name for any known spelling, else None."""
        n = self._norm(raw)
        if n in self._lookup:
            return self._lookup[n]
        for alias, name in self._lookup.items():          # tolerate suffixes such as "Inc" or "Ltd"
            if alias and (n.startswith(alias + " ") or alias.startswith(n + " ")):
                return name
        return None

    def key_for(self, canonical: str) -> str | None:
        for v in self._vendors:
            if v["name"] == canonical:
                return v["key"]
        return None

    def by_key(self, key: str) -> str | None:
        for v in self._vendors:
            if v["key"].lower() == key.lower():
                return v["name"]
        return None
