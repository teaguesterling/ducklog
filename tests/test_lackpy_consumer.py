"""Tests that lackpy can consume a ducklog policy database.

Validates claim H1 (integrator-without-fork) for lackpy.
"""
from __future__ import annotations

import pytest

from tests.conftest import parse_view
from ducklog.compiler import compile_view
from ducklog.consumers.lackpy import load_config_from_duckdb


@pytest.fixture
def lackpy_db(populated_db):
    view = parse_view('''
        tool { allow: true; max-level: 5; }
        tool[name="Bash"] { max-level: 2; }
        tool[name="Agent"] { allow: false; }
        tool[name="Bash"] { allow-pattern: "git *"; }
        tool[name="Bash"] { allow-pattern: "pytest *"; }
        tool[name="Bash"] { deny-pattern: "rm -rf *"; }
    ''')
    compile_view(populated_db, view, source_file="test.umw")
    return populated_db


class TestLackpyConfigShape:
    def test_has_required_keys(self, lackpy_db):
        config = load_config_from_duckdb(lackpy_db)
        for key in ("allowed_tools", "denied_tools", "kits", "max_level",
                     "allow_patterns", "deny_patterns"):
            assert key in config, f"missing key: {key}"

    def test_allowed_tools_is_list(self, lackpy_db):
        config = load_config_from_duckdb(lackpy_db)
        assert isinstance(config["allowed_tools"], list)

    def test_deny_patterns_is_dict(self, lackpy_db):
        config = load_config_from_duckdb(lackpy_db)
        assert isinstance(config["deny_patterns"], dict)


class TestLackpyContent:
    def test_allowed_tools(self, lackpy_db):
        config = load_config_from_duckdb(lackpy_db)
        assert "Read" in config["allowed_tools"]
        assert "Edit" in config["allowed_tools"]
        assert "Bash" in config["allowed_tools"]

    def test_denied_tools(self, lackpy_db):
        config = load_config_from_duckdb(lackpy_db)
        assert "Agent" in config["denied_tools"]
        assert "Read" not in config["denied_tools"]

    def test_max_level_tightest(self, lackpy_db):
        config = load_config_from_duckdb(lackpy_db)
        # Bash has max-level: 2 (<=), bare tool has max-level: 5
        # Tightest bound across all tools = 2
        assert config["max_level"] == 2

    def test_tool_specific_level(self, lackpy_db):
        config = load_config_from_duckdb(lackpy_db)
        assert config["tool_levels"]["Bash"] == 2

    def test_allow_patterns(self, lackpy_db):
        config = load_config_from_duckdb(lackpy_db)
        bash_patterns = config["allow_patterns"].get("Bash", [])
        assert "git *" in bash_patterns
        assert "pytest *" in bash_patterns

    def test_deny_patterns(self, lackpy_db):
        config = load_config_from_duckdb(lackpy_db)
        bash_deny = config["deny_patterns"].get("Bash", [])
        assert "rm -rf *" in bash_deny
