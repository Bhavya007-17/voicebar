from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Catalog:
    apps: list[str] = field(default_factory=list)
    aliases: dict[str, str] = field(default_factory=dict)
    sites: dict[str, str] = field(default_factory=dict)
    engines: dict[str, str] = field(default_factory=dict)
