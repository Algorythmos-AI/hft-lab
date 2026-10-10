"""When the currency market is open, when it rolls over, and when it must be left alone. No clock in here."""
from hftlab.calendar.events import Calendar, CalendarError, Closure, MacroEvent, load_calendar
from hftlab.calendar.week import (
    in_rollover_window,
    is_week_open,
    last_rollover,
    new_york,
    next_close,
    next_open,
    rollover_near,
)

__all__ = [
    "Calendar",
    "CalendarError",
    "Closure",
    "MacroEvent",
    "in_rollover_window",
    "is_week_open",
    "last_rollover",
    "load_calendar",
    "new_york",
    "next_close",
    "next_open",
    "rollover_near",
]
