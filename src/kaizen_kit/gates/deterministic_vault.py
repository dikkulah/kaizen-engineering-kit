#!/usr/bin/env python3
"""
Deterministic Pseudonymization Vault & Secret Detector (`deterministic_vault.py`).

Enforces format-preserving, relational-integrity-preserving pseudonymization:
- Maps real entities (MACs, IPs, SSIDs, Customer IDs) to deterministic pseudonyms (X -> Y).
- Preserves 100% relational integrity across logs and captures.
- Exempts `kaizen-u2` and `kaizen-staging` staging/testbed identifiers.
- Detects raw AWS keys, private keys, JWT tokens, and credential files for hard-blocking.
"""

import json
import re
import threading
from pathlib import Path
from typing import Dict, Optional, Tuple

from kaizen_kit.paths import resolve_vault_path

MAC_REGEX = re.compile(r"\b([0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5})\b")
IPV4_REGEX = re.compile(
    r"\b((?!127\.0\.0\.1|0\.0\.0\.0|255\.255\.255\.255)"
    r"(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}"
    r"(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?))\b"
)

AWS_ACCESS_KEY_REGEX = re.compile(r"\b((?:AKIA|ASIA)[0-9A-Z]{16})\b")
AWS_SECRET_KEY_REGEX = re.compile(
    r"(?i)(?:aws_secret_access_key|secret_key|secretaccesskey)[\s:='\"]+([0-9a-zA-Z/+]{40})"
)
PRIVATE_KEY_REGEX = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
JWT_TOKEN_REGEX = re.compile(r"\beyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\b")

SENSITIVE_FILES_REGEX = re.compile(
    r"(?i)(?:[\\/]\.aws[\\/](?:credentials|config)|\.env(?:\.[a-z0-9_-]+)?|[\\/]id_rsa(?:\.pub)?|[\\/]settings\.xml|\.dockercfg|[\\/]\.netrc)"
)
DANGEROUS_COMMANDS_REGEX = re.compile(
    r"(?i)(?:\bprintenv\b|Get-ChildItem\s+env:|Get-Item\s+env:|dir\s+env:|\bexport\s+-p\b|aws\s+configure\s+(?:get|list|export))"
)
U2_UCA_EXEMPT_REGEX = re.compile(
    r"(?i)\b(?:u2|uca|kaizen-u2|kaizen-staging|u2_all_parity|u2-parity|telemetry_dashboard-uca|staging)\b"
)


def is_u2_or_uca_exempt(text_or_path: str = "") -> bool:
    if not text_or_path:
        return False
    return bool(U2_UCA_EXEMPT_REGEX.search(text_or_path))


class DeterministicVault:
    """Thread-safe persistent vault for relational-preserving pseudonymization."""

    def __init__(self, vault_path: Optional[Path] = None, auto_save: bool = True):
        self.vault_path = vault_path or resolve_vault_path()
        self.auto_save = auto_save
        self._dirty = False
        self._lock = threading.Lock()
        self._forward: Dict[str, Dict[str, str]] = {"mac": {}, "ipv4": {}, "ssid": {}, "customer_id": {}}
        self._reverse: Dict[str, Dict[str, str]] = {"mac": {}, "ipv4": {}, "ssid": {}, "customer_id": {}}
        self._counters: Dict[str, int] = {"mac": 0, "ipv4": 0, "ssid": 0, "customer_id": 0}
        self._load()

    def _load(self) -> None:
        try:
            if self.vault_path.exists():
                data = json.loads(self.vault_path.read_text(encoding="utf-8"))
                self._forward = data.get("forward", self._forward)
                self._reverse = data.get("reverse", self._reverse)
                self._counters = data.get("counters", self._counters)
        except Exception:
            pass

    def _save(self) -> None:
        try:
            self.vault_path.parent.mkdir(parents=True, exist_ok=True)
            temp_file = self.vault_path.with_suffix(".tmp")
            temp_file.write_text(
                json.dumps(
                    {"forward": self._forward, "reverse": self._reverse, "counters": self._counters},
                    indent=2,
                ),
                encoding="utf-8",
            )
            temp_file.replace(self.vault_path)
        except Exception:
            pass

    def flush(self) -> None:
        with self._lock:
            if self._dirty:
                self._save()
                self._dirty = False

    def get_or_create_mac(self, real_mac: str) -> str:
        normalized = real_mac.upper().replace("-", ":")
        with self._lock:
            if normalized in self._forward["mac"]:
                return self._forward["mac"][normalized]
            self._counters["mac"] += 1
            idx = self._counters["mac"]
            pseudo = f"00:50:C2:{(idx >> 16) & 0xFF:02X}:{(idx >> 8) & 0xFF:02X}:{idx & 0xFF:02X}"
            self._forward["mac"][normalized] = pseudo
            self._reverse["mac"][pseudo] = normalized
            self._dirty = True
            if self.auto_save:
                self._save()
                self._dirty = False
            return pseudo

    def get_or_create_ipv4(self, real_ip: str) -> str:
        with self._lock:
            if real_ip in self._forward["ipv4"]:
                return self._forward["ipv4"][real_ip]
            self._counters["ipv4"] += 1
            idx = self._counters["ipv4"]
            pseudo = f"198.51.100.{(idx % 250) + 1}"
            self._forward["ipv4"][real_ip] = pseudo
            self._reverse["ipv4"][pseudo] = real_ip
            self._dirty = True
            if self.auto_save:
                self._save()
                self._dirty = False
            return pseudo

    def anonymize_text(self, text: str, scope: str = "") -> str:
        if not text or is_u2_or_uca_exempt(scope) or is_u2_or_uca_exempt(text):
            return text

        def replace_mac(match: re.Match) -> str:
            raw_mac = match.group(1)
            if raw_mac.upper() in ("FF:FF:FF:FF:FF:FF", "00:00:00:00:00:00"):
                return raw_mac
            return self.get_or_create_mac(raw_mac)

        def replace_ip(match: re.Match) -> str:
            raw_ip = match.group(1)
            if raw_ip.startswith("127.") or raw_ip == "0.0.0.0":
                return raw_ip
            return self.get_or_create_ipv4(raw_ip)

        text = MAC_REGEX.sub(replace_mac, text)
        return IPV4_REGEX.sub(replace_ip, text)

    def deanonymize_text(self, text: str) -> str:
        if not text:
            return text
        with self._lock:
            for pseudo, real in self._reverse["mac"].items():
                text = text.replace(pseudo, real)
            for pseudo, real in self._reverse["ipv4"].items():
                text = text.replace(pseudo, real)
        return text


def check_for_secrets(text: str) -> Optional[Tuple[str, str]]:
    if not text:
        return None
    m_aws = AWS_ACCESS_KEY_REGEX.search(text)
    if m_aws:
        return ("AWS_ACCESS_KEY", m_aws.group(1)[:8] + "...")
    m_sec = AWS_SECRET_KEY_REGEX.search(text)
    if m_sec:
        return ("AWS_SECRET_KEY", "[REDACTED_40_CHAR_SECRET]")
    m_pk = PRIVATE_KEY_REGEX.search(text)
    if m_pk:
        return ("PRIVATE_KEY", "-----BEGIN ... PRIVATE KEY-----")
    m_jwt = JWT_TOKEN_REGEX.search(text)
    if m_jwt:
        return ("JWT_TOKEN", m_jwt.group(0)[:15] + "...")
    return None


def is_sensitive_file_path(path: str) -> bool:
    if not path:
        return False
    return bool(SENSITIVE_FILES_REGEX.search(path.replace("\\", "/")))


def is_dangerous_command(command: str) -> bool:
    if not command:
        return False
    return bool(DANGEROUS_COMMANDS_REGEX.search(command))
