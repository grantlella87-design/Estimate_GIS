# Repository structure

The repository is intentionally organized with minimal root clutter.

Root-level files should be limited to repository-level files such as:

- `requirements.txt`
- `.gitignore`
- `README.md` if present

Folders:

- `src/` contains importable Python modules directly. There is no nested `src/estimate_gis/` package folder.
- `scripts/` contains runnable entry-point scripts and bootstrap utilities.
- `scripts/network/` contains network/proxy helpers, including the Zscaler helper.
- `docs/` contains project documentation.

Current bootstrap entry point:

```powershell
python .\scripts\bootstrap_estimate_gis.py --set-git-proxy --trust-explicit-proxy-success
```
