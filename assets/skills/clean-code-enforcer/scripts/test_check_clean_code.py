#!/usr/bin/env python3
"""
Unit and regression tests for `check_clean_code.py`.
Verifies detection of:
  - Long parameter lists (>4)
  - Primitive obsession (>=3)
  - Cryptic variable names (sta, thpt, rad, etc.)
  - Law of Demeter message chains (.getA().getB().getC())
  - Rust unwrap prohibition in production vs allowance in tests
  - Exit code 1 on violations, exit code 0 on clean code or --exit-zero
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Add scripts directory to path to import scanner functions directly
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

import check_clean_code


class TestCheckCleanCode(unittest.TestCase):

    def test_rust_long_parameter_list_detected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "handler.rs"
            file_path.write_text(
                "fn process(a: u32, b: u32, c: u32, d: u32, e: u32) -> bool {\n    true\n}\n",
                encoding="utf-8",
            )
            issues = check_clean_code.scan_rust_file(file_path)
            self.assertTrue(any("[LONG-PARAMETER-LIST]" in issue for issue in issues))
            self.assertTrue(any("[PRIMITIVE-OBSESSION]" in issue for issue in issues))

    def test_rust_multiline_parameters_detected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "multiline.rs"
            file_path.write_text(
                "fn process(\n"
                "    param1: String,\n"
                "    param2: String,\n"
                "    param3: String,\n"
                "    param4: String,\n"
                "    param5: String,\n"
                ") -> bool {\n"
                "    true\n"
                "}\n",
                encoding="utf-8",
            )
            issues = check_clean_code.scan_rust_file(file_path)
            self.assertTrue(any("[LONG-PARAMETER-LIST]" in issue for issue in issues))
            self.assertTrue(any("[PRIMITIVE-OBSESSION]" in issue for issue in issues))

    def test_rust_cryptic_variable_detected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "cryptic.rs"
            file_path.write_text(
                "fn calculate() {\n    let sta = 42;\n    let thpt = 100;\n}\n",
                encoding="utf-8",
            )
            issues = check_clean_code.scan_rust_file(file_path)
            self.assertTrue(any("[CLEAN-NAMING]" in issue and "sta" in issue for issue in issues))
            self.assertTrue(any("[CLEAN-NAMING]" in issue and "thpt" in issue for issue in issues))

    def test_message_chain_detected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "chain.rs"
            file_path.write_text(
                "fn get_city() {\n    let city = home.getNetwork().getNodes().getPrimary();\n}\n",
                encoding="utf-8",
            )
            issues = check_clean_code.scan_rust_file(file_path)
            self.assertTrue(any("[MESSAGE-CHAIN]" in issue for issue in issues))

    def test_rust_unwrap_in_production_vs_tests(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Production file should flag unwrap
            prod_path = Path(tmpdir) / "prod.rs"
            prod_path.write_text("fn run() {\n    let val = opt.unwrap();\n}\n", encoding="utf-8")
            prod_issues = check_clean_code.scan_rust_file(prod_path)
            self.assertTrue(any("[RUST-UNWRAP]" in issue for issue in prod_issues))

            # Test file under tests/ should NOT flag unwrap
            tests_dir = Path(tmpdir) / "tests"
            tests_dir.mkdir()
            test_path = tests_dir / "adversarial.rs"
            test_path.write_text("fn test_run() {\n    let val = opt.unwrap();\n}\n", encoding="utf-8")
            test_issues = check_clean_code.scan_rust_file(test_path)
            self.assertFalse(any("[RUST-UNWRAP]" in issue for issue in test_issues))

    def test_java_autowired_and_synchronized_detected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            java_path = Path(tmpdir) / "BadService.java"
            java_path.write_text(
                "public class BadService {\n"
                "    @Autowired\n"
                "    private StationRepository repo;\n"
                "    public synchronized void syncMethod() {}\n"
                "    public void emptyCatch() { try {} catch (Exception e) {} }\n"
                "}\n",
                encoding="utf-8",
            )
            issues = check_clean_code.scan_java_file(java_path)
            self.assertTrue(any("[JAVA-DI]" in issue for issue in issues))
            self.assertTrue(any("[LOOM-PINNING]" in issue for issue in issues))
            self.assertTrue(any("[SWALLOWED-EXCEPTION]" in issue for issue in issues))

    def test_clean_files_produce_zero_issues(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            clean_rust = Path(tmpdir) / "clean.rs"
            clean_rust.write_text(
                "pub struct StationSummary {\n"
                "    station_identifier: String,\n"
                "}\n"
                "impl StationSummary {\n"
                "    pub fn new(station_identifier: String) -> Self {\n"
                "        Self { station_identifier }\n"
                "    }\n"
                "}\n",
                encoding="utf-8",
            )
            issues = check_clean_code.scan_rust_file(clean_rust)
            self.assertEqual(len(issues), 0)

    def test_cli_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_file = Path(tmpdir) / "bad.rs"
            bad_file.write_text("fn run() {\n    let sta = 1;\n}\n", encoding="utf-8")

            # Default run on bad file should exit with code 1
            res_bad = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "check_clean_code.py"), str(bad_file)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res_bad.returncode, 1)
            self.assertIn("[CLEAN-CODE] Found 1 findings", res_bad.stdout)

            # Advisory mode (--exit-zero) on bad file should exit with code 0
            res_advisory = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "check_clean_code.py"), str(bad_file), "--exit-zero"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res_advisory.returncode, 0)
            self.assertIn("[CLEAN-CODE] Found 1 findings", res_advisory.stdout)

            # Clean file should exit with code 0
            clean_file = Path(tmpdir) / "clean.rs"
            clean_file.write_text("fn run() {\n    let station = 1;\n}\n", encoding="utf-8")
            res_clean = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "check_clean_code.py"), str(clean_file)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res_clean.returncode, 0)
            self.assertIn("[CLEAN-CODE] Zero Clean Code violations found.", res_clean.stdout)


if __name__ == "__main__":
    unittest.main()
