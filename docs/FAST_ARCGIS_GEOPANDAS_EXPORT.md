
# Estimate_GIS fast ArcGIS REST GeoPandas export

This patch adds a fast ArcGIS REST query path modeled after `leak_relocation_geopandas.py`.

## What changed

- Reads MapServer layer metadata before downloading data.
- Uses `returnIdsOnly=true` for the exact requested `WHERE` clause.
- Downloads only those OBJECTIDs in threaded batches.
- Converts Esri JSON geometries to Shapely geometry.
- Returns a GeoDataFrame.
- Can export directly to GeoPackage.

## Example

```powershell
$env:ARCGIS_TOKEN = '<portal token if required>'
python scripts/export_arcgis_to_geopackage.py `
  --layer-url "https://server/arcgis/rest/services/Folder/Service/MapServer/0" `
  --where "1=1" `
  --out-fields "*" `
  --out-gpkg "$env:USERPROFILE\Downloads\estimate_gis_export.gpkg" `
  --layer-name estimate_gis_export
```

The same `WHERE` clause is used to select OBJECTIDs and to download those features, so the query does not pull unrelated OBJECTIDs from a different request scope.
