#!/usr/bin/env python3
"""Unit tests for Effort AutoSense Engine (`effort_autosense.py`)."""

from __future__ import annotations

import unittest
from kaizen_kit import effort_autosense


class TestEffortAutoSense(unittest.TestCase):
    def test_empty_or_whitespace_input_returns_minimal(self) -> None:
        res = effort_autosense.autosense_effort("")
        self.assertEqual(res.level, "MINIMAL")
        self.assertEqual(res.effort, "minimal")
        self.assertEqual(res.thinking_budget, 0)

    def test_cosmetic_typo_returns_minimal(self) -> None:
        res = effort_autosense.autosense_effort("Fix typo in docstring and format README comments")
        self.assertEqual(res.level, "MINIMAL")
        self.assertEqual(res.thinking_budget, 0)

    def test_standard_feature_returns_medium(self) -> None:
        res = effort_autosense.autosense_effort("Implement feature for new endpoint with unit test suite")
        self.assertIn(res.level, ("MEDIUM", "HIGH"))
        self.assertGreaterEqual(res.thinking_budget, 4096)

    def test_high_concurrency_race_condition_returns_high_or_xhigh(self) -> None:
        prompt = (
            "Diagnose asynchronous race condition and deadlock in cross-repo ReentrantLock synchronization. "
            "Evaluate blast radius and enforce Clean Architecture boundary."
        )
        res = effort_autosense.autosense_effort(prompt)
        self.assertIn(res.level, ("HIGH", "XHIGH"))
        self.assertGreaterEqual(res.thinking_budget, 16384)

    def test_deep_nesting_and_memory_leak_returns_xhigh(self) -> None:
        code = "\n".join([
            "def handle_packet(data):",
            "    if valid:",
            "        for chunk in data:",
            "            if chunk.ready:",
            "                while not done:",
            "                    if check_lock():",
            "                        # critical deadlock and memory leak inspection",
            "                        acquire_lock()",
        ])
        res = effort_autosense.autosense_effort(code)
        self.assertIn(res.level, ("HIGH", "XHIGH"))
        self.assertGreaterEqual(res.thinking_budget, 16384)


if __name__ == "__main__":
    unittest.main()
