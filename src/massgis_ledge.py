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

These services are public: no token, no portal sign-in, no VPN. Nothing here has
to say so - `service_auth` reads it off the host, so no request to MassGIS
carries our token and no MassGIS-only run triggers a sign-in. That is deliberate,
so the ledge half of the analysis keeps working when the National Grid half needs
credentials.
"""
from __future__ import annotations

import os
from pathlib import Path
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

# The same publisher's data on ArcGIS Online, used when the MassGIS server itself
# cannot be reached. Corporate networks routinely allow services*.arcgis.com and
# block arcgisserver.digital.mass.gov, which shows up as a connection reset on
# the very first request.
#
# It is not the same data. This is the 1:250,000 mapping, roughly a tenth the
# detail of the 24k, and its nearest class is "Till or Bedrock" - till and rock
# in one polygon, where the 24k separates them. It over-reports ledge and it
# cannot resolve a single outcrop. Good enough to keep working and to scope a
# job; not good enough to price one. Every output says which source produced it.
AGOL_SURFICIAL_GEOLOGY_LAYER = os.environ.get(
    "ESTIMATE_GIS_AGOL_SURFGEO_LAYER",
    "https://services1.arcgis.com/hGdibHYSPO59RG1h/arcgis/rest/services"
    "/Surficial_Geology__1_250_000_/FeatureServer/0",
)
AGOL_CODE_FIELD = "CODE_DESC"
AGOL_LEDGE_CLASSES = ("Till or Bedrock",)

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
    out_fields: str | None = None,
) -> gpd.GeoDataFrame:
    bounds_in_massgis = None
    if bounds is not None:
        bounds_in_massgis = _bounds_to_massgis(bounds, bounds_crs)
    gdf = query_layer_to_geodataframe(
        layer_url=layer_url,
        where=where,
        out_fields=out_fields or f"{code_field},LABEL,NOTES",
        token=None,
        objectid_batch_size=batch_size,
        workers=workers,
        bounds=bounds_in_massgis,
        bounds_sr=MASSGIS_CRS,
        out_sr=MASSGIS_CRS,
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


# A query envelope is padded before it goes to the service. A run scoped to one
# street can produce an extent a few metres wide, or a zero-width one if every
# feature is on a single alignment, and an envelope like that returns no ledge
# even where there plainly is some.
ENVELOPE_PAD_METERS = 250.0

# Massachusetts in EPSG:26986, roughly. Used only to tell a caller that their
# extent landed nowhere near the data, which is what a CRS mix-up looks like.
MASSACHUSETTS_EXTENT = (30000.0, 770000.0, 340000.0, 965000.0)


def _bounds_to_massgis(
    bounds: tuple[float, float, float, float], bounds_crs: Any
) -> tuple[float, float, float, float]:
    """Put a caller's extent into the MassGIS coordinate system before querying."""
    if bounds_crs is None:
        raise ValueError(
            "A bounding box needs a CRS. Pass bounds_crs, or give the mainlines "
            "layer a CRS before deriving bounds from it. Without one there is no "
            "way to know what the numbers mean, and querying them raw would "
            "silently return no ledge."
        )
    values = [float(value) for value in bounds]
    if any(value != value for value in values):  # NaN, from an empty layer
        raise ValueError(
            "The extent contains NaN, which happens when the source layer has no "
            "features or no geometry. There is nothing to look up ledge for."
        )
    # Reproject the box itself, not its two opposite corners: between two
    # projections a rectangle does not stay a rectangle, and the corners alone
    # can sit inside the true reprojected extent.
    envelope = gpd.GeoSeries([shapely_box(*values)], crs=bounds_crs).to_crs(MASSGIS_CRS)
    minx, miny, maxx, maxy = (float(value) for value in envelope.total_bounds)
    padded = (
        minx - ENVELOPE_PAD_METERS,
        miny - ENVELOPE_PAD_METERS,
        maxx + ENVELOPE_PAD_METERS,
        maxy + ENVELOPE_PAD_METERS,
    )
    _warn_if_outside_massachusetts(padded, bounds, bounds_crs)
    return padded


def _warn_if_outside_massachusetts(
    padded: tuple[float, float, float, float],
    original: tuple[float, float, float, float],
    bounds_crs: Any,
) -> None:
    """Say so when the extent cannot overlap the data, rather than returning nothing.

    An extent that misses Massachusetts entirely is almost always a CRS that was
    guessed rather than read - feet read as metres, or a projected extent handed
    over as degrees. The symptom is an empty ledge layer and a report full of
    zeros, which looks like the ledge lookup is broken.
    """
    state_minx, state_miny, state_maxx, state_maxy = MASSACHUSETTS_EXTENT
    minx, miny, maxx, maxy = padded
    if maxx < state_minx or minx > state_maxx or maxy < state_miny or miny > state_maxy:
        progress(
            "WARNING: the requested extent does not overlap Massachusetts, so no "
            "ledge can be found."
        )
        progress(f"  extent given:      {tuple(round(v, 1) for v in original)} in {bounds_crs}")
        progress(f"  reprojected to 26986: {tuple(round(v, 1) for v in padded)}")
        progress(f"  Massachusetts is:  {MASSACHUSETTS_EXTENT} in EPSG:26986")
        progress("  Check the CRS of the layer the extent came from.")


def fetch_ledge_polygons(
    profile: str = DEFAULT_LEDGE_PROFILE,
    bounds: tuple[float, float, float, float] | None = None,
    bounds_crs: Any = None,
    *,
    workers: int = 8,
    batch_size: int = 500,
    cache_dir: str | Path | None = None,
    refresh_cache: bool = False,
) -> gpd.GeoDataFrame:
    """Return the ledge polygons for a profile, in EPSG:26986.

    `bounds` is (minx, miny, maxx, maxy) in `bounds_crs`, normally the extent of
    the mainlines being analysed. Statewide there are ~65,000 outcrop polygons
    and ~15,000 shallow-bedrock polygons, so passing the extent is the
    difference between a long download and a short one.

    With `cache_dir`, the result is written to a GeoPackage keyed by profile and
    extent and reused on the next run. Ledge does not move, and re-downloading it
    on every attempt turns a five-second change into a two-minute one.
    """
    definition = profile_definition(profile)

    cache_path = (
        _cache_path(cache_dir, profile, bounds, bounds_crs) if cache_dir else None
    )
    if cache_path is not None and cache_path.exists() and not refresh_cache:
        cached = gpd.read_file(cache_path)
        progress(f"Ledge polygons from cache: {len(cached):,} ({cache_path})")
        return cached.set_crs(MASSGIS_CRS, allow_override=True)

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
        progress(
            "No MassGIS ledge polygons matched this extent. That is a real answer "
            "in parts of the state with no mapped bedrock, but check the extent "
            "above against the coordinates the mainlines came back in before "
            "believing it."
        )
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
    if cache_path is not None and not combined.empty:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        combined.to_file(cache_path, driver="GPKG")
        progress(f"Ledge cached for the next run: {cache_path}")
    return combined


def fetch_agol_ledge_polygons(
    bounds: tuple[float, float, float, float] | None = None,
    bounds_crs: Any = None,
    *,
    workers: int = 6,
    batch_size: int = 500,
) -> gpd.GeoDataFrame:
    """Ledge from the ArcGIS Online copy of MassGIS surficial geology.

    The fallback for a network that blocks arcgisserver.digital.mass.gov. Coarser
    than the 24k mapping - see AGOL_SURFICIAL_GEOLOGY_LAYER for what that costs.
    """
    progress(f"Fetching ledge from ArcGIS Online: {AGOL_SURFICIAL_GEOLOGY_LAYER}")
    gdf = _fetch(
        layer_url=AGOL_SURFICIAL_GEOLOGY_LAYER,
        where=_in_clause(AGOL_CODE_FIELD, AGOL_LEDGE_CLASSES),
        code_field=AGOL_CODE_FIELD,
        labels={value: value for value in AGOL_LEDGE_CLASSES},
        source="agol_surficial_geology_250k",
        bounds=bounds,
        bounds_crs=bounds_crs,
        workers=workers,
        batch_size=batch_size,
        out_fields=AGOL_CODE_FIELD,
    )
    if gdf.empty:
        return gdf
    gdf = _repair(gdf)
    progress(f"Ledge polygons from ArcGIS Online: {len(gdf):,}")
    return gdf


def _repair(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Drop empty geometry and mend self-intersections, which break every overlay."""
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty]
    invalid = ~gdf.geometry.is_valid
    if bool(invalid.any()):
        progress(f"Repairing {int(invalid.sum()):,} invalid ledge polygons.")
        gdf = gdf.copy()
        gdf.loc[invalid, "geometry"] = gdf.loc[invalid, "geometry"].buffer(0)
    return gdf[~gdf.geometry.is_empty].reset_index(drop=True)


def _cache_path(
    cache_dir: str | Path,
    profile: str,
    bounds: tuple[float, float, float, float] | None,
    bounds_crs: Any,
) -> Path:
    """A cache file per profile and extent.

    The extent is rounded to a kilometre so that two runs over the same area
    share a cache even when their mainline selections differ by a few feet.
    """
    if bounds is None:
        key = "statewide"
    else:
        in_massgis = _bounds_to_massgis(bounds, bounds_crs)
        key = "_".join(str(int(round(value / 1000.0))) for value in in_massgis)
    return Path(cache_dir) / f"ledge_{profile}_{key}.gpkg"
