"""
Bootstrap Estimate_GIS for VS Code.

What it does:
- Checks Zscaler using ip.zscaler.com as the primary endpoint.
- Sets proxy variables for this bootstrap process if Zscaler appears active.
- Creates or reuses .venv.
- Upgrades pip.
- Installs packages from requirements.txt.
- Writes VS Code Python interpreter and terminal environment settings.
- Writes .env for Python/debugger environment loading.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import urllib.error
import urllib.request
import venv
from pathlib import Path
from typing import Any

DEFAULT_PROXY_URL = "http://zscaler.nationalgrid.com:80"
PRIMARY_ZSCALER_CHECK_URL = "http://ip.zscaler.com"
LEGACY_FALLBACK_CHECK_URL = "http://ip.axaler.com"
PROXY_ENV_NAMES = ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]
POSITIVE_ZSCALER_MARKERS = ["zscaler cloud security", "zscaler", "zscloud", "gateway ip address", "proxy vip", "cloud security"]
NEGATIVE_ZSCALER_MARKERS = ["didn't come from a zscaler ip", "did not come from a zscaler ip", "not going through the zscaler proxy", "not traversing a zscaler proxy"]

def log(message: str) -> None:
    print(message, flush=True)

def read_url(url: str, proxy_url: str | None, timeout: int) -> tuple[bool, str, str]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})) if proxy_url else urllib.request.build_opener()
    request = urllib.request.Request(url, headers={"User-Agent": "estimate-gis-bootstrap/1.0"})
    try:
        with opener.open(request, timeout=timeout) as response:
            text = response.read(50000).decode("utf-8", errors="replace")
            return True, text, ""
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        return False, "", str(exc)

def looks_like_active_zscaler_page(text: str) -> bool:
    lower = text.lower()
    positive = any(marker in lower for marker in POSITIVE_ZSCALER_MARKERS)
    negative = any(marker in lower for marker in NEGATIVE_ZSCALER_MARKERS)
    return positive and not negative

def check_zscaler(proxy_url: str, timeout: int, trust_proxy_success: bool) -> bool:
    log("==> Checking Zscaler status")
    checks = [(PRIMARY_ZSCALER_CHECK_URL, None), (PRIMARY_ZSCALER_CHECK_URL, proxy_url), (LEGACY_FALLBACK_CHECK_URL, None), (LEGACY_FALLBACK_CHECK_URL, proxy_url)]
    active = False
    for url, proxy in checks:
        mode = "explicit proxy" if proxy else "direct"
        log(f"Checking {mode}: {url}")
        ok, text, error = read_url(url, proxy, timeout)
        if not ok:
            log(f"WARNING: {mode} check failed for {url}: {error}")
            continue
        preview = "\n".join(text.splitlines()[:8])
        log(f"{mode} check succeeded for {url}")
        if preview:
            log(preview)
        if looks_like_active_zscaler_page(text) or (proxy and trust_proxy_success):
            active = True
    log(f"ZscalerActive={active}")
    return active

def set_proxy_env(proxy_url: str, env: dict[str, str]) -> None:
    log("==> Setting proxy variables for bootstrap child processes")
    for name in PROXY_ENV_NAMES:
        env[name] = proxy_url
        os.environ[name] = proxy_url
        log(f"{name}={proxy_url}")

def clear_proxy_env(env: dict[str, str]) -> None:
    for name in PROXY_ENV_NAMES:
        env.pop(name, None)

def venv_python_path(repo_root: Path, venv_dir: str) -> Path:
    base = repo_root / venv_dir
    if os.name == "nt":
        return base / "Scripts" / "python.exe"
    return base / "bin" / "python"

def create_venv(repo_root: Path, venv_dir: str) -> Path:
    python_path = venv_python_path(repo_root, venv_dir)
    if python_path.exists():
        log(f"==> Reusing virtual environment: {python_path}")
        return python_path
    log(f"==> Creating virtual environment: {repo_root / venv_dir}")
    venv.EnvBuilder(with_pip=True, clear=False, upgrade=False).create(repo_root / venv_dir)
    if not python_path.exists():
        raise RuntimeError(f"Virtual environment python was not created: {python_path}")
    return python_path

def run_command(command: list[str], cwd: Path, env: dict[str, str]) -> None:
    log(" ".join(command))
    completed = subprocess.run(command, cwd=str(cwd), env=env, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"Command failed with exit code {completed.returncode}: {' '.join(command)}")

def install_requirements(repo_root: Path, python_path: Path, requirements_path: Path, env: dict[str, str]) -> None:
    log("==> Upgrading pip")
    run_command([str(python_path), "-m", "pip", "install", "--upgrade", "pip"], repo_root, env)
    if not requirements_path.exists():
        log(f"WARNING: requirements file not found: {requirements_path}")
        return
    log(f"==> Installing requirements from {requirements_path}")
    run_command([str(python_path), "-m", "pip", "install", "-r", str(requirements_path)], repo_root, env)

def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        backup = path.with_suffix(path.suffix + ".bak")
        backup.write_text(path.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
        log(f"WARNING: Existing JSON was invalid. Backed up to {backup}")
        return {}

def vscode_interpreter_value(venv_dir: str) -> str:
    if os.name == "nt":
        return f"${{workspaceFolder}}\\{venv_dir}\\Scripts\\python.exe"
    return f"${{workspaceFolder}}/{venv_dir}/bin/python"

def update_vscode_settings(repo_root: Path, venv_dir: str, zscaler_active: bool, proxy_url: str) -> None:
    log("==> Updating VS Code settings")
    vscode_dir = repo_root / ".vscode"
    vscode_dir.mkdir(parents=True, exist_ok=True)
    settings_path = vscode_dir / "settings.json"
    settings = load_json(settings_path)
    settings["python.defaultInterpreterPath"] = vscode_interpreter_value(venv_dir)
    settings["python.terminal.activateEnvironment"] = True
    settings["python.envFile"] = "${workspaceFolder}/.env"
    if zscaler_active:
        settings.setdefault("terminal.integrated.env.windows", {})
        settings.setdefault("terminal.integrated.env.osx", {})
        settings.setdefault("terminal.integrated.env.linux", {})
        for env_block in [settings["terminal.integrated.env.windows"], settings["terminal.integrated.env.osx"], settings["terminal.integrated.env.linux"]]:
            for name in PROXY_ENV_NAMES:
                env_block[name] = proxy_url
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    log(f"Wrote {settings_path}")

def update_env_file(repo_root: Path, zscaler_active: bool, proxy_url: str) -> None:
    log("==> Updating .env")
    env_path = repo_root / ".env"
    existing_lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    filtered = [line for line in existing_lines if not any(line.startswith(f"{name}=") for name in PROXY_ENV_NAMES)]
    if zscaler_active:
        filtered.extend([f"{name}={proxy_url}" for name in PROXY_ENV_NAMES])
    env_path.write_text("\n".join(filtered).rstrip() + "\n", encoding="utf-8")
    log(f"Wrote {env_path}")

def update_gitignore(repo_root: Path, venv_dir: str) -> None:
    log("==> Updating .gitignore")
    gitignore = repo_root / ".gitignore"
    lines = gitignore.read_text(encoding="utf-8").splitlines() if gitignore.exists() else []
    needed = [venv_dir + "/", ".env", "__pycache__/", "*.pyc"]
    for item in needed:
        if item not in lines:
            lines.append(item)
    gitignore.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    log(f"Wrote {gitignore}")

def set_git_proxy(repo_root: Path, proxy_url: str, zscaler_active: bool) -> None:
    log("==> Updating local Git proxy config")
    if zscaler_active:
        run_command(["git", "config", "http.sslBackend", "schannel"], repo_root, os.environ.copy())
        run_command(["git", "config", "http.proxy", proxy_url], repo_root, os.environ.copy())
        run_command(["git", "config", "https.proxy", proxy_url], repo_root, os.environ.copy())
    else:
        subprocess.run(["git", "config", "--unset-all", "http.proxy"], cwd=str(repo_root), text=True, capture_output=True, check=False)
        subprocess.run(["git", "config", "--unset-all", "https.proxy"], cwd=str(repo_root), text=True, capture_output=True, check=False)

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bootstrap Estimate_GIS Python, pip requirements, VS Code, and Zscaler proxy settings.")
    parser.add_argument("--repo-root", default=None, help="Repo root. Defaults to the current working directory.")
    parser.add_argument("--requirements", default="requirements.txt")
    parser.add_argument("--venv", default=".venv")
    parser.add_argument("--proxy-url", default=DEFAULT_PROXY_URL)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--force-proxy", action="store_true")
    parser.add_argument("--trust-explicit-proxy-success", action="store_true")
    parser.add_argument("--set-git-proxy", action="store_true")
    parser.add_argument("--skip-install", action="store_true")
    return parser.parse_args()

def main() -> int:
    args = parse_args()
    repo_root = Path(args.repo_root).resolve() if args.repo_root else Path(__file__).resolve().parents[1]
    requirements_path = (repo_root / args.requirements).resolve()
    log(f"RepoRoot={repo_root}")
    log(f"Requirements={requirements_path}")
    env = os.environ.copy()
    zscaler_active = True if args.force_proxy else check_zscaler(args.proxy_url, args.timeout, args.trust_explicit_proxy_success)
    if zscaler_active:
        set_proxy_env(args.proxy_url, env)
    else:
        clear_proxy_env(env)
    python_path = create_venv(repo_root, args.venv)
    if not args.skip_install:
        install_requirements(repo_root, python_path, requirements_path, env)
    update_vscode_settings(repo_root, args.venv, zscaler_active, args.proxy_url)
    update_env_file(repo_root, zscaler_active, args.proxy_url)
    update_gitignore(repo_root, args.venv)
    if args.set_git_proxy:
        set_git_proxy(repo_root, args.proxy_url, zscaler_active)
    log("==> Bootstrap complete")
    log(f"Python interpreter: {python_path}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

