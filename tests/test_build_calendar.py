from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import build_calendar as bc

UTC = timezone.utc


def item(title, date, country="USD", impact="High", forecast="", previous=""):
    return {
        "title": title,
        "country": country,
        "date": date,
        "impact": impact,
        "forecast": forecast,
        "previous": previous,
    }


class TestParseDate(unittest.TestCase):
    def test_converts_offset_to_utc(self):
        parsed = bc.parse_date("2026-09-24T08:30:00-04:00")
        self.assertEqual(parsed, datetime(2026, 9, 24, 12, 30, tzinfo=UTC))

    def test_rejects_naive_date(self):
        with self.assertRaises(ValueError):
            bc.parse_date("2026-09-24T08:30:00")

    def test_rejects_garbage(self):
        with self.assertRaises(ValueError):
            bc.parse_date("not a date")


class TestMakeUid(unittest.TestCase):
    def test_same_event_gives_same_uid(self):
        start = datetime(2026, 9, 24, 12, 30, tzinfo=UTC)
        self.assertEqual(
            bc.make_uid("USD", "CPI m/m", start), bc.make_uid("USD", "CPI m/m", start)
        )

    def test_same_title_twice_on_one_day_gives_two_uids(self):
        morning = datetime(2026, 9, 24, 12, 30, tzinfo=UTC)
        evening = datetime(2026, 9, 24, 20, 0, tzinfo=UTC)
        self.assertNotEqual(
            bc.make_uid("USD", "President Trump Speaks", morning),
            bc.make_uid("USD", "President Trump Speaks", evening),
        )

    def test_uid_is_timezone_independent(self):
        utc_start = datetime(2026, 9, 24, 12, 30, tzinfo=UTC)
        other = bc.parse_date("2026-09-24T08:30:00-04:00")
        self.assertEqual(
            bc.make_uid("USD", "CPI m/m", utc_start),
            bc.make_uid("USD", "CPI m/m", other),
        )


class TestSelectEvents(unittest.TestCase):
    def test_filters_country_and_impact(self):
        feed = [
            item("CPI m/m", "2026-09-24T08:30:00-04:00"),
            item("German CPI", "2026-09-24T08:30:00-04:00", country="EUR"),
            item("Crude Oil Inventories", "2026-09-24T10:30:00-04:00", impact="Low"),
        ]
        events, skipped = bc.select_events(feed)
        self.assertEqual([e["title"] for e in events.values()], ["CPI m/m"])
        self.assertEqual(skipped, 0)

    def test_counts_unusable_items(self):
        feed = [
            item("No date", "2026-09-24T08:30:00-04:00"),
            {"title": "Broken", "country": "USD", "impact": "High"},
            "not a dict",
        ]
        events, skipped = bc.select_events(feed)
        self.assertEqual(len(events), 1)
        self.assertEqual(skipped, 2)

    def test_keeps_both_same_day_events(self):
        feed = [
            item("Fed Chair Powell Speaks", "2026-09-24T08:30:00-04:00"),
            item("Fed Chair Powell Speaks", "2026-09-24T15:00:00-04:00"),
        ]
        events, _ = bc.select_events(feed)
        self.assertEqual(len(events), 2)

    def test_missing_title_falls_back(self):
        feed = [item("", "2026-09-24T08:30:00-04:00")]
        events, _ = bc.select_events(feed)
        self.assertEqual(next(iter(events.values()))["title"], "Untitled event")


class TestFeedWindow(unittest.TestCase):
    def test_window_covers_whole_days(self):
        feed = [
            item("A", "2026-09-21T04:30:00-04:00"),
            item("B", "2026-09-25T14:00:00-04:00"),
        ]
        start, end = bc.feed_window(feed)
        self.assertEqual(start, datetime(2026, 9, 21, tzinfo=UTC))
        self.assertEqual(end, datetime(2026, 9, 26, tzinfo=UTC))

    def test_no_dates_returns_none(self):
        self.assertIsNone(bc.feed_window([{"title": "x"}, "junk"]))


class TestMerge(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 24, 6, 0, tzinfo=UTC)
        self.window = (
            datetime(2026, 9, 21, tzinfo=UTC),
            datetime(2026, 9, 26, tzinfo=UTC),
        )

    def stored(self, uid, title, start, stamp="20260101T000000Z"):
        return {
            uid: {
                "uid": uid,
                "country": "USD",
                "title": title,
                "start": start,
                "forecast": "",
                "previous": "",
                "stamp": stamp,
            }
        }

    def test_drops_events_older_than_keep_days(self):
        old = self.stored("old", "Ancient CPI", "2026-01-01T12:30:00+00:00")
        merged = bc.merge(old, {}, self.window, self.now, keep_days=45)
        self.assertEqual(merged, {})

    def test_keeps_recent_history_outside_window(self):
        recent = self.stored("recent", "Last week CPI", "2026-09-10T12:30:00+00:00")
        merged = bc.merge(recent, {}, self.window, self.now, keep_days=45)
        self.assertIn("recent", merged)

    def test_drops_cancelled_event_inside_window(self):
        cancelled = self.stored("gone", "Cancelled", "2026-09-23T12:30:00+00:00")
        merged = bc.merge(cancelled, {}, self.window, self.now)
        self.assertEqual(merged, {})

    def test_drops_cancelled_event_before_first_remaining_event(self):
        """Regression: the window must cover whole days, not min..max event times."""
        cancelled = self.stored("gone", "Cancelled", "2026-09-21T01:00:00+00:00")
        merged = bc.merge(cancelled, {}, self.window, self.now)
        self.assertEqual(merged, {})

    def test_unchanged_event_keeps_its_stamp(self):
        start = "2026-09-24T12:30:00+00:00"
        stored = self.stored("uid1", "CPI m/m", start, stamp="20260920T051500Z")
        fresh = {"uid1": dict(stored["uid1"])}
        fresh["uid1"].pop("stamp")
        merged = bc.merge(stored, fresh, self.window, self.now)
        self.assertEqual(merged["uid1"]["stamp"], "20260920T051500Z")

    def test_changed_event_gets_new_stamp(self):
        start = "2026-09-24T12:30:00+00:00"
        stored = self.stored("uid1", "CPI m/m", start, stamp="20260920T051500Z")
        fresh = {"uid1": dict(stored["uid1"], forecast="0.3%")}
        fresh["uid1"].pop("stamp")
        merged = bc.merge(stored, fresh, self.window, self.now)
        self.assertEqual(merged["uid1"]["stamp"], "20260924T060000Z")

    def test_rescheduled_event_does_not_duplicate(self):
        old_start = datetime(2026, 9, 24, 12, 30, tzinfo=UTC)
        new_start = datetime(2026, 9, 24, 18, 0, tzinfo=UTC)
        old_uid = bc.make_uid("USD", "FOMC Statement", old_start)
        stored = self.stored(old_uid, "FOMC Statement", old_start.isoformat())
        fresh, _ = bc.select_events([item("FOMC Statement", new_start.isoformat())])
        merged = bc.merge(stored, fresh, self.window, self.now)
        self.assertEqual(len(merged), 1)
        self.assertEqual(
            datetime.fromisoformat(next(iter(merged.values()))["start"]), new_start
        )


class TestEscapeAndFold(unittest.TestCase):
    def test_escapes_special_characters(self):
        self.assertEqual(bc.escape_text("a,b;c\\d\ne"), "a\\,b\\;c\\\\d\\ne")

    def test_backslash_escaped_only_once(self):
        self.assertEqual(bc.escape_text("\\"), "\\\\")

    def test_short_line_untouched(self):
        self.assertEqual(bc.fold_line("SUMMARY:short"), "SUMMARY:short")

    def test_folded_segments_fit_75_octets(self):
        folded = bc.fold_line("SUMMARY:" + "x" * 300)
        segments = folded.split("\r\n")
        self.assertGreater(len(segments), 1)
        for i, segment in enumerate(segments):
            self.assertLessEqual(len(segment.encode("utf-8")), 75)
            if i:
                self.assertTrue(segment.startswith(" "))
        self.assertEqual(folded.replace("\r\n ", ""), "SUMMARY:" + "x" * 300)

    def test_never_splits_a_utf8_character(self):
        folded = bc.fold_line("SUMMARY:" + "é" * 80)
        for segment in folded.split("\r\n"):
            segment.encode("utf-8").decode("utf-8")  # must not raise
        self.assertEqual(folded.replace("\r\n ", ""), "SUMMARY:" + "é" * 80)


class TestBuildIcs(unittest.TestCase):
    def build(self):
        feed = [
            item(
                "CPI m/m", "2026-09-24T08:30:00-04:00", forecast="0.3%", previous="0.2%"
            ),
            item("Non-Farm Employment Change", "2026-09-25T08:30:00-04:00"),
        ]
        events, _ = bc.select_events(feed)
        merged = bc.merge(
            {}, events, bc.feed_window(feed), datetime(2026, 9, 24, 6, tzinfo=UTC)
        )
        return bc.build_ics(merged)

    def test_structure_and_crlf(self):
        ics = self.build()
        self.assertTrue(ics.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertTrue(ics.endswith("END:VCALENDAR\r\n"))
        self.assertEqual(ics.count("BEGIN:VEVENT"), 2)
        self.assertEqual(ics.count("END:VEVENT"), 2)
        self.assertNotIn("\n\n", ics)
        for line in ics.split("\r\n"):
            self.assertNotIn("\n", line)

    def test_event_fields(self):
        ics = self.build()
        self.assertIn("DTSTART:20260924T123000Z", ics)
        self.assertIn("DTEND:20260924T124500Z", ics)
        self.assertIn("SUMMARY:USD: CPI m/m", ics)
        self.assertIn("Forecast: 0.3%", ics)

    def test_events_sorted_by_start(self):
        ics = self.build()
        self.assertLess(ics.index("20260924T123000Z"), ics.index("20260925T123000Z"))

    def test_empty_calendar_is_still_valid(self):
        ics = bc.build_ics({})
        self.assertIn("BEGIN:VCALENDAR", ics)
        self.assertNotIn("BEGIN:VEVENT", ics)


class TestState(unittest.TestCase):
    def test_round_trip(self):
        feed = [item("CPI m/m", "2026-09-24T08:30:00-04:00")]
        events, _ = bc.select_events(feed)
        merged = bc.merge(
            {}, events, bc.feed_window(feed), datetime(2026, 9, 24, 6, tzinfo=UTC)
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data" / "events.json"
            bc.save_state(merged, path)
            self.assertEqual(bc.load_state(path), merged)

    def test_missing_file_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(bc.load_state(Path(tmp) / "nope.json"), {})

    def test_corrupt_entries_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.json"
            path.write_text(
                json.dumps(
                    {
                        "good": {
                            "uid": "good",
                            "country": "USD",
                            "title": "CPI m/m",
                            "start": "2026-09-24T12:30:00+00:00",
                            "forecast": "",
                            "previous": "",
                        },
                        "missing_keys": {"uid": "missing_keys"},
                        "bad_date": {
                            "uid": "bad_date",
                            "country": "USD",
                            "title": "x",
                            "start": "whenever",
                            "forecast": "",
                            "previous": "",
                        },
                        "not_a_dict": 42,
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(list(bc.load_state(path)), ["good"])

    def test_top_level_list_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.json"
            path.write_text("[]", encoding="utf-8")
            self.assertEqual(bc.load_state(path), {})


class TestFetchFeed(unittest.TestCase):
    def test_rejects_non_https_url(self):
        with self.assertRaises(ValueError):
            bc.fetch_feed("file:///etc/passwd")


class TestRunOverTwoWeeks(unittest.TestCase):
    def test_history_is_kept_across_runs(self):
        week1 = [item("CPI m/m", "2026-09-10T08:30:00-04:00")]
        now1 = datetime(2026, 9, 11, 5, 15, tzinfo=UTC)
        state1 = bc.merge({}, bc.select_events(week1)[0], bc.feed_window(week1), now1)

        week2 = [item("Non-Farm Employment Change", "2026-09-18T08:30:00-04:00")]
        now2 = now1 + timedelta(days=7)
        state2 = bc.merge(
            state1, bc.select_events(week2)[0], bc.feed_window(week2), now2
        )

        titles = sorted(e["title"] for e in state2.values())
        self.assertEqual(titles, ["CPI m/m", "Non-Farm Employment Change"])


if __name__ == "__main__":
    unittest.main()
