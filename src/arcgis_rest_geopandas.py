from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable, Sequence
from concurrent import futures
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
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

# Which hosts get a token is decided from the URL, not by the caller.
import service_auth

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
    global PROGRESS_COMPLETED_BATCHES, PROGRESS_REQUESTED_OBJECTS, PROGRESS_TOTAL_BATCHES, PROGRESS_TOTAL_OBJECTS
    if not PROGRESS_ENABLED:
        return
    if isinstance(data, dict) and data.get("error"):
        return
    if _truthy(params.get("returnIdsOnly")):
        object_ids = data.get("objectIds") or []
        PROGRESS_TOTAL_OBJECTS = len(object_ids)
        PROGRESS_TOTAL_BATCHES = 0
        PROGRESS_COMPLETED_BATCHES = 0
        PROGRESS_REQUESTED_OBJECTS = 0
        max_record_count = int(params.get("resultRecordCount") or DEFAULT_OBJECTID_BATCH_SIZE)
        if max_record_count > 0:
            PROGRESS_TOTAL_BATCHES = (PROGRESS_TOTAL_OBJECTS + max_record_count - 1) // max_record_count
        progress(
            f"Object IDs received: {PROGRESS_TOTAL_OBJECTS:,} total | "
            f"max {max_record_count:,}/request | {PROGRESS_TOTAL_BATCHES:,} feature requests"
        )
        return
    features = data.get("features") if isinstance(data, dict) else None
    if features is None:
        return
    feature_count = len(features)
    PROGRESS_COMPLETED_BATCHES += 1
    PROGRESS_REQUESTED_OBJECTS = min(PROGRESS_TOTAL_OBJECTS, PROGRESS_REQUESTED_OBJECTS + feature_count)
    total_batches = PROGRESS_TOTAL_BATCHES or PROGRESS_COMPLETED_BATCHES
    progress(
        f"Feature batch {PROGRESS_COMPLETED_BATCHES:,}/{total_batches:,}: "
        f"{feature_count:,} ids | completed {PROGRESS_REQUESTED_OBJECTS:,}/{PROGRESS_TOTAL_OBJECTS:,} objects"
    )

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
    """Request ArcGIS JSON with retries for transient connection resets and service errors."""
    request_params = _with_token(session, params)
    progress_request(url, request_params)
    attempts = int(os.environ.get("ESTIMATE_GIS_REQUEST_RETRIES", "5"))
    last_error: dict[str, Any] | None = None
    last_exception: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
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
            if code in {498, 499, 500, 502, 503, 504} and attempt < attempts:
                progress(
                    f"ArcGIS REST retry {attempt}/{attempts} for service response "
                    f"code={code}, message={message}."
                )
                time.sleep(min(2 * attempt, 10))
                continue
            break
        except requests.RequestException as exc:
            last_exception = exc
            if attempt < attempts:
                progress(
                    f"ArcGIS REST retry {attempt}/{attempts} after connection error: "
                    f"{type(exc).__name__}: {exc}"
                )
                time.sleep(min(2 * attempt, 10))
                continue
            raise RuntimeError(f"ArcGIS REST request failed after {attempts} attempts: {url}: {exc}") from exc
    if last_error is not None:
        # A rejected token is nearly always the host policy, not a bad password:
        # say which side of the internal/public line this URL fell on.
        if last_error.get("code") in {498, 499}:
            raise RuntimeError(
                json.dumps(last_error, indent=2) + "\n" + service_auth.token_hint(url)
            )
        raise RuntimeError(json.dumps(last_error, indent=2))
    if last_exception is not None:
        raise RuntimeError(f"ArcGIS REST request failed after {attempts} attempts: {url}: {last_exception}") from last_exception
    raise RuntimeError(f"ArcGIS REST request failed without JSON response: {url}")

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

def chunked(values: Sequence[int], chunk_size: int) -> Iterable[Sequence[int]]:
    """Yield object IDs in ArcGIS query-sized batches."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than zero")
    for index in range(0, len(values), chunk_size):
        yield values[index : index + chunk_size]

def envelope_filter(
    bounds: Sequence[float], in_sr: int | str
) -> dict[str, Any]:
    """Build the ArcGIS query parameters that limit a query to a bounding box.

    Fetching a statewide reference layer to intersect a handful of miles of pipe
    wastes most of the download, so callers pass the extent they actually need.
    `bounds` is (minx, miny, maxx, maxy) in the `in_sr` coordinate system, which
    is the order GeoPandas `total_bounds` returns.
    """
    minx, miny, maxx, maxy = (float(value) for value in bounds)
    return {
        "geometry": json.dumps(
            {
                "xmin": minx,
                "ymin": miny,
                "xmax": maxx,
                "ymax": maxy,
                "spatialReference": {"wkid": int(in_sr)},
            }
        ),
        "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": str(in_sr),
    }

def query_count(
    session: requests.Session,
    layer_url: str,
    where: str,
    *,
    extra_params: dict[str, Any] | None = None,
) -> int | None:
    params: dict[str, Any] = {"where": where, "returnCountOnly": "true"}
    params.update(extra_params or {})
    data = request_json(session, _query_url(layer_url), params, post=True)
    count = data.get("count")
    return int(count) if count is not None else None

def query_object_ids(
    session: requests.Session,
    layer_url: str,
    where: str,
    *,
    order_by_fields: str | None = None,
    extra_params: dict[str, Any] | None = None,
) -> list[int]:
    """Ask the service for OBJECTIDs matching the exact WHERE clause that will be downloaded."""
    params: dict[str, Any] = {"where": where, "returnIdsOnly": "true"}
    if order_by_fields:
        params["orderByFields"] = order_by_fields
    params.update(extra_params or {})
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

def _ring_signed_area(coords: Sequence[tuple[float, float]]) -> float:
    """Shoelace area; negative for the clockwise winding Esri uses for outer rings."""
    total = 0.0
    for index in range(len(coords) - 1):
        x1, y1 = coords[index]
        x2, y2 = coords[index + 1]
        total += (x2 - x1) * (y2 + y1)
    return -total / 2.0

def esri_polygon_to_geom(geometry: dict[str, Any]) -> Any | None:
    """Rebuild an Esri polygon, honouring interior rings.

    Esri puts every ring in one flat list and distinguishes them by winding:
    clockwise is an outer ring, counter-clockwise is a hole. Treating each ring
    as its own polygon fills the holes back in, which inflates area and makes a
    line crossing a doughnut-shaped ledge polygon look like it runs through the
    middle of it.
    """
    if not geometry or "rings" not in geometry:
        return None
    exteriors: list[list[tuple[float, float]]] = []
    holes: list[list[tuple[float, float]]] = []
    for ring in geometry.get("rings", []):
        coords = [
            (float(point[0]), float(point[1])) for point in ring if len(point) >= 2
        ]
        if len(coords) < 4:
            continue
        if coords[0] != coords[-1]:
            coords.append(coords[0])
        if _ring_signed_area(coords) < 0:
            exteriors.append(coords)
        else:
            holes.append(coords)
    if not exteriors:
        # Malformed or single-ring-only winding: treat every ring as an outer ring
        # rather than dropping the feature.
        exteriors, holes = holes, []
    if not exteriors:
        return None
    shells = [Polygon(coords) for coords in exteriors]
    assigned: list[list[list[tuple[float, float]]]] = [[] for _ in shells]
    for hole in holes:
        point = Polygon(hole).representative_point()
        # Smallest containing shell, so nested rings land on the right parent.
        candidates = [
            index for index, shell in enumerate(shells) if shell.contains(point)
        ]
        if not candidates:
            continue
        assigned[min(candidates, key=lambda index: shells[index].area)].append(hole)
    polygons = [
        Polygon(exteriors[index], assigned[index]) for index in range(len(shells))
    ]
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
    """Convert ArcGIS feature JSON into a GeoDataFrame using the layer native CRS.

    A query that matches nothing is an ordinary outcome - an extent with no ledge
    in it, a WHERE clause that excludes everything - so it returns an empty
    GeoDataFrame with the layer's own columns rather than raising. Building one
    from an empty list without doing this fails with "Unknown column geometry",
    which says nothing about the query that produced it.
    """
    crs = crs_from_metadata(meta)
    if not features:
        columns = [
            field.get("name")
            for field in meta.get("fields") or []
            if field.get("name") and field.get("type") != "esriFieldTypeGeometry"
        ]
        empty = gpd.GeoDataFrame(
            {name: pd.Series(dtype="object") for name in columns},
            geometry=gpd.GeoSeries([], dtype="geometry", crs=crs),
            crs=crs,
        )
        return empty
    rows = []
    for feature in features:
        attributes = dict(feature.get("attributes") or {})
        attributes["geometry"] = esri_geometry_to_shape(feature.get("geometry"))
        rows.append(attributes)
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=crs)

def fetch_objectid_batch(
    layer_url: str,
    object_id_batch: Sequence[int],
    out_fields: str,
    token: str | None,
    *,
    out_sr: int | str | None = None,
) -> list[dict[str, Any]]:
    """Download a specific OBJECTID batch using the already-resolved token.

    An internal layer with no token is an error here rather than a run of
    "Token Required" responses that fail late and say nothing about the cause.
    A public layer needs no token, so a missing one is not a problem to report.
    """
    if not token and service_auth.requires_token(layer_url):
        raise RuntimeError(
            "Feature batch request has no ArcGIS token passed from "
            f"query_layer_to_geodataframe. {service_auth.token_hint(layer_url)}"
        )
    session = make_session(token)
    params = {
        "objectIds": ",".join(str(object_id) for object_id in object_id_batch),
        "outFields": out_fields,
        "returnGeometry": "true",
        "returnZ": "false",
        "returnM": "false",
    }
    if out_sr:
        params["outSR"] = str(out_sr)
    try:
        data = request_json(session, _query_url(layer_url), params, post=True)
    except RuntimeError as exc:
        first_id = object_id_batch[0] if object_id_batch else None
        last_id = object_id_batch[-1] if object_id_batch else None
        raise RuntimeError(
            f"Feature batch failed for OBJECTID range {first_id}..{last_id} "
            f"({len(object_id_batch)} ids): {exc}"
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
    *,
    bounds: Sequence[float] | None = None,
    bounds_sr: int | str | None = None,
    out_sr: int | str | None = None,
    sign_in: bool = True,
) -> gpd.GeoDataFrame:
    """Fast ArcGIS REST query that returns a GeoDataFrame.

    `bounds` limits the query to an extent, and `out_sr` asks the service to
    project on the way out so two layers from different services arrive in one
    coordinate system.

    The token is resolved from the URL rather than from the caller: our own
    servers get one, signing in through `auth.py` if `token` was not supplied,
    and public services get none. Passing a token for a public host does not
    send it - see `service_auth`.
    """
    progress(f"Querying layer to GeoDataFrame: {layer_url}")
    service_auth.report_once(layer_url)
    token = service_auth.token_for(layer_url, token, allow_sign_in=sign_in)
    session = make_session(token)
    meta = layer_metadata(session, layer_url)
    if out_sr:
        # The service projects the geometry it returns, so the GeoDataFrame CRS
        # has to follow the request rather than the layer's own spatial reference.
        meta = {**meta, "spatial_reference": {"wkid": int(out_sr)}, "wkid": int(out_sr)}
    extra_params: dict[str, Any] | None = None
    if bounds is not None:
        extra_params = envelope_filter(bounds, bounds_sr or meta.get("wkid") or 4326)
    object_ids = query_object_ids(
        session=session,
        layer_url=layer_url,
        where=where,
        order_by_fields=order_by_fields,
        extra_params=extra_params,
    )
    if not object_ids:
        progress("No object IDs returned for this query.")
        return features_to_geodataframe([], meta)
    object_id_batches = list(chunked(object_ids, objectid_batch_size))
    global PROGRESS_TOTAL_BATCHES, PROGRESS_COMPLETED_BATCHES, PROGRESS_REQUESTED_OBJECTS
    PROGRESS_TOTAL_BATCHES = len(object_id_batches)
    PROGRESS_COMPLETED_BATCHES = 0
    PROGRESS_REQUESTED_OBJECTS = 0
    progress(
        f"Downloading {len(object_ids):,} objects in {len(object_id_batches):,} "
        f"feature requests with {workers:,} workers"
    )
    features: list[dict[str, Any]] = []
    if workers <= 1:
        for object_id_batch in object_id_batches:
            features.extend(
                fetch_objectid_batch(
                    layer_url=layer_url,
                    object_id_batch=object_id_batch,
                    out_fields=out_fields,
                    token=token,
                    out_sr=out_sr,
                )
            )
    else:
        with futures.ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_batch = {
                executor.submit(
                    fetch_objectid_batch,
                    layer_url=layer_url,
                    object_id_batch=object_id_batch,
                    out_fields=out_fields,
                    token=token,
                    out_sr=out_sr,
                ): object_id_batch
                for object_id_batch in object_id_batches
            }
            for future in futures.as_completed(future_to_batch):
                features.extend(future.result())
    return features_to_geodataframe(features, meta)

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