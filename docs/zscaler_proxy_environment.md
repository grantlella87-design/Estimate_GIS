# Zscaler proxy environment helper

This repository includes `scripts/network/set_zscaler_proxy_environment.py` to check whether Zscaler appears active and then set proxy-related environment variables.

Primary check URL:

- `http://ip.zscaler.com`

Notes on `ip.axaler.com`:

- `ip.axaler.com` is retained only as a legacy/fallback check name from prior troubleshooting.
- Treat `ip.zscaler.com` as the authoritative Zscaler check endpoint.
- If `ip.axaler.com` closes the connection or fails, that does not by itself mean Zscaler is inactive.
- The script marks Zscaler active only when a response body looks like a Zscaler status page.

Default proxy used by the script:

- `http://zscaler.nationalgrid.com:80`

Example current-process run:

```powershell
python scripts\network\set_zscaler_proxy_environment.py
```

Example persistent user environment plus Git proxy:

```powershell
python scripts\network\set_zscaler_proxy_environment.py --scope user --set-git-proxy
```

Example print commands for the current PowerShell session:

```powershell
python scripts\network\set_zscaler_proxy_environment.py --print-powershell
```

Python limitation: a Python process cannot directly modify the already-running parent PowerShell session environment. Use `--scope user` for new terminals/processes or `--print-powershell` for commands to paste into the current shell.
