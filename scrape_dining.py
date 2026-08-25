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
BUSYNESS_FIELDS = ("currentBusynessPct", "popularTimesCurrent", "peopleWaiting", "popularTimesLivePercent")


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


def candidate_busyness(item: dict[str, Any]) -> int | None:
    for field in BUSYNESS_FIELDS:
        value = item.get(field)
        if value is None or value == "":
            continue
        try:
            result = int(value)
        except (TypeError, ValueError):
            continue
        if 0 <= result <= 100:
            return result
    return None


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
    return result_name in expected_names and bool(address) and address_matches


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
        if item and busyness is None:
            status = f"{status}:live_busyness_unavailable"
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


def log_actor_diagnostics(items: list[dict[str, Any]]) -> None:
    """Log a deliberately small public metadata projection for matching diagnosis."""
    print(f"Apify result count: {len(items)}")
    for index, item in enumerate(items[:20], start=1):
        summary = {
            "fields": sorted(item)[:50],
            "place_id": candidate_place_id(item),
            "name": first_value(item, NAME_FIELDS),
            "address": first_value(item, ADDRESS_FIELDS),
            "url": candidate_url(item),
            "busyness_pct": candidate_busyness(item),
        }
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


def call_actor(locations: list[dict[str, Any]], registry: dict[str, Any]) -> list[dict[str, Any]]:
    token = os.environ.get("APIFY_API_TOKEN")
    if not token:
        raise RuntimeError("APIFY_API_TOKEN is not defined")
    try:
        from apify_client import ApifyClient
    except ImportError as error:
        raise RuntimeError("apify-client is not installed; run pip install -r requirements.txt") from error
    run_input = build_actor_input(locations, registry)
    try:
        client = ApifyClient(token)
        run = client.actor(ACTOR_ID).call(run_input=run_input)
        if run is None:
            raise ActorExecutionError("actor_returned_no_run")
        dataset_id = getattr(run, "default_dataset_id", None) or run.get("defaultDatasetId")
        if not dataset_id:
            raise ActorExecutionError("actor_returned_no_dataset")
        return client.dataset(dataset_id).list_items().items
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
            items = call_actor(locations, registry)
        except ActorExecutionError as error:
            rows, discoveries = error_rows(now, locations, str(error)), {}
            print("Writing unavailable rows so this remote failure is visible in the historical dataset.", file=sys.stderr)
        else:
            log_actor_diagnostics(items)
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
