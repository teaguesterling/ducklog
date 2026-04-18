"""Shared fixtures for ducklog tests."""
from __future__ import annotations

import pytest
import duckdb


@pytest.fixture
def db():
    """In-memory DuckDB with the minimal entity schema."""
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE entities (
            id          INTEGER PRIMARY KEY,
            taxon       VARCHAR NOT NULL,
            type_name   VARCHAR NOT NULL,
            entity_id   VARCHAR,
            classes     VARCHAR[],
            attributes  MAP(VARCHAR, VARCHAR),
            parent_id   INTEGER,
        );

        CREATE TABLE entity_closure (
            ancestor_id   INTEGER NOT NULL,
            descendant_id INTEGER NOT NULL,
            depth         INTEGER NOT NULL,
            PRIMARY KEY (ancestor_id, descendant_id),
        );
    """)
    yield con
    con.close()


@pytest.fixture
def populated_db(db):
    """DB with a representative set of entities across all taxa."""
    db.execute("""
        INSERT INTO entities VALUES
            -- world: files
            (1,  'world', 'file', 'src/auth.py',       NULL, MAP{'path':'src/auth.py', 'name':'auth.py', 'language':'python'}, NULL),
            (2,  'world', 'file', 'src/util.py',        NULL, MAP{'path':'src/util.py', 'name':'util.py', 'language':'python'}, NULL),
            (3,  'world', 'file', 'tests/test_auth.py', NULL, MAP{'path':'tests/test_auth.py', 'name':'test_auth.py', 'language':'python'}, NULL),
            (4,  'world', 'file', 'README.md',          NULL, MAP{'path':'README.md', 'name':'README.md', 'language':'markdown'}, NULL),
            (5,  'world', 'file', 'src/main.pyc',       NULL, MAP{'path':'src/main.pyc', 'name':'main.pyc'}, NULL),

            -- world: dirs (for structural descent tests)
            (10, 'world', 'dir',  'src',                NULL, MAP{'path':'src', 'name':'src'}, NULL),
            (11, 'world', 'dir',  'tests',              NULL, MAP{'path':'tests', 'name':'tests'}, NULL),

            -- world: resources
            (20, 'world', 'resource', 'memory',         NULL, MAP{'kind':'memory'}, NULL),
            (21, 'world', 'resource', 'wall-time',      NULL, MAP{'kind':'wall-time'}, NULL),

            -- world: network
            (25, 'world', 'network', NULL,               NULL, MAP{}, NULL),

            -- world: exec
            (30, 'world', 'exec', 'bash',               NULL, MAP{'name':'bash', 'path':'/bin/bash'}, NULL),

            -- capability: tools
            (40, 'capability', 'tool', 'Read',           NULL, MAP{'name':'Read', 'altitude':'os', 'level':'2'}, NULL),
            (41, 'capability', 'tool', 'Edit',           NULL, MAP{'name':'Edit', 'altitude':'os', 'level':'3'}, NULL),
            (42, 'capability', 'tool', 'Bash',           NULL, MAP{'name':'Bash', 'altitude':'os', 'level':'5'}, NULL),
            (43, 'capability', 'tool', 'Grep',           NULL, MAP{'name':'Grep', 'altitude':'os', 'level':'1'}, NULL),
            (44, 'capability', 'tool', 'Agent',          NULL, MAP{'name':'Agent', 'altitude':'semantic', 'level':'7'}, NULL),

            -- state: modes
            (50, 'state', 'mode', NULL, ['implement'],       MAP{'writable':'src/, lib/'}, NULL),
            (51, 'state', 'mode', NULL, ['test'],            MAP{'writable':'tests/'}, NULL),
            (52, 'state', 'mode', NULL, ['explore'],         MAP{}, NULL),
            (53, 'state', 'mode', NULL, ['implement','tdd'], MAP{'writable':'src/, tests/'}, NULL),

            -- principal
            (60, 'principal', 'principal', 'Teague', NULL, MAP{'name':'Teague'}, NULL),

            -- audit
            (70, 'audit', 'observation', 'coach',   NULL, MAP{'name':'coach'}, NULL);

        -- Hierarchy for structural descent tests
        -- src/ contains files 1, 2, 5; tests/ contains file 3
        UPDATE entities SET parent_id = 10 WHERE id IN (1, 2, 5);
        UPDATE entities SET parent_id = 11 WHERE id = 3;

        INSERT INTO entity_closure
        WITH RECURSIVE closure(ancestor_id, descendant_id, depth) AS (
            SELECT id, id, 0 FROM entities
            UNION ALL
            SELECT c.ancestor_id, e.id, c.depth + 1
            FROM closure c
            JOIN entities e ON e.parent_id = c.descendant_id
        )
        SELECT DISTINCT * FROM closure;
    """)
    return db


def parse_selector(css_text: str):
    """Parse a CSS selector string via umwelt's parser and return the ComplexSelector."""
    from umwelt.sandbox.vocabulary import register_sandbox_vocabulary
    try:
        register_sandbox_vocabulary()
    except Exception:
        pass
    from umwelt.parser import parse
    view = parse(css_text + " { _test: true; }", validate=False)
    assert view.rules, f"no rules parsed from: {css_text}"
    return view.rules[0].selectors[0]


def parse_view(css_text: str):
    """Parse a full .umw view string."""
    from umwelt.sandbox.vocabulary import register_sandbox_vocabulary
    try:
        register_sandbox_vocabulary()
    except Exception:
        pass
    from umwelt.parser import parse
    return parse(css_text, validate=False)
