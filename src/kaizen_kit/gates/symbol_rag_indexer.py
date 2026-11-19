#!/usr/bin/env python3
"""
Portable Symbol-Summary Conceptual Discovery Engine (`symbol_rag_indexer.py`).

Implements zero-full-file conceptual symbol discovery:
  1. Indexes workspace symbol summaries (`name + split_tokens + kind + repo + path + signature + doc_summary`)
     from `workspace_symbols.sqlite` (reusing existing symbol DB when present or building one portably
     with stdlib regex/Avro parsing across single-repo or `<group>/<repo>` workspaces).
  2. Indexes lightweight architectural Markdown section headings (`README.md`, `CLAUDE.md`, `AGENTS.md`,
     `PROJECT-CATALOG.md`, `docs/architecture/*.md`).
  3. Scores queries via SQLite FTS5 BM25 (`porter unicode61`) + 64-bit 1-bit sign-quantized semantic
     sketches (`POPCNT` Hamming similarity `(q_bits ^ doc_bits).bit_count()`).
  4. Emits exact Serena/LSP symbol coordinates (`repo`, `rel_path:line`, `name`, `kind`, `signature`, `score`).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

_KIT_SRC = Path(__file__).resolve().parent.parent.parent
if str(_KIT_SRC) not in sys.path:
    sys.path.insert(0, str(_KIT_SRC))

from kaizen_kit.paths import resolve_state_dir

SKETCH_BITS = 64
SKETCH_HEX_LEN = SKETCH_BITS // 4
MAX_SOURCE_BYTES = 1_500_000

SOURCE_EXTS = {".java", ".kt", ".rs", ".ts", ".tsx", ".vue", ".js", ".mjs", ".py", ".go", ".avsc", ".proto"}
SKIP_DIRS = {
    ".git", "node_modules", "target", "build", "dist", "out", ".serena", "graphify-out", ".idea", ".vscode",
    ".gradle", "coverage", "vendor", ".next", ".nuxt", "__pycache__", ".venv", "venv", ".cursor", ".claude",
    ".tox", ".mypy_cache", ".pytest_cache", "bin", "obj", ".angular", "storybook-static", "generated-sources",
    ".triage-mirrors", "fw-analysis", "artifacts",
}

CAMEL_SPLIT_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|[_\-./:\\]+")
WORD_RE = re.compile(r"[a-zA-Z0-9]{2,}")
MD_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*$")
STOPWORDS = {
    "the", "and", "for", "with", "from", "into", "that", "this", "are", "was", "were",
    "bir", "icin", "olan", "veya", "ile", "gibi", "nasil", "nedir", "get", "set", "pub",
    "public", "private", "protected", "async", "void", "return", "impl", "func", "def",
}

_JAVA_TYPE = re.compile(
    r"^\s*(?:@\w+(?:\([^)]*\))?\s+)*(?:(?:public|protected|private|abstract|final|static|sealed|non-sealed|data|open|internal)\s+)*(class|interface|enum|record|@interface|object)\s+(\w+)"
)
_JAVA_METHOD = re.compile(
    r"^\s*(?:public|protected)\s+(?:(?:static|final|synchronized|abstract|default|native)\s+)*(?:<[^>]+>\s+)?[\w<>\[\],.?\s]+?\s+(\w+)\s*\("
)
_JAVA_ENDPOINT = re.compile(r'@(Get|Post|Put|Delete|Patch|Request)Mapping\s*\(\s*(?:value\s*=\s*|path\s*=\s*)?\{?\s*"([^"]+)"')
_RUST_ITEM = re.compile(
    r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?(?:extern\s+\"C\"\s+)?(fn|struct|enum|trait|type|mod|const|static|union|macro_rules!)\s*(\w+)"
)
_RUST_ROUTE = re.compile(r'\.route\(\s*"([^"]+)"')
_TS_DECL = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?(class|interface|enum|type|function|async function)\s+(\w+)"
)
_TS_EXPORT_CONST = re.compile(r"^\s*export\s+(?:const|let)\s+(\w+)")
_PY_DECL = re.compile(r"^(\s*)(class|def|async def)\s+(\w+)")
_GO_FUNC = re.compile(r"^func\s+(?:\([^)]*\)\s*)?(\w+)")
_GO_TYPE = re.compile(r"^type\s+(\w+)\s+(struct|interface)")
_PROTO = re.compile(r"^\s*(message|service|rpc|enum)\s+(\w+)")

_FEAT_HASH_CACHE: Dict[str, int] = {}
_BIT_MASKS_64 = tuple(1 << b for b in range(SKETCH_BITS))


def default_concept_db_path() -> Path:
    return resolve_state_dir() / "symbol_concept_rag.sqlite"


def default_symbol_db_path() -> Path:
    kit_sym_db = resolve_state_dir() / "workspace_symbols.sqlite"
    if kit_sym_db.is_file():
        return kit_sym_db
    if not os.environ.get("AI_KIT_STATE_DIR", "").strip():
        shared_agy_db = Path.home() / ".gemini" / "antigravity" / "workspace_symbols.sqlite"
        if shared_agy_db.is_file():
            return shared_agy_db
    return kit_sym_db


def resolve_workspace_root(explicit: Optional[Path] = None) -> Path:
    if explicit is not None:
        return explicit.expanduser().resolve()
    for env_key in ("AI_KIT_WORKSPACE", "KAIZEN_WORKSPACE", "CLAUDE_PROJECT_DIR"):
        raw = os.environ.get(env_key, "").strip()
        if raw:
            cand = Path(raw).expanduser().resolve()
            if cand.is_dir():
                return cand
    cwd = Path.cwd().resolve()
    for parent in (cwd, *list(cwd.parents)[:5]):
        if (parent / "PROJECT-CATALOG.md").is_file():
            return parent
    for parent in (cwd, *list(cwd.parents)[:5]):
        if (parent / ".git").exists():
            return parent
    return cwd


def discover_workspace_repos(ws_root: Path) -> List[Tuple[str, Path]]:
    """Discovers either a single repository at `ws_root` or `<group>/<repo>` / `<repo>` layouts."""
    if not ws_root.is_dir():
        return []
    if (ws_root / ".git").exists() or any((ws_root / m).is_file() for m in ("Cargo.toml", "pom.xml", "pyproject.toml", "package.json")):
        return [(ws_root.name, ws_root)]

    found: List[Tuple[str, Path]] = []
    try:
        top_entries = sorted(ws_root.iterdir())
    except OSError:
        return found

    for entry in top_entries:
        if not entry.is_dir() or entry.name in SKIP_DIRS or entry.name.startswith("."):
            continue
        if (entry / ".git").exists():
            found.append((entry.name, entry))
            continue
        try:
            sub_entries = sorted(entry.iterdir())
        except OSError:
            continue
        for sub in sub_entries:
            if sub.is_dir() and sub.name not in SKIP_DIRS and (sub / ".git").exists():
                found.append((sub.name, sub))

    return found or [(ws_root.name, ws_root)]


def _sig(line: str) -> str:
    s = line.strip()
    return s if len(s) <= 160 else s[:157] + "..."


def _extract_avro_symbols(text: str) -> List[Tuple[str, str, int, str]]:
    out: List[Tuple[str, str, int, str]] = []
    try:
        doc = json.loads(text)
    except Exception:
        return out

    def walk(node: Any, ns: str = "") -> None:
        if isinstance(node, list):
            for n in node:
                walk(n, ns)
            return
        if not isinstance(node, dict):
            return
        node_ns = node.get("namespace", ns)
        if node.get("type") in ("record", "enum", "fixed", "error") and isinstance(node.get("name"), str):
            full = f"{node_ns}.{node['name']}" if node_ns else node["name"]
            out.append((node["name"], f"avro-{node['type']}", 1, full))
        for f in node.get("fields", []) or []:
            if isinstance(f, dict):
                walk(f.get("type"), node_ns)
        for key in ("items", "values", "type"):
            if isinstance(node.get(key), (dict, list)):
                walk(node[key], node_ns)

    walk(doc)
    return out


def extract_file_symbols(path: Path) -> List[Tuple[str, str, int, str]]:
    ext = path.suffix.lower()
    try:
        if path.stat().st_size > MAX_SOURCE_BYTES:
            return []
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if ext == ".avsc":
        return _extract_avro_symbols(text)
    if ext == ".js" and path.name.endswith(".min.js"):
        return []

    syms: List[Tuple[str, str, int, str]] = []
    if ext == ".vue":
        syms.append((path.stem, "vue-component", 1, path.name))
    for i, line in enumerate(text.splitlines(), 1):
        if len(line) > 400:
            continue
        if ext in (".java", ".kt"):
            m = _JAVA_TYPE.match(line)
            if m:
                syms.append((m.group(2), m.group(1).lstrip("@"), i, _sig(line)))
                continue
            m = _JAVA_ENDPOINT.search(line)
            if m:
                syms.append((m.group(2), f"endpoint-{m.group(1).lower()}", i, _sig(line)))
                continue
            m = _JAVA_METHOD.match(line)
            if m and m.group(1) not in ("if", "for", "while", "switch", "catch"):
                syms.append((m.group(1), "method", i, _sig(line)))
        elif ext == ".rs":
            m = _RUST_ITEM.match(line)
            if m:
                syms.append((m.group(2), m.group(1).rstrip("!"), i, _sig(line)))
                continue
            m = _RUST_ROUTE.search(line)
            if m:
                syms.append((m.group(1), "endpoint-route", i, _sig(line)))
        elif ext in (".ts", ".tsx", ".vue", ".js", ".mjs"):
            m = _TS_DECL.match(line)
            if m:
                syms.append((m.group(2), m.group(1).replace("async ", ""), i, _sig(line)))
                continue
            m = _TS_EXPORT_CONST.match(line)
            if m:
                syms.append((m.group(1), "const", i, _sig(line)))
        elif ext == ".py":
            m = _PY_DECL.match(line)
            if m and len(m.group(1)) <= 4 and not m.group(3).startswith("_"):
                syms.append((m.group(3), m.group(2).replace("async ", ""), i, _sig(line)))
        elif ext == ".go":
            m = _GO_FUNC.match(line)
            if m:
                syms.append((m.group(1), "func", i, _sig(line)))
                continue
            m = _GO_TYPE.match(line)
            if m:
                syms.append((m.group(1), m.group(2), i, _sig(line)))
        elif ext == ".proto":
            m = _PROTO.match(line)
            if m:
                syms.append((m.group(2), f"proto-{m.group(1)}", i, _sig(line)))
    return syms


def build_portable_symbol_db(symbol_db_path: Path, workspace_root: Path) -> int:
    """Builds a portable `workspace_symbols.sqlite` (`sym` table) without external plugin dependencies."""
    symbol_db_path.parent.mkdir(parents=True, exist_ok=True)
    rows: List[Tuple[str, str, str, str, int, str]] = []
    for repo_name, repo_path in discover_workspace_repos(workspace_root):
        for root_str, dirnames, filenames in os.walk(repo_path):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                ext = os.path.splitext(fn)[1].lower()
                if ext not in SOURCE_EXTS:
                    continue
                fp = Path(root_str) / fn
                rel = fp.relative_to(repo_path).as_posix()
                for name, kind, line, sig in extract_file_symbols(fp):
                    rows.append((name, kind, repo_name, rel, line, sig))

    conn = sqlite3.connect(str(symbol_db_path))
    try:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sym (
                    name TEXT,
                    kind TEXT,
                    repo TEXT,
                    path TEXT,
                    line INTEGER,
                    signature TEXT
                )
                """
            )
            conn.execute("DELETE FROM sym")
            if rows:
                conn.executemany("INSERT INTO sym VALUES (?, ?, ?, ?, ?, ?)", rows)
    finally:
        conn.close()
    return len(rows)


def decompose_tokens(text: str) -> List[str]:
    spaced = CAMEL_SPLIT_RE.sub(" ", text or "")
    raw_words = [w.lower() for w in WORD_RE.findall(spaced)]
    return [w for w in raw_words if w not in STOPWORDS and len(w) >= 2]


def _feature_bits64(feat: str) -> int:
    cached = _FEAT_HASH_CACHE.get(feat)
    if cached is not None:
        return cached
    val = int.from_bytes(hashlib.blake2b(feat.encode("utf-8", errors="ignore"), digest_size=8).digest(), "little")
    if len(_FEAT_HASH_CACHE) < 131072:
        _FEAT_HASH_CACHE[feat] = val
    return val


def compute_1bit_sketch(features: Iterable[Tuple[str, int]]) -> int:
    feat_list = [(f, w) for f, w in features if f and w > 0]
    if not feat_list:
        return 0
    if len(feat_list) == 1:
        return _feature_bits64(feat_list[0][0])

    total_w = 0
    acc = [0] * SKETCH_BITS
    masks = _BIT_MASKS_64
    for feat, weight in feat_list:
        total_w += weight
        x = _feature_bits64(feat)
        while x:
            lsb = x & -x
            acc[lsb.bit_length() - 1] += weight
            x ^= lsb

    half = total_w >> 1
    sketch = 0
    for idx, pos_w in enumerate(acc):
        if pos_w > half:
            sketch |= masks[idx]
    return sketch


def build_text_sketch(
    name: str,
    rel_path: str,
    signature: str,
    summary: str = "",
    *,
    pre_tokens: Optional[Tuple[List[str], List[str], List[str], List[str]]] = None,
) -> str:
    feats: List[Tuple[str, int]] = []
    if pre_tokens is not None:
        name_toks, path_toks, sig_toks, sum_toks = pre_tokens
    else:
        name_toks = decompose_tokens(name)[:12]
        path_toks = decompose_tokens(rel_path)[:10]
        sig_toks = decompose_tokens(signature)[:12]
        sum_toks = decompose_tokens(summary)[:12]

    for t in name_toks[:12]:
        feats.append((f"t:{t}", 5))
        if len(t) >= 4:
            feats.append((f"p:{t[:4]}", 2))
    for t in path_toks[:10]:
        feats.append((f"t:{t}", 3))
    for t in sig_toks[:12]:
        feats.append((f"t:{t}", 2))
    for t in sum_toks[:12]:
        feats.append((f"t:{t}", 2))
    for i in range(min(len(name_toks), 12) - 1):
        feats.append((f"b:{name_toks[i]}_{name_toks[i+1]}", 4))

    bits = compute_1bit_sketch(feats)
    return f"{bits:0{SKETCH_HEX_LEN}x}"


def build_query_sketch(query: str) -> int:
    q_toks = decompose_tokens(query)[:14]
    feats: List[Tuple[str, int]] = []
    for t in q_toks:
        feats.append((f"t:{t}", 5))
        if len(t) >= 4:
            feats.append((f"p:{t[:4]}", 2))
    for i in range(len(q_toks) - 1):
        feats.append((f"b:{q_toks[i]}_{q_toks[i+1]}", 4))
    return compute_1bit_sketch(feats)


def hamming_similarity(q_sketch: int, hex_sketch: str) -> float:
    if q_sketch == 0 or not hex_sketch:
        return 0.0
    try:
        doc_sketch = int(hex_sketch, 16)
    except ValueError:
        return 0.0
    dist = (q_sketch ^ doc_sketch).bit_count()
    return max(0.0, 1.0 - (dist / float(SKETCH_BITS)))


def _open_concept_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS concept_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            repo TEXT NOT NULL,
            rel_path TEXT NOT NULL,
            line INTEGER NOT NULL,
            signature TEXT NOT NULL,
            doc_summary TEXT NOT NULL,
            sketch_hex TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS concept_fts USING fts5(
            expanded_tokens,
            name,
            repo,
            rel_path,
            signature,
            doc_summary,
            tokenize='porter unicode61'
        );
        """
    )
    return conn


def _normalize_rel_path(raw_path: str, repo_name: str) -> str:
    norm = (raw_path or "").replace("\\", "/")
    marker = f"/{repo_name}/"
    idx = norm.lower().find(marker.lower())
    if idx >= 0:
        return norm[idx + len(marker) :]
    return norm


def _iter_architecture_md_headings(
    roots: Optional[List[Path]] = None,
    workspace_root: Optional[Path] = None,
) -> List[Tuple[str, str, str, str, int, str, str]]:
    entries: List[Tuple[str, str, str, str, int, str, str]] = []
    candidate_files: List[Tuple[str, Path, Path]] = []

    if roots is not None:
        for r in roots:
            if r.is_dir():
                for md in r.rglob("*.md"):
                    if not any(part in SKIP_DIRS for part in md.parts):
                        candidate_files.append((r.name, r, md))
    else:
        ws_root = resolve_workspace_root(workspace_root)
        for top_md in ("PROJECT-CATALOG.md", "README-KAIZEN-LAYOUT.md", "AGENTS.md", "README.md"):
            p = ws_root / top_md
            if p.is_file():
                candidate_files.append((ws_root.name, ws_root, p))
        for repo_name, rpath in discover_workspace_repos(ws_root):
            for rel_glob in ("README.md", "CLAUDE.md", "AGENTS.md"):
                p = rpath / rel_glob
                if p.is_file() and (repo_name, rpath, p) not in candidate_files:
                    candidate_files.append((repo_name, rpath, p))
            arch_dir = rpath / "docs" / "architecture"
            if arch_dir.is_dir():
                for p in arch_dir.glob("*.md"):
                    candidate_files.append((repo_name, rpath, p))

    for repo_name, base_dir, md_file in candidate_files:
        try:
            lines = md_file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        rel = md_file.relative_to(base_dir).as_posix()
        for idx, ln in enumerate(lines, 1):
            m = MD_HEADING_RE.match(ln)
            if not m:
                continue
            title = m.group(2).strip()
            snippet_parts: List[str] = []
            for nxt in lines[idx : idx + 4]:
                s = nxt.strip()
                if s and not s.startswith("#") and not s.startswith("```"):
                    snippet_parts.append(s)
            summary = " ".join(snippet_parts)[:180]
            entries.append((title, "doc-heading", repo_name, rel, idx, ln.strip()[:160], summary))
    return entries


def build_concept_index(
    symbol_db_path: Optional[Path] = None,
    concept_db_path: Optional[Path] = None,
    extra_md_roots: Optional[List[Path]] = None,
    workspace_root: Optional[Path] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Incrementally synchronizes the symbol-summary conceptual index."""
    t0 = time.perf_counter()
    sym_db = symbol_db_path or default_symbol_db_path()
    con_db = concept_db_path or default_concept_db_path()
    ws_root = resolve_workspace_root(workspace_root)

    if (not sym_db.is_file() or (force and workspace_root is not None)) and extra_md_roots is None:
        target_sym_db = resolve_state_dir() / "workspace_symbols.sqlite" if symbol_db_path is None else sym_db
        build_portable_symbol_db(target_sym_db, ws_root)
        sym_db = target_sym_db

    src_mtime = str(int(sym_db.stat().st_mtime)) if sym_db.is_file() else "0"
    conn = _open_concept_db(con_db)
    try:
        cur_src = conn.execute("SELECT v FROM meta WHERE k='src_mtime'").fetchone()
        existing_cnt = conn.execute("SELECT COUNT(*) FROM concept_entries").fetchone()[0]
        if not force and cur_src and cur_src[0] == src_mtime and existing_cnt > 0:
            return {"status": "up_to_date", "entries": existing_cnt, "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1)}

        rows_to_insert: List[Tuple[str, str, str, str, int, str, str, str, str]] = []
        if sym_db.is_file():
            sconn = sqlite3.connect(str(sym_db))
            try:
                sym_rows = sconn.execute("SELECT name, kind, repo, path, line, signature FROM sym").fetchall()
            finally:
                sconn.close()
            path_cache: Dict[Tuple[str, str], Tuple[str, List[str]]] = {}
            for name, kind, repo, raw_path, line, sig in sym_rows:
                pkey = (raw_path, repo)
                cached_path = path_cache.get(pkey)
                if cached_path is None:
                    rel_path = _normalize_rel_path(raw_path, repo)
                    path_toks = decompose_tokens(rel_path)
                    path_cache[pkey] = (rel_path, path_toks)
                else:
                    rel_path, path_toks = cached_path
                name_toks = decompose_tokens(name)
                sig_toks = decompose_tokens(sig)
                expanded = " ".join(name_toks + path_toks + sig_toks)
                sketch = build_text_sketch(name, rel_path, sig, "", pre_tokens=(name_toks, path_toks, sig_toks, []))
                rows_to_insert.append((name, kind, repo, rel_path, int(line), sig, "", sketch, expanded))

        for title, kind, repo, rel_path, line, sig, summary in _iter_architecture_md_headings(
            roots=extra_md_roots,
            workspace_root=ws_root,
        ):
            t_toks = decompose_tokens(title)
            p_toks = decompose_tokens(rel_path)
            s_toks = decompose_tokens(sig)
            sum_toks = decompose_tokens(summary)
            expanded = " ".join(t_toks + p_toks + sum_toks)
            sketch = build_text_sketch(title, rel_path, sig, summary, pre_tokens=(t_toks, p_toks, s_toks, sum_toks))
            rows_to_insert.append((title, kind, repo, rel_path, int(line), sig, summary, sketch, expanded))

        with conn:
            conn.execute("DELETE FROM concept_entries")
            conn.execute("DELETE FROM concept_fts")
            conn.executemany(
                """
                INSERT INTO concept_entries(id, name, kind, repo, rel_path, line, signature, doc_summary, sketch_hex)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [(idx, r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7]) for idx, r in enumerate(rows_to_insert, 1)],
            )
            conn.executemany(
                """
                INSERT INTO concept_fts(rowid, expanded_tokens, name, repo, rel_path, signature, doc_summary)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [(idx, r[8], r[0], r[2], r[3], r[5], r[6]) for idx, r in enumerate(rows_to_insert, 1)],
            )
            conn.execute("INSERT OR REPLACE INTO meta(k, v) VALUES ('src_mtime', ?)", (src_mtime,))
            conn.execute("INSERT OR REPLACE INTO meta(k, v) VALUES ('built_at', ?)", (str(int(time.time())),))

        return {
            "status": "rebuilt",
            "entries": len(rows_to_insert),
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        }
    finally:
        conn.close()


def _try_optional_qdrant_bitnet(query: str, limit: int) -> Optional[List[Dict[str, Any]]]:
    qdrant_url = os.environ.get("KAIZEN_QDRANT_URL", "").strip()
    embed_url = os.environ.get("KAIZEN_BITNET_EMBED_URL", "").strip()
    if not qdrant_url or not embed_url:
        return None
    try:
        emb_req = urllib.request.Request(
            embed_url,
            data=json.dumps({"input": query}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        emb_data = json.loads(urllib.request.urlopen(emb_req, timeout=0.8).read().decode("utf-8"))
        vec = emb_data.get("embedding") or (emb_data.get("data") or [{}])[0].get("embedding")
        if not vec:
            return None
        q_req = urllib.request.Request(
            f"{qdrant_url.rstrip('/')}/collections/serena_symbols/points/search",
            data=json.dumps({"vector": vec, "limit": limit, "with_payload": True}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        q_res = json.loads(urllib.request.urlopen(q_req, timeout=0.8).read().decode("utf-8"))
        out = []
        for pt in q_res.get("result") or []:
            pl = pt.get("payload") or {}
            out.append(
                {
                    "name": pl.get("name", ""),
                    "kind": pl.get("kind", "symbol"),
                    "repo": pl.get("repo", ""),
                    "rel_path": pl.get("rel_path", ""),
                    "line": int(pl.get("line", 1)),
                    "signature": pl.get("signature", ""),
                    "doc_summary": pl.get("doc_summary", ""),
                    "score": round(float(pt.get("score", 0.0)), 4),
                    "engine": "bitnet-qdrant",
                }
            )
        return out or None
    except Exception:
        return None


def search_concepts(
    query: str,
    repo: Optional[str] = None,
    kind: Optional[str] = None,
    limit: int = 10,
    concept_db_path: Optional[Path] = None,
    symbol_db_path: Optional[Path] = None,
    workspace_root: Optional[Path] = None,
    *,
    rerank: bool = False,
    rrf_k: int = 60,
) -> List[Dict[str, Any]]:
    tokens = decompose_tokens(query)
    if not tokens:
        return []

    ext_hits = _try_optional_qdrant_bitnet(query, limit) or []

    con_db = concept_db_path or default_concept_db_path()
    sym_db = symbol_db_path or default_symbol_db_path()

    if workspace_root is not None:
        build_concept_index(symbol_db_path=sym_db, concept_db_path=con_db, workspace_root=workspace_root, force=True)
    elif not con_db.is_file():
        build_concept_index(symbol_db_path=sym_db, concept_db_path=con_db)
    else:
        chk = sqlite3.connect(str(con_db))
        try:
            row = chk.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='concept_entries'").fetchone()
            cnt = chk.execute("SELECT COUNT(*) FROM concept_entries").fetchone()[0] if row else 0
        finally:
            chk.close()
        if cnt == 0:
            build_concept_index(symbol_db_path=sym_db, concept_db_path=con_db, force=True)

    q_sketch = build_query_sketch(query)
    conn = _open_concept_db(con_db)
    try:
        fts_expr = " OR ".join(f'"{t}"*' for t in tokens[:10])
        sql = """
            SELECT e.name, e.kind, e.repo, e.rel_path, e.line, e.signature, e.doc_summary, e.sketch_hex,
                   bm25(concept_fts, 3.0, 5.0, 1.5, 2.0, 1.5, 2.0) AS rank_score
            FROM concept_fts
            JOIN concept_entries e ON e.id = concept_fts.rowid
            WHERE concept_fts MATCH ?
        """
        params: List[Any] = [fts_expr]
        if repo:
            sql += " AND lower(e.repo) LIKE ?"
            params.append(f"%{repo.lower()}%")
        if kind:
            sql += " AND lower(e.kind) LIKE ?"
            params.append(f"%{kind.lower()}%")
        sql += " ORDER BY rank_score LIMIT ?"
        params.append(max(limit * 8, 60))

        try:
            candidates = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            candidates = []

        if not candidates:
            fb_sql = "SELECT name, kind, repo, rel_path, line, signature, doc_summary, sketch_hex, 0.0 FROM concept_entries WHERE 1=1"
            fb_params: List[Any] = []
            if repo:
                fb_sql += " AND lower(repo) LIKE ?"
                fb_params.append(f"%{repo.lower()}%")
            if kind:
                fb_sql += " AND lower(kind) LIKE ?"
                fb_params.append(f"%{kind.lower()}%")
            fb_sql += " LIMIT 2500"
            candidates = conn.execute(fb_sql, fb_params).fetchall()
    finally:
        conn.close()

    scored: List[Dict[str, Any]] = []
    by_id: Dict[str, Dict[str, Any]] = {}
    bm25_order: List[Tuple[str, float]] = []
    sketch_order: List[Tuple[str, float]] = []
    q_set = set(tokens)
    q_prefixes = {t[:4] for t in tokens if len(t) >= 4}
    for name, c_kind, c_repo, rel_path, line, sig, doc_sum, sketch_hex, raw_bm25 in candidates:
        bm25_pos = max(0.0, -float(raw_bm25))
        sim_1bit = hamming_similarity(q_sketch, sketch_hex)
        cand_toks = set(decompose_tokens(f"{name} {rel_path} {sig} {doc_sum}"))
        overlap = len(q_set & cand_toks) / float(max(1, len(q_set)))
        has_stem_overlap = overlap > 0.0 or any(ct[:4] in q_prefixes for ct in cand_toks if len(ct) >= 4)
        if not has_stem_overlap and sim_1bit < 0.86:
            continue
        if bm25_pos <= 0.0 and overlap <= 0.0 and sim_1bit < 0.86:
            continue
        combined = (min(bm25_pos, 18.0) * 0.45) + (overlap * 5.0) + (sim_1bit * 3.0)
        cid = f"{c_repo}:{rel_path}:{int(line)}:{name}"
        entry = {
            "name": name,
            "kind": c_kind,
            "repo": c_repo,
            "rel_path": rel_path,
            "line": int(line),
            "signature": sig,
            "doc_summary": doc_sum,
            "score": round(combined, 3),
            "serena_handoff": f"lsp-query find {name} --project {c_repo}",
        }
        by_id[cid] = entry
        bm25_order.append((cid, bm25_pos + overlap * 5.0))
        sketch_order.append((cid, sim_1bit))

    dense_list: List[str] = []
    for ext in ext_hits:
        cid = f"{ext.get('repo', '')}:{ext.get('rel_path', '')}:{int(ext.get('line', 1))}:{ext.get('name', '')}"
        dense_list.append(cid)
        if cid not in by_id:
            by_id[cid] = {
                **ext,
                "serena_handoff": f"lsp-query find {ext.get('name', '')} --project {ext.get('repo', '')}",
            }

    from kaizen_kit.localmodels import HybridEmbedRerankClient, reciprocal_rank_fusion

    bm25_ids = [cid for cid, _ in sorted(bm25_order, key=lambda x: x[1], reverse=True)]
    sketch_ids = [cid for cid, _ in sorted(sketch_order, key=lambda x: x[1], reverse=True)]
    ranked_lists = [bm25_ids, sketch_ids]
    weights = [1.2, 0.8]
    if dense_list:
        ranked_lists.append(dense_list)
        weights.append(1.0)
    rrf_pairs = dict(reciprocal_rank_fusion(ranked_lists, k=rrf_k, weights=weights))

    for cid, entry in by_id.items():
        rrf_boost = rrf_pairs.get(cid, 0.0) * 15.0
        entry["rrf_score"] = round(rrf_pairs.get(cid, 0.0), 5)
        entry["score"] = round(float(entry.get("score", 0.0)) + rrf_boost, 3)
        scored.append(entry)

    scored.sort(key=lambda x: x["score"], reverse=True)
    top_candidates = scored[: max(limit * 2, limit)]
    if rerank and top_candidates:
        return HybridEmbedRerankClient().rerank(query, top_candidates, text_key="signature", top_k=limit)
    return top_candidates[:limit]


def format_concept_hits(hits: List[Dict[str, Any]]) -> str:
    if not hits:
        return "No conceptual symbol matches found."
    lines = ["[Symbol-Summary Conceptual RAG -> Hand off exact symbol to Serena MCP (:24280) / `lsp-query find`]"]
    for h in hits:
        doc_part = f" -- {h['doc_summary']}" if h.get("doc_summary") else ""
        lines.append(
            f"  [{h['score']:>5.2f}] {h['kind']:<11s} {h['repo']:<24s} {h['rel_path']}:{h['line']}  {h['name']}{doc_part}"
        )
    top = hits[0]
    if top["kind"] != "doc-heading":
        lines.append(f"  -> Next Serena step: `{top['serena_handoff']}`")
    return "\n".join(lines)


def run_self_test() -> int:
    with tempfile.TemporaryDirectory(prefix="kaizen_sym_rag_") as tmp:
        td = Path(tmp)
        sym_db = td / "sym.sqlite"
        con_db = td / "concept.sqlite"
        md_root = td / "sample-docs"
        md_root.mkdir(parents=True, exist_ok=True)
        (md_root / "architecture.md").write_text(
            "# Principal-Isolated Singleflight Coalescing\n"
            "Prevents cross-tenant cache bleed during 14->1 TelemetryDashboard request fan-out.\n",
            encoding="utf-8",
        )
        sconn = sqlite3.connect(str(sym_db))
        with sconn:
            sconn.execute("CREATE TABLE sym(name TEXT, kind TEXT, repo TEXT, path TEXT, line INT, signature TEXT)")
            sconn.executemany(
                "INSERT INTO sym VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        "VectorCubeSingleflightCoordinator",
                        "struct",
                        "vector_cube-engine",
                        "/workspace/vector_cube-engine/src/ingest/coordinator.rs",
                        28,
                        "pub struct VectorCubeSingleflightCoordinator",
                    ),
                    (
                        "fold_station_qoe_score",
                        "fn",
                        "vector_cube-engine",
                        "/workspace/vector_cube-engine/src/ingest/fold_avro.rs",
                        112,
                        "pub fn fold_station_qoe_score(rssi: i32) -> Option<u8>",
                    ),
                    (
                        "GreeterTeamLookupService",
                        "class",
                        "aas",
                        "/workspace/aas/src/main/java/GreeterTeamLookupService.java",
                        45,
                        "public class GreeterTeamLookupService",
                    ),
                ],
            )
        sconn.close()

        build_res = build_concept_index(symbol_db_path=sym_db, concept_db_path=con_db, extra_md_roots=[md_root], force=True)
        if build_res["entries"] < 4:
            sys.stderr.write(f"[symbol_rag_indexer FAIL] Expected >= 4 indexed entries, got {build_res}\n")
            return 1

        hits_qoe = search_concepts("station qoe telemetry score", concept_db_path=con_db, symbol_db_path=sym_db, limit=3)
        if not hits_qoe or hits_qoe[0]["name"] != "fold_station_qoe_score":
            sys.stderr.write(f"[symbol_rag_indexer FAIL] Unexpected top hit for station qoe: {hits_qoe}\n")
            return 1

        hits_sf = search_concepts("singleflight tenant coalescing", concept_db_path=con_db, symbol_db_path=sym_db, limit=3)
        if not hits_sf or "Singleflight" not in hits_sf[0]["name"]:
            sys.stderr.write(f"[symbol_rag_indexer FAIL] Expected Singleflight match, got: {hits_sf}\n")
            return 1

        hits_none = search_concepts("xyznonexistenttoken99999", concept_db_path=con_db, symbol_db_path=sym_db, limit=3)
        if hits_none:
            sys.stderr.write(f"[symbol_rag_indexer FAIL] Expected empty result for gibberish, got: {hits_none}\n")
            return 1

        # Verify zero-dependency portable workspace indexing when no external symbol DB exists
        ws_dir = td / "clean-ws" / "sample-repo"
        (ws_dir / ".git").mkdir(parents=True)
        (ws_dir / "src").mkdir(parents=True)
        (ws_dir / "src" / "reconcile.rs").write_text(
            "pub fn reconcile_mesh_topology_buckets(window_days: u8) -> bool { window_days > 0 }\n",
            encoding="utf-8",
        )
        portable_sym_db = td / "portable_sym.sqlite"
        portable_con_db = td / "portable_con.sqlite"
        hits_ws = search_concepts(
            "reconcile mesh topology",
            concept_db_path=portable_con_db,
            symbol_db_path=portable_sym_db,
            workspace_root=td / "clean-ws",
            limit=3,
        )
        if not hits_ws or hits_ws[0]["name"] != "reconcile_mesh_topology_buckets":
            sys.stderr.write(f"[symbol_rag_indexer FAIL] Portable workspace indexing failed: {hits_ws}\n")
            return 1

    print("  [PASS] symbol_rag_indexer -- camelCase BM25 + 64-bit 1-bit semantic sketch & portable workspace indexing verified")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Kaizen v3.4 Portable Symbol-Summary Conceptual Discovery")
    ap.add_argument("query", nargs="?", default="", help="Conceptual query when exact symbol name is unknown (or 'index')")
    ap.add_argument("--index", action="store_true", help="Build/refresh symbol concept index")
    ap.add_argument("--force", action="store_true", help="Force full rebuild of concept index")
    ap.add_argument("--workspace", default="", help="Workspace or git repo root to scan when building portable index")
    ap.add_argument("--repo", default=None, help="Filter by repository substring")
    ap.add_argument("--kind", default=None, help="Filter by symbol kind")
    ap.add_argument("--limit", type=int, default=10, help="Max results")
    ap.add_argument("--json", action="store_true", help="Emit compact JSON")
    ap.add_argument("--self-test", action="store_true", help="Run deterministic self-test")
    args = ap.parse_args(argv)

    if args.self_test:
        return run_self_test()

    ws_override = Path(args.workspace).expanduser().resolve() if args.workspace else None
    if args.index or args.query.strip().lower() == "index":
        res = build_concept_index(workspace_root=ws_override, force=args.force)
        print(json.dumps(res, indent=2))
        return 0
    if not args.query.strip():
        ap.print_help()
        return 1

    t0 = time.perf_counter()
    hits = search_concepts(args.query, repo=args.repo, kind=args.kind, limit=args.limit, workspace_root=ws_override)
    ms = round((time.perf_counter() - t0) * 1000, 1)
    if args.json:
        print(json.dumps({"query": args.query, "elapsed_ms": ms, "hits": hits}, indent=2))
        return 0
    print(format_concept_hits(hits))
    print(f"[{len(hits)} conceptual symbol candidates in {ms} ms]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
