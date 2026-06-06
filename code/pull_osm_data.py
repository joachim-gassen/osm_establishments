from __future__ import annotations

import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from shapely.geometry import Point
import geopandas as gpd


OVERPASS_URL = "https://overpass-api.de/api/interpreter"
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
PLACE_TAGS = [
    "shop", "amenity", "office", "craft", "tourism", "leisure", "healthcare"
]
# Example: Berlin-Mitte bounding box
# Format: south, west, north, east
BBOX = (52.49, 13.35, 52.54, 13.43)

OUTPUT_DIR = Path("data/pulled")
CACHE_DIR = Path("data/cache/osm")
OUTPUT_GEOJSON = OUTPUT_DIR / "business_places.geojson"
REPULL_DATA = False
TEARDOWN_CACHE_FOLDER = False
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def build_overpass_query(
    tag: str,
    bbox: tuple[float, float, float, float],
) -> str:
    """
    Build an Overpass QL query for one business-like tag.

    We request nodes, ways, and relations with the requested tag.
    `out center tags` gives us tags plus a representative center point 
    for ways/relations.
    """
    south, west, north, east = bbox
    bbox_text = f"{south},{west},{north},{east}"

    return f"""
    [out:json][timeout:120];
    (
      node["{tag}"]({bbox_text});
      way["{tag}"]({bbox_text});
      relation["{tag}"]({bbox_text});
    );
    out center tags;
    """


def query_overpass(
    tag: str,
    query: str,
    pause: float = 2.0,
    max_retries: int = 5,
    initial_backoff: float = 1.0,
) -> dict[str, Any]:
    """
    Send a query to Overpass.

    The pause is included to be polite towards the API.
    """
    time.sleep(pause)

    for attempt in range(max_retries + 1):
        attempt_number = attempt + 1
        total_attempts = max_retries + 1

        try:
            logger.info(
                "Calling Overpass API for tag=%s attempt=%s/%s",
                tag,
                attempt_number,
                total_attempts,
            )
            response = requests.post(
                OVERPASS_URL,
                data={"data": query},
                headers={"User-Agent": "osm-establishments/0.1"},
                timeout=180,
            )
            logger.info(
                "Overpass API returned status=%s for tag=%s attempt=%s/%s",
                response.status_code,
                tag,
                attempt_number,
                total_attempts,
            )

            if response.status_code not in RETRYABLE_STATUS_CODES:
                response.raise_for_status()
                return response.json()

            response.raise_for_status()
        except requests.exceptions.RequestException as error:
            status_code = None
            if error.response is not None:
                status_code = error.response.status_code

            if status_code is not None and status_code not in RETRYABLE_STATUS_CODES:
                raise

            if attempt == max_retries:
                logger.exception(
                    "Overpass API failed for tag=%s after %s attempts",
                    tag,
                    total_attempts,
                )
                raise

            backoff = initial_backoff * (2 ** attempt)
            logger.warning(
                "Overpass API failed for tag=%s attempt=%s/%s; retrying in %.0f seconds",
                tag,
                attempt_number,
                total_attempts,
                backoff,
            )
            time.sleep(backoff)

    raise RuntimeError("Overpass retry loop exited unexpectedly.")


def load_or_query_raw_response(tag: str, query: str) -> dict[str, Any]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f"raw_{tag}_response.json"

    if cache_path.exists():
        logger.info("Using cached response for tag=%s from %s", tag, cache_path)
        return json.loads(cache_path.read_text(encoding="utf-8"))

    raw = query_overpass(tag, query)
    cache_path.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return raw


def tear_down_cache_dir() -> None:
    if CACHE_DIR.exists():
        logger.info("Removing OSM pull cache directory %s", CACHE_DIR)
        shutil.rmtree(CACHE_DIR)

    cache_parent = CACHE_DIR.parent
    if cache_parent.exists() and not any(cache_parent.iterdir()):
        cache_parent.rmdir()


def write_pretty_geojson(gdf: gpd.GeoDataFrame, path: Path) -> None:
    geojson = json.loads(gdf.to_json())
    path.write_text(
        json.dumps(geojson, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def element_to_record(element: dict[str, Any]) -> dict[str, Any] | None:
    """
    Convert an OSM element into one flat record.

    Nodes have lat/lon directly.
    Ways and relations often have a computed `center`.
    """
    tags = element.get("tags", {})

    if "lat" in element and "lon" in element:
        lat = element["lat"]
        lon = element["lon"]
    elif "center" in element:
        lat = element["center"]["lat"]
        lon = element["center"]["lon"]
    else:
        return None

    record = {
        "osm_type": element.get("type"),
        "osm_id": element.get("id"),
        "lat": lat,
        "lon": lon,
        "name": tags.get("name"),
        "brand": tags.get("brand"),
        "operator": tags.get("operator"),
        "street": tags.get("addr:street"),
        "housenumber": tags.get("addr:housenumber"),
        "postcode": tags.get("addr:postcode"),
        "city": tags.get("addr:city"),
        "phone": tags.get("phone") or tags.get("contact:phone"),
        "website": tags.get("website") or tags.get("contact:website"),
        "opening_hours": tags.get("opening_hours"),
        "shop": tags.get("shop"),
        "amenity": tags.get("amenity"),
        "office": tags.get("office"),
        "craft": tags.get("craft"),
        "tourism": tags.get("tourism"),
        "leisure": tags.get("leisure"),
        "healthcare": tags.get("healthcare"),
        "all_tags": json.dumps(tags, ensure_ascii=False),
    }

    return record


def classify_business_type(row: pd.Series) -> str | None:
    for key in PLACE_TAGS:
        if pd.notna(row.get(key)):
            return f"{key}:{row[key]}"
    return None


def records_from_elements(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records = []
    for element in elements:
        record = element_to_record(element)
        if record is not None:
            records.append(record)
    return records


def build_geodataframe(records: list[dict[str, Any]]) -> gpd.GeoDataFrame:
    df = pd.DataFrame(records)

    if df.empty:
        raise ValueError("No records returned. Try a larger bounding box or different tags.")

    df["business_type"] = df.apply(classify_business_type, axis=1)

    # Remove exact duplicate OSM objects, if any.
    df = df.drop_duplicates(subset=["osm_type", "osm_id"])

    geometry = [Point(xy) for xy in zip(df["lon"], df["lat"])]
    return gpd.GeoDataFrame(df, geometry=geometry, crs="EPSG:4326")


def run_pipeline() -> gpd.GeoDataFrame:
    if OUTPUT_GEOJSON.exists() and not REPULL_DATA:
        logger.info("Using existing OSM pull output %s", OUTPUT_GEOJSON)
        if TEARDOWN_CACHE_FOLDER:
            tear_down_cache_dir()
        return gpd.read_file(OUTPUT_GEOJSON)

    all_records = []

    for tag in PLACE_TAGS:
        logger.info("Querying OSM elements tagged with %s", tag)

        query = build_overpass_query(tag, BBOX)
        raw = load_or_query_raw_response(tag, query)

        records = records_from_elements(raw.get("elements", []))
        all_records.extend(records)

        logger.info("Found %s places for tag=%s", f"{len(records):,}", tag)

    gdf = build_geodataframe(all_records)
    write_pretty_geojson(gdf, OUTPUT_GEOJSON)
    if TEARDOWN_CACHE_FOLDER:
        tear_down_cache_dir()

    return gdf


if __name__ == "__main__":
    gdf = run_pipeline()
    logger.info("OSM establishments available: %s", f"{len(gdf):,}")
