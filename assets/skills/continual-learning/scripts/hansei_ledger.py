#!/usr/bin/env python3
"""
Hansei & Yokoten Institutional Memory Engine (`hansei_ledger.py`).

Implements Toyota Production System Hansei (relentless self-reflection on failures),
Yokoten (horizontal deployment of lessons across all future sessions), and Goldratt
Fail-Forward milestone parking across Antigravity conversations.

Manages:
  - ~/.gemini/antigravity/kaizen_ledger.json (Structured Architectural & Milestone Ledger)
  - ~/.gemini/antigravity/DEAD_ENDS.md           (Human & Agent Readable Anti-Regression Vault)
  - ~/.gemini/antigravity/repo_locks/*.lock      (Active Cross-Chat Worktree Mutex Inspection)
"""

import argparse
import datetime
import json
import os
import sys
import time
from pathlib import Path

AGY_DIR = Path(os.path.expanduser("~/.gemini/antigravity"))
LEDGER_PATH = AGY_DIR / "kaizen_ledger.json"
DEAD_ENDS_PATH = AGY_DIR / "DEAD_ENDS.md"
REPO_LOCKS_DIR = AGY_DIR / "repo_locks"


def utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_ledger(path: Path = LEDGER_PATH) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "schema_version": "3.0",
        "updated_at": utc_now_iso(),
        "decisions": [],
        "dead_ends": [],
        "parked_milestones": [],
    }


def save_ledger(data: dict, path: Path = LEDGER_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = utc_now_iso()
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def sync_dead_ends_markdown(ledger: dict, md_path: Path = DEAD_ENDS_PATH) -> None:
    md_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Hansei & Yokoten Dead-Ends Vault (`DEAD_ENDS.md`)",
        "",
        f"> Auto-synced at `{ledger.get('updated_at', utc_now_iso())}` via `hansei_ledger.py`.",
        "> Every failure recorded here MUST correspond to a permanent automated guard (Poka-Yoke).",
        "",
        "| ID | Date | Symptom / Failure Mode | Root Cause | Permanent Automated Guard |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ]
    for de in ledger.get("dead_ends", []):
        lines.append(
            f"| `{de.get('id', 'DE-?')}` | `{de.get('date', '')[:10]}` | "
            f"{de.get('symptom', '')} | {de.get('root_cause', '')} | `{de.get('guard', '')}` |"
        )
    if ledger.get("parked_milestones"):
        lines.extend([
            "",
            "## Parked Milestones (3-Attempt Fail-Forward)",
            "| Milestone | Status | Checkpoint | Reason |",
            "| :--- | :--- | :--- | :--- |",
        ])
        for pm in ledger["parked_milestones"]:
            lines.append(
                f"| `{pm.get('id')}` | `{pm.get('status', 'BLOCKED')}` | "
                f"`{pm.get('checkpoint', 'HEAD')}` | {pm.get('reason', '')} |"
            )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def active_repo_locks(locks_dir: Path = REPO_LOCKS_DIR) -> list[dict]:
    active = []
    if not locks_dir.exists():
        return active
    now = time.time()
    for f in sorted(locks_dir.glob("*.lock")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            age_s = int(now - float(d.get("time", 0)))
            if age_s < 1800:
                active.append({
                    "repo": d.get("repo", f.stem),
                    "root_cid": str(d.get("root_cid") or d.get("cid") or "")[:8],
                    "age_min": age_s // 60,
                })
        except Exception:
            continue
    return active


def build_session_brief(current_cid: str = "", ledger_path: Path = LEDGER_PATH) -> str:
    """Compact (<220 tokens) Yokoten startup briefing injected once at SessionStart."""
    ledger = load_ledger(ledger_path)
    locks = active_repo_locks()
    decisions = ledger.get("decisions", [])[-5:]
    dead_ends = ledger.get("dead_ends", [])[-5:]
    parked = [m for m in ledger.get("parked_milestones", []) if m.get("status") == "BLOCKED"]

    parts = ["[Kaizen v3.0 SessionStart Briefing — Yokoten Memory]"]
    if locks:
        lock_str = ", ".join(f"{lk['repo']} (locked by {lk['root_cid']}, {lk['age_min']}m ago)" for lk in locks)
        parts.append(f"- ACTIVE WORKTREE LOCKS: {lock_str}")
    if decisions:
        dec_str = " | ".join(f"{d['id']}: {d['title']}" for d in decisions)
        parts.append(f"- LOCKED DECISIONS: {dec_str}")
    if dead_ends:
        de_str = " | ".join(f"{d['id']}: {d['symptom']}" for d in dead_ends)
        parts.append(f"- NEVER REPEAT (DEAD ENDS): {de_str}")
    if parked:
        pk_str = ", ".join(f"{p['id']}@{p.get('checkpoint', 'HEAD')}" for p in parked)
        parts.append(f"- PARKED MILESTONES: {pk_str}")
    parts.append("- FULL DETAILS: Read `~/.gemini/antigravity/kaizen_ledger.json` or `~/.gemini/antigravity/DEAD_ENDS.md` if touching locked areas.")
    return "\n".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description="Hansei & Yokoten Institutional Memory CLI")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("status", help="Show ledger summary and active worktree locks")
    p_brief = sub.add_parser("session-brief", help="Emit compact SessionStart briefing")
    p_brief.add_argument("--cid", default="", help="Current conversation ID")

    p_dec = sub.add_parser("record-decision", help="Record an architectural decision (ADR)")
    p_dec.add_argument("--id", required=True)
    p_dec.add_argument("--title", required=True)
    p_dec.add_argument("--summary", required=True)
    p_dec.add_argument("--scope", default="global")

    p_de = sub.add_parser("record-dead-end", help="Record a Hansei failure & permanent guard")
    p_de.add_argument("--id", required=True)
    p_de.add_argument("--symptom", required=True)
    p_de.add_argument("--root-cause", required=True)
    p_de.add_argument("--guard", required=True)

    p_pm = sub.add_parser("park-milestone", help="Park a blocked milestone (3-Attempt Fail-Forward)")
    p_pm.add_argument("--id", required=True)
    p_pm.add_argument("--reason", required=True)
    p_pm.add_argument("--checkpoint", default="HEAD")

    sub.add_parser("self-test", help="Run isolated self-test")

    args = ap.parse_args()
    if args.cmd == "self-test":
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            lp = Path(td) / "ledger.json"
            mp = Path(td) / "DEAD_ENDS.md"
            data = load_ledger(lp)
            data["decisions"].append({"id": "ADR-TEST", "title": "OnceLock L1 Cache", "summary": "Singleton orchestrator", "scope": "ondemand-telemetry-engine", "date": utc_now_iso()})
            data["dead_ends"].append({"id": "DE-TEST", "symptom": "0% L1 hit", "root_cause": "Per-request new()", "guard": "story_guard Gate 4", "date": utc_now_iso()})
            save_ledger(data, lp)
            sync_dead_ends_markdown(data, mp)
            brief = build_session_brief("testcid", lp)
            assert "ADR-TEST" in brief and "DE-TEST" in brief and mp.exists()
        print("hansei_ledger self-test: OK")
        return 0

    ledger = load_ledger()
    if args.cmd == "record-decision":
        ledger["decisions"] = [d for d in ledger["decisions"] if d.get("id") != args.id]
        ledger["decisions"].append({
            "id": args.id,
            "date": utc_now_iso(),
            "scope": args.scope,
            "title": args.title,
            "summary": args.summary,
        })
        save_ledger(ledger)
        print(f"Recorded decision {args.id}")
        return 0

    if args.cmd == "record-dead-end":
        ledger["dead_ends"] = [d for d in ledger["dead_ends"] if d.get("id") != args.id]
        ledger["dead_ends"].append({
            "id": args.id,
            "date": utc_now_iso(),
            "symptom": args.symptom,
            "root_cause": args.root_cause,
            "guard": args.guard,
        })
        save_ledger(ledger)
        sync_dead_ends_markdown(ledger)
        print(f"Recorded dead-end {args.id} and synced DEAD_ENDS.md")
        return 0

    if args.cmd == "park-milestone":
        ledger["parked_milestones"] = [m for m in ledger["parked_milestones"] if m.get("id") != args.id]
        ledger["parked_milestones"].append({
            "id": args.id,
            "date": utc_now_iso(),
            "status": "BLOCKED",
            "checkpoint": args.checkpoint,
            "reason": args.reason,
        })
        save_ledger(ledger)
        sync_dead_ends_markdown(ledger)
        print(f"Parked milestone {args.id} at {args.checkpoint}")
        return 0

    if args.cmd == "session-brief":
        print(build_session_brief(args.cid))
        return 0

    sync_dead_ends_markdown(ledger)
    print(json.dumps({
        "updated_at": ledger.get("updated_at"),
        "decisions_count": len(ledger.get("decisions", [])),
        "dead_ends_count": len(ledger.get("dead_ends", [])),
        "parked_milestones_count": len(ledger.get("parked_milestones", [])),
        "active_repo_locks": active_repo_locks(),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
