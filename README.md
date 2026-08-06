# Estimate_GIS

Estimate_GIS is a small GIS automation repository for querying ArcGIS REST services quickly and exporting the results into GeoPandas-friendly outputs.

## Current layout

```text
.
â”œâ”€ README.md
â”œâ”€ requirements.txt
â”œâ”€ .gitignore
â”œâ”€ scripts
â”‚  â”œâ”€ bootstrap_estimate_gis.py
â”‚  â”œâ”€ export_arcgis_to_geopackage.py
â”‚  â”œâ”€ query_ma_main_lines_2022_plus.py
â”‚  â””â”€ network
â”‚     â””â”€ set_zscaler_proxy_environment.py
â””â”€ src
   â”œâ”€ __init__.py
   â”œâ”€ arcgis_rest_geopandas.py
   â”œâ”€ auth.py
   â”œâ”€ config.py
   â””â”€ output.py
```

## Bootstrap

From the repo root:

```powershell
python .\scriptsootstrap_estimate_gis.py --set-git-proxy --trust-explicit-proxy-success
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

## Local generated files

Do not commit `.venv`, `.env`, `__pycache__`, `.pyc`, build outputs, logs, or GeoPackage sidecar files.
