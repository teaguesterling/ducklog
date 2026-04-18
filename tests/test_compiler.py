"""Test-driven development of the selector-to-SQL compiler.

Organized from atoms to molecules:
  Level 1: Type selectors (file, tool, mode)
  Level 2: ID selectors (file#README.md, tool#Bash)
  Level 3: Attribute selectors ([path="..."], [path^="..."], [path$="..."])
  Level 4: Class selectors (mode.implement, mode.implement.tdd)
  Level 5: Compound selectors — cross-axis (mode.implement tool)
  Level 6: Three-axis compounds (principal#Teague mode.implement tool#Bash)
  Level 7: Structural descendants (dir[name="src"] file)
  Level 8: Pseudo-classes (:glob("src/**/*.py"))
"""
from __future__ import annotations

import pytest
from tests.conftest import parse_selector
from ducklog.compiler import compile_selector


# ============================================================================
# Level 1: Bare type selectors
# ============================================================================

class TestTypeSelectors:
    def test_bare_file_matches_all_files(self, populated_db):
        sql = compile_selector(parse_selector("file"))
        ids = _query_ids(populated_db, sql)
        assert ids == {1, 2, 3, 4, 5}

    def test_bare_tool_matches_all_tools(self, populated_db):
        sql = compile_selector(parse_selector("tool"))
        ids = _query_ids(populated_db, sql)
        assert ids == {40, 41, 42, 43, 44, 45, 46}

    def test_bare_mode_matches_all_modes(self, populated_db):
        sql = compile_selector(parse_selector("mode"))
        ids = _query_ids(populated_db, sql)
        assert ids == {50, 51, 52, 53, 54}

    def test_bare_resource_matches_all_resources(self, populated_db):
        sql = compile_selector(parse_selector("resource"))
        ids = _query_ids(populated_db, sql)
        assert ids == {20, 21}

    def test_type_selector_excludes_other_types(self, populated_db):
        sql = compile_selector(parse_selector("tool"))
        ids = _query_ids(populated_db, sql)
        # No file, dir, mode, etc. should appear
        assert all(40 <= i <= 46 for i in ids)


# ============================================================================
# Level 2: ID selectors
# ============================================================================

class TestIDSelectors:
    def test_file_with_id(self, populated_db):
        sql = compile_selector(parse_selector("file#README.md"))
        ids = _query_ids(populated_db, sql)
        assert ids == {4}

    def test_tool_with_id(self, populated_db):
        sql = compile_selector(parse_selector("tool#Bash"))
        ids = _query_ids(populated_db, sql)
        assert ids == {42}

    def test_principal_with_id(self, populated_db):
        sql = compile_selector(parse_selector("principal#Teague"))
        ids = _query_ids(populated_db, sql)
        assert ids == {60}

    def test_nonexistent_id_matches_nothing(self, populated_db):
        sql = compile_selector(parse_selector("tool#NonExistent"))
        ids = _query_ids(populated_db, sql)
        assert ids == set()


# ============================================================================
# Level 3: Attribute selectors
# ============================================================================

class TestAttributeSelectors:
    def test_exact_match(self, populated_db):
        sql = compile_selector(parse_selector('file[path="src/auth.py"]'))
        ids = _query_ids(populated_db, sql)
        assert ids == {1}

    def test_prefix_match(self, populated_db):
        sql = compile_selector(parse_selector('file[path^="src/"]'))
        ids = _query_ids(populated_db, sql)
        assert ids == {1, 2, 5}  # src/auth.py, src/util.py, src/main.pyc

    def test_suffix_match(self, populated_db):
        sql = compile_selector(parse_selector('file[path$=".py"]'))
        ids = _query_ids(populated_db, sql)
        assert ids == {1, 2, 3}  # all .py files, not .pyc or .md

    def test_contains_match(self, populated_db):
        sql = compile_selector(parse_selector('file[path*="auth"]'))
        ids = _query_ids(populated_db, sql)
        assert ids == {1, 3}  # src/auth.py and tests/test_auth.py

    def test_multiple_attributes_conjoin(self, populated_db):
        sql = compile_selector(parse_selector('file[path^="src/"][language="python"]'))
        ids = _query_ids(populated_db, sql)
        assert ids == {1, 2}  # src/auth.py and src/util.py, NOT src/main.pyc

    def test_attribute_on_tool(self, populated_db):
        sql = compile_selector(parse_selector('tool[altitude="os"]'))
        ids = _query_ids(populated_db, sql)
        assert ids == {40, 41, 42, 43, 45, 46}  # Read, Edit, Bash, Grep, Glob, Write — not Agent

    def test_resource_kind_attribute(self, populated_db):
        sql = compile_selector(parse_selector('resource[kind="memory"]'))
        ids = _query_ids(populated_db, sql)
        assert ids == {20}


# ============================================================================
# Level 4: Class selectors
# ============================================================================

class TestClassSelectors:
    def test_single_class(self, populated_db):
        sql = compile_selector(parse_selector("mode.implement"))
        ids = _query_ids(populated_db, sql)
        # Both mode entities that have 'implement' in their classes
        assert ids == {50, 53}

    def test_class_excludes_non_matching(self, populated_db):
        sql = compile_selector(parse_selector("mode.explore"))
        ids = _query_ids(populated_db, sql)
        assert ids == {52}

    def test_multiple_classes_must_all_match(self, populated_db):
        sql = compile_selector(parse_selector("mode.implement.tdd"))
        ids = _query_ids(populated_db, sql)
        assert ids == {53}  # only the one with both classes

    def test_class_not_present_matches_nothing(self, populated_db):
        sql = compile_selector(parse_selector("mode.deploy"))
        ids = _query_ids(populated_db, sql)
        assert ids == set()


# ============================================================================
# Level 5: Compound selectors — cross-axis
# ============================================================================

class TestCompoundSelectors:
    def test_two_axis_mode_tool(self, populated_db):
        """mode.implement tool → all tools, gated by mode existing."""
        sql = compile_selector(parse_selector("mode.implement tool"))
        ids = _query_ids(populated_db, sql)
        # mode.implement exists → context holds → all tools match
        assert ids == {40, 41, 42, 43, 44, 45, 46}

    def test_two_axis_mode_tool_with_attr(self, populated_db):
        """mode.implement tool[name="Bash"] → just Bash, gated by mode."""
        sql = compile_selector(parse_selector('mode.implement tool[name="Bash"]'))
        ids = _query_ids(populated_db, sql)
        assert ids == {42}

    def test_context_qualifier_nonexistent_mode_produces_nothing(self, populated_db):
        """mode.deploy tool → no 'deploy' mode exists → no matches."""
        sql = compile_selector(parse_selector("mode.deploy tool"))
        ids = _query_ids(populated_db, sql)
        assert ids == set()

    def test_two_axis_principal_tool(self, populated_db):
        """principal#Teague tool → all tools, gated by principal existing."""
        sql = compile_selector(parse_selector("principal#Teague tool"))
        ids = _query_ids(populated_db, sql)
        assert ids == {40, 41, 42, 43, 44, 45, 46}

    def test_two_axis_principal_nonexistent(self, populated_db):
        """principal#Nobody tool → principal doesn't exist → no matches."""
        sql = compile_selector(parse_selector("principal#Nobody tool"))
        ids = _query_ids(populated_db, sql)
        assert ids == set()


# ============================================================================
# Level 6: Three-axis compounds
# ============================================================================

class TestThreeAxisCompounds:
    def test_principal_mode_tool(self, populated_db):
        """principal#Teague mode.implement tool#Bash → Bash, if both qualifiers hold."""
        sql = compile_selector(parse_selector('principal#Teague mode.implement tool[name="Bash"]'))
        ids = _query_ids(populated_db, sql)
        assert ids == {42}

    def test_three_axis_one_qualifier_fails(self, populated_db):
        """principal#Nobody mode.implement tool#Bash → 0 matches."""
        sql = compile_selector(parse_selector('principal#Nobody mode.implement tool[name="Bash"]'))
        ids = _query_ids(populated_db, sql)
        assert ids == set()

    def test_three_axis_different_qualifier_fails(self, populated_db):
        """principal#Teague mode.deploy tool#Bash → 0 matches (no deploy mode)."""
        sql = compile_selector(parse_selector('principal#Teague mode.deploy tool[name="Bash"]'))
        ids = _query_ids(populated_db, sql)
        assert ids == set()


# ============================================================================
# Level 7: Structural descendants (closure table)
# ============================================================================

class TestStructuralDescendants:
    def test_dir_file_descendant(self, populated_db):
        """dir[name="src"] file → files under the src directory."""
        sql = compile_selector(parse_selector('dir[name="src"] file'))
        ids = _query_ids(populated_db, sql)
        assert ids == {1, 2, 5}  # auth.py, util.py, main.pyc — all under src/

    def test_dir_file_other_dir(self, populated_db):
        """dir[name="tests"] file → files under tests/."""
        sql = compile_selector(parse_selector('dir[name="tests"] file'))
        ids = _query_ids(populated_db, sql)
        assert ids == {3}  # test_auth.py

    def test_dir_file_nonexistent_dir(self, populated_db):
        """dir[name="lib"] file → no dir named lib → 0 matches."""
        sql = compile_selector(parse_selector('dir[name="lib"] file'))
        ids = _query_ids(populated_db, sql)
        assert ids == set()


# ============================================================================
# Level 8: Pseudo-classes
# ============================================================================

class TestPseudoClasses:
    def test_glob_pseudo(self, populated_db):
        """file:glob("src/*.py") → Python files directly in src/."""
        sql = compile_selector(parse_selector('file:glob("src/*.py")'))
        ids = _query_ids(populated_db, sql)
        assert ids == {1, 2}  # auth.py, util.py — not main.pyc, not test_auth.py

    def test_glob_recursive(self, populated_db):
        """file:glob("**/*.py") → all Python files anywhere."""
        sql = compile_selector(parse_selector('file:glob("**/*.py")'))
        ids = _query_ids(populated_db, sql)
        assert ids == {1, 2, 3}  # all .py files

    def test_glob_no_match(self, populated_db):
        """file:glob("*.rs") → no Rust files."""
        sql = compile_selector(parse_selector('file:glob("*.rs")'))
        ids = _query_ids(populated_db, sql)
        assert ids == set()


# ============================================================================
# Helpers
# ============================================================================

def _query_ids(con, sql: str) -> set[int]:
    """Execute a compiled selector SQL and return matched entity IDs."""
    result = con.execute(f"SELECT e.id FROM entities e WHERE {sql}").fetchall()
    return {row[0] for row in result}
