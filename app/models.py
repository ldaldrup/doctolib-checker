from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class BookingMeta:
    state_key: str
    practice_name: str
    practitioner_name: str
    motive_id: str
    agenda_ids_str: str
    practice_id: str
    display_name: str


@dataclass
class SessionStats:
    """In-memory stats for the current run session. Resets on restart."""

    session_start: datetime
    total_cycles: int = 0
    total_hits: int = 0
    total_errors: int = 0
    last_summary_sent: Optional[datetime] = None
    last_slot_time: Optional[datetime] = None
    last_slot_practitioner: Optional[str] = None
    last_slot_practice: Optional[str] = None
    last_slot_total: int = 0
