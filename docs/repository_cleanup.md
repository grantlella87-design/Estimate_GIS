# Repository cleanup notes

This repository keeps source code and project documentation in Git while excluding local generated files.

Ignored local artifacts include:

- `.venv/` and other virtual environments
- `.env` files
- Python bytecode and cache folders
- build/dist outputs
- test and coverage caches
- local Git metadata backups created during repair
- GeoPackage sidecar lock/write-ahead files

Important source folders intentionally kept:

- `src/`
- `scripts/`
- `docs/`
- `.git/`

The bootstrap entry point is `bootstrap_estimate_gis.py`.
The Zscaler helper is `scripts/network/set_zscaler_proxy_environment.py`.
