"""Regression checks for architecture-layer documentation governance."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
LAYERS = (
    "data-feature-store",
    "forecasting",
    "market-context",
    "optimization",
        "planning-service",
        "planning-runtime",
    "plan-compiler",
    "live-control",
    "actuation",
    "savings",
    "evaluation",
)

PRIVATE_SETUP_PATTERNS = {
    "web URL": re.compile(r"https?://", re.IGNORECASE),
    "IPv4 address": re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)"),
    "email address": re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b"),
    "Home Assistant entity ID": re.compile(
        r"\b(?:binary_sensor|climate|input_boolean|number|select|sensor|switch)"
        r"\.[a-z0-9_]+\b"
    ),
    "absolute local path": re.compile(r"(?:/Users/|/config/|/srv/|[A-Za-z]:\\)"),
}


class ArchitectureDocumentationTests(unittest.TestCase):
    def test_every_layer_has_public_guide_and_agent_rules(self):
        index = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
        architecture = (ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")

        for layer in LAYERS:
            with self.subTest(layer=layer):
                layer_dir = ROOT / "docs" / layer
                readme = layer_dir / "README.md"
                agents = layer_dir / "AGENTS.md"
                self.assertTrue(readme.is_file(), f"missing {readme}")
                self.assertTrue(agents.is_file(), f"missing {agents}")

                readme_text = readme.read_text(encoding="utf-8")
                agents_text = agents.read_text(encoding="utf-8")
                self.assertIn("## Setup independence", readme_text)
                self.assertIn("## Verification", readme_text)
                self.assertIn("## Forbidden", agents_text)
                self.assertIn("## Required checks", agents_text)
                self.assertIn("## Setup independence", agents_text)
                self.assertIn(f"{layer}/README.md", index)
                self.assertIn(f"docs/{layer}/README.md", architecture)

    def test_public_layer_guidance_has_no_concrete_setup_references(self):
        for layer in LAYERS:
            for filename in ("README.md", "AGENTS.md"):
                path = ROOT / "docs" / layer / filename
                text = path.read_text(encoding="utf-8")
                for label, pattern in PRIVATE_SETUP_PATTERNS.items():
                    with self.subTest(layer=layer, file=filename, pattern=label):
                        self.assertIsNone(
                            pattern.search(text),
                            f"{path} contains a concrete {label}",
                        )

    def test_repository_entry_points_link_documentation_governance(self):
        root_readme = (ROOT / "README.md").read_text(encoding="utf-8")
        architecture = (ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
        contracts = (ROOT / "INTERFACE_CONTRACTS.md").read_text(encoding="utf-8")

        self.assertIn("docs/README.md", root_readme)
        self.assertIn("Documentation and agent-guidance gate", architecture)
        self.assertIn("AGENTS.md", architecture)
        self.assertIn("part of contract governance", contracts)

    def test_source_tree_has_boundary_guidance(self):
        package = ROOT / "custom_components" / "battery_strategy"
        self.assertTrue((package / "AGENTS.md").is_file())
        self.assertTrue((package / "contracts" / "AGENTS.md").is_file())
        self.assertTrue((package / "forecasting" / "AGENTS.md").is_file())


if __name__ == "__main__":
    unittest.main()
