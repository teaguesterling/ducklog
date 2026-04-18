"""Round-trip enforcement tests: .umw → compile → bwrap → probe from inside.

Verifies claim B1 (enforcement fidelity): the compiled sandbox config
actually blocks what the policy forbids and allows what it permits.

These tests launch real bwrap sandboxes and probe from inside.
Requires bwrap installed (skips otherwise).

NOT testing umwelt's Python resolver — testing that the SQL-compiled
policy, when emitted as bwrap argv, produces a real sandbox that
enforces the declared bounds.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.conftest import parse_view
from ducklog.compiler import compile_view

# Skip entire module if bwrap is not available
pytestmark = pytest.mark.skipif(
    shutil.which("bwrap") is None,
    reason="bwrap not installed",
)


@pytest.fixture
def sandbox(tmp_path, populated_db):
    """Set up a sandbox workspace and compile a policy into bwrap argv."""
    return SandboxHarness(tmp_path, populated_db)


class SandboxHarness:
    """Helper that compiles a policy and runs commands inside bwrap."""

    def __init__(self, tmp_path: Path, db):
        self.tmp_path = tmp_path
        self.db = db
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir()

    def setup_files(self, file_specs: dict[str, str]):
        """Create files in the workspace. file_specs: {relative_path: content}."""
        for rel_path, content in file_specs.items():
            p = self.workspace / rel_path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)

    def compile_policy(self, umw_text: str):
        """Parse and compile a .umw policy into the DB."""
        # Re-register entities for the workspace files
        self.db.execute("DELETE FROM entities WHERE taxon = 'world' AND type_name = 'file'")
        file_id = 100
        for p in sorted(self.workspace.rglob("*")):
            if not p.is_file():
                continue
            rel = str(p.relative_to(self.workspace))
            self.db.execute(
                "INSERT INTO entities VALUES (?, 'world', 'file', ?, NULL, MAP{'path': ?}, NULL)",
                [file_id, rel, rel],
            )
            file_id += 1

        view = parse_view(umw_text)
        compile_view(self.db, view, source_file="test.umw")

    def bwrap_argv(self) -> list[str]:
        """Build bwrap argv from the compiled policy's bwrap views."""
        argv = ["bwrap"]

        # System mounts (minimal — enough for sh/cat/touch to work)
        for sys_dir in ["/bin", "/usr", "/lib", "/lib64", "/sbin"]:
            if os.path.exists(sys_dir):
                argv.extend(["--ro-bind", sys_dir, sys_dir])

        # proc and dev
        argv.extend(["--proc", "/proc"])
        argv.extend(["--dev", "/dev"])

        # Workspace file mounts from the resolved policy
        rows = self.db.execute("""
            SELECT
                e.entity_id AS rel_path,
                COALESCE(rp.property_value, 'false') = 'true' AS rw,
            FROM entities e
            LEFT JOIN resolved_properties rp
                ON e.id = rp.entity_id AND rp.property_name = 'editable'
            WHERE e.type_name = 'file'
              AND e.taxon = 'world'
              AND e.entity_id IS NOT NULL
        """).fetchall()

        for rel_path, rw in rows:
            src = str(self.workspace / rel_path)
            dst = f"/workspace/{rel_path}"
            if rw:
                argv.extend(["--bind", src, dst])
            else:
                argv.extend(["--ro-bind", src, dst])

        # Tmp for scratch space
        argv.extend(["--tmpfs", "/tmp"])

        return argv

    def run_inside(self, command: str, timeout: int = 5) -> subprocess.CompletedProcess:
        """Run a shell command inside the bwrap sandbox."""
        argv = self.bwrap_argv()
        argv.extend(["--", "sh", "-c", command])
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
        )


# ============================================================================
# B1: Enforcement fidelity — forbidden operations are blocked
# ============================================================================

class TestB1ForbiddenBlocked:
    """Policy says a file is NOT editable → bwrap mounts it read-only → writes fail."""

    def test_readonly_file_blocks_write(self, sandbox):
        sandbox.setup_files({
            "src/auth.py": "original content",
            "README.md": "readme",
        })
        sandbox.compile_policy('''
            file[path^="src/"] { editable: true; }
            file { editable: false; }
        ''')

        # README.md is editable: false → mounted read-only
        result = sandbox.run_inside("echo 'hacked' > /workspace/README.md")
        assert result.returncode != 0, (
            f"B1 VIOLATION: write to read-only file succeeded!\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_readonly_file_content_unchanged(self, sandbox):
        sandbox.setup_files({"README.md": "original"})
        sandbox.compile_policy('file { editable: false; }')

        sandbox.run_inside("echo 'hacked' > /workspace/README.md 2>/dev/null || true")

        # Verify the file on the host is unchanged
        assert (sandbox.workspace / "README.md").read_text() == "original"


# ============================================================================
# B1: Enforcement fidelity — permitted operations succeed
# ============================================================================

class TestB1PermittedAllowed:
    """Policy says a file IS editable → bwrap mounts it read-write → writes succeed."""

    def test_writable_file_allows_write(self, sandbox):
        sandbox.setup_files({"src/auth.py": "original"})
        sandbox.compile_policy('''
            file[path^="src/"] { editable: true; }
        ''')

        result = sandbox.run_inside("echo 'modified' > /workspace/src/auth.py")
        assert result.returncode == 0, (
            f"write to editable file failed!\nstderr: {result.stderr}"
        )

    def test_writable_file_content_changed(self, sandbox):
        sandbox.setup_files({"src/auth.py": "original"})
        sandbox.compile_policy('file[path^="src/"] { editable: true; }')

        sandbox.run_inside("echo 'modified' > /workspace/src/auth.py")
        assert "modified" in (sandbox.workspace / "src/auth.py").read_text()

    def test_readable_file_allows_read(self, sandbox):
        sandbox.setup_files({"README.md": "hello world"})
        sandbox.compile_policy('file { editable: false; }')

        result = sandbox.run_inside("cat /workspace/README.md")
        assert result.returncode == 0
        assert "hello world" in result.stdout


# ============================================================================
# B1: Mixed permissions — some files writable, some not
# ============================================================================

class TestB1MixedPermissions:
    def test_mixed_src_writable_docs_readonly(self, sandbox):
        sandbox.setup_files({
            "src/main.py": "main",
            "docs/guide.md": "guide",
        })
        sandbox.compile_policy('''
            file[path^="src/"] { editable: true; }
            file { editable: false; }
        ''')

        # src/ is writable
        r1 = sandbox.run_inside("echo 'new' > /workspace/src/main.py")
        assert r1.returncode == 0, f"src write failed: {r1.stderr}"

        # docs/ is read-only
        r2 = sandbox.run_inside("echo 'hacked' > /workspace/docs/guide.md")
        assert r2.returncode != 0, "B1 VIOLATION: write to read-only docs/ succeeded"

    def test_specificity_wins_in_enforcement(self, sandbox):
        """file[path^='src/'] (specific) beats file (bare) even in enforcement."""
        sandbox.setup_files({"src/app.py": "app"})
        sandbox.compile_policy('''
            file { editable: false; }
            file[path^="src/"] { editable: true; }
        ''')

        # The specific rule should win → src/app.py is writable
        result = sandbox.run_inside("echo 'updated' > /workspace/src/app.py")
        assert result.returncode == 0, f"specificity didn't win in enforcement: {result.stderr}"
        assert "updated" in (sandbox.workspace / "src/app.py").read_text()


# ============================================================================
# B1: Cross-axis enforcement — mode-gated file permissions
# ============================================================================

class TestB1CrossAxis:
    def test_mode_gated_editable(self, sandbox):
        """mode.implement file[path^='src/'] { editable: true } should resolve
        (mode exists in entities) and produce a writable mount."""
        sandbox.setup_files({"src/auth.py": "original"})
        sandbox.compile_policy('''
            file { editable: false; }
            mode.implement file[path^="src/"] { editable: true; }
        ''')

        # mode.implement exists in populated_db → cross-axis rule fires → src writable
        result = sandbox.run_inside("echo 'modified' > /workspace/src/auth.py")
        assert result.returncode == 0, f"mode-gated write failed: {result.stderr}"
