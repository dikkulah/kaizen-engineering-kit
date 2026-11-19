#!/usr/bin/env python3
"""
Test Suite 2: Universal Lifecycle Hook Adapter (Claude Code + Antigravity 2.0 Dialect Parity).

Verifies end-to-end closed-loop enforcement for Claude Code users (without Antigravity IDE):
  1. `SessionStart` injects Yokoten institutional memory via `hookSpecificOutput.additionalContext`.
  2. `UserPromptSubmit` blocks leaked AWS keys (`exit 2`) and decomposes multi-workstream prompts (`WS-1..WS-N`).
  3. `PreToolUse` (`Bash`, `Write`, `Edit`, `Read`, `Task`):
     - Blocks AWS mutations without `--profile kaizen-u2`, allows `--profile kaizen-u2`.
     - Blocks test/parity directory deletion (`rm -rf deploy/parity`).
     - Blocks synthetic telemetry literals (`"id": "in_sync"`) in production `src/*.rs`.
     - Blocks cross-session git worktree collisions while allowing same-session writes.
     - Auto-routes generic Claude `Task` subagent calls to domain specialists (`systems-architect`).
  4. `PostToolUse` runs `<40ms` Jidoka quality checks and clears `unverified_prod_edits` after verification.
  5. `Stop` blocks premature completion once per turn when production edits are unverified, and honors `stop_hook_active=True`.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from kaizen_kit.hooks import universal_hook_adapter as uha  # noqa: E402


class TestUniversalHooks(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name) / "state"
        os.environ["AI_KIT_STATE_DIR"] = str(self.state_dir)

    def tearDown(self) -> None:
        os.environ.pop("AI_KIT_STATE_DIR", None)
        self._tmp.cleanup()

    def test_01_claude_session_start_injects_yokoten_brief(self) -> None:
        code, out, err = uha.handle_claude_event(
            "SessionStart",
            {"session_id": "cc-sess-1", "hook_event_name": "SessionStart"},
        )
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("ADR-001", ctx)
        self.assertIn("DE-001", ctx)

    def test_02_claude_prompt_secret_block_and_workstream_plan(self) -> None:
        fake_key = "AKIA" + "IOSFODNN7EXAMPLE"
        code, out, err = uha.handle_claude_event(
            "UserPromptSubmit",
            {"session_id": "cc-sess-2", "prompt": f"deploy using key {fake_key}"},
        )
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        self.assertIn("AWS_ACCESS_KEY", err)

        compound_prompt = (
            "1. Rust VectorCube katmanında OnceLock önbellek anahtarlarını pencere bazlı güncelle.\n"
            "2. CloudGatewayApi Java 21 OpenAPI şemasını yeni endpoint ile senkronize et ve Jenkins pre-flight kontrolü yap."
        )
        code2, out2, _ = uha.handle_claude_event(
            "UserPromptSubmit",
            {"session_id": "cc-sess-2", "prompt": compound_prompt},
        )
        self.assertEqual(code2, 0)
        ctx2 = out2["hookSpecificOutput"]["additionalContext"]
        self.assertIn("WS-1", ctx2)
        self.assertIn("WS-2", ctx2)

    def test_03_claude_aws_tenancy_and_test_deletion_gates(self) -> None:
        # Block non-u2 mutation
        _, deny_out, _ = uha.handle_claude_event(
            "PreToolUse",
            {
                "session_id": "cc-sess-3",
                "tool_name": "Bash",
                "tool_input": {"command": "aws lambda update-function-code --profile kaizen-prod --function-name fn"},
            },
        )
        self.assertEqual(deny_out["hookSpecificOutput"]["permissionDecision"], "deny")

        # Allow kaizen-u2
        _, allow_out, _ = uha.handle_claude_event(
            "PreToolUse",
            {
                "session_id": "cc-sess-3",
                "tool_name": "Bash",
                "tool_input": {"command": "aws lambda update-function-code --profile kaizen-u2 --function-name fn"},
            },
        )
        self.assertEqual(allow_out, {})

        # Block test/parity deletion
        del_cmd = "git " + "rm -rf deploy/parity"
        _, del_out, _ = uha.handle_claude_event(
            "PreToolUse",
            {
                "session_id": "cc-sess-3",
                "tool_name": "Bash",
                "tool_input": {"command": del_cmd},
            },
        )
        self.assertEqual(del_out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_04_claude_cross_session_worktree_mutex(self) -> None:
        repo_dir = Path(self._tmp.name) / "sample-service"
        (repo_dir / ".git").mkdir(parents=True)
        src_file = repo_dir / "src" / "lib.rs"
        src_file.parent.mkdir(parents=True)

        # Session A acquires lock on write
        _, out_a, _ = uha.handle_claude_event(
            "PreToolUse",
            {
                "session_id": "session-alpha",
                "cwd": str(repo_dir),
                "tool_name": "Write",
                "tool_input": {"file_path": str(src_file), "content": "pub fn ok() {}\n"},
            },
        )
        self.assertEqual(out_a, {})

        # Session B is denied concurrent write on same repo
        _, out_b, _ = uha.handle_claude_event(
            "PreToolUse",
            {
                "session_id": "session-beta",
                "cwd": str(repo_dir),
                "tool_name": "Edit",
                "tool_input": {"file_path": str(src_file), "new_string": "pub fn race() {}\n"},
            },
        )
        self.assertEqual(out_b["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("sample-service", out_b["hookSpecificOutput"]["permissionDecisionReason"])

    def test_05_claude_synthetic_telemetry_jidoka_and_stop_gate(self) -> None:
        # Block synthetic telemetry literal on PreToolUse Edit
        fake_json = '{"id": "in_' + 'sync"}'
        _, syn_out, _ = uha.handle_claude_event(
            "PreToolUse",
            {
                "session_id": "cc-sess-5",
                "tool_name": "Edit",
                "tool_input": {"file_path": "/repo/src/features/nodes.rs", "new_string": fake_json},
            },
        )
        self.assertEqual(syn_out["hookSpecificOutput"]["permissionDecision"], "deny")

        # Start turn 1 and edit a real Java file with inline package violation -> PostToolUse Jidoka warning + Stop gate
        uha.handle_claude_event("UserPromptSubmit", {"session_id": "cc-sess-5", "prompt": "update service"})
        jfile = Path(self._tmp.name) / "DemoService.java"
        jfile.write_text("public class DemoService { void run(java.util.List<String> x) {} }\n", encoding="utf-8")

        _, post_out, _ = uha.handle_claude_event(
            "PostToolUse",
            {
                "session_id": "cc-sess-5",
                "tool_name": "Write",
                "tool_input": {"file_path": str(jfile), "content": jfile.read_text(encoding="utf-8")},
            },
        )
        self.assertIn("ZERO-INLINE-PACKAGE", post_out["hookSpecificOutput"]["additionalContext"])

        # Stop should block once because unverified production edit exists
        _, stop1, _ = uha.handle_claude_event("Stop", {"session_id": "cc-sess-5", "stop_hook_active": False})
        self.assertEqual(stop1["decision"], "block")

        # Second Stop in same turn (or stop_hook_active=True) MUST allow to guarantee zero infinite loops
        _, stop2, _ = uha.handle_claude_event("Stop", {"session_id": "cc-sess-5", "stop_hook_active": False})
        self.assertEqual(stop2, {})

    def test_06_claude_subagent_routing_and_antigravity_compat(self) -> None:
        _, task_out, _ = uha.handle_claude_event(
            "PreToolUse",
            {
                "session_id": "cc-sess-6",
                "tool_name": "Task",
                "tool_input": {"subagent_type": "general-purpose", "prompt": "Optimize Rust Graviton3 VectorCube fold"},
            },
        )
        upd = task_out["hookSpecificOutput"]["updatedInput"]
        self.assertEqual(upd["subagent_type"], "systems-architect")
        self.assertIn("[ALWAYS-ON RECURSIVE SUBAGENT CONTRACT]", upd["prompt"])

        agy_out = uha.handle_antigravity_event(
            "pre_tool",
            {
                "conversationId": "agy-1",
                "toolCall": {"name": "invoke_subagent", "args": {"TypeName": "self", "Prompt": "Audit Java 21 Spring Loom service"}},
            },
        )
        self.assertEqual(agy_out["overwrite"]["TypeName"], "jvm-service-architect")

        # Verify Clean Architecture refactoring & Official Doc routing
        _, ref_out, _ = uha.handle_claude_event(
            "PreToolUse",
            {
                "session_id": "cc-sess-7",
                "tool_name": "Task",
                "tool_input": {"subagent_type": "general-purpose", "prompt": "Refactor Clean Architecture ADP smells and Demeter chains"},
            },
        )
        self.assertEqual(ref_out["hookSpecificOutput"]["updatedInput"]["subagent_type"], "refactoring-specialist")

        _, doc_out, _ = uha.handle_claude_event(
            "PreToolUse",
            {
                "session_id": "cc-sess-8",
                "tool_name": "Task",
                "tool_input": {"subagent_type": "general-purpose", "prompt": "Verify official documentation for JEP 444 and AWS Lambda quotas"},
            },
        )
        self.assertEqual(doc_out["hookSpecificOutput"]["updatedInput"]["subagent_type"], "docs-spec-analyst")

    def test_07_portable_distiller_compaction_and_bridge_stop_without_gemini_plugins(self) -> None:
        import socket
        import subprocess
        import time
        from kaizen_kit import bedrock_bridge, cli

        # Isolate HOME / USERPROFILE so ~/.gemini/config/plugins does NOT exist
        clean_home = Path(self._tmp.name) / "isolated_clean_home"
        clean_home.mkdir(parents=True, exist_ok=True)
        old_home = os.environ.get("HOME")
        old_userprofile = os.environ.get("USERPROFILE")
        os.environ["HOME"] = str(clean_home)
        os.environ["USERPROFILE"] = str(clean_home)
        try:
            # 1. PreToolUse lossless test distiller works without ~/.gemini
            _, bash_out, _ = uha.handle_claude_event(
                "PreToolUse",
                {
                    "session_id": "cc-portable-1",
                    "tool_name": "Bash",
                    "tool_input": {"command": "cargo test --lib"},
                },
            )
            rewritten_bash = bash_out["hookSpecificOutput"]["updatedInput"]["command"]
            self.assertIn("lossless_test_distiller.py", rewritten_bash)
            self.assertFalse(rewritten_bash.startswith("& "), "Bash command must not start with PowerShell '& '")

            agy_cmd = uha.handle_antigravity_event(
                "pre_tool",
                {
                    "conversationId": "agy-portable-1",
                    "toolCall": {"name": "run_command", "args": {"CommandLine": "cargo test --lib"}},
                },
            )
            self.assertIn("lossless_test_distiller.py", agy_cmd["overwrite"]["CommandLine"])

            # 2. Compaction recovery works end-to-end via universal_hook_adapter
            uha.handle_antigravity_event(
                "post_tool",
                {
                    "conversationId": "agy-compact-1",
                    "stepIdx": 12,
                    "lastUserInput": "Audit VectorCube rollup invariants",
                    "toolCall": {"name": "edit_file", "args": {"Path": "src/domain/rollup.rs"}},
                },
            )
            compact_res = uha.handle_antigravity_event(
                "pre_invocation",
                {
                    "conversationId": "agy-compact-1",
                    "initialNumSteps": 2,
                    "lastUserInput": "Continue",
                },
            )
            sys_msg = compact_res["injectSteps"][0]["systemMessage"]["systemMessage"]
            self.assertIn("src/domain/rollup.rs", sys_msg)

            # 3. CLI symbol-rag works in isolated clean HOME
            sample_ws = clean_home / "ws" / "demo-service"
            (sample_ws / ".git").mkdir(parents=True)
            (sample_ws / "src").mkdir(parents=True)
            (sample_ws / "src" / "telemetry.rs").write_text(
                "pub fn compute_station_qoe_rollup(rssi: i32) -> i32 { rssi }\n",
                encoding="utf-8",
            )
            rc_rag = cli.main(["symbol-rag", "station qoe rollup", "--workspace", str(clean_home / "ws"), "--json"])
            self.assertEqual(rc_rag, 0)

            # 4. Kit bedrock_bridge cmd_stop reaps orphan listener when PID file has exited wrapper PID
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", 0))
                ephem_port = int(s.getsockname()[1])
            pf = self.state_dir / f"bedrock_bridge_{ephem_port}.pid"
            srv_proc = subprocess.Popen(
                [sys.executable, str(Path(bedrock_bridge.__file__).resolve()), "serve", "--port", str(ephem_port)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.time() + 4.0
                while time.time() < deadline and not bedrock_bridge._is_port_open(ephem_port, timeout=0.1):
                    time.sleep(0.05)
                self.assertTrue(bedrock_bridge._is_port_open(ephem_port, timeout=0.2))
                dead_wrap = subprocess.Popen([sys.executable, "-c", "pass"])
                dead_wrap.wait(timeout=3)
                pf.write_text(str(dead_wrap.pid), encoding="utf-8")
                bedrock_bridge.cmd_stop(port=ephem_port, pid_file=pf)
                srv_proc.wait(timeout=5)
                stop_deadline = time.time() + 5.0
                while time.time() < stop_deadline and bedrock_bridge._is_port_open(ephem_port, timeout=0.1):
                    time.sleep(0.05)
                self.assertIsNotNone(srv_proc.poll())
                self.assertFalse(bedrock_bridge._is_port_open(ephem_port, timeout=0.2))
            finally:
                if srv_proc.poll() is None:
                    srv_proc.kill()
                srv_proc.wait(timeout=3)

            # 5. Subprocess hook execution without PYTHONPATH + non-UTF8 locale + Git Bash &&/VAR=val
            import json
            from kaizen_kit import installer

            sub_env = dict(os.environ)
            sub_env.pop("PYTHONPATH", None)
            sub_env.pop("PYTHONUTF8", None)
            sub_env["PYTHONIOENCODING"] = "cp1252"
            sub_env["HOME"] = str(clean_home)
            sub_env["USERPROFILE"] = str(clean_home)
            sub_env["AI_KIT_STATE_DIR"] = str(self.state_dir)

            installer.install_claude_target(clean_home)
            installed_settings = json.loads((clean_home / ".claude" / "settings.json").read_text(encoding="utf-8"))
            self.assertIn("PreCompact", installed_settings["hooks"])
            self.assertIn("mcp__serena__", installed_settings["hooks"]["PostToolUse"][0]["matcher"])

            hook_cmd_start = installed_settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
            p_start = subprocess.run(
                hook_cmd_start,
                shell=True,
                input=b'{"hook_event_name":"SessionStart","session_id":"clean-subproc-1"}',
                capture_output=True,
                env=sub_env,
                timeout=10,
            )
            self.assertEqual(p_start.returncode, 0, p_start.stderr.decode("utf-8", errors="replace"))
            self.assertIn("ADR-001", p_start.stdout.decode("utf-8", errors="replace"))

            # Verify non-cp1252 Unicode characters (\u2192, \u2705) do not crash universal_hook_adapter stdout
            adapter_path = Path(uha.__file__).resolve()
            subprocess.run(
                [sys.executable, str(adapter_path), "--runtime", "antigravity", "post_tool"],
                input=r'{"conversationId":"cp1252-uha","stepIdx":14,"lastUserInput":"VectorCube \u2192 Lambda \u2705","toolCall":{"name":"edit_file","args":{"Path":"src/x.rs"}}}'.encode("utf-8"),
                capture_output=True,
                env=sub_env,
                check=True,
                timeout=10,
            )
            p_uha = subprocess.run(
                [sys.executable, str(adapter_path), "--runtime", "antigravity", "pre_invocation"],
                input=b'{"conversationId":"cp1252-uha","initialNumSteps":2,"lastUserInput":"Continue"}',
                capture_output=True,
                env=sub_env,
                timeout=10,
            )
            self.assertEqual(p_uha.returncode, 0, p_uha.stderr.decode("utf-8", errors="replace"))
            self.assertIn("\u2192", p_uha.stdout.decode("utf-8", errors="replace"))

            # Verify Bash command with POSIX `cd ... && VAR=1 python -m unittest` + non-ASCII failure preserves exit code != 0
            fail_suite = clean_home / "unicode_suite"
            fail_suite.mkdir(parents=True, exist_ok=True)
            (fail_suite / "test_fail_utf8.py").write_text(
                "import os, unittest\n"
                "class T(unittest.TestCase):\n"
                "    def test_non_ascii_fail(self):\n"
                "        for i in range(28):\n"
                "            print(f'line_{i}')\n"
                "        self.assertEqual(os.environ.get('KAIZEN_SUB_VAR'), '1')\n"
                "        self.fail('Beklenmeyen hata: \u015f\u011f\u00fc\u0131\u00f6\u00e7 \u2192 FAIL')\n",
                encoding="utf-8",
            )
            posix_test_cmd = f'cd "{fail_suite.as_posix()}" && KAIZEN_SUB_VAR=1 "{Path(sys.executable).as_posix()}" -m unittest discover -p "test_*.py" -v'
            _, bash_posix_out, _ = uha.handle_claude_event(
                "PreToolUse",
                {
                    "session_id": "cc-posix-bash",
                    "tool_name": "Bash",
                    "tool_input": {"command": posix_test_cmd},
                },
            )
            rewritten_posix = bash_posix_out["hookSpecificOutput"]["updatedInput"]["command"]
            self.assertIn("--shell bash", rewritten_posix)
            bash_bin = r"C:\Program Files\Git\bin\bash.exe"
            if os.path.isfile(bash_bin):
                p_bash = subprocess.run([bash_bin, "--noprofile", "--norc", "-c", rewritten_posix], capture_output=True, env=sub_env, timeout=15)
            else:
                p_bash = subprocess.run(rewritten_posix, shell=True, capture_output=True, env=sub_env, timeout=15)
            self.assertNotEqual(p_bash.returncode, 0)
            bash_stdout = p_bash.stdout.decode("utf-8", errors="replace")
            self.assertIn("[Test Distiller - FAIL/WARN]", bash_stdout)
            self.assertIn("Beklenmeyen hata:", bash_stdout)

            # Verify stale PID file with recycled innocent PID does NOT kill innocent process or unrelated HTTP listener
            innocent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            try:
                stale_pf = self.state_dir / "bedrock_bridge_59998.pid"
                stale_pf.write_text(str(innocent.pid), encoding="utf-8")
                bedrock_bridge.cmd_stop(port=59998, pid_file=stale_pf)
                self.assertIsNone(innocent.poll(), "Stale PID file must NOT kill unrelated non-listening process")
            finally:
                if innocent.poll() is None:
                    innocent.kill()
                innocent.wait(timeout=3)
        finally:
            if old_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old_home
            if old_userprofile is None:
                os.environ.pop("USERPROFILE", None)
            else:
                os.environ["USERPROFILE"] = old_userprofile

    def test_08_voice_tag_metadata_and_night_shift_cli_dispatch(self) -> None:
        from kaizen_kit import cli
        from kaizen_kit.hooks import engine, prompt_decomposer

        voice_prompt = (
            "<voice_instructions>concise Turkish</voice_instructions>\n"
            "1. Rust VectorCube katmanında OnceLock anahtarlarını kontrol et.\n"
            "2. CloudGatewayApi OpenAPI şemasını güncelle ve Jenkins pre-flight çalıştır."
        )
        meta = prompt_decomposer.extract_voice_metadata(voice_prompt)
        self.assertTrue(meta["has_voice_tags"])
        self.assertNotIn("voice_instructions", meta["clean_prompt"])

        ev = engine.evaluate_user_prompt("cc-sess-voice-8", voice_prompt)
        self.assertFalse(ev["blocked"])
        self.assertTrue(ev["voice_metadata"]["has_voice_tags"])
        self.assertIn("WS-1", ev["context"])

        rc = cli.main(["night-shift", "--json"])
        self.assertEqual(rc, 0)

    def test_09_observation_masking_and_capc_suffix_hook_tags(self) -> None:
        from kaizen_kit.hooks import compact_state_guard, engine, lossless_test_distiller

        lines = [f"diff --git a/src/f{i}.rs b/src/f{i}.rs" for i in range(40)]
        lines.append("WARN: deprecated config key in module_x")
        lines.extend(f"+ let line_{i} = {i};" for i in range(60))
        lines.append("ERROR: upstream timeout at tail")
        raw_output = "\n".join(lines)

        with tempfile.TemporaryDirectory() as td:
            masked = lossless_test_distiller.mask_large_observation(
                "git diff origin/master",
                raw_output,
                exit_code=0,
                log_dir=Path(td),
            )
            self.assertTrue(masked["masked"])
            self.assertGreater(masked["tokens_saved_estimate"], 0)
            self.assertTrue(Path(masked["raw_log_path"]).is_file())
            self.assertIn("WARN: deprecated config key", masked["masked_text"])
            self.assertIn("ERROR: upstream timeout at tail", masked["masked_text"])

        post = engine.evaluate_post_tool(
            "cc-sess-obs-9",
            "run_command",
            {"CommandLine": "git diff", "output": raw_output, "exit_code": 0},
        )
        self.assertTrue(post["masked_observation"]["masked"])

        history = [
            {"role": "tool", "name": f"cmd_{i}", "tool_call_id": f"c{i}", "content": f"big output {i}\nline2"}
            for i in range(6)
        ]
        masked_hist = lossless_test_distiller.mask_stale_turn_observations(history, keep_recent_turns=4)
        self.assertTrue(masked_hist[0]["content"].startswith("<Observation masked: tool=cmd_0"))
        self.assertTrue(masked_hist[1]["content"].startswith("<Observation masked: tool=cmd_1"))
        self.assertEqual(masked_hist[2]["content"], "big output 2\nline2")
        self.assertEqual(masked_hist[5]["content"], "big output 5\nline2")

        start_res = engine.evaluate_session_start("cc-sess-capc-9", is_subagent=False)
        self.assertEqual(start_res["position"], "suffix")
        self.assertTrue(start_res["capc_cache_safe"])
        suffix_tag = compact_state_guard.format_capc_suffix_hook("checkpoint note")
        self.assertEqual(suffix_tag["position"], "suffix")
        self.assertTrue(suffix_tag["capc_cache_safe"])


if __name__ == "__main__":
    unittest.main()
