# Purdue Campus Busyness Tracker

An automated collector for live Google Maps busyness readings at selected Purdue campus locations. It runs every two hours in GitHub Actions, but Python only calls Apify from 07:00 through 21:00 in `America/Indiana/Indianapolis`.

## Setup

1. Create an Apify token and add it as the repository Actions secret `APIFY_API_TOKEN`.
2. The collector uses `seemuapps/google-maps-popular-times-scraper`, whose API accepts `placeUrls` and returns `currentBusynessPct`, `placeId`, `googlePlaceId`, `placeName`, `placeAddress`, and `placeUrl`. Its contract is centralized in `scrape_dining.py`.
3. Trigger **Collect Purdue campus busyness** manually. Inspect its log for each `unverified_identifier_candidate` and verify it is the intended location.
4. Trigger it again with `accept_identifiers` selected. This saves the discovered Google Place IDs in `location_registry.json`. Future matching uses those IDs before any URL fallback.

## Identity model

- `config.json` `id`: permanent internal ID for one tracked facility.
- `location_registry.json` `google_place_id`: Google identifier confirmed after review.
- URL: request/bootstrap fallback only. It is not trusted as the primary identity after registration.

Do not accept identifiers until the Action log has been reviewed. A missing live reading is normal when Google does not provide enough current traffic data; it is saved with an empty percentage and an explanatory `Source_Status`.

## Local checks

```bash
python -m unittest discover -s tests -v
```

The tests use mocked scraper records and do not need an Apify token.
