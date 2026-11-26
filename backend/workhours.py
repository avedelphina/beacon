"""When people are expected to look: the fleet's working hours.

Agents run around the clock; the people who answer them do not. A wait that is
measured on the clock expires a request made on Friday afternoon before anyone
could see it, so waits for a person count working hours only.

One window for the whole fleet, from BEACON_WORK_HOURS:
"Mon-Fri 08:00-17:00 Europe/Berlin" (days, hours, IANA time zone), or "always"
for clock time. Holidays and per-person hours are not known.
"""

import datetime
import os
import zoneinfo

ENV = "BEACON_WORK_HOURS"
DEFAULT = "Mon-Fri 08:00-17:00 UTC"
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def window() -> tuple[set[int], datetime.time, datetime.time, zoneinfo.ZoneInfo] | None:
    """(weekdays, start, end, zone), or None for "always". A bad value raises."""
    spec = (os.environ.get(ENV) or "").strip() or DEFAULT
    if spec.casefold() == "always":
        return None
    try:
        days, hours, zone = spec.split()
        first, _, last = days.casefold().partition("-")
        first, last = _DAYS.index(first), _DAYS.index(last or first)
        start, end = (datetime.time.fromisoformat(part) for part in hours.split("-"))
        if start >= end:
            raise ValueError("the window must start before it ends, on the same day")
        return ({(first + i) % 7 for i in range((last - first) % 7 + 1)}, start, end, zoneinfo.ZoneInfo(zone))
    except (ValueError, zoneinfo.ZoneInfoNotFoundError) as exc:
        raise ValueError(f'{ENV}={spec!r} is not "Mon-Fri 08:00-17:00 Europe/Berlin" or "always": {exc}') from exc


def day_seconds() -> int:
    """The length of one working day; 24 hours when the clock counts."""
    hours = window()
    if hours is None:
        return 24 * 3600
    _, start, end, _ = hours
    return (end.hour - start.hour) * 3600 + (end.minute - start.minute) * 60


def after(start: float, seconds: float) -> float:
    """The time at which `seconds` of working time have passed since `start`."""
    hours = window()
    if hours is None:
        return start + seconds
    days, opens, closes, zone = hours
    day = datetime.datetime.fromtimestamp(start, zone).date()
    while True:
        if day.weekday() in days:
            begin = max(start, datetime.datetime.combine(day, opens, zone).timestamp())
            left = datetime.datetime.combine(day, closes, zone).timestamp() - begin
            if left >= seconds:
                return begin + seconds
            seconds -= max(left, 0)
        day += datetime.timedelta(days=1)
