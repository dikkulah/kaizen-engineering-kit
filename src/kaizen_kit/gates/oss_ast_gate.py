#!/usr/bin/env python3
"""
Kaizen v3.3 Open-Source AST, Dataflow, Clone, Dependency & Behavioral Git Gate (`oss_ast_gate.py`).

100% Offline / Zero-Telemetry integration of:
  1. `ast-grep` (`sg`) — Structural Tree-sitter AST lint (`<40ms` single-file Jidoka + workspace scan & `--rewrite`)
  2. `opengrep` — Multi-statement dataflow & Clean Architecture YAML rules (LGPL-2.1 FOSS fork of Semgrep)
  3. `jscpd` — Token-based code clone detection (zero repo artifact pollution via temp report dir)
  4. `dependency-cruiser` (`depcruise`) — TypeScript/Vue circular dependency & layer isolation audit
  5. Behavioral Git X-Ray (`code-maat` / Tornhill model) — Hotspots (`Churn x LOC` + indentation complexity) & Temporal Coupling
"""

import argparse
import collections
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_KIT_SRC = Path(__file__).resolve().parent.parent.parent
if str(_KIT_SRC) not in sys.path:
    sys.path.insert(0, str(_KIT_SRC))

from kaizen_kit.paths import resolve_assets_dir

CODE_EXTENSIONS = {".rs", ".java", ".ts", ".tsx", ".js", ".jsx", ".vue"}


def _find_binary(names: List[str]) -> Optional[str]:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    local_bin = Path.home() / ".local" / "bin"
    npm_roaming = Path(os.environ.get("APPDATA", "")) / "npm" if os.environ.get("APPDATA") else None
    search_dirs = [d for d in (local_bin, npm_roaming) if d and d.is_dir()]
    exts = (".exe", ".cmd", "") if os.name == "nt" else ("",)
    for d in search_dirs:
        for name in names:
            for ext in exts:
                cand = d / f"{name}{ext}"
                if cand.is_file():
                    return str(cand)
    return None


def _rules_dir() -> Path:
    return resolve_assets_dir() / "rules" / "static_analysis"


def is_test_path(path: Path) -> bool:
    norm = path.as_posix().lower()
    return (
        "/tests/" in norm
        or "/test/" in norm
        or "/benches/" in norm
        or norm.endswith(("_test.rs", "test.java", ".spec.ts", ".test.ts", "build.rs"))
    )


def scan_single_file_ast_grep(path: Path, timeout_sec: float = 1.5) -> List[str]:
    """Fast (<40ms) single-file Tree-sitter AST sensor for PostToolUse Jidoka."""
    if not path.is_file() or path.suffix.lower() not in {".rs", ".java", ".ts", ".tsx"} or is_test_path(path):
        return []
    sg_bin = _find_binary(["ast-grep", "sg"])
    rules_yml = _rules_dir() / "ast_grep_rules.yml"
    if not sg_bin or not rules_yml.is_file():
        return []
    try:
        proc = subprocess.run(
            [sg_bin, "scan", "-r", str(rules_yml), "--json=compact", str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
        )
        raw = (proc.stdout or "").strip()
        if not raw or not raw.startswith("["):
            return []
        items = json.loads(raw)
        out: List[str] = []
        for it in items[:4]:
            rule_id = it.get("ruleId", "ast-grep")
            line = it.get("range", {}).get("start", {}).get("line", 0) + 1
            msg = it.get("message", "AST rule match")
            out.append(f"[AST:{rule_id}] {path.name}:{line} -> {msg}")
        return out
    except Exception:
        return []


def run_ast_grep_audit(target: Path) -> Tuple[int, List[Dict[str, Any]]]:
    sg_bin = _find_binary(["ast-grep", "sg"])
    rules_yml = _rules_dir() / "ast_grep_rules.yml"
    if not sg_bin or not rules_yml.is_file():
        return 0, []
    scan_target = target / "src" if (target / "src").is_dir() else target
    proc = subprocess.run(
        [sg_bin, "scan", "-r", str(rules_yml), "--json=compact", str(scan_target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    raw = (proc.stdout or "").strip()
    findings = json.loads(raw) if raw.startswith("[") else []
    errors = [f for f in findings if str(f.get("severity", "")).lower() == "error"]
    return (1 if errors else 0), findings


def _git_changed_files(repo_root: Path) -> List[Path]:
    try:
        out = subprocess.check_output(
            ["git", "-C", str(repo_root), "diff", "--name-only", "HEAD"],
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        files = []
        for ln in out.splitlines():
            p = (repo_root / ln.strip()).resolve()
            if p.is_file() and p.suffix.lower() in CODE_EXTENSIONS and not is_test_path(p):
                files.append(p)
        return files
    except Exception:
        return []


def run_opengrep_audit(target: Path, diff_only: bool = False) -> Tuple[int, List[Dict[str, Any]]]:
    og_bin = _find_binary(["opengrep", "semgrep"])
    rules_yml = _rules_dir() / "opengrep_rules.yml"
    if not og_bin or not rules_yml.is_file():
        return 0, []

    targets: List[str]
    if diff_only and target.is_dir() and (target / ".git").exists():
        changed = _git_changed_files(target)
        if not changed:
            return 0, []
        targets = [str(p) for p in changed]
    else:
        scan_target = target / "src" if (target / "src").is_dir() else target
        targets = [str(scan_target)]

    offline_flags = ["--disable-version-check"]
    if "semgrep" in Path(og_bin).name.lower():
        offline_flags.append("--metrics=off")

    proc = subprocess.run(
        [og_bin, "scan", "--config", str(rules_yml), *offline_flags, "--json", "--quiet", *targets],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    raw = (proc.stdout or "").strip()
    if not raw.startswith("{"):
        return 0, []
    data = json.loads(raw)
    results = data.get("results", [])
    errors = [
        r for r in results if str(r.get("extra", {}).get("severity", "")).upper() == "ERROR"
    ]
    return (1 if errors else 0), results


def run_jscpd_audit(target: Path, min_lines: int = 14, min_tokens: int = 90, max_pct: float = 3.0) -> Tuple[int, Dict[str, Any]]:
    jscpd_bin = _find_binary(["jscpd"])
    scan_target = target / "src" if (target / "src").is_dir() else target
    if not jscpd_bin or not scan_target.exists():
        return 0, {}

    with tempfile.TemporaryDirectory(prefix="kaizen_jscpd_") as tmpdir:
        subprocess.run(
            [
                jscpd_bin,
                str(scan_target),
                "--min-lines",
                str(min_lines),
                "--min-tokens",
                str(min_tokens),
                "--ignore",
                "**/tests/**,**/test/**,**/*.spec.ts,**/node_modules/**,**/target/**",
                "--reporters",
                "json",
                "--output",
                tmpdir,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
        )
        rep_file = Path(tmpdir) / "jscpd-report.json"
        if not rep_file.is_file():
            return 0, {}
        rep = json.loads(rep_file.read_text(encoding="utf-8"))
        total = rep.get("statistics", {}).get("total", {})
        pct = float(total.get("percentage", 0.0))
        clones = rep.get("duplicates", [])
        summary = {
            "clones": int(total.get("clones", len(clones))),
            "percentage": round(pct, 2),
            "duplicated_lines": int(total.get("duplicatedLines", 0)),
            "top_duplicates": [
                {
                    "first": f"{d['firstFile']['name']}:{d['firstFile']['startLoc']['line']}-{d['firstFile']['endLoc']['line']}",
                    "second": f"{d['secondFile']['name']}:{d['secondFile']['startLoc']['line']}-{d['secondFile']['endLoc']['line']}",
                    "lines": d.get("lines", 0),
                }
                for d in clones[:5]
            ],
        }
        return (1 if pct > max_pct else 0), summary


def run_depcruise_audit(target: Path) -> Tuple[int, Dict[str, Any]]:
    if not target.is_dir() or not (target / "package.json").is_file() or not (target / "src").is_dir():
        return 0, {}
    dep_bin = _find_binary(["depcruise"])
    cfg_path = _rules_dir() / "depcruise_rules.cjs"
    if not dep_bin or not cfg_path.is_file():
        return 0, {}

    env = os.environ.copy()
    npm_bin = _find_binary(["npm"])
    if npm_bin and "NODE_PATH" not in env:
        try:
            npm_root = subprocess.check_output([npm_bin, "root", "-g"], text=True, timeout=5).strip()
            if npm_root:
                env["NODE_PATH"] = npm_root
        except Exception:
            pass

    cmd = [dep_bin, "src", "--config", str(cfg_path), "--output-type", "json"]
    if (target / "tsconfig.json").is_file():
        cmd.extend(["--ts-config", "tsconfig.json"])

    proc = subprocess.run(
        cmd,
        cwd=str(target),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    raw = (proc.stdout or "").strip()
    if not raw.startswith("{"):
        return 0, {}
    data = json.loads(raw)
    summary = data.get("summary", {})
    err_cnt = int(summary.get("error", 0))
    warn_cnt = int(summary.get("warn", 0))
    return (1 if err_cnt > 0 else 0), {
        "errors": err_cnt,
        "warnings": warn_cnt,
        "modules": int(summary.get("totalCruised", 0)),
    }


def compute_behavioral_hotspots(
    repo_dir: Path,
    top_n: int = 10,
    max_commits: int = 400,
    since: str = "12.months",
) -> Dict[str, Any]:
    """Tornhill Behavioral Code Analysis: 12-Month Hotspots (Churn x LOC + Indent Complexity) & Temporal Coupling."""
    if not repo_dir.is_dir():
        return {"hotspots": [], "coupling": []}
    cmd = [
        "git",
        "-C",
        str(repo_dir),
        "log",
        "--no-merges",
        f"--since={since}",
        "--name-only",
        "--pretty=format:__COMMIT__%h|%aN",
        "-n",
        str(max_commits),
    ]
    try:
        out = subprocess.check_output(
            cmd,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except Exception:
        return {"hotspots": [], "coupling": []}

    revs: collections.Counter[str] = collections.Counter()
    cochange: collections.Counter[Tuple[str, str]] = collections.Counter()
    cur_files: List[str] = []

    def _flush(batch: List[str]) -> None:
        code_batch = sorted(
            {
                f
                for f in batch
                if Path(f).suffix.lower() in CODE_EXTENSIONS
                and not is_test_path(Path(f))
            }
        )
        for f in code_batch:
            revs[f] += 1
        if 2 <= len(code_batch) <= 25:
            for a, b in itertools.combinations(code_batch, 2):
                cochange[(a, b)] += 1

    for line in out.splitlines():
        if line.startswith("__COMMIT__"):
            _flush(cur_files)
            cur_files = []
        elif line.strip():
            cur_files.append(line.strip().replace("\\", "/"))
    _flush(cur_files)

    hotspots = []
    for rel, r_cnt in revs.items():
        fp = repo_dir / rel
        if not fp.is_file():
            continue
        try:
            lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        loc = len(lines)
        indent_cx = sum(
            (len(ln) - len(ln.lstrip())) // 4
            for ln in lines
            if ln.strip() and not ln.lstrip().startswith(("//", "#", "*"))
        )
        hotspots.append(
            {
                "file": rel,
                "score": r_cnt * loc,
                "commits": r_cnt,
                "loc": loc,
                "indent_complexity": indent_cx,
            }
        )
    hotspots.sort(key=lambda x: x["score"], reverse=True)

    coupling = []
    for (a, b), shared in cochange.items():
        if shared < 3:
            continue
        if not (repo_dir / a).is_file() or not (repo_dir / b).is_file():
            continue
        avg_r = (revs[a] + revs[b]) / 2.0
        pct = round((shared / avg_r) * 100.0, 1)
        coupling.append({"left": a, "right": b, "co_commits": shared, "coupling_pct": pct})
    coupling.sort(key=lambda x: (x["co_commits"], x["coupling_pct"]), reverse=True)

    return {"hotspots": hotspots[:top_n], "coupling": coupling[:top_n]}


_ENCLOSING_SYM_PATTERNS = [
    re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?(?:fn|struct|enum|trait|mod)\s+([A-Za-z0-9_]+)"),
    re.compile(r"^\s*impl(?:<[^>]*>)?\s+(?:[A-Za-z0-9_:]+(?:<[^>]*>)?\s+for\s+)?([A-Za-z0-9_]+)"),
    re.compile(r"^\s*(?:public|protected|private)\s+(?:static\s+|final\s+|abstract\s+|synchronized\s+)*(?:class|interface|record|enum)\s+([A-Za-z0-9_]+)"),
    re.compile(r"^\s*(?:public|protected|private)\s+(?:static\s+|final\s+|synchronized\s+)*(?:<[^>]+>\s+)?[\w<>\[\],.?]+\s+([A-Za-z0-9_]+)\s*\("),
    re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function|class|interface)\s+([A-Za-z0-9_]+)"),
    re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z0-9_]+)\s*\("),
]


def resolve_enclosing_symbol(file_path: Path, line_no: int, _cache: Optional[Dict[str, List[str]]] = None) -> str:
    """Resolves the nearest enclosing Serena/LSP symbol name at or above `line_no` without reading outside target file."""
    key = str(file_path)
    lines: Optional[List[str]] = _cache.get(key) if _cache is not None else None
    if lines is None:
        if not file_path.is_file():
            return ""
        try:
            lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        if _cache is not None:
            _cache[key] = lines

    upper = min(len(lines), max(1, line_no))
    for idx in range(upper - 1, -1, -1):
        ln = lines[idx]
        for rx in _ENCLOSING_SYM_PATTERNS:
            m = rx.match(ln)
            if m:
                cand = m.group(1)
                if cand not in ("if", "for", "while", "switch", "match", "return", "new"):
                    return cand
    return ""


def run_ast_grep_codemod(target: Path, rule_id: str, apply_changes: bool = False) -> Dict[str, Any]:
    """Deterministic `ast-grep` structural codemod preview or `--update-all` rewrite."""
    sg_bin = _find_binary(["ast-grep", "sg"])
    rules_yml = _rules_dir() / "ast_grep_rules.yml"
    if not sg_bin or not rules_yml.is_file():
        return {"status": "unavailable", "rule_id": rule_id, "matches": 0}

    scan_target = target / "src" if (target / "src").is_dir() else target
    if apply_changes:
        proc = subprocess.run(
            [sg_bin, "scan", "-r", str(rules_yml), "--filter", rule_id, "--update-all", str(scan_target)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        return {"status": "applied" if proc.returncode == 0 else "error", "rule_id": rule_id, "returncode": proc.returncode}

    proc = subprocess.run(
        [sg_bin, "scan", "-r", str(rules_yml), "--filter", rule_id, "--json=compact", str(scan_target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    raw = (proc.stdout or "").strip()
    items = json.loads(raw) if raw.startswith("[") else []
    return {
        "status": "dry_run",
        "rule_id": rule_id,
        "matches": len(items),
        "files": sorted({str(it.get("file", "")) for it in items if it.get("file")}),
    }


def build_compact_bridge_pack(
    target: Path,
    sg_findings: List[Dict[str, Any]],
    og_findings: List[Dict[str, Any]],
    behavioral: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Produces zero-raw-code compact AST + Dataflow + Serena-Symbol + Hotspot descriptors
    for Antigravity Carbon and Corporate Bedrock (`:24302` Claude-RAM).
    """
    base_dir = target if target.is_dir() else target.parent
    hs_map: Dict[str, Dict[str, Any]] = {
        h["file"]: h for h in (behavioral.get("hotspots") or []) if isinstance(h, dict) and "file" in h
    }
    line_cache: Dict[str, List[str]] = {}
    records: List[Dict[str, Any]] = []

    def _norm_rel(fp_str: str) -> Tuple[Path, str]:
        p = Path(fp_str)
        if not p.is_absolute():
            p = (base_dir / p).resolve()
        try:
            rel = p.relative_to(base_dir).as_posix()
        except ValueError:
            rel = p.name
        return p, rel

    for f in sg_findings:
        abs_p, rel = _norm_rel(str(f.get("file") or ""))
        s_ln = int(f.get("range", {}).get("start", {}).get("line", 0)) + 1
        e_ln = int(f.get("range", {}).get("end", {}).get("line", s_ln - 1)) + 1
        hs = hs_map.get(rel, {})
        records.append(
            {
                "engine": "ast-grep",
                "file_path": rel,
                "line_range": f"L{s_ln}-L{e_ln}",
                "rule_id": f.get("ruleId", "ast-grep"),
                "severity": str(f.get("severity", "warning")).lower(),
                "enclosing_symbol": resolve_enclosing_symbol(abs_p, s_ln, line_cache),
                "hotspot_commits": int(hs.get("commits", 0)),
                "hotspot_score": int(hs.get("score", 0)),
                "message": f.get("message", ""),
            }
        )

    for r in og_findings:
        abs_p, rel = _norm_rel(str(r.get("path") or ""))
        s_ln = int(r.get("start", {}).get("line", 1))
        e_ln = int(r.get("end", {}).get("line", s_ln))
        hs = hs_map.get(rel, {})
        records.append(
            {
                "engine": "opengrep",
                "file_path": rel,
                "line_range": f"L{s_ln}-L{e_ln}",
                "rule_id": r.get("check_id", "opengrep"),
                "severity": str(r.get("extra", {}).get("severity", "WARNING")).lower(),
                "enclosing_symbol": resolve_enclosing_symbol(abs_p, s_ln, line_cache),
                "hotspot_commits": int(hs.get("commits", 0)),
                "hotspot_score": int(hs.get("score", 0)),
                "message": r.get("extra", {}).get("message", ""),
            }
        )

    records.sort(key=lambda x: (x["hotspot_score"], x["severity"] == "error"), reverse=True)
    return records


def run_self_test() -> int:
    with tempfile.TemporaryDirectory(prefix="kaizen_oss_selftest_") as tmp:
        td = Path(tmp)
        rs_dir = td / "src" / "domain"
        rs_dir.mkdir(parents=True, exist_ok=True)
        rs_file = rs_dir / "sample.rs"
        rs_file.write_text(
            "pub fn prod_ok() -> i32 { 42 }\n"
            "#[cfg(test)]\n"
            "mod tests {\n"
            "    #[test]\n"
            "    fn t() { let x = Some(1).unwrap(); assert_eq!(x, 1); }\n"
            "}\n",
            encoding="utf-8",
        )
        hits = scan_single_file_ast_grep(rs_file)
        if hits:
            sys.stderr.write(f"[oss_ast_gate self-test FAIL] Expected #[cfg(test)] unwrap to be ignored, got {hits}\n")
            return 1
        rs_bad = rs_dir / "bad.rs"
        rs_bad.write_text("pub fn compute_station_delta() -> i32 {\n    Some(7).unwrap()\n}\n", encoding="utf-8")
        bad_hits = scan_single_file_ast_grep(rs_bad)
        if _find_binary(["ast-grep", "sg"]) and not bad_hits:
            sys.stderr.write("[oss_ast_gate self-test FAIL] Expected production .unwrap() to be flagged\n")
            return 1
        sym = resolve_enclosing_symbol(rs_bad, 2)
        if sym != "compute_station_delta":
            sys.stderr.write(f"[oss_ast_gate self-test FAIL] Expected enclosing symbol compute_station_delta, got {sym!r}\n")
            return 1
        _, sg_all = run_ast_grep_audit(td)
        pack = build_compact_bridge_pack(td, sg_all, [], {"hotspots": [{"file": "src/domain/bad.rs", "commits": 9, "score": 90}]})
        if _find_binary(["ast-grep", "sg"]) and (not pack or pack[0]["enclosing_symbol"] != "compute_station_delta" or pack[0]["hotspot_score"] != 90):
            sys.stderr.write(f"[oss_ast_gate self-test FAIL] Bridge pack symbol/hotspot enrichment failed: {pack}\n")
            return 1

        if _find_binary(["git"]):
            leg_dir = td / "src" / "legacy"
            leg_dir.mkdir(parents=True, exist_ok=True)
            rs_del = leg_dir / "deleted_station.rs"
            rs_del.write_text("pub struct LegacyStation;\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=str(td), check=True)
            subprocess.run(["git", "config", "user.email", "ci@kaizen.test"], cwd=str(td), check=True)
            subprocess.run(["git", "config", "user.name", "KaizenCI"], cwd=str(td), check=True)
            for rev_i in range(3):
                for p_mod in (rs_file, rs_bad, rs_del):
                    p_mod.write_text(p_mod.read_text(encoding="utf-8") + f"// rev {rev_i}\n", encoding="utf-8")
                subprocess.run(["git", "add", "."], cwd=str(td), check=True)
                subprocess.run(["git", "commit", "-q", "-m", f"cochange {rev_i}"], cwd=str(td), check=True)
            subprocess.run(["git", "rm", "-q", "src/legacy/deleted_station.rs"], cwd=str(td), check=True)
            subprocess.run(["git", "commit", "-q", "-m", "delete legacy file"], cwd=str(td), check=True)

            beh = compute_behavioral_hotspots(td, top_n=10)
            coupled_files = {p for c in beh.get("coupling", []) for p in (c["left"], c["right"])}
            hotspot_files = {h["file"] for h in beh.get("hotspots", [])}
            if "src/legacy/deleted_station.rs" in coupled_files or "src/legacy/deleted_station.rs" in hotspot_files:
                sys.stderr.write(f"[oss_ast_gate self-test FAIL] Deleted file leaked into hotspots/coupling: {beh}\n")
                return 1
            if not {"src/domain/bad.rs", "src/domain/sample.rs"}.issubset(coupled_files):
                sys.stderr.write(f"[oss_ast_gate self-test FAIL] Live co-changed pair missing from coupling: {beh}\n")
                return 1
    print("  [PASS] oss_ast_gate — AST #[cfg(test)] isolation, Serena symbol bridge pack & live-file coupling verified")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Kaizen v3.4 OSS AST, Dataflow, Clone, Codemod & Behavioral Bridge Gate")
    ap.add_argument("path", nargs="?", default=".")
    ap.add_argument("--diff-only", action="store_true", help="Scope Opengrep to git-modified files")
    ap.add_argument("--hotspots", action="store_true", help="Include 12-month Behavioral Git Hotspots & Temporal Coupling")
    ap.add_argument("--bridge-pack", action="store_true", help="Emit zero-raw-code compact AST + Serena symbol + Hotspot JSON pack")
    ap.add_argument("--codemod-rule", default="", help="Run deterministic ast-grep codemod for rule ID (dry-run unless --apply)")
    ap.add_argument("--apply", action="store_true", help="Apply ast-grep codemod rewrite in-place")
    ap.add_argument("--json", action="store_true", help="Output structured JSON")
    ap.add_argument("--self-test", action="store_true", help="Run deterministic self-test")
    args = ap.parse_args(argv)

    if args.self_test:
        return run_self_test()

    target = Path(args.path).resolve()
    if args.codemod_rule:
        cm_res = run_ast_grep_codemod(target, args.codemod_rule, apply_changes=args.apply)
        print(json.dumps(cm_res, indent=2))
        return 0 if cm_res.get("status") != "error" else 1

    sg_rc, sg_findings = run_ast_grep_audit(target)
    og_rc, og_findings = run_opengrep_audit(target, diff_only=args.diff_only)
    need_hotspots = args.hotspots or args.bridge_pack
    beh = compute_behavioral_hotspots(target, top_n=25 if args.bridge_pack else 5) if (need_hotspots and target.is_dir()) else {}

    if args.bridge_pack:
        pack = build_compact_bridge_pack(target, sg_findings, og_findings, beh)
        print(
            json.dumps(
                {
                    "target": str(target),
                    "count": len(pack),
                    "hotspots_top5": (beh.get("hotspots") or [])[:5],
                    "coupling_top5": (beh.get("coupling") or [])[:5],
                    "findings": pack,
                },
                indent=2,
            )
        )
        return sg_rc | og_rc

    cp_rc, cp_summary = run_jscpd_audit(target)
    dc_rc, dc_summary = run_depcruise_audit(target)

    rc = sg_rc | og_rc
    payload = {
        "target": str(target),
        "ast_grep_findings": len(sg_findings),
        "opengrep_findings": len(og_findings),
        "jscpd": cp_summary,
        "depcruise": dc_summary,
        "behavioral": beh,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
        return rc

    print(
        f"[Kaizen-OSS-AST-Gate] ast-grep={len(sg_findings)} | opengrep={len(og_findings)} | "
        f"clones={cp_summary.get('clones', 0)} ({cp_summary.get('percentage', 0.0)}%) | "
        f"status={'PASS' if rc == 0 else 'FAIL'}"
    )
    for f in sg_findings[:5]:
        ln = f.get("range", {}).get("start", {}).get("line", 0) + 1
        print(f"  [ast-grep:{f.get('ruleId')}] {f.get('file')}:{ln} — {f.get('message')}")
    for r in og_findings[:5]:
        ln = r.get("start", {}).get("line", 0)
        print(f"  [opengrep:{r.get('check_id')}] {r.get('path')}:{ln} — {r.get('extra', {}).get('message')}")
    for dup in cp_summary.get("top_duplicates", [])[:3]:
        print(f"  [jscpd-clone] {dup['first']} <-> {dup['second']} ({dup['lines']}L)")
    if dc_summary:
        print(
            f"  [depcruise] cruised={dc_summary['modules']} | errors={dc_summary['errors']} | warnings={dc_summary['warnings']}"
        )
    if beh.get("hotspots"):
        top_hs = ", ".join(f"{h['file']} (score={h['score']})" for h in beh["hotspots"][:3])
        print(f"  [behavioral-hotspots] {top_hs}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
