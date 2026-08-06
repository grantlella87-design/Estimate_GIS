
"""
Fast ArcGIS REST to GeoPandas utilities for Estimate_GIS.

This is intentionally modeled after the fast leak_relocation_geopandas pattern:
- Read layer metadata first so OBJECTID, maxRecordCount, fields, and native CRS come from the MapServer layer.
- Ask the server for the matching OBJECTIDs for the exact WHERE clause.
- Download only those matching OBJECTIDs in threaded batches instead of walking every OBJECTID from an unrelated query.
- Convert Esri JSON geometries directly into Shapely geometries.
- Return a GeoDataFrame so downstream code can filter, join, export, or write a GeoPackage.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from concurrent import futures
from pathlib import Path
from typing import Any

import geopandas as gpd
import requests
from pyproj import CRS
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Point, Polygon, shape

import geopandas as gpd
import pandas as pd
import requests
from pyproj import CRS
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Point, Polygon, shape

DEFAULT_REQUEST_PAGE_SIZE = int(os.environ.get("ESTIMATE_GIS_REQUEST_PAGE_SIZE", "2000"))
DEFAULT_OBJECTID_BATCH_SIZE = int(os.environ.get("ESTIMATE_GIS_OBJECTID_BATCH_SIZE", "500"))
DEFAULT_DOWNLOAD_WORKERS = int(os.environ.get("ESTIMATE_GIS_DOWNLOAD_WORKERS", "8"))
DEFAULT_TIMEOUT_SECONDS = int(os.environ.get("ESTIMATE_GIS_TIMEOUT_SECONDS", "120"))
VERIFY_SSL = os.environ.get("ESTIMATE_GIS_VERIFY_SSL", "true").strip().lower() not in {"0", "false", "no"}

def make_session(token: str | None = None) -> requests.Session:
    """Create a requests session and attach an ArcGIS token if one is supplied."""
    session = requests.Session()
    session._arcgis_access_token = token or os.environ.get("ARCGIS_TOKEN") or os.environ.get("ESTIMATE_GIS_ARCGIS_TOKEN")
    return session

def _query_url(layer_url: str) -> str:
    return layer_url.rstrip("/") + "/query"

def _with_token(session: requests.Session, params: dict[str, Any] | None) -> dict[str, Any]:
    request_params = dict(params or {})
    request_params.setdefault("f", "json")
    token = getattr(session, "_arcgis_access_token", None)
    if token and "token" not in request_params:
        request_params["token"] = token
    return request_params

def request_json(session: requests.Session, url: str, params: dict[str, Any] | None = None, *, post: bool = False, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Request ArcGIS JSON and raise a useful exception for service-side errors."""
    request_params = _with_token(session, params)
    if post:
        response = session.post(url, data=request_params, timeout=timeout, verify=VERIFY_SSL)
    else:
        response = session.get(url, params=request_params, timeout=timeout, verify=VERIFY_SSL)
    response.raise_for_status()
    data = response.json()
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(json.dumps(data["error"], indent=2))
    return data

def layer_metadata(session: requests.Session, layer_url: str) -> dict[str, Any]:
    """Read layer metadata needed for fast REST querying and GeoDataFrame CRS assignment."""
    data = request_json(session, layer_url, {"f": "json"})
    fields = data.get("fields", []) or []
    object_id_field = data.get("objectIdField") or next((field.get("name") for field in fields if field.get("type") == "esriFieldTypeOID"), None)
    spatial_reference = (data.get("extent", {}) or {}).get("spatialReference") or data.get("spatialReference") or {}
    wkid = spatial_reference.get("latestWkid") or spatial_reference.get("wkid")
    max_record_count = int(data.get("maxRecordCount") or DEFAULT_REQUEST_PAGE_SIZE)
    page_size = min(DEFAULT_REQUEST_PAGE_SIZE, max_record_count) if max_record_count > 0 else DEFAULT_REQUEST_PAGE_SIZE
    return {
        "object_id_field": object_id_field,
        "fields": fields,
        "spatial_reference": spatial_reference,
        "wkid": wkid,
        "max_record_count": max_record_count,
        "page_size": page_size,
        "geometry_type": data.get("geometryType"),
        "name": data.get("name"),
    }

def crs_from_metadata(meta: dict[str, Any]) -> CRS | None:
    """Build CRS from service metadata without forcing EPSG:2249 or another default."""
    sr = meta.get("spatial_reference") or {}
    if sr.get("wkt"):
        return CRS.from_wkt(sr["wkt"])
    wkid = sr.get("latestWkid") or sr.get("wkid") or meta.get("wkid")
    if wkid:
        return CRS.from_epsg(int(wkid))
    return None

def chunk_list(values: Sequence[Any], chunk_size: int) -> Iterable[Sequence[Any]]:
    for index in range(0, len(values), chunk_size):
        yield values[index:index + chunk_size]

def query_count(session: requests.Session, layer_url: str, where: str) -> int | None:
    data = request_json(session, _query_url(layer_url), {"where": where, "returnCountOnly": "true"})
    count = data.get("count")
    return int(count) if count is not None else None

def query_object_ids(session: requests.Session, layer_url: str, where: str, *, order_by_fields: str | None = None) -> list[int]:
    """Ask the service for OBJECTIDs matching the exact WHERE clause that will be downloaded."""
    params = {"where": where, "returnIdsOnly": "true"}
    if order_by_fields:
        params["orderByFields"] = order_by_fields
    data = request_json(session, _query_url(layer_url), params, post=True)
    object_ids = data.get("objectIds") or []
    return [int(object_id) for object_id in object_ids]

def esri_point_to_geom(geometry: dict[str, Any]) -> Point | None:
    if not geometry or "x" not in geometry or "y" not in geometry:
        return None
    return Point(float(geometry["x"]), float(geometry["y"]))

def esri_polyline_to_geom(geometry: dict[str, Any]) -> Any | None:
    if not geometry or "paths" not in geometry:
        return None
    lines = []
    for path in geometry.get("paths", []):
        coords = [(float(point[0]), float(point[1])) for point in path if len(point) >= 2]
        if len(coords) >= 2:
            lines.append(LineString(coords))
    if not lines:
        return None
    if len(lines) == 1:
        return lines[0]
    return MultiLineString(lines)

def esri_polygon_to_geom(geometry: dict[str, Any]) -> Any | None:
    if not geometry or "rings" not in geometry:
        return None
    polygons = []
    for ring in geometry.get("rings", []):
        coords = [(float(point[0]), float(point[1])) for point in ring if len(point) >= 2]
        if len(coords) >= 4:
            polygons.append(Polygon(coords))
    if not polygons:
        return None
    if len(polygons) == 1:
        return polygons[0]
    return MultiPolygon(polygons)

def esri_geometry_to_shape(geometry: dict[str, Any] | None) -> Any | None:
    if not geometry:
        return None
    if "x" in geometry and "y" in geometry:
        return esri_point_to_geom(geometry)
    if "paths" in geometry:
        return esri_polyline_to_geom(geometry)
    if "rings" in geometry:
        return esri_polygon_to_geom(geometry)
    try:
        return shape(geometry)
    except (AttributeError, TypeError, ValueError):
        return None

def features_to_geodataframe(features: list[dict[str, Any]], meta: dict[str, Any]) -> gpd.GeoDataFrame:
    """Convert ArcGIS feature JSON into a GeoDataFrame using the layer native CRS."""
    rows = []
    for feature in features:
        attributes = dict(feature.get("attributes") or {})
        attributes["geometry"] = esri_geometry_to_shape(feature.get("geometry"))
        rows.append(attributes)
    crs = crs_from_metadata(meta)
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=crs)

def fetch_objectid_batch(layer_url: str, object_id_batch: Sequence[int], meta: dict[str, Any], out_fields: str, token: str | None, batch_number: int, batch_total: int) -> list[dict[str, Any]]:
    """Download a specific OBJECTID batch. The ids come from query_object_ids for the same WHERE clause."""
    session = make_session(token)
    params = {
        "objectIds": ",".join(str(object_id) for object_id in object_id_batch),
        "outFields": out_fields,
        "returnGeometry": "true",
        "returnZ": "false",
        "returnM": "false",
    }
    data = request_json(session, _query_url(layer_url), params, post=True)
    return data.get("features") or []

def query_layer_to_geodataframe(
    layer_url: str,
    where: str = "1=1",
    out_fields: str = "*",
    token: str | None = None,
    objectid_batch_size: int = DEFAULT_OBJECTID_BATCH_SIZE,
    workers: int = DEFAULT_DOWNLOAD_WORKERS,
    order_by_fields: str | None = None,
) -> gpd.GeoDataFrame:
    """Fast ArcGIS REST query that returns a GeoDataFrame.

    This avoids offset pagination and avoids pulling OBJECTIDs from a different request scope.
    The WHERE clause used to request OBJECTIDs is the same WHERE clause that defines the download set.
    """
    session = make_session(token)
    meta = layer_metadata(session, layer_url)
    object_ids = query_object_ids(session, layer_url, where, order_by_fields=order_by_fields)
    if not object_ids:
        return features_to_geodataframe([], meta)
    batches = list(chunk_list(object_ids, objectid_batch_size))
    token_value = getattr(session, "_arcgis_access_token", None)
    features: list[dict[str, Any]] = []
    max_workers = max(1, min(int(workers), len(batches)))
    if max_workers == 1:
        for batch_number, batch in enumerate(batches, start=1):
            features.extend(fetch_objectid_batch(layer_url, batch, meta, out_fields, token_value, batch_number, len(batches)))
    else:
        with futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_list = [executor.submit(fetch_objectid_batch, layer_url, batch, meta, out_fields, token_value, index, len(batches)) for index, batch in enumerate(batches, start=1)]
            for future in futures.as_completed(future_list):
                features.extend(future.result())
    gdf = features_to_geodataframe(features, meta)
    object_id_field = meta.get("object_id_field")
    if object_id_field and object_id_field in gdf.columns:
        gdf = gdf.sort_values(object_id_field).reset_index(drop=True)
    return gdf

def export_layer_to_geopackage(
    layer_url: str,
    output_gpkg: str | Path,
    layer_name: str = "arcgis_export",
    where: str = "1=1",
    out_fields: str = "*",
    token: str | None = None,
    objectid_batch_size: int = DEFAULT_OBJECTID_BATCH_SIZE,
    workers: int = DEFAULT_DOWNLOAD_WORKERS,
) -> gpd.GeoDataFrame:
    """Query an ArcGIS layer to GeoPandas and write it to a GeoPackage layer."""
    output_path = Path(output_gpkg)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    gdf = query_layer_to_geodataframe(
        layer_url=layer_url,
        where=where,
        out_fields=out_fields,
        token=token,
        objectid_batch_size=objectid_batch_size,
        workers=workers,
    )
    if output_path.exists():
        output_path.unlink()
    gdf.to_file(output_path, layer=layer_name, driver="GPKG")
    return gdf
