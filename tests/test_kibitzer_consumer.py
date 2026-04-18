"""Tests that kibitzer can consume a ducklog database as its config source.

This is the first real consumer integration test. It proves that a .umw
policy, compiled through the ducklog pipeline, produces a config dict
that kibitzer's existing code can read — same shape as load_config()
returns from TOML.

Validates claim H1 (integrator-without-fork) for kibitzer.
"""
from __future__ import annotations

import pytest

from tests.conftest import parse_view
from ducklog.compiler import compile_view
from ducklog.consumers.kibitzer import load_config_from_duckdb


@pytest.fixture
def kibitzer_db(populated_db):
    """A policy DB with modes + tools compiled from a .umw snippet."""
    # The populated_db fixture already has modes and tools.
    # Compile a realistic policy over them.
    view = parse_view('''
        /* Mode-gated tool surface — the kibitzer use case */
        mode.implement tool { allow: true; }
        mode.implement tool[name="Bash"] { allow: true; }

        mode.explore tool { allow: false; }
        mode.explore tool[name="Read"] { allow: true; }
        mode.explore tool[name="Grep"] { allow: true; }
        mode.explore tool[name="Glob"] { allow: true; }

        mode.test tool { allow: true; }
        mode.test tool[name="Edit"] { allow: false; }
        mode.test tool[name="Write"] { allow: false; }

        mode.review tool { allow: false; }
        mode.review tool[name="Read"] { allow: true; }
        mode.review tool[name="Grep"] { allow: true; }

        tool[name="Agent"] { allow: false; }
    ''')
    compile_view(populated_db, view, source_file="test-kibitzer.umw")

    # Also create the kibitzer convenience views
    populated_db.execute("""
        CREATE OR REPLACE VIEW kibitzer_modes AS
            SELECT
                e.classes[1] AS mode_name,
                e.attributes['writable'] AS writable,
                e.attributes['strategy'] AS strategy,
            FROM entities e
            WHERE e.type_name = 'mode';

        CREATE OR REPLACE VIEW kibitzer_tool_surface AS
            WITH mode_tool_candidates AS (
                SELECT
                    cc.selector_text,
                    e.entity_id AS tool_name,
                    cc.property_value AS allowed,
                    cc.specificity,
                    cc.rule_index,
                    -- Extract mode name from selector
                    regexp_extract(cc.selector_text, 'mode[.](\w+)', 1) AS mode_name,
                FROM cascade_candidates cc
                JOIN entities e ON cc.entity_id = e.id
                WHERE e.type_name = 'tool'
                  AND cc.property_name = 'allow'
                  AND cc.selector_text LIKE 'mode.%'
            )
            SELECT DISTINCT ON (mode_name, tool_name)
                selector_text, tool_name, allowed, mode_name,
            FROM mode_tool_candidates
            ORDER BY mode_name, tool_name, specificity DESC, rule_index DESC;
    """)

    return populated_db


# ============================================================================
# Shape compatibility — the dict kibitzer expects
# ============================================================================

class TestConfigShape:
    def test_returns_dict_with_modes_key(self, kibitzer_db):
        config = load_config_from_duckdb(kibitzer_db)
        assert "modes" in config
        assert isinstance(config["modes"], dict)

    def test_mode_has_writable_list(self, kibitzer_db):
        config = load_config_from_duckdb(kibitzer_db)
        for mode_name, mode_config in config["modes"].items():
            assert "writable" in mode_config, f"mode {mode_name} missing 'writable'"
            assert isinstance(mode_config["writable"], list), f"mode {mode_name} writable is not a list"

    def test_mode_has_strategy_string(self, kibitzer_db):
        config = load_config_from_duckdb(kibitzer_db)
        for mode_name, mode_config in config["modes"].items():
            assert "strategy" in mode_config, f"mode {mode_name} missing 'strategy'"
            assert isinstance(mode_config["strategy"], str)


# ============================================================================
# Mode content
# ============================================================================

class TestModeContent:
    def test_implement_mode_writable_paths(self, kibitzer_db):
        config = load_config_from_duckdb(kibitzer_db)
        impl = config["modes"].get("implement", {})
        assert "src/" in impl["writable"]

    def test_explore_mode_readonly(self, kibitzer_db):
        config = load_config_from_duckdb(kibitzer_db)
        explore = config["modes"].get("explore", {})
        assert explore["writable"] == []

    def test_test_mode_has_strategy(self, kibitzer_db):
        config = load_config_from_duckdb(kibitzer_db)
        test_mode = config["modes"].get("test", {})
        assert "expected behavior" in test_mode["strategy"]

    def test_all_five_modes_present(self, kibitzer_db):
        config = load_config_from_duckdb(kibitzer_db)
        # populated_db has implement, test, explore, implement.tdd
        # modes.toml-style entities: implement, test, explore
        mode_names = set(config["modes"].keys())
        assert "implement" in mode_names
        assert "test" in mode_names
        assert "explore" in mode_names


# ============================================================================
# Tool surfaces per mode
# ============================================================================

class TestToolSurfaces:
    def test_returns_tools_dict(self, kibitzer_db):
        config = load_config_from_duckdb(kibitzer_db)
        assert "tools" in config
        assert isinstance(config["tools"], dict)

    def test_explore_mode_allows_only_read_tools(self, kibitzer_db):
        config = load_config_from_duckdb(kibitzer_db)
        explore = config["tools"].get("explore", {})
        assert "Read" in explore.get("allowed", [])
        assert "Grep" in explore.get("allowed", [])
        assert "Glob" in explore.get("allowed", [])
        # Everything else should be denied
        assert "Bash" in explore.get("denied", [])
        assert "Edit" in explore.get("denied", [])

    def test_implement_mode_allows_all(self, kibitzer_db):
        config = load_config_from_duckdb(kibitzer_db)
        impl = config["tools"].get("implement", {})
        assert "Read" in impl.get("allowed", [])
        assert "Edit" in impl.get("allowed", [])
        assert "Bash" in impl.get("allowed", [])

    def test_test_mode_denies_write_tools(self, kibitzer_db):
        config = load_config_from_duckdb(kibitzer_db)
        test_tools = config["tools"].get("test", {})
        assert "Edit" in test_tools.get("denied", [])
        assert "Write" in test_tools.get("denied", [])

    def test_review_mode_read_only_surface(self, kibitzer_db):
        config = load_config_from_duckdb(kibitzer_db)
        review = config["tools"].get("review", {})
        assert "Read" in review.get("allowed", [])
        assert "Grep" in review.get("allowed", [])
        assert "Bash" in review.get("denied", [])
        assert "Edit" in review.get("denied", [])


# ============================================================================
# Integration: kibitzer's get_mode_policy works with our dict
# ============================================================================

class TestKibitzerIntegration:
    def test_get_mode_policy_compatible(self, kibitzer_db):
        """The config dict works with kibitzer's get_mode_policy function."""
        from kibitzer.config import get_mode_policy

        config = load_config_from_duckdb(kibitzer_db)
        policy = get_mode_policy(config, "explore")
        assert policy["writable"] == []
        assert isinstance(policy["strategy"], str)

    def test_path_guard_compatible(self, kibitzer_db):
        """The config dict works with kibitzer's check_path function."""
        from kibitzer.config import get_mode_policy
        from kibitzer.guards.path_guard import check_path

        config = load_config_from_duckdb(kibitzer_db)

        # In implement mode, src/ should be writable
        impl_policy = get_mode_policy(config, "implement")
        result = check_path("src/foo.py", impl_policy)
        assert result.allowed

        # In explore mode, nothing should be writable
        explore_policy = get_mode_policy(config, "explore")
        result = check_path("src/foo.py", explore_policy)
        assert not result.allowed
