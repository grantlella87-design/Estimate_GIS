"""MassGIS ledge (bedrock) polygons, fetched straight from the public services.

"Ledge" is a construction word, not a MassGIS one, so it has to be built from
what MassGIS actually publishes. The closest statewide mapping is the Surficial
Geology (1:24,000) dataset, which carries two things a pipe crew would call
ledge:

* map-unit polygons coded ``bk`` - "Bedrock outcrops", rock at the surface;
* overlay polygons coded ``sb`` - "Areas of abundant outcrop or shallow
  bedrock", where rock is close enough to the surface to be hit in a trench.

Those two make up the default profile. Thin till and talus are rock-adjacent
but not the same claim, so they live in a wider profile a caller has to ask
for. Nothing here decides which profile is right for a given estimate - the
profile is recorded in the output so a number can always be traced back to the
definition that produced it.

These services are public: no token, no portal sign-in, no VPN. That is
deliberate, so the ledge half of the analysis keeps working when the National
Grid half needs credentials.
"""
from __future__ import annotations

import os
from typing import Any

import geopandas as gpd
import pandas as pd
from shapely.geometry import box as shapely_box

from arcgis_rest_geopandas import progress, query_layer_to_geodataframe

MASSGIS_REST_ROOT = os.environ.get(
    "ESTIMATE_GIS_MASSGIS_ROOT",
    "https://arcgisserver.digital.mass.gov/arcgisserver/rest/services",
).rstrip("/")

SURFICIAL_GEOLOGY_SERVICE = f"{MASSGIS_REST_ROOT}/AGOL/SurfGeo24k/MapServer"
OVERLAY_LAYER_URL = f"{SURFICIAL_GEOLOGY_SERVICE}/0"
MAP_UNIT_LAYER_URL = f"{SURFICIAL_GEOLOGY_SERVICE}/1"

# Massachusetts State Plane Mainland, metres. The source data is stored in it,
# so requesting it back costs no reprojection and keeps lengths measurable.
MASSGIS_CRS = 26986

MAP_UNIT_LABELS = {
    "af": "Artificial fill",
    "cb": "Cranberry bog deposits",
    "bd": "Beach and dune deposits",
    "sw": "Swamp deposits",
    "sm": "Salt-marsh and estuarine deposits",
    "al": "Floodplain alluvium",
    "ff": "Valley-floor fluvial deposits",
    "alf": "Alluvial-fan deposits",
    "d": "Inland-dune deposits",
    "rs": "Marine regressive deposits",
    "st": "Stream-terrace deposits",
    "ta": "Talus deposits",
    "sd-c": "Glacial stratified deposits, coarse",
    "sd-f": "Glacial stratified deposits, fine",
    "sd-fm": "Glacial stratified deposits, glaciomarine fine",
    "sid": "Stagnant-ice deposits",
    "em": "End moraine deposits",
    "tm": "Thrust-moraine deposits",
    "tt": "Thick till",
    "tvt": "Thick valley till and fine deposits",
    "cph": "Glacially-modified coastal plain hill deposits",
    "t": "Thin till",
    "bk": "Bedrock outcrops",
}

OVERLAY_TYPE_LABELS = {
    "sb": "Areas of abundant outcrop or shallow bedrock",
}

LEDGE_PROFILES: dict[str, dict[str, tuple[str, ...]]] = {
    # Only rock mapped at the surface. The tightest, least arguable definition.
    "outcrop": {"map_units": ("bk",), "overlay_types": ()},
    # Surface rock plus the shallow-bedrock overlay. The default: it matches
    # what a crew hits with a trench box, not just what shows on the surface.
    "standard": {"map_units": ("bk",), "overlay_types": ("sb",)},
    # Adds thin till and talus, which are usually rock within a few feet.
    # The widest of the three, and the one most likely to over-report.
    "broad": {"map_units": ("bk", "t", "ta"), "overlay_types": ("sb",)},
}
DEFAULT_LEDGE_PROFILE = "standard"

LEDGE_COLUMNS = ("ledge_code", "ledge_class", "ledge_source")


def profile_definition(profile: str) -> dict[str, tuple[str, ...]]:
    try:
        return LEDGE_PROFILES[profile]
    except KeyError:
        raise ValueError(
            f"Unknown ledge profile {profile!r}. Choose one of "
            f"{sorted(LEDGE_PROFILES)}."
        ) from None


def describe_profile(profile: str) -> str:
    """One line naming every class the profile counts as ledge, for the report."""
    definition = profile_definition(profile)
    parts = [
        f"{code} ({MAP_UNIT_LABELS.get(code, code)})"
        for code in definition["map_units"]
    ]
    parts += [
        f"{code} ({OVERLAY_TYPE_LABELS.get(code, code)})"
        for code in definition["overlay_types"]
    ]
    return ", ".join(parts)


def _in_clause(field: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join("'" + value.replace("'", "''") + "'" for value in values)
    return f"{field} IN ({quoted})"


def _fetch(
    layer_url: str,
    where: str,
    code_field: str,
    labels: dict[str, str],
    source: str,
    bounds: tuple[float, float, float, float] | None,
    bounds_crs: Any,
    workers: int,
    batch_size: int,
) -> gpd.GeoDataFrame:
    bounds_in_massgis = None
    if bounds is not None:
        bounds_in_massgis = _bounds_to_massgis(bounds, bounds_crs)
    gdf = query_layer_to_geodataframe(
        layer_url=layer_url,
        where=where,
        out_fields=f"{code_field},LABEL,NOTES",
        token=None,
        objectid_batch_size=batch_size,
        workers=workers,
        bounds=bounds_in_massgis,
        bounds_sr=MASSGIS_CRS,
        out_sr=MASSGIS_CRS,
        allow_anonymous=True,
    )
    if gdf.empty:
        return gpd.GeoDataFrame(
            {name: pd.Series(dtype="object") for name in LEDGE_COLUMNS},
            geometry=gpd.GeoSeries([], crs=MASSGIS_CRS),
            crs=MASSGIS_CRS,
        )
    codes = gdf[code_field].astype("string").str.strip().str.lower()
    gdf["ledge_code"] = codes
    gdf["ledge_class"] = codes.map(labels).fillna(codes)
    gdf["ledge_source"] = source
    keep = [*LEDGE_COLUMNS, "geometry"]
    return gdf[keep].set_geometry("geometry")


def _bounds_to_massgis(
    bounds: tuple[float, float, float, float], bounds_crs: Any
) -> tuple[float, float, float, float]:
    """Put a caller's extent into the MassGIS coordinate system before querying."""
    if bounds_crs is None:
        raise ValueError(
            "A bounding box needs a CRS. Pass bounds_crs, or give the mainlines "
            "layer a CRS before deriving bounds from it."
        )
    # Reproject the box itself, not its two opposite corners: between two
    # projections a rectangle does not stay a rectangle, and the corners alone
    # can sit inside the true reprojected extent.
    envelope = gpd.GeoSeries([shapely_box(*bounds)], crs=bounds_crs).to_crs(MASSGIS_CRS)
    return tuple(float(value) for value in envelope.total_bounds)


def fetch_ledge_polygons(
    profile: str = DEFAULT_LEDGE_PROFILE,
    bounds: tuple[float, float, float, float] | None = None,
    bounds_crs: Any = None,
    *,
    workers: int = 8,
    batch_size: int = 500,
) -> gpd.GeoDataFrame:
    """Return the ledge polygons for a profile, in EPSG:26986.

    `bounds` is (minx, miny, maxx, maxy) in `bounds_crs`, normally the extent of
    the mainlines being analysed. Statewide there are ~65,000 outcrop polygons
    and ~15,000 shallow-bedrock polygons, so passing the extent is the
    difference between a long download and a short one.
    """
    definition = profile_definition(profile)
    frames: list[gpd.GeoDataFrame] = []

    if definition["map_units"]:
        progress(
            f"Fetching MassGIS surficial geology map units: {', '.join(definition['map_units'])}"
        )
        frames.append(
            _fetch(
                layer_url=MAP_UNIT_LAYER_URL,
                where=_in_clause("MAPUNIT", definition["map_units"]),
                code_field="MAPUNIT",
                labels=MAP_UNIT_LABELS,
                source="surficial_geology_map_unit",
                bounds=bounds,
                bounds_crs=bounds_crs,
                workers=workers,
                batch_size=batch_size,
            )
        )

    if definition["overlay_types"]:
        progress(
            f"Fetching MassGIS surficial geology overlay: {', '.join(definition['overlay_types'])}"
        )
        frames.append(
            _fetch(
                layer_url=OVERLAY_LAYER_URL,
                where=_in_clause("TYPE", definition["overlay_types"]),
                code_field="TYPE",
                labels=OVERLAY_TYPE_LABELS,
                source="surficial_geology_overlay",
                bounds=bounds,
                bounds_crs=bounds_crs,
                workers=workers,
                batch_size=batch_size,
            )
        )

    frames = [frame for frame in frames if not frame.empty]
    if not frames:
        progress("No MassGIS ledge polygons matched this extent.")
        return gpd.GeoDataFrame(
            {name: pd.Series(dtype="object") for name in LEDGE_COLUMNS},
            geometry=gpd.GeoSeries([], crs=MASSGIS_CRS),
            crs=MASSGIS_CRS,
        )
    combined = gpd.GeoDataFrame(
        pd.concat(frames, ignore_index=True), geometry="geometry", crs=MASSGIS_CRS
    )
    combined = combined[combined.geometry.notna() & ~combined.geometry.is_empty]
    # Source polygons occasionally self-intersect, which makes every later
    # overlay raise. A zero-width buffer repairs them without moving edges.
    invalid = ~combined.geometry.is_valid
    if bool(invalid.any()):
        progress(f"Repairing {int(invalid.sum()):,} invalid ledge polygons.")
        combined.loc[invalid, "geometry"] = combined.loc[invalid, "geometry"].buffer(0)
    combined = combined[~combined.geometry.is_empty].reset_index(drop=True)
    progress(f"Ledge polygons: {len(combined):,} ({describe_profile(profile)})")
    return combined
