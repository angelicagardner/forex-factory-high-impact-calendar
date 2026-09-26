from __future__ import annotations

import hashlib
import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# --- Settings you may want to change ---------------------------------------
FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
COUNTRIES = {"USD"}  # e.g. {"USD", "EUR"}
IMPACTS = {"High"}  # feed values: "High", "Medium", "Low", "Holiday"
KEEP_DAYS = 45  # how long past events stay in the calendar
EVENT_MINUTES = 15  # length of each calendar entry
CALENDAR_NAME = "USD High Impact"
CALENDAR_DESCRIPTION = "High-impact USD economic events from the Forex Factory feed"
USER_AGENT = "usd-high-impact-calendar/1.0"
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "data" / "events.json"
ICS_FILE = ROOT / "docs" / "usd-high-impact.ics"


def fetch_feed(url: str = FEED_URL, timeout: int = 30) -> list[dict]:
    if not url.startswith("https://"):
        raise ValueError(f"Refusing to fetch a non-https URL: {url!r}")
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    request = urllib.request.Request(url, headers=headers)  # noqa: S310
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        data = json.load(response)
    if not isinstance(data, list):
        raise TypeError("Unexpected feed format: expected a JSON list")
    return data


def parse_date(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"Date without timezone: {value!r}")
    return parsed.astimezone(timezone.utc)


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def make_uid(country: str, title: str, start: datetime) -> str:
    key = f"{country}|{title}|{_utc(start)}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:20] + "@usd-high-impact"


def select_events(feed: list[dict]) -> tuple[dict[str, dict], int]:
    events: dict[str, dict] = {}
    skipped = 0
    for item in feed:
        if not isinstance(item, dict):
            skipped += 1
            continue
        if item.get("country") not in COUNTRIES or item.get("impact") not in IMPACTS:
            continue
        try:
            start = parse_date(item["date"])
        except (KeyError, TypeError, ValueError):
            skipped += 1
            continue
        title = str(item.get("title") or "").strip() or "Untitled event"
        uid = make_uid(item["country"], title, start)
        events[uid] = {
            "uid": uid,
            "country": item["country"],
            "title": title,
            "start": start.isoformat(),
            "forecast": str(item.get("forecast") or ""),
            "previous": str(item.get("previous") or ""),
        }
    return events, skipped


def feed_window(feed: list[dict]) -> tuple[datetime, datetime] | None:
    dates = []
    for item in feed:
        if not isinstance(item, dict):
            continue
        try:
            dates.append(parse_date(item["date"]))
        except (KeyError, TypeError, ValueError):
            continue
    if not dates:
        return None
    day_start = min(dates).replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = max(dates).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
        days=1
    )
    return day_start, day_end


def _content(event: dict) -> tuple:
    return (event["title"], event["start"], event["forecast"], event["previous"])


def merge(
    stored: dict[str, dict],
    fresh: dict[str, dict],
    window: tuple[datetime, datetime] | None,
    now: datetime,
    keep_days: int = KEEP_DAYS,
) -> dict[str, dict]:
    cutoff = now - timedelta(days=keep_days)
    merged: dict[str, dict] = {}

    for uid, event in stored.items():
        start = datetime.fromisoformat(event["start"])
        if start < cutoff:
            continue
        if window and window[0] <= start < window[1] and uid not in fresh:
            continue
        merged[uid] = event

    stamp_now = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for uid, event in fresh.items():
        previous = merged.get(uid)
        if previous and _content(previous) == _content(event):
            stamp = previous.get("stamp", stamp_now)
        else:
            stamp = stamp_now
        merged[uid] = {**event, "stamp": stamp}

    return merged


def escape_text(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


FOLD_LIMIT = 75  # max octets per line in an ICS file


def fold_line(line: str) -> str:
    if len(line.encode("utf-8")) <= FOLD_LIMIT:
        return line
    parts: list[str] = []
    current = ""
    current_len = 0
    limit = FOLD_LIMIT
    for char in line:
        char_len = len(char.encode("utf-8"))
        if current_len + char_len > limit:
            parts.append(current)
            current, current_len = char, char_len
            limit = FOLD_LIMIT - 1  # continuation lines start with a space
        else:
            current += char
            current_len += char_len
    parts.append(current)
    return "\r\n ".join(parts)


def build_ics(events: dict[str, dict]) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//usd-high-impact-calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{escape_text(CALENDAR_NAME)}",
        f"X-WR-CALDESC:{escape_text(CALENDAR_DESCRIPTION)}",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
    ]
    for event in sorted(events.values(), key=lambda e: (e["start"], e["title"])):
        start = datetime.fromisoformat(event["start"])
        end = start + timedelta(minutes=EVENT_MINUTES)
        details = []
        if event.get("forecast"):
            details.append(f"Forecast: {event['forecast']}")
        if event.get("previous"):
            details.append(f"Previous: {event['previous']}")
        details.append("Source: Forex Factory, https://www.forexfactory.com/calendar")
        description = escape_text("\n".join(details))
        summary = escape_text(f"{event['country']}: {event['title']}")
        lines += [
            "BEGIN:VEVENT",
            f"UID:{event['uid']}",
            f"DTSTAMP:{event.get('stamp', _utc(start))}",
            f"DTSTART:{_utc(start)}",
            f"DTEND:{_utc(end)}",
            f"SUMMARY:{summary}",
            f"DESCRIPTION:{description}",
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold_line(line) for line in lines) + "\r\n"


REQUIRED_KEYS = ("uid", "country", "title", "start", "forecast", "previous")


def load_state(path: Path = STATE_FILE) -> dict[str, dict]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8") or "{}")
    if not isinstance(data, dict):
        return {}
    events: dict[str, dict] = {}
    for uid, event in data.items():
        if not isinstance(event, dict) or not all(k in event for k in REQUIRED_KEYS):
            continue
        try:
            parse_date(str(event["start"]))
        except (TypeError, ValueError):
            continue
        events[uid] = event
    return events


def save_state(events: dict[str, dict], path: Path = STATE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = dict(
        sorted(events.items(), key=lambda kv: (kv[1]["start"], kv[1]["title"]))
    )
    path.write_text(
        json.dumps(ordered, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def main() -> int:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        feed = fetch_feed()
    except (OSError, ValueError, TypeError) as exc:
        print(
            f"ERROR: could not fetch feed, keeping the existing calendar: {exc}",
            file=sys.stderr,
        )
        return 1

    window = feed_window(feed)
    if window is None:
        print(
            "ERROR: feed had no dated events, keeping the existing calendar",
            file=sys.stderr,
        )
        return 1

    fresh, skipped = select_events(feed)
    merged = merge(load_state(), fresh, window, now)

    save_state(merged)
    ICS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ICS_FILE.write_text(build_ics(merged), encoding="utf-8", newline="")

    last_day = window[1] - timedelta(days=1)
    print(
        f"Feed week {window[0]:%Y-%m-%d} to {last_day:%Y-%m-%d}: "
        f"{len(fresh)} matching events, {len(merged)} in calendar, {skipped} skipped"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
