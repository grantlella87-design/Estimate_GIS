"""Read a vector layer from wherever it lives: an ArcGIS service or a local file.

One function so the analysis scripts never branch on "is this a URL". It also
means the same command works against the live service on a National Grid
workstation and against an exported GeoPackage on a machine with no VPN, which
is the difference between a script two people can run and a script one person
can run.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

import geopandas as gpd
import service_auth
from shapely.geometry import box as shapely_box

from arcgis_rest_geopandas import (
    DEFAULT_DOWNLOAD_WORKERS,
    DEFAULT_OBJECTID_BATCH_SIZE,
    make_session,
    progress,
    query_count,
    query_layer_to_geodataframe,
)


def is_service(source: str) -> bool:
    return str(source).lower().startswith(("http://", "https://"))


def count_features(
    source: str, where: str = "1=1", token: str | None = None, *, sign_in: bool = True
) -> int | None:
    """Feature count without downloading anything. Only services can answer cheaply."""
    if not is_service(source):
        return None
    service_auth.report_once(str(source))
    resolved = service_auth.token_for(str(source), token, allow_sign_in=sign_in)
    return query_count(make_session(resolved), str(source), where)


def read_source(
    source: str | Path,
    *,
    layer: str | None = None,
    where: str = "1=1",
    out_fields: str = "*",
    token: str | None = None,
    bounds: Sequence[float] | None = None,
    bounds_crs: Any = None,
    out_sr: int | None = None,
    workers: int = DEFAULT_DOWNLOAD_WORKERS,
    batch_size: int = DEFAULT_OBJECTID_BATCH_SIZE,
    sign_in: bool = True,
    decode_domains: bool = True,
) -> gpd.GeoDataFrame:
    """Return a GeoDataFrame from an ArcGIS layer URL or any file GeoPandas reads.

    Authentication is not the caller's problem: our own servers get a token,
    signing in if `token` was not supplied, and public ones get none. `sign_in`
    turns the sign-in off for an unattended run, leaving `token` as the only way
    to authenticate.
    """
    source = str(source)
    if is_service(source):
        return query_layer_to_geodataframe(
            layer_url=source,
            where=where,
            out_fields=out_fields,
            token=token,
            objectid_batch_size=batch_size,
            workers=workers,
            bounds=bounds,
            bounds_sr=_epsg_of(bounds_crs) if bounds is not None else None,
            out_sr=out_sr,
            sign_in=sign_in,
            decode_domains=decode_domains,
        )

    path = Path(source).expanduser()
    if not path.exists():
        raise FileNotFoundError(
            f"{path} does not exist. Pass an ArcGIS layer URL or a readable "
            "vector file (.gpkg, .geojson, .shp, .parquet)."
        )
    progress(f"Reading {path}" + (f" layer {layer}" if layer else ""))
    gdf = gpd.read_file(path, layer=layer) if layer else gpd.read_file(path)

    if where and where.strip() not in ("", "1=1"):
        # A file has no SQL engine behind it, so the WHERE has to be a pandas
        # expression here. Saying so beats silently ignoring the filter.
        progress(f"Applying local filter: {where}")
        gdf = gdf.query(where)

    if bounds is not None and not gdf.empty:
        envelope = gpd.GeoSeries(
            [shapely_box(*[float(value) for value in bounds])],
            crs=bounds_crs or gdf.crs,
        ).to_crs(gdf.crs)
        gdf = gdf[gdf.intersects(envelope.iloc[0])]

    if out_sr is not None and gdf.crs is not None:
        gdf = gdf.to_crs(out_sr)
    return gdf.reset_index(drop=True)


def _epsg_of(crs: Any) -> int:
    from pyproj import CRS

    if crs is None:
        raise ValueError("A bounding box needs a CRS.")
    code = CRS.from_user_input(crs).to_epsg()
    if code is None:
        raise ValueError(
            "The bounding-box CRS has no EPSG code, which an ArcGIS query needs. "
            "Pass bounds in an EPSG-coded CRS."
        )
    return code


def expand_bounds(bounds: Sequence[float], margin: float) -> tuple[float, float, float, float]:
    """Grow an extent by a margin in the extent's own units."""
    minx, miny, maxx, maxy = (float(value) for value in bounds)
    return (minx - margin, miny - margin, maxx + margin, maxy + margin)
