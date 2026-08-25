"""Collect live busyness data for configured Purdue campus locations.

The internal config ID identifies the tracked facility.  A separate committed
registry maps that ID to Google's stable place ID after a reviewed bootstrap.
URLs are only used to request and bootstrap the mapping; they are never the
primary identity after a stable ID has been captured.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
import unicodedata
from datetime import datetime, time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
REGISTRY_PATH = ROOT / "location_registry.json"
DATA_DIR = ROOT / "data"
TIMEZONE = ZoneInfo("America/Indiana/Indianapolis")
ACTOR_ID = "compass/crawler-google-places"
CSV_FIELDS = [
    "Timestamp_EST",
    "Location_ID",
    "Location_Name",
    "Current_Busyness_Pct",
    "Source_Status",
    "Google_Place_ID",
    "Source_URL",
]
PLACE_ID_FIELDS = ("googlePlaceId", "placeId", "place_id", "cid")
URL_FIELDS = ("url", "placeUrl", "place_url", "googleMapsUrl")
NAME_FIELDS = ("title", "name", "placeName")
ADDRESS_FIELDS = ("address", "fullAddress", "placeAddress")
BUSYNESS_FIELDS = (
    "currentBusynessPct",
    "popularTimesCurrent",
    "popularTimesLivePercent",
    "liveBusynessPct",
    "liveBusyness",
    "currentPopularity",
)
LIVE_FIELD_MARKERS = ("live", "current")
BUSYNESS_FIELD_MARKERS = ("busy", "popular", "occup", "crowd", "percent")
COORDINATE_FIELDS = (("latitude", "longitude"), ("lat", "lng"), ("lat", "lon"))


class ConfigurationError(ValueError):
    """Raised when a committed configuration file is invalid."""


class ActorExecutionError(RuntimeError):
    """Raised for a failed remote collection after configuration was validated."""


def load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as source:
            value = json.load(source)
    except FileNotFoundError as error:
        raise ConfigurationError(f"Required file not found: {path.name}") from error
    except json.JSONDecodeError as error:
        raise ConfigurationError(f"Invalid JSON in {path.name}: {error}") from error
    if not isinstance(value, dict):
        raise ConfigurationError(f"{path.name} must contain a JSON object")
    return value


def parse_clock(value: Any, field: str) -> time:
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ConfigurationError(f"{field} must use HH:MM 24-hour format")
    return time.fromisoformat(value)


def validate_config(config: dict[str, Any]) -> None:
    start = parse_clock(config.get("start_time_est"), "start_time_est")
    end = parse_clock(config.get("end_time_est"), "end_time_est")
    if start > end:
        raise ConfigurationError("Overnight time windows are not supported")
    locations = config.get("locations")
    if not isinstance(locations, list) or not locations:
        raise ConfigurationError("locations must be a non-empty list")
    ids: set[str] = set()
    for location in locations:
        if not isinstance(location, dict):
            raise ConfigurationError("Each location must be an object")
        location_id = location.get("id")
        if not isinstance(location_id, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", location_id):
            raise ConfigurationError("Each location id must be lowercase kebab-case")
        if location_id in ids:
            raise ConfigurationError(f"Duplicate location id: {location_id}")
        ids.add(location_id)
        if not isinstance(location.get("name"), str) or not location["name"].strip():
            raise ConfigurationError(f"Location {location_id} needs a name")
        url = location.get("url")
        if url is not None and (not isinstance(url, str) or urlparse(url).scheme != "https" or not urlparse(url).netloc):
            raise ConfigurationError(f"Location {location_id} has an invalid optional URL")
        if not isinstance(location.get("search_query"), str) or not location["search_query"].strip():
            raise ConfigurationError(f"Location {location_id} needs a search_query")
        fallback_queries = location.get("fallback_search_queries", [])
        if not isinstance(fallback_queries, list) or not all(isinstance(query, str) and query.strip() for query in fallback_queries):
            raise ConfigurationError(f"Location {location_id} has invalid fallback_search_queries")
        start_urls = location.get("start_urls", [])
        if not isinstance(start_urls, list) or not all(
            isinstance(url, str) and urlparse(url).scheme == "https" and urlparse(url).netloc for url in start_urls
        ):
            raise ConfigurationError(f"Location {location_id} has invalid start_urls")
        if not isinstance(location.get("location_query"), str) or not location["location_query"].strip():
            raise ConfigurationError(f"Location {location_id} needs a location_query")
        match_names = location.get("match_names")
        if not isinstance(match_names, list) or not match_names or not all(isinstance(name, str) and name.strip() for name in match_names):
            raise ConfigurationError(f"Location {location_id} needs non-empty match_names")
        if not isinstance(location.get("address_hint"), str) or not location["address_hint"].strip():
            raise ConfigurationError(f"Location {location_id} needs an address_hint")
        tokens = location.get("address_match_tokens")
        variants = location.get("address_match_variants")
        tokens_valid = isinstance(tokens, list) and bool(tokens) and all(isinstance(token, str) and token.strip() for token in tokens)
        variants_valid = (
            isinstance(variants, list)
            and bool(variants)
            and all(isinstance(variant, list) and variant and all(isinstance(token, str) and token.strip() for token in variant) for variant in variants)
        )
        if not tokens_valid and not variants_valid:
            raise ConfigurationError(f"Location {location_id} needs address_match_tokens or address_match_variants")
        coordinates = location.get("expected_coordinates")
        if coordinates is not None:
            if (
                not isinstance(coordinates, dict)
                or not isinstance(coordinates.get("latitude"), (int, float))
                or not isinstance(coordinates.get("longitude"), (int, float))
                or not -90 <= coordinates["latitude"] <= 90
                or not -180 <= coordinates["longitude"] <= 180
            ):
                raise ConfigurationError(f"Location {location_id} has invalid expected_coordinates")
            tolerance = location.get("coordinate_tolerance_meters", 150)
            if not isinstance(tolerance, (int, float)) or isinstance(tolerance, bool) or tolerance <= 0 or tolerance > 1_000:
                raise ConfigurationError(f"Location {location_id} has invalid coordinate_tolerance_meters")


def validate_registry(registry: dict[str, Any], config: dict[str, Any]) -> None:
    if registry.get("version") != 1 or not isinstance(registry.get("locations"), dict):
        raise ConfigurationError("location_registry.json must use version 1 with a locations object")
    configured_ids = {item["id"] for item in config["locations"]}
    if set(registry["locations"]) != configured_ids:
        raise ConfigurationError("Registry location IDs must exactly match config.json")
    for location_id, entry in registry["locations"].items():
        if not isinstance(entry, dict):
            raise ConfigurationError(f"Registry entry {location_id} must be an object")
        place_id = entry.get("google_place_id")
        url = entry.get("canonical_url")
        if place_id is not None and (not isinstance(place_id, str) or not place_id.strip()):
            raise ConfigurationError(f"Registry place ID for {location_id} is invalid")
        if url is not None and (not isinstance(url, str) or not url.startswith("https://")):
            raise ConfigurationError(f"Registry canonical URL for {location_id} is invalid")


def is_active(now: datetime, config: dict[str, Any]) -> bool:
    start = parse_clock(config["start_time_est"], "start_time_est")
    end = parse_clock(config["end_time_est"], "end_time_est")
    return start <= now.timetz().replace(tzinfo=None) <= end


def normalized_text(value: Any) -> str:
    ascii_value = unicodedata.normalize("NFKD", unquote(str(value or ""))).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", ascii_value.lower()).strip()


def normalized_url(value: Any) -> str:
    parsed = urlparse(str(value or ""))
    return f"{parsed.netloc.lower()}{unquote(parsed.path).rstrip('/').lower()}"


def first_value(item: dict[str, Any], fields: tuple[str, ...]) -> str | None:
    for field in fields:
        value = item.get(field)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def candidate_place_id(item: dict[str, Any]) -> str | None:
    return first_value(item, PLACE_ID_FIELDS)


def candidate_url(item: dict[str, Any]) -> str | None:
    return first_value(item, URL_FIELDS)


def bounded_percentage(value: Any) -> int | None:
    """Return a percentage only when the source value is an unambiguous 0–100 number."""
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if 0 <= result <= 100 else None


def nested_live_busyness(value: Any, key: str = "") -> int | None:
    """Find explicitly live/current busyness values without treating historical data as live."""
    key_normalized = normalized_text(key).replace(" ", "")
    is_live_key = any(marker in key_normalized for marker in LIVE_FIELD_MARKERS)
    is_busyness_key = any(marker in key_normalized for marker in BUSYNESS_FIELD_MARKERS)
    if is_live_key and is_busyness_key:
        percentage = bounded_percentage(value)
        if percentage is not None:
            return percentage
        if isinstance(value, dict):
            for nested_key in ("percent", "percentage", "value", "current"):
                percentage = bounded_percentage(value.get(nested_key))
                if percentage is not None:
                    return percentage
    if isinstance(value, dict):
        for nested_key, nested_value in value.items():
            percentage = nested_live_busyness(nested_value, str(nested_key))
            if percentage is not None:
                return percentage
    if isinstance(value, list):
        for nested_value in value:
            percentage = nested_live_busyness(nested_value, key)
            if percentage is not None:
                return percentage
    return None


def candidate_busyness(item: dict[str, Any]) -> int | None:
    for field in BUSYNESS_FIELDS:
        value = item.get(field)
        percentage = bounded_percentage(value)
        if percentage is not None:
            return percentage
    return nested_live_busyness(item)


def candidate_coordinates(item: dict[str, Any]) -> tuple[float, float] | None:
    for latitude_field, longitude_field in COORDINATE_FIELDS:
        latitude, longitude = item.get(latitude_field), item.get(longitude_field)
        if isinstance(latitude, (int, float)) and isinstance(longitude, (int, float)):
            return float(latitude), float(longitude)
    coordinates = item.get("coordinates")
    if isinstance(coordinates, dict):
        return candidate_coordinates(coordinates)
    return None


def distance_meters(first: tuple[float, float], second: tuple[float, float]) -> float:
    """Calculate great-circle distance using the Haversine formula."""
    latitude_1, longitude_1, latitude_2, longitude_2 = map(math.radians, (*first, *second))
    a = math.sin((latitude_2 - latitude_1) / 2) ** 2 + math.cos(latitude_1) * math.cos(latitude_2) * math.sin((longitude_2 - longitude_1) / 2) ** 2
    return 6_371_000 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def candidate_is_closed(item: dict[str, Any]) -> bool:
    for field in ("isOpen", "openNow", "isOpenNow", "currentlyOpen"):
        if item.get(field) is False:
            return True
    status = normalized_text(item.get("businessStatus"))
    return "closed" in status


def has_historical_popular_times(item: dict[str, Any]) -> bool:
    def contains_popular_times(value: Any, key: str = "") -> bool:
        key_normalized = normalized_text(key).replace(" ", "")
        if "populartime" in key_normalized and value not in (None, "", [], {}):
            return True
        if isinstance(value, dict):
            return any(contains_popular_times(nested_value, str(nested_key)) for nested_key, nested_value in value.items())
        if isinstance(value, list):
            return any(contains_popular_times(nested_value, key) for nested_value in value)
        return False
    return contains_popular_times(item)


def build_actor_input(locations: list[dict[str, Any]], registry: dict[str, Any]) -> dict[str, Any]:
    """Scrape saved Google IDs directly and search only locations still unresolved."""
    location_queries = {location["location_query"] for location in locations}
    if len(location_queries) != 1:
        raise ConfigurationError("A Compass run can use only one location_query")
    search_strings = list(dict.fromkeys(
        query
        for location in locations
        if not registry["locations"][location["id"]].get("google_place_id")
        for query in [location["search_query"], *location.get("fallback_search_queries", [])]
    ))
    start_urls = list(dict.fromkeys(
        url
        for location in locations
        if not registry["locations"][location["id"]].get("google_place_id")
        for url in location.get("start_urls", [])
    ))
    place_ids = list(dict.fromkeys(
        registry["locations"][location["id"]]["google_place_id"]
        for location in locations
        if registry["locations"][location["id"]].get("google_place_id")
    ))
    actor_input = {
        # Local name/address checks remain the authority for unresolved
        # locations. Multiple candidates handle Google name formatting.
        "maxCrawledPlacesPerSearch": 3,
        "searchMatching": "all",
        "language": "en",
        "scrapePlaceDetailPage": True,
        "maxReviews": 0,
        "maxImages": 0,
        "scrapeReviewsPersonalData": False,
        "scrapeContacts": False,
    }
    if place_ids:
        actor_input["placeIds"] = place_ids
    if search_strings:
        actor_input["searchStringsArray"] = search_strings
        actor_input["locationQuery"] = location_queries.pop()
    if start_urls:
        actor_input["startUrls"] = [{"url": url} for url in start_urls]
    return actor_input


def safe_actor_error(error: Exception) -> str:
    """Keep Action diagnostics useful while preventing accidental token disclosure."""
    message = str(error)
    message = re.sub(r"(?i)(token|api[_-]?key|authorization)(=|:|%3D)[^\s&]+", r"\1\2[REDACTED]", message)
    status_code = getattr(error, "status_code", None)
    prefix = f"HTTP {status_code}: " if status_code else ""
    return (prefix + message).strip() or type(error).__name__


def bootstrap_matches(location: dict[str, Any], item: dict[str, Any]) -> bool:
    """Allow discovery only for one strict, reviewable URL/name/address match."""
    result_url = candidate_url(item)
    expected_url = location.get("url")
    if expected_url and result_url and normalized_url(result_url) == normalized_url(expected_url):
        return True
    result_name = normalized_text(first_value(item, NAME_FIELDS))
    expected_names = {normalized_text(location["name"]), *(normalized_text(name) for name in location["match_names"])}
    address = normalized_text(first_value(item, ADDRESS_FIELDS))
    token_variants = location.get("address_match_variants") or [location["address_match_tokens"]]
    normalized_variants = [[normalized_text(token) for token in variant] for variant in token_variants]
    address_matches = any(all(token in address for token in variant) for variant in normalized_variants)
    expected_coordinates = location.get("expected_coordinates")
    result_coordinates = candidate_coordinates(item)
    coordinate_matches = False
    if expected_coordinates and result_coordinates:
        coordinate_matches = distance_meters(
            result_coordinates,
            (float(expected_coordinates["latitude"]), float(expected_coordinates["longitude"])),
        ) <= float(location.get("coordinate_tolerance_meters", 150))
    return result_name in expected_names and ((bool(address) and address_matches) or coordinate_matches)


def resolve_location(location: dict[str, Any], registry_entry: dict[str, Any], items: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
    registered_id = registry_entry.get("google_place_id")
    if registered_id:
        matches = [item for item in items if candidate_place_id(item) == registered_id]
        if len(matches) == 1:
            return matches[0], "matched_google_place_id"
        if len(matches) > 1:
            return None, "ambiguous_google_place_id"

    registered_url = registry_entry.get("canonical_url")
    if registered_url:
        matches = [item for item in items if normalized_url(candidate_url(item)) == normalized_url(registered_url)]
        if len(matches) == 1:
            return matches[0], "matched_canonical_url"
        if len(matches) > 1:
            return None, "ambiguous_canonical_url"

    matches = [item for item in items if bootstrap_matches(location, item)]
    if len(matches) == 1:
        return matches[0], "unverified_identifier_candidate"
    if len(matches) > 1:
        return None, "ambiguous_bootstrap_match"
    return None, "no_matching_result"


def busyness_status(item: dict[str, Any], resolution_status: str) -> str:
    """Classify absent live data without inventing an empty or zero occupancy reading."""
    if candidate_busyness(item) is not None:
        return resolution_status
    if candidate_is_closed(item):
        return f"{resolution_status}:location_closed"
    if has_historical_popular_times(item):
        return f"{resolution_status}:historical_popular_times_only"
    return f"{resolution_status}:live_data_not_published_by_google"


def build_rows(now: datetime, locations: list[dict[str, Any]], registry: dict[str, Any], items: list[dict[str, Any]]) -> tuple[list[dict[str, str]], dict[str, dict[str, str]]]:
    rows: list[dict[str, str]] = []
    discoveries: dict[str, dict[str, str]] = {}
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
    for location in locations:
        item, status = resolve_location(location, registry["locations"][location["id"]], items)
        place_id = candidate_place_id(item) if item else None
        source_url = candidate_url(item) if item else None
        busyness = candidate_busyness(item) if item else None
        if item and status == "unverified_identifier_candidate" and place_id:
            discoveries[location["id"]] = {"google_place_id": place_id, "canonical_url": source_url}
        if item:
            status = busyness_status(item, status)
        rows.append({
            "Timestamp_EST": timestamp,
            "Location_ID": location["id"],
            "Location_Name": location["name"],
            "Current_Busyness_Pct": "" if busyness is None else str(busyness),
            "Source_Status": status,
            "Google_Place_ID": place_id or "",
            "Source_URL": source_url or "",
        })
    return rows, discoveries


def status_rows(now: datetime, locations: list[dict[str, Any]], status: str) -> list[dict[str, str]]:
    """Preserve an observable collection attempt when no usable result is available."""
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
    return [{
        "Timestamp_EST": timestamp,
        "Location_ID": location["id"],
        "Location_Name": location["name"],
        "Current_Busyness_Pct": "",
        "Source_Status": status,
        "Google_Place_ID": "",
        "Source_URL": "",
    } for location in locations]


def error_rows(now: datetime, locations: list[dict[str, Any]], reason: str) -> list[dict[str, str]]:
    return status_rows(now, locations, f"actor_error:{reason}")


def weekly_csv_path(now: datetime) -> Path:
    year, week, _ = now.isocalendar()
    return DATA_DIR / f"busyness_{year}_W{week:02d}.csv"


def append_rows(path: Path, rows: list[dict[str, str]]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as destination:
        writer = csv.DictWriter(destination, fieldnames=CSV_FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerows(rows)


def diagnostic_projection(item: dict[str, Any]) -> dict[str, Any]:
    """Keep only public identity and occupancy-related data in a durable diagnostic record."""
    relevant_terms = ("popular", "busy", "live", "current", "open", "occup", "wait")
    values = {
        key: value
        for key, value in item.items()
        if any(term in normalized_text(key) for term in relevant_terms)
    }
    return {
        "fields": sorted(item),
        "place_id": candidate_place_id(item),
        "name": first_value(item, NAME_FIELDS),
        "address": first_value(item, ADDRESS_FIELDS),
        "coordinates": candidate_coordinates(item),
        "url": candidate_url(item),
        "live_busyness_pct": candidate_busyness(item),
        "is_closed": candidate_is_closed(item),
        "has_historical_popular_times": has_historical_popular_times(item),
        "occupancy_fields": values,
    }


def diagnostics_path(now: datetime) -> Path:
    year, week, _ = now.isocalendar()
    return DATA_DIR / f"busyness_diagnostics_{year}_W{week:02d}.jsonl"


def append_diagnostics(path: Path, now: datetime, run_ids: list[str], locations: list[dict[str, Any]], registry: dict[str, Any], items: list[dict[str, Any]]) -> None:
    """Persist small, sanitized records for parser and identity diagnosis across runs."""
    DATA_DIR.mkdir(exist_ok=True)
    resolved = []
    for location in locations:
        item, status = resolve_location(location, registry["locations"][location["id"]], items)
        resolved.append({
            "location_id": location["id"],
            "resolution_status": status,
            "matched_item": diagnostic_projection(item) if item else None,
        })
    record = {
        "timestamp_est": now.strftime("%Y-%m-%d %H:%M:%S"),
        "actor_id": ACTOR_ID,
        "actor_run_ids": run_ids,
        "locations": resolved,
        "unmatched_candidates": [diagnostic_projection(item) for item in items[:30]],
    }
    with path.open("a", encoding="utf-8") as destination:
        destination.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def log_actor_diagnostics(items: list[dict[str, Any]], run_ids: list[str]) -> None:
    """Log a deliberately small public metadata projection for matching diagnosis."""
    print(f"Apify run IDs: {', '.join(run_ids) if run_ids else 'unknown'}")
    print(f"Apify result count: {len(items)}")
    for index, item in enumerate(items[:20], start=1):
        summary = diagnostic_projection(item)
        print(f"Apify result {index}: {json.dumps(summary, ensure_ascii=False)}")
    if len(items) > 20:
        print(f"Apify result diagnostics truncated: {len(items) - 20} additional records.")


def persist_discoveries(registry: dict[str, Any], discoveries: dict[str, dict[str, str]]) -> bool:
    if not discoveries:
        return False
    for location_id, values in discoveries.items():
        registry["locations"][location_id] = values
    REGISTRY_PATH.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return True


def call_actor(locations: list[dict[str, Any]], registry: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    token = os.environ.get("APIFY_API_TOKEN")
    if not token:
        raise RuntimeError("APIFY_API_TOKEN is not defined")
    try:
        from apify_client import ApifyClient
    except ImportError as error:
        raise RuntimeError("apify-client is not installed; run pip install -r requirements.txt") from error
    try:
        client = ApifyClient(token)
        # Keep direct-ID collection independent from unresolved discovery URLs.
        # The actor supports both inputs, but separate calls remove ambiguity
        # about source-input precedence and bound BIDC experimentation.
        saved = [location for location in locations if registry["locations"][location["id"]].get("google_place_id")]
        unresolved = [location for location in locations if not registry["locations"][location["id"]].get("google_place_id")]
        items: list[dict[str, Any]] = []
        run_ids: list[str] = []
        for batch in (saved, unresolved):
            if not batch:
                continue
            run = client.actor(ACTOR_ID).call(run_input=build_actor_input(batch, registry))
            if run is None:
                raise ActorExecutionError("actor_returned_no_run")
            dataset_id = getattr(run, "default_dataset_id", None) or run.get("defaultDatasetId")
            if not dataset_id:
                raise ActorExecutionError("actor_returned_no_dataset")
            run_id = getattr(run, "id", None) or run.get("id")
            if run_id:
                run_ids.append(str(run_id))
            items.extend(client.dataset(dataset_id).list_items().items)
        return items, run_ids
    except ActorExecutionError:
        raise
    except Exception as error:
        detail = safe_actor_error(error)
        print(f"Apify collection failed for {ACTOR_ID}: {detail}", file=sys.stderr)
        raise ActorExecutionError(type(error).__name__) from error


def select_locations(locations: list[dict[str, Any]], location_id: str | None) -> list[dict[str, Any]]:
    if location_id is None:
        return locations
    selected = [location for location in locations if location["id"] == location_id]
    if not selected:
        raise ConfigurationError(f"Unknown location ID: {location_id}")
    return selected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--accept-discovered-identifiers", action="store_true", help="Commit strict bootstrap Google IDs after manual review")
    parser.add_argument("--location-id", help="Collect one configured location for a low-cost validation run")
    args = parser.parse_args(argv)
    try:
        config = load_json(CONFIG_PATH)
        registry = load_json(REGISTRY_PATH)
        validate_config(config)
        validate_registry(registry, config)
        locations = select_locations(config["locations"], args.location_id)
        now = datetime.now(TIMEZONE)
        if not is_active(now, config):
            print(f"Outside active tracking hours at {now.isoformat()}; exiting successfully.")
            return 0
        try:
            items, run_ids = call_actor(locations, registry)
        except ActorExecutionError as error:
            rows, discoveries = error_rows(now, locations, str(error)), {}
            print("Writing unavailable rows so this remote failure is visible in the historical dataset.", file=sys.stderr)
        else:
            log_actor_diagnostics(items, run_ids)
            append_diagnostics(diagnostics_path(now), now, run_ids, locations, registry, items)
            if not items:
                rows, discoveries = status_rows(now, locations, "actor_returned_zero_results"), {}
            else:
                rows, discoveries = build_rows(now, locations, registry, items)
        append_rows(weekly_csv_path(now), rows)
        if discoveries:
            print("Discovered unverified Google identifiers: " + ", ".join(sorted(discoveries)))
            if args.accept_discovered_identifiers:
                persist_discoveries(registry, discoveries)
                print("Approved discovered identifiers were saved to location_registry.json.")
            else:
                print("Review the Action log, then use workflow_dispatch with accept_identifiers=true to save them.")
        print(f"Wrote {len(rows)} rows to {weekly_csv_path(now).relative_to(ROOT)}")
        return 0
    except (ConfigurationError, RuntimeError, OSError) as error:
        print(f"Pipeline error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
