#!/usr/bin/env python3
"""Unit and integration tests for universal `kaizen_kit.qlora_pipeline`."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from kaizen_kit import gpu_burst_orchestrator, qlora_pipeline
from kaizen_kit.gates.deterministic_vault import DeterministicVault


class TestQloraPipeline(unittest.TestCase):
    def test_schema_prompt_cache_manager_all_four_languages(self) -> None:
        mgr = qlora_pipeline.SchemaPromptCacheManager()
        schemas = [
            (
                "user_json",
                '{\n  "$comment": "internal note",\n  "type": "object",\n  "properties": {"id": {"type": "string"}}\n}',
                "auto",
            ),
            (
                "ledger_sql",
                "-- primary ledger\nCREATE TABLE ledger_entries (\n    id TEXT PRIMARY KEY,\n    amount_cents BIGINT NOT NULL\n);\n",
                "auto",
            ),
            (
                "snapshot_rust",
                "/* state snapshot */\npub struct Snapshot {\n    pub epoch: u64,\n    pub checksum: String,\n}\n",
                "auto",
            ),
            (
                "invoice_java",
                "// DTO record\npublic record InvoiceView(\n    String invoiceId,\n    long amountCents\n) {}\n",
                "auto",
            ),
        ]
        msg_bundle_1 = mgr.build_cached_messages("Generate a query for ledger_entries.", schemas=schemas)
        msg_bundle_2 = mgr.build_cached_messages("Different dynamic suffix.", schemas=list(reversed(schemas)))

        # Byte-exact static prefix invariance regardless of registration order
        self.assertEqual(msg_bundle_1["cache_key"], msg_bundle_2["cache_key"])
        self.assertEqual(msg_bundle_1["static_prefix"], msg_bundle_2["static_prefix"])
        self.assertEqual(msg_bundle_1["messages"][0]["role"], "system")
        self.assertEqual(msg_bundle_1["messages"][1]["content"], "Generate a query for ledger_entries.")

        json_entry = mgr.get_entry("user_json")
        sql_entry = mgr.get_entry("ledger_sql")
        rust_entry = mgr.get_entry("snapshot_rust")
        java_entry = mgr.get_entry("invoice_java")
        assert json_entry and sql_entry and rust_entry and java_entry
        self.assertEqual(json_entry.kind, "json")
        self.assertEqual(sql_entry.kind, "sql")
        self.assertEqual(rust_entry.kind, "rust")
        self.assertEqual(java_entry.kind, "java")
        self.assertNotIn("$comment", json_entry.compressed_text)
        self.assertNotIn("primary ledger", sql_entry.compressed_text)
        self.assertGreater(sql_entry.savings_ratio, 0.0)

    def test_execution_oracle_best_of_n_and_pii_vault_scrubbing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            vault = DeterministicVault(vault_path=Path(td) / "vault.json", auto_save=False)
            fake_secret = "AKIA" + "IOSFODNN7EXAMPLE"

            def check_math(code: str) -> tuple[bool, float, str]:
                ns: dict = {}
                exec(code, ns)  # noqa: S102
                fn = ns.get("clamp_ratio")
                if not callable(fn):
                    return False, 0.1, "missing_clamp_ratio"
                ok = fn(-0.5) == 0.0 and fn(0.4) == 0.4 and fn(1.7) == 1.0
                return (True, 0.95, "") if ok else (False, 0.35, "assertion_failed")

            tasks = [
                {
                    "entity_id": "module_math",
                    "prompt": "Node 192.0.2.44 (MAC 12:34:56:78:9A:BC, ops@corp.example) needs clamp_ratio(x).",
                    "language": "python",
                    "required_patterns": [r"def clamp_ratio\("],
                    "verifier_fn": check_math,
                    "candidates": [
                        # Buggy candidate -> becomes hard-negative DPO rejected!
                        "def clamp_ratio(x: float) -> float:\n    return max(0.0, x)\n",
                        # Verified candidate -> becomes SFT + DPO chosen!
                        "def clamp_ratio(x: float) -> float:\n    # Tested on 192.0.2.44 / 12:34:56:78:9A:BC\n    return min(1.0, max(0.0, x))\n",
                        # Secret leak -> quarantined, must NEVER appear in SFT or DPO!
                        f"SECRET = '{fake_secret}'\ndef clamp_ratio(x: float) -> float:\n    return min(1.0, max(0.0, x))\n",
                    ],
                },
                {
                    "entity_id": "module_sql",
                    "prompt": "Write safe SQL select.",
                    "language": "sql",
                    "candidates": [
                        "DROP DATABASE production;",
                        "SELECT id, status FROM jobs WHERE state = 'READY';",
                    ],
                },
            ]

            out_dir = Path(td) / "jsonl_out"
            report = qlora_pipeline.synthesize_verified_sft_dpo_records(
                tasks,
                vault=vault,
                output_dir=out_dir,
            )

            self.assertEqual(len(report.sft_records), 2)
            self.assertEqual(len(report.dpo_records), 2)
            self.assertEqual(report.quarantined_count, 1)
            self.assertTrue(Path(report.sft_path or "").is_file())
            self.assertTrue(Path(report.dpo_path or "").is_file())

            serialized = json.dumps(report.to_dict())
            self.assertNotIn("192.0.2.44", serialized)
            self.assertNotIn("12:34:56:78:9A:BC", serialized)
            self.assertNotIn("ops@corp.example", serialized)
            self.assertNotIn(fake_secret, serialized)
            self.assertIn("198.51.100.", serialized)
            self.assertIn("00:50:C2:", serialized)
            self.assertEqual(report.dpo_records[0]["rejected_reason"], "assertion_failed")

    def test_split_train_eval_by_entity_zero_leakage(self) -> None:
        records = [
            {"entity_id": f"service_{i % 6}", "row_id": i}
            for i in range(30)
        ]
        train_a, eval_a = qlora_pipeline.split_train_eval_by_entity(records, eval_ratio=0.3, seed=99)
        train_b, eval_b = qlora_pipeline.split_train_eval_by_entity(records, eval_ratio=0.3, seed=99)

        self.assertEqual(train_a, train_b)
        self.assertEqual(eval_a, eval_b)
        self.assertEqual(len(train_a) + len(eval_a), 30)

        train_entities = {r["entity_id"] for r in train_a}
        eval_entities = {r["entity_id"] for r in eval_a}
        self.assertTrue(train_entities)
        self.assertTrue(eval_entities)
        self.assertTrue(train_entities.isdisjoint(eval_entities))

        with self.assertRaises(ValueError):
            qlora_pipeline.split_train_eval_by_entity(records, eval_ratio=1.0)

    def test_emit_training_scripts_driven_by_lora_portfolio(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            bundle = qlora_pipeline.emit_training_scripts(output_dir=td, lora_rank=16)
            expected_adapters = [entry["adapter"] for entry in gpu_burst_orchestrator.LORA_PORTFOLIO]
            emitted_adapters = [entry["adapter"] for entry in bundle["adapters"]]
            self.assertEqual(emitted_adapters, expected_adapters)
            self.assertEqual(len(bundle["written_files"]), 4)

            sm75 = bundle["compute_profiles"]["sm75"]
            sm89 = bundle["compute_profiles"]["sm89"]
            self.assertTrue(sm75["fp16"])
            self.assertFalse(sm75["bf16"])
            self.assertFalse(sm89["fp16"])
            self.assertTrue(sm89["bf16"])

            for adapter_id in expected_adapters:
                self.assertIn(adapter_id, bundle["vllm_multilora_script"])
                self.assertIn(adapter_id, bundle["gguf_export_script"])

            self.assertIn("SFTTrainer", bundle["train_script"])
            self.assertIn("DPOTrainer", bundle["train_script"])
            self.assertIn("--max-lora-rank 16", bundle["vllm_multilora_script"])
            self.assertIn("convert_lora_to_gguf.py", bundle["gguf_export_script"])

    def test_self_test_entrypoint(self) -> None:
        self.assertEqual(qlora_pipeline.run_self_test(), 0)
        self.assertEqual(qlora_pipeline.main(["self-test"]), 0)


if __name__ == "__main__":
    unittest.main()
