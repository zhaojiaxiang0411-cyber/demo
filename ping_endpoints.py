#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Read endpoint.txt from the current working directory and ping each IP concurrently.

endpoint.txt may be plain one-IP-per-line text, or pasted output of
    fabric <node> show endpoint
In the latter case, IPv4 addresses are taken from the 3rd whitespace-separated
column. Rows whose first column starts with "overlay" are skipped (overlay IPs).
MAC addresses in column 3 are ignored.

Cross-platform: Windows, macOS, Linux (iputils), and common BSDs. Other Unix-like
systems fall back to `ping -c 1` with subprocess-level timeout.

Run from the folder that contains endpoint.txt:
    python3 ping_endpoints.py

Requires Python 3.6+ (validated for 3.6.8 on RHEL/CentOS-style hosts; also works on newer 3.x).
"""

import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

# IPv4 only (column 3 in show endpoint is either MAC or IPv4)
_IPV4_RE = re.compile(
    r"^(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}"
    r"(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)$"
)


def _ping_command(ip: str, timeout_ms: int) -> List[str]:
    """
    Build OS-specific ping argv.

    - Windows: -n count, -w timeout in milliseconds
    - macOS / FreeBSD: -c count, -W timeout in milliseconds
    - Linux (iputils): -c count, -W timeout in seconds
    - Unknown: -c 1 only; rely on subprocess.run(timeout=...)
    """
    sec = max(1, (timeout_ms + 999) // 1000)
    plat = sys.platform

    if plat == "win32":
        return ["ping", "-n", "1", "-w", str(timeout_ms), ip]

    if plat == "darwin":
        return ["ping", "-c", "1", "-W", str(timeout_ms), ip]

    if plat.startswith("linux"):
        return ["ping", "-c", "1", "-W", str(sec), ip]

    # FreeBSD: -W is wait time in milliseconds (same unit as macOS)
    if plat.startswith("freebsd"):
        return ["ping", "-c", "1", "-W", str(timeout_ms), ip]

    # OpenBSD / NetBSD: prefer second-based -w where available; else plain ping
    if plat.startswith(("openbsd", "netbsd")):
        return ["ping", "-c", "1", "-w", str(sec), ip]

    # Generic Unix: no portable timeout flag; subprocess enforces deadline
    return ["ping", "-c", "1", ip]


def ping_one(ip: str, timeout_ms: int = 3000) -> Tuple[str, bool, str]:
    """Ping one host; returns (ip, success, short detail)."""
    ip = ip.strip()
    if not ip or ip.startswith("#"):
        return ip, False, "skipped"

    cmd = _ping_command(ip, timeout_ms)
    run_kw = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "universal_newlines": True,
        "timeout": timeout_ms / 1000.0 + 3.0,
    }  # type: Dict[str, Any]
    if sys.platform == "win32":
        # CREATE_NO_WINDOW exists on Python 3.7+ (Windows); older 3.6 has no flag
        cf = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if cf:
            run_kw["creationflags"] = cf

    try:
        proc = subprocess.run(cmd, **run_kw)
        ok = proc.returncode == 0
        tail = (proc.stdout or proc.stderr or "").strip().splitlines()
        detail = tail[-1] if tail else ""
        return ip, ok, detail[:300]
    except subprocess.TimeoutExpired:
        return ip, False, "timeout"
    except OSError as e:
        return ip, False, str(e)


def _is_ipv4(token: str) -> bool:
    return bool(_IPV4_RE.match(token.strip()))


def _is_noise_line(line: str) -> bool:
    """Skip banners, table borders, legend, and CLI echo lines."""
    s = line.strip()
    if not s:
        return True
    low = s.lower()
    if low.startswith("legend"):
        return True
    if "show endpoint" in low:
        return True
    if low.startswith("node ") and "fabric" in low:
        return True
    # Table borders: +----+----+, -----+-----, etc.
    if re.match(r"^[+\-|=\s.]+$", s) and len(s) >= 4:
        return True
    return False


def extract_ips_from_show_endpoint(raw: str) -> List[str]:
    """
    Parse 'fabric ... show endpoint' style text: use 3rd column when it is IPv4.
    Skip rows whose 1st column starts with 'overlay' (case-insensitive).
    Order preserved; duplicates removed (first occurrence wins).
    """
    out = []  # type: List[str]
    seen = set()  # type: Set[str]
    # Typical header first cells to ignore
    skip_first = frozenset(
        ("vlan", "bridge-domain", "bd", "domain", "encap", "mac", "ip", "intf", "interface")
    )

    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if _is_noise_line(s):
            continue

        parts = re.split(r"\s+", s)
        if len(parts) < 3:
            continue

        col0 = parts[0].strip()
        col0_lower = col0.lower()
        if col0_lower.startswith("overlay"):
            continue
        if col0_lower in skip_first or col0.startswith("-"):
            continue

        col2 = parts[2].strip()
        if not _is_ipv4(col2):
            continue

        if col2 not in seen:
            seen.add(col2)
            out.append(col2)

    return out


def load_hosts(file_path: Path) -> List[str]:
    raw = file_path.read_text(encoding="utf-8", errors="replace")
    parsed = extract_ips_from_show_endpoint(raw)
    if parsed:
        return parsed

    # Fallback: one IPv4 per non-comment line (legacy behavior)
    hosts = []  # type: List[str]
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if _is_ipv4(s):
            hosts.append(s)
    return hosts


def main() -> None:
    if sys.version_info < (3, 6):
        print("Requires Python 3.6 or newer (you have %d.%d)." % sys.version_info[:2], file=sys.stderr)
        sys.exit(1)

    endpoint_file = Path.cwd() / "endpoint.txt"
    if not endpoint_file.is_file():
        print(f"Missing file: {endpoint_file}", file=sys.stderr)
        print("Place endpoint.txt in the current working directory and retry.", file=sys.stderr)
        sys.exit(1)

    hosts = load_hosts(endpoint_file)
    if not hosts:
        print(
            "No IPv4 addresses to ping: none extracted from show endpoint output "
            "(or legacy one-IP-per-line file is empty / comments only)."
        )
        sys.exit(0)

    timeout_ms = 3000
    workers = min(32, len(hosts))

    print(
        f"Pinging {len(hosts)} host(s) on {sys.platform} "
        f"with up to {workers} workers (timeout {timeout_ms} ms)..."
    )
    print("-" * 60)

    results = []  # type: List[Tuple[str, bool, str]]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(ping_one, h, timeout_ms): h for h in hosts}
        for fut in as_completed(futures):
            results.append(fut.result())

    # Stable output order: same as file order
    order = {h: i for i, h in enumerate(hosts)}
    results.sort(key=lambda x: order.get(x[0], 9999))

    ok_n = 0
    for ip, ok, detail in results:
        if ok:
            ok_n += 1
        status = "OK" if ok else "FAIL"
        extra = f"  |  {detail}" if detail and detail != "skipped" else ""
        print(f"{status}\t{ip}{extra}")

    print("-" * 60)
    print(f"Done: {ok_n}/{len(hosts)} reachable.")


if __name__ == "__main__":
    main()
