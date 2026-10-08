from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from sportflex.core.rules import BookingRules
from sportflex.paths import VENUES_DIR


class Venue(BaseModel):
    """One sports centre. Adding a centre on a known platform is just a new venues/*.yaml."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str
    provider: str  # key in sportflex.providers.registry
    params: dict = Field(default_factory=dict)  # handed to the provider, e.g. Changjia LID
    rules: BookingRules = Field(default_factory=BookingRules)
    categories: list[str] = Field(min_length=1)
    prices: dict = Field(default_factory=dict)  # informational only
    info: dict = Field(default_factory=dict)  # address, phone, notes …

    def public(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "provider": self.provider,
            "categories": self.categories,
            "prices": self.prices,
            "info": self.info,
            "rules": self.rules.model_dump(),
            "rulesText": self.rules.describe(),
        }


@lru_cache(maxsize=1)
def load_venues(directory: Path = VENUES_DIR) -> dict[str, Venue]:
    venues: dict[str, Venue] = {}
    for path in sorted(directory.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        try:
            venue = Venue.model_validate(raw)
        except Exception as exc:
            raise ValueError(f"{path.name} 設定有誤：{exc}") from exc
        if venue.id in venues:
            raise ValueError(f"場館 id 重複：{venue.id}")
        venues[venue.id] = venue
    if not venues:
        raise ValueError(f"{directory} 裡沒有場館設定")
    return venues


def get_venue(venue_id: str | None = None) -> Venue:
    venues = load_venues()
    if not venue_id:
        return next(iter(venues.values()))
    if venue_id in venues:
        return venues[venue_id]
    names = "、".join(f"{v.id}（{v.name}）" for v in venues.values())
    raise ValueError(f"找不到場館「{venue_id}」。可選：{names}")
