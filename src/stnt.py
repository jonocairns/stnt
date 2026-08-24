#!/usr/bin/env python3
"""Atomic host-side lifecycle for Stnt Docker Sandbox sessions."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
import platform
import re
import select
import shutil
import signal
import socket
import ssl
import stat
import subprocess
import sys
import tarfile
import tempfile
import termios
import threading
import time
import tty
import urllib.request
import uuid
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from contextlib import ExitStack, contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence
from urllib.parse import quote, urlsplit


ROOT = Path(__file__).resolve().parent.parent
RUNTIME = Path(os.environ.get("STNT_RUNTIME", ROOT / "bin/docker-sandbox"))
THREADS = Path(os.environ.get("STNT_THREAD_ADAPTER", ROOT / "bin/amp-thread"))
FINISH = Path(os.environ.get("STNT_FINISH", ROOT / "bin/phase0b-finish"))
STACK_FINISH = Path(os.environ.get("STNT_STACK_FINISH", ROOT / "bin/stack-finish"))
THREAD_RE = re.compile(r"^T-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
WORKSPACE_RE = re.compile(r"^W-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
EDITOR_ALIAS_RE = re.compile(r"^[wt][0-9a-f]{32}-[0-9a-f]{12}\.stnt\.sbx$")
SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSION = 1
INTERNAL_PORT = 8000
SUPPORTED_SBX_VERSION = "v0.38.0"
SERVICE_HEALTH_TIMEOUT_SECONDS = 15.0
KIT_SPEC = ROOT / "assets/amp-kit/spec.yaml"
PROFILE_SCHEMA_VERSION = 1
STACK_SCHEMA_VERSION = 1
STACK_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
STACK_INGRESS_PORT = 8000
STACK_HTTP_PORT = 4000
STACK_WEBSOCKET_PORT = 4500
NIX_VERSION = "2.32.3"
NIX_AARCH64_LINUX_SHA256 = "9f4e5ea6b5259e651d727646172f53c726ebf9b0f708dfb68ed2023698dd00a3"
NIX_AARCH64_LINUX_URL = f"https://releases.nixos.org/nix/nix-{NIX_VERSION}/nix-{NIX_VERSION}-aarch64-linux.tar.xz"
NIX_BOOTSTRAP_DOMAINS = [
    "releases.nixos.org:443",
    "cache.nixos.org:443",
]
GITHUB_FLAKE_DOMAINS = [
    "github.com:443",
    "api.github.com:443",
    "codeload.github.com:443",
]
GITHUB_PUSH_CAPABILITY = {
    "name": "github-read-write",
    "provider": "docker-sandboxes",
    "providerReference": "github",
    "consumer": "git",
    "lifetime": "workspace",
}
PACKAGE_MANAGER_DOMAINS = {
    "yarn": ["repo.yarnpkg.com:443", "registry.yarnpkg.com:443"],
    "pnpm": ["registry.npmjs.org:443"],
    "npm": ["registry.npmjs.org:443"],
}


class TimingRecorder:
    """One command's fixed-schema, secret-free lifecycle timings."""

    def __init__(self, path: Path, operation: str):
        self.path = path
        self.operation = operation
        self.started_wall = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self.started = time.monotonic()
        self.stages: Dict[str, int] = {}
        self.milestones: Dict[str, int] = {}

    @staticmethod
    def milliseconds(seconds: float) -> int:
        return round(seconds * 1000)

    @contextmanager
    def stage(self, name: str, progress: Optional[str] = None) -> Iterator[None]:
        if name in self.stages:
            raise StntError(f"duplicate timing stage: {name}")
        started = time.monotonic()
        try:
            yield
        finally:
            self.stages[name] = self.milliseconds(time.monotonic() - started)

    def milestone(self, name: str) -> None:
        if name in self.milestones:
            raise StntError(f"duplicate timing milestone: {name}")
        self.milestones[name] = self.milliseconds(time.monotonic() - self.started)

    def write(self, outcome: str) -> None:
        if self.path.is_symlink():
            raise StntError(f"timing output must not be a symbolic link: {self.path}")
        payload = {
            "schemaVersion": 1,
            "operation": self.operation,
            "startedAt": self.started_wall,
            "outcome": outcome,
            "durationMs": self.milliseconds(time.monotonic() - self.started),
            "stagesMs": self.stages,
            "milestonesMs": self.milestones,
        }
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.path, flags, 0o600)
        try:
            os.write(descriptor, (json.dumps(payload, sort_keys=True) + "\n").encode())
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


TIMING: Optional[TimingRecorder] = None
VERBOSE = False
ACTIVE_PROGRESS: Optional["ProgressIndicator"] = None


class ProgressIndicator:
    FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
    INTERVAL_SECONDS = 0.1

    def __init__(self, message: str):
        self.message = message
        self.started = time.monotonic()
        self.interactive = sys.stderr.isatty() and not VERBOSE
        self.stopped = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.finished = False

    def _line(self, symbol: str) -> str:
        elapsed = time.monotonic() - self.started
        return f"{symbol} stnt: {self.message} ({elapsed:.1f}s)"

    def _animate(self) -> None:
        index = 0
        while not self.stopped.wait(self.INTERVAL_SECONDS):
            print(f"\r\033[2K{self._line(self.FRAMES[index])}", end="", file=sys.stderr, flush=True)
            index = (index + 1) % len(self.FRAMES)

    def start(self) -> None:
        if not self.interactive:
            print(f"stnt: {self.message}...", file=sys.stderr)
            return
        print(f"\r\033[2K{self._line(self.FRAMES[0])}", end="", file=sys.stderr, flush=True)
        self.thread = threading.Thread(target=self._animate, name="stnt-progress", daemon=True)
        self.thread.start()

    def finish(self, success: bool) -> None:
        if self.finished:
            return
        self.finished = True
        self.stopped.set()
        if self.thread is not None:
            self.thread.join()
        if self.interactive:
            symbol = "✓" if success else "✗"
            print(f"\r\033[2K{self._line(symbol)}", file=sys.stderr, flush=True)


@contextmanager
def progress_indicator(message: Optional[str]) -> Iterator[None]:
    global ACTIVE_PROGRESS
    if not message:
        yield
        return
    indicator = ProgressIndicator(message)
    previous = ACTIVE_PROGRESS
    ACTIVE_PROGRESS = indicator
    indicator.start()
    try:
        yield
    except BaseException:
        indicator.finish(False)
        raise
    else:
        indicator.finish(True)
    finally:
        ACTIVE_PROGRESS = previous


@contextmanager
def timed(name: str, progress: Optional[str] = None) -> Iterator[None]:
    with progress_indicator(progress):
        if TIMING is None:
            yield
        else:
            with TIMING.stage(name):
                yield


def timing_milestone(name: str) -> None:
    if TIMING is not None:
        TIMING.milestone(name)


def renamed_directory(preferred: Path, legacy: Path) -> Path:
    """Keep an existing pre-rename installation usable without moving its state."""
    if preferred.exists() or legacy.is_symlink() or not legacy.is_dir():
        return preferred
    try:
        next(legacy.iterdir())
    except (StopIteration, OSError):
        return preferred
    return legacy


def cache_home() -> Path:
    if "STNT_CACHE_HOME" in os.environ:
        return Path(os.environ["STNT_CACHE_HOME"]).expanduser()
    if platform.system() == "Darwin":
        base = Path.home() / "Library/Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return renamed_directory(base / "stnt", base / "ampx")


class StntError(RuntimeError):
    pass


def parse_service_url(value: str) -> tuple[str, str, int]:
    if not isinstance(value, str) or not value:
        raise StntError("--service-url must be a non-empty HTTP or HTTPS origin with an explicit port")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise StntError(f"invalid --service-url: {value!r}") from error
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or port is None or
            parsed.username is not None or parsed.password is not None or
            parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise StntError("--service-url must be an HTTP or HTTPS origin with an explicit port and no path")
    return parsed.scheme, parsed.hostname, port


def require_loopback_service_host(hostname: str, port: int) -> None:
    try:
        addresses = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise StntError(f"service URL hostname {hostname} does not resolve on the host") from error
    if not addresses or any(not ipaddress.ip_address(address[4][0]).is_loopback for address in addresses):
        raise StntError(f"service URL hostname {hostname} must resolve only to host loopback addresses")


def open_browser(service_url: str) -> None:
    _, hostname, port = parse_service_url(service_url)
    require_loopback_service_host(hostname, port)
    opened = run(["/usr/bin/open", service_url], check=False, capture=False)
    if opened.returncode != 0:
        raise StntError(f"macOS could not open the verified service URL: {service_url}")


def run(args: List[str], *, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=check, text=True, capture_output=capture)


def _emit_process_output(stdout: Optional[str], stderr: Optional[str]) -> None:
    if ACTIVE_PROGRESS is not None:
        ACTIVE_PROGRESS.finish(False)
    if stdout:
        sys.stdout.write(stdout)
        if not stdout.endswith("\n"):
            sys.stdout.write("\n")
    if stderr:
        sys.stderr.write(stderr)
        if not stderr.endswith("\n"):
            sys.stderr.write("\n")


def run_lifecycle(args: List[str], *, check: bool = True) -> subprocess.CompletedProcess:
    """Hide successful provider chatter, but retain live or failed diagnostics."""
    try:
        completed = run(args, check=check, capture=not VERBOSE)
    except subprocess.CalledProcessError as error:
        if not VERBOSE:
            _emit_process_output(error.stdout, error.stderr)
        raise
    if completed.returncode != 0 and not VERBOSE:
        _emit_process_output(completed.stdout, completed.stderr)
    return completed


def result(
    check_id: str,
    status: str,
    summary: str,
    *,
    next_command: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    value: Dict[str, Any] = {"id": check_id, "status": status, "summary": summary}
    if next_command:
        value["nextCommand"] = next_command
    if details:
        value["details"] = details
    return value


def kit_amp_version() -> Optional[str]:
    try:
        text = KIT_SPEC.read_text()
    except OSError:
        return None
    match = re.search(r"AMP_VERSION=([^\s]+)", text)
    return match.group(1) if match else None


def optional_repository() -> Optional[Path]:
    if not shutil.which("git"):
        return None
    completed = run(["git", "rev-parse", "--show-toplevel"], check=False)
    if completed.returncode != 0:
        return None
    return Path(completed.stdout.strip()).resolve()


def tool_checks() -> List[Dict[str, Any]]:
    commands = {
        "git": "brew install git",
        "jq": "brew install jq",
        "amp": "curl -fsSL https://ampcode.com/install.sh | bash",
        "sbx": "brew install --cask docker-sandboxes",
    }
    checks = []
    for name, install in commands.items():
        location = shutil.which(name)
        checks.append(
            result(
                f"tool.{name}",
                "pass" if location else "blocked",
                f"{name} found at {location}" if location else f"{name} is not installed or not on PATH",
                next_command=None if location else install,
            )
        )
    return checks


def host_checks(repo: Optional[Path]) -> List[Dict[str, Any]]:
    system = platform.system()
    machine = platform.machine()
    supported = system == "Darwin" and machine == "arm64"
    checks = [
        result(
            "host.platform",
            "pass" if supported else "blocked",
            f"{system} {platform.release()} on {machine}",
            next_command=None if supported else "uname -mrs; sw_vers",
        )
    ]
    memory = None
    if system == "Darwin":
        memory_result = run(["sysctl", "-n", "hw.memsize"], check=False)
        if memory_result.returncode == 0 and memory_result.stdout.strip().isdigit():
            memory = int(memory_result.stdout.strip())
    free_disk = shutil.disk_usage(str(repo or Path.home())).free
    capacity_status = "pass" if (memory is None or memory >= 12 * 1024**3) and free_disk >= 20 * 1024**3 else "warning"
    memory_text = "unknown" if memory is None else f"{memory / 1024**3:.1f} GiB memory"
    checks.append(
        result(
            "host.capacity",
            capacity_status,
            f"{memory_text}; {free_disk / 1024**3:.1f} GiB disk free",
            next_command="free at least 20 GiB of disk before creating a sandbox" if free_disk < 20 * 1024**3 else None,
            details={"memoryBytes": memory, "freeDiskBytes": free_disk},
        )
    )
    return checks


def git_checks(repo: Optional[Path]) -> List[Dict[str, Any]]:
    if not shutil.which("git"):
        return []
    if repo is None:
        return [result("git.repository", "blocked", "current directory is not inside a Git repository", next_command="git rev-parse --show-toplevel")]
    details: Dict[str, Any] = {"path": str(repo)}
    ordinary = (repo / ".git").is_dir()
    status = run(["git", "--no-optional-locks", "-C", str(repo), "status", "--porcelain=v1", "--untracked-files=all"], check=False)
    branch = run(["git", "-C", str(repo), "branch", "--show-current"], check=False)
    head = run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=False)
    details.update({"branch": branch.stdout.strip(), "head": head.stdout.strip(), "clean": status.returncode == 0 and not status.stdout})
    if not ordinary:
        return [result("git.repository", "blocked", "linked worktrees and non-ordinary Git directories are unsupported", next_command="cd to the repository's primary checkout", details=details)]
    if status.returncode != 0 or branch.returncode != 0 or head.returncode != 0 or not branch.stdout.strip():
        return [result("git.repository", "blocked", "repository must have a named branch and committed HEAD", next_command="git status", details=details)]
    if status.stdout:
        return [result(
            "git.repository", "warning",
            "host worktree has local changes; workspace Git starts from the pinned committed branch",
            next_command="git status --short", details=details,
        )]
    return [result("git.repository", "pass", f"clean {branch.stdout.strip()} at {head.stdout.strip()}", details=details)]


def amp_checks() -> List[Dict[str, Any]]:
    if not shutil.which("amp"):
        return []
    version = run(["amp", "--version"], check=False)
    if version.returncode != 0:
        return [result("amp.capabilities", "blocked", "Amp version lookup failed", next_command="amp --version")]
    outputs = []
    for extra in ([], ["--include-archived"]):
        listed = run(["amp", "threads", "list", "--json", "--limit", "1", *extra], check=False)
        if listed.returncode != 0:
            return [result("amp.capabilities", "blocked", "Amp exact-thread list/authentication check failed", next_command="amp login")]
        try:
            value = json.loads(listed.stdout)
        except json.JSONDecodeError:
            return [result("amp.capabilities", "blocked", "Amp thread list returned malformed JSON", next_command="amp threads list --json --limit 1")]
        if not isinstance(value, list) or any(not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in value):
            return [result("amp.capabilities", "blocked", "Amp thread list JSON contract is incompatible", next_command="amp threads list --json --limit 1")]
        outputs.append(value)
    host_version = version.stdout.strip()
    pin = kit_amp_version()
    checks = [result("amp.capabilities", "pass", f"Amp {host_version}; authenticated active/archive JSON lookup available")]
    if pin is None:
        checks.append(result("amp.drift", "blocked", "bundled kit has no readable Amp version pin", next_command=f"inspect {KIT_SPEC}"))
    elif pin in host_version:
        checks.append(result("amp.drift", "pass", f"host Amp matches bundled kit pin {pin}"))
    else:
        checks.append(result("amp.drift", "warning", f"host Amp {host_version.split()[0]} differs from kit pin {pin}; this pairing is not auto-upgraded"))
    return checks


def docker_credential_binding_check(
    command: str,
    check_id: str,
    label: str,
    domain: str,
    *,
    missing_status: str,
) -> Dict[str, Any]:
    binding = run([str(RUNTIME), command], check=False)
    approved = file_exists = valid = False
    try:
        value = json.loads(binding.stdout)
        valid = (
            binding.returncode == 0 and isinstance(value, dict) and
            set(value) == {"approved", "fileExists"} and
            isinstance(value["approved"], bool) and
            isinstance(value["fileExists"], bool)
        )
        if valid:
            approved, file_exists = value["approved"], value["fileExists"]
    except json.JSONDecodeError:
        pass
    if approved:
        summary = f"{label} apiKey use is approved only for {domain}"
        next_command = None
    elif valid and file_exists:
        summary = f"Docker credential bindings exist without the exact {label} approval; Stnt will not modify them"
        next_command = f"review ~/.config/sbx/credentials.yaml and add the documented {domain} binding"
    elif valid:
        summary = f"{label} apiKey use by third-party Stnt kits is not yet approved"
        next_command = "stnt setup"
    else:
        summary = f"Docker {label} credential binding approval is unavailable or malformed"
        next_command = f"bin/docker-sandbox {command}"
    return result(
        check_id, "pass" if approved else missing_status,
        summary, next_command=next_command,
    )


def docker_checks() -> List[Dict[str, Any]]:
    if not shutil.which("sbx") or not shutil.which("jq"):
        return []
    checks: List[Dict[str, Any]] = []
    version = run([str(RUNTIME), "version"], check=False)
    match = re.search(r"\bv?\d+\.\d+\.\d+\b", version.stdout + version.stderr)
    actual_version = match.group(0) if match else None
    if version.returncode != 0 or actual_version != SUPPORTED_SBX_VERSION:
        checks.append(result("docker.version", "blocked", f"Docker Sandboxes version is {actual_version or 'unreadable'}; supported contract is {SUPPORTED_SBX_VERSION}", next_command="brew info --cask docker-sandboxes"))
    else:
        checks.append(result("docker.version", "pass", f"Docker Sandboxes {actual_version} matches the supported contract"))

    kit = run([str(RUNTIME), "validate-kit"], check=False)
    checks.append(result("docker.kit", "pass" if kit.returncode == 0 else "blocked", "bundled schema-v2 Amp kit is valid" if kit.returncode == 0 else "bundled Amp kit validation failed", next_command=None if kit.returncode == 0 else "bin/docker-sandbox validate-kit"))

    checks.append(docker_credential_binding_check(
        "amp-binding-status", "docker.amp-binding", "Amp", "ampcode.com",
        missing_status="blocked",
    ))
    checks.append(docker_credential_binding_check(
        "github-binding-status", "docker.github-binding", "GitHub", "github.com",
        missing_status="warning",
    ))

    daemon_probe = run([str(RUNTIME), "daemon-status"], check=False)
    daemon_running = any(line.strip() == "Status: running" for line in daemon_probe.stdout.splitlines())
    if daemon_probe.returncode != 0 or not daemon_running:
        checks.append(result("docker.daemon", "blocked", "sandbox daemon is stopped or unavailable", next_command="sbx daemon restart"))
        checks.append(result("docker.login", "blocked", "Docker login cannot be checked while the daemon is stopped", next_command="sbx daemon restart"))
        for check_id, name in (
            ("docker.json", "sandbox JSON contract"),
            ("docker.policy", "network policy"),
            ("docker.secret", "secret names and targets"),
            ("docker.github-secret", "GitHub credential"),
        ):
            checks.append(result(check_id, "ambiguous", f"{name} cannot be checked while the daemon is stopped", next_command="sbx daemon restart"))
        return checks

    diagnosis = run([str(RUNTIME), "diagnose"], check=False)
    daemon_status = auth_status = "blocked"
    daemon_summary = "Docker sandbox daemon diagnosis is unavailable"
    auth_summary = "Docker sandbox login diagnosis is unavailable"
    try:
        diagnosed = json.loads(diagnosis.stdout)
        entries = diagnosed.get("checks") if isinstance(diagnosed, dict) else None
        if not isinstance(entries, list) or not all(
            isinstance(entry, dict) and isinstance(entry.get("name"), str) and
            isinstance(entry.get("status"), str) for entry in entries
        ):
            raise ValueError
        names = [entry.get("name") for entry in entries if isinstance(entry, dict)]
        if len(names) != len(set(names)):
            raise ValueError
        by_name = {entry.get("name"): entry for entry in entries if isinstance(entry, dict)}
        daemon = by_name.get("Daemon")
        socket_check = by_name.get("Socket")
        auth = by_name.get("Authentication")
        if daemon and socket_check and daemon.get("status") == socket_check.get("status") == "pass":
            daemon_status, daemon_summary = "pass", "sandbox daemon is healthy and responsive"
        elif daemon or socket_check:
            daemon_summary = "sandbox daemon is unavailable or unhealthy"
        if auth and auth.get("status") == "pass":
            auth_status, auth_summary = "pass", "Docker sandbox login is authenticated"
        elif auth and auth.get("status") == "fail":
            auth_summary = "Docker sandbox login is unauthenticated"
        elif daemon_status != "pass":
            auth_summary = "Docker login cannot be checked until the daemon is healthy"
    except (json.JSONDecodeError, ValueError):
        daemon_summary = "Docker diagnosis returned malformed JSON"
        auth_summary = "Docker login cannot be classified because diagnosis JSON is malformed"
    if diagnosis.returncode != 0:
        daemon_status = "blocked"
        daemon_summary = "Docker diagnosis command failed"
        auth_status = "blocked"
        auth_summary = "Docker login cannot be trusted because diagnosis failed"
    checks.append(result("docker.daemon", daemon_status, daemon_summary, next_command=None if daemon_status == "pass" else "sbx daemon restart"))
    checks.append(result("docker.login", auth_status, auth_summary, next_command=None if auth_status == "pass" else "sbx login"))

    inventory = run([str(RUNTIME), "list"], check=False)
    try:
        inventory_value = json.loads(inventory.stdout)
        sandboxes = inventory_value.get("sandboxes") if isinstance(inventory_value, dict) else None
        valid_inventory = inventory.returncode == 0 and isinstance(sandboxes, list) and all(
            isinstance(entry, dict) and isinstance(entry.get("name"), str) and
            isinstance(entry.get("id"), str) and isinstance(entry.get("workspaces"), list) and
            all(isinstance(workspace, str) for workspace in entry["workspaces"])
            for entry in sandboxes
        )
    except json.JSONDecodeError:
        valid_inventory = False
    checks.append(result("docker.json", "pass" if valid_inventory else "blocked", "strict sandbox inventory JSON contract is compatible" if valid_inventory else "sandbox inventory JSON contract is unavailable or malformed", next_command=None if valid_inventory else "bin/docker-sandbox list"))

    policy = run([str(RUNTIME), "policy"], check=False)
    active_network_names: List[str] = []
    policy_valid = False
    try:
        policy_value = json.loads(policy.stdout)
        rules = policy_value.get("rules") if isinstance(policy_value, dict) else None
        if policy.returncode != 0 or not isinstance(rules, list) or not all(
            isinstance(entry, dict) and isinstance(entry.get("name"), str) and
            isinstance(entry.get("status"), str) and isinstance(entry.get("resource_type"), str) and
            isinstance(entry.get("decision"), str) and isinstance(entry.get("resources"), list)
            for entry in rules
        ):
            raise ValueError
        policy_valid = True
        active_network_names = [entry["name"] for entry in rules if entry.get("status") == "active" and entry.get("resource_type") == "network"]
    except (json.JSONDecodeError, ValueError, KeyError):
        pass
    policy_check = run([str(RUNTIME), "policy-check"], check=False)
    policy_lines = [line.strip() for line in policy_check.stdout.splitlines()]
    effective_deny = (
        policy_check.returncode == 1 and
        policy_lines[:1] == ["Denied: stnt-doctor.invalid:443"] and
        "Context: global" in policy_lines and
        "Reason: no matching allow rule (default deny)" in policy_lines
    )
    policy_pass = policy_valid and effective_deny
    names_summary = ", ".join(active_network_names) if active_network_names else "none"
    policy_summary = f"effective default deny confirmed; active network policies: {names_summary}" if policy_pass else ("network policy does not provide a confirmed effective default deny" if policy_valid else "network policy inventory is unavailable or malformed")
    policy_next = None if policy_pass else "sbx policy check network stnt-doctor.invalid:443"
    checks.append(result("docker.policy", "pass" if policy_pass else "blocked", policy_summary, next_command=policy_next, details={"activeNetworkPolicies": active_network_names, "effectiveDefaultDeny": effective_deny}))

    secrets = run([str(RUNTIME), "secrets"], check=False)
    secret_names: List[Dict[str, str]] = []
    secrets_valid = False
    try:
        parsed_secrets = json.loads(secrets.stdout)
        if secrets.returncode != 0 or not isinstance(parsed_secrets, list) or any(not isinstance(entry, dict) or set(entry) != {"target", "name"} for entry in parsed_secrets):
            raise ValueError
        secret_names = parsed_secrets
        secrets_valid = True
    except (json.JSONDecodeError, ValueError):
        pass
    has_amp = any(entry == {"target": "ampcode.com", "name": "AMP_API_KEY"} for entry in secret_names)
    secret_command = "sbx secret set-custom --host ampcode.com --env AMP_API_KEY"
    secret_summary = "AMP_API_KEY is registered for ampcode.com; values were not requested" if has_amp else ("AMP_API_KEY is not registered for ampcode.com" if secrets_valid else "secret-name inventory is unavailable or malformed; no value was requested")
    secret_next = None if has_amp else (secret_command if secrets_valid else "bin/docker-sandbox secrets")
    checks.append(result("docker.secret", "pass" if has_amp else "blocked", secret_summary, next_command=secret_next, details={"namesAndTargets": secret_names}))
    has_github = any(entry == {"target": "github", "name": "GITHUB_TOKEN"} for entry in secret_names)
    github_command = "gh auth token | sbx secret set github"
    github_summary = "GitHub credential is registered; its value was not requested" if has_github else ("GitHub credential is not registered" if secrets_valid else "GitHub credential inventory is unavailable or malformed; no value was requested")
    github_next = None if has_github else (github_command if secrets_valid else "bin/docker-sandbox secrets")
    checks.append(result("docker.github-secret", "pass" if has_github else "warning", github_summary, next_command=github_next))
    return checks


def state_checks(*, runtime_available: bool = True) -> List[Dict[str, Any]]:
    root = state_root()
    if not root.exists():
        return [result("state.layout", "warning", f"Stnt state directory is absent: {root}", next_command="stnt setup"), result("state.sessions", "pass", "no durable session records")]
    unsafe = []
    for path in (root, root / "sessions", root / "locks"):
        if not path.is_dir() or stat.S_IMODE(path.stat().st_mode) != 0o700:
            unsafe.append(str(path))
    for path in list((root / "sessions").glob("*.json")) + list((root / "locks").glob("*.lock")):
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            unsafe.append(str(path))
    checks = [result("state.layout", "pass" if not unsafe else "warning", "state directories exist with mode 0700" if not unsafe else f"state layout needs local permission repair: {', '.join(unsafe)}", next_command=None if not unsafe else "stnt setup")]
    sessions = root / "sessions"
    records = list(sessions.glob("*.json")) if sessions.is_dir() else []
    if not records:
        checks.append(result("state.sessions", "pass", "no durable session records"))
        return checks
    try:
        loaded_records = load_sessions()
    except StntError as error:
        checks.append(result(
            "state.sessions", "ambiguous", str(error), next_command="stnt list",
            details={"records": [str(path) for path in records]},
        ))
        return checks
    if not runtime_available:
        checks.append(result("state.sessions", "ambiguous", f"{len(records)} session record(s) cannot be reconciled while the sandbox daemon is stopped", next_command="sbx daemon restart"))
        return checks
    needs_host_amp = any(record.get("lifecycleOwner") != "workspace" for _, record in loaded_records)
    if (needs_host_amp and not shutil.which("amp")) or not shutil.which("sbx") or not shutil.which("jq"):
        checks.append(result("state.sessions", "ambiguous", f"{len(records)} session record(s) cannot be reconciled until prerequisites are available", next_command="stnt doctor"))
        return checks
    summaries = []
    overall = "retained"
    for path, record in loaded_records:
        try:
            validate_record_repository(record, Path(record["repositoryPath"]))
            found = runtime_find(record["sandbox"])
            selector = record_selector(record)
            amp_state = None
            if record.get("lifecycleOwner") != "workspace":
                amp_state = thread_status(
                    record["threadID"],
                    allow_empty=record["status"] in {
                        "creating", "starting", "active", "paused", "ambiguous",
                    },
                )
            if found is None:
                summaries.append(f"{selector}: sandbox {record['sandbox']} missing")
                overall = "ambiguous"
            elif found.get("id") != record.get("sandboxID"):
                summaries.append(f"{selector}: sandbox identity mismatch")
                overall = "ambiguous"
            else:
                authority = f"thread {amp_state}" if amp_state is not None else "workspace-owned"
                summaries.append(f"{selector}: {record['status']}, {authority}, sandbox retained")
        except (StntError, OSError) as error:
            summaries.append(f"{path.name}: {error}")
            overall = "ambiguous"
    checks.append(result("state.sessions", overall, "; ".join(summaries), next_command="stnt doctor" if overall == "ambiguous" else None, details={"records": [str(path) for path in records]}))
    return checks


def vscode_command() -> Optional[str]:
    discovered = shutil.which("code")
    bundled = "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"
    return discovered or (bundled if Path(bundled).is_file() else None)


def vscode_remote_ssh_offline_ready() -> bool:
    settings = Path.home() / "Library/Application Support/Code/User/settings.json"
    try:
        contents = settings.read_text()
    except OSError:
        return False
    return bool(
        re.search(r'"remote\.SSH\.localServerDownload"\s*:\s*"always"', contents)
        and re.search(r'"remote\.SSH\.useExecServer"\s*:\s*false\b', contents)
        and stnt_ssh_configured()
    )


def stnt_ssh_configured() -> bool:
    config = Path.home() / ".ssh/config"
    try:
        contents = config.read_text()
    except OSError:
        return False
    stnt = contents.find(STNT_SSH_BEGIN)
    end = contents.find(STNT_SSH_END, stnt + 1)
    docker = contents.find("Host *.sbx")
    managed = contents[stnt:end] if stnt >= 0 and end > stnt else ""
    return bool(
        managed and
        "Host *.stnt.sbx" in managed and
        " ssh-proxy %n" in managed and
        " ssh-known-hosts %H" in managed and
        (docker < 0 or stnt < docker)
    )


def clipboard_image_paste_enabled() -> Optional[bool]:
    status = run([str(RUNTIME), "clipboard-image-paste-status"], check=False)
    if status.returncode != 0:
        return None
    try:
        value = json.loads(status.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("value"), bool):
        return None
    return value["value"]


def integration_checks(*, runtime_available: bool = True) -> List[Dict[str, Any]]:
    ssh_config = Path.home() / ".ssh/config"
    docker_configured = False
    stnt_configured = False
    if ssh_config.is_file():
        try:
            contents = ssh_config.read_text()
            docker_configured = "Host *.sbx" in contents
            stnt_configured = stnt_ssh_configured()
        except OSError:
            pass
    feature_enabled = False
    try:
        if shutil.which("sbx"):
            feature = run([str(RUNTIME), "ssh-status"], check=False)
            value = json.loads(feature.stdout)
            feature_enabled = feature.returncode == 0 and value.get("enabled") is True
    except (json.JSONDecodeError, AttributeError):
        pass
    ssh_status = (
        "pass" if shutil.which("ssh") and docker_configured and stnt_configured and feature_enabled
        else "warning"
    )
    ssh_summary = (
        f"SSH client={'available' if shutil.which('ssh') else 'absent'}, "
        f"Stnt editor transport={'available' if stnt_configured else 'absent'}, "
        f"Docker config={'available' if docker_configured else 'absent'}, "
        f"daemon feature={'enabled' if feature_enabled else 'disabled or unreadable'} (informational)"
    )
    next_command = None if ssh_status == "pass" else (
        "stnt setup" if docker_configured else "sbx setup ssh; then run stnt setup"
    )
    checks = [result("ssh.client", ssh_status, ssh_summary, next_command=next_command)]
    image_paste = clipboard_image_paste_enabled() if shutil.which("sbx") and runtime_available else None
    if image_paste is True:
        checks.append(result(
            "clipboard.image-paste", "pass",
            "native guest-Amp screenshot paste is enabled; revoke host clipboard access with: sbx settings set clipboard.imagePaste false",
        ))
    elif image_paste is False:
        checks.append(result(
            "clipboard.image-paste", "warning",
            "native guest-Amp screenshot paste is disabled; enabling it permits image-only host clipboard reads on explicit Ctrl+V",
            next_command="stnt setup",
        ))
    else:
        checks.append(result(
            "clipboard.image-paste", "warning" if runtime_available else "ambiguous",
            "native guest-Amp screenshot-paste setting is unavailable or unreadable",
            next_command="sbx settings get --json clipboard.imagePaste" if runtime_available else "sbx daemon restart",
        ))
    code = vscode_command()
    cursor = shutil.which("cursor") or ("/Applications/Cursor.app/Contents/Resources/app/bin/cursor" if Path("/Applications/Cursor.app/Contents/Resources/app/bin/cursor").is_file() else None)
    remote_ssh = False
    if code:
        extensions = run([code, "--list-extensions"], check=False)
        remote_ssh = any(line.lower() == "ms-vscode-remote.remote-ssh" for line in extensions.stdout.splitlines())
    summary = f"VS Code={'available' if code else 'absent'}, Remote SSH={'available' if remote_ssh else 'absent'}, Cursor={'available' if cursor else 'absent'} (informational)"
    checks.append(result("editor.availability", "pass" if remote_ssh or cursor else "warning", summary, next_command=None if remote_ssh or cursor else "code --install-extension ms-vscode-remote.remote-ssh"))
    offline_ready = bool(code and remote_ssh and vscode_remote_ssh_offline_ready())
    checks.append(result(
        "editor.remote-server-download",
        "pass" if offline_ready else "warning",
        "VS Code Remote SSH downloads its server on the host before copying it into network-restricted sandboxes"
        if offline_ready else
        "VS Code Remote SSH may fail because its default exec server downloads from inside the network-restricted sandbox",
        next_command=None if offline_ready else
        'Set "remote.SSH.localServerDownload" to "always" and "remote.SSH.useExecServer" to false, then restart VS Code',
    ))
    return checks


def doctor_results(repo: Optional[Path]) -> List[Dict[str, Any]]:
    checks = tool_checks()
    checks.extend(host_checks(repo))
    checks.extend(git_checks(repo))
    checks.extend(amp_checks())
    docker = docker_checks()
    checks.extend(docker)
    runtime_available = any(check["id"] == "docker.daemon" and check["status"] == "pass" for check in docker)
    checks.extend(state_checks(runtime_available=runtime_available))
    checks.extend(integration_checks(runtime_available=runtime_available))
    return checks


CRITICAL_PREFLIGHT_IDS = {
    "tool.git", "tool.jq", "tool.sbx", "host.platform", "git.repository",
    "docker.version", "docker.daemon", "docker.login",
    "docker.json", "docker.policy", "docker.secret", "docker.kit",
    "docker.amp-binding", "state.layout",
}
HOST_AMP_PREFLIGHT_IDS = {"tool.amp", "amp.capabilities", "amp.drift"}


def critical_preflight(
    repo: Path,
    *,
    require_host_amp: bool = False,
    require_github: bool = False,
) -> None:
    required = CRITICAL_PREFLIGHT_IDS | (HOST_AMP_PREFLIGHT_IDS if require_host_amp else set())
    if require_github:
        required.update({"docker.github-secret", "docker.github-binding"})
    blocked = [
        check for check in doctor_results(repo)
        if check["id"] in required and (
            check["status"] == "blocked" or
            (check["id"] in {"docker.github-secret", "docker.github-binding"} and
             check["status"] != "pass")
        )
    ]
    if blocked:
        lines = [f"{check['id']}: {check['summary']}; next: {check.get('nextCommand', 'stnt doctor')}" for check in blocked]
        raise StntError("prerequisite checks failed before workspace creation:\n" + "\n".join(lines))


def print_doctor(checks: Sequence[Dict[str, Any]]) -> None:
    labels = {"pass": "PASS", "warning": "WARNING", "blocked": "BLOCKED", "retained": "RETAINED", "ambiguous": "AMBIGUOUS"}
    for check in checks:
        print(f"[{labels[check['status']]}] {check['id']}: {check['summary']}")
        if check.get("nextCommand"):
            print(f"  next: {check['nextCommand']}")


def state_root() -> Path:
    override = os.environ.get("STNT_STATE_HOME")
    if override:
        return Path(override).expanduser().resolve()
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        base = Path(xdg).expanduser().resolve()
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path.home() / ".local/state"
    return renamed_directory(base / "stnt", base / "ampx")


def config_root() -> Path:
    override = os.environ.get("STNT_CONFIG_HOME")
    if override:
        return Path(override).expanduser().resolve()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        base = Path(xdg).expanduser().resolve()
    else:
        base = Path.home() / ".config"
    return renamed_directory(base / "stnt", base / "ampx")


def stack_profile_path(name: str) -> Path:
    if not isinstance(name, str) or not STACK_NAME_RE.fullmatch(name):
        raise StntError("stack name must begin with a lowercase letter and contain only lowercase letters, digits, and hyphens (maximum 32 characters)")
    return config_root() / "stacks" / f"{name}.json"


def stack_state_path(name: str) -> Path:
    stack_profile_path(name)
    return state_root() / "stacks" / f"{name}.json"


def ensure_directory(path: Path) -> None:
    missing = []
    candidate = path
    while not candidate.exists():
        missing.append(candidate)
        candidate = candidate.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        fsync_directory(directory.parent)


def ensure_state_layout() -> None:
    root = state_root()
    ensure_directory(root)
    ensure_directory(root / "sessions")
    ensure_directory(root / "stacks")
    ensure_directory(root / "locks")
    os.chmod(root, 0o700)
    os.chmod(root / "sessions", 0o700)
    os.chmod(root / "stacks", 0o700)
    os.chmod(root / "locks", 0o700)
    for path in (list((root / "sessions").glob("*.json")) +
                 list((root / "stacks").glob("*.json")) +
                 list((root / "locks").glob("*.lock"))):
        os.chmod(path, 0o600)


def repository() -> Path:
    if not shutil.which("git"):
        raise StntError("Git is not installed or not on PATH; next: brew install git")
    result = run(["git", "rev-parse", "--show-toplevel"])
    repo = Path(result.stdout.strip()).resolve()
    if not (repo / ".git").is_dir():
        raise StntError("linked worktrees and non-ordinary Git directories are not supported in Phase 1A")
    branch = run(["git", "-C", str(repo), "branch", "--show-current"]).stdout.strip()
    if not branch:
        raise StntError("detached or unborn repositories are not supported in Phase 1A")
    run(["git", "-C", str(repo), "rev-parse", "HEAD"])
    return repo


def repo_key(repo: Path) -> str:
    return hashlib.sha256(os.fsencode(str(repo))).hexdigest()[:20]


def compact_thread_id(thread_id: str) -> str:
    if not isinstance(thread_id, str) or not THREAD_RE.fullmatch(thread_id):
        raise StntError(f"invalid Amp thread ID: {thread_id!r}")
    return thread_id[2:].replace("-", "")


def new_workspace_id() -> str:
    return f"W-{uuid.uuid4()}"


def compact_workspace_id(workspace_id: str) -> str:
    if not isinstance(workspace_id, str) or not WORKSPACE_RE.fullmatch(workspace_id):
        raise StntError(f"invalid Stnt workspace ID: {workspace_id!r}")
    return workspace_id[2:].replace("-", "")


def migrated_workspace_id(thread_id: str) -> str:
    compact_thread_id(thread_id)
    return f"W-{thread_id[2:]}"


def legacy_session_path(repo: Path) -> Path:
    return state_root() / "sessions" / f"{repo_key(repo)}.json"


def session_path(repo: Path, thread_id: Optional[str] = None) -> Path:
    if thread_id is None:
        legacy = legacy_session_path(repo)
        matches = list((state_root() / "sessions").glob(f"{repo_key(repo)}--*.json"))
        # Compatibility for callers inspecting the only/default record.
        return matches[0] if not legacy.exists() and len(matches) == 1 else legacy
    return state_root() / "sessions" / f"{repo_key(repo)}--{compact_thread_id(thread_id)}.json"


def workspace_session_path(repo: Path, workspace_id: str) -> Path:
    return state_root() / "sessions" / f"{repo_key(repo)}--{compact_workspace_id(workspace_id)}.json"


def record_session_path(record: Dict[str, Any]) -> Path:
    repo = Path(record["repositoryPath"])
    if record.get("schemaVersion") == SCHEMA_VERSION:
        return workspace_session_path(repo, record["workspaceID"])
    return session_path(repo, record["threadID"])


def fsync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(path: Path, value: Dict[str, Any], *, create_only: bool = False) -> None:
    ensure_directory(path.parent)
    os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w") as output:
            json.dump(value, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        if os.environ.get("STNT_TEST_INTERRUPT_AT") == "before-replace":
            raise StntError("synthetic interruption before atomic replace")
        if create_only:
            try:
                os.link(temporary, path)
            except FileExistsError as error:
                raise StntError(f"session state already exists; refusing to replace it: {path}") from error
        else:
            os.replace(temporary, path)
        os.chmod(path, 0o600)
        fsync_directory(path.parent)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def atomic_write_text(path: Path, value: str) -> None:
    ensure_directory(path.parent)
    os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w") as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        fsync_directory(path.parent)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def configuration_repository() -> Path:
    """Resolve an ordinary repository without inspecting or requiring its worktree."""
    if not shutil.which("git"):
        raise StntError("Git is not installed or not on PATH; next: brew install git")
    resolved = run(["git", "rev-parse", "--show-toplevel"], check=False)
    if resolved.returncode != 0 or not resolved.stdout.strip():
        raise StntError("current directory is not inside a Git repository")
    repo = Path(resolved.stdout.strip()).resolve()
    if not (repo / ".git").is_dir():
        raise StntError("linked worktrees and non-ordinary Git directories are not supported")
    if run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=False).returncode != 0:
        raise StntError("repository initialization requires a committed HEAD")
    return repo


def normalize_repository_remote(value: str) -> str:
    remote = value.strip()
    scp = re.fullmatch(r"(?:[^@/]+@)?([^:/]+):(.+)", remote)
    if scp and "://" not in remote:
        host, path = scp.groups()
        return f"{host.lower()}/{path.removesuffix('.git').strip('/')}"
    parsed = urlsplit(remote)
    if parsed.scheme in {"http", "https", "ssh", "git"} and parsed.hostname:
        path = parsed.path.removesuffix(".git").strip("/")
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.hostname.lower()}{port}/{path}"
    if parsed.scheme == "file":
        return f"file:{Path(parsed.path).expanduser().resolve()}"
    return remote.removesuffix(".git").rstrip("/")


def repository_identity(repo: Path) -> Dict[str, Any]:
    origin = run(["git", "-C", str(repo), "config", "--get", "remote.origin.url"], check=False)
    remote: Optional[str] = None
    if origin.returncode == 0 and origin.stdout.strip():
        remote = normalize_repository_remote(origin.stdout)
    else:
        remotes = run(["git", "-C", str(repo), "remote"], check=False)
        names = [name for name in remotes.stdout.splitlines() if name]
        if len(names) == 1:
            candidate = run(
                ["git", "-C", str(repo), "config", "--get", f"remote.{names[0]}.url"],
                check=False,
            )
            if candidate.returncode == 0 and candidate.stdout.strip():
                remote = normalize_repository_remote(candidate.stdout)
    canonical = str(repo.resolve())
    material = json.dumps({"remote": remote, "path": canonical}, sort_keys=True, separators=(",", ":"))
    return {
        "remote": remote,
        "path": canonical,
        "key": hashlib.sha256(material.encode()).hexdigest(),
    }


def profile_path(identity: Dict[str, Any]) -> Path:
    name = Path(identity["path"]).name or "repository"
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "repository"
    return config_root() / "repositories" / f"{slug}--{identity['key'][:24]}.json"


def parse_command_argv(value: str, option: str) -> List[str]:
    try:
        argv = json.loads(value)
    except json.JSONDecodeError as error:
        raise StntError(f"{option} must be a JSON array of command arguments") from error
    if (not isinstance(argv, list) or not argv or
            any(not isinstance(argument, str) or not argument for argument in argv)):
        raise StntError(f"{option} must be a non-empty JSON array of non-empty strings")
    return argv


def stack_repository(path_value: str, role: str, argv: List[str], guest_path: str) -> Dict[str, Any]:
    path = Path(path_value).expanduser().resolve()
    if not (path / ".git").is_dir():
        raise StntError(f"stack {role} path is not an ordinary Git repository: {path}")
    status = run(["git", "-C", str(path), "status", "--porcelain=v1", "--untracked-files=all"], check=False)
    if status.returncode != 0 or status.stdout:
        raise StntError(f"stack {role} repository must be clean: {path}")
    branch = run(["git", "-C", str(path), "branch", "--show-current"], check=False).stdout.strip()
    sha = run(["git", "-C", str(path), "rev-parse", "--verify", "HEAD^{commit}"], check=False).stdout.strip()
    if (not branch or run(["git", "check-ref-format", "--branch", branch], check=False).returncode != 0 or
            not re.fullmatch(r"[0-9a-f]{40}", sha)):
        raise StntError(f"stack {role} repository requires a named branch and committed HEAD: {path}")
    identity = repository_identity(path)
    return {
        "role": role,
        "path": str(path),
        "remote": identity["remote"],
        "branch": branch,
        "sha": sha,
        "guestPath": guest_path,
        "argv": argv,
    }


def validate_stack_profile(value: Any, path: Path, expected_name: Optional[str] = None) -> Dict[str, Any]:
    if (not isinstance(value, dict) or value.get("schemaVersion") != STACK_SCHEMA_VERSION or
            not isinstance(value.get("name"), str) or not STACK_NAME_RE.fullmatch(value["name"]) or
            (expected_name is not None and value["name"] != expected_name)):
        raise StntError(f"stack profile is invalid or unsupported: {path}")
    repositories = value.get("repositories")
    if not isinstance(repositories, list) or len(repositories) != 2:
        raise StntError(f"stack profile must contain exactly frontend and backend repositories: {path}")
    roles = {item.get("role") for item in repositories if isinstance(item, dict)}
    if roles != {"frontend", "backend"}:
        raise StntError(f"stack profile repository roles are invalid: {path}")
    for item in repositories:
        if (set(item) != {"role", "path", "remote", "branch", "sha", "guestPath", "argv"} or
                not Path(item["path"]).is_absolute() or not Path(item["guestPath"]).is_absolute() or
                not isinstance(item["branch"], str) or
                run(["git", "check-ref-format", "--branch", item["branch"]], check=False).returncode != 0 or
                not re.fullmatch(r"[0-9a-f]{40}", str(item["sha"])) or
                not isinstance(item["argv"], list) or not item["argv"] or
                any(not isinstance(argument, str) or not argument for argument in item["argv"]) or
                (item["remote"] is not None and not isinstance(item["remote"], str))):
            raise StntError(f"stack profile contains an invalid repository entry: {path}")
    if repositories[0]["path"] == repositories[1]["path"]:
        raise StntError(f"stack repositories must be distinct: {path}")
    ingress = value.get("ingress")
    if (not isinstance(ingress, dict) or set(ingress) != {"hostPort", "sandboxPort", "healthPath"} or
            isinstance(ingress["hostPort"], bool) or not isinstance(ingress["hostPort"], int) or
            not (1 <= ingress["hostPort"] <= 65535) or ingress["sandboxPort"] != STACK_INGRESS_PORT or
            not isinstance(ingress["healthPath"], str) or not re.fullmatch(r"/\S*", ingress["healthPath"])):
        raise StntError(f"stack profile contains an invalid ingress: {path}")
    internal = value.get("internalPorts")
    if internal != {"http": STACK_HTTP_PORT, "websocket": STACK_WEBSOCKET_PORT}:
        raise StntError(f"stack profile internal ports are invalid: {path}")
    if set(value) != {"schemaVersion", "name", "repositories", "ingress", "internalPorts"}:
        raise StntError(f"stack profile contains unsupported fields: {path}")
    return value


def load_stack_profile(name: str) -> tuple[Path, Dict[str, Any]]:
    path = stack_profile_path(name)
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError as error:
        raise StntError(f"stack {name!r} is not initialized; next: stnt stack init {name} ...") from error
    except (OSError, json.JSONDecodeError) as error:
        raise StntError(f"stack profile is unreadable ({path}): {error}") from error
    return path, validate_stack_profile(value, path, name)


def stack_role(profile: Dict[str, Any], role: str) -> Dict[str, Any]:
    return next(item for item in profile["repositories"] if item["role"] == role)


def stack_state_repositories(profile: Dict[str, Any], thread_id: str) -> List[Dict[str, Any]]:
    suffix = compact_thread_id(thread_id)
    return [
        dict(
            item,
            preservationBranch=f"stnt-preserved/stack-{profile['name']}-{item['role']}-{suffix}",
        )
        for item in profile["repositories"]
    ]


def stack_record_matches_profile(record: Dict[str, Any], profile: Dict[str, Any]) -> bool:
    profile_fields = set(profile["repositories"][0])
    repositories = [
        {field: item.get(field) for field in profile_fields}
        for item in record["repositories"]
    ]
    return (
        record["profileDigest"] == canonical_digest(profile)
        and repositories == profile["repositories"]
        and record["ingress"] == profile["ingress"]
    )


def ensure_stack_preservation_intent(record: Dict[str, Any], profile: Dict[str, Any]) -> None:
    expected = stack_state_repositories(profile, record["threadID"])
    observed = [item.get("preservationBranch") for item in record["repositories"]]
    intended = [item["preservationBranch"] for item in expected]
    if observed == intended:
        return
    if any(value is not None for value in observed) or not stack_record_matches_profile(record, profile):
        raise StntError(f"stack preservation intent is incomplete or inconsistent; retained {record['name']}")
    record["repositories"] = expected
    atomic_write(stack_state_path(record["name"]), record)


def require_stack_sources(profile: Dict[str, Any]) -> None:
    for item in profile["repositories"]:
        current = stack_repository(item["path"], item["role"], item["argv"], item["guestPath"])
        if any(current[field] != item[field] for field in ("remote", "branch", "sha")):
            raise StntError(
                f"stack {item['role']} source changed after review; rerun stack init with a new name or remove the inactive profile explicitly"
            )


def command_stack_init(
    name: str,
    frontend_path: str,
    backend_path: str,
    ingress_port: int,
    frontend_command: str,
    backend_command: str,
) -> int:
    path = stack_profile_path(name)
    if path.exists():
        raise StntError(f"stack profile already exists and was not changed: {path}")
    frontend = stack_repository(
        frontend_path, "frontend", parse_command_argv(frontend_command, "--frontend-command"),
        str(Path(frontend_path).expanduser().resolve()),
    )
    backend = stack_repository(
        backend_path, "backend", parse_command_argv(backend_command, "--backend-command"),
        f"/home/agent/stnt-stacks/{name}/backend",
    )
    profile = validate_stack_profile({
        "schemaVersion": STACK_SCHEMA_VERSION,
        "name": name,
        "repositories": [frontend, backend],
        "ingress": {"hostPort": ingress_port, "sandboxPort": STACK_INGRESS_PORT, "healthPath": "/health"},
        "internalPorts": {"http": STACK_HTTP_PORT, "websocket": STACK_WEBSOCKET_PORT},
    }, path, name)
    print(json.dumps(profile, indent=2, sort_keys=True))
    if not sys.stdin.isatty():
        raise StntError("stack initialization requires an interactive review; no profile was written")
    try:
        accepted = input("Write this user-local stack profile? [y/N] ").strip().lower()
    except EOFError:
        accepted = ""
    if accepted not in {"y", "yes"}:
        raise StntError("stack initialization cancelled; no profile was written")
    atomic_write(path, profile, create_only=True)
    print(f"initialized stack {name}; start with: stnt stack start {name}")
    return 0


def committed_file(repo: Path, relative: str) -> Optional[tuple[str, str]]:
    """Return a checked-in root file's blob ID and text without reading the worktree."""
    if "/" in relative or relative in {"", ".", ".."}:
        raise StntError(f"discovery supports root files only: {relative!r}")
    blob = run(["git", "-C", str(repo), "rev-parse", f"HEAD:{relative}"], check=False)
    if blob.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", blob.stdout.strip()):
        return None
    content = run(["git", "-C", str(repo), "show", f"HEAD:{relative}"], check=False)
    if content.returncode != 0:
        raise StntError(f"could not read checked-in discovery evidence: {relative}")
    return blob.stdout.strip(), content.stdout


def local_input_metadata(repo: Path, relative: str) -> Dict[str, Any]:
    candidate = repo / relative
    try:
        mode = candidate.lstat().st_mode
    except FileNotFoundError:
        state = "missing"
    except OSError:
        state = "unreadable-metadata"
    else:
        state = "regular-file" if stat.S_ISREG(mode) else "not-regular-file"
    return {"source": relative, "hostState": state}


def _literal_strings(text: str) -> Dict[str, str]:
    return {
        name: value
        for name, _, value in re.findall(
            r"\bconst\s+([A-Za-z_$][\w$]*)\s*=\s*(['\"])([^'\"\r\n]*)\2\s*;", text
        )
    }


def _vite_discovery(repo: Path, name: str, text: str, package: Dict[str, Any]) -> Dict[str, Any]:
    service: Dict[str, Any] = {
        "name": "web",
        "framework": "vite",
        "confidence": "candidate",
        "evidence": [name, "package.json"],
        "healthPath": "/",
        "unresolved": [],
    }
    constants = _literal_strings(text)
    server_match = re.search(r"\bconst\s+server(?:\s*:\s*[^=]+)?\s*=\s*\{(?P<body>.*?)\}\s*;", text, re.DOTALL)
    body = server_match.group("body") if server_match else ""
    host_match = re.search(r"\bhost\s*:\s*(['\"])([^'\"\r\n]+)\1", body)
    port_match = re.search(r"\bport\s*:\s*(\d{1,5})\b", body)
    origin_match = re.search(r"\borigin\s*:\s*(['\"])(https?://[^'\"\r\n]+)\1", body)
    if host_match:
        service["hostname"] = host_match.group(2)
    if port_match and 1 <= int(port_match.group(1)) <= 65535:
        service["port"] = int(port_match.group(1))

    referenced_paths: List[str] = []
    https_assignment = re.search(r"\bserver\.https\s*=\s*\{(?P<body>.*?)\}\s*;", text, re.DOTALL)
    https_body = https_assignment.group("body") if https_assignment else ""
    for argument in re.findall(r"\breadFileSync\s*\(\s*([^,)]+)", https_body):
        token = argument.strip()
        quoted = re.fullmatch(r"(['\"])([^'\"]+)\1", token)
        value = quoted.group(2) if quoted else constants.get(token)
        if value and value.startswith("./") and ".." not in Path(value).parts:
            relative = value[2:]
            if relative not in referenced_paths:
                referenced_paths.append(relative)

    conditional_https = bool(re.search(r"\bserver\.https\s*=", text))
    direct_https = bool(re.search(r"\bhttps\s*:\s*true\b", body))
    protocol: Optional[str] = None
    if origin_match:
        scheme, hostname, port = parse_service_url(origin_match.group(2))
        service.update({"origin": origin_match.group(2).rstrip("/"), "hostname": hostname, "port": port})
        protocol = scheme
        service["confidence"] = "confirmed"
    elif direct_https:
        protocol = "https"
    elif conditional_https:
        protocol = "https"
        service["unresolved"].append("HTTPS is conditional on the referenced local certificate files")
    else:
        protocol = "http"
    service["protocol"] = protocol
    if "hostname" in service and "port" in service:
        service["origin"] = f"{protocol}://{service['hostname']}:{service['port']}"
    else:
        service["unresolved"].append("No complete literal Vite hostname and exact port were found")

    package_manager = package.get("packageManager") if isinstance(package, dict) else None
    manager = package_manager.split("@", 1)[0] if isinstance(package_manager, str) else None
    scripts = package.get("scripts") if isinstance(package, dict) else None
    script = "dev" if isinstance(scripts, dict) and isinstance(scripts.get("dev"), str) else None
    if manager and script:
        service["argv"] = [manager, script, "--host", "0.0.0.0", "--strictPort"]
        service["unresolved"].append("Confirm the proposed command and sandbox bind override")
    else:
        service["unresolved"].append("No package-manager dev command could be proposed")

    service["localInputs"] = []
    for relative in referenced_paths:
        item = local_input_metadata(repo, relative)
        item.update({"exposure": "read-only-link", "consumer": "web", "confidence": "confirmed-reference"})
        service["localInputs"].append(item)
    return service


def discover_repository(repo: Path) -> Dict[str, Any]:
    """Perform static discovery using only Git object reads and local-input metadata."""
    evidence: Dict[str, Dict[str, Any]] = {}

    def read(name: str, role: str) -> Optional[str]:
        found = committed_file(repo, name)
        if found is None:
            return None
        blob_id, content = found
        entry = evidence.setdefault(name, {"blobID": blob_id, "roles": []})
        if role not in entry["roles"]:
            entry["roles"].append(role)
        return content

    package_text = read("package.json", "package-metadata")
    package: Dict[str, Any] = {}
    if package_text is not None:
        try:
            loaded = json.loads(package_text)
        except json.JSONDecodeError as error:
            raise StntError("checked-in package.json is malformed; static discovery stopped") from error
        if not isinstance(loaded, dict):
            raise StntError("checked-in package.json must contain a JSON object")
        package = loaded

    toolchain: Dict[str, Any]
    flake = read("flake.nix", "toolchain")
    if flake is not None:
        lock = read("flake.lock", "toolchain-lock")
        supported = bool(re.search(r"\bdevShells(?:\.[A-Za-z0-9_-]+)?\.default\s*=|\bdevShells\.default\s*=", flake))
        flake_domains: List[str] = []
        if lock is not None:
            try:
                lock_value = json.loads(lock)
            except json.JSONDecodeError as error:
                raise StntError("checked-in flake.lock is malformed; static discovery stopped") from error
            nodes = lock_value.get("nodes") if isinstance(lock_value, dict) else None
            if not isinstance(nodes, dict):
                raise StntError("checked-in flake.lock has an unsupported shape")
            locked_types = {
                node.get("locked", {}).get("type")
                for node in nodes.values()
                if isinstance(node, dict) and isinstance(node.get("locked"), dict)
            }
            if "github" in locked_types:
                flake_domains.extend(GITHUB_FLAKE_DOMAINS)
        toolchain = {
            "provider": "nix",
            "declaration": "flake.nix",
            "lock": "flake.lock" if lock is not None else None,
            "confidence": "confirmed" if supported and lock is not None else "candidate",
            "unresolved": [] if supported and lock is not None else ["Confirm that the root flake exposes a supported default development shell and lock"],
            "bootstrap": {
                "version": NIX_VERSION,
                "url": NIX_AARCH64_LINUX_URL,
                "sha256": NIX_AARCH64_LINUX_SHA256,
            },
            "requiredDomains": sorted(set(NIX_BOOTSTRAP_DOMAINS + flake_domains)),
        }
    else:
        versions = read(".tool-versions", "toolchain")
        if versions is not None:
            declarations = []
            for line in versions.splitlines():
                stripped = line.split("#", 1)[0].strip()
                if stripped:
                    declarations.append(stripped.split())
            toolchain = {
                "provider": "asdf",
                "declaration": ".tool-versions",
                "versions": declarations,
                "confidence": "candidate",
                "unresolved": ["Confirm supported, pinned asdf plugin implementations before provisioning"],
            }
        else:
            toolchain = {
                "provider": "package",
                "declaration": "package.json" if package_text is not None else None,
                "packageManager": package.get("packageManager"),
                "engines": package.get("engines", {}),
                "confidence": "candidate",
                "unresolved": ["No supported Nix or asdf declaration was selected"],
            }

    setup: List[Dict[str, Any]] = []
    package_manager = package.get("packageManager")
    manager = package_manager.split("@", 1)[0] if isinstance(package_manager, str) else None
    lock_commands = {
        "yarn": ("yarn.lock", ["yarn", "install", "--immutable"]),
        "pnpm": ("pnpm-lock.yaml", ["pnpm", "install", "--frozen-lockfile"]),
        "npm": ("package-lock.json", ["npm", "ci"]),
    }
    if manager in lock_commands:
        lock_name, argv = lock_commands[manager]
        if read(lock_name, "setup-lock") is not None:
            setup.append({"argv": argv, "confidence": "candidate", "evidence": ["package.json", lock_name]})
            if "requiredDomains" in toolchain:
                toolchain["requiredDomains"] = sorted(set(
                    toolchain["requiredDomains"] + PACKAGE_MANAGER_DOMAINS.get(manager, [])
                ))

    services: List[Dict[str, Any]] = []
    for name in ("vite.config.ts", "vite.config.js", "vite.config.mts", "vite.config.mjs"):
        vite = read(name, "service")
        if vite is not None:
            scripts = package.get("scripts", {})
            dependencies: Dict[str, Any] = {}
            for field in ("dependencies", "devDependencies"):
                declared = package.get(field, {})
                if isinstance(declared, dict):
                    dependencies.update(declared)
            if (isinstance(scripts, dict) and any(isinstance(value, str) and re.search(r"(^|\s)vite(?:\s|$)", value) for value in scripts.values())) or "vite" in dependencies:
                services.append(_vite_discovery(repo, name, vite, package))
            break

    env_example = read(".env.example", "local-input-candidate")
    local_inputs = [item for service in services for item in service.pop("localInputs", [])]
    if env_example is not None and services:
        item = local_input_metadata(repo, ".env")
        item.update({
            "exposure": "service-env",
            "consumer": services[0]["name"],
            "confidence": "candidate",
            "evidence": ".env.example",
        })
        local_inputs.insert(0, item)

    return {
        "toolchain": toolchain,
        "setup": setup,
        "services": services,
        "localInputs": local_inputs,
        "secretCapabilities": [],
        "network": {
            "domains": list(toolchain.get("requiredDomains", [])),
            "unresolved": ["Confirm this complete Nix bootstrap, locked-flake, and binary-cache egress allowlist"],
        },
        "resources": {
            "cpus": 4,
            "memoryGiB": 8,
            "unresolved": ["Confirm the initial per-sandbox resource allocation"],
        },
        "evidence": evidence,
    }


def build_profile(repo: Path) -> Dict[str, Any]:
    identity = repository_identity(repo)
    discovery = discover_repository(repo)
    if isinstance(identity["remote"], str) and identity["remote"].startswith("github.com/"):
        discovery["secretCapabilities"] = [dict(GITHUB_PUSH_CAPABILITY)]
        discovery["network"]["domains"] = sorted(set(
            [*discovery["network"]["domains"], "github.com:443"]
        ))
    return {
        "schemaVersion": PROFILE_SCHEMA_VERSION,
        "status": "reviewed",
        "repository": identity,
        "reviewedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        **discovery,
    }


def review_profile(profile: Dict[str, Any], *, command: str = "init") -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise StntError(
            f"repository configuration is not guessed non-interactively; "
            f"run stnt {command} in an interactive terminal"
        )
    stages = [
        ("Toolchain and setup commands", {"toolchain": profile["toolchain"], "setup": profile["setup"]}, [profile["toolchain"], *profile["setup"]]),
        ("Service URL, port, and commands", {"services": profile["services"]}, profile["services"]),
        ("Local inputs and secret capabilities", {"localInputs": profile["localInputs"], "secretCapabilities": profile["secretCapabilities"]}, profile["localInputs"]),
        ("Network", profile["network"], [profile["network"]]),
        ("Resources", profile["resources"], [profile["resources"]]),
    ]
    print(f"Repository: {profile['repository']['path']}")
    print(f"Identity: {profile['repository']['remote'] or '(no normalized remote)'}")
    print("Discovery was static: no project config, script, hook, Nix, Node, or package command was executed.")
    for title, value, approved_items in stages:
        print(f"\n{title}\n{json.dumps(value, indent=2, sort_keys=True)}")
        try:
            accepted = input("Approve this stage? [y/N] ").strip().lower()
        except EOFError as error:
            raise StntError("configuration review cancelled; repository profile was not changed") from error
        if accepted not in {"y", "yes"}:
            raise StntError("configuration review cancelled; repository profile was not changed")
        for item in approved_items:
            if isinstance(item, dict) and "unresolved" in item:
                item["unresolved"] = []


def validate_profile(value: Any, path: Path, identity: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(value, dict) or value.get("schemaVersion") != PROFILE_SCHEMA_VERSION:
        raise StntError(f"repository profile is invalid or unsupported: {path}")
    repository_value = value.get("repository")
    if not isinstance(repository_value, dict) or repository_value != identity:
        raise StntError(f"repository profile identity collision or mismatch: {path}")
    if value.get("status") not in {"reviewed", "active"} or not isinstance(value.get("evidence"), dict):
        raise StntError(f"repository profile is invalid or unsupported: {path}")
    try:
        datetime.fromisoformat(value["reviewedAt"].replace("Z", "+00:00"))
    except (KeyError, AttributeError, ValueError) as error:
        raise StntError(f"repository profile has an invalid review timestamp: {path}") from error
    for relative, evidence in value["evidence"].items():
        if (not isinstance(relative, str) or not relative or "/" in relative or
                not isinstance(evidence, dict) or
                not re.fullmatch(r"[0-9a-f]{40,64}", str(evidence.get("blobID", ""))) or
                not isinstance(evidence.get("roles"), list) or
                any(not isinstance(role, str) or not role for role in evidence["roles"])):
            raise StntError(f"repository profile has invalid evidence: {path}")
    toolchain = value.get("toolchain")
    if not isinstance(toolchain, dict) or toolchain.get("provider") not in {"nix", "asdf", "package"}:
        raise StntError(f"repository profile has an invalid toolchain: {path}")
    setup, services = value.get("setup"), value.get("services")
    if not isinstance(setup, list) or not isinstance(services, list):
        raise StntError(f"repository profile has invalid command collections: {path}")
    if len(services) > 1:
        raise StntError(f"repository profile contains more than one service: {path}")
    for command in [*setup, *services]:
        if not isinstance(command, dict):
            raise StntError(f"repository profile has an invalid command: {path}")
        argv = command.get("argv")
        if argv is not None and (not isinstance(argv, list) or not argv or any(not isinstance(argument, str) or not argument for argument in argv)):
            raise StntError(f"repository profile has an invalid argument array: {path}")
    local_inputs, secret_capabilities = value.get("localInputs"), value.get("secretCapabilities")
    if not isinstance(local_inputs, list) or not isinstance(secret_capabilities, list):
        raise StntError(f"repository profile has invalid capabilities: {path}")
    for item in local_inputs:
        if not isinstance(item, dict) or not isinstance(item.get("source"), str):
            raise StntError(f"repository profile has an invalid local input: {path}")
        source = Path(item["source"])
        if source.is_absolute() or ".." in source.parts or item["source"] in {"", "."}:
            raise StntError(f"repository profile local input escapes the repository: {path}")
        if not services and (item.get("exposure") == "service-env" or "consumer" in item):
            raise StntError(f"repository profile has a service-only local input without a service: {path}")
    allowed_capability_fields = {"name", "provider", "reference", "providerReference", "capabilityID", "consumer", "lifetime"}
    if any(not isinstance(capability, dict) or not set(capability) <= allowed_capability_fields for capability in secret_capabilities):
        raise StntError(f"repository profile has invalid secret capabilities: {path}")
    network, resources = value.get("network"), value.get("resources")
    if (not isinstance(network, dict) or not isinstance(network.get("domains"), list) or
            any(not isinstance(domain, str) for domain in network["domains"]) or
            not isinstance(resources, dict)):
        raise StntError(f"repository profile has invalid network or resources: {path}")
    return value


def load_profile(repo: Path) -> tuple[Path, Dict[str, Any]]:
    identity = repository_identity(repo)
    path = profile_path(identity)
    if not path.exists():
        raise StntError("no repository profile exists; run: stnt init")
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise StntError(f"repository profile is unreadable: {path}") from error
    return path, validate_profile(value, path, identity)


def optional_profile(repo: Path) -> Optional[tuple[Path, Dict[str, Any]]]:
    identity = repository_identity(repo)
    path = profile_path(identity)
    if not path.exists():
        return None
    return load_profile(repo)


def profile_drift(repo: Path, profile: Dict[str, Any]) -> Dict[str, str]:
    drift: Dict[str, str] = {}
    for relative, evidence in sorted(profile["evidence"].items()):
        current = committed_file(repo, relative)
        if current is None:
            drift[relative] = "missing"
        elif not isinstance(evidence, dict) or current[0] != evidence.get("blobID"):
            drift[relative] = "changed"
        else:
            drift[relative] = "unchanged"
    return drift


def redact_profile(profile: Dict[str, Any]) -> Dict[str, Any]:
    redacted = json.loads(json.dumps(profile))
    capabilities = []
    for capability in redacted.get("secretCapabilities", []):
        if isinstance(capability, dict):
            visible = {
                field: capability[field]
                for field in ("name", "provider", "consumer", "lifetime")
                if field in capability
            }
            visible["capability"] = "<redacted>"
            capabilities.append(visible)
        else:
            capabilities.append("<redacted>")
    redacted["secretCapabilities"] = capabilities
    return redacted


def current_local_input_status(repo: Path, profile: Dict[str, Any]) -> Dict[str, str]:
    statuses: Dict[str, str] = {}
    for item in profile.get("localInputs", []):
        if isinstance(item, dict) and isinstance(item.get("source"), str):
            statuses[item["source"]] = local_input_metadata(repo, item["source"])["hostState"]
    return statuses


def profile_change_summary(
    repo: Path,
    current: Dict[str, Any],
    proposed: Dict[str, Any],
) -> Dict[str, Any]:
    def comparable(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: comparable(item)
                for key, item in value.items()
                if key != "unresolved"
            }
        if isinstance(value, list):
            return [comparable(item) for item in value]
        return value

    current_approval = profile_approval(current)
    proposed_approval = profile_approval(proposed)
    sections = (
        "evidence", "toolchain", "setup", "services", "localInputs",
        "secretCapabilities", "network", "resources",
    )
    changed = [
        section for section in sections
        if comparable(current_approval[section]) != comparable(proposed_approval[section])
    ]
    if current.get("status") != "reviewed" or any(
        field in current for field in ("activeApprovalDigest", "proofs")
    ):
        changed.insert(0, "profileState")
    return {
        "evidenceDrift": profile_drift(repo, current),
        "changedSections": changed,
    }


def command_profile_review(command: str) -> int:
    repo = configuration_repository()
    ensure_state_layout()
    with repository_lock(repo):
        identity = repository_identity(repo)
        path = profile_path(identity)
        if command == "init" and path.exists():
            raise StntError(f"repository profile already exists; review changes with: stnt reconfigure")
        current = load_profile(repo)[1] if command == "reconfigure" else None
        with timed(f"{command}.discovery", "discovering repository configuration"):
            profile = build_profile(repo)
        if current is not None:
            summary = profile_change_summary(repo, current, profile)
            print("Current profile drift and proposed capability changes:")
            print(json.dumps(summary, indent=2, sort_keys=True))
            if not summary["changedSections"] and all(
                state == "unchanged" for state in summary["evidenceDrift"].values()
            ):
                print("Repository profile is unchanged; no review or write was needed.")
                return 0
        # The review is interactive. A live progress line would repeatedly
        # overwrite input() prompts while the command waits for approval.
        with timed(f"{command}.review"):
            review_profile(profile, command=command)
        with timed(f"{command}.persist", "saving reviewed profile"):
            atomic_write(path, profile, create_only=command == "init")
    timing_milestone(f"{command}.reviewed")
    action = "saved" if command == "init" else "reconfigured"
    print(f"Reviewed repository profile {action} atomically: {path}")
    print("The profile governs future workspace creation; retained workspaces keep their durable provision plans.")
    return 0


def command_init() -> int:
    return command_profile_review("init")


def command_reconfigure() -> int:
    return command_profile_review("reconfigure")


def command_config_show() -> int:
    repo = configuration_repository()
    path, profile = load_profile(repo)
    output = redact_profile(profile)
    output["profilePath"] = str(path)
    output["drift"] = profile_drift(repo, profile)
    output["localInputStatus"] = current_local_input_status(repo, profile)
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def profile_approval(profile: Dict[str, Any]) -> Dict[str, Any]:
    local_inputs = []
    for item in profile["localInputs"]:
        local_inputs.append({key: value for key, value in item.items() if key != "hostState"})
    return {
        "schemaVersion": profile["schemaVersion"],
        "repository": profile["repository"],
        "evidence": profile["evidence"],
        "toolchain": profile["toolchain"],
        "setup": profile["setup"],
        "services": profile["services"],
        "localInputs": local_inputs,
        "secretCapabilities": profile["secretCapabilities"],
        "network": profile["network"],
        "resources": profile["resources"],
    }


def github_push_remote(profile: Dict[str, Any]) -> Optional[Dict[str, str]]:
    capabilities = profile.get("secretCapabilities")
    if not isinstance(capabilities, list):
        raise StntError("repository profile has invalid secret capabilities")
    if not capabilities:
        return None
    if capabilities != [GITHUB_PUSH_CAPABILITY]:
        raise StntError("repository profile contains an unsupported secret capability")
    remote = profile.get("repository", {}).get("remote")
    if not isinstance(remote, str) or not re.fullmatch(
        r"github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", remote
    ):
        raise StntError("GitHub read/write capability requires an exact github.com repository remote")
    return {
        "normalized": remote,
        "httpsURL": f"https://{remote}.git",
    }


def committed_blob_at(repo: Path, commit: str, relative: str) -> Optional[str]:
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or "/" in relative:
        raise StntError("invalid source-bound profile evidence request")
    blob = run(["git", "-C", str(repo), "rev-parse", f"{commit}:{relative}"], check=False)
    value = blob.stdout.strip()
    return value if blob.returncode == 0 and re.fullmatch(r"[0-9a-f]{40,64}", value) else None


def require_profile_evidence(repo: Path, profile: Dict[str, Any], source_sha: str) -> None:
    changed = [
        relative
        for relative, evidence in sorted(profile["evidence"].items())
        if committed_blob_at(repo, source_sha, relative) != evidence["blobID"]
    ]
    if changed:
        raise StntError(
            "reviewed profile evidence does not match the selected source commit "
            f"({', '.join(changed)}); rerun stnt reconfigure from that source or choose a matching --from branch"
        )


def _has_unresolved(value: Any) -> bool:
    if isinstance(value, dict):
        return bool(value.get("unresolved")) or any(_has_unresolved(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_unresolved(item) for item in value)
    return False


def provision_plan(repo: Path, profile: Dict[str, Any], source_sha: str) -> Dict[str, Any]:
    require_profile_evidence(repo, profile, source_sha)
    if _has_unresolved(profile_approval(profile)):
        raise StntError("repository profile still has unresolved execution choices; rerun interactively: stnt reconfigure")
    toolchain = profile["toolchain"]
    expected_bootstrap = {
        "version": NIX_VERSION,
        "url": NIX_AARCH64_LINUX_URL,
        "sha256": NIX_AARCH64_LINUX_SHA256,
    }
    if toolchain.get("provider") != "nix" or toolchain.get("bootstrap") != expected_bootstrap:
        raise StntError("only the reviewed pinned Nix aarch64-linux provider is provisionable in this slice")
    domains = profile["network"].get("domains")
    required_domains = toolchain.get("requiredDomains")
    if (not isinstance(domains, list) or sorted(set(domains)) != sorted(set(required_domains or [])) or
            any(not re.fullmatch(r"(?:\*\.)?[A-Za-z0-9.-]+:\d{1,5}", domain) for domain in domains)):
        raise StntError("reviewed network domains do not exactly match the selected Nix toolchain requirements; rerun: stnt reconfigure")
    cpus, memory = profile["resources"].get("cpus"), profile["resources"].get("memoryGiB")
    if (isinstance(cpus, bool) or not isinstance(cpus, int) or not 1 <= cpus <= 64 or
            isinstance(memory, bool) or not isinstance(memory, int) or not 1 <= memory <= 32):
        raise StntError("reviewed resources require integer cpus and memoryGiB values")
    services = profile["services"]
    if len(services) > 1:
        raise StntError("reviewed profile contains more than one service")
    approved_local_inputs = profile_approval(profile)["localInputs"]
    if not services and any(
        item.get("exposure") == "service-env" or "consumer" in item
        for item in approved_local_inputs
    ):
        raise StntError("reviewed profile has a service-only local input without a service")
    setup = [command["argv"] for command in profile["setup"]]
    git_remote = github_push_remote(profile)
    if git_remote is not None and "github.com:443" not in domains:
        raise StntError("GitHub read/write capability requires reviewed github.com:443 egress")
    plan = {
        "schemaVersion": 1,
        "sourceSHA": source_sha,
        "profileApprovalDigest": canonical_digest(profile_approval(profile)),
        "toolchain": toolchain,
        "setup": setup,
        "localInputs": approved_local_inputs,
        "secretCapabilities": profile["secretCapabilities"],
        "gitRemote": git_remote,
        "networkDomains": sorted(domains),
        "resources": {"cpus": cpus, "memoryGiB": memory},
    }
    if services:
        service = services[0]
        argv = service.get("argv")
        origin = service.get("origin")
        if not isinstance(argv, list) or not origin:
            raise StntError("reviewed service requires an argument-array command and canonical origin")
        scheme, hostname, port = parse_service_url(origin)
        if service.get("protocol") != scheme or service.get("hostname") != hostname or service.get("port") != port:
            raise StntError("reviewed service origin fields disagree")
        plan["service"] = {
            "argv": argv,
            "origin": origin,
            "port": port,
            "healthPath": service.get("healthPath", "/"),
        }
    plan["digest"] = canonical_digest(plan)
    return plan


def nix_argv(argv: List[str]) -> List[str]:
    return [
        "/home/agent/.nix-profile/bin/nix",
        "--extra-experimental-features", "nix-command flakes",
        "develop", "--no-write-lock-file", "--command", *argv,
    ]


def render_profile_kit(plan: Dict[str, Any]) -> str:
    try:
        base = KIT_SPEC.read_text()
    except OSError as error:
        raise StntError(f"bundled Amp kit is unreadable: {KIT_SPEC}") from error
    domain_anchor = '    - "*.ampcode.com:443"\n'
    credential_anchor = '          format: "Bearer %s"\n'
    command_anchor = "      description: Install the host-tested Amp CLI version\n"
    if domain_anchor not in base or credential_anchor not in base or command_anchor not in base:
        raise StntError("bundled Amp kit shape changed; profile kit generation refused")
    capabilities = plan.get("secretCapabilities")
    if not isinstance(capabilities, list) or any(capability != GITHUB_PUSH_CAPABILITY for capability in capabilities):
        raise StntError("reviewed profile contains an unsupported secret capability")
    domains = "".join(f"    - {json.dumps(domain)}\n" for domain in plan["networkDomains"])
    credentials = ""
    if GITHUB_PUSH_CAPABILITY in capabilities:
        credentials = (
            "  - service: github\n"
            "    description: GitHub read/write access for Git over HTTPS\n"
            "    required: true\n"
            "    apiKey:\n"
            "      name: GITHUB_TOKEN\n"
            "      inject:\n"
            "        - domain: github.com\n"
            "          header: Authorization\n"
            "          scheme: basic\n"
            "          username: x-access-token\n"
        )
    bootstrap = plan["toolchain"]["bootstrap"]
    decompress = (
        "import lzma, shutil, sys; "
        "source=lzma.open(sys.argv[1], 'rb'); destination=open(sys.argv[2], 'wb'); "
        "shutil.copyfileobj(source, destination); source.close(); destination.close()"
    )
    install = (
        "set -euo pipefail; work=$(mktemp -d); trap 'rm -rf \"$work\"' EXIT; "
        f"curl -fL {shell_quote(bootstrap['url'])} -o \"$work/nix.tar.xz\"; "
        f"printf '%s  %s\\n' {shell_quote(bootstrap['sha256'])} \"$work/nix.tar.xz\" | sha256sum -c -; "
        f"python3 -c {shell_quote(decompress)} \"$work/nix.tar.xz\" \"$work/nix.tar\"; "
        "tar -xf \"$work/nix.tar\" -C \"$work\"; "
        "\"$work\"/nix-*/install --no-daemon --yes --no-channel-add --no-modify-profile"
    )
    configure = (
        "mkdir -p /home/agent/.config/nix && "
        "printf '%s\\n' 'experimental-features = nix-command flakes' "
        "> /home/agent/.config/nix/nix.conf"
    )
    commands = (
        "    - command: \"mkdir -p /nix && chown 1000:1000 /nix\"\n"
        "      user: \"root\"\n"
        "      description: Prepare the persistent single-user Nix store\n"
        f"    - command: {json.dumps(install)}\n"
        "      user: \"1000\"\n"
        f"      description: Install checksum-pinned Nix {NIX_VERSION}\n"
        f"    - command: {json.dumps(configure)}\n"
        "      user: \"1000\"\n"
        "      description: Enable reviewed Nix command and flake interfaces\n"
    )
    return base.replace(domain_anchor, domain_anchor + domains).replace(
        credential_anchor, credential_anchor + credentials
    ).replace(command_anchor, command_anchor + commands)


def ensure_profile_kit(plan: Dict[str, Any]) -> tuple[Path, str]:
    rendered = render_profile_kit(plan)
    digest = hashlib.sha256(rendered.encode()).hexdigest()
    path = config_root() / "kits" / digest / "spec.yaml"
    if path.exists():
        try:
            existing = path.read_text()
        except OSError as error:
            raise StntError(f"generated profile kit is unreadable: {path}") from error
        if existing != rendered:
            raise StntError(f"generated profile kit digest collision: {path}")
    else:
        atomic_write_text(path, rendered)
    validated = run([str(RUNTIME), "validate-kit-path", str(path.parent)], check=False)
    if validated.returncode != 0:
        raise StntError("generated profile kit failed Docker validation")
    return path.parent, digest


def remove_state(path: Path) -> None:
    path.unlink()
    fsync_directory(path.parent)


def load_state(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise StntError(f"session state is unreadable ({path}): {error}") from error
    common_required = {
        "schemaVersion", "runtime", "sandbox", "repositoryPath",
        "baseSHA", "baseBranch", "branch", "preservationBranch", "status", "createdAt",
    }
    if (not isinstance(value, dict) or value.get("schemaVersion") not in {
            LEGACY_SCHEMA_VERSION, SCHEMA_VERSION
    } or not common_required <= value.keys()):
        raise StntError(f"session state is invalid or unsupported: {path}")
    if value["schemaVersion"] == SCHEMA_VERSION:
        if not {"workspaceID", "lifecycleOwner"} <= value.keys():
            raise StntError(f"session state is invalid or unsupported: {path}")
        if not WORKSPACE_RE.fullmatch(str(value["workspaceID"])):
            raise StntError(f"session state contains an invalid workspace ID: {path}")
        if value["lifecycleOwner"] not in {"thread", "workspace"}:
            raise StntError(f"session state contains an invalid lifecycle owner: {path}")
        if value["lifecycleOwner"] == "thread" and "threadID" not in value:
            raise StntError(f"thread-owned session state is missing its Amp thread ID: {path}")
    elif "threadID" not in value:
        raise StntError(f"legacy session state is missing its Amp thread ID: {path}")
    strings = ("runtime", "sandbox", "repositoryPath", "baseSHA", "baseBranch", "branch", "preservationBranch", "status", "createdAt")
    if any(not isinstance(value.get(field), str) or not value[field] for field in strings):
        raise StntError(f"session state contains an invalid string field: {path}")
    if "threadID" in value and not THREAD_RE.fullmatch(str(value["threadID"])):
        raise StntError(f"session state contains an invalid thread ID: {path}")
    if value["runtime"] != "docker-sandbox" or not re.fullmatch(r"[0-9a-f]{40}", value["baseSHA"]):
        raise StntError(f"session state contains an invalid runtime or base SHA: {path}")
    if not Path(value["repositoryPath"]).is_absolute():
        raise StntError(f"session state contains an invalid path or port: {path}")
    if "sandboxPort" in value and (
        isinstance(value["sandboxPort"], bool) or
        not isinstance(value["sandboxPort"], int) or
        not (1 <= value["sandboxPort"] <= 65535)
    ):
        raise StntError(f"session state contains an invalid path or port: {path}")
    try:
        datetime.fromisoformat(value["createdAt"].replace("Z", "+00:00"))
    except ValueError as error:
        raise StntError(f"session state contains an invalid timestamp: {path}") from error
    if "sandboxID" in value and (not isinstance(value["sandboxID"], str) or not value["sandboxID"]):
        raise StntError(f"session state contains an invalid sandbox ID: {path}")
    source_fields = ("sourceBranch", "sourceSHA")
    if any(field in value for field in source_fields):
        if any(not isinstance(value.get(field), str) or not value[field] for field in source_fields):
            raise StntError(f"session state contains an incomplete source branch identity: {path}")
        if not re.fullmatch(r"[0-9a-f]{40}", value["sourceSHA"]):
            raise StntError(f"session state contains an invalid source branch SHA: {path}")
        if run(["git", "check-ref-format", "--branch", value["sourceBranch"]], check=False).returncode != 0:
            raise StntError(f"session state contains an invalid source branch: {path}")
    if value["status"] not in {
        "creating", "starting", "active", "paused", "archived", "ambiguous",
    }:
        raise StntError(f"session state contains an invalid lifecycle status: {path}")
    editor_authorization = value.get("editorAuthorization")
    if editor_authorization is not None and (
        value["status"] != "active" or
        not isinstance(editor_authorization, str) or
        not re.fullmatch(r"[0-9a-f]{32}", editor_authorization)
    ):
        raise StntError(f"session state contains an invalid editor authorization: {path}")
    profile_fields = (
        "profilePlan", "profilePlanDigest", "profileApprovalDigest", "profileKitDigest",
    )
    has_profile = any(field in value for field in profile_fields)
    if "serviceCommand" in value:
        if has_profile or "serviceArgv" in value or "sandboxPort" not in value:
            raise StntError(f"session state mixes incompatible service or profile fields: {path}")
        if not isinstance(value["serviceCommand"], str) or not value["serviceCommand"]:
            raise StntError(f"session state contains an invalid service command: {path}")
        health_path = value.get("healthPath", "/")
        if not isinstance(health_path, str) or not re.fullmatch(r"/\S*", health_path):
            raise StntError(f"session state contains an invalid service health path: {path}")
        if "serviceURL" in value:
            try:
                _, _, port = parse_service_url(value["serviceURL"])
            except StntError as error:
                raise StntError(f"session state contains an invalid service URL: {path}") from error
            if value["sandboxPort"] != port:
                raise StntError(f"session state service URL and sandbox port disagree: {path}")
    elif has_profile:
        if any(field not in value for field in profile_fields):
            raise StntError(f"session state contains incomplete profile bindings: {path}")
        digests = ("profilePlanDigest", "profileApprovalDigest", "profileKitDigest")
        if any(not re.fullmatch(r"[0-9a-f]{64}", str(value.get(field, ""))) for field in digests):
            raise StntError(f"session state contains an invalid profile digest: {path}")
        plan = value.get("profilePlan")
        if (not isinstance(plan, dict) or plan.get("digest") != value["profilePlanDigest"] or
                canonical_digest({key: item for key, item in plan.items() if key != "digest"}) != plan["digest"] or
                plan.get("profileApprovalDigest") != value["profileApprovalDigest"] or
                plan.get("sourceSHA") != value.get("sourceSHA")):
            raise StntError(f"session state contains an invalid or mismatched profile plan: {path}")
        if "service" in plan:
            service = plan["service"]
            argv = service.get("argv") if isinstance(service, dict) else None
            origin = service.get("origin") if isinstance(service, dict) else None
            health_path = service.get("healthPath", "/") if isinstance(service, dict) else None
            if (not isinstance(argv, list) or not argv or
                    any(not isinstance(argument, str) or not argument for argument in argv) or
                    not isinstance(origin, str) or
                    not isinstance(health_path, str) or not re.fullmatch(r"/\S*", health_path)):
                raise StntError(f"session state contains an invalid profile service: {path}")
            try:
                _, _, port = parse_service_url(origin)
            except StntError as error:
                raise StntError(f"session state contains an invalid profile service: {path}") from error
            if (service.get("port") != port or value.get("sandboxPort") != port or
                    value.get("serviceArgv") != nix_argv(argv) or
                    value.get("serviceURL") != origin or
                    value.get("healthPath", "/") != health_path):
                raise StntError(f"session state profile service fields disagree: {path}")
        elif any(field in value for field in ("sandboxPort", "serviceArgv", "serviceURL", "healthPath")):
            raise StntError(f"service-less profile state contains service fields: {path}")
    elif "serviceArgv" in value:
        raise StntError(f"session state contains a profile service without profile bindings: {path}")
    else:
        if "sandboxPort" not in value:
            raise StntError(f"session state contains an invalid path or port: {path}")
    if "serviceURL" in value and "serviceCommand" not in value and not has_profile:
        raise StntError(f"session state contains a service URL without a service command: {path}")
    return value


def load_stack_state(name: str) -> Optional[Dict[str, Any]]:
    path = stack_state_path(name)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise StntError(f"stack state is unreadable ({path}): {error}") from error
    required = {
        "schemaVersion", "name", "profileDigest", "threadID", "runtime", "sandbox",
        "repositories", "ingress", "status", "createdAt",
    }
    if (not isinstance(value, dict) or set(value) - (required | {"sandboxID"}) or
            not required <= set(value) or value.get("schemaVersion") != STACK_SCHEMA_VERSION or
            value.get("name") != name or not THREAD_RE.fullmatch(str(value.get("threadID", ""))) or
            value.get("runtime") != "docker-sandbox" or
            not re.fullmatch(r"[0-9a-f]{64}", str(value.get("profileDigest", ""))) or
            value.get("status") not in {"creating", "paused", "archived", "ambiguous"}):
        raise StntError(f"stack state is invalid or unsupported: {path}")
    if (not isinstance(value.get("sandbox"), str) or not value["sandbox"] or
            ("sandboxID" in value and (not isinstance(value["sandboxID"], str) or not value["sandboxID"])) or
            not isinstance(value.get("repositories"), list) or len(value["repositories"]) != 2 or
            not isinstance(value.get("ingress"), dict)):
        raise StntError(f"stack state contains an invalid identity: {path}")
    for item in value["repositories"]:
        preservation = item.get("preservationBranch") if isinstance(item, dict) else None
        if preservation is not None and (
            not isinstance(preservation, str) or
            run(["git", "check-ref-format", f"refs/heads/{preservation}"], check=False).returncode != 0
        ):
            raise StntError(f"stack state contains an invalid preservation branch: {path}")
    try:
        datetime.fromisoformat(value["createdAt"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise StntError(f"stack state contains an invalid timestamp: {path}") from error
    return value


def load_stack_states() -> List[tuple[Path, Dict[str, Any]]]:
    directory = state_root() / "stacks"
    if not directory.exists():
        return []
    partials = list(directory.glob(".*.json.*"))
    if partials:
        raise StntError(f"partial stack representation exists: {partials[0]}")
    loaded = []
    sandboxes = set()
    for path in sorted(directory.glob("*.json")):
        value = load_stack_state(path.stem)
        if value is None:
            raise StntError(f"stack inventory changed concurrently; retry: {path}")
        if value["sandbox"] in sandboxes:
            raise StntError(f"duplicate sandbox name in stack state: {value['sandbox']}")
        sandboxes.add(value["sandbox"])
        loaded.append((path, value))
    return loaded


def _validate_record_path(path: Path, value: Dict[str, Any]) -> None:
    key = repo_key(Path(value["repositoryPath"]))
    expected_old = f"{key}.json"
    expected = {expected_old}
    if "threadID" in value:
        expected.add(f"{key}--{compact_thread_id(value['threadID'])}.json")
    if value["schemaVersion"] == SCHEMA_VERSION:
        expected.add(f"{key}--{compact_workspace_id(value['workspaceID'])}.json")
    if path.name not in expected:
        raise StntError(f"session filename does not match its repository/workspace identity: {path}")


def load_sessions(repo: Optional[Path] = None) -> List[tuple[Path, Dict[str, Any]]]:
    """Strictly load all records, optionally selecting an exact canonical repo path."""
    directory = state_root() / "sessions"
    if not directory.exists():
        return []
    loaded: List[tuple[Path, Dict[str, Any]]] = []
    thread_ids: Dict[str, Path] = {}
    workspace_ids: Dict[str, Path] = {}
    sandboxes: Dict[str, Path] = {}
    sandbox_ids: Dict[str, Path] = {}
    legacy_repos: Dict[str, Path] = {}
    new_repos: Dict[str, Path] = {}
    partials = list(directory.glob(".*.json.*"))
    if partials:
        raise StntError(f"partial session representation exists: {partials[0]}")
    for path in sorted(directory.glob("*.json")):
        value = load_state(path)
        if value is None:
            raise StntError(f"session inventory changed concurrently; retry: {path}")
        _validate_record_path(path, value)
        tid = value.get("threadID")
        sandbox, repository_path = value["sandbox"], value["repositoryPath"]
        if tid and tid in thread_ids:
            raise StntError(f"duplicate thread ID in session state: {tid}")
        workspace_id = value.get("workspaceID")
        if workspace_id and workspace_id in workspace_ids:
            raise StntError(f"duplicate workspace ID in session state: {workspace_id}")
        if sandbox in sandboxes:
            raise StntError(f"duplicate sandbox name in session state: {sandbox}")
        sandbox_id = value.get("sandboxID")
        if sandbox_id and sandbox_id in sandbox_ids:
            raise StntError(f"duplicate sandbox ID in session state: {sandbox_id}")
        if tid:
            thread_ids[tid] = path
        sandboxes[sandbox] = path
        if workspace_id:
            workspace_ids[workspace_id] = path
        if sandbox_id:
            sandbox_ids[sandbox_id] = path
        key = repo_key(Path(repository_path))
        if "--" in path.stem:
            new_repos[key] = path
        else:
            legacy_repos[key] = path
        loaded.append((path, value))
    conflict = set(legacy_repos) & set(new_repos)
    if conflict:
        raise StntError(f"conflicting legacy/new session representations for repository hash {sorted(conflict)[0]}")
    if repo is not None:
        return [(path, value) for path, value in loaded if value["repositoryPath"] == str(repo)]
    return loaded


def migrate_legacy_session(repo: Path) -> Optional[Path]:
    """Migrate state identity under the repository lock without touching runtime resources."""
    legacy = legacy_session_path(repo)
    suffixed = list((state_root() / "sessions").glob(f"{repo_key(repo)}--*.json"))
    if legacy.exists() and suffixed:
        raise StntError("conflicting legacy/workspace session representations; migration refused")
    candidates = []
    for path in ([legacy] if legacy.exists() else []) + sorted(suffixed):
        value = load_state(path)
        if value is None:
            raise StntError(f"legacy session changed concurrently; retry: {path}")
        if path == legacy or value["schemaVersion"] == LEGACY_SCHEMA_VERSION:
            candidates.append(path)
    migrated = None
    for old in candidates:
        value = load_state(old)
        if value is None:
            raise StntError(f"legacy session changed concurrently; retry: {old}")
        _validate_record_path(old, value)
        if value["repositoryPath"] != str(repo):
            raise StntError("legacy session repository path mismatch; refusing to relink")
        if value["schemaVersion"] == LEGACY_SCHEMA_VERSION:
            value = dict(
                value,
                schemaVersion=SCHEMA_VERSION,
                workspaceID=migrated_workspace_id(value["threadID"]),
                lifecycleOwner="thread",
            )
            atomic_write(old, value)
        target = workspace_session_path(repo, value["workspaceID"])
        if old == target:
            migrated = target
            continue
        if target.exists():
            raise StntError("conflicting legacy/workspace session representations; migration refused")
        if os.environ.get("STNT_TEST_INTERRUPT_AT") == "migration-before-rename":
            raise StntError("synthetic interruption before migration rename")
        os.replace(old, target)
        os.chmod(target, 0o600)
        if os.environ.get("STNT_TEST_INTERRUPT_AT") == "migration-after-rename":
            raise StntError("synthetic interruption after migration rename before directory fsync")
        fsync_directory(target.parent)
        if os.environ.get("STNT_TEST_INTERRUPT_AT") == "migration-after-fsync":
            raise StntError("synthetic interruption after migration directory fsync")
        migrated = target
    return migrated


@contextmanager
def repository_lock(repo: Path) -> Iterator[None]:
    locks = state_root() / "locks"
    lock_path = locks / f"{repo_key(repo)}.lock"
    with lock_path.open("a+") as lock:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise StntError(f"another Stnt invocation owns this repository; retry: stnt") from error
        yield


@contextmanager
def creation_lock() -> Iterator[None]:
    lock_path = state_root() / "locks" / "creation.lock"
    with lock_path.open("a+") as lock:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise StntError("another Stnt invocation is creating a session; retry later") from error
        yield


@contextmanager
def session_lock(identity: str) -> Iterator[None]:
    compact_identity = (
        compact_workspace_id(identity) if WORKSPACE_RE.fullmatch(identity)
        else compact_thread_id(identity)
    )
    lock_path = state_root() / "locks" / f"session-{compact_identity}.lock"
    with lock_path.open("a+") as lock:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise StntError(f"session {identity} is already in use; retry later") from error
        yield


def editor_lock_path(record: Dict[str, Any], purpose: str) -> Path:
    identity = record_lock_identity(record)
    compact_identity = (
        compact_workspace_id(identity) if WORKSPACE_RE.fullmatch(identity)
        else compact_thread_id(identity)
    )
    return state_root() / "locks" / f"editor-{purpose}-{compact_identity}.lock"


@contextmanager
def editor_drain(record: Dict[str, Any], *, exclusive: bool) -> Iterator[None]:
    path = editor_lock_path(record, "drain")
    with path.open("a+") as lock:
        os.chmod(path, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield


class EditorAuthorization:
    def __init__(self, record: Dict[str, Any], path: Path):
        self.record = record
        self.path = path
        self.lock = editor_lock_path(record, "authorization").open("a+")
        os.chmod(self.lock.name, 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self.lock.close()
            raise StntError("editor authorization is already owned by another Stnt lifecycle") from error
        self.generation = uuid.uuid4().hex
        record["status"] = "active"
        record["editorAuthorization"] = self.generation
        try:
            atomic_write(path, record)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if not self.lock.closed:
            fcntl.flock(self.lock, fcntl.LOCK_UN)
            self.lock.close()


def revoke_editor_authorization(
    record: Dict[str, Any], path: Path, authorization: Optional[EditorAuthorization] = None,
) -> None:
    if "editorAuthorization" in record:
        record.pop("editorAuthorization")
        atomic_write(path, record)
    if authorization is not None:
        authorization.close()


def record_lock_identity(record: Dict[str, Any]) -> str:
    return record["workspaceID"] if "workspaceID" in record else record["threadID"]


def record_selector(record: Dict[str, Any]) -> str:
    if record.get("lifecycleOwner") == "workspace":
        return record["workspaceID"]
    return record["threadID"]


@contextmanager
def stack_lock(name: str) -> Iterator[None]:
    stack_profile_path(name)
    lock_path = state_root() / "locks" / f"stack-{name}.lock"
    with lock_path.open("a+") as lock:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise StntError(f"another operation owns stack {name}") from error
        yield


@contextmanager
def lifecycle_gate(*, prune: bool = False) -> Iterator[None]:
    lock_path = state_root() / "locks" / "lifecycle.lock"
    with lock_path.open("a+") as lock:
        os.chmod(lock_path, 0o600)
        mode = fcntl.LOCK_EX if prune else fcntl.LOCK_SH
        try:
            fcntl.flock(lock, mode | fcntl.LOCK_NB)
        except BlockingIOError as error:
            message = (
                "another Stnt lifecycle operation is active; retry prune later"
                if prune else "Stnt prune is active; retry later"
            )
            raise StntError(message) from error
        yield


def thread_status(thread_id: str, *, allow_empty: bool = False) -> str:
    result = run([str(THREADS), "status", thread_id], check=False)
    observed = result.stdout.strip()
    if result.returncode == 0 and observed in {"active", "archived"}:
        return observed
    if result.returncode == 0 and observed == "empty" and allow_empty:
        return "empty"
    if result.returncode == 3:
        exported_status = empty_thread_export_status(thread_id)
        if exported_status == "archived":
            return "archived"
        if exported_status == "empty" and allow_empty:
            return "empty"
    detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
    raise StntError(f"Amp thread {thread_id} cannot be authoritatively reconciled ({detail})")


def empty_thread_export_status(thread_id: str) -> Optional[str]:
    """Resolve a known list-omitted empty thread from authoritative export."""
    exported = run(["amp", "threads", "export", thread_id], check=False)
    if exported.returncode != 0:
        return None
    try:
        value = json.loads(exported.stdout)
    except json.JSONDecodeError:
        return None
    if (
        not isinstance(value, dict) or
        value.get("id") != thread_id or
        ("archived" in value and not isinstance(value["archived"], bool)) or
        not isinstance(value.get("messages"), list) or
        len(value["messages"]) != 0
    ):
        return None
    return "archived" if value.get("archived", False) else "empty"


def runtime_find(sandbox: str) -> Optional[Dict[str, Any]]:
    result = run([str(RUNTIME), "find", sandbox], check=False)
    if result.returncode == 0:
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise StntError("Docker sandbox inventory returned malformed JSON") from error
        if not isinstance(value, dict) or value.get("name") != sandbox:
            raise StntError("Docker sandbox inventory returned an inconsistent identity")
        return value
    if result.returncode == 4:
        return None
    raise StntError(f"Docker sandbox inventory lookup failed: {result.stderr.strip() or result.returncode}")


def repository_default_branch(repo: Path) -> tuple[str, str]:
    remotes = run(["git", "-C", str(repo), "remote"], check=False)
    if remotes.returncode != 0:
        raise StntError("Git remote inventory is unavailable; create explicitly: stnt new --from <local-branch>")
    names = [line for line in remotes.stdout.splitlines() if line]
    if len(names) != len(set(names)):
        raise StntError("Git remote inventory is ambiguous; create explicitly: stnt new --from <local-branch>")
    defaults: List[tuple[str, str]] = []
    for remote in names:
        symbolic = run([
            "git", "-C", str(repo), "symbolic-ref", "--quiet", "--no-recurse",
            f"refs/remotes/{remote}/HEAD",
        ], check=False)
        if symbolic.returncode == 1:
            continue
        prefix = f"refs/remotes/{remote}/"
        target = symbolic.stdout.strip()
        if symbolic.returncode != 0 or not target.startswith(prefix):
            raise StntError(
                f"remote {remote} has malformed default-branch metadata; "
                "create explicitly: stnt new --from <local-branch>"
            )
        branch = target[len(prefix):]
        if run(["git", "check-ref-format", "--branch", branch], check=False).returncode != 0:
            raise StntError(
                f"remote {remote} has an invalid default branch; "
                "create explicitly: stnt new --from <local-branch>"
            )
        defaults.append((remote, branch))
    branches = sorted({branch for _, branch in defaults})
    if not branches:
        raise StntError(
            "no authoritative repository default branch is recorded; "
            "create explicitly: stnt new --from <local-branch>"
        )
    if len(branches) != 1:
        detail = ", ".join(f"{remote}={branch}" for remote, branch in defaults)
        raise StntError(
            f"remote default branches conflict ({detail}); "
            "create explicitly: stnt new --from <local-branch>"
        )
    branch = branches[0]
    source = run([
        "git", "-C", str(repo), "rev-parse", "--verify",
        f"refs/heads/{branch}^{{commit}}",
    ], check=False)
    sha = source.stdout.strip()
    if source.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise StntError(
            f"authoritative default branch {branch} has no exact local branch; "
            f"create explicitly: stnt new --from {branch}"
        )
    return branch, sha


def create_record(
    repo: Path,
    *,
    service_command: Optional[str] = None,
    service_url: Optional[str] = None,
    health_path: str = "/",
    from_branch: Optional[str] = None,
    repository_profile: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if service_url and not service_command:
        raise StntError("--service-url requires --service-command")
    sandbox_port: Optional[int] = parse_service_url(service_url)[2] if service_url else INTERNAL_PORT
    base_sha = run(["git", "-C", str(repo), "rev-parse", "HEAD"]).stdout.strip()
    base_branch = run(["git", "-C", str(repo), "branch", "--show-current"]).stdout.strip()
    if from_branch is None:
        source_branch, source_sha = repository_default_branch(repo)
    else:
        if run(["git", "check-ref-format", "--branch", from_branch], check=False).returncode != 0:
            raise StntError(f"invalid local source branch: {from_branch!r}")
        source = run([
            "git", "-C", str(repo), "rev-parse", "--verify",
            f"refs/heads/{from_branch}^{{commit}}",
        ], check=False)
        if source.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", source.stdout.strip()):
            raise StntError(f"local source branch does not exist: {from_branch}")
        source_branch = from_branch
        source_sha = source.stdout.strip()
    plan = None
    kit_digest = None
    if repository_profile is not None:
        if service_command or service_url:
            raise StntError("reviewed repository profiles cannot be combined with explicit service flags")
        plan = provision_plan(repo, repository_profile, source_sha)
        _, kit_digest = ensure_profile_kit(plan)
        sandbox_port = plan["service"]["port"] if "service" in plan else None
    lifecycle_signals = {signal.SIGHUP, signal.SIGINT, signal.SIGTERM}
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, lifecycle_signals)
    try:
        # Must run while repository -> creation locks are held. Reload globally at
        # the last possible point so generated identities cannot collide across repos.
        existing = load_sessions()
        workspace_id = new_workspace_id()
        compact_id = compact_workspace_id(workspace_id)
        slug = re.sub(r"[^a-z0-9.-]+", "-", repo.name.lower()).strip("-.") or "repository"
        identity = f"{repo_key(repo)[:8]}-{compact_id}"
        record = {
            "schemaVersion": SCHEMA_VERSION,
            "workspaceID": workspace_id,
            "lifecycleOwner": "workspace",
            "runtime": "docker-sandbox",
            "sandbox": f"stnt-{slug[:16]}-{identity}",
            "repositoryPath": str(repo),
            "baseSHA": base_sha,
            "baseBranch": base_branch,
            # Schema-1 compatibility field: for new records this is the clone's
            # initial branch, not a branch Stnt owns or requires after creation.
            "branch": source_branch,
            "preservationBranch": f"stnt-preserved/phase1c-{identity}",
            "status": "creating",
            "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "sourceBranch": source_branch,
            "sourceSHA": source_sha,
        }
        if sandbox_port is not None:
            record["sandboxPort"] = sandbox_port
        if service_command:
            record["serviceCommand"] = service_command
            record["healthPath"] = health_path
            if service_url:
                record["serviceURL"] = service_url
        if plan is not None:
            record["profilePlan"] = plan
            record["profilePlanDigest"] = plan["digest"]
            record["profileApprovalDigest"] = plan["profileApprovalDigest"]
            record["profileKitDigest"] = kit_digest
            if "service" in plan:
                record["serviceArgv"] = nix_argv(plan["service"]["argv"])
                record["serviceURL"] = plan["service"]["origin"]
                record["healthPath"] = plan["service"]["healthPath"]
        identities = ("workspaceID", "sandbox", "preservationBranch")
        for _, prior in existing:
            duplicate = next((field for field in identities if prior.get(field) == record[field]), None)
            if duplicate or workspace_session_path(repo, workspace_id).exists():
                raise StntError(
                    f"workspace identity collision ({duplicate or 'target path'}); "
                    "existing state was retained and no sandbox operation was attempted"
                )
        # No lifecycle signal can open a gap between generating and persisting the ID.
        try:
            atomic_write(workspace_session_path(repo, workspace_id), record, create_only=True)
        except Exception as error:
            raise StntError(
                f"could not durably record newly created workspace {workspace_id}; "
                "no sandbox operation was attempted"
            ) from error
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
    return record


def require_sandbox(record: Dict[str, Any], *, allow_unrecorded_id: bool = False) -> Dict[str, Any]:
    found = runtime_find(record["sandbox"])
    if found is None:
        raise StntError(
            f"session {record_selector(record)} is retained but sandbox {record['sandbox']} is missing; "
            "do not create a replacement"
        )
    workspaces = found.get("workspaces")
    if not isinstance(workspaces, list) or record["repositoryPath"] not in workspaces:
        raise StntError(f"sandbox workspace identity changed for {record['sandbox']}; retained")
    expected_id = record.get("sandboxID")
    actual_id = found.get("id")
    if expected_id and actual_id != expected_id:
        raise StntError(f"sandbox identity changed for {record['sandbox']}; retained")
    if not expected_id and not allow_unrecorded_id:
        raise StntError(f"sandbox ID is absent from durable state for {record['sandbox']}; retained")
    return found


def verify_profile_record(record: Dict[str, Any]) -> tuple[Path, str]:
    repo = Path(record["repositoryPath"])
    plan = record.get("profilePlan")
    if (not isinstance(plan, dict) or plan.get("digest") != record.get("profilePlanDigest") or
            canonical_digest({key: value for key, value in plan.items() if key != "digest"}) != plan.get("digest") or
            plan.get("profileApprovalDigest") != record.get("profileApprovalDigest") or
            plan.get("sourceSHA") != record.get("sourceSHA")):
        raise StntError("durable profile provision plan changed or is malformed; retained")
    kit_path, kit_digest = ensure_profile_kit(plan)
    local_inputs = plan.get("localInputs")
    if not isinstance(local_inputs, list):
        raise StntError("durable profile provision plan has invalid local inputs; retained")
    for item in local_inputs:
        if not isinstance(item, dict) or not isinstance(item.get("source"), str):
            raise StntError("durable profile provision plan has invalid local inputs; retained")
        metadata = local_input_metadata(repo, item["source"])
        if metadata["hostState"] != "regular-file":
            raise StntError(
                f"approved local input {item['source']} is {metadata['hostState']}; retained without provisioning"
            )
    return kit_path, kit_digest


def profile_environment_file(plan: Dict[str, Any]) -> str:
    if "service" not in plan:
        raise StntError("service-less profile has no process environment")
    files = [
        item["source"] for item in plan["localInputs"]
        if item.get("exposure") == "service-env" and item.get("consumer") == "web"
    ]
    if len(files) > 1:
        raise StntError("profile has multiple process environment inputs for one service")
    return f"/run/sandbox/source/{files[0]}" if files else "-"


def provision_profile(record: Dict[str, Any]) -> None:
    plan = record["profilePlan"]
    sandbox = record["sandbox"]
    repository_path = record["repositoryPath"]
    if VERBOSE:
        print("stnt: provisioning pinned Nix development shell", file=sys.stderr)
    version = run([
        str(RUNTIME), "project-exec", sandbox, repository_path,
        "/home/agent/.nix-profile/bin/nix", "--version",
    ], check=False)
    if version.returncode != 0 or f"{NIX_VERSION}" not in version.stdout:
        raise StntError(f"pinned Nix {NIX_VERSION} is unavailable after kit installation")
    for argv in plan["setup"]:
        if VERBOSE:
            print(f"stnt: running reviewed setup command: {' '.join(argv)}", file=sys.stderr)
        completed = run_lifecycle(
            [str(RUNTIME), "project-exec", sandbox, repository_path, *nix_argv(argv)], check=False,
        )
        if completed.returncode != 0:
            raise StntError(f"reviewed setup command failed: {argv!r}")
    for item in plan["localInputs"]:
        if item.get("exposure") == "read-only-link":
            linked = run_lifecycle([
                str(RUNTIME), "project-input-link", sandbox, repository_path, item["source"],
            ], check=False)
            if linked.returncode != 0:
                raise StntError(f"approved read-only local input could not be projected: {item['source']}")
    if "service" in plan:
        _, hostname, host_port = parse_service_url(plan["service"]["origin"])
        require_loopback_service_host(hostname, host_port)
        mapping = json.loads(run([str(RUNTIME), "port", sandbox, str(record["sandboxPort"])]).stdout)
        if mapping.get("host_port") != host_port:
            raise StntError("profile proof observed the wrong fixed host port")
        if VERBOSE:
            print(f"stnt: proving reviewed service at {plan['service']['origin']}", file=sys.stderr)
        restart_service(record, host_port)


def configure_profile_git_remote(record: Dict[str, Any]) -> None:
    plan = record.get("profilePlan")
    git_remote = plan.get("gitRemote") if isinstance(plan, dict) else None
    if git_remote is None:
        return
    if (not isinstance(git_remote, dict) or set(git_remote) != {"normalized", "httpsURL"} or
            not isinstance(git_remote["normalized"], str) or
            not isinstance(git_remote["httpsURL"], str)):
        raise StntError("reviewed Git remote plan is malformed; retained")
    sandbox = record["sandbox"]
    repository = record["repositoryPath"]
    observed = run_lifecycle([
        str(RUNTIME), "project-exec", sandbox, repository,
        "git", "config", "--get", "remote.origin.url",
    ], check=False)
    lines = observed.stdout.splitlines()
    if (observed.returncode != 0 or len(lines) != 1 or
            normalize_repository_remote(lines[0]) != git_remote["normalized"]):
        raise StntError("sandbox origin does not match the reviewed GitHub repository; retained")
    changed = run_lifecycle([
        str(RUNTIME), "project-exec", sandbox, repository,
        "git", "remote", "set-url", "origin", git_remote["httpsURL"],
    ], check=False)
    if changed.returncode != 0:
        raise StntError("sandbox GitHub origin could not be normalized to HTTPS; retained")
    verified = run_lifecycle([
        str(RUNTIME), "project-exec", sandbox, repository,
        "git", "config", "--get", "remote.origin.url",
    ], check=False)
    if verified.returncode != 0 or verified.stdout.strip() != git_remote["httpsURL"]:
        raise StntError("sandbox GitHub HTTPS origin could not be verified; retained")


def ensure_creation(record: Dict[str, Any], path: Path) -> None:
    sandbox = record["sandbox"]
    profile_context = verify_profile_record(record) if "profilePlan" in record else None
    found = runtime_find(sandbox)
    if profile_context is not None and profile_context[1] != record["profileKitDigest"]:
        if record.get("sandboxID") or found is not None:
            raise StntError("generated profile kit changed after sandbox creation; retained")
        record["profileKitDigest"] = profile_context[1]
        atomic_write(path, record)
    if found is None:
        if record.get("sandboxID"):
            raise StntError(
                f"session {record_selector(record)} retained sandbox ID {record['sandboxID']}, but "
                f"{sandbox} is missing; do not create a replacement"
            )
        create_args = [str(RUNTIME), "create", sandbox, record["repositoryPath"]]
        if profile_context is not None:
            plan = record["profilePlan"]
            create_args.extend([
                str(profile_context[0]),
                str(plan["resources"]["cpus"]),
                str(plan["resources"]["memoryGiB"]),
            ])
        with timed("create.sandbox", "creating cold sandbox"):
            run_lifecycle(create_args)
        found = runtime_find(sandbox)
        if found is None:
            raise StntError("sandbox creation returned but its identity is absent from inventory")
    if not isinstance(found.get("id"), str) or not found["id"]:
        raise StntError("sandbox inventory omitted its stable ID; retained")
    if record.get("sandboxID") and record["sandboxID"] != found["id"]:
        raise StntError(f"sandbox identity changed for {sandbox}; retained")
    if not record.get("sandboxID"):
        record["sandboxID"] = found["id"]
        atomic_write(path, record)
    require_sandbox(record)
    started = False
    try:
        # sbx exec may start the VM even if the command itself later fails.
        started = True
        with timed("create.sandboxBoot", "starting cold sandbox"):
            run_lifecycle([str(RUNTIME), "exec", sandbox, "true"])
        if "sandboxPort" in record:
            mapping = run([str(RUNTIME), "port", sandbox, str(record["sandboxPort"])], check=False)
            if mapping.returncode == 4:
                publish_spec = str(record["sandboxPort"])
                if "serviceURL" in record:
                    host_port = parse_service_url(record["serviceURL"])[2]
                    publish_spec = f"{host_port}:{record['sandboxPort']}"
                run_lifecycle([str(RUNTIME), "publish", sandbox, publish_spec])
                mapping = run([str(RUNTIME), "port", sandbox, str(record["sandboxPort"])])
            elif mapping.returncode != 0:
                raise StntError("sandbox port inventory is unavailable or ambiguous; retained")
            if "serviceURL" in record:
                observed = json.loads(mapping.stdout)
                expected_host_port = parse_service_url(record["serviceURL"])[2]
                if observed.get("host_port") != expected_host_port:
                    raise StntError("sandbox service port is not published on its configured host port; retained")
        if "sourceBranch" in record:
            source_pin = f"refs/stnt/source-pin/{record['sourceSHA']}"
            base_ref = f"refs/heads/{record['baseBranch']}"
            source_ref = f"refs/heads/{record['sourceBranch']}"
            script = (
                "ref=$(git symbolic-ref --quiet HEAD) || exit 1; "
                "case \"$ref\" in refs/heads/*) ;; *) exit 1;; esac; "
                "branch=${ref#refs/heads/}; sha=$(git rev-parse HEAD); "
                f"( [ \"$branch\" = {shell_quote(record['baseBranch'])} ] && "
                f"[ \"$sha\" = {shell_quote(record['baseSHA'])} ] ) || "
                f"( [ \"$branch\" = {shell_quote(record['sourceBranch'])} ] && "
                f"[ \"$sha\" = {shell_quote(record['sourceSHA'])} ] ) || exit 1; "
                f"git update-ref -d {shell_quote(source_pin)} || exit 1; "
                f"cleanup() {{ git update-ref -d {shell_quote(source_pin)} >/dev/null 2>&1 || :; }}; "
                "trap cleanup EXIT; trap 'exit 1' HUP INT TERM; "
                f"git fetch --no-tags --force /run/sandbox/source "
                f"{shell_quote('refs/heads/' + record['sourceBranch'] + ':' + source_pin)} || exit 1; "
                f"fetched=$(git rev-parse --verify {shell_quote(source_pin + '^{commit}')}) || exit 1; "
                f"[ \"$fetched\" = {shell_quote(record['sourceSHA'])} ] || exit 1; "
                "drop_base=false; "
                f"if [ {shell_quote(record['baseBranch'])} != {shell_quote(record['sourceBranch'])} ] && "
                f"git show-ref --verify --quiet {shell_quote(base_ref)}; then "
                f"[ \"$(git rev-parse {shell_quote(base_ref + '^{commit}')})\" = {shell_quote(record['baseSHA'])} ] || exit 1; "
                f"git merge-base --is-ancestor {shell_quote(record['baseSHA'])} {shell_quote(record['sourceSHA'])}; "
                "ancestry=$?; if [ \"$ancestry\" -eq 1 ]; then drop_base=true; "
                "elif [ \"$ancestry\" -ne 0 ]; then exit 1; fi; fi; "
                f"if [ \"$branch\" = {shell_quote(record['sourceBranch'])} ]; then :; "
                f"elif git show-ref --verify --quiet {shell_quote(source_ref)}; then "
                f"[ \"$(git rev-parse {shell_quote(source_ref + '^{commit}')})\" = {shell_quote(record['sourceSHA'])} ] || exit 1; "
                f"git switch -- {shell_quote(record['sourceBranch'])} || exit 1; "
                f"else git switch -c {shell_quote(record['sourceBranch'])} {shell_quote(record['sourceSHA'])} || exit 1; fi; "
                f"[ \"$(git symbolic-ref --quiet HEAD)\" = {shell_quote(source_ref)} ] || exit 1; "
                f"[ \"$(git rev-parse HEAD)\" = {shell_quote(record['sourceSHA'])} ] || exit 1; "
                f"if [ \"$drop_base\" = true ]; then "
                f"git update-ref -d {shell_quote(base_ref)} {shell_quote(record['baseSHA'])} || exit 1; fi; "
                f"git update-ref -d {shell_quote(source_pin)} || exit 1; "
                "trap - EXIT HUP INT TERM"
            )
        else:
            script = (
                "ref=$(git symbolic-ref --quiet HEAD) || exit 1; "
                "case \"$ref\" in refs/heads/*) ;; *) exit 1;; esac; "
                "branch=${ref#refs/heads/}; sha=$(git rev-parse HEAD); "
                f"[ \"$sha\" = {shell_quote(record['baseSHA'])} ] || exit 1; "
                f"if [ \"$branch\" = {shell_quote(record['branch'])} ]; then exit 0; fi; "
                # Existing schema-1 records retain their generated scratch-branch
                # recovery behavior. New default records have branch == baseBranch.
                f"[ {shell_quote(record['branch'])} != {shell_quote(record['baseBranch'])} ] && "
                f"[ \"$branch\" = {shell_quote(record['baseBranch'])} ] && "
                f"git switch -c {shell_quote(record['branch'])}"
            )
        branch_result = run_lifecycle(
            [str(RUNTIME), "exec", sandbox, "bash", "-lc", script], check=False
        )
        if branch_result.returncode != 0:
            raise StntError(f"sandbox Git branch is ambiguous; retained {sandbox}")
        if profile_context is not None:
            configure_profile_git_remote(record)
            with timed("create.provision", "provisioning reviewed project"):
                provision_profile(record)
    except BaseException as error:
        if started:
            stopped = run_lifecycle([str(RUNTIME), "stop", sandbox], check=False)
            if stopped.returncode != 0:
                raise StntError(
                    f"creation failed and sandbox {sandbox} could not be stopped; "
                    f"session {record_selector(record)} is retained with ambiguous runtime state"
                ) from error
        raise
    run_lifecycle([str(RUNTIME), "stop", sandbox])
    record["status"] = "paused"
    atomic_write(path, record)
    timing_milestone("create.readyPaused")


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def wait_for_health(
    host_port: int,
    health_path: str,
    timeout: float = SERVICE_HEALTH_TIMEOUT_SECONDS,
    *,
    scheme: str = "http",
    hostname: str = "127.0.0.1",
) -> None:
    deadline = time.monotonic() + timeout
    last_error = "no response"
    while time.monotonic() < deadline:
        connection_timeout = min(1.0, max(0.1, deadline - time.monotonic()))
        if scheme == "https":
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            connection = HTTPSConnection(hostname, host_port, timeout=connection_timeout, context=context)
        else:
            connection = HTTPConnection(hostname, host_port, timeout=connection_timeout)
        try:
            connection.request("GET", health_path)
            response = connection.getresponse()
            if 200 <= response.status < 400:
                return
            last_error = f"HTTP {response.status}"
        except (HTTPException, TimeoutError, OSError) as error:
            last_error = str(error)
        finally:
            connection.close()
        time.sleep(0.2)
    raise StntError(
        f"service health check timed out at {scheme}://{hostname}:{host_port}{health_path} ({last_error})"
    )


def restart_service(record: Dict[str, Any], host_port: int) -> None:
    service_command = record.get("serviceCommand")
    service_argv = record.get("serviceArgv")
    if not service_command and not service_argv:
        return
    if service_argv:
        env_file = profile_environment_file(record["profilePlan"])
        _, service_hostname, _ = parse_service_url(record["serviceURL"])
        # Vite consumes this documented compatibility variable; other services ignore it.
        service_argv = [
            "/usr/bin/env",
            f"__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS={service_hostname}",
            *service_argv,
        ]
        start_args = [
            str(RUNTIME), "service-start-argv", record["sandbox"], record["repositoryPath"],
            env_file, str(record["sandboxPort"]), "0.0.0.0", *service_argv,
        ]
    else:
        environment_command = (
            f"export STNT_PORT={record['sandboxPort']} STNT_HOST=0.0.0.0; "
            f"{service_command}"
        )
        start_args = [
            str(RUNTIME), "service-start", record["sandbox"], record["repositoryPath"],
            environment_command,
        ]
    started = run(start_args, check=False)
    if started.returncode != 0:
        raise StntError(
            f"service command failed to start in {record['sandbox']}; retry: stnt --session {record_selector(record)} start; "
            f"inspect (starts the retained VM): bin/docker-sandbox exec {record['sandbox']} "
            "cat /tmp/stnt-service.log"
        )
    health_path = record.get("healthPath", "/")
    try:
        if "serviceURL" in record:
            scheme, hostname, _ = parse_service_url(record["serviceURL"])
            wait_for_health(host_port, health_path, scheme=scheme, hostname=hostname)
        else:
            wait_for_health(host_port, health_path)
        status = run([str(RUNTIME), "service-status", record["sandbox"]], check=False)
        if status.returncode != 0:
            raise StntError("service process exited or changed identity after becoming healthy")
    except StntError as error:
        raise StntError(
            f"{error}; retry: stnt --session {record_selector(record)} start; inspect (starts the retained VM): "
            f"bin/docker-sandbox exec {record['sandbox']} cat /tmp/stnt-service.log"
        ) from error


@contextmanager
def sandbox_hold(sandbox: str) -> Iterator[None]:
    process = subprocess.Popen(
        [str(RUNTIME), "hold", sandbox],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready = process.stdout.readline() if process.stdout is not None else ""
        if ready != "ready\n":
            if process.poll() is None:
                process.terminate()
            _, errors = process.communicate(timeout=5)
            detail = errors.strip()
            raise StntError(f"sandbox keepalive failed to attach: {detail or process.poll()}")
        yield
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def validate_record_repository(record: Dict[str, Any], repo: Path) -> None:
    if record["repositoryPath"] != str(repo):
        raise StntError("session repository path does not match the current repository")


def runner_id(record: Dict[str, Any]) -> str:
    return f"stnt-{compact_thread_id(record['threadID'])}"


def start_session(record: Dict[str, Any], path: Path) -> None:
    validate_record_repository(record, Path(record["repositoryPath"]))
    if record.get("lifecycleOwner") != "workspace":
        with timed("start.threadAuthority", "checking Amp thread authority"):
            state = thread_status(
                record["threadID"],
                allow_empty=record["status"] in {"starting", "active", "paused", "ambiguous"},
            )
        if state == "archived":
            record["status"] = "archived"
            atomic_write(path, record)
            raise StntError(f"thread is archived; safely preserve and remove with: stnt --session {record['threadID']} finish")
    if "profilePlan" in record:
        _, kit_digest = verify_profile_record(record)
        if kit_digest != record["profileKitDigest"]:
            raise StntError("generated profile kit changed after sandbox creation; retained")
    if "serviceURL" in record:
        _, hostname, host_port = parse_service_url(record["serviceURL"])
        require_loopback_service_host(hostname, host_port)
    require_sandbox(record)
    record.pop("editorAuthorization", None)
    record["status"] = "starting"
    atomic_write(path, record)
    # A prior Stnt process may have died while an editor proxy was attached.
    # Publishing starting revokes it; exclusive drain proves it has exited
    # before any provider call can start this sandbox again.
    with editor_drain(record, exclusive=True):
        pass
    authorization: Optional[EditorAuthorization] = None
    try:
        with timed("start.sandbox", "starting and reconciling sandbox"):
            branch_result = run([
                str(RUNTIME), "exec", record["sandbox"], "git", "symbolic-ref", "--quiet", "HEAD"
            ], check=False)
        branch_ref = branch_result.stdout.strip()
        if branch_result.returncode != 0 or not branch_ref.startswith("refs/heads/") or run(
            ["git", "check-ref-format", branch_ref], check=False
        ).returncode != 0:
            raise StntError("sandbox HEAD is detached or is not a valid local branch; retained")
        host_port = None
        if "sandboxPort" in record:
            mapping = json.loads(run([
                str(RUNTIME), "port", record["sandbox"], str(record["sandboxPort"])
            ]).stdout)
            host_port = mapping["host_port"]
            if not isinstance(host_port, int) or not (1 <= host_port <= 65535):
                raise StntError("sandbox port inventory returned an invalid host port; retained")
            if "serviceURL" in record and host_port != parse_service_url(record["serviceURL"])[2]:
                raise StntError("sandbox service port is not published on its configured host port; retained")
        if "serviceCommand" in record or "serviceArgv" in record:
            with timed("start.service", "starting service and waiting for health"):
                restart_service(record, host_port)
            if "serviceURL" in record:
                open_browser(record["serviceURL"])
        elif TIMING is not None:
            with TIMING.stage("start.service"):
                pass
        timing_milestone("start.serviceReady")
        authorization = EditorAuthorization(record, path)
        automatic_editor_handoff(record)
        if record.get("lifecycleOwner") != "workspace":
            runner_args = [
                str(RUNTIME), "runner-start", record["sandbox"], runner_id(record), record["threadID"],
                record["repositoryPath"],
            ]
            if "profilePlan" in record:
                runner_args.append("nix")
            with timed("start.runner", "starting exact thread-bound Amp runner"):
                run(runner_args)
            timing_milestone("start.runnerReady")
    except BaseException as error:
        revoke_editor_authorization(record, path, authorization)
        with editor_drain(record, exclusive=True):
            stopped = run_lifecycle([str(RUNTIME), "stop", record["sandbox"]], check=False)
        if stopped.returncode != 0:
            record["status"] = "ambiguous"
            atomic_write(path, record)
            raise StntError(
                f"reconciliation failed and sandbox {record['sandbox']} could not be stopped; "
                "runtime state is ambiguous"
            ) from error
        record["status"] = "paused"
        atomic_write(path, record)
        raise
    if record.get("lifecycleOwner") == "workspace":
        print(f"workspace={record['workspaceID']}")
    else:
        print(f"session={record['threadID']}")
    print(f"sandbox={record['sandbox']}")
    if record.get("lifecycleOwner") != "workspace":
        print(f"runner={runner_id(record)}")
    if host_port is not None:
        display_url = record.get("serviceURL", f"http://127.0.0.1:{host_port}")
        print(f"url={display_url}")
    handoff = (
        "sandbox Amp TUI" if record.get("lifecycleOwner") == "workspace"
        else "native host Amp"
    )
    print(f"stnt: handing control to {handoff}...", file=sys.stderr)
    timing_milestone("start.ampHandoff")
    if record.get("lifecycleOwner") == "workspace":
        tui_args = [
            str(RUNTIME), "tui-start", record["sandbox"], record["repositoryPath"],
        ]
        if "profilePlan" in record:
            tui_args.append("nix")
        while True:
            try:
                attach_result = run(
                    tui_args,
                    check=False,
                    capture=False,
                )
            except BaseException:
                pause_after_exit(record, path, authorization)
                raise
            if attach_result.returncode != 0:
                pause_after_exit(record, path, authorization)
                raise StntError(
                    f"Amp exited with status {attach_result.returncode}; workspace was retained"
                )
            try:
                decision = workspace_exit_decision(record, path, authorization)
            except BaseException:
                authorization.close()
                raise
            if decision != "cancel":
                authorization.close()
                return
            print("returning to Amp workspace...", file=sys.stderr)
    else:
        attach_error = None
        try:
            with sandbox_hold(record["sandbox"]):
                attach_result = run(
                    [str(THREADS), "continue", record["threadID"]],
                    check=False,
                    capture=False,
                )
        except BaseException as error:
            attach_error = error
        pause_after_exit(record, path, authorization)
        authorization.close()
        if attach_error:
            raise attach_error
        if attach_result.returncode != 0:
            raise StntError(f"Amp exited with status {attach_result.returncode}; session was retained")


def pause_after_exit(
    record: Dict[str, Any], path: Path,
    authorization: Optional[EditorAuthorization] = None,
) -> None:
    revoke_editor_authorization(record, path, authorization)
    with editor_drain(record, exclusive=True), timed("pause.sandboxStop", "stopping sandbox"):
        stopped = run_lifecycle([str(RUNTIME), "stop", record["sandbox"]], check=False)
    if stopped.returncode != 0:
        record["status"] = "ambiguous"
        atomic_write(path, record)
        raise StntError(
            f"sandbox stop failed; session {record_selector(record)} is retained with ambiguous runtime state"
        )
    if record.get("lifecycleOwner") == "workspace":
        record["status"] = "paused"
    else:
        try:
            with timed("pause.threadAuthority", "checking post-exit Amp thread state"):
                state = thread_status(record["threadID"], allow_empty=True)
            record["status"] = "archived" if state == "archived" else "paused"
        except StntError as error:
            record["status"] = "ambiguous"
            print(f"warning: {error}", file=sys.stderr)
    atomic_write(path, record)
    timing_milestone("pause.durable")
    if record["status"] == "archived" and record.get("lifecycleOwner") != "workspace":
        command_finish(Path(record["repositoryPath"]), (path, record))
        print(f"thread archived; preserved work and removed sandbox {record['sandbox']}")
    elif record["status"] == "ambiguous":
        print(f"session {record_selector(record)} retained with ambiguous state; next: stnt list")
    else:
        print(f"session paused; resume with: stnt --session {record_selector(record)} start")


def command_start(
    repo: Path,
    *,
    selected: Optional[tuple[Path, Dict[str, Any]]] = None,
    force_new: bool = False,
    service_command: Optional[str] = None,
    service_url: Optional[str] = None,
    health_path: str = "/",
) -> None:
    path, record = selected if selected else (session_path(repo), load_state(session_path(repo)))
    if force_new:
        record = None
    if record is None:
        try:
            critical_preflight(repo)
            record = create_record(
                repo, service_command=service_command, service_url=service_url, health_path=health_path
            )
            path = record_session_path(record)
            ensure_creation(record, path)
        except BaseException as error:
            path = record_session_path(record) if record else path
            retained = load_state(path)
            if retained:
                selector = record_selector(retained)
                raise StntError(
                    f"creation interrupted after retaining session {selector}. "
                    f"Safe retry: stnt --session {selector} recover-create\nCause: {error}"
                ) from error
            raise
    elif record["status"] == "creating":
        selector = record_selector(record)
        raise StntError(
            f"creation of session {selector} is incomplete. Safe retry: stnt --session {selector} recover-create"
        )
    elif service_command is not None or service_url is not None:
        raise StntError(
            "--service-command and --service-url are accepted only when creating a new session; "
            "the retained session was not changed"
        )
    start_session(record, path)


def command_recover_create(repo: Path, selected: Optional[tuple[Path, Dict[str, Any]]] = None) -> None:
    path = selected[0] if selected else session_path(repo)
    record = selected[1] if selected else load_state(path)
    if not record or record["status"] != "creating":
        raise StntError("there is no incomplete creation for this repository")
    if record.get("lifecycleOwner") != "workspace":
        thread_status(record["threadID"], allow_empty=True)
    try:
        with repository_lock(repo):
            reloaded = load_state(path)
            if reloaded is None or record_lock_identity(reloaded) != record_lock_identity(record):
                raise StntError("incomplete creation changed before recovery; retry: stnt list")
            record = reloaded
            ensure_creation(record, path)
    except Exception as error:
        raise StntError(
            f"creation remains ambiguous; retained session {record_selector(record)} and sandbox "
            f"{record['sandbox']}. Safe retry: stnt --session {record_selector(record)} "
            f"recover-create\nCause: {error}"
        ) from error
    selector = record_selector(record)
    print(f"recovered session {selector}; start with: stnt --session {selector} start")


def command_pause(repo: Path, selected: Optional[tuple[Path, Dict[str, Any]]] = None) -> None:
    path = selected[0] if selected else session_path(repo)
    record = selected[1] if selected else load_state(path)
    if not record:
        raise StntError("no Stnt session exists for this repository")
    if record["status"] == "creating":
        raise StntError(f"creation is incomplete. Safe retry: stnt --session {record_selector(record)} recover-create")
    state = None
    if record.get("lifecycleOwner") != "workspace":
        with timed("pause.threadAuthority", "checking Amp thread authority"):
            state = thread_status(
                record["threadID"],
                allow_empty=record["status"] in {"starting", "active", "paused", "ambiguous"},
            )
    require_sandbox(record)
    revoke_editor_authorization(record, path)
    with editor_drain(record, exclusive=True), timed("pause.sandboxStop", "stopping sandbox"):
        stopped = run_lifecycle([str(RUNTIME), "stop", record["sandbox"]], check=False)
    if stopped.returncode != 0:
        record["status"] = "ambiguous"
        atomic_write(path, record)
        raise StntError(
            f"sandbox stop failed; session {record_selector(record)} is retained with ambiguous runtime state"
        )
    record["status"] = "archived" if state == "archived" else "paused"
    atomic_write(path, record)
    timing_milestone("pause.durable")
    print(f"paused {record_selector(record)}; resume with: stnt --session {record_selector(record)} start")


def command_open(repo: Path, selected: tuple[Path, Dict[str, Any]]) -> None:
    _, record = selected
    validate_record_repository(record, repo)
    selector = record_selector(record)
    if record["status"] == "creating":
        raise StntError(
            f"creation of session {selector} is incomplete. "
            f"Safe retry: stnt --session {selector} recover-create"
        )
    if record["status"] == "starting":
        raise StntError(
            f"workspace {selector} start is incomplete; reconcile it with: "
            f"stnt --session {selector} start"
        )
    if record["status"] in {"ambiguous", "archived"}:
        raise StntError(
            f"workspace {selector} lifecycle state is {record['status']}; "
            "reconcile it with: stnt list"
        )
    service_url = record.get("serviceURL")
    if service_url is None:
        if "serviceCommand" in record or "serviceArgv" in record:
            raise StntError(
                f"workspace {selector} has no reviewed fixed service URL; "
                "configure one with stnt reconfigure and create a new workspace"
            )
        raise StntError(
            f"workspace {selector} is service-less; configure a service with "
            "stnt reconfigure and create a new workspace"
        )
    if record["status"] == "paused":
        raise StntError(
            f"workspace {selector} is paused; resume it with: "
            f"stnt --session {selector} start"
        )
    scheme, hostname, expected_port = parse_service_url(service_url)
    require_loopback_service_host(hostname, expected_port)
    sandbox = require_sandbox(record)
    runtime_state = sandbox.get("state", sandbox.get("status"))
    if runtime_state != "running":
        raise StntError(
            f"workspace {selector} is paused; resume it with: stnt --session {selector} start"
        )
    mapping_result = run([
        str(RUNTIME), "port", record["sandbox"], str(record["sandboxPort"]),
    ], check=False)
    try:
        mapping = json.loads(mapping_result.stdout)
    except json.JSONDecodeError as error:
        raise StntError("sandbox service port inventory is unavailable or ambiguous; browser was not opened") from error
    if (mapping_result.returncode != 0 or not isinstance(mapping, dict) or
            mapping.get("sandbox_port") != record["sandboxPort"] or
            mapping.get("host_port") != expected_port):
        raise StntError("sandbox service URL is stale or unverified; browser was not opened")
    wait_for_health(
        expected_port, record.get("healthPath", "/"), scheme=scheme, hostname=hostname,
    )
    service_status = run([str(RUNTIME), "service-status", record["sandbox"]], check=False)
    if service_status.returncode != 0:
        raise StntError("sandbox service process identity is unverified; browser was not opened")
    open_browser(service_url)


def command_finish(repo: Path, selected: Optional[tuple[Path, Dict[str, Any]]] = None) -> None:
    path = selected[0] if selected else session_path(repo)
    record = selected[1] if selected else load_state(path)
    if record and record.get("lifecycleOwner") == "workspace":
        validate_record_repository(record, repo)
        if record["status"] == "creating":
            raise StntError(
                f"creation is incomplete. Safe retry: stnt --session {record_selector(record)} recover-create"
            )
        command_destructive_finish(path, record)
        return
    command_remove(repo, selected, operation="finish")


def command_detach(repo: Path, selected: Optional[tuple[Path, Dict[str, Any]]] = None) -> None:
    command_remove(repo, selected, operation="detach")


def command_remove(
    repo: Path,
    selected: Optional[tuple[Path, Dict[str, Any]]],
    *,
    operation: str,
) -> None:
    path = selected[0] if selected else session_path(repo)
    record = selected[1] if selected else load_state(path)
    if not record:
        raise StntError("no Stnt session exists for this repository")
    validate_record_repository(record, repo)
    if operation == "detach" and record["status"] == "creating":
        raise StntError(
            f"creation is incomplete. Safe retry: stnt --session {record_selector(record)} recover-create"
        )
    if operation == "finish":
        if record.get("lifecycleOwner") == "workspace":
            raise StntError(
                "workspace-owned sessions are not removed by archiving a guest thread; "
                f"retain it, or explicitly preserve and remove it with: stnt --session {record_selector(record)} detach"
            )
        with timed("finish.threadAuthority", "checking archived-thread authority"):
            state = thread_status(record["threadID"])
        if state != "archived":
            raise StntError(
                f"thread is {state}, not archived; finish requires an archived Amp thread. "
                f"Archive without starting the sandbox: amp threads archive {record['threadID']}; "
                f"then retry: stnt --session {record['threadID']} finish"
            )
    found = runtime_find(record["sandbox"])
    if found is None:
        preservation = run([
            "git", "-C", str(repo), "rev-parse", "--verify",
            f"refs/heads/{record['preservationBranch']}^{{commit}}",
        ], check=False)
        if preservation.returncode == 0:
            raise StntError(
                f"{operation} outcome is indeterminate: sandbox is absent and preservation branch "
                f"{record['preservationBranch']} exists at {preservation.stdout.strip()}. "
                f"State retained at {path}; verify inventory and the branch before removing it manually"
            )
        raise StntError(
            f"session {record_selector(record)} is retained but sandbox {record['sandbox']} is missing; "
            "do not create a replacement"
        )
    require_sandbox(record)
    with timed("finish.preservationTransaction", "preserving committed work and removing sandbox"):
        result = run_lifecycle([
            str(FINISH), operation, record["sandbox"], str(repo), record_selector(record),
            "--current-branch", record["preservationBranch"],
        ], check=False)
    if result.returncode != 0:
        try:
            post_finish = runtime_find(record["sandbox"])
            if post_finish is not None:
                require_sandbox(record)
        except StntError as error:
            raise StntError(
                f"{operation} outcome is indeterminate for {record_selector(record)}; state retained at {path}. "
                f"Verify: git rev-parse {record['preservationBranch']} and bin/docker-sandbox list"
            ) from error
        if post_finish is None:
            raise StntError(
                f"{operation} outcome is indeterminate for {record_selector(record)}; state retained at {path}. "
                f"Verify: git rev-parse {record['preservationBranch']} and bin/docker-sandbox list"
            )
        raise StntError(
            f"{operation} retained session {record_selector(record)}. "
            f"Safe retry: stnt --session {record_selector(record)} {operation}"
        )
    remove_state(path)
    timing_milestone("finish.removedAndReachable")


def require_stack_sandbox(record: Dict[str, Any], *, allow_unrecorded_id: bool = False) -> Dict[str, Any]:
    found = runtime_find(record["sandbox"])
    if found is None:
        raise StntError(f"stack {record['name']} is retained but sandbox {record['sandbox']} is missing; do not create a replacement")
    expected_workspaces = {item["path"] for item in record["repositories"]}
    workspaces = found.get("workspaces")
    observed_workspaces = {
        workspace[:-3] if workspace.endswith(":ro") else workspace
        for workspace in workspaces
    } if isinstance(workspaces, list) and all(isinstance(item, str) for item in workspaces) else set()
    if not isinstance(workspaces, list) or not expected_workspaces <= observed_workspaces:
        raise StntError(f"stack sandbox workspace identity changed for {record['sandbox']}; retained")
    expected_id = record.get("sandboxID")
    if expected_id and found.get("id") != expected_id:
        raise StntError(f"stack sandbox identity changed for {record['sandbox']}; retained")
    if not expected_id and not allow_unrecorded_id:
        raise StntError(f"stack sandbox ID is absent from durable state for {record['sandbox']}; retained")
    return found


def create_stack_record(profile: Dict[str, Any]) -> Dict[str, Any]:
    existing = [value for _, value in load_sessions()]
    stack_directory = state_root() / "stacks"
    for existing_path in sorted(stack_directory.glob("*.json")):
        existing_name = existing_path.stem
        existing_state = load_stack_state(existing_name)
        if existing_state is None:
            raise StntError(f"stack state changed during creation inventory: {existing_path}")
        existing.append(existing_state)
    lifecycle_signals = {signal.SIGHUP, signal.SIGINT, signal.SIGTERM}
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, lifecycle_signals)
    try:
        created = run([str(THREADS), "new"])
        thread_id = created.stdout.strip()
        if not THREAD_RE.fullmatch(thread_id):
            raise StntError(f"amp threads new returned an invalid ID: {thread_id!r}")
        record = {
            "schemaVersion": STACK_SCHEMA_VERSION,
            "name": profile["name"],
            "profileDigest": canonical_digest(profile),
            "threadID": thread_id,
            "runtime": "docker-sandbox",
            "sandbox": f"stnt-stack-{profile['name']}-{compact_thread_id(thread_id)}",
            "repositories": stack_state_repositories(profile, thread_id),
            "ingress": profile["ingress"],
            "status": "creating",
            "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        for prior in existing:
            if prior["threadID"] == thread_id or prior["sandbox"] == record["sandbox"]:
                raise StntError("thread provider returned an identity already owned by Stnt")
        path = stack_state_path(profile["name"])
        try:
            atomic_write(path, record, create_only=True)
        except Exception as error:
            raise StntError(f"could not durably record newly created stack thread {thread_id}; no sandbox operation was attempted") from error
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
    return record


def ensure_stack_creation(record: Dict[str, Any], profile: Dict[str, Any]) -> None:
    path = stack_state_path(record["name"])
    if not stack_record_matches_profile(record, profile):
        raise StntError(f"stack profile changed after thread creation; retained {record['threadID']}")
    frontend = stack_role(profile, "frontend")
    backend = stack_role(profile, "backend")
    found = runtime_find(record["sandbox"])
    if found is None:
        if record.get("sandboxID"):
            raise StntError(f"stack {record['name']} retained a sandbox ID, but {record['sandbox']} is missing; do not create a replacement")
        run([
            str(RUNTIME), "create-stack", record["sandbox"], frontend["path"], backend["path"],
        ], capture=False)
        found = runtime_find(record["sandbox"])
        if found is None:
            raise StntError("stack sandbox creation returned without an inventory identity")
    if not isinstance(found.get("id"), str) or not found["id"]:
        raise StntError("stack sandbox inventory omitted its stable ID; retained")
    if record.get("sandboxID") and record["sandboxID"] != found["id"]:
        raise StntError(f"stack sandbox identity changed for {record['sandbox']}; retained")
    if not record.get("sandboxID"):
        record["sandboxID"] = found["id"]
        atomic_write(path, record)
    require_stack_sandbox(record)
    started = False
    try:
        started = True
        run([str(RUNTIME), "exec", record["sandbox"], "true"], capture=False)
        mapping = run([str(RUNTIME), "port", record["sandbox"], str(STACK_INGRESS_PORT)], check=False)
        if mapping.returncode == 4:
            run([
                str(RUNTIME), "publish", record["sandbox"],
                f"{profile['ingress']['hostPort']}:{STACK_INGRESS_PORT}",
            ], capture=False)
            mapping = run([str(RUNTIME), "port", record["sandbox"], str(STACK_INGRESS_PORT)])
        elif mapping.returncode != 0:
            raise StntError("stack ingress inventory is unavailable or ambiguous; retained")
        observed = json.loads(mapping.stdout)
        if observed.get("host_port") != profile["ingress"]["hostPort"]:
            raise StntError("stack ingress is not published on its reviewed host port; retained")
        prepared = run([
            str(RUNTIME), "stack-prepare", record["sandbox"],
            frontend["path"], frontend["branch"], frontend["sha"],
            backend["path"], backend["guestPath"], backend["branch"], backend["sha"],
        ], check=False, capture=False)
        if prepared.returncode != 0:
            raise StntError("stack private-clone preparation or pin verification failed; retained")
    except BaseException as error:
        if started and run([str(RUNTIME), "stop", record["sandbox"]], check=False, capture=False).returncode != 0:
            raise StntError(f"stack creation failed and {record['sandbox']} could not be stopped; runtime state is ambiguous") from error
        raise
    run([str(RUNTIME), "stop", record["sandbox"]], capture=False)
    record["status"] = "paused"
    atomic_write(path, record)


def stack_url(record: Dict[str, Any]) -> str:
    return f"http://127.0.0.1:{record['ingress']['hostPort']}"


def pause_stack_after_exit(record: Dict[str, Any]) -> None:
    path = stack_state_path(record["name"])
    if run([str(RUNTIME), "stop", record["sandbox"]], check=False, capture=False).returncode != 0:
        record["status"] = "ambiguous"
        atomic_write(path, record)
        raise StntError(f"stack sandbox stop failed; {record['name']} is retained with ambiguous runtime state")
    try:
        observed = thread_status(record["threadID"], allow_empty=True)
        record["status"] = "archived" if observed == "archived" else "paused"
    except StntError as error:
        record["status"] = "ambiguous"
        print(f"warning: {error}", file=sys.stderr)
    atomic_write(path, record)
    if record["status"] == "archived":
        command_stack_finish(record["name"])
        print(f"thread archived; preserved both repositories and removed stack sandbox {record['sandbox']}")
    else:
        print(f"stack paused; resume with: stnt stack start {record['name']}")


def start_stack(record: Dict[str, Any], profile: Dict[str, Any]) -> None:
    if not stack_record_matches_profile(record, profile):
        raise StntError(f"stack profile changed after creation; retained {record['name']}")
    observed_thread = thread_status(record["threadID"], allow_empty=record["status"] in {"paused", "ambiguous"})
    if observed_thread == "archived":
        record["status"] = "archived"
        atomic_write(stack_state_path(record["name"]), record)
        raise StntError(f"stack thread is archived; finish with: stnt stack finish {record['name']}")
    require_stack_sandbox(record)
    frontend = stack_role(profile, "frontend")
    backend = stack_role(profile, "backend")
    try:
        verified = run([
            str(RUNTIME), "stack-verify", record["sandbox"],
            frontend["path"], frontend["branch"], frontend["sha"],
            backend["guestPath"], backend["branch"], backend["sha"],
        ], check=False, capture=False)
        if verified.returncode != 0:
            raise StntError("one or both stack private clones changed identity or are dirty; retained")
        mapping = json.loads(run([
            str(RUNTIME), "port", record["sandbox"], str(STACK_INGRESS_PORT),
        ]).stdout)
        if mapping.get("host_port") != record["ingress"]["hostPort"]:
            raise StntError("stack canonical ingress changed; retained")
        started = run([
            str(RUNTIME), "stack-start", record["sandbox"], record["name"],
            frontend["guestPath"], backend["guestPath"],
            json.dumps(frontend["argv"], separators=(",", ":")),
            json.dumps(backend["argv"], separators=(",", ":")),
        ], check=False, capture=False)
        if started.returncode != 0:
            raise StntError("stack services failed to start; inspect /tmp/stnt-stack-*.log in the retained sandbox")
        wait_for_health(record["ingress"]["hostPort"], record["ingress"]["healthPath"])
        if run([str(RUNTIME), "stack-status", record["sandbox"]], check=False).returncode != 0:
            raise StntError("a stack service exited after ingress became healthy")
        run([
            str(RUNTIME), "runner-start", record["sandbox"], runner_id(record), record["threadID"], frontend["guestPath"],
        ])
    except BaseException as error:
        if run([str(RUNTIME), "stop", record["sandbox"]], check=False, capture=False).returncode != 0:
            record["status"] = "ambiguous"
            atomic_write(stack_state_path(record["name"]), record)
            raise StntError(f"stack start failed and {record['sandbox']} could not be stopped; runtime state is ambiguous") from error
        record["status"] = "paused"
        atomic_write(stack_state_path(record["name"]), record)
        raise
    print(f"stack={record['name']}")
    print(f"session={record['threadID']}")
    print(f"sandbox={record['sandbox']}")
    print(f"url={stack_url(record)}")
    attach_result = None
    attach_error = None
    try:
        with sandbox_hold(record["sandbox"]):
            attach_result = run([str(THREADS), "continue", record["threadID"]], check=False, capture=False)
    except BaseException as error:
        attach_error = error
    pause_stack_after_exit(record)
    if attach_error:
        raise attach_error
    if attach_result.returncode != 0:
        raise StntError(f"Amp exited with status {attach_result.returncode}; stack was retained")


def command_stack_start(name: str) -> None:
    _, profile = load_stack_profile(name)
    path = stack_state_path(name)
    record = load_stack_state(name)
    if record is not None:
        ensure_stack_preservation_intent(record, profile)
    if record is None:
        require_stack_sources(profile)
        critical_preflight(Path(stack_role(profile, "frontend")["path"]), require_host_amp=True)
        with creation_lock():
            record = create_stack_record(profile)
            ensure_stack_creation(record, profile)
    elif record["status"] == "creating":
        require_stack_sources(profile)
        thread_status(record["threadID"], allow_empty=True)
        ensure_stack_creation(record, profile)
    record = load_stack_state(name)
    if record is None:
        raise StntError(f"stack state disappeared during start: {path}")
    start_stack(record, profile)


def command_stack_pause(name: str) -> None:
    record = load_stack_state(name)
    if record is None:
        raise StntError(f"stack {name!r} has no runtime state")
    if record["status"] == "creating":
        raise StntError(f"stack creation is incomplete; retry: stnt stack start {name}")
    require_stack_sandbox(record)
    pause_stack_after_exit(record)


def command_stack_finish(name: str) -> None:
    command_stack_remove(name, operation="finish")


def command_stack_detach(name: str) -> None:
    command_stack_remove(name, operation="detach")


def command_stack_remove(name: str, *, operation: str) -> None:
    path = stack_state_path(name)
    record = load_stack_state(name)
    if record is None:
        raise StntError(f"stack {name!r} has no runtime state")
    if record["status"] == "creating":
        raise StntError(f"stack creation is incomplete; retry: stnt stack start {name}")
    _, profile = load_stack_profile(name)
    ensure_stack_preservation_intent(record, profile)
    if not stack_record_matches_profile(record, profile):
        raise StntError(f"stack profile changed after creation; retained {name}")
    repositories = {item["role"]: item for item in record["repositories"]}
    if any(not item.get("preservationBranch") for item in repositories.values()):
        raise StntError(
            f"stack {name} predates durable preservation intent; retained without runtime mutation"
        )
    if operation == "finish":
        state = thread_status(record["threadID"])
        if state != "archived":
            raise StntError(
                f"thread is {state}, not archived; finish requires an archived Amp thread. "
                f"Archive without starting the sandbox: amp threads archive {record['threadID']}; "
                f"then retry: stnt stack finish {name}"
            )
    found = runtime_find(record["sandbox"])
    if found is None:
        anchors = []
        for item in repositories.values():
            preserved = run([
                "git", "-C", item["path"], "rev-parse", "--verify",
                f"refs/heads/{item['preservationBranch']}^{{commit}}",
            ], check=False)
            if preserved.returncode == 0:
                anchors.append(f"{item['role']}={item['preservationBranch']}@{preserved.stdout.strip()}")
        if anchors:
            raise StntError(
                f"{operation} outcome is indeterminate: sandbox is absent and preservation evidence exists "
                f"({', '.join(anchors)}). State retained at {path}; verify both repositories and runtime inventory"
            )
        raise StntError(f"stack {name} is retained but sandbox {record['sandbox']} is missing; do not create a replacement")
    require_stack_sandbox(record)
    frontend = repositories["frontend"]
    backend = repositories["backend"]
    completed = run([
        str(STACK_FINISH), operation, record["sandbox"], name, record["threadID"],
        frontend["path"], frontend["guestPath"], frontend["preservationBranch"],
        backend["path"], backend["guestPath"], backend["preservationBranch"],
    ], check=False, capture=False)
    if completed.returncode != 0:
        try:
            post_finish = runtime_find(record["sandbox"])
            if post_finish is not None:
                require_stack_sandbox(record)
        except StntError as error:
            raise StntError(
                f"{operation} outcome is indeterminate for stack {name}; state retained at {path}. "
                f"Verify both preservation branches and: bin/docker-sandbox list"
            ) from error
        if post_finish is None:
            raise StntError(
                f"{operation} outcome is indeterminate for stack {name}; state retained at {path}. "
                f"Verify both preservation branches and: bin/docker-sandbox list"
            )
        raise StntError(
            f"{operation} retained stack {name}, sandbox {record['sandbox']}, "
            f"frontend branch {frontend['preservationBranch']}, and backend branch {backend['preservationBranch']}. "
            f"Safe retry: stnt stack {operation} {name}"
        )
    remove_state(path)


def parse_args(argv: Sequence[str]) -> Any:
    parser = argparse.ArgumentParser(prog="stnt")
    parser.add_argument("--session", metavar="ID", help="select an exact durable workspace or legacy thread")
    parser.add_argument(
        "--verbose", action="store_true",
        help="stream provider, Git, provisioning, and package-manager output",
    )
    parser.add_argument(
        "--timings", metavar="JSONL_PATH",
        help="append fixed-schema, secret-free command timings as one JSON line",
    )
    subparsers = parser.add_subparsers(dest="command")
    start = subparsers.add_parser("start", help="create or resume the default session")
    start.add_argument("--session", dest="session", default=argparse.SUPPRESS, metavar="ID")
    start.add_argument("--service-command", help="explicit command to run from the private clone on every start/resume; it must bind 0.0.0.0:$STNT_PORT")
    start.add_argument("--service-url", help="fixed loopback HTTP(S) origin to publish on every start/resume, for example https://app.example.test:8010")
    start.add_argument("--health-path", default="/", help="HTTP health path on the one published service (default: /)")
    new = subparsers.add_parser("new", help="create another independent session")
    new.add_argument("--from", dest="from_branch", metavar="LOCAL_BRANCH", help="start the private clone from an existing local branch without switching the host checkout")
    new.add_argument("--service-command", help="explicit foreground service command")
    new.add_argument("--service-url", help="fixed loopback HTTP(S) service origin")
    new.add_argument("--health-path", default="/", help="HTTP health path (default: /)")
    for name, help_text in (
        ("recover-create", "resume an interrupted creation"),
        ("pause", "stop and retain the current session"),
        ("finish", "destructively remove a workspace or preserve/remove an archived legacy session"),
        ("detach", "preserve and remove the sandbox while leaving the Amp thread unchanged"),
        ("open", "open the selected workspace's verified service URL"),
        ("editor", "start the selected session and open its private clone in VS Code"),
    ):
        action = subparsers.add_parser(name, help=help_text)
        action.add_argument("--session", dest="session", default=argparse.SUPPRESS, metavar="ID")
    doctor = subparsers.add_parser("doctor", help="run read-only prerequisite and reconciliation diagnostics")
    doctor.add_argument("--json", action="store_true", help="emit the narrow local diagnostics JSON schema")
    prune = subparsers.add_parser("prune", help="discard and remove every Stnt-owned sandbox")
    prune.add_argument("--force", action="store_true", help="skip the destructive confirmation")
    subparsers.add_parser("show", help="interactively manage Stnt sessions")
    subparsers.add_parser("setup", help="initialize local Stnt state and print approval-aware setup guidance")
    subparsers.add_parser("init", help="statically discover and review this repository's execution profile")
    subparsers.add_parser("reconfigure", help="review drift and atomically replace this repository's execution profile")
    config = subparsers.add_parser("config", help="inspect repository configuration")
    config_subparsers = config.add_subparsers(dest="config_command", required=True)
    config_subparsers.add_parser("show", help="show the reviewed profile, provenance, and evidence drift")
    stack = subparsers.add_parser("stack", help="explicitly configure and run a two-repository proof stack")
    stack_subparsers = stack.add_subparsers(dest="stack_command", required=True)
    stack_init = stack_subparsers.add_parser("init", help="review and save an explicit user-local stack")
    stack_init.add_argument("name")
    stack_init.add_argument("--frontend", required=True, metavar="PATH")
    stack_init.add_argument("--backend", required=True, metavar="PATH")
    stack_init.add_argument("--ingress-port", required=True, type=int, metavar="PORT")
    stack_init.add_argument("--frontend-command", required=True, metavar="JSON_ARGV")
    stack_init.add_argument("--backend-command", required=True, metavar="JSON_ARGV")
    for name, help_text in (
        ("start", "create or resume the named stack and attach its one Amp thread"),
        ("pause", "stop and retain every service and private clone in the named stack"),
        ("finish", "preserve both repositories and remove an archived stack"),
        ("detach", "preserve both repositories and remove the stack while leaving Amp unchanged"),
    ):
        action = stack_subparsers.add_parser(name, help=help_text)
        action.add_argument("name")
    subparsers.add_parser("list", help="list all durable sessions without mutation")
    proxy = subparsers.add_parser(
        "ssh-proxy", help="internal exact-alias editor transport for managed SSH configuration"
    )
    proxy.add_argument("alias")
    known_hosts = subparsers.add_parser(
        "ssh-known-hosts", help="internal exact-alias host-key lookup for managed SSH configuration"
    )
    known_hosts.add_argument("alias")
    return parser.parse_args(list(argv))


def read_only_inventory() -> tuple[
    Dict[str, Dict[str, str]], bool, Dict[str, Dict[str, Any]], bool, List[str]
]:
    """Return Amp titles, sandboxes, and lookup warnings using list/export only."""
    warnings: List[str] = []
    threads: Dict[str, Dict[str, str]] = {}
    amp_valid = False
    listed = run([str(THREADS), "list"], check=False)
    if listed.returncode == 0:
        try:
            entries = json.loads(listed.stdout)
            if not isinstance(entries, list):
                raise ValueError
            for entry in entries:
                if (not isinstance(entry, dict) or set(entry) != {"id", "title", "status"} or
                        not isinstance(entry.get("id"), str) or not THREAD_RE.fullmatch(entry["id"]) or
                        not isinstance(entry.get("title"), str) or
                        entry.get("status") not in {"active", "archived"}):
                    raise ValueError
                if entry["id"] in threads:
                    raise ValueError
                threads[entry["id"]] = {"title": entry["title"], "status": entry["status"]}
            amp_valid = True
        except (json.JSONDecodeError, ValueError):
            warnings.append("Amp list returned malformed or duplicate data")
            threads = {}
    else:
        warnings.append("Amp list lookup failed")
    sandboxes: Dict[str, Dict[str, Any]] = {}
    sandbox_valid = False
    inventory = run([str(RUNTIME), "list"], check=False)
    if inventory.returncode == 0:
        try:
            parsed = json.loads(inventory.stdout)
            entries = parsed.get("sandboxes") if isinstance(parsed, dict) else None
            if not isinstance(entries, list):
                raise ValueError
            ids = set()
            for entry in entries:
                if (not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or
                        not entry["name"] or not isinstance(entry.get("id"), str) or
                        not isinstance(entry.get("workspaces"), list) or
                        any(not isinstance(workspace, str) for workspace in entry["workspaces"])):
                    raise ValueError
                if entry["name"] in sandboxes or entry["id"] in ids or not entry["id"]:
                    raise ValueError
                sandboxes[entry["name"]] = entry
                ids.add(entry["id"])
            sandbox_valid = True
        except (json.JSONDecodeError, ValueError):
            warnings.append("sandbox list returned malformed or duplicate data")
            sandboxes = {}
    else:
        warnings.append("sandbox list lookup failed")
    return threads, amp_valid, sandboxes, sandbox_valid, warnings


def select_session(repo: Path, requested: Optional[str], sessions: List[tuple[Path, Dict[str, Any]]]) -> tuple[Path, Dict[str, Any]]:
    ordered = sorted(sessions, key=lambda item: (item[1]["createdAt"], record_selector(item[1])))
    if requested:
        matches = [
            item for item in ordered
            if item[1].get("threadID") == requested or item[1].get("workspaceID") == requested
        ]
        if len(matches) != 1:
            raise StntError(f"no session {requested} exists for repository {repo}")
        return matches[0]
    if len(ordered) == 1:
        return ordered[0]
    if not ordered:
        raise StntError("no Stnt session exists for this repository")
    threads, _, _, _, warnings = read_only_inventory()
    rows = []
    for item in ordered:
        record = item[1]
        if record.get("lifecycleOwner") == "workspace":
            rows.append(
                f"{record['workspaceID']}  (guest threads)  [{record['status']}]  "
                f"stnt --session {record['workspaceID']}"
            )
        else:
            rows.append(
                f"{record['threadID']}  "
                f"{(threads.get(record['threadID']) or {}).get('title') or '(untitled)'}  "
                f"[{(threads.get(record['threadID']) or {}).get('status') or 'ambiguous'}]  "
                f"stnt --session {record['threadID']}"
            )
    if sys.stdin.isatty():
        print("Select a session:")
        for index, row in enumerate(rows, 1):
            print(f"{index}. {row}")
        for warning in warnings:
            print(f"warning: {warning}", file=sys.stderr)
        try:
            choice = int(input("Session number: "))
        except (ValueError, EOFError):
            raise StntError("invalid session selection")
        if not 1 <= choice <= len(ordered):
            raise StntError("invalid session selection")
        return ordered[choice - 1]
    detail = "\n".join(rows + warnings)
    raise StntError(f"multiple sessions exist; select one explicitly:\n{detail}")


def command_list() -> None:
    sessions = load_sessions()
    threads, amp_valid, sandboxes, sandbox_valid, warnings = read_only_inventory()
    for _, record in sorted(sessions, key=lambda item: (item[1]["repositoryPath"], item[1]["createdAt"], record_selector(item[1]))):
        tid = record.get("threadID")
        workspace_owned = record.get("lifecycleOwner") == "workspace"
        thread = None if workspace_owned else threads.get(tid)
        title = "(guest threads)" if workspace_owned else (thread.get("title") if thread else None)
        thread_observed = "workspace-owned" if workspace_owned else (thread.get("status") if thread else "ambiguous")
        if not workspace_owned and thread is None and amp_valid:
            exported_status = empty_thread_export_status(tid)
            thread_observed = "empty/retained-omission" if exported_status == "empty" else (exported_status or "ambiguous")
        sandbox = sandboxes.get(record["sandbox"])
        identity_match = sandbox is not None and sandbox.get("id") == record.get("sandboxID")
        workspace_match = sandbox is not None and record["repositoryPath"] in sandbox["workspaces"]
        if sandbox is None:
            observed = "lookup-ambiguous" if not sandbox_valid else "missing-stale"
        elif not identity_match:
            observed = "identity-mismatch"
        elif not workspace_match:
            observed = "workspace-mismatch"
        else:
            observed = str(sandbox.get("state", sandbox.get("status", "present")))
        sandbox_id = "?" if sandbox is None else sandbox["id"]
        unsafe = (not workspace_owned and thread_observed == "ambiguous") or observed in {
            "lookup-ambiguous", "missing-stale", "identity-mismatch", "workspace-mismatch"
        }
        selector = record.get("workspaceID", tid) if workspace_owned else tid
        if unsafe:
            next_command = "stnt list"
        elif workspace_owned:
            next_command = f"stnt --session {selector} " + ("recover-create" if record["status"] == "creating" else "start")
        else:
            next_command = f"stnt --session {tid} " + ("finish" if thread_observed == "archived" else ("recover-create" if record["status"] == "creating" else ("finish" if record["status"] == "archived" else "start")))
        source_branch = record.get("sourceBranch", record["baseBranch"])
        source_sha = record.get("sourceSHA", record["baseSHA"])
        print(f"repo={record['repositoryPath']} source={source_branch}@{source_sha} workspace={record.get('workspaceID', '-')} thread={tid or '-'} title={title or '(untitled)'} threadState={thread_observed} lifecycle={record['status']} sandbox={record['sandbox']} id={sandbox_id} state={observed} next={next_command}")
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)


def _read_menu_key() -> str:
    descriptor = sys.stdin.fileno()
    previous = termios.tcgetattr(descriptor)
    try:
        tty.setraw(descriptor)
        first = os.read(descriptor, 1)
        if first == b"\x03":
            raise KeyboardInterrupt
        if first in {b"\r", b"\n"}:
            return "enter"
        if first in {b"q", b"Q"}:
            return "quit"
        if first == b"\x1b" and select.select([descriptor], [], [], 0.05)[0]:
            second = os.read(descriptor, 1)
            if second == b"[" and select.select([descriptor], [], [], 0.05)[0]:
                third = os.read(descriptor, 1)
                if third == b"A":
                    return "up"
                if third == b"B":
                    return "down"
        return "other"
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, previous)


def terminal_menu(title: str, rows: Sequence[str]) -> Optional[int]:
    if not rows:
        return None
    selected = 0
    while True:
        print("\033[2J\033[H", end="")
        print(title)
        print("↑/↓ select  •  Enter confirm  •  q back\n")
        for index, row in enumerate(rows):
            print(f"{'›' if index == selected else ' '} {row}")
        sys.stdout.flush()
        key = _read_menu_key()
        if key == "up":
            selected = (selected - 1) % len(rows)
        elif key == "down":
            selected = (selected + 1) % len(rows)
        elif key == "enter":
            print("\033[2J\033[H", end="", flush=True)
            return selected
        elif key == "quit":
            print("\033[2J\033[H", end="", flush=True)
            return None


def inspect_workspace_exit(record: Dict[str, Any]) -> Dict[str, Any]:
    found = require_sandbox(record)
    observed_state = str(found.get("state", found.get("status", ""))).lower()
    if observed_state not in {"running", "stopped"}:
        raise StntError("sandbox running state is ambiguous; workspace was retained")
    was_stopped = observed_state == "stopped"
    temporary_ref = f"refs/stnt/upstream-check/{compact_workspace_id(record['workspaceID'])}/{uuid.uuid4()}"
    script = r'''
set -euo pipefail
temporary_ref=$1
cleanup() { git update-ref -d "$temporary_ref" >/dev/null 2>&1 || true; }
trap cleanup EXIT
status=$(git status --porcelain=v1 --untracked-files=all)
tracked=$(printf '%s\n' "$status" | awk 'NF && substr($0,1,2) != "??" { count++ } END { print count + 0 }')
untracked=$(printf '%s\n' "$status" | awk 'substr($0,1,2) == "??" { count++ } END { print count + 0 }')
branch=$(git symbolic-ref --quiet --short HEAD)
upstream=$(git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null || true)
ahead=unknown
behind=unknown
if [[ -n $upstream ]]; then
  remote=$(git config --get "branch.$branch.remote" || true)
  merge_ref=$(git config --get "branch.$branch.merge" || true)
  if [[ -n $remote && $merge_ref == refs/heads/* ]] &&
     git fetch --quiet --no-tags --no-write-fetch-head --force "$remote" "$merge_ref:$temporary_ref"; then
    ahead=$(git rev-list --count "$temporary_ref..HEAD")
    behind=$(git rev-list --count "HEAD..$temporary_ref")
  fi
fi
printf '%s\n%s\n%s\n%s\n%s\n%s\n' "$tracked" "$untracked" "$branch" "$upstream" "$ahead" "$behind"
'''
    try:
        inspected = run([
            str(RUNTIME), "exec", record["sandbox"], "bash", "--noprofile", "--norc", "-c",
            script, "stnt-exit-inspect", temporary_ref,
        ], check=False)
    finally:
        if was_stopped:
            stopped = run_lifecycle([str(RUNTIME), "stop", record["sandbox"]], check=False)
            if stopped.returncode != 0:
                raise StntError(
                    "exit inspection started the sandbox but could not stop it; runtime state is ambiguous"
                )
    lines = inspected.stdout.splitlines()
    if (inspected.returncode != 0 or len(lines) != 6 or
            any(not re.fullmatch(r"\d+", value) for value in lines[:2]) or
            not lines[2] or
            any(value != "unknown" and not re.fullmatch(r"\d+", value) for value in lines[4:])):
        raise StntError("private-clone exit inspection was ambiguous; workspace was retained")
    return {
        "tracked": int(lines[0]),
        "untracked": int(lines[1]),
        "branch": lines[2],
        "upstream": lines[3] or None,
        "ahead": None if lines[4] == "unknown" else int(lines[4]),
        "behind": None if lines[5] == "unknown" else int(lines[5]),
    }


def workspace_exit_summary(inspection: Dict[str, Any]) -> str:
    changes = (
        f"Private clone: {inspection['tracked']} tracked change"
        f"{'s' if inspection['tracked'] != 1 else ''}, "
        f"{inspection['untracked']} untracked file"
        f"{'s' if inspection['untracked'] != 1 else ''}."
    )
    if inspection["ahead"] is None:
        upstream = inspection["upstream"] or "not configured"
        remote = f"Upstream: {upstream}; remote status unknown (refresh could not be proved)."
    else:
        remote = (
            f"Upstream: {inspection['upstream']}; {inspection['ahead']} commit"
            f"{'s' if inspection['ahead'] != 1 else ''} not on upstream, "
            f"{inspection['behind']} commit{'s' if inspection['behind'] != 1 else ''} behind."
        )
    return f"Branch: {inspection['branch']}\n{changes}\n{remote}"


def confirm_destructive_finish(record: Dict[str, Any], inspection: Dict[str, Any]) -> bool:
    selector = record_selector(record)
    print(workspace_exit_summary(inspection))
    print(
        "Finish permanently removes the private clone, guest threads, transcripts, "
        "and any work not present elsewhere."
    )
    try:
        response = input(f'Type "finish {selector}" to permanently remove this workspace: ')
    except EOFError:
        return False
    return response.strip() == f"finish {selector}"


def remove_workspace_without_preservation(
    path: Path, record: Dict[str, Any],
    authorization: Optional[EditorAuthorization] = None,
) -> None:
    require_sandbox(record)
    revoke_editor_authorization(record, path, authorization)
    with editor_drain(record, exclusive=True):
        with progress_indicator(f"permanently removing {record['sandbox']}"):
            removed = run_lifecycle([str(RUNTIME), "remove", record["sandbox"]], check=False)
    if removed.returncode != 0:
        try:
            found = runtime_find(record["sandbox"])
            if found is not None:
                require_sandbox(record)
        except StntError as error:
            raise StntError(
                f"Finish outcome is indeterminate for {record_selector(record)}; state retained at {path}. "
                "Inspect with: stnt list"
            ) from error
        if found is None:
            raise StntError(
                f"Finish outcome is indeterminate for {record_selector(record)}; state retained at {path}. "
                "Inspect with: stnt list"
            )
        raise StntError(
            f"Finish failed; workspace {record_selector(record)} and its state were retained"
        )
    try:
        remove_state(path)
    except OSError as error:
        raise StntError(
            "workspace was removed but state cleanup was interrupted; inspect with: stnt list"
        ) from error
    print(f"finished {record_selector(record)}; permanently removed sandbox and workspace state")


def command_destructive_finish(path: Path, record: Dict[str, Any]) -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise StntError("destructive Finish requires an interactive terminal; nothing was removed")
    inspection = inspect_workspace_exit(record)
    print()
    if not confirm_destructive_finish(record, inspection):
        raise StntError("Finish cancelled; workspace was retained")
    remove_workspace_without_preservation(path, record)


def workspace_exit_decision(
    record: Dict[str, Any], path: Path,
    authorization: Optional[EditorAuthorization] = None,
) -> str:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        if authorization is None:
            pause_after_exit(record, path)
        else:
            pause_after_exit(record, path, authorization)
        return "pause"
    try:
        with progress_indicator("checking private-clone and upstream state"):
            inspection = inspect_workspace_exit(record)
    except StntError as error:
        print(f"warning: {error}", file=sys.stderr)
        if authorization is None:
            pause_after_exit(record, path)
        else:
            pause_after_exit(record, path, authorization)
        return "pause"
    choice = terminal_menu(
        f"Workspace exit\n\n{workspace_exit_summary(inspection)}",
        [
            "Pause — stop and retain the complete workspace (safe default)",
            "Finish — permanently discard the workspace and guest history",
            "Cancel — return to Amp",
        ],
    )
    if choice in {None, 2}:
        return "cancel"
    if choice == 0:
        if authorization is None:
            pause_after_exit(record, path)
        else:
            pause_after_exit(record, path, authorization)
        return "pause"
    print()
    if not confirm_destructive_finish(record, inspection):
        return "cancel"
    if authorization is None:
        remove_workspace_without_preservation(path, record)
    else:
        remove_workspace_without_preservation(path, record, authorization)
    return "finish"


def inspect_discard_changes(record: Dict[str, Any]) -> tuple[int, int]:
    found = require_sandbox(record)
    observed_state = str(found.get("state", found.get("status", ""))).lower()
    if observed_state not in {"running", "stopped"}:
        raise StntError("sandbox running state is ambiguous; deletion refused")
    was_stopped = observed_state == "stopped"
    baseline = record.get("sourceSHA", record["baseSHA"])
    script = r'''
set -euo pipefail
baseline=$1
git cat-file -e "$baseline^{commit}"
git symbolic-ref --quiet HEAD >/dev/null
git rev-parse --verify HEAD^{commit} >/dev/null
dirty=$(git status --porcelain=v1 --untracked-files=all | wc -l | tr -d ' ')
changed=0
while read -r sha; do
  if ! git merge-base --is-ancestor "$sha" "$baseline"; then
    changed=$((changed + 1))
  fi
done < <(git for-each-ref --format='%(objectname)' refs/heads)
printf '%s\n%s\n' "$dirty" "$changed"
'''
    inspected = run_lifecycle([
        str(RUNTIME), "exec", record["sandbox"], "bash", "--noprofile", "--norc", "-c",
        script, "stnt-discard-inspect", baseline,
    ], check=False)
    if was_stopped:
        stopped = run_lifecycle([str(RUNTIME), "stop", record["sandbox"]], check=False)
        if stopped.returncode != 0:
            raise StntError("discard inspection started the sandbox but could not stop it; deletion refused")
    lines = inspected.stdout.splitlines()
    if (inspected.returncode != 0 or len(lines) != 2 or
            any(not re.fullmatch(r"\d+", line) for line in lines)):
        raise StntError("private-clone change inspection was ambiguous; deletion refused")
    return int(lines[0]), int(lines[1])


def command_delete_session(path: Path, record: Dict[str, Any]) -> None:
    dirty, changed_branches = inspect_discard_changes(record)
    if dirty or changed_branches:
        print(
            f"{record['sandbox']} has {dirty} dirty/untracked entr{'y' if dirty == 1 else 'ies'} "
            f"and {changed_branches} branch{'es' if changed_branches != 1 else ''} with commits "
            "beyond the session source."
        )
        accepted = input('Type "delete" to permanently discard this work: ').strip() == "delete"
    else:
        accepted = input(f"Delete {record['sandbox']} and forget this session? [y/N] ").strip().lower() == "y"
    if not accepted:
        raise StntError("delete cancelled; nothing was removed")
    with progress_indicator(f"deleting {record['sandbox']}"):
        removed = run_lifecycle([str(RUNTIME), "remove", record["sandbox"]], check=False)
    if removed.returncode != 0:
        raise StntError(
            f"delete failed; session state was retained. Retry from stnt show or: stnt prune"
        )
    try:
        remove_state(path)
    except OSError as error:
        raise StntError("sandbox was removed but session cleanup was interrupted; run: stnt prune") from error
    if record.get("lifecycleOwner") == "workspace":
        print(f"deleted {record['sandbox']}; workspace state was removed")
    else:
        print(f"deleted {record['sandbox']}; Amp thread {record['threadID']} was left unchanged")


def command_show() -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise StntError("show requires an interactive terminal; use stnt list in scripts")
    sessions = load_sessions()
    if not sessions:
        print("No Stnt sessions. Orphaned sandboxes, if any, can be removed with: stnt prune")
        return
    threads, amp_valid, sandboxes, sandbox_valid, warnings = read_only_inventory()
    needs_host_amp = any(record.get("lifecycleOwner") != "workspace" for _, record in sessions)
    if (needs_host_amp and not amp_valid) or not sandbox_valid:
        raise StntError("session authority is ambiguous; use stnt list for recovery details")
    ordered = sorted(sessions, key=lambda item: (item[1]["repositoryPath"], item[1]["createdAt"], record_selector(item[1])))
    rows = []
    for _, record in ordered:
        if record.get("lifecycleOwner") == "workspace":
            thread = {"title": "(guest threads)", "status": "workspace-owned"}
        else:
            thread = threads.get(record["threadID"])
        if thread is None:
            exported = empty_thread_export_status(record["threadID"])
            if exported in {"empty", "archived"}:
                thread = {"title": "", "status": exported}
                threads[record["threadID"]] = thread
            else:
                raise StntError(
                    f"thread {record['threadID']} authority is ambiguous; use stnt list for recovery details"
                )
        sandbox = sandboxes.get(record["sandbox"])
        if (sandbox is None or sandbox.get("id") != record.get("sandboxID") or
                record["repositoryPath"] not in sandbox.get("workspaces", [])):
            raise StntError(
                f"sandbox {record['sandbox']} identity is ambiguous; use stnt list for recovery details"
            )
        state = str((sandbox or {}).get("state", (sandbox or {}).get("status", "missing")))
        identity = record_selector(record)
        rows.append(
            f"{Path(record['repositoryPath']).name:<24} {thread.get('title') or '(untitled)':<36} "
            f"{state:<8} {identity}"
        )
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    selected_index = terminal_menu("Stnt sessions", rows)
    if selected_index is None:
        return
    path, record = ordered[selected_index]
    thread_state = (
        (threads.get(record["threadID"]) or {}).get("status", "empty")
        if record.get("lifecycleOwner") != "workspace" else "workspace-owned"
    )
    actions = []
    if ((thread_state == "archived" or record["status"] == "archived") and
            record.get("lifecycleOwner") != "workspace"):
        actions.append(("Finish — preserve committed work and remove sandbox", "finish"))
    else:
        actions.extend([
            ("Start/resume", "start"),
            ("Pause", "pause"),
            ("Open editor", "editor"),
        ])
        if "serviceURL" in record:
            actions.insert(2, ("Open service", "open"))
        if record.get("lifecycleOwner") == "workspace":
            actions.append(("Finish — permanently discard workspace and guest history", "finish"))
    if record.get("lifecycleOwner") != "workspace":
        actions.append(("Delete — permanently discard private-clone work", "delete"))
    actions.append(("Back", "back"))
    action_index = terminal_menu(record_selector(record), [label for label, _ in actions])
    if action_index is None or actions[action_index][1] == "back":
        return
    action = actions[action_index][1]
    repo = Path(record["repositoryPath"])
    with lifecycle_gate(), session_lock(record_lock_identity(record)):
        current = load_state(path)
        if current is None or record_lock_identity(current) != record_lock_identity(record):
            raise StntError("selected session changed; reopen: stnt show")
        if action == "start":
            start_session(current, path)
        elif action == "pause":
            command_pause(repo, (path, current))
        elif action == "open":
            command_open(repo, (path, current))
        elif action == "editor":
            command_editor(repo, (path, current))
        elif action == "finish":
            command_finish(repo, (path, current))
        else:
            command_delete_session(path, current)


def command_doctor(*, json_output: bool = False) -> int:
    checks = doctor_results(optional_repository())
    if json_output:
        print(json.dumps({"schemaVersion": 1, "checks": checks}, indent=2, sort_keys=True))
    else:
        print_doctor(checks)
        print("\nRead-only: no thread, sandbox, Git, policy, secret, SSH, editor, clipboard, or daemon state was changed.")
    return 1 if any(check["status"] == "blocked" for check in checks) else 0


def configure_clipboard_image_paste() -> bool:
    enabled = clipboard_image_paste_enabled()
    if enabled is True:
        print("Docker Sandbox image paste is enabled for the guest Amp TUI.")
        print("  revoke host clipboard access: sbx settings set clipboard.imagePaste false")
        return False
    if enabled is None:
        print("Docker Sandbox image-paste setting is unavailable or unreadable.")
        print("  inspect: sbx settings get --json clipboard.imagePaste")
        return False

    print("Docker Sandbox image paste is disabled.")
    print("Enabling it lets sandboxed agents request PNG data from your host clipboard only when you press Ctrl+V.")
    print("Docker does not cache or log the clipboard image, but this still allows host data to cross the sandbox boundary.")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("  enable explicitly: sbx settings set clipboard.imagePaste true")
        return False
    if input("Enable host clipboard image paste for Docker Sandboxes? [y/N] ").strip().lower() != "y":
        print("Docker Sandbox image paste remains disabled.")
        return False

    changed = run([str(RUNTIME), "clipboard-image-paste-enable"], check=False)
    if changed.returncode != 0 or clipboard_image_paste_enabled() is not True:
        raise StntError("Docker Sandbox image paste could not be enabled; inspect: sbx settings get --json clipboard.imagePaste")
    print("Docker Sandbox image paste enabled. Revoke with: sbx settings set clipboard.imagePaste false")
    return True


def credential_binding_status(command: str) -> Optional[Dict[str, bool]]:
    status = run([str(RUNTIME), command], check=False)
    try:
        value = json.loads(status.stdout)
    except json.JSONDecodeError:
        return None
    if (status.returncode != 0 or not isinstance(value, dict) or
            set(value) != {"approved", "fileExists"} or
            not all(isinstance(value[field], bool) for field in value)):
        return None
    return value


def configure_stnt_bindings() -> bool:
    amp_status = credential_binding_status("amp-binding-status")
    github_status = credential_binding_status("github-binding-status")
    if (amp_status is not None and amp_status["approved"] and
            github_status is not None and github_status["approved"]):
        print("Docker permits Stnt kits to use Amp on ampcode.com and GitHub on github.com only.")
        return False
    if amp_status is None or github_status is None:
        print("Docker credential-binding state is unavailable or unreadable.")
        print("  inspect: bin/docker-sandbox amp-binding-status")
        print("  inspect: bin/docker-sandbox github-binding-status")
        return False
    if amp_status["fileExists"] or github_status["fileExists"]:
        print("Docker credential bindings already exist without both exact Stnt approvals.")
        print("Stnt will not rewrite or merge that shared file; add the documented ampcode.com and github.com bindings manually.")
        return False

    print("Docker has not approved Amp and GitHub credential use by third-party Stnt kits.")
    print("This approval contains no token. It permits only the stored apiKeys to be injected into ampcode.com and github.com.")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("  run interactively: stnt setup")
        return False
    if input("Approve proxy-managed Amp and GitHub credentials for Stnt kits on their exact domains? [y/N] ").strip().lower() != "y":
        print("Docker Stnt credential bindings were not created.")
        return False

    changed = run([str(RUNTIME), "stnt-bindings-enable"], check=False)
    verified_amp = credential_binding_status("amp-binding-status")
    verified_github = credential_binding_status("github-binding-status")
    if (changed.returncode != 0 or verified_amp is None or not verified_amp["approved"] or
            verified_github is None or not verified_github["approved"]):
        raise StntError("Docker Stnt credential bindings could not be created; no existing binding file was overwritten")
    print("Docker Stnt credential bindings created for ampcode.com and github.com only.")
    return True


STNT_SSH_BEGIN = "# >>> stnt editor transport (managed) >>>"
STNT_SSH_END = "# <<< stnt editor transport (managed) <<<"
DOCKER_SSH_BEGIN = "# >>> docker sandboxes (managed) >>>"


def stnt_executable() -> Path:
    discovered = shutil.which("stnt")
    path = Path(discovered) if discovered else ROOT / "bin/stnt"
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise StntError("Stnt executable path is unavailable; install or link bin/stnt on PATH")
    return path


def ssh_config_quote(value: str) -> str:
    if not value or any(character in value for character in {'"', "\n", "\r"}):
        raise StntError("SSH configuration path contains unsupported characters")
    return f'"{value}"'


def stnt_ssh_block(executable: Path) -> str:
    command = ssh_config_quote(str(executable))
    return (
        f"{STNT_SSH_BEGIN}\n"
        "Host *.stnt.sbx\n"
        "    User _default_user_\n"
        f"    ProxyCommand {command} ssh-proxy %n\n"
        "    IdentityAgent none\n"
        "    IdentityFile /dev/null\n"
        "    IdentitiesOnly yes\n"
        "    ControlMaster no\n"
        "    ControlPath none\n"
        "    UserKnownHostsFile /dev/null\n"
        f"    KnownHostsCommand {command} ssh-known-hosts %H\n"
        "    StrictHostKeyChecking yes\n"
        f"{STNT_SSH_END}\n"
    )


def install_stnt_ssh_config() -> bool:
    ssh_directory = Path.home() / ".ssh"
    config = ssh_directory / "config"
    if ssh_directory.exists() and (ssh_directory.is_symlink() or not ssh_directory.is_dir()):
        raise StntError(f"SSH directory is not a regular directory; refusing to change it: {ssh_directory}")
    if config.is_symlink() or (config.exists() and not config.is_file()):
        raise StntError(f"SSH config is not a regular file; refusing to change it: {config}")
    try:
        contents = config.read_text() if config.exists() else ""
    except OSError as error:
        raise StntError(f"SSH config is unreadable; refusing to change it: {config}") from error
    block = stnt_ssh_block(stnt_executable())
    start, end = contents.find(STNT_SSH_BEGIN), contents.find(STNT_SSH_END)
    if (contents.count(STNT_SSH_BEGIN) != contents.count(STNT_SSH_END) or
            contents.count(STNT_SSH_BEGIN) > 1 or
            (start >= 0 and end < start)):
        raise StntError("SSH config contains an incomplete Stnt managed block; refusing to rewrite it")
    if start >= 0:
        end += len(STNT_SSH_END)
        remainder = (contents[:start] + contents[end:]).strip("\n")
    else:
        remainder = contents.strip("\n")
    replacement = block + (f"\n{remainder}\n" if remainder else "")
    if replacement == contents:
        return False
    atomic_write_text(config, replacement)
    return True


def configure_stnt_ssh() -> bool:
    config = Path.home() / ".ssh/config"
    if config.is_file() and not config.is_symlink():
        try:
            if (stnt_ssh_configured() and
                    stnt_ssh_block(stnt_executable()).rstrip() in config.read_text()):
                print("Stnt editor SSH transport is installed and takes precedence over Docker's broad alias.")
                return False
        except OSError:
            pass
    print("Stnt needs one narrow SSH alias namespace to prevent editor reconnects from restarting paused sandboxes.")
    print("It accepts only exact durable Stnt workspace aliases and delegates them to Docker's exact sandbox transport.")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("  install interactively: stnt setup")
        return False
    if input("Install the managed Host *.stnt.sbx SSH block? [y/N] ").strip().lower() != "y":
        print("Stnt editor SSH transport was not installed.")
        return False
    changed = install_stnt_ssh_config()
    print("Stnt editor SSH transport installed." if changed else "Stnt editor SSH transport was already current.")
    return changed


def command_setup() -> int:
    ensure_state_layout()
    checks = doctor_results(optional_repository())
    print_doctor(checks)
    print("\nStnt initialized only its local state directories.")
    binding_changed = configure_stnt_bindings()
    ssh_changed = configure_stnt_ssh()
    daemon_ready = any(check["id"] == "docker.daemon" and check["status"] == "pass" for check in checks)
    clipboard_changed = configure_clipboard_image_paste() if daemon_ready else False
    if binding_changed or ssh_changed or clipboard_changed:
        changes = []
        if binding_changed:
            changes.append("Stnt credential bindings")
        if ssh_changed:
            changes.append("Stnt editor SSH transport")
        if clipboard_changed:
            changes.append("clipboard image-paste setting")
        print(f"Only the explicitly approved local {' and '.join(changes)} changed.")
    else:
        print("No Docker login, policy, secret, binding, daemon, SSH, editor, or clipboard setting change was made.")
    print("Review each remaining command above and run it explicitly; global/shared changes require your approval.")
    return 1 if any(check["status"] == "blocked" for check in checks) else 0


def command_prune(*, force: bool = False) -> None:
    sessions = load_sessions()
    stacks = load_stack_states()
    records = sessions + stacks
    recorded_names = {record["sandbox"] for _, record in records}
    if len(recorded_names) != len(records):
        raise StntError("duplicate sandbox identity exists across Stnt state; nothing was removed")

    inventory = run([str(RUNTIME), "list"], check=False)
    if inventory.returncode != 0:
        raise StntError("sandbox inventory lookup failed; nothing was removed")
    try:
        parsed = json.loads(inventory.stdout)
        entries = parsed.get("sandboxes") if isinstance(parsed, dict) else None
        if not isinstance(entries, list):
            raise ValueError
        names = [entry.get("name") for entry in entries if isinstance(entry, dict)]
        if (len(names) != len(entries) or any(not isinstance(name, str) or not name for name in names) or
                len(set(names)) != len(names)):
            raise ValueError
    except (json.JSONDecodeError, ValueError) as error:
        raise StntError("sandbox inventory is malformed; nothing was removed") from error

    runtime_names = {name for name in names if name.startswith("stnt-") or name in recorded_names}
    targets = sorted(runtime_names | recorded_names)
    if not targets and not records:
        print("nothing to prune")
        return

    print("Stnt prune permanently discards private-clone work and removes:")
    for name in targets:
        suffix = " (state only; sandbox already absent)" if name not in runtime_names else ""
        print(f"  {name}{suffix}")
    print(f"  {len(records)} Stnt session/stack record(s)")
    print("Amp threads and host preservation branches are not removed.")
    sys.stdout.flush()

    if not force:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise StntError("prune requires an interactive confirmation; rerun explicitly with: stnt prune --force")
        if input('Type "prune" to continue: ').strip() != "prune":
            raise StntError("prune cancelled; nothing was removed")

    removed = 0
    for name in sorted(runtime_names):
        with progress_indicator(f"removing {name}"):
            result = run_lifecycle([str(RUNTIME), "remove", name], check=False)
        if result.returncode != 0:
            raise StntError(
                f"prune stopped after removing {removed} sandbox(es); all state records were retained. "
                "Inspect with: stnt list; then retry: stnt prune"
            )
        removed += 1

    try:
        for path, _ in records:
            remove_state(path)
    except OSError as error:
        raise StntError(
            "sandboxes were removed but state cleanup was interrupted; rerun: stnt prune"
        ) from error
    print(f"pruned {removed} Stnt sandbox(es) and {len(records)} state record(s)")


def vscode_server_identity(code: str) -> tuple[str, str]:
    version = run([code, "--version"], check=False)
    lines = version.stdout.splitlines()
    if (version.returncode != 0 or len(lines) < 3 or
            not re.fullmatch(r"[0-9a-f]{40}", lines[1]) or lines[2] != "arm64"):
        raise StntError("VS Code version output cannot identify the matching Linux ARM64 server")
    return lines[0], lines[1]


def editor_alias(record: Dict[str, Any]) -> str:
    identity = record_lock_identity(record)
    prefix = "w" if WORKSPACE_RE.fullmatch(identity) else "t"
    compact_identity = (
        compact_workspace_id(identity) if prefix == "w" else compact_thread_id(identity)
    )
    sandbox_id = record.get("sandboxID")
    if not isinstance(sandbox_id, str) or not sandbox_id:
        raise StntError("sandbox ID is absent from durable state; editor alias is unavailable")
    digest = hashlib.sha256(sandbox_id.encode()).hexdigest()[:12]
    return f"{prefix}{compact_identity}-{digest}.stnt.sbx"


def resolve_editor_alias(alias: str) -> tuple[Path, Dict[str, Any]]:
    if not isinstance(alias, str) or not EDITOR_ALIAS_RE.fullmatch(alias):
        raise StntError("invalid Stnt editor alias")
    matches = [item for item in load_sessions() if editor_alias(item[1]) == alias]
    if len(matches) != 1:
        raise StntError("Stnt editor alias does not resolve to exactly one durable workspace")
    path, record = matches[0]
    require_sandbox(record)
    return path, record


def editor_authorization_is_owned(record: Dict[str, Any]) -> bool:
    path = editor_lock_path(record, "authorization")
    with path.open("a+") as lock:
        os.chmod(path, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lock, fcntl.LOCK_UN)
        return False


def editor_alias_is_authorized(
    alias: str, path: Path, generation: str, expected_sandbox_id: str,
) -> bool:
    try:
        current_path, current = resolve_editor_alias(alias)
        return bool(
            current_path == path and
            current.get("sandboxID") == expected_sandbox_id and
            current.get("status") == "active" and
            current.get("editorAuthorization") == generation and
            editor_authorization_is_owned(current)
        )
    except (StntError, OSError):
        return False


def command_ssh_known_hosts(alias: str) -> None:
    resolve_editor_alias(alias)
    sbx = shutil.which("sbx")
    if not sbx:
        raise StntError("Docker Sandboxes CLI is unavailable")
    completed = run([sbx, "ssh", "known-hosts", alias], check=False, capture=False)
    if completed.returncode != 0:
        raise StntError("Docker SSH host key is unavailable")


def command_ssh_proxy(alias: str) -> None:
    path, record = resolve_editor_alias(alias)
    generation = record.get("editorAuthorization")
    sandbox_id = record.get("sandboxID")
    if (record.get("status") != "active" or not isinstance(generation, str) or
            not isinstance(sandbox_id, str)):
        raise StntError("workspace editor access is not active; resume it with stnt start")
    sbx = shutil.which("sbx")
    if not sbx:
        raise StntError("Docker Sandboxes CLI is unavailable")
    with editor_drain(record, exclusive=False):
        if not editor_alias_is_authorized(alias, path, generation, sandbox_id):
            raise StntError("workspace editor access was revoked before connection")
        process = subprocess.Popen([sbx, "ssh", "proxy", f"{record['sandbox']}.sbx"])
        try:
            while process.poll() is None:
                if not editor_alias_is_authorized(alias, path, generation, sandbox_id):
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    raise StntError("workspace editor access was revoked")
                time.sleep(0.2)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        if process.returncode != 0:
            raise StntError(f"Docker SSH proxy exited with status {process.returncode}")


def validate_vscode_server_archive(path: Path) -> None:
    try:
        with tarfile.open(path, "r:gz") as archive:
            members = archive.getmembers()
            if not members:
                raise StntError("cached VS Code server archive is empty")
            code_servers = []
            for member in members:
                pure = Path(member.name)
                if pure.is_absolute() or ".." in pure.parts:
                    raise StntError("cached VS Code server archive has an unsafe path")
                if member.issym() or member.islnk():
                    link = Path(member.linkname)
                    if link.is_absolute() or ".." in link.parts:
                        raise StntError("cached VS Code server archive has an unsafe link")
                if len(pure.parts) == 3 and pure.parts[1:] == ("bin", "code-server"):
                    code_servers.append(member)
            if len(code_servers) != 1 or not (code_servers[0].mode & 0o100):
                raise StntError("cached VS Code server archive has an incompatible layout")
    except (OSError, tarfile.TarError) as error:
        raise StntError(f"cached VS Code server archive is unreadable: {path}") from error


def cached_vscode_server(code: str) -> tuple[str, Path]:
    version, commit = vscode_server_identity(code)
    directory = cache_home() / "editor/vscode" / commit
    archive = directory / "vscode-server-linux-arm64.tar.gz"
    if archive.exists():
        if not archive.is_file() or archive.is_symlink():
            raise StntError(f"VS Code server cache path is not a regular file: {archive}")
        validate_vscode_server_archive(archive)
        return commit, archive
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    url = f"https://update.code.visualstudio.com/commit:{commit}/server-linux-arm64/stable"
    temporary = directory / f".{archive.name}.{os.getpid()}"
    if VERBOSE:
        print(f"stnt: downloading VS Code {version} Linux ARM64 server to the host cache", file=sys.stderr)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "stnt/phase1c"})
        with urllib.request.urlopen(request, timeout=180) as response, open(temporary, "xb") as output:
            os.chmod(temporary, 0o600)
            shutil.copyfileobj(response, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        validate_vscode_server_archive(temporary)
        os.replace(temporary, archive)
        fsync_directory(directory)
    except BaseException as error:
        temporary.unlink(missing_ok=True)
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        raise StntError(f"VS Code server download failed; retry: stnt editor ({url})") from error
    return commit, archive


def prepare_vscode_server(record: Dict[str, Any], code: str) -> None:
    _, commit = vscode_server_identity(code)
    status_args = [str(RUNTIME), "editor-server-status", record["sandbox"], commit]
    if run(status_args, check=False).returncode == 0:
        return
    commit, archive = cached_vscode_server(code)
    installed = run([
        str(RUNTIME), "editor-server-install", record["sandbox"], commit, str(archive),
    ], check=False, capture=False)
    if installed.returncode != 0 or run(status_args, check=False).returncode != 0:
        raise StntError(
            f"VS Code server prewarm failed; cache retained at {archive}. Retry: "
            f"stnt --session {record_selector(record)} editor; inspect SSH: ssh {record['sandbox']}.sbx"
        )


def vscode_editor_command(*, required: bool) -> Optional[str]:
    prerequisite_check = (
        timed("editor.prerequisites", "checking editor prerequisites")
        if required else nullcontext()
    )
    with prerequisite_check:
        code = vscode_command()
        if not code:
            if required:
                raise StntError("VS Code is unavailable; install it before opening the sandbox-private clone")
            return None
        extensions = run([code, "--list-extensions"], check=False)
        if extensions.returncode != 0 or not any(
            extension.lower() == "ms-vscode-remote.remote-ssh"
            for extension in extensions.stdout.splitlines()
        ):
            if required:
                raise StntError(
                    "VS Code Remote SSH is unavailable; install explicitly with: "
                    f"{shell_quote(code)} --install-extension ms-vscode-remote.remote-ssh"
                )
            return None
        if not vscode_remote_ssh_offline_ready():
            if required:
                raise StntError(
                    "VS Code Remote SSH cannot bootstrap reliably inside a network-restricted sandbox. "
                    'Set "remote.SSH.localServerDownload" to "always" and '
                    '"remote.SSH.useExecServer" to false in VS Code user settings, restart VS Code, '
                    "then run this command again"
                )
            return None
    return code


def open_vscode_editor(record: Dict[str, Any], code: str) -> None:
    with timed("editor.prewarm", "preparing matching VS Code server"):
        prepare_vscode_server(record, code)
    timing_milestone("editor.backendReady")
    host = editor_alias(record)
    remote_path = quote(record["repositoryPath"], safe="/")
    folder_uri = f"vscode-remote://ssh-remote+{host}{remote_path}"
    with timed("editor.launch", "opening sandbox-private clone in VS Code"):
        opened = run([code, "--new-window", "--folder-uri", folder_uri], check=False, capture=False)
    if opened.returncode != 0:
        raise StntError(
            f"VS Code could not open {host}; verify SSH explicitly with: ssh {shell_quote(host)}"
        )
    timing_milestone("editor.launchAccepted")


def automatic_editor_handoff(record: Dict[str, Any]) -> None:
    try:
        code = vscode_editor_command(required=False)
        if code is None:
            return
        open_vscode_editor(record, code)
    except (StntError, OSError) as error:
        selector = record_selector(record)
        print(
            f"stnt: editor handoff failed; continuing terminal-only: {error}. "
            f"Retry explicitly with: stnt --session {selector} editor",
            file=sys.stderr,
        )


def command_editor(repo: Path, selected: tuple[Path, Dict[str, Any]]) -> None:
    path, record = selected
    validate_record_repository(record, repo)
    if record["status"] == "creating":
        selector = record_selector(record)
        raise StntError(
            f"creation of session {selector} is incomplete. "
            f"Safe retry: stnt --session {selector} recover-create"
        )
    if vscode_editor_command(required=True) is None:
        raise AssertionError("required VS Code prerequisites returned no command")
    start_session(record, path)


def _main(argv: Optional[Sequence[str]] = None) -> int:
    global VERBOSE
    args = parse_args(sys.argv[1:] if argv is None else argv)
    VERBOSE = args.verbose
    command = args.command or "start"
    if command in {"ssh-proxy", "ssh-known-hosts"}:
        try:
            ensure_state_layout()
            if command == "ssh-proxy":
                command_ssh_proxy(args.alias)
            else:
                command_ssh_known_hosts(args.alias)
            return 0
        except (StntError, OSError) as error:
            print(f"stnt: {error}", file=sys.stderr)
            return 1
    if command == "doctor":
        return command_doctor(json_output=args.json)
    if command == "setup":
        return command_setup()
    if command == "prune":
        try:
            ensure_state_layout()
            with lifecycle_gate(prune=True):
                command_prune(force=args.force)
            return 0
        except (StntError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
            print(f"stnt: {error}", file=sys.stderr)
            return 1
    if command == "show":
        try:
            ensure_state_layout()
            command_show()
            return 0
        except (StntError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
            print(f"stnt: {error}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("\nstnt: show cancelled", file=sys.stderr)
            return 130
    if command in {"init", "reconfigure"}:
        try:
            return command_init() if command == "init" else command_reconfigure()
        except (StntError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
            print(f"stnt: {error}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("stnt: configuration review interrupted; repository profile was not changed", file=sys.stderr)
            return 130
    if command == "config":
        try:
            return command_config_show()
        except (StntError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
            print(f"stnt: {error}", file=sys.stderr)
            return 1
    if command == "stack":
        try:
            if args.stack_command == "init":
                return command_stack_init(
                    args.name, args.frontend, args.backend, args.ingress_port,
                    args.frontend_command, args.backend_command,
                )
            ensure_state_layout()
            with lifecycle_gate():
                with stack_lock(args.name):
                    if args.stack_command == "start":
                        command_stack_start(args.name)
                    elif args.stack_command == "pause":
                        command_stack_pause(args.name)
                    elif args.stack_command == "finish":
                        command_stack_finish(args.name)
                    else:
                        command_stack_detach(args.name)
            return 0
        except (StntError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
            print(f"stnt: {error}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("stnt: interrupted; the stack and both private clones were retained", file=sys.stderr)
            return 130
    if command == "list":
        try:
            command_list()
            return 0
        except StntError as error:
            print(f"stnt: {error}", file=sys.stderr)
            return 1
    try:
        ensure_state_layout()
        repo = repository()
        with lifecycle_gate(), ExitStack() as session_stack:
            with ExitStack() as ownership_stack:
                ownership_stack.enter_context(repository_lock(repo))
                migrate_legacy_session(repo)
                sessions = load_sessions(repo)
                selected = None
                if command != "new" and not (command == "start" and not sessions and not args.session):
                    selected = select_session(repo, args.session, sessions)
                elif args.session:
                    raise StntError("--session cannot select a session for explicit new")
                if selected:
                    session_stack.enter_context(session_lock(record_lock_identity(selected[1])))
                    # Selection preceded the session lock. A concurrent finish may
                    # have removed the record before releasing that lock, so never
                    # dispatch the stale in-memory copy after ownership transfers.
                    reloaded = load_state(selected[0])
                    if reloaded is None:
                        raise StntError(
                            f"session {record_selector(selected[1])} changed while being selected; retry: stnt list"
                        )
                    _validate_record_path(selected[0], reloaded)
                    if (record_lock_identity(reloaded) != record_lock_identity(selected[1]) or
                            reloaded["repositoryPath"] != str(repo)):
                        raise StntError("selected session identity changed; retry: stnt list")
                    selected = (selected[0], reloaded)
                service_command = getattr(args, "service_command", None)
                service_url = getattr(args, "service_url", None)
                health_path = getattr(args, "health_path", "/")
                from_branch = getattr(args, "from_branch", None)
                if service_command is not None and not service_command.strip():
                    raise StntError("--service-command must contain a non-empty foreground service command")
                if service_url is not None:
                    parse_service_url(service_url)
                    if service_command is None:
                        raise StntError("--service-url requires --service-command")
                if not re.fullmatch(r"/\S*", health_path):
                    raise StntError("--health-path must be an absolute HTTP path beginning with /")
                if command in {"start", "new"} and not selected:
                # Create/setup while serialized, then lock the new session before
                # dropping the repository lock. There is no unlocked ownership gap.
                    ownership_stack.enter_context(creation_lock())
                    reviewed_profile = optional_profile(repo) if service_command is None and service_url is None else None
                    critical_preflight(
                        repo,
                        require_github=bool(
                            reviewed_profile and github_push_remote(reviewed_profile[1])
                        ),
                    )
                    created = create_record(
                        repo,
                        service_command=service_command,
                        service_url=service_url,
                        health_path=health_path,
                        from_branch=from_branch,
                        repository_profile=reviewed_profile[1] if reviewed_profile else None,
                    )
                    created_path = record_session_path(created)
                    try:
                        ensure_creation(created, created_path)
                    except BaseException as error:
                        selector = record_selector(created)
                        raise StntError(
                            f"creation interrupted after retaining session {selector}. "
                            f"Safe retry: stnt --session {selector} recover-create\nCause: {error}"
                        ) from error
                    selected = (created_path, created)
                    session_stack.enter_context(session_lock(record_lock_identity(created)))
                    service_command = None  # already bound durably to the new record
                    service_url = None
            # ownership_stack releases repository and creation before long dispatch;
            # session_stack remains held and releases on every exit path.
            if command in {"start", "new"}:
                command_start(
                    repo,
                    selected=selected,
                    service_command=service_command,
                    service_url=service_url,
                    health_path=health_path,
                )
            elif command == "recover-create":
                command_recover_create(repo, selected)
            elif command == "pause":
                command_pause(repo, selected)
            elif command == "finish":
                command_finish(repo, selected)
            elif command == "open":
                command_open(repo, selected)
            elif command == "editor":
                command_editor(repo, selected)
            else:
                command_detach(repo, selected)
        return 0
    except (StntError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
        print(f"stnt: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("stnt: interrupted; any known session was retained", file=sys.stderr)
        return 130


def main(argv: Optional[Sequence[str]] = None) -> int:
    global TIMING
    actual_argv = list(sys.argv[1:] if argv is None else argv)
    args = parse_args(actual_argv)
    command = args.command or "start"
    if args.timings is None:
        return _main(actual_argv)
    timing_path = Path(args.timings).expanduser()
    if not timing_path.parent.is_dir():
        print(f"stnt: timing output parent does not exist: {timing_path.parent}", file=sys.stderr)
        return 1
    TIMING = TimingRecorder(timing_path, command)
    try:
        result_code = _main(actual_argv)
        TIMING.write("success" if result_code == 0 else ("interrupted" if result_code == 130 else "failed"))
        return result_code
    except BaseException:
        TIMING.write("failed")
        raise
    finally:
        TIMING = None


if __name__ == "__main__":
    raise SystemExit(main())
