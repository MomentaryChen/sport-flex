from __future__ import annotations

import json
from pathlib import Path

from sportflex.paths import OUTPUT_DIR

SNIPES_DIR = OUTPUT_DIR / "snipes"


class SnipeStore:
    """One JSON file per (venue, account) so an armed snipe survives a restart."""

    def __init__(self, venue_id: str, account_id: str, directory: Path | None = None) -> None:
        self.path = (directory or SNIPES_DIR) / f"{venue_id}__{account_id}.json"

    def load(self) -> dict | None:
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def save(self, spec: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        keep = {key: value for key, value in spec.items() if key != "phase"}
        self.path.write_text(json.dumps(keep, ensure_ascii=False, indent=2), encoding="utf-8")

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
