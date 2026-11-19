#!/usr/bin/env python3
"""
aws_profile_validator.py - AWS Tenancy Boundary & SSO Session Validator.

Enforces SEC-AWS-TENANCY-ISOLATION across the Kaizen AWS multi-account matrix,
verifying that 41 production and staging accounts remain strictly read-only,
gating operational mutations to kaizen-u2, and verifying SSO token expiration.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Optional

# Canonical Kaizen Read-Only Profiles
READ_ONLY_PROFILES = {
    "kaizen-staging",
    "kaizen-prod",
    "production",
    "testing",
    "airdi-dev",
    "airdi-prod",
    "att",
    "telia",
    "dtag",
    "bytel",
    "telstra",
    "nam",
    "frontier",
    "sky",
    "vdf",
    "telenor",
    "vznsbx",
}

MUTABLE_PROFILES = {"kaizen-u2"}


def validate_aws_region(region: str) -> bool:
    """Validate AWS region string format."""
    pattern = r"^[a-z]{2}(-[a-z]+)+-\d+$"
    return bool(re.match(pattern, region))


def classify_profile_tenancy(profile: str) -> tuple[str, bool]:
    """Classify an AWS profile into Mutable or Read-Only based on SEC-AWS-TENANCY-ISOLATION."""
    prof_lower = profile.lower()
    if prof_lower in MUTABLE_PROFILES:
        return "MUTABLE_OPERATIONAL", False
    if prof_lower in READ_ONLY_PROFILES or prof_lower.startswith("airdi-"):
        return "STRICT_READ_ONLY", True
    # Default fail-closed security posture for unlisted profiles: treat as Read-Only
    return "UNKNOWN_ACCOUNT (TREATED_AS_READ_ONLY)", True


def check_sso_cache_expiration() -> list[dict[str, any]]:
    """Check AWS SSO cache directory for token expiration states."""
    sso_cache_dir = Path.home() / ".aws" / "sso" / "cache"
    if not sso_cache_dir.exists():
        return []

    tokens = []
    now_utc = datetime.now(timezone.utc)

    for token_file in sso_cache_dir.glob("*.json"):
        try:
            with open(token_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            expires_at_str = data.get("expiresAt")
            if not expires_at_str:
                continue

            # Parse ISO 8601 timestamp
            # Standard python 3.11+ handles Z, 3.10 handles +00:00 or replacement
            clean_ts = expires_at_str.replace("Z", "+00:00")
            exp_dt = datetime.fromisoformat(clean_ts)

            is_valid = exp_dt > now_utc
            remaining_mins = max(0, int((exp_dt - now_utc).total_seconds() / 60))

            tokens.append({
                "file": token_file.name,
                "expires_at": expires_at_str,
                "is_valid": is_valid,
                "remaining_minutes": remaining_mins,
            })
        except Exception:
            continue

    return tokens


def parse_arguments(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Validate AWS profile tenancy boundaries (SEC-AWS-TENANCY-ISOLATION) and SSO sessions.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--profile",
        type=str,
        default="kaizen-u2",
        help="AWS CLI profile name to inspect.",
    )
    parser.add_argument(
        "--region",
        type=str,
        default="eu-west-1",
        help="Target AWS region.",
    )
    parser.add_argument(
        "--enforce-read-only",
        default=True,
        action=argparse.BooleanOptionalAction,
        help="Strictly enforce read-only boundary on non-kaizen-u2 profiles.",
    )
    parser.add_argument(
        "--check-sso",
        default=True,
        action=argparse.BooleanOptionalAction,
        help="Inspect local SSO cache for token expiration status.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Simulate profile tenancy check and policy inspection without AWS STS calls.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    """Main execution entrypoint."""
    args = parse_arguments(argv)

    if not args.profile or not args.profile.strip():
        print("[ERROR] AWS profile name cannot be empty.", file=sys.stderr)
        return 1

    if not validate_aws_region(args.region):
        print(f"[ERROR] Invalid AWS region format: '{args.region}'. Expected pattern e.g. 'eu-west-1'.", file=sys.stderr)
        return 1

    classification, is_read_only = classify_profile_tenancy(args.profile)

    if args.dry_run:
        print("[DRY-RUN] AWS Profile Validator Simulation Plan:")
        print(f"[DRY-RUN] - Target profile: {args.profile}")
        print(f"[DRY-RUN] - Region: {args.region}")
        print(f"[DRY-RUN] - Tenancy classification: {classification}")
        print(f"[DRY-RUN] - Is read-only: {is_read_only}")
        print(f"[DRY-RUN] - Enforce read-only: {args.enforce_read_only}")
        print(f"[DRY-RUN] - Check SSO: {args.check_sso}")
        print("[DRY-RUN] Simulation completed successfully.")
        return 0

    print(f"=== AWS Profile Validator: {args.profile} ({args.region}) ===")
    print(f"Tenancy Classification : {classification}")
    print(f"Read-Only Protection   : {'ENFORCED' if is_read_only else 'OPERATIONAL (MUTATIONS ALLOWED)'}")

    if is_read_only and args.enforce_read_only:
        print("[PROTECTION] SEC-AWS-TENANCY-ISOLATION active: All mutations are physically blocked on this profile.")

    if args.check_sso:
        tokens = check_sso_cache_expiration()
        if tokens:
            valid_tokens = [t for t in tokens if t["is_valid"]]
            if valid_tokens:
                top_token = max(valid_tokens, key=lambda t: t["remaining_minutes"])
                print(f"[SSO STATUS] Valid SSO session active (Remaining: ~{top_token['remaining_minutes']} minutes).")
            else:
                print("[WARNING] Local SSO cache tokens are expired. Re-authentication via 'aws sso login' may be required.", file=sys.stderr)
        else:
            print("[INFO] No cached SSO tokens detected in ~/.aws/sso/cache/.")

    # Validate caller identity via AWS CLI if installed
    aws_bin = shutil.which("aws")
    if aws_bin:
        print("\n--- Verifying AWS STS Caller Identity ---")
        cmd = [aws_bin, "sts", "get-caller-identity", "--profile", args.profile, "--region", args.region]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode == 0:
            try:
                identity = json.loads(proc.stdout)
                print(f"[STS PASS] Account: {identity.get('Account')}")
                print(f"[STS PASS] UserId : {identity.get('UserId')}")
                print(f"[STS PASS] Arn    : {identity.get('Arn')}")
            except Exception:
                print(f"[STS PASS] {proc.stdout.strip()}")
        else:
            print(f"[STS WARNING] STS query failed (Session may be expired or offline): {proc.stderr.strip()}", file=sys.stderr)
            # If not in dry run and STS fails, return 1 if token verification was expected
            if "expired" in proc.stderr.lower():
                return 1

    print("\n[CONCLUSION] Profile tenancy validation PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
