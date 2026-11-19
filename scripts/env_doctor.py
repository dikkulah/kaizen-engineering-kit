#!/usr/bin/env python3
"""
env_doctor.py - Developer Environment Connectivity, DNS & Zombie Process Diagnostic Doctor.

Diagnoses network connectivity to Kaizen internal and external infrastructure,
measures DNS resolution times, tests TCP socket reachability, flushes DNS caches,
and cleans orphaned background zombie processes.
"""

import argparse
import socket
import subprocess
import sys
import time
from typing import Optional


def check_endpoint_reachability(host: str, timeout_sec: float) -> dict[str, any]:
    """Test DNS resolution and TCP socket reachability for an endpoint."""
    result = {
        "host": host,
        "dns_resolved": False,
        "resolved_ip": None,
        "dns_time_ms": 0.0,
        "tcp_connected": False,
        "tcp_time_ms": 0.0,
        "port": 443,
        "error": None,
    }

    start_dns = time.perf_counter()
    try:
        ip = socket.gethostbyname(host)
        result["dns_time_ms"] = round((time.perf_counter() - start_dns) * 1000, 2)
        result["dns_resolved"] = True
        result["resolved_ip"] = ip
    except Exception as exc:
        result["error"] = f"DNS resolution failed: {exc}"
        return result

    # Test TCP connection to port 443 (or 53 for DNS resolvers like 1.1.1.1)
    port = 53 if host in ("1.1.1.1", "8.8.8.8") else 443
    result["port"] = port

    start_tcp = time.perf_counter()
    try:
        sock = socket.create_connection((ip, port), timeout=timeout_sec)
        result["tcp_time_ms"] = round((time.perf_counter() - start_tcp) * 1000, 2)
        result["tcp_connected"] = True
        sock.close()
    except Exception as exc:
        result["error"] = f"TCP port {port} unreachable: {exc}"

    return result


def flush_system_dns(dry_run: bool = False) -> tuple[bool, str]:
    """Flush operating system DNS resolver cache."""
    if dry_run:
        return True, "[DRY-RUN] Would execute 'ipconfig /flushdns'"
    try:
        proc = subprocess.run(
            ["ipconfig", "/flushdns"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            return True, proc.stdout.strip()
        return False, proc.stderr.strip()
    except Exception as exc:
        return False, str(exc)


def scan_and_clean_zombies(dry_run: bool = False) -> tuple[int, list[str]]:
    """Scan and terminate orphaned or runaway developer processes."""
    target_process_names = ["vmmemWSL.exe", "wslhost.exe", "chromedriver.exe"]
    found_zombies = []

    if sys.platform != "win32":
        return 0, found_zombies

    try:
        output = subprocess.check_output(["tasklist", "/FO", "CSV", "/NH"], text=True)
        for line in output.strip().splitlines():
            line_clean = line.strip().replace('"', "")
            parts = line_clean.split(",")
            if not parts or len(parts) < 2:
                continue
            proc_name = parts[0]
            pid = parts[1]
            if proc_name.lower() in [p.lower() for p in target_process_names]:
                found_zombies.append(f"{proc_name} (PID: {pid})")
    except Exception as exc:
        print(f"[WARNING] Could not list processes: {exc}", file=sys.stderr)
        return 0, []

    if dry_run:
        return len(found_zombies), found_zombies

    for entry in found_zombies:
        pid = entry.split("PID: ")[1].rstrip(")")
        subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, check=False)

    return len(found_zombies), found_zombies


def parse_arguments(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Diagnose network reachability, DNS health, and clean background development zombies.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--check-endpoints",
        type=str,
        default="gitlab.corp.kaizen.com,git.corp.kaizen.com,jenkins.corp.kaizen.com,1.1.1.1",
        help="Comma-separated list of hostnames or IP addresses to probe.",
    )
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=3000,
        help="Timeout in milliseconds for network socket operations.",
    )
    parser.add_argument(
        "--flush-dns",
        action="store_true",
        default=False,
        help="Flush Windows DNS resolver cache.",
    )
    parser.add_argument(
        "--clean-zombies",
        action="store_true",
        default=False,
        help="Scan and terminate orphaned development processes (vmmemWSL, wslhost, etc.).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Simulate diagnostic and cleanup steps without modifying system state.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    """Main execution entrypoint."""
    args = parse_arguments(argv)

    if args.timeout_ms <= 0:
        print(f"[ERROR] --timeout-ms must be a positive integer (> 0). Got: {args.timeout_ms}", file=sys.stderr)
        return 1

    endpoints = [ep.strip() for ep in args.check_endpoints.split(",") if ep.strip()]
    if not endpoints:
        print("[ERROR] --check-endpoints cannot be empty.", file=sys.stderr)
        return 1

    timeout_sec = args.timeout_ms / 1000.0

    if args.dry_run:
        print("[DRY-RUN] Environment Doctor Simulation Plan:")
        print(f"[DRY-RUN] 1. Diagnostic probing of {len(endpoints)} endpoint(s) with timeout {args.timeout_ms}ms:")
        for ep in endpoints:
            print(f"[DRY-RUN]    - Probe DNS & TCP for {ep}")
        if args.flush_dns:
            print("[DRY-RUN] 2. Windows DNS Cache Flush ('ipconfig /flushdns') simulation")
        if args.clean_zombies:
            print("[DRY-RUN] 3. Scan running processes for orphaned background processes simulation")
        print("[DRY-RUN] Simulation completed successfully.")
        return 0

    print(f"=== Environment Doctor Diagnostic (Timeout: {args.timeout_ms}ms) ===")
    has_failure = False

    for ep in endpoints:
        res = check_endpoint_reachability(ep, timeout_sec)
        if res["tcp_connected"]:
            print(f" [PASS] {ep} -> {res['resolved_ip']}:{res['port']} (DNS: {res['dns_time_ms']}ms, TCP: {res['tcp_time_ms']}ms)")
            continue

        has_failure = True
        err_msg = res["error"] or "Connection refused"
        print(f" [FAIL] {ep} -> {err_msg}", file=sys.stderr)

    if args.flush_dns:
        print("\n--- Flushing DNS Cache ---")
        ok, msg = flush_system_dns(dry_run=False)
        if ok:
            print(f"[SUCCESS] DNS cache flushed: {msg}")
        else:
            print(f"[ERROR] DNS flush failed: {msg}", file=sys.stderr)

    if args.clean_zombies:
        print("\n--- Scanning for Zombie Processes ---")
        count, zombies = scan_and_clean_zombies(dry_run=False)
        if count == 0:
            print("[INFO] No orphaned development processes found.")
        else:
            print(f"[SUCCESS] Cleaned {count} zombie process(es): {', '.join(zombies)}")

    if has_failure:
        print("\n[CONCLUSION] One or more connectivity checks failed. Check VPN connection.", file=sys.stderr)
        return 1

    print("\n[CONCLUSION] All environment diagnostics PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
