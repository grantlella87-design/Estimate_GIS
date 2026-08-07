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
|  +- fetch_massgis_ledge.py
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

### When the ledge is not working

Prove the ledge half on its own first. It needs no sign-in, no VPN and no
National Grid layer, so if this works the problem is elsewhere:

```powershell
python .\scripts\fetch_massgis_ledge.py --out outputs\ledge.gpkg --self-test
```

#### If the MassGIS host is blocked

`ConnectionResetError 10054` on the first request is the network refusing the
connection, not the service answering. National Grid hosts keep working because
interception for them is transparent and needs no proxy, so only the public half
fails. Two ways through, and the run now takes the second one on its own:

**Point requests at the proxy Windows already uses.** `requests` reads proxies
only from environment variables; Windows keeps its in the registry. Startup now
bridges the two automatically and excludes National Grid hosts so the working
half stays direct. Override it when the detection is wrong:

```powershell
$env:ESTIMATE_GIS_PROXY_URL = "http://your.zscaler.proxy:80"
```

**Or take the ledge from ArcGIS Online.** MassGIS publishes a copy on
`services1.arcgis.com`, which corporate networks almost always allow. If the
MassGIS server cannot be reached the report falls back to it automatically,
says so, and labels every output with which source produced the numbers:

```powershell
python .\scripts\mainline_ledge_report.py --ledge agol      # ask for it directly
python .\scripts\fetch_massgis_ledge.py --agol --self-test  # test it on its own
python .\scripts\mainline_ledge_report.py --no-agol-fallback  # fail instead
```

**It is not the same data.** The AGOL copy is 1:250,000, roughly a tenth the
detail of the 1:24,000 service, and its only relevant class is `Till or Bedrock`
- till and rock in one polygon, where the 24k separates them. On the same test
area it reported 59% of footage in ledge against 8.7% from the 24k data. Use it
to keep working and to scope a job. Do not price one from it.

That pulls a small area of Worcester known to have ledge and reports what came
back. If it fails, the output names the things to check in the order they
usually go wrong. If it works, download what you need once and hand the file to
the report, which then never touches MassGIS:

```powershell
python .\scripts\fetch_massgis_ledge.py --out outputs\ledge.gpkg
python .\scripts\mainline_ledge_report.py --ledge outputs\ledge.gpkg
```

The report caches ledge per profile and extent under `<out-dir>/ledge_cache`, so
only the first run of an area downloads anything. `--refresh-ledge-cache` forces
a re-download; `--no-ledge-cache` turns it off.

Two failures used to be silent and are not any more:

* **An extent that misses Massachusetts.** Almost always a CRS that was guessed
  rather than read. The run now prints the lookup extent every time and says so
  outright when it cannot overlap the data.
* **No ledge found.** The report stops instead of writing a page of zeros, since
  zeros read as an answer. `--allow-empty-ledge` reports them anyway.

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
