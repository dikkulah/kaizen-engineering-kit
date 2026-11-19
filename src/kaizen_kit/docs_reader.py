#!/usr/bin/env python3
"""
4-Channel Canonical Codex & Official Documentation Reader (`docs_reader.py` -> `kaizen-kit docs`).

Gives every specialist agent (and developer in Claude Code / Antigravity / Terminal)
deterministic, zero-MCP-required access to:
  1. Channel 1 (`codex`): Instant <5ms search across the 7 Canonical Engineering Codices
     (`[CC-*]`, `[CA-*]`, `[RG-*]`, `[GOF-*]`, `[APOSD-*]`, `[DDD-*]`, `[RUST-*]`, `[EJ-*]`, `[AWS-*]`).
  2. Channel 2 (`fetch`): Zero-MCP pure Python official documentation reader with curated
     domain shortcuts (`refactoring.guru`, `docs.aws.amazon.com`, `doc.rust-lang.org`,
     `docs.rs`, `docs.spring.io`, `docs.oracle.com`, `openjdk.org`, `vuejs.org`), stripping
     HTML nav/script noise into clean Markdown.
  3. Channel 3 (`symbol`): Genchi Genbutsu local installed dependency / workspace symbol
     verifier scanning local Cargo cache (`~/.cargo/registry/src`), `node_modules` `.d.ts`,
     and workspace sources so agents never hallucinate nonexistent SDK methods.
  4. Channel 4 (`library`): Optional gitignored personal bookshelf (`~/.kaizen-kit/library/`)
     search for developer-owned study notes / Markdown extracts.
"""

import argparse
import html
import json
import re
import ssl
import sys
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_KIT_SRC = Path(__file__).resolve().parent.parent
if str(_KIT_SRC) not in sys.path:
    sys.path.insert(0, str(_KIT_SRC))

from kaizen_kit.paths import resolve_assets_dir, resolve_state_dir

OFFICIAL_DOC_SHORTCUTS: Dict[str, str] = {
    # Refactoring.Guru
    "rg-smells": "https://refactoring.guru/refactoring/smells",
    "rg-techniques": "https://refactoring.guru/refactoring/techniques",
    "rg-patterns": "https://refactoring.guru/design-patterns/catalog",
    # Official Rust & Tokio
    "rust-api-guidelines": "https://rust-lang.github.io/api-guidelines/checklist.html",
    "rust-reference": "https://doc.rust-lang.org/reference/introduction.html",
    "rust-nomicon": "https://doc.rust-lang.org/nomicon/intro.html",
    "tokio-tutorial": "https://tokio.rs/tokio/tutorial",
    # Official AWS Hard Quotas & Developer Guides (Bypassing shallow MCP summaries)
    "aws-lambda-quotas": "https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html",
    "aws-lambda-alb": "https://docs.aws.amazon.com/lambda/latest/dg/services-alb.html",
    "aws-lambda-rust": "https://docs.aws.amazon.com/lambda/latest/dg/lambda-rust.html",
    "aws-s3-optimizing": "https://docs.aws.amazon.com/AmazonS3/latest/userguide/optimizing-performance.html",
    "aws-dynamodb-limits": "https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/ServiceQuotas.html",
    "aws-xray-header": "https://docs.aws.amazon.com/xray/latest/devguide/xray-concepts.html#xray-concepts-tracingheader",
    "aws-bedrock-converse": "https://docs.aws.amazon.com/bedrock/latest/userguide/conversation-inference.html",
    # Official Java 21 / Spring Boot / Vue 3
    "java21-virtual-threads": "https://openjdk.org/jeps/444",
    "java21-pattern-switch": "https://openjdk.org/jeps/441",
    "java21-record-patterns": "https://openjdk.org/jeps/440",
    "spring-restclient": "https://docs.spring.io/spring-framework/reference/integration/rest-clients.html",
    "vue3-reactivity": "https://vuejs.org/guide/essentials/reactivity-fundamentals.html",
}

ALLOWED_DOC_DOMAINS = (
    "refactoring.guru",
    "docs.aws.amazon.com",
    "aws.amazon.com",
    "doc.rust-lang.org",
    "rust-lang.github.io",
    "docs.rs",
    "tokio.rs",
    "serde.rs",
    "docs.spring.io",
    "docs.oracle.com",
    "openjdk.org",
    "vuejs.org",
    "vite.dev",
    "playwright.dev",
    "developer.mozilla.org",
    "datatracker.ietf.org",
    "www.rfc-editor.org",
)


class _CleanDocHTMLParser(HTMLParser):
    """Lightweight stdlib HTML -> clean Markdown extractor stripping nav/script/style boilerplate."""

    SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "footer", "header", "aside", "form", "button"}
    HEADING_TAGS = {"h1": "# ", "h2": "\n## ", "h3": "\n### ", "h4": "\n#### "}

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self._in_pre = False
        self._in_code = False
        self._chunks: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        t = tag.lower()
        if t in self.SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth > 0:
            return
        if t in self.HEADING_TAGS:
            self._chunks.append(self.HEADING_TAGS[t])
        elif t == "pre":
            self._in_pre = True
            self._chunks.append("\n```\n")
        elif t == "code" and not self._in_pre:
            self._in_code = True
            self._chunks.append("`")
        elif t == "li":
            self._chunks.append("\n- ")
        elif t in ("p", "div", "section", "article", "tr", "br"):
            self._chunks.append("\n")
        elif t in ("td", "th"):
            self._chunks.append(" | ")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in self.SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth > 0:
            return
        if t in self.HEADING_TAGS:
            self._chunks.append("\n")
        elif t == "pre":
            self._in_pre = False
            self._chunks.append("\n```\n")
        elif t == "code" and self._in_code:
            self._in_code = False
            self._chunks.append("`")

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if self._in_pre:
            self._chunks.append(data)
        else:
            cleaned = re.sub(r"[ \t\r\f\v]+", " ", data)
            if cleaned.strip():
                self._chunks.append(cleaned)

    def to_markdown(self) -> str:
        raw = html.unescape("".join(self._chunks))
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def html_to_markdown(raw_html: str) -> str:
    parser = _CleanDocHTMLParser()
    parser.feed(raw_html)
    return parser.to_markdown()


def resolve_codices_dir() -> Path:
    assets_codices = resolve_assets_dir() / "references" / "codices"
    if assets_codices.is_dir():
        return assets_codices
    state_codices = resolve_state_dir() / "codices"
    return state_codices


def resolve_personal_library_dir() -> Path:
    lib = resolve_state_dir() / "library"
    lib.mkdir(parents=True, exist_ok=True)
    return lib


def search_markdown_corpus(directory: Path, query: str, max_sections: int = 8) -> List[Dict[str, str]]:
    if not directory.is_dir():
        return []
    tokens = [t.lower() for t in re.split(r"\s+", query.strip()) if t]
    if not tokens:
        return []

    matches: List[Tuple[int, str, str, str]] = []
    for md_file in sorted(directory.rglob("*.md")):
        txt = md_file.read_text(encoding="utf-8", errors="replace")
        # Split by H2 / H3 sections
        sections = re.split(r"(?m)^(?=##+ )", txt)
        for sec in sections:
            sec_low = sec.lower()
            score = sum(3 if f"[{t}]" in sec_low else (2 if t in sec.splitlines()[0].lower() else 1) for t in tokens if t in sec_low)
            matched_tokens = sum(1 for t in tokens if t in sec_low)
            if matched_tokens == len(tokens) or (len(tokens) > 1 and matched_tokens >= len(tokens) - 1 and score >= 2):
                heading = sec.splitlines()[0].strip() if sec.splitlines() else md_file.name
                matches.append((score, md_file.name, heading, sec.strip()[:3500]))

    matches.sort(key=lambda x: (-x[0], x[1]))
    return [
        {"file": fname, "heading": head, "excerpt": body}
        for _, fname, head, body in matches[:max_sections]
    ]


def fetch_official_doc(target: str, max_chars: int = 12000, allow_any_https: bool = False) -> Dict[str, str]:
    url = OFFICIAL_DOC_SHORTCUTS.get(target.strip().lower(), target.strip())
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"Only HTTPS documentation URLs are allowed (got `{url}`). See `kaizen-kit docs list` for shortcuts.")
    host = (parsed.hostname or "").lower()
    if not allow_any_https and not any(host == d or host.endswith("." + d) for d in ALLOWED_DOC_DOMAINS):
        raise ValueError(
            f"Domain `{host}` is outside curated official documentation allowlist {ALLOWED_DOC_DOMAINS}. "
            f"Pass `--allow-https` if querying another public specification host."
        )

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "KaizenEngineeringDocsReader/3.2 (+python-urllib)",
            "Accept": "text/html,text/markdown,text/plain;q=0.9,*/*;q=0.5",
        },
    )
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=12, context=ctx) as resp:
        ctype = resp.headers.get("Content-Type", "")
        raw_bytes = resp.read(1_500_000)
    decoded = raw_bytes.decode("utf-8", errors="replace")
    content = html_to_markdown(decoded) if "html" in ctype.lower() or "<html" in decoded[:500].lower() else decoded.strip()
    return {
        "url": url,
        "chars_total": str(len(content)),
        "markdown": content[:max_chars],
    }


def search_local_symbols(symbol: str, workspace: Path, max_hits: int = 15) -> List[Dict[str, str]]:
    """Genchi Genbutsu local symbol signature lookup across workspace + Cargo/NPM type definitions."""
    pat = re.compile(
        rf"\b(?:pub\s+(?:async\s+)?fn|pub\s+struct|pub\s+enum|pub\s+trait|interface|record|class)\s+{re.escape(symbol)}\b"
    )
    roots: List[Path] = [workspace.resolve()]
    cargo_src = Path.home() / ".cargo" / "registry" / "src"
    if cargo_src.is_dir():
        roots.append(cargo_src)
    nm = workspace.resolve() / "node_modules"
    if nm.is_dir():
        roots.append(nm)

    hits: List[Dict[str, str]] = []
    for r in roots:
        exts = (".d.ts",) if r.name == "node_modules" else (".rs", ".java", ".ts", ".d.ts")
        for fp in r.rglob("*"):
            if len(hits) >= max_hits:
                return hits
            if not fp.is_file() or not any(fp.name.endswith(e) for e in exts):
                continue
            if any(p in {".git", "target", "dist"} for p in fp.parts):
                continue
            try:
                txt = fp.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if symbol not in txt:
                continue
            for idx, line in enumerate(txt.splitlines(), start=1):
                if pat.search(line):
                    hits.append({"file": str(fp), "line": str(idx), "signature": line.strip()[:220]})
                    if len(hits) >= max_hits:
                        return hits
    return hits


def run_self_test() -> int:
    codices_dir = resolve_codices_dir()
    assert codices_dir.is_dir(), f"Missing codices directory: {codices_dir}"
    codex_files = list(codices_dir.glob("*.md"))
    assert len(codex_files) >= 7, f"Expected >= 7 Canonical Codices, found {len(codex_files)}"

    res_adp = search_markdown_corpus(codices_dir, "CA-ADP")
    assert res_adp, "Expected CA-ADP codex lookup hit"
    res_rg = search_markdown_corpus(codices_dir, "RG-RF37c Guard Clauses")
    assert res_rg, "Expected RG-RF37c Guard Clauses codex lookup hit"
    res_aws = search_markdown_corpus(codices_dir, "AWS-01 1,048,576")
    assert res_aws, "Expected AWS-01 ALB 1MB hard limit lookup hit"

    sample_html = "<html><head><script>evil()</script><style>body{}</style></head><body><nav>menu</nav><h1>Title</h1><p>Hello <code>Result&lt;T&gt;</code></p></body></html>"
    md = html_to_markdown(sample_html)
    assert "# Title" in md and "`Result<T>`" in md and "evil" not in md and "menu" not in md, f"Unexpected markdown: {md}"

    print(f"docs_reader v1.0 self-test: OK ({len(codex_files)} Canonical Codices verified)")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Kaizen 4-Channel Canonical Codex & Official Documentation Reader")
    sub = ap.add_subparsers(dest="subcmd", required=False)

    sub.add_parser("list", help="List local Canonical Codices and official documentation shortcuts")

    p_codex = sub.add_parser("codex", help="Search the 7 offline Canonical Engineering Codices (<5ms)")
    p_codex.add_argument("query", nargs="+", help="Citation ID (e.g. CC-G23, CA-ADP, RG-RF37c, AWS-01) or topic keywords")
    p_codex.add_argument("--json", action="store_true")

    p_fetch = sub.add_parser("fetch", help="Fetch and sanitize official documentation URL or shortcut into clean Markdown")
    p_fetch.add_argument("target", help="Shortcut name (see `list`) or HTTPS official docs URL")
    p_fetch.add_argument("--max-chars", type=int, default=12000)
    p_fetch.add_argument("--allow-https", action="store_true", help="Allow HTTPS specification hosts outside default list")

    p_sym = sub.add_parser("symbol", help="Verify exact local crate/SDK/workspace symbol signature (Genchi Genbutsu)")
    p_sym.add_argument("name", help="Struct, trait, fn, interface, record, or class name")
    p_sym.add_argument("--workspace", default=".")

    p_lib = sub.add_parser("library", help="Search optional gitignored personal library notes (~/.kaizen-kit/library)")
    p_lib.add_argument("query", nargs="+")

    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return run_self_test()

    if not args.subcmd or args.subcmd == "list":
        cdir = resolve_codices_dir()
        ldir = resolve_personal_library_dir()
        print("Canonical Offline Engineering Codices:")
        for cf in sorted(cdir.glob("*.md")):
            first_h1 = next((ln.lstrip("# ").strip() for ln in cf.read_text(encoding="utf-8").splitlines() if ln.startswith("# ")), cf.name)
            print(f"  - {cf.name:<44} : {first_h1}")
        print("\nOfficial Live Documentation Shortcuts (`kaizen-kit docs fetch <shortcut>`):")
        for k, v in sorted(OFFICIAL_DOC_SHORTCUTS.items()):
            print(f"  - {k:<24} -> {v}")
        print(f"\nOptional Personal Study Library (gitignored): {ldir}")
        return 0

    if args.subcmd == "codex":
        q = " ".join(args.query)
        hits = search_markdown_corpus(resolve_codices_dir(), q)
        if args.json:
            print(json.dumps(hits, indent=2))
        elif not hits:
            print(f"No codex section matched `{q}`. Run `kaizen-kit docs list` to browse codices.")
            return 1
        else:
            for h in hits:
                print(f"=== [{h['file']}] {h['heading']} ===\n{h['excerpt']}\n")
        return 0

    if args.subcmd == "fetch":
        try:
            res = fetch_official_doc(args.target, max_chars=args.max_chars, allow_any_https=args.allow_https)
        except Exception as exc:
            sys.stderr.write(f"[docs fetch error] {exc}\n")
            return 1
        print(f"# Source: {res['url']} (total={res['chars_total']} chars)\n\n{res['markdown']}")
        return 0

    if args.subcmd == "symbol":
        hits = search_local_symbols(args.name, Path(args.workspace))
        if not hits:
            print(f"No local definition found for `{args.name}`.")
            return 1
        for h in hits:
            print(f"{h['file']}:{h['line']} -> {h['signature']}")
        return 0

    if args.subcmd == "library":
        q = " ".join(args.query)
        hits = search_markdown_corpus(resolve_personal_library_dir(), q)
        if not hits:
            print(f"No personal library note matched `{q}` in {resolve_personal_library_dir()}.")
            return 1
        for h in hits:
            print(f"=== [{h['file']}] {h['heading']} ===\n{h['excerpt']}\n")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())

