from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable, Sequence
from concurrent import futures
from pathlib import Path
from typing import Any

import geopandas as gpd
import requests
from pyproj import CRS
from shapely.geometry import (
    LineString,
    MultiLineString,
    MultiPolygon,
    Point,
    Polygon,
    shape,
)

# Estimate_GIS module defaults must exist before function signatures.
DEFAULT_TIMEOUT_SECONDS = int(os.environ.get("ESTIMATE_GIS_TIMEOUT_SECONDS", "120"))
DEFAULT_REQUEST_PAGE_SIZE = int(os.environ.get("ESTIMATE_GIS_REQUEST_PAGE_SIZE", "2000"))
DEFAULT_OBJECTID_BATCH_SIZE = int(os.environ.get("ESTIMATE_GIS_OBJECTID_BATCH_SIZE", "2000"))
DEFAULT_DOWNLOAD_WORKERS = int(os.environ.get("ESTIMATE_GIS_DOWNLOAD_WORKERS", "8"))
VERIFY_SSL = os.environ.get("ESTIMATE_GIS_VERIFY_SSL", "1").lower() not in {"0", "false", "no"}

PROGRESS_ENABLED = os.environ.get("ESTIMATE_GIS_PROGRESS", "1").lower() not in {"0", "false", "no"}
PROGRESS_TOTAL_OBJECTS = 0
PROGRESS_TOTAL_BATCHES = 0
PROGRESS_COMPLETED_BATCHES = 0

def progress(message: str) -> None:
    if PROGRESS_ENABLED:
        print(f"[Estimate_GIS] {message}", flush=True)

def _truthy(value: object) -> bool:
    return value is True or str(value).lower() == "true"

def progress_request(url: str, params: dict[str, object]) -> None:
    if not PROGRESS_ENABLED:
        return
    if "objectIds" in params:
        return
    if _truthy(params.get("returnIdsOnly")):
        progress(f"Requesting object IDs from {url} where={params.get('where', '1=1')}")
        return
    preview: dict[str, object] = {}
    for key in ("f", "where", "returnCountOnly", "resultOffset", "resultRecordCount", "outFields"):
        if key in params:
            preview[key] = params[key]
    progress(f"GET {url} params={preview}")

def progress_response(params: dict[str, object], data: dict[str, object]) -> None:
    global PROGRESS_TOTAL_OBJECTS, PROGRESS_TOTAL_BATCHES, PROGRESS_COMPLETED_BATCHES
    if not PROGRESS_ENABLED:
        return
    if isinstance(data, dict) and data.get("error"):
        return
    if _truthy(params.get("returnIdsOnly")):
        object_ids = data.get("objectIds") or []
        PROGRESS_TOTAL_OBJECTS = len(object_ids) if isinstance(object_ids, list) else 0
        batch_size = int(os.environ.get("ESTIMATE_GIS_OBJECTID_BATCH_SIZE", "2000"))
        PROGRESS_TOTAL_BATCHES = (PROGRESS_TOTAL_OBJECTS + batch_size - 1) // batch_size if PROGRESS_TOTAL_OBJECTS else 0
        PROGRESS_COMPLETED_BATCHES = 0
        progress(
            f"Object IDs received: {PROGRESS_TOTAL_OBJECTS:,} total | "
            f"max {batch_size:,}/request | {PROGRESS_TOTAL_BATCHES:,} feature requests"
        )
        return
    if "objectIds" in params:
        PROGRESS_COMPLETED_BATCHES += 1
        object_ids_text = str(params.get("objectIds", ""))
        object_id_count = len([item for item in object_ids_text.split(",") if item])
        batch_size = int(os.environ.get("ESTIMATE_GIS_OBJECTID_BATCH_SIZE", "2000"))
        requested = min(PROGRESS_COMPLETED_BATCHES * batch_size, PROGRESS_TOTAL_OBJECTS) if PROGRESS_TOTAL_OBJECTS else PROGRESS_COMPLETED_BATCHES * object_id_count
        if PROGRESS_TOTAL_BATCHES:
            progress(
                f"Feature batch {PROGRESS_COMPLETED_BATCHES:,}/{PROGRESS_TOTAL_BATCHES:,}: "
                f"{object_id_count:,} ids | requested {requested:,}/{PROGRESS_TOTAL_OBJECTS:,} objects"
            )
        else:
            progress(f"Feature batch {PROGRESS_COMPLETED_BATCHES:,}: {object_id_count:,} ids")

def make_session(token: str | None = None) -> requests.Session:
    """Create a requests session and attach an ArcGIS token for all requests."""
    session = requests.Session()
    session.headers.update({"User-Agent": "Estimate_GIS/1.0"})
    if token:
        setattr(session, "arcgis_token", token)
        session.params.update({"token": token})
    return session

def _with_token(session: requests.Session, params: dict[str, Any] | None = None) -> dict[str, Any]:
    request_params = dict(params or {})
    request_params.setdefault("f", "json")
    token = getattr(session, "arcgis_token", None)
    if token and "token" not in request_params:
        request_params["token"] = token
    return request_params

def _query_url(layer_url: str) -> str:
    layer_url = layer_url.rstrip("/")
    if layer_url.lower().endswith("/query"):
        return layer_url
    return f"{layer_url}/query"

def _post_query_params(session: requests.Session, request_params: dict[str, Any]) -> dict[str, Any]:
    """Keep token/f in the URL query for secured ArcGIS Server POST feature requests."""
    post_params: dict[str, Any] = {"f": request_params.get("f", "json")}
    token = request_params.get("token") or getattr(session, "arcgis_token", None)
    if token:
        post_params["token"] = token
    return post_params

def request_json(
    session: requests.Session,
    url: str,
    params: dict[str, Any] | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    post: bool = False,
) -> dict[str, Any]:
    """Request ArcGIS JSON and retry transient Token Required responses."""
    request_params = _with_token(session, params)
    progress_request(url, request_params)
    attempts = int(os.environ.get("ESTIMATE_GIS_REQUEST_RETRIES", "3"))
    last_error: dict[str, Any] | None = None
    for attempt in range(1, attempts + 1):
        if post:
            response = session.post(
                url,
                params=_post_query_params(session, request_params),
                data=request_params,
                timeout=timeout,
                verify=VERIFY_SSL,
            )
        else:
            response = session.get(
                url,
                params=request_params,
                timeout=timeout,
                verify=VERIFY_SSL,
            )
        response.raise_for_status()
        data = response.json()
        error = data.get("error") if isinstance(data, dict) else None
        if not error:
            progress_response(request_params, data)
            return data
        last_error = error
        code = error.get("code")
        message = error.get("message", "")
        if code in {498, 499} and attempt < attempts:
            if attempt == 1:
                progress(
                    f"ArcGIS Server rejected a secured POST feature request even though a cached token is present. code={code}, message={message}. "
                    f"Retrying up to {attempts} attempts."
                )
            time.sleep(min(2 * attempt, 8))
            continue
        break
    raise RuntimeError(json.dumps(last_error, indent=2))

def layer_metadata(session: requests.Session, layer_url: str) -> dict[str, Any]:
    """Read layer metadata needed for fast REST querying and GeoDataFrame CRS assignment."""
    data = request_json(session, layer_url, {"f": "json"})
    fields = data.get("fields", []) or []
    object_id_field = data.get("objectIdField") or next(
        (
            field.get("name")
            for field in fields
            if field.get("type") == "esriFieldTypeOID"
        ),
        None,
    )
    spatial_reference = (
        (data.get("extent", {}) or {}).get("spatialReference")
        or data.get("spatialReference")
        or {}
    )
    wkid = spatial_reference.get("latestWkid") or spatial_reference.get("wkid")
    max_record_count = int(data.get("maxRecordCount") or DEFAULT_REQUEST_PAGE_SIZE)
    page_size = (
        min(DEFAULT_REQUEST_PAGE_SIZE, max_record_count)
        if max_record_count > 0
        else DEFAULT_REQUEST_PAGE_SIZE
    )
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
        yield values[index : index + chunk_size]

def query_count(session: requests.Session, layer_url: str, where: str) -> int | None:
    data = request_json(
        session, _query_url(layer_url), {"where": where, "returnCountOnly": "true"}
    )
    count = data.get("count")
    return int(count) if count is not None else None

def query_object_ids(
    session: requests.Session,
    layer_url: str,
    where: str,
    *,
    order_by_fields: str | None = None,
) -> list[int]:
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
        coords = [
            (float(point[0]), float(point[1])) for point in path if len(point) >= 2
        ]
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
        coords = [
            (float(point[0]), float(point[1])) for point in ring if len(point) >= 2
        ]
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

def features_to_geodataframe(
    features: list[dict[str, Any]], meta: dict[str, Any]
) -> gpd.GeoDataFrame:
    """Convert ArcGIS feature JSON into a GeoDataFrame using the layer native CRS."""
    rows = []
    for feature in features:
        attributes = dict(feature.get("attributes") or {})
        attributes["geometry"] = esri_geometry_to_shape(feature.get("geometry"))
        rows.append(attributes)
    crs = crs_from_metadata(meta)
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=crs)

def fetch_objectid_batch(
    layer_url: str,
    object_id_batch: Sequence[int],
    meta: dict[str, Any],
    out_fields: str,
    token: str | None,
    batch_number: int,
    batch_total: int,
) -> list[dict[str, Any]]:
    """Download a specific OBJECTID batch. The ids come from query_object_ids for the same WHERE clause."""
    if not token:
        raise RuntimeError(
            "Feature batch request has no ArcGIS token. The main runner resolved a token, "
            "but it was not passed into fetch_objectid_batch."
        )
    session = make_session(token)
    params = {
        "objectIds": ",".join(str(object_id) for object_id in object_id_batch),
        "outFields": out_fields,
        "returnGeometry": "true",
        "returnZ": "false",
        "returnM": "false",
    }
    try:
        data = request_json(session, _query_url(layer_url), params, post=True)
    except RuntimeError as exc:
        first_id = object_id_batch[0] if object_id_batch else None
        last_id = object_id_batch[-1] if object_id_batch else None
        raise RuntimeError(
            f"Feature batch failed for OBJECTID range {first_id}..{last_id} "
            f"({len(object_id_batch)} ids). Secured service still returned auth failure after retries. "
            f"Keep WORKERS=1 or rerun after a fresh portal token. Details: {exc}"
        ) from exc
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
    progress(f"Querying layer to GeoDataFrame: {layer_url}")
    """Fast ArcGIS REST query that returns a GeoDataFrame.

    This avoids offset pagination and avoids pulling OBJECTIDs from a different request scope.
    The WHERE clause used to request OBJECTIDs is the same WHERE clause that defines the download set.
    """
    session = make_session(token)
    meta = layer_metadata(session, layer_url)
    object_ids = query_object_ids(
        session, layer_url, where, order_by_fields=order_by_fields
    )
    if not object_ids:
        return features_to_geodataframe([], meta)
    batches = list(chunk_list(object_ids, objectid_batch_size))
    token_value = getattr(session, "_arcgis_access_token", None)
    features: list[dict[str, Any]] = []
    max_workers = max(1, min(int(workers), len(batches)))
    if max_workers == 1:
        for batch_number, batch in enumerate(batches, start=1):
            features.extend(
                fetch_objectid_batch(
                    layer_url,
                    batch,
                    meta,
                    out_fields,
                    token_value,
                    batch_number,
                    len(batches),
                )
            )
    else:
        with futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_list = [
                executor.submit(
                    fetch_objectid_batch,
                    layer_url,
                    batch,
                    meta,
                    out_fields,
                    token_value,
                    index,
                    len(batches),
                )
                for index, batch in enumerate(batches, start=1)
            ]
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
    progress(f"Starting export: layer_url={layer_url}, output_gpkg={output_gpkg}, layer_name={layer_name}, where={where}")
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