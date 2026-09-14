"""Tests for dataset manifest generation config loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from dataset.generation.datasets_config import load_manifest_generation_config


def test_load_manifest_generation_config_reads_quotas_and_projects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NEO4J_PASSWORD", "test-password")
    config_path = tmp_path / "datasets.config.toml"
    config_path.write_text(
        """
[generation]
name = "test_profile"
output = "dataset/test_manifest.jsonl"
ready_repos_csv = "dataset/helper/projects_1_5mb.csv"
repo_cache_root = "temp/eval_repos"
timeout_seconds = 120
skip_baseline_verification = true
fail_if_quotas_unfilled = false
enrich_java_after_generation = false

[projects]
names = ["Tap4j", "Drugis Common"]

[selection]
limit_per_project = 3
min_ref_count = 2
max_ref_count = 10
min_elements = 2
max_elements = 8
outlier_percentile = 0.9
min_elements_with_smell = 1

[runtime]
min_smells = 15
max_smells = 20
max_classes = 10

[runtime.type_quotas]
2 = 1
5 = 2

[neo4j]
uri = "http://localhost:7474"
user = "neo4j"

[extras]
graph_fallback_projects = ["Pusher Java Client"]
""",
        encoding="utf-8",
    )

    config = load_manifest_generation_config(config_path)
    assert config.name == "test_profile"
    assert config.projects == ("Tap4j", "Drugis Common")
    assert config.runtime_type_quotas == {2: 1, 5: 2}
    assert config.runtime_min_smells == 15
    assert config.skip_baseline_verification is True
    assert config.graph_fallback_projects == ("Pusher Java Client",)

    namespace = config.as_namespace()
    assert namespace.projects == "Tap4j,Drugis Common"
    assert namespace.runtime_type_quotas == "2:1,5:2"
    assert namespace.password == "test-password"
