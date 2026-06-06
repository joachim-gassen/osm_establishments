from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", "data/cache/matplotlib")
os.environ.setdefault("XDG_CACHE_HOME", "data/cache")
Path(os.environ["MPLCONFIGDIR"]).mkdir(parents=True, exist_ok=True)

import matplotlib.pyplot as plt
import pandas as pd
import geopandas as gpd


INPUT_GEOJSON = Path("data/pulled/business_places.geojson")
OUTPUT_PNG = Path("output/berlin_establishments_url_map.png")
DEFAULT_CLIP_QUANTILE = 0.005
DEFAULT_HIGHLIGHT_OSM_ID = "326807348"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def has_url(value: object) -> bool:
    return isinstance(value, str) and value.strip() != ""


def load_points(input_geojson: Path) -> pd.DataFrame:
    df = pd.DataFrame(gpd.read_file(input_geojson).drop(columns="geometry"))
    df["lat"] = pd.to_numeric(df["lat"], errors="coerce")
    df["lon"] = pd.to_numeric(df["lon"], errors="coerce")
    df = df.dropna(subset=["lat", "lon"]).copy()
    df["has_url"] = df["website"].apply(has_url)
    return df


def clip_to_display_extent(df: pd.DataFrame, clip_quantile: float) -> pd.DataFrame:
    if clip_quantile <= 0:
        return df

    lower_lon = df["lon"].quantile(clip_quantile)
    upper_lon = df["lon"].quantile(1 - clip_quantile)
    lower_lat = df["lat"].quantile(clip_quantile)
    upper_lat = df["lat"].quantile(1 - clip_quantile)

    return df[
        df["lon"].between(lower_lon, upper_lon)
        & df["lat"].between(lower_lat, upper_lat)
    ].copy()


def render_map(
    df: pd.DataFrame,
    output_png: Path,
    clip_quantile: float,
    highlight_osm_id: str | None,
) -> None:
    display_df = clip_to_display_extent(df, clip_quantile)

    if display_df.empty:
        raise ValueError("No points left after clipping. Lower --clip-quantile.")

    hidden_count = len(df) - len(display_df)
    with_url = display_df[display_df["has_url"]]
    without_url = display_df[~display_df["has_url"]]
    if highlight_osm_id is None:
        highlighted = display_df.iloc[0:0]
    else:
        highlighted = display_df[
            display_df["osm_id"].astype(str) == str(highlight_osm_id)
        ]
        if highlighted.empty:
            logger.warning("Highlight OSM ID %s is not visible in the map data", highlight_osm_id)
        else:
            logger.info("Highlighting OSM ID %s", highlight_osm_id)
    subtitle = f"{len(display_df):,} establishments shown from the OSM pull step"
    if hidden_count:
        subtitle = f"{subtitle}; {hidden_count:,} edge points hidden by display clipping"

    fig, ax = plt.subplots(figsize=(10, 10), dpi=220)
    fig.patch.set_facecolor("#f7f4ee")
    ax.set_facecolor("#f7f4ee")

    ax.scatter(
        without_url["lon"],
        without_url["lat"],
        s=5,
        c="#9aa1a6",
        alpha=0.28,
        linewidths=0,
        label=f"No website ({len(without_url):,})",
        rasterized=True,
    )
    ax.scatter(
        with_url["lon"],
        with_url["lat"],
        s=8,
        c="#007f7a",
        alpha=0.72,
        linewidths=0,
        label=f"Website URL ({len(with_url):,})",
        rasterized=True,
    )
    if not highlighted.empty:
        ax.scatter(
            highlighted["lon"],
            highlighted["lat"],
            s=86,
            facecolors="#e53935",
            edgecolors="#ffffff",
            linewidths=1.4,
            alpha=0.98,
            zorder=8,
        )
        ax.scatter(
            highlighted["lon"],
            highlighted["lat"],
            s=180,
            facecolors="none",
            edgecolors="#e53935",
            linewidths=1.6,
            alpha=0.85,
            zorder=7,
        )

    lon_padding = (display_df["lon"].max() - display_df["lon"].min()) * 0.025
    lat_padding = (display_df["lat"].max() - display_df["lat"].min()) * 0.025
    xlim = (
        display_df["lon"].min() - lon_padding,
        display_df["lon"].max() + lon_padding,
    )
    ylim = (
        display_df["lat"].min() - lat_padding,
        display_df["lat"].max() + lat_padding,
    )

    ax.set_title(
        "Berlin OSM Establishments by Website Availability",
        loc="left",
        fontsize=17,
        fontweight="bold",
        pad=28,
    )
    ax.text(
        0,
        1.015,
        subtitle,
        transform=ax.transAxes,
        fontsize=10,
        color="#4c5155",
    )

    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.grid(color="#d7d0c6", linewidth=0.6, alpha=0.75)
    legend = ax.legend(
        loc="lower right",
        frameon=True,
        facecolor="#fffdfa",
        edgecolor="#cbc3b7",
        framealpha=0.94,
        markerscale=2.4,
    )
    legend.set_zorder(10)

    for spine in ax.spines.values():
        spine.set_color("#b8afa3")

    ax.set_xlim(xlim)
    ax.set_ylim(ylim)
    ax.set_aspect("auto")
    fig.tight_layout()

    output_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_png, bbox_inches="tight")
    plt.close(fig)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render a PNG map of OSM establishments colored by website availability."
    )
    parser.add_argument("--input", type=Path, default=INPUT_GEOJSON)
    parser.add_argument("--output", type=Path, default=OUTPUT_PNG)
    parser.add_argument(
        "--clip-quantile",
        type=float,
        default=DEFAULT_CLIP_QUANTILE,
        help=(
            "Clip this fraction of extreme coordinates on each side for display. "
            "Use 0 to show the full extent."
        ),
    )
    parser.add_argument(
        "--highlight-osm-id",
        default=DEFAULT_HIGHLIGHT_OSM_ID,
        help="OSM ID to highlight in red. Use an empty value to disable.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    points = load_points(args.input)
    highlight_osm_id = args.highlight_osm_id.strip() or None
    render_map(points, args.output, args.clip_quantile, highlight_osm_id)
    logger.info("Wrote %s", args.output)
