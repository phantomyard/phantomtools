"""Smoke tests for the CLI wrappers (bin/po, bin/phantomorg, *.cmd).

The POSIX wrappers are the install surface on Linux/macOS; they resolve the
repo root through python3 (GNU-only ``readlink -f`` does not exist on macOS
BSD), so these tests run them via bash to prove the resolution still works.
The Windows ``.cmd`` wrappers cannot run on Linux — their presence and the
delegation contract are checked instead.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "posix", "POSIX-only wrappers")
class BinWrapperTest(unittest.TestCase):
    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    def _run_env(
        self, env: dict[str, str], *args: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env=env,
        )

    def test_pf_wrapper_help_exits_zero(self):
        proc = self._run("bash", str(REPO_ROOT / "bin" / "po"), "--help")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_phantomorg_wrapper_help_exits_zero(self):
        proc = self._run("bash", str(REPO_ROOT / "bin" / "phantomorg"), "--help")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_wrapper_resolves_repo_even_via_symlink(self):
        # Simulate the install.sh layout: a symlink elsewhere pointing at
        # bin/po — the wrapper must still resolve the real repo root.
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "po"
            link.symlink_to(REPO_ROOT / "bin" / "po")
            proc = self._run("bash", str(link), "--help")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_wrapper_works_with_venv_and_no_global_python3(self):
        """C3 (adversarial review v0.5.5): a repo with its own .venv must
        work even when no global python3 exists in PATH."""
        bash = shutil.which("bash")
        self.assertIsNotNone(bash)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            shutil.copy2(REPO_ROOT / "bin" / "po", bin_dir / "po")

            # A fake repo venv interpreter that answers --help with exit 0.
            venv_py = root / ".venv" / "bin" / "python"
            venv_py.parent.mkdir(parents=True)
            venv_py.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            venv_py.chmod(0o755)

            # PATH with base utils but NO global python3: only the stub
            # venv can provide an interpreter.
            fakebin = Path(tmp) / "fakebin"
            fakebin.mkdir()
            for tool in ("dirname", "readlink"):
                real = shutil.which(tool)
                self.assertIsNotNone(real, tool)
                (fakebin / tool).symlink_to(real)
            self.assertIsNone(shutil.which("python3", path=str(fakebin)))
            env = {"PATH": str(fakebin)}
            proc = self._run_env(env, bash, str(bin_dir / "po"), "--help")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_wrapper_fails_without_python_and_without_venv(self):
        """C3: with neither a global python3 nor a repo venv, the wrapper
        fails with a clear message instead of crashing."""
        bash = shutil.which("bash")
        self.assertIsNotNone(bash)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            shutil.copy2(REPO_ROOT / "bin" / "po", bin_dir / "po")
            fakebin = Path(tmp) / "fakebin"
            fakebin.mkdir()
            for tool in ("dirname", "readlink"):
                real = shutil.which(tool)
                self.assertIsNotNone(real, tool)
                (fakebin / tool).symlink_to(real)
            proc = self._run_env(
                {"PATH": str(fakebin)}, bash, str(bin_dir / "po"), "--help"
            )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no Python interpreter", proc.stderr)


class WindowsCmdWrapperTest(unittest.TestCase):
    def test_cmd_wrappers_present_and_delegate(self):
        for name in ("po.cmd", "phantomorg.cmd"):
            path = REPO_ROOT / "bin" / name
            self.assertTrue(path.is_file(), f"missing bin/{name}")
            text = path.read_text(encoding="utf-8")
            self.assertIn("-m phantomorg.cli", text)
            self.assertIn(".venv\\Scripts\\python.exe", text)


if __name__ == "__main__":
    unittest.main()
