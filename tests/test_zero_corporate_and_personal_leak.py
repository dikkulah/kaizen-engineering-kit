#!/usr/bin/env python3
"""
Zero Corporate & Personal Leak Gate (`test_zero_corporate_and_personal_leak.py`).

Scans all tracked text/source/config/asset files across `kaizen-engineering-kit`
(`.py`, `.md`, `.mdc`, `.json`, `.toml`, `.yaml`, `.yml`, `.sh`, `.ps1`, `.cjs`, `.rs`, `.tf`)
and asserts:
  1. Zero corporate product, service, account, instance, or profile identifiers.
  2. Zero personal usernames, local machine hostnames, or developer names.
  3. Zero UTF-8 BOMs (`\\ufeff`) and zero raw credentials/private keys.
"""

from __future__ import annotations

from pathlib import Path
import re
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".venv",
    ".venv-radar",
    "target",
    "dist",
    "build",
}

SCANNED_SUFFIXES = (
    ".py",
    ".md",
    ".mdc",
    ".json",
    ".toml",
    ".yaml",
    ".yml",
    ".sh",
    ".ps1",
    ".cjs",
    ".rs",
    ".tf",
)

# Assembled dynamically so this test file itself contains zero literal forbidden tokens.
FORBIDDEN_SUBSTRINGS = (
    # Corporate brand, products, internal services & infra identifiers
    "air" + "ties",
    "care" + "view",
    "aca" + "pi",
    "eua" + "pi",
    "wu" + "xs",
    "rm" + "ng",
    "view" + "service",
    "easy" + "mesh",
    "864617" + "344666",
    "054081" + "389041",
    "i-0bf67a" + "7d3e7fb0428",
    "unified" + "-u2",
    "unified" + "-ucprd",
    "unified" + "-uca",
    "wifi" + "-doctor",
    "cloud" + "rain",
    "telemetry" + "_lsp",
    "lora-rust-home" + "cube-parity",
    "lora-vue-care" + "view-ui",
    "lora-uc-rca" + "-triage",
    "lora-openapi-ac" + "dc-guard",
    # Personal & local machine footprint identifiers
    "dikku" + "lah",
    "uf" + "uk",
    "mac" + "mini",
    "188" + "90",
    "SPOOFED_" + "USER_AGENT",
    "5d72f3d8" + "-6e21",
    "1122" + "ae50",
    "3ec6" + "1ef8",
    "erdin" + "ç",
    "erdin" + "c",
    "başp" + "ınar",
    "kaşık" + "çı",
    "altun" + "dağ",
    "pulaset" + "ty",
)

FORBIDDEN_WORD_PATTERNS = (
    ("et" + "is", re.compile(r"\b" + "et" + "is" + r"\b", re.IGNORECASE)),
)

RAW_SECRET_PATTERNS = (
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA)(?!IOSFODNN7EXAMPLE)[0-9A-Z]{16}\b")),
    ("acc_sk", re.compile(r"acc-sk-[0-9a-fA-F]{16,}")),
    ("gitlab_pat", re.compile(r"glpat-(?!00000000)[0-9a-zA-Z_-]{16,}")),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----\s+[A-Za-z0-9+/=]{32,}")),
)


class TestZeroCorporateAndPersonalLeak(unittest.TestCase):
    def test_zero_corporate_and_personal_leaks_across_repo(self) -> None:
        violations: list[str] = []
        scanned = 0

        for path in sorted(REPO_ROOT.rglob("*")):
            if any(part in SKIP_DIRS for part in path.parts) or not path.is_file():
                continue
            if path.suffix.lower() not in SCANNED_SUFFIXES:
                continue

            scanned += 1
            raw = path.read_text(encoding="utf-8", errors="replace")
            rel = path.relative_to(REPO_ROOT).as_posix()

            if raw.startswith("\ufeff"):
                violations.append(f"{rel}: UTF-8 BOM (\\ufeff) at byte 0")

            allowed_gh_slug = "github.com/" + "dikku" + "lah/kaizen-engineering-kit"
            lowered = raw.lower().replace(allowed_gh_slug, "github.com/oss/kaizen-engineering-kit")
            for token in FORBIDDEN_SUBSTRINGS:
                if token.lower() in lowered:
                    violations.append(f"{rel}: contains forbidden corporate/personal token '{token}'")

            for label, wrx in FORBIDDEN_WORD_PATTERNS:
                if wrx.search(raw):
                    violations.append(f"{rel}: contains forbidden corporate word '{label}'")

            for label, srx in RAW_SECRET_PATTERNS:
                match = srx.search(raw)
                if match:
                    violations.append(f"{rel}: contains raw credential '{label}' ({match.group(0)[:10]}...)")

        self.assertGreaterEqual(scanned, 250, f"Expected >= 250 repository files scanned, got {scanned}")
        self.assertEqual(
            violations,
            [],
            "Corporate/personal leak violations detected:\n  " + "\n  ".join(violations[:50]),
        )


if __name__ == "__main__":
    unittest.main()
