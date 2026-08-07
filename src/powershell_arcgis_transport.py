from __future__ import annotations
import json
import os
import subprocess
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any

_HOSTS = {'arcgisserver.digital.mass.gov'}

def _enabled(url: str) -> bool:
    if os.environ.get('ESTIMATE_GIS_DISABLE_POWERSHELL_TRANSPORT', '').lower() in {'1', 'true', 'yes'}:
        return False
    return urllib.parse.urlparse(str(url)).netloc.lower() in _HOSTS

def _append_params(url: str, params: Any) -> str:
    if not params:
        return str(url)
    query = params if isinstance(params, str) else urllib.parse.urlencode(params, doseq=True)
    return str(url) + ('&' if '?' in str(url) else '?') + query

def _invoke_powershell(requests_module: Any, method: str, url: str, **kwargs: Any):
    if (method or 'GET').upper() != 'GET':
        raise RuntimeError('PowerShell fallback only supports GET')
    full_url = _append_params(url, kwargs.get('params'))
    headers = dict(kwargs.get('headers') or {})
    headers.setdefault('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)')
    headers.setdefault('Accept', 'application/json,text/plain,*/*')
    timeout = int(kwargs.get('timeout') or 180)
    spec = {'url': full_url, 'headers': headers, 'timeout': timeout}
    with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8') as f:
        json.dump(spec, f)
        spec_path = f.name
    ps_lines = [
        "$ErrorActionPreference = 'Stop'",
        "$spec = Get-Content -Raw -LiteralPath $args[0] | ConvertFrom-Json",
        "$headers = @{}",
        "foreach ($p in $spec.headers.PSObject.Properties) { $headers[$p.Name] = [string]$p.Value }",
        "$r = Invoke-RestMethod -Uri ([string]$spec.url) -Method Get -Headers $headers -TimeoutSec ([int]$spec.timeout)",
        "$r | ConvertTo-Json -Depth 100 -Compress",
    ]
    ps = '\n'.join(ps_lines)
    try:
        proc = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', ps, spec_path], capture_output=True, text=True, timeout=timeout + 30)
    finally:
        try:
            Path(spec_path).unlink(missing_ok=True)
        except Exception:
            pass
    response = requests_module.Response()
    response.url = full_url
    response.encoding = 'utf-8'
    response.headers['Content-Type'] = 'application/json'
    response.status_code = 200 if proc.returncode == 0 else 599
    response._content = (proc.stdout if proc.returncode == 0 else (proc.stderr or proc.stdout)).encode('utf-8', errors='replace')
    return response

def install_powershell_transport_fallback(requests_module: Any) -> None:
    if getattr(requests_module, '_estimate_gis_ps_transport', False):
        return
    original = requests_module.sessions.Session.request
    def patched(self, method, url, **kwargs):
        if _enabled(str(url)):
            return _invoke_powershell(requests_module, method, str(url), **kwargs)
        return original(self, method, url, **kwargs)
    requests_module.sessions.Session.request = patched
    requests_module._estimate_gis_ps_transport = True
