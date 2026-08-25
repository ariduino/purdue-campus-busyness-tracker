import csv
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import scrape_dining as pipeline


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((Path(__file__).parents[1] / "config.json").read_text())
        self.registry = json.loads((Path(__file__).parents[1] / "location_registry.json").read_text())

    def test_committed_config_and_registry_are_valid(self):
        pipeline.validate_config(self.config)
        pipeline.validate_registry(self.registry, self.config)

    def test_time_window_is_inclusive(self):
        zone = ZoneInfo("America/Indiana/Indianapolis")
        self.assertTrue(pipeline.is_active(datetime(2026, 8, 25, 7, 0, tzinfo=zone), self.config))
        self.assertTrue(pipeline.is_active(datetime(2026, 8, 25, 21, 0, tzinfo=zone), self.config))
        self.assertFalse(pipeline.is_active(datetime(2026, 8, 25, 21, 1, tzinfo=zone), self.config))

    def test_place_id_wins_over_url(self):
        location = self.config["locations"][0]
        registry_entry = {"google_place_id": "stable-google-id", "canonical_url": None}
        item = {"placeId": "stable-google-id", "url": "https://maps.google.com/changed", "currentBusynessPct": 46}
        resolved, status = pipeline.resolve_location(location, registry_entry, [item])
        self.assertIs(resolved, item)
        self.assertEqual(status, "matched_google_place_id")

    def test_compass_actor_input_uses_precise_searches_and_detail_pages(self):
        actor_input = pipeline.build_actor_input(self.config["locations"][:2])
        self.assertEqual(actor_input["searchStringsArray"], [item["search_query"] for item in self.config["locations"][:2]])
        self.assertEqual(actor_input["locationQuery"], "West Lafayette, IN")
        self.assertEqual(actor_input["maxCrawledPlacesPerSearch"], 1)
        self.assertEqual(actor_input["searchMatching"], "only_includes")
        self.assertTrue(actor_input["scrapePlaceDetailPage"])
        self.assertFalse(actor_input["scrapeReviewsPersonalData"])
        self.assertEqual(actor_input["language"], "en")
        self.assertNotIn("startUrls", actor_input)

    def test_actor_error_redacts_a_token(self):
        message = pipeline.safe_actor_error(RuntimeError("token=secret-value request rejected"))
        self.assertNotIn("secret-value", message)
        self.assertIn("[REDACTED]", message)

    def test_bootstrap_is_strict_and_discovery_needs_approval(self):
        location = self.config["locations"][0]
        item = {
            "placeId": "new-google-id",
            "placeUrl": "https://www.google.com/maps/place/Wiley+Dining+Court/",
            "placeName": "Wiley Dining Court",
            "placeAddress": "498 S Martin Jischke Drive, West Lafayette, IN 47906",
            "currentBusynessPct": 52,
        }
        rows, discoveries = pipeline.build_rows(datetime(2026, 8, 25, 12, 0, tzinfo=pipeline.TIMEZONE), [location], {"locations": {location["id"]: {"google_place_id": None, "canonical_url": None}}}, [item])
        self.assertEqual(rows[0]["Source_Status"], "unverified_identifier_candidate")
        self.assertEqual(discoveries[location["id"]]["google_place_id"], "new-google-id")

    def test_unavailable_busyness_is_recorded_not_failed(self):
        location = self.config["locations"][0]
        item = {"placeId": "known", "currentBusynessPct": None}
        rows, _ = pipeline.build_rows(datetime(2026, 8, 25, 12, 0, tzinfo=pipeline.TIMEZONE), [location], {"locations": {location["id"]: {"google_place_id": "known", "canonical_url": None}}}, [item])
        self.assertEqual(rows[0]["Current_Busyness_Pct"], "")
        self.assertIn("live_busyness_unavailable", rows[0]["Source_Status"])

    def test_remote_actor_failure_creates_traceable_empty_rows(self):
        now = datetime(2026, 8, 25, 12, 0, tzinfo=pipeline.TIMEZONE)
        rows = pipeline.error_rows(now, self.config["locations"][:2], "TimeoutError")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["Current_Busyness_Pct"], "")
        self.assertEqual(rows[1]["Source_Status"], "actor_error:TimeoutError")

    def test_zero_result_status_rows_are_distinct_from_actor_failure(self):
        now = datetime(2026, 8, 25, 12, 0, tzinfo=pipeline.TIMEZONE)
        rows = pipeline.status_rows(now, self.config["locations"][:1], "actor_returned_zero_results")
        self.assertEqual(rows[0]["Source_Status"], "actor_returned_zero_results")

    def test_single_location_selection(self):
        selected = pipeline.select_locations(self.config["locations"], "wiley-dining-court")
        self.assertEqual([item["id"] for item in selected], ["wiley-dining-court"])
        with self.assertRaises(pipeline.ConfigurationError):
            pipeline.select_locations(self.config["locations"], "not-a-location")

    def test_csv_is_created_once_with_headers(self):
        rows = [{field: "" for field in pipeline.CSV_FIELDS}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "weekly.csv"
            pipeline.append_rows(path, rows)
            pipeline.append_rows(path, rows)
            with path.open(newline="", encoding="utf-8") as source:
                values = list(csv.reader(source))
        self.assertEqual(values[0], pipeline.CSV_FIELDS)
        self.assertEqual(len(values), 3)


if __name__ == "__main__":
    unittest.main()
