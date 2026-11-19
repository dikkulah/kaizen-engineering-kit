#!/usr/bin/env python3
"""EC2 Cloud-Brain: Zero-Rate-Limit S3 Workspace Sync, Zoekt Code Search & Code-RAG.

Provides deterministic file scanning, manifest tracking, delta tarball packaging,
S3 transport to `s3://kaizen-u2-dev-ncv-duckdb/kaizen-cloud-brain/workspace-seed/`,
and SSM-driven extraction into `/work-space` on EC2 (`i-0123456789abcdef0`).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .mini_pool import DEFAULT_INSTANCE_ID, DEFAULT_PROFILE, DEFAULT_REGION
from .paths import resolve_state_dir

_LOG = logging.getLogger("kaizen_kit.cloud_brain")

DEFAULT_S3_BUCKET = os.environ.get(
    "KAIZEN_S3_SEED_BUCKET",
    "s3://kaizen-u2-dev-ncv-duckdb/kaizen-cloud-brain/workspace-seed",
)
DEFAULT_WORKSPACE_ROOT = os.environ.get("WORKSPACE_ROOT", "C:/work-space")
DEFAULT_REMOTE_WORKSPACE = "/work-space"
DEFAULT_GATEWAY_URL = os.environ.get("KAIZEN_GATEWAY_URL", "http://127.0.0.1:8765")
DEFAULT_ZOEKT_URL = os.environ.get("KAIZEN_ZOEKT_URL", "http://127.0.0.1:6070")
DEFAULT_CODERAG_URL = os.environ.get("KAIZEN_CODERAG_URL", "http://127.0.0.1:18808")

MAX_FILE_SIZE_BYTES = 25 * 1024 * 1024  # 25 MB

INCLUDED_EXTENSIONS: Set[str] = {
    ".rs",
    ".java",
    ".py",
    ".ts",
    ".tsx",
    ".vue",
    ".js",
    ".go",
    ".c",
    ".h",
    ".avsc",
    ".proto",
    ".sql",
    ".md",
    ".yml",
    ".yaml",
    ".toml",
    ".json",
    ".xml",
    ".gradle",
}

EXCLUDED_DIR_NAMES: Set[str] = {
    "target",
    "node_modules",
    "dist",
    "build",
    ".scannerwork",
    "artifacts",
    ".triage-mirrors",
    ".pytest_cache",
    "__pycache__",
}


def _resolve_file_size(file_path: Path, file_size: Optional[int]) -> Optional[int]:
    if file_size is not None:
        return file_size
    try:
        return file_path.stat().st_size
    except OSError:
        return None


def _is_git_metadata_included(parts: Tuple[str, ...], name: str) -> bool:
    git_idx = parts.index(".git")
    rel_under_git = parts[git_idx + 1 :]
    # Keep essential git refs for branch/commit lineage; exclude config (may contain tokens) and packfiles/logs
    if len(rel_under_git) == 1 and name in ("HEAD", "packed-refs"):
        return True
    return len(rel_under_git) > 1 and rel_under_git[0] == "refs"


def _matches_code_extension(file_path: Path) -> bool:
    return file_path.suffix.lower() in INCLUDED_EXTENSIONS


def should_include_file(
    file_path: Path,
    rel_path: str,
    max_file_size: int = MAX_FILE_SIZE_BYTES,
    file_size: Optional[int] = None,
) -> bool:
    """Determine whether a file should be included in the Cloud-Brain workspace tarball."""
    size = _resolve_file_size(file_path, file_size)
    if size is None or size > max_file_size:
        return False

    name = file_path.name
    if name == "PROJECT-CATALOG.md" or name.startswith("Jenkinsfile"):
        return True

    parts = Path(rel_path).parts
    if ".git" in parts:
        return _is_git_metadata_included(parts, name)

    return _matches_code_extension(file_path)


def _should_prune_dir(name: str, in_git: bool) -> bool:
    """Return True if directory should be pruned during workspace traversal."""
    if name in EXCLUDED_DIR_NAMES or name.startswith(".venv"):
        return True
    if name == "objects" and in_git:
        return True
    return name.startswith(".git") and name != ".git"


def _try_manifest_entry(
    fpath: Path,
    rel_fposix: str,
    max_file_size: int,
) -> Optional[Dict[str, Any]]:
    """Build manifest entry with a single stat() call if file passes inclusion rules."""
    try:
        stat = fpath.stat()
    except OSError:
        return None
    if not should_include_file(
        fpath,
        rel_fposix,
        max_file_size=max_file_size,
        file_size=stat.st_size,
    ):
        return None
    return {
        "size": stat.st_size,
        "mtime": round(stat.st_mtime, 3),
    }


def _relative_posix(fpath: Path, root_resolved: Path) -> Optional[str]:
    try:
        return fpath.relative_to(root_resolved).as_posix()
    except ValueError:
        return None


def _collect_dir_entries(
    dir_p: Path,
    root_resolved: Path,
    filenames: List[str],
    max_file_size: int,
    manifest: Dict[str, Dict[str, Any]],
) -> None:
    for fname in filenames:
        fpath = dir_p / fname
        rel_fposix = _relative_posix(fpath, root_resolved)
        if rel_fposix is None:
            continue
        entry = _try_manifest_entry(fpath, rel_fposix, max_file_size)
        if entry is not None:
            manifest[rel_fposix] = entry


def scan_workspace(
    root: Path,
    max_file_size: int = MAX_FILE_SIZE_BYTES,
) -> Dict[str, Dict[str, Any]]:
    """Scan root workspace strictly read-only and return a normalized manifest."""
    root_resolved = Path(root).resolve()
    if not root_resolved.is_dir():
        return {}

    manifest: Dict[str, Dict[str, Any]] = {}

    for dirpath, dirnames, filenames in os.walk(root_resolved):
        dir_p = Path(dirpath)
        in_git = ".git" in dir_p.parts
        dirnames[:] = [d for d in dirnames if not _should_prune_dir(d, in_git)]
        _collect_dir_entries(dir_p, root_resolved, filenames, max_file_size, manifest)

    return dict(sorted(manifest.items(), key=lambda x: x[0]))


def get_manifest_path(custom_path: Optional[Path] = None) -> Path:
    """Return local path for the persistent workspace manifest."""
    if custom_path is not None:
        return Path(custom_path)
    return resolve_state_dir() / "cloud_brain_manifest.json"


def load_manifest(path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """Load local manifest from JSON file; returns empty dict if absent or corrupt."""
    p = get_manifest_path(path)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
        return {}
    except Exception as exc:
        _LOG.warning("Failed to load manifest from %s: %s", p, exc)
        return {}


def save_manifest(
    manifest: Dict[str, Dict[str, Any]],
    path: Optional[Path] = None,
) -> Path:
    """Save manifest to JSON file deterministically."""
    p = get_manifest_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return p


def _is_valid_number(val: Any) -> bool:
    return isinstance(val, (int, float)) and not isinstance(val, bool)


def _entry_changed(meta: Dict[str, Any], prev_meta: Any) -> bool:
    if not isinstance(prev_meta, dict):
        return True
    if meta.get("size") != prev_meta.get("size"):
        return True
    curr_mtime = meta.get("mtime", 0.0)
    prev_mtime = prev_meta.get("mtime", 0.0)
    if not (_is_valid_number(curr_mtime) and _is_valid_number(prev_mtime)):
        return True
    return abs(curr_mtime - prev_mtime) > 1e-3


def compute_delta(
    current: Dict[str, Dict[str, Any]],
    previous: Dict[str, Dict[str, Any]],
) -> List[str]:
    """Compute list of modified or added files relative to previous manifest."""
    delta: List[str] = []
    for rel_path, meta in current.items():
        if isinstance(meta, dict) and _entry_changed(meta, previous.get(rel_path)):
            delta.append(rel_path)

    return sorted(delta)


def _add_tar_entry(tar: tarfile.TarFile, root_p: Path, rel_str: str) -> bool:
    fpath = root_p / rel_str
    if not fpath.is_file():
        return False
    try:
        stat = fpath.stat()
        ti = tar.gettarinfo(str(fpath), arcname=rel_str)
        ti.uid = 1000
        ti.gid = 1000
        ti.uname = "ubuntu"
        ti.gname = "ubuntu"
        ti.mtime = int(stat.st_mtime)
        with open(fpath, "rb") as f:
            tar.addfile(ti, f)
        return True
    except OSError as exc:
        _LOG.warning("Could not add %s to tarball: %s", rel_str, exc)
        return False


def package_tarball(
    root: Path,
    rel_paths: Iterable[str],
    output_path: Path,
) -> int:
    """Package selected files into a deterministic gzip tarball."""
    root_p = Path(root).resolve()
    out_p = Path(output_path).resolve()
    out_p.parent.mkdir(parents=True, exist_ok=True)

    count = 0
    with tarfile.open(out_p, "w:gz", format=tarfile.GNU_FORMAT) as tar:
        for rel_str in sorted(rel_paths):
            if _add_tar_entry(tar, root_p, rel_str):
                count += 1

    return count


def upload_to_s3(
    file_path: Path,
    s3_bucket: str = DEFAULT_S3_BUCKET,
    profile: str = DEFAULT_PROFILE,
    region: str = DEFAULT_REGION,
) -> Tuple[bool, str]:
    """Upload a file to S3 bucket via aws s3 cp."""
    fpath = Path(file_path).resolve()
    if not fpath.is_file():
        return False, f"File not found: {fpath}"

    s3_dest = s3_bucket.rstrip("/") + "/" + fpath.name
    cmd = [
        "aws",
        "--profile",
        profile,
        "--region",
        region,
        "s3",
        "cp",
        str(fpath),
        s3_dest,
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            return False, res.stderr.strip() or f"aws s3 cp exited with {res.returncode}"
        return True, s3_dest
    except Exception as exc:
        return False, str(exc)


def _send_ssm_command(
    commands: List[str],
    instance_id: str,
    profile: str,
    region: str,
    ssm_timeout: int,
) -> Tuple[int, str, str]:
    payload = json.dumps({"commands": commands})
    cmd_send = [
        "aws",
        "--profile",
        profile,
        "--region",
        region,
        "ssm",
        "send-command",
        "--instance-ids",
        instance_id,
        "--document-name",
        "AWS-RunShellScript",
        "--timeout-seconds",
        str(ssm_timeout),
        "--parameters",
        payload,
        "--output",
        "json",
    ]
    try:
        proc = subprocess.run(cmd_send, capture_output=True, text=True, check=False, timeout=ssm_timeout)
        if proc.returncode != 0:
            return proc.returncode, "", proc.stderr.strip()
        data = json.loads(proc.stdout)
        command_id = data.get("Command", {}).get("CommandId", "")
        if not command_id:
            return 1, "", "Failed to retrieve CommandId from SSM send-command"
        return 0, command_id, ""
    except subprocess.TimeoutExpired:
        return 1, "", f"SSM send-command timed out after {ssm_timeout}s"
    except Exception as exc:
        return 1, "", str(exc)


def _fetch_ssm_invocation(cmd_inv: List[str], ssm_timeout: int) -> Tuple[Optional[int], str, str]:
    try:
        res_inv = subprocess.run(cmd_inv, capture_output=True, text=True, check=False, timeout=ssm_timeout)
        if res_inv.returncode != 0:
            return res_inv.returncode, "", res_inv.stderr.strip()
        inv_data = json.loads(res_inv.stdout)
        status = inv_data.get("Status", "Unknown")
        if status in ("Pending", "InProgress", "Delayed"):
            return None, "", ""
        return (
            0 if status == "Success" else 1,
            inv_data.get("StandardOutputContent", ""),
            inv_data.get("StandardErrorContent", ""),
        )
    except subprocess.TimeoutExpired:
        return 1, "", f"SSM get-command-invocation timed out after {ssm_timeout}s"
    except Exception as exc:
        return 1, "", str(exc)


def _poll_ssm_invocation(cmd_inv: List[str], ssm_timeout: int) -> Tuple[int, str, str]:
    deadline = time.monotonic() + ssm_timeout
    while True:
        code, stdout, stderr = _fetch_ssm_invocation(cmd_inv, ssm_timeout)
        if code is not None:
            return code, stdout, stderr
        if time.monotonic() >= deadline:
            return 1, "", f"SSM command still in progress after {ssm_timeout}s"
        time.sleep(5)


def run_ssm_commands(
    commands: List[str],
    instance_id: str = DEFAULT_INSTANCE_ID,
    profile: str = DEFAULT_PROFILE,
    region: str = DEFAULT_REGION,
    timeout_s: int = 300,
) -> Tuple[int, str, str]:
    """Execute commands on remote EC2 instance via AWS Systems Manager."""
    ssm_timeout = max(30, int(timeout_s))
    rc, command_id, err = _send_ssm_command(commands, instance_id, profile, region, ssm_timeout)
    if rc != 0:
        return rc, "", err

    cmd_wait = [
        "aws",
        "--profile",
        profile,
        "--region",
        region,
        "ssm",
        "wait",
        "command-executed",
        "--command-id",
        command_id,
        "--instance-id",
        instance_id,
    ]
    try:
        subprocess.run(cmd_wait, capture_output=True, text=True, check=False, timeout=ssm_timeout)
    except subprocess.TimeoutExpired:
        return 1, "", f"SSM command-executed wait timed out after {ssm_timeout}s"

    cmd_inv = [
        "aws",
        "--profile",
        profile,
        "--region",
        region,
        "ssm",
        "get-command-invocation",
        "--command-id",
        command_id,
        "--instance-id",
        instance_id,
        "--output",
        "json",
    ]
    return _poll_ssm_invocation(cmd_inv, ssm_timeout)


def extract_remote_tarball(
    tarball_name: str,
    s3_bucket: str = DEFAULT_S3_BUCKET,
    remote_workspace: str = DEFAULT_REMOTE_WORKSPACE,
    instance_id: str = DEFAULT_INSTANCE_ID,
    profile: str = DEFAULT_PROFILE,
    region: str = DEFAULT_REGION,
    timeout_s: int = 600,
) -> Tuple[bool, str]:
    """Extract uploaded workspace tarball into remote /work-space and trigger indexing."""
    s3_src = s3_bucket.rstrip("/") + "/" + tarball_name
    remote_tmp = f"/tmp/{tarball_name}"
    q_ws = shlex.quote(remote_workspace)
    q_s3 = shlex.quote(s3_src)
    q_tmp = shlex.quote(remote_tmp)
    q_region = shlex.quote(region)
    q_zoekt_idx = shlex.quote("/var/lib/zoekt/index")
    cmds = [
        "set -e",
        f"sudo mkdir -p {q_ws} {q_zoekt_idx} && sudo chown -R ubuntu:ubuntu {q_ws} {q_zoekt_idx}",
        f"aws s3 cp {q_s3} {q_tmp} --region {q_region}",
        f"tar -xzf {q_tmp} -C {q_ws}",
        f"rm -f {q_tmp}",
        f"sudo chown -R ubuntu:ubuntu {q_ws} {q_zoekt_idx}",
        f"sudo -u ubuntu /usr/local/bin/zoekt-index -index {q_zoekt_idx} {q_ws}",
        "curl -fsS -X POST http://127.0.0.1:18808/v1/rag/reindex",
    ]
    code, out, err = run_ssm_commands(
        cmds,
        instance_id=instance_id,
        profile=profile,
        region=region,
        timeout_s=timeout_s,
    )
    if code != 0:
        return False, f"SSM extraction error (code {code}): {err or out}"
    return True, out


def _select_sync_plan(
    full: bool,
    curr_manifest: Dict[str, Dict[str, Any]],
    prev_manifest: Dict[str, Dict[str, Any]],
) -> Tuple[str, List[str], str]:
    if full or not prev_manifest:
        return "full", list(curr_manifest.keys()), "seed.tar.gz"
    return "incremental", compute_delta(curr_manifest, prev_manifest), "delta.tar.gz"


def _package_and_upload_tarball(
    root_path: Path,
    files_to_sync: List[str],
    tarball_filename: str,
    s3_bucket: str,
    profile: str,
    region: str,
) -> Tuple[bool, int, int, str]:
    with tempfile.TemporaryDirectory(prefix="cloud_brain_") as tmp_dir:
        tmp_tar_path = Path(tmp_dir) / tarball_filename
        packed_count = package_tarball(root_path, files_to_sync, tmp_tar_path)
        tar_size = tmp_tar_path.stat().st_size
        upload_ok, upload_msg = upload_to_s3(
            tmp_tar_path,
            s3_bucket=s3_bucket,
            profile=profile,
            region=region,
        )
        return upload_ok, packed_count, tar_size, upload_msg


def sync_workspace(
    root: Path = Path(DEFAULT_WORKSPACE_ROOT),
    full: bool = False,
    s3_bucket: str = DEFAULT_S3_BUCKET,
    instance_id: str = DEFAULT_INSTANCE_ID,
    profile: str = DEFAULT_PROFILE,
    region: str = DEFAULT_REGION,
    dry_run: bool = False,
    skip_ssm: bool = False,
    manifest_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Perform full or incremental zero-rate-limit workspace sync to S3 & EC2."""
    root_path = Path(root).resolve()
    if not root_path.is_dir():
        return {"status": "error", "message": f"Workspace root does not exist: {root_path}"}

    prev_manifest = load_manifest(manifest_path)
    curr_manifest = scan_workspace(root_path)
    mode, files_to_sync, tarball_filename = _select_sync_plan(full, curr_manifest, prev_manifest)

    if not files_to_sync:
        return {
            "status": "skipped",
            "mode": mode,
            "message": "Workspace is already in sync; 0 modified or new files.",
            "total_files": len(curr_manifest),
            "synced_files": 0,
        }

    if dry_run:
        return {
            "status": "dry_run",
            "mode": mode,
            "tarball": tarball_filename,
            "total_files": len(curr_manifest),
            "synced_files": len(files_to_sync),
            "sample_files": files_to_sync[:10],
        }

    upload_ok, packed_count, tar_size, upload_msg = _package_and_upload_tarball(
        root_path, files_to_sync, tarball_filename, s3_bucket, profile, region
    )
    if not upload_ok:
        return {"status": "error", "message": f"S3 upload failed: {upload_msg}", "mode": mode}

    ssm_output = ""
    if not skip_ssm:
        extract_ok, extract_msg = extract_remote_tarball(
            tarball_filename,
            s3_bucket=s3_bucket,
            remote_workspace=DEFAULT_REMOTE_WORKSPACE,
            instance_id=instance_id,
            profile=profile,
            region=region,
        )
        if not extract_ok:
            return {
                "status": "partial_success",
                "message": f"Uploaded to S3, but SSM extraction failed: {extract_msg}",
                "mode": mode,
                "tarball": tarball_filename,
                "synced_files": packed_count,
            }
        ssm_output = extract_msg

    # Save and upload manifest only after SSM extraction succeeds (or skip_ssm=True)
    saved_mpath = save_manifest(curr_manifest, manifest_path)
    manifest_ok, manifest_msg = upload_to_s3(
        saved_mpath,
        s3_bucket=s3_bucket,
        profile=profile,
        region=region,
    )
    if not manifest_ok:
        return {
            "status": "partial_success",
            "message": f"Extracted on EC2, but manifest S3 upload failed: {manifest_msg}",
            "mode": mode,
            "tarball": tarball_filename,
            "synced_files": packed_count,
        }

    return {
        "status": "success",
        "mode": mode,
        "tarball": tarball_filename,
        "tarball_bytes": tar_size,
        "total_files": len(curr_manifest),
        "synced_files": packed_count,
        "s3_destination": s3_bucket.rstrip("/") + "/" + tarball_filename,
        "ssm_output": ssm_output[:500] if ssm_output else "skipped",
    }


def query_gateway_status(
    gateway_url: str = DEFAULT_GATEWAY_URL,
    timeout_s: float = 3.0,
) -> Dict[str, Any]:
    """Query Kaizen JEV Gateway (:8765) health and pool status."""
    url = gateway_url.rstrip("/") + "/v1/pool/status"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return {"status": "online", "pool_status": data}
    except Exception as exc:
        return {
            "status": "offline",
            "gateway_url": gateway_url,
            "error": str(exc),
        }


def query_zoekt_search(
    query: str,
    limit: int = 20,
    gateway_url: str = DEFAULT_GATEWAY_URL,
    zoekt_url: str = DEFAULT_ZOEKT_URL,
    timeout_s: float = 5.0,
) -> Dict[str, Any]:
    """Query Zoekt regex/trigram code search via Gateway (:8765) or direct port (:6070)."""
    encoded_q = urllib.parse.quote(query)
    last_error = ""

    # 1. Attempt via Gateway proxy (GET)
    gw_url = f"{gateway_url.rstrip('/')}/v1/zoekt/search?q={encoded_q}&num={limit}"
    try:
        req = urllib.request.Request(gw_url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return {"status": "ok", "source": gw_url, "results": data}
    except Exception as exc:
        last_error = str(exc)

    # 2. Attempt direct Zoekt RPC (/api/search POST)
    direct_url = f"{zoekt_url.rstrip('/')}/api/search"
    try:
        post_data = json.dumps({"Q": query, "Opts": {"MaxDocDisplayCount": limit}}).encode("utf-8")
        req = urllib.request.Request(
            direct_url,
            data=post_data,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return {"status": "ok", "source": direct_url, "results": data}
    except Exception as exc:
        last_error = str(exc)

    return {
        "status": "error",
        "message": f"Zoekt search failed across all endpoints: {last_error}",
        "query": query,
    }


def query_code_rag(
    query: str,
    limit: int = 10,
    gateway_url: str = DEFAULT_GATEWAY_URL,
    coderag_url: str = DEFAULT_CODERAG_URL,
    timeout_s: float = 10.0,
    *,
    hybrid: bool = True,
    rerank: bool = True,
    rrf_k: int = 60,
    local_fallback: bool = False,
) -> Dict[str, Any]:
    """Query Code-RAG service (Tree-sitter + LanceDB + BGE-M3/Qwen3-Reranker RRF) via Gateway or direct port."""
    req_body: Dict[str, Any] = {"query": query, "limit": limit}
    if not hybrid:
        req_body["hybrid"] = False
    if not rerank:
        req_body["rerank"] = False
    if rrf_k != 60:
        req_body["rrf_k"] = rrf_k
    payload = json.dumps(req_body).encode("utf-8")
    urls_to_try = [
        f"{gateway_url.rstrip('/')}/v1/rag/query",
        f"{coderag_url.rstrip('/')}/v1/rag/query",
    ]

    last_error = ""
    for url in urls_to_try:
        try:
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return {"status": "ok", "source": url, "results": data}
        except Exception as exc:
            last_error = str(exc)

    if local_fallback:
        from .gates.symbol_rag_indexer import search_concepts

        hits = search_concepts(query, limit=limit)
        return {"status": "ok", "source": "local:symbol_rag_indexer", "results": {"query": query, "results": hits}}

    return {
        "status": "error",
        "message": f"Code-RAG query failed across all endpoints: {last_error}",
        "query": query,
    }


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kaizen-kit cloud-brain",
        description="EC2 Cloud-Brain: S3 Workspace Sync, Zoekt Code Search & Code-RAG",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sync = sub.add_parser("sync", help="Sync workspace files to S3 and extract on EC2")
    p_sync.add_argument("--full", action="store_true", help="Force full seed sync instead of delta")
    p_sync.add_argument("--root", default=DEFAULT_WORKSPACE_ROOT, help="Local workspace root path")
    p_sync.add_argument("--bucket", default=DEFAULT_S3_BUCKET, help="S3 bucket destination")
    p_sync.add_argument("--instance-id", default=DEFAULT_INSTANCE_ID, help="Target EC2 instance ID")
    p_sync.add_argument("--profile", default=DEFAULT_PROFILE, help="AWS CLI profile")
    p_sync.add_argument("--region", default=DEFAULT_REGION, help="AWS region")
    p_sync.add_argument("--dry-run", action="store_true", help="Scan and diff without upload")
    p_sync.add_argument("--no-ssm", action="store_true", help="Upload to S3 only, skip remote extraction")

    p_status = sub.add_parser("status", help="Check Cloud-Brain services & gateway status")
    p_status.add_argument("--gateway-url", default=DEFAULT_GATEWAY_URL, help="Kaizen Gateway URL")

    p_search = sub.add_parser("search", help="Search workspace symbols and text via Zoekt")
    p_search.add_argument("query", help="Regex or trigram search term")
    p_search.add_argument("--limit", type=int, default=20, help="Max results")
    p_search.add_argument("--gateway-url", default=DEFAULT_GATEWAY_URL, help="Kaizen Gateway URL")
    p_search.add_argument("--zoekt-url", default=DEFAULT_ZOEKT_URL, help="Direct Zoekt URL")

    p_rag = sub.add_parser("rag", help="Hybrid semantic + AST code search via LanceDB")
    p_rag.add_argument("query", help="Natural language or semantic code query")
    p_rag.add_argument("--limit", type=int, default=10, help="Max results")
    p_rag.add_argument("--gateway-url", default=DEFAULT_GATEWAY_URL, help="Kaizen Gateway URL")
    p_rag.add_argument("--coderag-url", default=DEFAULT_CODERAG_URL, help="Direct Code-RAG URL")
    return parser


def _dispatch_cli(args: argparse.Namespace) -> Tuple[Dict[str, Any], Set[str]]:
    if args.cmd == "sync":
        res = sync_workspace(
            root=Path(args.root),
            full=args.full,
            s3_bucket=args.bucket,
            instance_id=args.instance_id,
            profile=args.profile,
            region=args.region,
            dry_run=args.dry_run,
            skip_ssm=args.no_ssm,
        )
        return res, {"success", "skipped", "dry_run"}
    if args.cmd == "status":
        return query_gateway_status(gateway_url=args.gateway_url), {"online"}
    if args.cmd == "search":
        res = query_zoekt_search(
            query=args.query,
            limit=args.limit,
            gateway_url=args.gateway_url,
            zoekt_url=args.zoekt_url,
        )
        return res, {"ok"}
    res = query_code_rag(
        query=args.query,
        limit=args.limit,
        gateway_url=args.gateway_url,
        coderag_url=args.coderag_url,
    )
    return res, {"ok"}


def main(argv: Optional[List[str]] = None) -> int:
    """CLI dispatcher for `kaizen-kit cloud-brain`."""
    args = _build_cli_parser().parse_args(argv)
    res, ok_statuses = _dispatch_cli(args)
    print(json.dumps(res, indent=2))
    return 0 if res.get("status") in ok_statuses else 1


if __name__ == "__main__":
    sys.exit(main())
