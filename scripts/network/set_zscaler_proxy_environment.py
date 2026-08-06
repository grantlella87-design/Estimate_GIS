"""
Check whether Zscaler appears active and set proxy environment variables.

Python cannot change the already-running parent PowerShell environment.
Use --scope user or --scope both to persist Windows user variables for new terminals.
Use --print-powershell to print commands for the current PowerShell session.
"""
from __future__ import annotations
import argparse
import os
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from typing import Iterable

DEFAULT_PROXY_URL = "http://zscaler.nationalgrid.com:80"
PRIMARY_ZSCALER_CHECK_URL = "http://ip.zscaler.com"
LEGACY_FALLBACK_CHECK_URL = "http://ip.axaler.com"
ZSCALER_CHECK_URLS = [PRIMARY_ZSCALER_CHECK_URL, LEGACY_FALLBACK_CHECK_URL]
PROXY_ENV_NAMES = ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]
ZSCALER_MARKERS = ["zscaler", "zscloud", "gateway", "proxy", "zia", "cloud security", "zsc"]

@dataclass
class CheckResult:
    url: str
    used_explicit_proxy: bool
    success: bool
    status_code: int | None
    looks_active: bool
    summary: str
    error: str

def log(message: str) -> None:
    print(message, flush=True)

def first_lines(text: str, limit: int = 20) -> str:
    return "\n".join(text.splitlines()[:limit])

def build_opener(proxy_url: str | None) -> urllib.request.OpenerDirector:
    if proxy_url:
        return urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
    return urllib.request.build_opener()

def check_url(url: str, proxy_url: str, use_explicit_proxy: bool, timeout_seconds: int) -> CheckResult:
    opener = build_opener(proxy_url if use_explicit_proxy else None)
    request = urllib.request.Request(url, headers={"User-Agent": "estimate-gis-zscaler-proxy-check/1.0"})
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            content = response.read(25000).decode("utf-8", errors="replace")
            lower = content.lower()
            looks_active = any(marker in lower for marker in ZSCALER_MARKERS)
            return CheckResult(url, use_explicit_proxy, True, int(response.status), looks_active, first_lines(content), "")
    except Exception as exc:
        return CheckResult(url, use_explicit_proxy, False, None, False, "", str(exc))

def print_check_result(result: CheckResult) -> None:
    mode = "proxy" if result.used_explicit_proxy else "direct"
    if result.success:
        log(f"{mode} status from {result.url}: HTTP {result.status_code}")
        if result.summary:
            log(result.summary)
    else:
        log(f"WARNING: {mode} check failed for {result.url}: {result.error}")

def check_zscaler_active(proxy_url: str, timeout_seconds: int) -> tuple[bool, list[CheckResult]]:
    log("==> Checking Zscaler status")
    results: list[CheckResult] = []
    for url in ZSCALER_CHECK_URLS:
        log(f"Checking direct: {url}")
        direct = check_url(url, proxy_url, False, timeout_seconds)
        results.append(direct)
        print_check_result(direct)
        log(f"Checking through explicit proxy {proxy_url}: {url}")
        proxied = check_url(url, proxy_url, True, timeout_seconds)
        results.append(proxied)
        print_check_result(proxied)
    active = any(result.success and result.looks_active for result in results)
    return active, results

def set_process_environment(proxy_url: str) -> None:
    log("==> Setting process environment variables")
    for name in PROXY_ENV_NAMES:
        os.environ[name] = proxy_url
        log(f"Process {name}={proxy_url}")

def clear_process_environment() -> None:
    log("==> Clearing process environment variables")
    for name in PROXY_ENV_NAMES:
        os.environ.pop(name, None)
        log(f"Cleared process {name}")

def set_user_environment(proxy_url: str) -> None:
    if os.name != "nt":
        log("WARNING: User environment persistence is implemented for Windows only.")
        return
    log("==> Setting Windows user environment variables")
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment", 0, winreg.KEY_SET_VALUE) as key:
        for name in PROXY_ENV_NAMES:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, proxy_url)
            log(f"User {name}={proxy_url}")
    broadcast_environment_change()

def clear_user_environment() -> None:
    if os.name != "nt":
        log("WARNING: User environment persistence is implemented for Windows only.")
        return
    log("==> Clearing Windows user environment variables")
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment", 0, winreg.KEY_SET_VALUE) as key:
        for name in PROXY_ENV_NAMES:
            try:
                winreg.DeleteValue(key, name)
                log(f"Cleared user {name}")
            except FileNotFoundError:
                log(f"User {name} was not set")
    broadcast_environment_change()

def broadcast_environment_change() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        hwnd_broadcast = 0xFFFF
        wm_settingchange = 0x001A
        smto_abortifhung = 0x0002
        result = ctypes.c_ulong()
        ctypes.windll.user32.SendMessageTimeoutW(hwnd_broadcast, wm_settingchange, 0, "Environment", smto_abortifhung, 5000, ctypes.byref(result))
        log("Broadcasted Windows Environment setting change.")
    except Exception as exc:
        log(f"WARNING: Could not broadcast environment change: {exc}")

def run_command(args: Iterable[str], allow_failure: bool = False) -> int:
    command = list(args)
    log(" ".join(command))
    completed = subprocess.run(command, text=True, capture_output=True)
    if completed.stdout:
        log(completed.stdout.rstrip())
    if completed.stderr:
        log(completed.stderr.rstrip())
    if completed.returncode != 0 and not allow_failure:
        raise RuntimeError(f"Command failed with exit code {completed.returncode}: {' '.join(command)}")
    return completed.returncode

def set_git_proxy(proxy_url: str) -> None:
    log("==> Setting Git proxy configuration")
    run_command(["git", "config", "--global", "http.sslBackend", "schannel"], allow_failure=True)
    run_command(["git", "config", "--global", "http.proxy", proxy_url], allow_failure=True)
    run_command(["git", "config", "--global", "https.proxy", proxy_url], allow_failure=True)
    run_command(["git", "config", "--global", "http.https://github.com.proxy", proxy_url], allow_failure=True)

def clear_git_proxy() -> None:
    log("==> Clearing Git proxy configuration")
    run_command(["git", "config", "--global", "--unset-all", "http.proxy"], allow_failure=True)
    run_command(["git", "config", "--global", "--unset-all", "https.proxy"], allow_failure=True)
    run_command(["git", "config", "--global", "--unset-all", "http.https://github.com.proxy"], allow_failure=True)
    run_command(["git", "config", "--global", "http.sslBackend", "schannel"], allow_failure=True)

def print_powershell_commands(proxy_url: str) -> None:
    log("==> PowerShell commands for current session")
    for name in PROXY_ENV_NAMES:
        log(f'$env:{name} = "{proxy_url}"')

def copy_log_to_clipboard(text: str) -> None:
    if os.name != "nt":
        return
    try:
        subprocess.run(["clip"], input=text, text=True, check=False)
    except Exception:
        pass

def apply_proxy(scope: str, proxy_url: str, set_git: bool) -> None:
    if scope in {"process", "both"}:
        set_process_environment(proxy_url)
    if scope in {"user", "both"}:
        set_user_environment(proxy_url)
    if set_git:
        set_git_proxy(proxy_url)

def clear_proxy(scope: str, set_git: bool) -> None:
    if scope in {"process", "both"}:
        clear_process_environment()
    if scope in {"user", "both"}:
        clear_user_environment()
    if set_git:
        clear_git_proxy()

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check Zscaler and set proxy environment variables.")
    parser.add_argument("--proxy-url", default=DEFAULT_PROXY_URL)
    parser.add_argument("--scope", choices=["process", "user", "both"], default="process")
    parser.add_argument("--set-git-proxy", action="store_true")
    parser.add_argument("--clear-when-inactive", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--print-powershell", action="store_true")
    return parser.parse_args()

def main() -> int:
    args = parse_args()
    captured_lines: list[str] = []
    original_stdout = sys.stdout
    class Tee:
        def write(self, value: str) -> int:
            captured_lines.append(value)
            return original_stdout.write(value)
        def flush(self) -> None:
            original_stdout.flush()
    sys.stdout = Tee()
    try:
        log("==> Starting Zscaler proxy environment setup")
        log(f"ProxyUrl: {args.proxy_url}")
        log(f"Scope: {args.scope}")
        active = True if args.force else check_zscaler_active(args.proxy_url, args.timeout)[0]
        if active:
            log("==> Zscaler appears active. Applying proxy settings.")
            apply_proxy(args.scope, args.proxy_url, args.set_git_proxy)
            if args.print_powershell:
                print_powershell_commands(args.proxy_url)
        else:
            log("WARNING: Zscaler did not appear active from the check sites.")
            if args.clear_when_inactive:
                clear_proxy(args.scope, args.set_git_proxy)
            else:
                log("No environment variables were changed. Use --clear-when-inactive if you want them removed when inactive.")
        log("==> Result")
        log(f"ZscalerActive={active}")
        log(f"HTTP_PROXY={os.environ.get('HTTP_PROXY', '')}")
        log(f"HTTPS_PROXY={os.environ.get('HTTPS_PROXY', '')}")
        return 0
    except Exception as exc:
        log(f"ERROR: {exc}")
        return 1
    finally:
        sys.stdout = original_stdout
        copy_log_to_clipboard("".join(captured_lines))

if __name__ == "__main__":
    raise SystemExit(main())
