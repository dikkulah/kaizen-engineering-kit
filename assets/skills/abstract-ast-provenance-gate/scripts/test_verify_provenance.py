#!/usr/bin/env python3
"""
Unit and regression tests for `verify_provenance.py`.
Verifies enforcement of the 4 Abstract Architectural Invariants:
  1. INV-ISOLATION (APOSD Information Leakage & Evans Bounded Context)
  2. INV-PROVENANCE (Fowler Anti-Feature-Envy & Uncle Bob Law of Demeter)
  3. INV-NO-SYNTHETIC-LITERAL (Evans Ubiquitous Authenticity)
  4. INV-COMPILE-TIME-SCHEMA (Bloch Item 6 Static Immutable Assets)
Plus:
  - Parity Oracle Gate (VACUOUS_PASS in tests)
  - Strict refusal of vacuous passes on zero files (Status=FAIL, exit 1)
  - Recursive nested workspace discovery
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

import verify_provenance


class TestVerifyProvenance(unittest.TestCase):

    def test_refuse_vacuous_pass_on_zero_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            res = verify_provenance.audit_workspace(Path(tmpdir))
            self.assertFalse(res["passed"])
            self.assertEqual(res["scanned_files"], 0)
            self.assertIn("VACUOUS-PASS-ZERO-FILES", res["by_invariant"])

            # Verify CLI returns exit code 1
            cmd_res = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "verify_provenance.py"), "--workspace", tmpdir],
                capture_output=True,
                text=True,
            )
            self.assertEqual(cmd_res.returncode, 1)
            self.assertIn("Status=FAIL", cmd_res.stdout)
            self.assertIn("Scanned=0 files", cmd_res.stdout)

    def test_inv_isolation_avro_bson(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            src_dir = Path(tmpdir) / "crates" / "codecs" / "src"
            src_dir.mkdir(parents=True)

            # Avro module referencing BSON symbol
            avro_file = src_dir / "avro_parser.rs"
            avro_file.write_text(
                "use crate::bson::TopologyRecord;\npub fn parse() {}\n",
                encoding="utf-8",
            )

            # BSON module referencing Avro symbol
            bson_file = src_dir / "bson_reader.rs"
            bson_file.write_text(
                "use apache_avro::Schema;\npub fn read() {}\n",
                encoding="utf-8",
            )

            res = verify_provenance.audit_workspace(Path(tmpdir))
            self.assertFalse(res["passed"])
            self.assertEqual(res["by_invariant"].get("INV-ISOLATION"), 2)

    def test_inv_provenance_hollow_controller(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ctrl_dir = Path(tmpdir) / "crates" / "rest" / "src" / "controllers"
            ctrl_dir.mkdir(parents=True)
            ctrl_file = ctrl_dir / "devices.rs"
            ctrl_file.write_text(
                "pub fn handle_devices(_cube: &MaterializedHomeState) -> Result<Vec<u8>, String> {\n"
                "    let resp = ItemsResponse::new(Vec::<DeviceDto>::new());\n"
                "    Ok(resp)\n"
                "}\n",
                encoding="utf-8",
            )

            res = verify_provenance.audit_workspace(Path(tmpdir))
            self.assertFalse(res["passed"])
            self.assertGreaterEqual(res["by_invariant"].get("INV-PROVENANCE", 0), 1)

    def test_inv_no_synthetic_literal(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            src_dir = Path(tmpdir) / "crates" / "projector" / "src"
            src_dir.mkdir(parents=True)
            proj_file = src_dir / "wan.rs"
            proj_file.write_text(
                'pub fn get_wan() {\n    let wan_type = "ETHERNET";\n    let model = "Kaizen Gateway";\n}\n',
                encoding="utf-8",
            )

            res = verify_provenance.audit_workspace(Path(tmpdir))
            self.assertFalse(res["passed"])
            self.assertEqual(res["by_invariant"].get("INV-NO-SYNTHETIC-LITERAL"), 2)

    def test_inv_compile_time_schema(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            src_dir = Path(tmpdir) / "src"
            src_dir.mkdir(parents=True)
            schema_file = src_dir / "loader.rs"
            schema_file.write_text(
                "pub fn init() {\n    ensure_briefcase_schema_loaded();\n}\n",
                encoding="utf-8",
            )

            res = verify_provenance.audit_workspace(Path(tmpdir))
            self.assertFalse(res["passed"])
            self.assertEqual(res["by_invariant"].get("INV-COMPILE-TIME-SCHEMA"), 1)

    def test_parity_oracle_gate_vacuous_pass_detected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tests_dir = Path(tmpdir) / "tests"
            tests_dir.mkdir(parents=True)
            test_file = tests_dir / "api_test.rs"
            test_file.write_text(
                "#[tokio::test]\n"
                "async fn test_endpoint() {\n"
                "    let response = client.get(\"/api\").await;\n"
                "    assert!(response.status() == 200);\n"
                "}\n",
                encoding="utf-8",
            )

            res = verify_provenance.audit_workspace(Path(tmpdir))
            self.assertFalse(res["passed"])
            self.assertIn("PARITY-ORACLE-GATE", res["by_invariant"])

    def test_nested_workspace_discovery(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create nested workspace structure
            nested_crate = Path(tmpdir) / "sub_repo" / "nested_crates" / "engine" / "src"
            nested_crate.mkdir(parents=True)
            clean_file = nested_crate / "lib.rs"
            clean_file.write_text(
                "pub struct MaterializedState {\n    pub count: u64,\n}\n",
                encoding="utf-8",
            )

            res = verify_provenance.audit_workspace(Path(tmpdir))
            self.assertTrue(res["passed"])
            self.assertEqual(res["scanned_files"], 1)
            self.assertEqual(res["violation_count"], 0)

            # CLI should pass with exit code 0
            cmd_res = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "verify_provenance.py"), "--workspace", tmpdir],
                capture_output=True,
                text=True,
            )
            self.assertEqual(cmd_res.returncode, 0)
            self.assertIn("Status=PASS", cmd_res.stdout)
            self.assertIn("Scanned=1 files", cmd_res.stdout)


if __name__ == "__main__":
    unittest.main()
