#!/usr/bin/env python3
"""Unit tests for Universal Multi-Provider Agent SDK Adapters (`agent_sdk_adapters.py`)."""

from __future__ import annotations

import unittest
from kaizen_kit import agent_sdk_adapters


class TestAgentSdkAdapters(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = agent_sdk_adapters.AgentRoleSpec(
            role_id="systems-architect",
            name="Principal Systems Architect",
            goal="Design deep modules and verify Clean Architecture invariants",
            backstory="Experienced distributed systems architect mastering Evans DDD and APOSD.",
            tools=["git_grep", "arch_audit", "parity_gate"],
            preferred_model="claude-sonnet-5-5",
        )

    def test_factory_creates_all_supported_sdks(self) -> None:
        for sdk in agent_sdk_adapters.SUPPORTED_SDKS:
            runner = agent_sdk_adapters.create_agent_runner(self.spec, sdk=sdk)
            out = runner.run("Audit layer boundaries")
            self.assertEqual(out["sdk"], sdk)
            self.assertEqual(out["agent"], "systems-architect")
            self.assertEqual(out["status"], "ready")

    def test_unsupported_sdk_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            agent_sdk_adapters.create_agent_runner(self.spec, sdk="unsupported_framework")


if __name__ == "__main__":
    unittest.main()
