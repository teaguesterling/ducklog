"""Round-trip tests: parse .umw → compile → populate DuckDB → resolve → verify.

Tests the full pipeline from CSS source to queryable resolved properties.
Each test writes a .umw snippet, compiles it against a populated entity
database, and verifies the resolved values match expectations.
"""
from __future__ import annotations

import pytest
from tests.conftest import parse_view
from ducklog.compiler import compile_view


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def world(populated_db):
    """A populated DB with the resolution views ready to go."""
    return populated_db


def _resolve(world, umw_text: str) -> dict:
    """Compile a .umw snippet into the world DB and return helpers for querying."""
    view = parse_view(umw_text)
    compile_view(world, view, source_file="test.umw")
    return _resolved(world)


def _resolved(con):
    """Query helpers over the resolved world."""
    class Resolved:
        def __init__(self, con):
            self.con = con

        def property(self, entity_id: str, prop_name: str) -> str | None:
            """Get the resolved value of a property for an entity."""
            row = self.con.execute("""
                SELECT rp.property_value
                FROM resolved_properties rp
                JOIN entities e ON rp.entity_id = e.id
                WHERE e.entity_id = ? AND rp.property_name = ?
            """, [entity_id, prop_name]).fetchone()
            return row[0] if row else None

        def property_by_id(self, entity_db_id: int, prop_name: str) -> str | None:
            row = self.con.execute("""
                SELECT property_value FROM resolved_properties
                WHERE entity_id = ? AND property_name = ?
            """, [entity_db_id, prop_name]).fetchone()
            return row[0] if row else None

        def all_props(self, entity_id: str) -> dict[str, str]:
            rows = self.con.execute("""
                SELECT rp.property_name, rp.property_value
                FROM resolved_properties rp
                JOIN entities e ON rp.entity_id = e.id
                WHERE e.entity_id = ?
            """, [entity_id]).fetchall()
            return dict(rows)

        def entities_where(self, type_name: str, prop_name: str, prop_value: str) -> list[str]:
            rows = self.con.execute("""
                SELECT e.entity_id
                FROM resolved_properties rp
                JOIN entities e ON rp.entity_id = e.id
                WHERE e.type_name = ? AND rp.property_name = ? AND rp.property_value = ?
                ORDER BY e.entity_id
            """, [type_name, prop_name, prop_value]).fetchall()
            return [r[0] for r in rows]

        def candidate_count(self) -> int:
            return self.con.execute("SELECT COUNT(*) FROM cascade_candidates").fetchone()[0]

        def resolved_count(self) -> int:
            return self.con.execute("SELECT COUNT(*) FROM resolved_properties").fetchone()[0]

        def assert_a1(self):
            """Every (entity, property) has exactly one resolved value."""
            dupes = self.con.execute("""
                SELECT entity_id, property_name, COUNT(*) AS n
                FROM resolved_properties
                GROUP BY entity_id, property_name HAVING n > 1
            """).fetchall()
            assert dupes == [], f"A1 violated: duplicate winners {dupes}"

    return Resolved(con)


# ============================================================================
# Round-trip: file permissions
# ============================================================================

class TestRoundtripFilePermissions:
    def test_prefix_match_sets_editable(self, world):
        rv = _resolve(world, '''
            file[path^="src/"] { editable: true; }
            file { editable: false; }
        ''')
        assert rv.property("src/auth.py", "editable") == "true"
        assert rv.property("src/util.py", "editable") == "true"
        assert rv.property("tests/test_auth.py", "editable") == "false"
        assert rv.property("README.md", "editable") == "false"
        rv.assert_a1()

    def test_suffix_match_hides_pyc(self, world):
        rv = _resolve(world, '''
            file { visible: true; }
            file[path$=".pyc"] { visible: false; }
        ''')
        assert rv.property("src/auth.py", "visible") == "true"
        assert rv.property("src/main.pyc", "visible") == "false"
        rv.assert_a1()

    def test_specificity_ordering(self, world):
        """More specific selector wins regardless of document order."""
        rv = _resolve(world, '''
            file { editable: false; }
            file[path^="src/"] { editable: true; }
        ''')
        # file[path^=...] has higher specificity than bare file,
        # even though bare file comes first in document order.
        assert rv.property("src/auth.py", "editable") == "true"
        rv.assert_a1()

    def test_document_order_breaks_ties(self, world):
        """Same specificity → later rule wins."""
        rv = _resolve(world, '''
            file[path^="src/"] { editable: false; }
            file[path^="src/"] { editable: true; }
        ''')
        assert rv.property("src/auth.py", "editable") == "true"
        rv.assert_a1()


# ============================================================================
# Round-trip: tool permissions
# ============================================================================

class TestRoundtripToolPermissions:
    def test_tool_allow_deny(self, world):
        rv = _resolve(world, '''
            tool { allow: true; }
            tool[name="Bash"] { allow: false; }
        ''')
        assert rv.property("Read", "allow") == "true"
        assert rv.property("Bash", "allow") == "false"
        rv.assert_a1()

    def test_max_level_tightest_wins(self, world):
        """<= comparison: the minimum value wins, regardless of specificity."""
        rv = _resolve(world, '''
            tool { max-level: 5; }
            tool[name="Bash"] { max-level: 3; }
        ''')
        # Both rules match Bash. max-level uses <= comparison.
        # The tightest bound (3) should win.
        assert rv.property("Bash", "max-level") == "3"
        # Read only matched by the bare rule → max-level: 5
        assert rv.property("Read", "max-level") == "5"
        rv.assert_a1()

    def test_allow_pattern_aggregates(self, world):
        """pattern-in comparison: all patterns from all matching rules aggregate."""
        rv = _resolve(world, '''
            tool[name="Bash"] { allow-pattern: "git *"; }
            tool[name="Bash"] { allow-pattern: "pytest *"; }
        ''')
        patterns = rv.property("Bash", "allow-pattern")
        assert patterns is not None
        assert "git *" in patterns
        assert "pytest *" in patterns
        rv.assert_a1()


# ============================================================================
# Round-trip: mode-gated tools
# ============================================================================

class TestRoundtripModeTools:
    def test_mode_gates_tool(self, world):
        """mode.explore tool { allow: false; } gates all tools when explore mode exists."""
        rv = _resolve(world, '''
            tool { allow: true; }
            mode.explore tool { allow: false; }
        ''')
        # mode.explore exists in entities → the cross-axis rule fires.
        # 2-axis rule (mode+tool) beats 1-axis (tool) by axis_count.
        assert rv.property("Read", "allow") == "false"
        assert rv.property("Bash", "allow") == "false"
        rv.assert_a1()

    def test_mode_specific_tool_override(self, world):
        """Specific tool override within a mode beats the mode default."""
        rv = _resolve(world, '''
            mode.explore tool { allow: false; }
            mode.explore tool[name="Read"] { allow: true; }
        ''')
        # Both are 2-axis; tool[name="Read"] has higher within-axis specificity.
        assert rv.property("Read", "allow") == "true"
        assert rv.property("Bash", "allow") == "false"
        rv.assert_a1()

    def test_nonexistent_mode_gates_nothing(self, world):
        """mode.deploy doesn't exist → the rule produces no candidates."""
        rv = _resolve(world, '''
            tool { allow: true; }
            mode.deploy tool { allow: false; }
        ''')
        # mode.deploy doesn't exist → 0 candidates from that rule.
        # Only the bare tool rule fires → allow: true.
        assert rv.property("Read", "allow") == "true"
        rv.assert_a1()


# ============================================================================
# Round-trip: cross-axis cascade ordering
# ============================================================================

class TestRoundtripCrossAxis:
    def test_three_axis_beats_two_axis(self, world):
        """principal × mode × tool (3-axis) beats mode × tool (2-axis)."""
        rv = _resolve(world, '''
            mode.implement tool[name="Bash"] { allow: false; }
            principal#Teague mode.implement tool[name="Bash"] { allow: true; }
        ''')
        # 3-axis rule has higher axis_count → wins.
        assert rv.property("Bash", "allow") == "true"
        rv.assert_a1()

    def test_two_axis_beats_one_axis(self, world):
        """mode × tool (2-axis) beats bare tool (1-axis)."""
        rv = _resolve(world, '''
            tool[name="Bash"] { allow: true; }
            mode.implement tool[name="Bash"] { allow: false; }
        ''')
        assert rv.property("Bash", "allow") == "false"
        rv.assert_a1()


# ============================================================================
# Round-trip: structural descendants
# ============================================================================

class TestRoundtripStructuralDescendants:
    def test_dir_file_descendant(self, world):
        """dir[name="src"] file { editable: true; } → files under src/."""
        rv = _resolve(world, '''
            file { editable: false; }
            dir[name="src"] file { editable: true; }
        ''')
        assert rv.property("src/auth.py", "editable") == "true"
        assert rv.property("src/util.py", "editable") == "true"
        assert rv.property("tests/test_auth.py", "editable") == "false"
        assert rv.property("README.md", "editable") == "false"
        rv.assert_a1()


# ============================================================================
# Round-trip: multiple properties per rule
# ============================================================================

class TestRoundtripMultipleProperties:
    def test_multiple_declarations(self, world):
        rv = _resolve(world, '''
            file[path^="src/"] { editable: true; visible: true; }
            file { editable: false; visible: true; }
        ''')
        props = rv.all_props("src/auth.py")
        assert props["editable"] == "true"
        assert props["visible"] == "true"
        rv.assert_a1()


# ============================================================================
# Round-trip: full policy (integration)
# ============================================================================

class TestRoundtripFullPolicy:
    def test_realistic_policy(self, world):
        """A realistic multi-rule policy exercises the full pipeline."""
        rv = _resolve(world, '''
            file { editable: false; visible: true; }
            file[path^="src/"] { editable: true; }
            file[path$=".pyc"] { visible: false; }

            tool { allow: true; max-level: 3; }
            tool[name="Bash"] { max-level: 2; }
            tool[name="Agent"] { allow: false; }

            tool[name="Bash"] { allow-pattern: "git *"; }
            tool[name="Bash"] { allow-pattern: "pytest *"; }

            network { deny: "*"; }
            resource[kind="memory"] { limit: 512MB; }
        ''')
        # Files
        assert rv.property("src/auth.py", "editable") == "true"
        assert rv.property("README.md", "editable") == "false"
        assert rv.property("src/main.pyc", "visible") == "false"
        assert rv.property("src/auth.py", "visible") == "true"

        # Tools
        assert rv.property("Read", "allow") == "true"
        assert rv.property("Agent", "allow") == "false"
        assert rv.property("Read", "max-level") == "3"
        assert rv.property("Bash", "max-level") == "2"  # <= tightest

        # Patterns aggregate
        patterns = rv.property("Bash", "allow-pattern")
        assert "git *" in patterns
        assert "pytest *" in patterns

        # Network
        assert rv.property_by_id(25, "deny") == "*"

        # Resource
        assert rv.property("memory", "limit") == "512MB"

        # Verification
        rv.assert_a1()
        assert rv.candidate_count() > 0
        assert rv.resolved_count() > 0
