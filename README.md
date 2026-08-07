# Estimate_GIS

Estimate_GIS is a small GIS automation repository for querying ArcGIS REST services quickly and exporting the results into GeoPandas-friendly outputs.

## Current layout

```text
.
+- README.md
+- requirements.txt
+- main.py
+- .gitignore
+- scripts
|  +- bootstrap_estimate_gis.py
|  +- export_arcgis_to_geopackage.py
|  +- query_ma_main_lines_2022_plus.py
|  +- mainline_ledge_report.py
|  +- network
|     +- set_zscaler_proxy_environment.py
+- src
   +- __init__.py
   +- arcgis_rest_geopandas.py
   +- auth.py
   +- config.py
   +- enterprise_network.py
   +- ledge_analysis.py
   +- leaflet_viewer.py
   +- massgis_ledge.py
   +- output.py
   +- service_auth.py
   +- vector_source.py
   +- templates
      +- leaflet_viewer.html
```

## Bootstrap

From the repo root:

```powershell
python .\scripts\bootstrap_estimate_gis.py --set-git-proxy --trust-explicit-proxy-success
```

The bootstrap checks Zscaler, sets proxy values for child processes if needed, creates or reuses `.venv`, installs `requirements.txt`, and writes VS Code Python settings.

## Zscaler helper

The network helper lives at:

```text
scripts/network/set_zscaler_proxy_environment.py
```

`ip.zscaler.com` is the primary Zscaler check endpoint. `ip.axaler.com` is kept only as a legacy fallback from earlier troubleshooting and should not be treated as authoritative.

## Fast ArcGIS REST export

The fast REST query logic lives in:

```text
src/arcgis_rest_geopandas.py
```

The export script lives in:

```text
scripts/export_arcgis_to_geopackage.py
```

The query flow mirrors the fast Leak Relocation GeoPandas approach: read layer metadata, request OBJECTIDs for the same WHERE clause, download matching OBJECTIDs in batches, convert Esri JSON geometry into Shapely geometry, and return a GeoDataFrame or GeoPackage.

## Which services get a token

```text
src/service_auth.py
```

Nothing has to be told whether a layer needs signing in to. The host in the URL
decides:

| Host | Token | Sign-in |
| --- | --- | --- |
| `nationalgrid.com` and anything under it | the token from `src/auth.py` | on first use, cached for the run |
| everything else | none, ever | never |

A public service is sent no token even when the caller has one in hand, so our
credentials cannot leave for a host that is not ours by accident. The other
direction fails loudly instead: an internal layer with no token says so before
downloading anything, rather than after a few hundred "Token Required" batches.

The practical effect is that a run touching only public services - MassGIS
ledge, for example - never loads keyring, never opens a browser and needs no
VPN, while the same command against a National Grid layer signs in on its own.

Widen the internal list with an environment variable rather than by editing
code:

```powershell
$env:ESTIMATE_GIS_INTERNAL_HOSTS = "nationalgrid.com,gis.internal.example"
```

Host suffixes match on label boundaries, so `notnationalgrid.com` is not treated
as ours. When a service rejects a token, the error says which side of the line
its host fell on and what to do about it.

## Main lines versus ledge

```text
scripts/mainline_ledge_report.py
```

Intersects a main line layer with MassGIS ledge polygons, reports how much of the
main line footage was installed or created on or after a cutoff date, and writes a
standalone Leaflet viewer with an attribute table.

```powershell
# Everything on defaults: MA main lines, MassGIS ledge, cutoff 2022-01-01
python .\scripts\mainline_ledge_report.py --where "jurisdiction = 'MA'"

# How many main lines would this pull? Answers without downloading anything
python .\scripts\mainline_ledge_report.py --where "citycode = 'WORCESTER'" --count-only

# Scope by extent instead of by attribute (minx,miny,maxx,maxy)
python .\scripts\mainline_ledge_report.py --extent 200000,890000,215000,905000 --extent-crs 26986

# No VPN: run against an export, ledge still comes from the public MassGIS service
python .\scripts\mainline_ledge_report.py --mainlines outputs\mains.gpkg --mainlines-layer mains
```

### What counts as ledge

MassGIS does not publish a layer called "ledge", so it is built from the Surficial
Geology (1:24,000) dataset. `--ledge-profile` chooses how wide the definition is:

| Profile | MassGIS classes | Use when |
| --- | --- | --- |
| `outcrop` | `bk` Bedrock outcrops | Only rock mapped at the surface counts. |
| `standard` (default) | `bk` plus `sb` Areas of abundant outcrop or shallow bedrock | Rock a trench would hit. |
| `broad` | adds `t` Thin till and `ta` Talus deposits | Widest reading; over-reports most. |

The profile used is written into every output, so a number can always be traced
back to the definition that produced it. `--ledge-buffer-ft` grows the polygons
before intersecting, for mapping slop between the 1:24,000 geology and the pipe
centrelines.

### What counts as "installed or created after"

The cutoff is inclusive and defaults to `2022-01-01`. A line counts as post-cutoff
when the later of its installation date and its creation date falls on or after
the cutoff. The fields are looked up in the layer's own metadata rather than
assumed, and named in the output; override the search with `--install-fields` and
`--creation-fields`. Lines with no usable date stay in the denominator and count
as before the cutoff, which is reported rather than hidden.

### Outputs

Written to `--out-dir` (default `outputs`), named from `--basename`:

```text
<basename>.gpkg                  mainlines / ledge / mainline_in_ledge layers
<basename>_summary.json          the full summary
<basename>_summary.csv           the same summary flattened to metric,value
<basename>_crosstab.csv          in-ledge x post-cutoff, by count and by length
<basename>_by_ledge_class.csv    ledge length broken down by geology class
<basename>_mainlines.csv         every main line with its ledge and date columns
<basename>_viewer.html           standalone Leaflet viewer
```

Percentages are reported both by count and by length, because they routinely
disagree: a handful of long recent mains can be a small share of the segments and
a large share of the footage.

## Leaflet viewer

```text
src/leaflet_viewer.py
src/templates/leaflet_viewer.html
```

Builds one HTML file that opens by double-clicking - no server, no Python, no
ArcGIS licence at the other end. It knows nothing about ledge or main lines, so
any GeoDataFrame can be handed to it:

```python
import leaflet_viewer

leaflet_viewer.build_viewer(
    "outputs/viewer.html",
    [leaflet_viewer.make_layer(gdf, "Anything")],
    title="Anything",
    stats={"headline": ["whatever needs saying"], "totals": {"features": len(gdf)}},
)
```

The attribute table builds its columns from whatever properties the features
carry: sortable headers, search across all columns, a "only features in the
current map view" filter, a column chooser, CSV export of the filtered rows, and
click-through both ways between a table row and the feature on the map. Basemaps,
a legend and a summary panel come from the same call.

Useful flags on the report script:

* `--inline-leaflet` embeds Leaflet in the HTML, so the viewer works with no internet.
* `--external-data` writes the payload to a sidecar `.js` beside the HTML.
* `--max-viewer-features` caps what the browser is asked to draw (default 40000).
  The summary numbers are always computed from the full set, and any cap is stated
  in the viewer.
* `--simplify-ft` shrinks the file by simplifying geometry for display only.

## Reading a layer from anywhere

```text
src/vector_source.py
```

`read_source` takes an ArcGIS layer URL or any file GeoPandas reads, so the same
command works against the live service on a National Grid workstation and against
an exported GeoPackage on a machine with no VPN. Signing in is not the caller's
problem either - see "Which services get a token" above.

## Local generated files

Do not commit `.venv`, `.env`, `__pycache__`, `.pyc`, build outputs, logs, or GeoPackage sidecar files.
