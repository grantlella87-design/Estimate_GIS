"""Intersect linear assets with ledge polygons and age them against a cutoff date.

The two questions this answers are independent, which is why they are computed
separately and then crossed:

* how much of each line runs through ledge, measured as length rather than as a
  yes/no flag - a 900 ft main that clips 20 ft of outcrop is not "in ledge" in
  any sense an estimator would accept;
* whether the line was installed or created on or after a cutoff date, taken
  from whichever date fields the layer actually has.

Nothing here is specific to gas mains or to Massachusetts. Any line layer with a
date field and any polygon layer will run through it, which is what keeps the
same script usable for the next layer someone asks about.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

import geopandas as gpd
import pandas as pd
from pandas.api.types import is_datetime64_any_dtype
from pyproj import CRS

from arcgis_rest_geopandas import progress

METERS_PER_FOOT = 0.3048

# The field names carrying "installed" and "created" differ between layers and
# between vintages of the same layer, so both are looked up rather than assumed.
DEFAULT_INSTALL_FIELDS = (
    "installationdate",
    "inservicedate",
    "install_date",
    "installdate",
    "date_installed",
)
DEFAULT_CREATION_FIELDS = (
    "CREATIONDATE",
    "created_date",
    "createdate",
    "create_date",
    "date_created",
)

UID_COLUMN = "mainline_uid"
LEDGE_LENGTH_COLUMN = "ledge_length_ft"
LEDGE_PCT_COLUMN = "ledge_pct_of_line"
LEDGE_CLASSES_COLUMN = "ledge_classes"
LENGTH_COLUMN = "line_length_ft"
EFFECTIVE_DATE_COLUMN = "effective_date"
POST_CUTOFF_COLUMN = "is_post_cutoff"
IN_LEDGE_COLUMN = "in_ledge"


@dataclass
class DateResolution:
    """Which date columns were used, so the report can state it rather than imply it."""

    install_column: str | None
    creation_column: str | None
    searched_install: tuple[str, ...] = ()
    searched_creation: tuple[str, ...] = ()

    @property
    def columns(self) -> list[str]:
        return [name for name in (self.install_column, self.creation_column) if name]

    def describe(self) -> str:
        if not self.columns:
            return "no date field found"
        parts = []
        if self.install_column:
            parts.append(f"installed: {self.install_column}")
        if self.creation_column:
            parts.append(f"created: {self.creation_column}")
        return ", ".join(parts)


@dataclass
class LedgeResult:
    """Everything a caller needs to write outputs, in one object."""

    mainlines: gpd.GeoDataFrame
    ledge_segments: gpd.GeoDataFrame
    ledge: gpd.GeoDataFrame
    stats: dict[str, Any]
    by_class: pd.DataFrame
    dates: DateResolution
    analysis_crs: CRS
    warnings: list[str] = field(default_factory=list)


# --- Coordinate systems -----------------------------------------------------

def unit_to_feet(crs: CRS) -> float:
    """Feet per one unit of `crs`, so lengths can be reported in feet whatever the CRS."""
    axis = crs.axis_info[0]
    meters_per_unit = float(axis.unit_conversion_factor)
    return meters_per_unit / METERS_PER_FOOT


def resolve_analysis_crs(
    lines: gpd.GeoDataFrame,
    polygons: gpd.GeoDataFrame,
    requested: str | int | None = None,
) -> CRS:
    """Pick the CRS lengths get measured in.

    Measuring in degrees produces numbers that look like answers and are not, so
    a geographic CRS is never used. Preference order: what the caller asked for,
    the lines' own projected CRS, the polygons' projected CRS, then a UTM zone
    derived from the data.
    """
    if requested not in (None, "", "auto"):
        return CRS.from_user_input(requested)
    for frame in (lines, polygons):
        if frame is None or frame.empty or frame.crs is None:
            continue
        crs = CRS.from_user_input(frame.crs)
        if crs.is_projected:
            return crs
    source = lines if lines is not None and not lines.empty else polygons
    if source is None or source.empty or source.crs is None:
        raise ValueError(
            "Cannot choose an analysis CRS: the inputs have no CRS. Pass "
            "--analysis-crs, or set a CRS on the input files."
        )
    progress("No projected CRS on either input; estimating a UTM zone for measurement.")
    return CRS.from_user_input(source.estimate_utm_crs())


# --- Dates ------------------------------------------------------------------

def find_column(columns: Sequence[str], candidates: Sequence[str]) -> str | None:
    """First candidate the frame actually has, matched case-insensitively."""
    lookup = {str(name).lower(): name for name in columns}
    for candidate in candidates:
        match = lookup.get(str(candidate).lower())
        if match is not None:
            return match
    return None


def resolve_date_columns(
    gdf: gpd.GeoDataFrame,
    install_candidates: Sequence[str] = DEFAULT_INSTALL_FIELDS,
    creation_candidates: Sequence[str] = DEFAULT_CREATION_FIELDS,
) -> DateResolution:
    return DateResolution(
        install_column=find_column(gdf.columns, install_candidates),
        creation_column=find_column(gdf.columns, creation_candidates),
        searched_install=tuple(install_candidates),
        searched_creation=tuple(creation_candidates),
    )


def coerce_datetime(values: pd.Series) -> pd.Series:
    """Return a UTC datetime series from whatever the source used for dates.

    ArcGIS REST sends dates as epoch milliseconds, a GeoPackage round-trips them
    as datetimes, and a CSV or GeoJSON carries strings. All three arrive here.
    """
    if is_datetime64_any_dtype(values):
        series = pd.to_datetime(values, errors="coerce")
        if series.dt.tz is None:
            return series.dt.tz_localize("UTC")
        return series.dt.tz_convert("UTC")

    numeric = pd.to_numeric(values, errors="coerce")
    non_null = values.notna() & (values.astype("string").str.strip() != "")
    if non_null.any() and bool((numeric.notna() | ~non_null).all()):
        # Every populated value is a number, so it is an epoch. Milliseconds and
        # seconds are told apart by magnitude: a seconds epoch for any plausible
        # install date is ~1e9, milliseconds ~1e12.
        magnitude = numeric.abs().replace(0, pd.NA).median()
        unit = "ms" if pd.notna(magnitude) and magnitude > 1e11 else "s"
        # ArcGIS writes 0 for "no date" in some layers; that is not 1970.
        numeric = numeric.replace(0, pd.NA)
        return pd.to_datetime(numeric, unit=unit, errors="coerce", utc=True)

    try:
        return pd.to_datetime(values, errors="coerce", utc=True, format="mixed")
    except (TypeError, ValueError):
        return pd.to_datetime(values, errors="coerce", utc=True)


def parse_cutoff(value: str | datetime) -> pd.Timestamp:
    if isinstance(value, datetime):
        stamp = pd.Timestamp(value)
    else:
        stamp = pd.Timestamp(str(value))
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    return stamp.tz_convert("UTC")


def apply_cutoff(
    gdf: gpd.GeoDataFrame, dates: DateResolution, cutoff: pd.Timestamp
) -> tuple[gpd.GeoDataFrame, list[str]]:
    """Add parsed dates, an effective date and the post-cutoff flag.

    "Installed or created" is an OR: the later of the two dates decides, so a
    line created in the GIS in 2023 to record a 1950s install still counts as
    post-cutoff, which is what the phrase asks for. A line with no date at all
    is counted as not post-cutoff and reported separately, because silently
    dropping it would change the denominator.
    """
    warnings: list[str] = []
    result = gdf.copy()
    parsed: list[pd.Series] = []

    for column, label in (
        (dates.install_column, "installed"),
        (dates.creation_column, "created"),
    ):
        if not column:
            continue
        series = coerce_datetime(result[column])
        result[f"date_{label}"] = series
        parsed.append(series)

    if not parsed:
        warnings.append(
            "The layer has no installation or creation date field, so every "
            "line is reported as undated. Pass --install-fields/--creation-fields "
            f"to name them. Searched: {list(dates.searched_install + dates.searched_creation)}"
        )
        result[EFFECTIVE_DATE_COLUMN] = pd.Series(
            pd.NaT, index=result.index, dtype="datetime64[ns, UTC]"
        )
    else:
        # The later of the two dates, ignoring missing ones, so a line with only
        # one of the two still gets an effective date.
        result[EFFECTIVE_DATE_COLUMN] = pd.concat(parsed, axis=1).max(axis=1)

    # Only worth saying when the other field exists: with neither, the message
    # above has already said so, and both halves of this would contradict it.
    if not dates.install_column and dates.creation_column:
        warnings.append(
            f"No installation date field on this layer; the cutoff uses "
            f"{dates.creation_column} alone."
        )
    if not dates.creation_column and dates.install_column:
        warnings.append(
            f"No creation date field on this layer; the cutoff uses "
            f"{dates.install_column} alone."
        )

    result[POST_CUTOFF_COLUMN] = (
        result[EFFECTIVE_DATE_COLUMN].notna() & (result[EFFECTIVE_DATE_COLUMN] >= cutoff)
    )
    undated = int(result[EFFECTIVE_DATE_COLUMN].isna().sum())
    if undated:
        warnings.append(
            f"{undated:,} of {len(result):,} lines have no usable date. They stay "
            "in the denominator and count as before the cutoff."
        )
    return result, warnings


# --- Intersection -----------------------------------------------------------

def ensure_uid(gdf: gpd.GeoDataFrame, preferred: Sequence[str] = ("OBJECTID", "GLOBALID", "objectid")) -> gpd.GeoDataFrame:
    """Give every line a stable id to join intersection results back onto."""
    result = gdf.copy()
    if UID_COLUMN in result.columns:
        return result
    source = find_column(result.columns, preferred)
    if source is not None and result[source].is_unique and result[source].notna().all():
        result[UID_COLUMN] = result[source]
    else:
        result[UID_COLUMN] = range(1, len(result) + 1)
    return result


def intersect_with_ledge(
    mainlines: gpd.GeoDataFrame,
    ledge: gpd.GeoDataFrame,
    analysis_crs: CRS,
    class_field: str = "ledge_class",
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Return (mainlines with ledge columns, per-class ledge segments, ledge in analysis CRS).

    The per-line ledge length is the length of the *union* of the pieces, not the
    sum of them. Bedrock outcrop polygons and the shallow-bedrock overlay
    routinely cover the same ground, and adding both would report more ledge on a
    line than the line has length.
    """
    lines = mainlines.to_crs(analysis_crs)
    polygons = ledge.to_crs(analysis_crs) if not ledge.empty else ledge
    feet_per_unit = unit_to_feet(CRS.from_user_input(analysis_crs))

    lines[LENGTH_COLUMN] = lines.geometry.length * feet_per_unit

    if polygons.empty or lines.empty:
        lines[LEDGE_LENGTH_COLUMN] = 0.0
        lines[LEDGE_PCT_COLUMN] = 0.0
        lines[LEDGE_CLASSES_COLUMN] = ""
        lines[IN_LEDGE_COLUMN] = False
        empty = lines.iloc[0:0].copy()
        return lines, empty, polygons

    keep_class = class_field if class_field in polygons.columns else None
    polygon_columns = [column for column in (keep_class, "ledge_code") if column]
    polygons_for_overlay = polygons[[*polygon_columns, "geometry"]].copy()

    progress(
        f"Intersecting {len(lines):,} lines with {len(polygons_for_overlay):,} ledge polygons."
    )
    # Shortlist first: overlay is the expensive step and most lines never come
    # near ledge, so the spatial index does the elimination.
    candidates = lines[[UID_COLUMN, "geometry"]].sjoin(
        polygons_for_overlay[["geometry"]], predicate="intersects", how="inner"
    )
    candidate_uids = pd.Index(candidates[UID_COLUMN].unique())
    progress(f"{len(candidate_uids):,} lines touch a ledge polygon.")

    if len(candidate_uids) == 0:
        lines[LEDGE_LENGTH_COLUMN] = 0.0
        lines[LEDGE_PCT_COLUMN] = 0.0
        lines[LEDGE_CLASSES_COLUMN] = ""
        lines[IN_LEDGE_COLUMN] = False
        return lines, lines.iloc[0:0].copy(), polygons

    subset = lines[lines[UID_COLUMN].isin(candidate_uids)][[UID_COLUMN, "geometry"]]
    segments = gpd.overlay(
        subset, polygons_for_overlay, how="intersection", keep_geom_type=True
    )
    segments = segments[~segments.geometry.is_empty & segments.geometry.notna()]
    segments["segment_length_ft"] = segments.geometry.length * feet_per_unit

    if segments.empty:
        lines[LEDGE_LENGTH_COLUMN] = 0.0
        lines[LEDGE_PCT_COLUMN] = 0.0
        lines[LEDGE_CLASSES_COLUMN] = ""
        lines[IN_LEDGE_COLUMN] = False
        return lines, segments, polygons

    # Dissolve per line before measuring, so overlapping ledge classes count once.
    merged = segments[[UID_COLUMN, "geometry"]].dissolve(by=UID_COLUMN)
    ledge_length = merged.geometry.length * feet_per_unit

    lines[LEDGE_LENGTH_COLUMN] = (
        lines[UID_COLUMN].map(ledge_length).astype(float).fillna(0.0)
    )
    if keep_class:
        class_names = (
            segments.groupby(UID_COLUMN)[keep_class]
            .apply(lambda values: "; ".join(sorted({str(value) for value in values if pd.notna(value)})))
        )
        lines[LEDGE_CLASSES_COLUMN] = lines[UID_COLUMN].map(class_names).fillna("")
    else:
        lines[LEDGE_CLASSES_COLUMN] = ""

    # A line intersected exactly at an endpoint has zero ledge length; that is a
    # touch, not a crossing, so it is not "in ledge".
    lines[IN_LEDGE_COLUMN] = lines[LEDGE_LENGTH_COLUMN] > 0
    denominator = lines[LENGTH_COLUMN].replace(0.0, float("nan"))
    pct = lines[LEDGE_LENGTH_COLUMN] / denominator * 100.0
    # Clip to 100: a line lying exactly along a polygon edge can measure a
    # hair over its own length through floating point.
    lines[LEDGE_PCT_COLUMN] = pct.fillna(0.0).clip(upper=100.0)
    return lines, segments, polygons


# --- Summary ----------------------------------------------------------------

def _pct(part: float, whole: float) -> float:
    return round(float(part) / float(whole) * 100.0, 2) if whole else 0.0


def summarize(
    lines: gpd.GeoDataFrame,
    segments: gpd.GeoDataFrame,
    cutoff: pd.Timestamp,
    *,
    ledge_profile: str = "",
    ledge_description: str = "",
    dates: DateResolution | None = None,
    class_field: str = "ledge_class",
) -> tuple[dict[str, Any], pd.DataFrame]:
    """Cross ledge against the cutoff, by count and by length.

    Both measures are reported because they answer different questions and
    routinely disagree: a handful of long recent mains can be a small percentage
    of the count and a large percentage of the footage.
    """
    total_count = int(len(lines))
    total_length = float(lines[LENGTH_COLUMN].sum())
    post = lines[POST_CUTOFF_COLUMN]
    in_ledge = lines[IN_LEDGE_COLUMN]

    post_count = int(post.sum())
    post_length = float(lines.loc[post, LENGTH_COLUMN].sum())
    ledge_length = float(lines[LEDGE_LENGTH_COLUMN].sum())
    ledge_length_post = float(lines.loc[post, LEDGE_LENGTH_COLUMN].sum())
    in_ledge_count = int(in_ledge.sum())
    in_ledge_post_count = int((in_ledge & post).sum())
    in_ledge_length = float(lines.loc[in_ledge, LENGTH_COLUMN].sum())
    in_ledge_post_length = float(lines.loc[in_ledge & post, LENGTH_COLUMN].sum())

    stats: dict[str, Any] = {
        "cutoff_date": cutoff.date().isoformat(),
        "ledge_profile": ledge_profile,
        "ledge_definition": ledge_description,
        "date_fields_used": dates.describe() if dates else "",
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "totals": {
            "line_count": total_count,
            "line_length_ft": round(total_length, 1),
            "line_length_mi": round(total_length / 5280.0, 3),
            "ledge_length_ft": round(ledge_length, 1),
            "ledge_length_mi": round(ledge_length / 5280.0, 3),
            "lines_touching_ledge": in_ledge_count,
        },
        # The headline the request asked for, stated both ways.
        "post_cutoff": {
            "count": post_count,
            "pct_of_lines_by_count": _pct(post_count, total_count),
            "length_ft": round(post_length, 1),
            "length_mi": round(post_length / 5280.0, 3),
            "pct_of_lines_by_length": _pct(post_length, total_length),
        },
        # The same question asked only of what runs through ledge.
        "post_cutoff_in_ledge": {
            "count": in_ledge_post_count,
            "pct_of_ledge_lines_by_count": _pct(in_ledge_post_count, in_ledge_count),
            "ledge_length_ft": round(ledge_length_post, 1),
            "pct_of_ledge_length": _pct(ledge_length_post, ledge_length),
            "line_length_ft": round(in_ledge_post_length, 1),
            "pct_of_ledge_line_length": _pct(in_ledge_post_length, in_ledge_length),
        },
        "ledge_exposure": {
            "pct_of_all_length_in_ledge": _pct(ledge_length, total_length),
            "pct_of_post_cutoff_length_in_ledge": _pct(ledge_length_post, post_length),
            "pct_of_pre_cutoff_length_in_ledge": _pct(
                ledge_length - ledge_length_post, total_length - post_length
            ),
            "pct_of_lines_touching_ledge": _pct(in_ledge_count, total_count),
        },
        "undated_lines": int(lines[EFFECTIVE_DATE_COLUMN].isna().sum())
        if EFFECTIVE_DATE_COLUMN in lines.columns
        else 0,
    }

    stats["crosstab"] = [
        {
            "ledge": ledge_label,
            "age": age_label,
            "count": int(mask.sum()),
            "length_ft": round(float(lines.loc[mask, LENGTH_COLUMN].sum()), 1),
            "pct_of_total_length": _pct(
                float(lines.loc[mask, LENGTH_COLUMN].sum()), total_length
            ),
        }
        for ledge_label, ledge_mask in (
            ("In ledge", in_ledge),
            ("Not in ledge", ~in_ledge),
        )
        for age_label, age_mask in (
            (f"On/after {cutoff.date().isoformat()}", post),
            (f"Before {cutoff.date().isoformat()}", ~post),
        )
        for mask in [ledge_mask & age_mask]
    ]

    stats["headline"] = [
        f"{stats['post_cutoff']['pct_of_lines_by_length']:.2f}% of main line footage "
        f"was installed or created on/after {stats['cutoff_date']} "
        f"({stats['post_cutoff']['length_mi']:,.2f} of "
        f"{stats['totals']['line_length_mi']:,.2f} miles).",
        f"{stats['post_cutoff']['pct_of_lines_by_count']:.2f}% of main line segments "
        f"were installed or created on/after {stats['cutoff_date']} "
        f"({post_count:,} of {total_count:,}).",
        f"{stats['ledge_exposure']['pct_of_all_length_in_ledge']:.2f}% of all main line "
        f"footage runs through ledge.",
        f"Of main line footage in ledge, "
        f"{stats['post_cutoff_in_ledge']['pct_of_ledge_length']:.2f}% was installed or "
        f"created on/after {stats['cutoff_date']}.",
    ]

    by_class = _class_summary(lines, segments, class_field, post, total_length)
    return stats, by_class


def _class_summary(
    lines: gpd.GeoDataFrame,
    segments: gpd.GeoDataFrame,
    class_field: str,
    post: pd.Series,
    total_length: float,
) -> pd.DataFrame:
    """Ledge length by ledge class.

    Classes overlap, so these rows sum to more than the de-duplicated total. That
    is stated in the output rather than hidden by scaling the rows.
    """
    columns = [
        "ledge_class",
        "segments",
        "ledge_length_ft",
        "ledge_length_mi",
        "post_cutoff_length_ft",
        "pct_post_cutoff",
        "pct_of_total_line_length",
    ]
    if segments.empty or class_field not in segments.columns:
        return pd.DataFrame(columns=columns)

    post_uids = set(lines.loc[post, UID_COLUMN])
    working = segments.copy()
    working["is_post_cutoff"] = working[UID_COLUMN].isin(post_uids)
    grouped = working.groupby(class_field, dropna=False)
    rows = []
    for name, group in grouped:
        length = float(group["segment_length_ft"].sum())
        post_length = float(group.loc[group["is_post_cutoff"], "segment_length_ft"].sum())
        rows.append(
            {
                "ledge_class": str(name),
                "segments": int(len(group)),
                "ledge_length_ft": round(length, 1),
                "ledge_length_mi": round(length / 5280.0, 3),
                "post_cutoff_length_ft": round(post_length, 1),
                "pct_post_cutoff": _pct(post_length, length),
                "pct_of_total_line_length": _pct(length, total_length),
            }
        )
    return pd.DataFrame(rows, columns=columns).sort_values(
        "ledge_length_ft", ascending=False, ignore_index=True
    )


def analyze(
    mainlines: gpd.GeoDataFrame,
    ledge: gpd.GeoDataFrame,
    cutoff: str | datetime = "2022-01-01",
    *,
    analysis_crs: str | int | None = None,
    install_fields: Sequence[str] = DEFAULT_INSTALL_FIELDS,
    creation_fields: Sequence[str] = DEFAULT_CREATION_FIELDS,
    class_field: str = "ledge_class",
    ledge_profile: str = "",
    ledge_description: str = "",
) -> LedgeResult:
    """Run the whole analysis and return every piece of it."""
    cutoff_stamp = parse_cutoff(cutoff)
    crs = resolve_analysis_crs(mainlines, ledge, analysis_crs)
    progress(f"Measuring in {CRS.from_user_input(crs).name}")

    lines = ensure_uid(mainlines)
    dates = resolve_date_columns(lines, install_fields, creation_fields)
    progress(f"Date fields: {dates.describe()}")
    lines, warnings = apply_cutoff(lines, dates, cutoff_stamp)

    lines, segments, ledge_projected = intersect_with_ledge(
        lines, ledge, crs, class_field=class_field
    )
    stats, by_class = summarize(
        lines,
        segments,
        cutoff_stamp,
        ledge_profile=ledge_profile,
        ledge_description=ledge_description,
        dates=dates,
        class_field=class_field,
    )
    return LedgeResult(
        mainlines=lines,
        ledge_segments=segments,
        ledge=ledge_projected,
        stats=stats,
        by_class=by_class,
        dates=dates,
        analysis_crs=CRS.from_user_input(crs),
        warnings=warnings,
    )
