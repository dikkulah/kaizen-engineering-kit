#!/usr/bin/env python3
"""
Test Suite: Milestone 3 Refactor Verification (Universal skills YAML frontmatter,
Teamwork `self` subagent preservation, AWS tenancy & git hook guard, and Antigravity/Claude skill sync).
"""

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from kaizen_kit import installer  # noqa: E402
from kaizen_kit.hooks import engine  # noqa: E402


class TestMilestone3Refactor(unittest.TestCase):
    def _assert_yaml_skill(self, sk: Path) -> None:
        txt = sk.read_text(encoding="utf-8")
        self.assertTrue(txt.startswith("---"), f"Missing YAML frontmatter in {sk}")
        parts = txt.split("---", 2)
        self.assertGreaterEqual(len(parts), 3, f"Malformed frontmatter in {sk}")
        data = yaml.safe_load(parts[1])
        self.assertIsInstance(data, dict, f"Frontmatter must be a mapping in {sk}")
        self.assertTrue(str(data.get("name", "")).strip(), f"Missing name in {sk}")
        self.assertTrue(str(data.get("description", "")).strip(), f"Missing description in {sk}")

    def test_all_asset_skills_have_valid_yaml_frontmatter(self) -> None:
        skills_dir = REPO_ROOT / "assets" / "skills"
        self.assertFalse((skills_dir / "wsl-env-memory-watchdog").exists())
        skill_files = sorted(skills_dir.glob("*/SKILL.md"))
        self.assertGreaterEqual(len(skill_files), 35)
        for sk in skill_files:
            self._assert_yaml_skill(sk)

    def test_pillars_reflect_claude_5_5(self) -> None:
        p4 = (REPO_ROOT / "assets" / "rules" / "pillars" / "04-bedrock-data-residency.md").read_text(encoding="utf-8")
        self.assertIn("Claude 5.5", p4)
        self.assertIn("8770", p4)

    def test_engine_preserves_teamwork_self_subagent(self) -> None:
        res = engine.evaluate_pre_tool(
            session_id="sess-m3",
            root_session_id="sess-m3",
            canonical_tool="invoke_subagent",
            args={"TypeName": "self", "Prompt": "Read .agents/teamwork/DISPATCH.md and build Rust module"},
        )
        self.assertTrue(res["allow"])
        upd = res["updated_args"] or {}
        self.assertEqual(upd.get("TypeName"), "self")
        self.assertIn("[ALWAYS-ON RECURSIVE SUBAGENT CONTRACT]", upd.get("Prompt", ""))

        res_succ = engine.evaluate_pre_tool(
            session_id="sess-m3",
            root_session_id="sess-m3",
            canonical_tool="invoke_subagent",
            args={"TypeName": "self", "Prompt": "Resume work at C:\\scratch\\handoff.md and update BRIEFING.md."},
        )
        self.assertTrue(res_succ["allow"])
        self.assertEqual((res_succ["updated_args"] or {}).get("TypeName"), "self")

    def test_engine_blocks_git_hook_bypass_and_aligns_aws_tenancy(self) -> None:
        git_cases = [
            ("git commit --no-verify -m msg", False),
            ('git commit -n -m "fix"', False),
            ("git.exe commit -n -m x", False),
            ("echo hi && git commit -n -m x", False),
            ("git commit -m 'hello;world' -n", False),
            ("git commit --message='a;b' -n", False),
            ("git commit -an -m x", False),
            ("git commit -nm x", False),
            ("git commit -am msg -n", False),
            ("git push --no-verify origin main", False),
            ("git -c core.hooksPath=/dev/null commit -m msg", False),
            ('git -c "core.hooksPath=/dev/null" commit -m msg', False),
            ("git config core.hooksPath /dev/null", False),
            ("git --config-env core.hooksPath=EV commit -m msg", False),
            ("git --config-env=core.hooksPath=EV commit -m msg", False),
            ("echo ok & git commit -n -m x", False),
            ("cmd /c git commit -n -m x", False),
            ('cmd.exe /c "git commit --no-verify -m x"', False),
            ('powershell -NoProfile -Command "git commit -n -m x"', False),
            ('bash -c "echo hi && git commit -n -m x"', False),
            ('git commit -m "fix"', True),
            ("git commit -mn", True),
            ('git commit -m "docs: explain why --no-verify is blocked"', True),
            ("git add -n .", True),
            ("git push -n origin main", True),
            ("cmd /c echo ok", True),
            ('bash -c "git commit -m ok"', True),
        ]
        for git_cmd, expected_allow in git_cases:
            res = engine.evaluate_pre_tool(
                session_id="sess-m3-git",
                root_session_id="sess-m3-git",
                canonical_tool="run_command",
                args={"CommandLine": git_cmd},
            )
            self.assertEqual(res["allow"], expected_allow, f"Unexpected engine result for: {git_cmd}")
            if not expected_allow:
                self.assertIn("Git Hook Guard", res["reason"])

        aws_cases = [
            ("aws lambda update-function-code --function-name fn --zip-file fileb://x.zip --profile telia", False),
            ("aws lambda update-function-code --function-name fn --zip-file fileb://x.zip --profile bytel", False),
            ("aws ec2 terminate-instances --instance-ids i-123 --profile production", False),
            ("aws lambda update-function-code --function-name fn --zip-file fileb://x.zip --profile kaizen-u2-prod", False),
            ("aws lambda update-function-code --function-name fn --zip-file fileb://x.zip --profile kaizen-u2x", False),
            ("aws lambda update-function-code --function-name fn --zip-file fileb://x.zip --profile=kaizen-u2-prod", False),
            ("AWS_PROFILE=kaizen-u2-prod aws lambda update-function-code --function-name fn --zip-file fileb://x.zip", False),
            ("aws lambda update-function-code --function-name fn --zip-file fileb://x.zip", False),
            ("AWS_PROFILE=kaizen-u2; aws lambda delete-function --function-name fn", False),
            ("AWS_PROFILE=kaizen-u2; aws lambda delete-function --function-name fn --profile production", False),
            ("aws lambda delete-function --function-name fn --profile production && echo AWS_PROFILE=kaizen-u2", False),
            ("aws lambda delete-function --function-name fn --profile production; echo --profile kaizen-u2", False),
            ("aws lambda update-function-code --function-name fn --zip-file fileb://x.zip --profile kaizen-u2 --profile production", False),
            ("export AWS_PROFILE=kaizen-u2; unset AWS_PROFILE; aws lambda delete-function --function-name fn", False),
            ("$env:AWS_PROFILE='kaizen-u2'; $env:AWS_PROFILE='production'; aws lambda delete-function --function-name fn", False),
            ("aws --profile production lambda delete-function --function-name fn", False),
            ("aws --region eu-west-1 --profile production s3 rm s3://b/k", False),
            ("aws --no-cli-pager --profile telia lambda update-function-code --function-name fn --zip-file fileb://x.zip", False),
            ("aws --output json ssm send-command --instance-ids i-123", False),
            ("aws ssm put-parameter --name /a --value b --profile production", False),
            ("aws ssm delete-parameter --name /a --profile production", False),
            ("aws ssm start-session --target i-123 --profile production", False),
            ("aws.exe lambda delete-function --function-name fn", False),
            ("aws.exe lambda delete-function --function-name fn --profile production", False),
            ('"C:\\Program Files\\Amazon\\AWSCLIV2\\aws.exe" lambda delete-function --function-name fn --profile production', False),
            ("AWS_PROFILE=kaizen-u2 & aws lambda delete-function --function-name fn", False),
            ("echo AWS_PROFILE=kaizen-u2 & aws lambda delete-function --function-name fn", False),
            ("echo --profile kaizen-u2 & aws lambda delete-function --function-name fn", False),
            ("cmd /c aws lambda delete-function --function-name fn --profile production", False),
            ('powershell -ExecutionPolicy Bypass -Command "aws lambda delete-function --function-name fn --profile production"', False),
            ("aws lambda update-function-code --function-name fn --zip-file fileb://x.zip --profile kaizen-u2", True),
            ("aws lambda update-function-code --function-name fn --zip-file fileb://x.zip --profile=kaizen-u2", True),
            ("AWS_PROFILE=kaizen-u2 aws lambda update-function-code --function-name fn --zip-file fileb://x.zip", True),
            ("aws lambda update-function-code --function-name fn --zip-file fileb://x.zip --profile kaizen-u2 && echo ok", True),
            ("export AWS_PROFILE=kaizen-u2; aws lambda update-function-code --function-name fn --zip-file fileb://x.zip", True),
            ("$env:AWS_PROFILE='kaizen-u2'; aws lambda update-function-code --function-name fn --zip-file fileb://x.zip", True),
            ("aws ec2 describe-instances --profile production", True),
            ("aws lambda get-function --function-name fn --profile production", True),
            ("aws ssm get-parameter --name /a --profile production", True),
            ("2>&1 aws lambda delete-function --function-name fn --profile kaizen-u2", True),
            ("& aws.exe lambda update-function-code --function-name fn --zip-file fileb://x.zip --profile kaizen-u2", True),
            ("aws --profile kaizen-u2 lambda update-function-code --function-name fn --zip-file fileb://x.zip", True),
            ("cmd /c echo ok", True),
        ]
        for cmd, expected_allow in aws_cases:
            res = engine.evaluate_pre_tool(
                session_id="sess-m3-aws",
                root_session_id="sess-m3-aws",
                canonical_tool="run_command",
                args={"CommandLine": cmd},
            )
            self.assertEqual(res["allow"], expected_allow, f"Unexpected engine result for: {cmd}")

    def test_install_antigravity_syncs_skills_and_domains(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td)
            agy_cfg = fake_home / ".gemini" / "config"
            (agy_cfg / "plugins" / "kaizen-optimizer" / "skills" / "kaizen-optimizer").mkdir(parents=True)
            (agy_cfg / "skills" / "wsl-env-memory-watchdog").mkdir(parents=True)

            res = installer.install_antigravity_target(fake_home)
            self.assertFalse(res.get("skipped", False))
            self.assertGreaterEqual(res["skills_domains_synced"], 3)
            self.assertFalse((agy_cfg / "skills" / "wsl-env-memory-watchdog").exists())
            self.assertTrue((agy_cfg / "plugins" / "agent-orchestrator" / "rules" / "domains" / "java-spring-loom.md").is_file())
            src_sha = hashlib.sha256((REPO_ROOT / "assets" / "skills" / "kaizen-optimizer" / "SKILL.md").read_bytes()).hexdigest()
            dst_sha = hashlib.sha256((agy_cfg / "plugins" / "kaizen-optimizer" / "skills" / "kaizen-optimizer" / "SKILL.md").read_bytes()).hexdigest()
            self.assertEqual(src_sha, dst_sha, "Parity mismatch for kaizen-optimizer/SKILL.md")

    def test_sync_claude_skills_assets_precedence_and_legacy_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td)
            stale_plugin = fake_home / ".gemini" / "config" / "plugins" / "kaizen-optimizer" / "skills" / "kaizen-optimizer"
            stale_plugin.mkdir(parents=True)
            (stale_plugin / "SKILL.md").write_text("---\nname: kaizen-optimizer\ndescription: stale\n---\nOld\n", encoding="utf-8")
            skills_dst = fake_home / ".claude" / "skills"
            (skills_dst / "wsl-env-memory-watchdog").mkdir(parents=True)
            (skills_dst / "wsl-env-memory-watchdog" / "SKILL.md").write_text("---\nname: wsl\ndescription: old\n---\n", encoding="utf-8")

            installed = installer._sync_claude_skills(REPO_ROOT / "assets", fake_home, skills_dst)
            self.assertIn("kaizen-optimizer", installed)
            self.assertFalse((skills_dst / "wsl-env-memory-watchdog").exists())
            src_sha = hashlib.sha256((REPO_ROOT / "assets" / "skills" / "kaizen-optimizer" / "SKILL.md").read_bytes()).hexdigest()
            dst_sha = hashlib.sha256((skills_dst / "kaizen-optimizer" / "SKILL.md").read_bytes()).hexdigest()
            self.assertEqual(src_sha, dst_sha)


if __name__ == "__main__":
    unittest.main()
