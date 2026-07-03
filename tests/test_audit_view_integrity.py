"""Audit-view integrity regression tests (GHSA-mr2g-ffm7-38mp).

These tests encode three security properties of the compiled audit view:

1. **No drift / no fail-open on structural, :glob, and ~= selectors.**
   The audit view must derive from the tested AST resolver, not a separate
   regex compiler that silently over-matches. A policy the view renders as
   *allowed* must match what enforcement would *deny*, and vice-versa.

2. **Fail CLOSED.** A selector construct the resolver does not support (an
   unknown pseudo-class, an unknown attribute operator) must raise, not
   silently drop the constraint and match every entity of the target type.

3. **No SQL injection.** A policy value containing a quote must be neutralised
   (escaped), never concatenated raw into the generated SQL.

The drift/fail-open/injection cases drive the *real* example pipeline
(`compile_policy_to_tables` → `cascade_candidates` → `resolved_properties`),
i.e. the exact code path that builds the shipped `world.duckdb`. They assert
the resolved allow/deny decision, so they stay meaningful (non-circular).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ducklog.compiler import UnsupportedSelector, compile_selector
from tests.conftest import parse_selector, parse_view

# The example "compile_world.py" lives outside the package; import it directly.
_EXAMPLE_DIR = Path(__file__).resolve().parent.parent / "examples" / "compile"
sys.path.insert(0, str(_EXAMPLE_DIR))
import compile_world as ex  # noqa: E402


# ---------------------------------------------------------------------------
# Real-pipeline harness: compile a policy into the audit view over the
# conftest entity fixture and read back resolved decisions.
# ---------------------------------------------------------------------------

@pytest.fixture
def audit(populated_db):
    """The conftest entities + the example's materialized policy tables."""
    ex.create_schema(populated_db)  # rules/selectors/declarations/vocab tables
    return populated_db


def _compile_policy(con, umw_text: str):
    view = parse_view(umw_text)
    ex.compile_policy_to_tables(con, view)
    ex.create_resolution_views(con)


def _resolved(con, entity_id: str, prop: str) -> str | None:
    row = con.execute(
        """
        SELECT rp.property_value
        FROM resolved_properties rp
        JOIN entities e ON rp.entity_id = e.id
        WHERE e.entity_id = ? AND rp.property_name = ?
        """,
        [entity_id, prop],
    ).fetchone()
    return row[0] if row else None


# ===========================================================================
# 1. No fail-open drift: view decision must equal enforcement decision
# ===========================================================================

class TestNoFailOpenDrift:
    def test_glob_does_not_grant_unrelated_files(self, audit):
        """file:glob("src/*.py") { editable: true } must not make README
        editable. The regex path drops :glob → the rule matches everything →
        the audit view renders README as editable while enforcement denies it."""
        _compile_policy(audit, """
            file:glob("src/*.py") { editable: true; }
            file { editable: false; }
        """)
        assert _resolved(audit, "src/auth.py", "editable") == "true"
        assert _resolved(audit, "README.md", "editable") == "false", (
            "drift: :glob dropped → README rendered editable (fail open)"
        )
        assert _resolved(audit, "tests/test_auth.py", "editable") == "false"

    def test_structural_descendant_does_not_grant_all(self, audit):
        """dir[name="src"] file must grant only files under src/, not every
        file just because a src dir exists."""
        _compile_policy(audit, """
            file { editable: false; }
            dir[name="src"] file { editable: true; }
        """)
        assert _resolved(audit, "src/auth.py", "editable") == "true"
        assert _resolved(audit, "README.md", "editable") == "false", (
            "drift: structural descendant collapsed to EXISTS(any src dir)"
        )
        assert _resolved(audit, "tests/test_auth.py", "editable") == "false"

    def test_contains_op_not_dropped(self, audit):
        """[path~="README.md"] uses ~= (whitespace-list contains). Dropping it
        would match every file."""
        _compile_policy(audit, """
            file { editable: false; }
            file[path~="README.md"] { editable: true; }
        """)
        assert _resolved(audit, "README.md", "editable") == "true"
        assert _resolved(audit, "src/auth.py", "editable") == "false", (
            "drift: ~= operator dropped → all files rendered editable"
        )


# ===========================================================================
# 2. SQL injection through a policy selector value
# ===========================================================================

class TestSqlInjection:
    def test_attr_value_quote_is_neutralised(self, audit):
        """A single quote in a selector attribute value must be escaped. The
        payload  x'or''='  breaks out to  = 'x' or '' = ''  (always true) and
        would make every file editable if interpolated raw."""
        _compile_policy(audit, """
            file[name="x'or''='"] { editable: true; }
            file { editable: false; }
        """)
        # No file is literally named  x'or''='  → the crafted rule matches
        # nothing → every file falls through to the bare `file` rule.
        assert _resolved(audit, "README.md", "editable") == "false", (
            "SQL injection: crafted selector value matched all files"
        )
        assert _resolved(audit, "src/auth.py", "editable") == "false"


# ===========================================================================
# 3. Fail closed: unsupported constructs abort the build, never match-all
# ===========================================================================

class TestFailClosed:
    def test_unknown_pseudo_aborts_build(self, audit):
        """An unsupported pseudo-class must raise during compilation rather
        than silently collapse to `type_name = 'file'` (matches every file)."""
        with pytest.raises(UnsupportedSelector):
            _compile_policy(audit, "file:not(.generated) { editable: true; }")

    def test_unknown_pseudo_raises_in_library(self):
        sel = parse_selector("file:not(.generated)")
        with pytest.raises(UnsupportedSelector):
            compile_selector(sel)


# ===========================================================================
# 4. Library-level narrowness guards (unit, fast, always-run)
# ===========================================================================

class TestLibraryCompilerNarrowness:
    def test_glob_matches_only_target(self, populated_db):
        where = compile_selector(parse_selector('file:glob("src/*.py")'))
        matched = {
            r[0] for r in populated_db.execute(
                f"SELECT entity_id FROM entities e WHERE {where}"
            ).fetchall()
        }
        assert matched == {"src/auth.py", "src/util.py"}

    def test_glob_underscore_is_literal(self, populated_db):
        """:glob("src/*.py") vs a literal underscore: `main_x.py` must not be
        matched by a pattern meant for `main.pyc` etc. Verify `_` is escaped:
        glob `util?py`'s `?` is a wildcard, but a literal `_` must not be."""
        populated_db.execute(
            "INSERT INTO entities VALUES "
            "(99,'world','file','src/utilXpy',NULL,MAP{'path':'src/utilXpy'},NULL)"
        )
        # Literal underscore in the glob must match only the underscore file.
        where = compile_selector(parse_selector('file:glob("src/util_py")'))
        matched = {
            r[0] for r in populated_db.execute(
                f"SELECT entity_id FROM entities e WHERE {where}"
            ).fetchall()
        }
        assert "src/utilXpy" not in matched, "literal `_` treated as wildcard"

    def test_injection_escaped_in_library(self, populated_db):
        where = compile_selector(parse_selector("file[name=\"x'or''='\"]"))
        matched = {
            r[0] for r in populated_db.execute(
                f"SELECT entity_id FROM entities e WHERE {where}"
            ).fetchall()
        }
        assert matched == set(), f"SQL injection: matched {matched}"
