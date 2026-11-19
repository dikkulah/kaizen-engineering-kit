"""Structural smell and boundary extractor powered by ast-grep and Opengrep."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from kaizen_kit.paths import resolve_assets_dir
from kaizen_kit.radar.capsule_schema import BoundaryItem, SmellItem

_SQL_TABLE_RE = re.compile(
    r"\b(?:FROM|INTO|UPDATE|JOIN|DELETE\s+FROM)\s+[`\"\[]?([a-zA-Z_][a-zA-Z0-9_\.]*)",
    re.IGNORECASE,
)


def find_binary(names: list[str]) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    local_bin = Path.home() / ".local" / "bin"
    exts = (".exe", ".cmd", "") if os.name == "nt" else ("",)
    for name in names:
        for ext in exts:
            cand = local_bin / f"{name}{ext}"
            if cand.is_file():
                return str(cand)
    return None


def default_rules_dir() -> Path:
    return resolve_assets_dir() / "rules" / "refactor_radar"


def normalize_rel_path(raw: str, repo_root: Path) -> str:
    cleaned = raw.replace("\\", "/")
    root_norm = repo_root.resolve().as_posix().rstrip("/") + "/"
    if cleaned.lower().startswith(root_norm.lower()):
        cleaned = cleaned[len(root_norm) :]
    return cleaned.lstrip("./")


_QUOTED_RE = re.compile(r'"([^"]+)"')


def _extract_target_text(match: dict[str, Any]) -> str:
    mvars = match.get("metaVariables") or {}
    single = mvars.get("single") or {}
    target = single.get("TARGET") or {}
    if target.get("text"):
        return str(target["text"]).strip().strip("\"'`")
    raw_text = str(match.get("text") or "").strip()
    m = _QUOTED_RE.search(raw_text)
    if m:
        return m.group(1).strip()
    return raw_text.strip("\"'`")


def _parse_boundary_rule_id(rule_id: str) -> tuple[str, str]:
    parts = rule_id.lower().split("-")
    direction = "OUTBOUND"
    protocol = "HTTP"
    if len(parts) >= 3:
        if parts[1] == "inbound":
            direction = "INBOUND"
        elif parts[1] == "outbound":
            direction = "OUTBOUND"
        proto_map = {"http": "HTTP", "grpc": "GRPC", "kafka": "KAFKA", "sql": "SQL"}
        protocol = proto_map.get(parts[2], "HTTP")
    return direction, protocol


def _normalize_boundary_identifier(protocol: str, raw_target: str) -> str:
    if protocol == "SQL":
        m = _SQL_TABLE_RE.search(raw_target)
        if m:
            return f"table:{m.group(1).lower()}"
        clean = re.sub(r"\s+", " ", raw_target).strip()
        if clean and len(clean) <= 64 and " " not in clean:
            return f"table:{clean.lower()}"
        return clean[:120]
    if protocol == "HTTP":
        clean = re.sub(r"\s+", " ", raw_target).strip()
        return clean[:160]
    return raw_target.strip()[:160]


def run_ast_grep_scan(repo: Path, sgconfig: Path) -> list[dict[str, Any]]:
    sg_bin = find_binary(["ast-grep", "sg"])
    if not sg_bin or not sgconfig.is_file():
        return []
    proc = subprocess.run(
        [sg_bin, "scan", "--config", str(sgconfig), "--json", str(repo)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    raw = (proc.stdout or "").strip()
    if proc.returncode in (0, 1) and raw.startswith("["):
        try:
            data = json.loads(raw)
            return data if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []
    return []


def run_opengrep_scan(repo: Path, rule_file: Path) -> list[SmellItem]:
    og_bin = find_binary(["opengrep"])
    if not og_bin or not rule_file.is_file():
        return []
    proc = subprocess.run(
        [og_bin, "scan", "--config", str(rule_file), "--json", "--quiet", str(repo)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    raw = (proc.stdout or "").strip()
    if not raw.startswith("{"):
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    out: list[SmellItem] = []
    for res in payload.get("results", []):
        extra = res.get("extra") or {}
        start = res.get("start") or {}
        out.append(
            SmellItem(
                rule_id=str(res.get("check_id", "opengrep")).split(".")[-1],
                severity=str(extra.get("severity", "WARNING")).lower(),
                message=str(extra.get("message", "")),
                file_path=normalize_rel_path(str(res.get("path", "")), repo),
                line=int(start.get("line", 1)),
                snippet=str(extra.get("lines", ""))[:240],
                engine="opengrep",
            )
        )
    return out


def scan_smells(repo: Path, rules_dir: Path | None = None) -> list[SmellItem]:
    rdir = rules_dir or default_rules_dir()
    smells_dir = rdir / "smells"
    items: list[SmellItem] = []
    for m in run_ast_grep_scan(repo, smells_dir / "sgconfig.yml"):
        rng = m.get("range") or {}
        start = rng.get("start") or {}
        items.append(
            SmellItem(
                rule_id=str(m.get("ruleId", "ast-grep")),
                severity=str(m.get("severity", "warning")).lower(),
                message=str(m.get("message", "")),
                file_path=normalize_rel_path(str(m.get("file", "")), repo),
                line=int(start.get("line", 0)) + 1,
                snippet=str(m.get("lines", m.get("text", "")))[:240],
                engine="ast-grep",
            )
        )
    items.extend(run_opengrep_scan(repo, smells_dir / "opengrep_dataflow.yml"))
    return items


def extract_boundaries(
    repo: Path,
    service_name: str,
    rules_dir: Path | None = None,
) -> list[BoundaryItem]:
    rdir = rules_dir or default_rules_dir()
    bdir = rdir / "boundaries"
    out: list[BoundaryItem] = []
    seen: set[tuple[str, str, str, int]] = set()
    for m in run_ast_grep_scan(repo, bdir / "sgconfig.yml"):
        rule_id = str(m.get("ruleId", "unknown"))
        direction, protocol = _parse_boundary_rule_id(rule_id)
        raw_target = _extract_target_text(m)
        identifier = _normalize_boundary_identifier(protocol, raw_target)
        if not identifier:
            continue
        rng = m.get("range") or {}
        start = rng.get("start") or {}
        line = int(start.get("line", 0)) + 1
        fp = normalize_rel_path(str(m.get("file", "")), repo)
        dedup_key = (direction, protocol, identifier, line)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        out.append(
            BoundaryItem(
                service_name=service_name,
                direction=direction,  # type: ignore[arg-type]
                protocol=protocol,  # type: ignore[arg-type]
                identifier=identifier,
                rule_id=rule_id,
                file_path=fp,
                line=line,
            )
        )
    return out
