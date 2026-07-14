"""Модель события — то, что LLM извлекает и что уходит в календарь."""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import datetime


@dataclass
class Event:
    title: str
    start: datetime               # timezone-aware
    end: datetime | None = None   # если None и не all_day -> +1 час
    all_day: bool = False
    location: str | None = None
    notes: str | None = None
    reminders_minutes: list[int] = field(default_factory=list)
    recurrence: str | None = None  # RRULE-строка, например "FREQ=WEEKLY;BYDAY=TU"
    uid: str | None = None        # заполняется после записи в CalDAV

    def to_dict(self) -> dict:
        d = asdict(self)
        d["start"] = self.start.isoformat()
        d["end"] = self.end.isoformat() if self.end else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        return cls(
            title=d["title"],
            start=datetime.fromisoformat(d["start"]),
            end=datetime.fromisoformat(d["end"]) if d.get("end") else None,
            all_day=d.get("all_day", False),
            location=d.get("location"),
            notes=d.get("notes"),
            reminders_minutes=d.get("reminders_minutes", []),
            recurrence=d.get("recurrence"),
            uid=d.get("uid"),
        )
