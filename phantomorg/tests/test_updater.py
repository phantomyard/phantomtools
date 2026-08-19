"""`po update` tests: GitHub release discovery, version comparison and the
checkout fast-forward flow (phantombot-style update cycle).

The updater talks to GitHub (HTTP) and the local checkout (git). Both are
injected/tested through seams: ``http_get`` for the API call and
``subprocess.run`` mocking for git, so no network or repo mutation happens
in tests.
"""

import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from phantomorg.updater import (
    EXIT_AVAILABLE,
    EXIT_ERROR,
    EXIT_OK,
    apply_update,
    find_latest_release,
    read_local_version,
    refresh_venv,
    remote_origin_repo,
    repo_root,
    run_update,
    version_cmp,
)


def fake_http(status: int, body: object | None = None, error: str | None = None):
    """Build an ``http_get`` seam returning a canned response."""

    def _get(url: str, token: str | None):
        return status, body, error

    return _get


class VersionCompareTest(unittest.TestCase):
    def test_equal(self):
        self.assertEqual(version_cmp("0.4.19", "0.4.19"), 0)

    def test_patch_higher(self):
        self.assertEqual(version_cmp("0.4.20", "0.4.19"), 1)
        self.assertEqual(version_cmp("0.4.19", "0.4.20"), -1)

    def test_two_digit_parts_not_lexicographic(self):
        # "0.4.10" > "0.4.9" numerically, not string-wise.
        self.assertEqual(version_cmp("0.4.10", "0.4.9"), 1)

    def test_minor_higher(self):
        self.assertEqual(version_cmp("0.5.0", "0.4.19"), 1)
        self.assertEqual(version_cmp("0.4.19", "0.5.0"), -1)

    def test_v_prefix_stripped_by_caller_but_parts_tolerant(self):
        # version_cmp itself only parses numeric parts, so a leading v is fine.
        self.assertEqual(version_cmp("v0.5.0", "0.4.19"), 1)


class FindLatestReleaseTest(unittest.TestCase):
    def test_parses_release(self):
        body = {
            "tag_name": "v0.5.0",
            "published_at": "2026-08-10T12:00:00Z",
            "body": "notes",
            "assets": [],
        }
        result = find_latest_release("owner/repo", None, http_get=fake_http(200, body))
        self.assertTrue(result.ok)
        assert result.release is not None
        self.assertEqual(result.release.version, "0.5.0")
        self.assertEqual(result.release.tag, "v0.5.0")
        self.assertEqual(result.release.body, "notes")

    def test_missing_tag_name(self):
        result = find_latest_release("owner/repo", None, http_get=fake_http(200, {}))
        self.assertFalse(result.ok)
        self.assertIn("missing tag_name", result.error or "")

    def test_404_private_or_wrong_repo(self):
        result = find_latest_release("owner/repo", None, http_get=fake_http(404))
        self.assertFalse(result.ok)
        self.assertIn("no releases found", result.error or "")

    def test_network_error(self):
        result = find_latest_release(
            "owner/repo", None, http_get=fake_http(0, error="boom")
        )
        self.assertFalse(result.ok)
        self.assertIn("network error", result.error or "")

    def test_retries_unauth_on_401(self):
        calls = []

        def _get(url, token):
            calls.append(token)
            if len(calls) == 1:
                return 401, None, None
            return 200, {"tag_name": "v1.0.0"}, None

        result = find_latest_release("owner/repo", "sometoken", http_get=_get)
        self.assertTrue(result.ok)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls, ["sometoken", None])

    def test_rate_limited_without_token(self):
        result = find_latest_release("owner/repo", None, http_get=fake_http(403))
        self.assertFalse(result.ok)
        self.assertIn("rate-limited", result.error or "")


class ApplyUpdateTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="pf-update-test-"))

    def tearDown(self):
        import shutil

        shutil.rmtree(self.root, ignore_errors=True)

    def test_refuses_dirty_tree(self):
        # status --porcelain returns one tracked modification (M file).
        with unittest.mock.patch(
            "phantomorg.updater.subprocess.run",
            return_value=subprocess.CompletedProcess(
                [], 0, stdout=" M phantomorg/cli.py\n", stderr=""
            ),
        ) as mock_run:
            ok, message = apply_update(self.root, "v0.5.0")
        self.assertFalse(ok)
        self.assertIn("uncommitted changes", message)
        mock_run.assert_called_once()

    def test_allows_untracked_files(self):
        # Only untracked (??) entries: fast-forward is fine.
        with unittest.mock.patch(
            "phantomorg.updater.subprocess.run",
            side_effect=[
                subprocess.CompletedProcess(
                    [], 0, stdout="?? newfile.txt\n", stderr=""
                ),
                subprocess.CompletedProcess([], 0, stdout="", stderr=""),  # fetch
                subprocess.CompletedProcess([], 0, stdout="", stderr=""),  # rev-parse
                subprocess.CompletedProcess([], 0, stdout="", stderr=""),  # merge
            ],
        ):
            ok, message = apply_update(self.root, "v0.5.0")
        self.assertTrue(ok)
        self.assertIn("updated to v0.5.0", message)

    def test_merge_failure_reported(self):
        with unittest.mock.patch(
            "phantomorg.updater.subprocess.run",
            side_effect=[
                subprocess.CompletedProcess([], 0, stdout="", stderr=""),
                subprocess.CompletedProcess([], 0, stdout="", stderr=""),
                subprocess.CompletedProcess([], 0, stdout="", stderr=""),
                subprocess.CompletedProcess(
                    [], 1, stdout="", stderr="not a fast-forward"
                ),
            ],
        ):
            ok, message = apply_update(self.root, "v0.5.0")
        self.assertFalse(ok)
        self.assertIn("not a fast-forward", message)


class RepoDiscoveryTest(unittest.TestCase):
    def test_repo_root_finds_pyproject_and_bin(self):
        root = repo_root()
        self.assertTrue((root / "pyproject.toml").is_file())
        self.assertTrue((root / "bin" / "po").is_file())

    def test_read_local_version(self):
        root = repo_root()
        version = read_local_version(root)
        self.assertIsNotNone(version)
        assert version is not None
        self.assertRegex(version, r"^\d+\.\d+\.\d+")

    def test_remote_origin_normalization(self):
        cases = [
            (
                "git@github.com:phantomyard/phantomtools.git",
                "phantomyard/phantomtools",
            ),
            (
                "https://github.com/phantomyard/phantomtools.git",
                "phantomyard/phantomtools",
            ),
            (
                "https://github.com/phantomyard/phantomtools",
                "phantomyard/phantomtools",
            ),
            (
                "https://x-access-token:abc123@github.com/phantomyard/phantomtools.git",
                "phantomyard/phantomtools",
            ),
        ]
        for url, expected in cases:
            with (
                self.subTest(url=url),
                unittest.mock.patch(
                    "phantomorg.updater.subprocess.run",
                    return_value=subprocess.CompletedProcess(
                        [], 0, stdout=url, stderr=""
                    ),
                ),
            ):
                self.assertEqual(remote_origin_repo(Path("/tmp/x")), expected)

    def test_remote_origin_non_github(self):
        with unittest.mock.patch(
            "phantomorg.updater.subprocess.run",
            return_value=subprocess.CompletedProcess(
                [], 0, stdout="git@gitlab.com:foo/bar.git", stderr=""
            ),
        ):
            self.assertIsNone(remote_origin_repo(Path("/tmp/x")))


class RefreshVenvTest(unittest.TestCase):
    def test_noop_without_venv(self):
        with tempfile.TemporaryDirectory() as tmp:
            ok, message = refresh_venv(Path(tmp))
        self.assertTrue(ok)
        self.assertEqual(message, "")

    def test_posix_layout_uses_bin_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            venv_bin = root / ".venv" / "bin"
            venv_bin.mkdir(parents=True)
            (venv_bin / "python").touch()
            with (
                unittest.mock.patch("phantomorg.updater.os.name", "posix"),
                unittest.mock.patch(
                    "phantomorg.updater.subprocess.run",
                    return_value=subprocess.CompletedProcess(
                        [], 0, stdout="", stderr=""
                    ),
                ) as mock_run,
            ):
                ok, message = refresh_venv(root)
        self.assertTrue(ok)
        self.assertIn("refreshed", message)
        cmd = mock_run.call_args.args[0]
        self.assertEqual(cmd[0], str(venv_bin / "python"))

    def test_windows_layout_uses_scripts_python_exe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scripts = root / ".venv" / "Scripts"
            scripts.mkdir(parents=True)
            (scripts / "python.exe").touch()
            with (
                unittest.mock.patch("phantomorg.updater.os.name", "nt"),
                unittest.mock.patch(
                    "phantomorg.updater.subprocess.run",
                    return_value=subprocess.CompletedProcess(
                        [], 0, stdout="", stderr=""
                    ),
                ) as mock_run,
            ):
                ok, message = refresh_venv(root)
        self.assertTrue(ok)
        self.assertIn("refreshed", message)
        cmd = mock_run.call_args.args[0]
        self.assertEqual(cmd[0], str(scripts / "python.exe"))

    def test_windows_layout_ignores_posix_venv(self):
        # A POSIX-style venv must not be picked up on Windows.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".venv" / "bin").mkdir(parents=True)
            (root / ".venv" / "bin" / "python").touch()
            with unittest.mock.patch("phantomorg.updater.os.name", "nt"):
                ok, message = refresh_venv(root)
        self.assertTrue(ok)
        self.assertEqual(message, "")


class RunUpdateTest(unittest.TestCase):
    def _release_body(self, tag="v0.5.0"):
        return {
            "tag_name": tag,
            "published_at": "2026-08-10T12:00:00Z",
            "body": "release notes",
            "assets": [],
        }

    def test_check_reports_available_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # A fake git checkout: rev-parse succeeds (returncode 0).
            with (
                unittest.mock.patch(
                    "phantomorg.updater.is_git_checkout", return_value=True
                ),
                unittest.mock.patch(
                    "phantomorg.updater.remote_origin_repo", return_value="owner/repo"
                ),
                unittest.mock.patch(
                    "phantomorg.updater.read_local_version", return_value="0.4.19"
                ),
                unittest.mock.patch(
                    "phantomorg.updater.find_latest_release",
                    return_value=unittest.mock.MagicMock(
                        ok=True,
                        release=unittest.mock.MagicMock(
                            version="0.5.0", tag="v0.5.0", published_at=None, body=""
                        ),
                    ),
                ),
            ):
                code = run_update(check=True, root=root)
        self.assertEqual(code, EXIT_AVAILABLE)

    def test_check_up_to_date_exit_0(self):
        with (
            unittest.mock.patch(
                "phantomorg.updater.is_git_checkout", return_value=True
            ),
            unittest.mock.patch(
                "phantomorg.updater.remote_origin_repo", return_value="owner/repo"
            ),
            unittest.mock.patch(
                "phantomorg.updater.read_local_version", return_value="0.5.0"
            ),
            unittest.mock.patch(
                "phantomorg.updater.find_latest_release",
                return_value=unittest.mock.MagicMock(
                    ok=True,
                    release=unittest.mock.MagicMock(
                        version="0.5.0", tag="v0.5.0", published_at=None, body=""
                    ),
                ),
            ),
        ):
            code = run_update(check=True, root=Path("/tmp/x"))
        self.assertEqual(code, EXIT_OK)

    def test_not_a_git_checkout_exit_1(self):
        with unittest.mock.patch(
            "phantomorg.updater.is_git_checkout", return_value=False
        ):
            code = run_update(check=True, root=Path("/tmp/x"))
        self.assertEqual(code, EXIT_ERROR)

    def test_no_remote_exit_1(self):
        with (
            unittest.mock.patch(
                "phantomorg.updater.is_git_checkout", return_value=True
            ),
            unittest.mock.patch(
                "phantomorg.updater.remote_origin_repo", return_value=None
            ),
            unittest.mock.patch.dict("os.environ", {}, clear=True),
        ):
            code = run_update(check=True, root=Path("/tmp/x"))
        self.assertEqual(code, EXIT_ERROR)

    def test_origin_mismatch_refused_exit_1(self):
        """H3: an update repo override that does not match the checkout's
        git origin must be refused — otherwise the tag is installed from a
        different repository than the one consulted on GitHub."""
        with (
            unittest.mock.patch(
                "phantomorg.updater.is_git_checkout", return_value=True
            ),
            unittest.mock.patch(
                "phantomorg.updater.remote_origin_repo",
                return_value="owner/repo",
            ),
            unittest.mock.patch.dict(
                "os.environ",
                {"PHANTOMORG_UPDATE_REPO": "trusted-owner/phantomorg"},
            ),
        ):
            code = run_update(check=True, root=Path("/tmp/x"))
        self.assertEqual(code, EXIT_ERROR)

    def test_origin_missing_with_override_refused_exit_1(self):
        """H3: with an update repo override but no git origin at all, the
        update is refused (nothing to verify the override against)."""
        with (
            unittest.mock.patch(
                "phantomorg.updater.is_git_checkout", return_value=True
            ),
            unittest.mock.patch(
                "phantomorg.updater.remote_origin_repo", return_value=None
            ),
            unittest.mock.patch.dict(
                "os.environ",
                {"PHANTOMORG_UPDATE_REPO": "owner/repo"},
            ),
        ):
            code = run_update(check=True, root=Path("/tmp/x"))
        self.assertEqual(code, EXIT_ERROR)

    def test_merge_uses_double_dash_separator(self):
        """H3: the ff-only merge must use `--` so a tag can never be
        interpreted as a git option."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with unittest.mock.patch(
                "phantomorg.updater.subprocess.run",
                side_effect=[
                    unittest.mock.MagicMock(returncode=0, stdout="", stderr=""),
                    unittest.mock.MagicMock(returncode=0, stdout="", stderr=""),
                    unittest.mock.MagicMock(returncode=0, stdout="", stderr=""),
                    unittest.mock.MagicMock(returncode=0, stdout="", stderr=""),
                ],
            ) as mock_run:
                ok, message = apply_update(root, "v0.5.0")
        self.assertTrue(ok, message)
        merge_call = [c for c in mock_run.call_args_list if "merge" in c.args[0]]
        self.assertEqual(len(merge_call), 1)
        self.assertEqual(
            merge_call[0].args[0],
            ["git", "-C", str(root), "merge", "--ff-only", "--", "v0.5.0"],
        )

    def test_refresh_venv_uses_checkout_cwd(self):
        """C2: pip install -e . must run with cwd=root, not the caller's
        cwd — `po update` from /tmp must install THIS repo."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            venv_py = root / ".venv" / "bin" / "python"
            venv_py.parent.mkdir(parents=True)
            venv_py.touch()
            with unittest.mock.patch(
                "phantomorg.updater.subprocess.run",
                return_value=unittest.mock.MagicMock(
                    returncode=0, stdout="", stderr=""
                ),
            ) as mock_run:
                ok, message = refresh_venv(root)
        self.assertTrue(ok, message)
        call = mock_run.call_args
        self.assertIsNotNone(call)
        kwargs = call.kwargs or {}
        self.assertEqual(kwargs.get("cwd"), str(root))

    def test_force_update_applies(self):
        with (
            unittest.mock.patch(
                "phantomorg.updater.is_git_checkout", return_value=True
            ),
            unittest.mock.patch(
                "phantomorg.updater.remote_origin_repo", return_value="owner/repo"
            ),
            unittest.mock.patch(
                "phantomorg.updater.read_local_version",
                side_effect=["0.4.19", "0.5.0"],
            ),
            unittest.mock.patch(
                "phantomorg.updater.find_latest_release",
                return_value=unittest.mock.MagicMock(
                    ok=True,
                    release=unittest.mock.MagicMock(
                        version="0.5.0", tag="v0.5.0", published_at=None, body=""
                    ),
                ),
            ),
            unittest.mock.patch(
                "phantomorg.updater.apply_update",
                return_value=(True, "updated to v0.5.0"),
            ),
            unittest.mock.patch(
                "phantomorg.updater.refresh_venv", return_value=(True, "")
            ),
        ):
            code = run_update(force=True, root=Path("/tmp/x"))
        self.assertEqual(code, EXIT_OK)

    def test_pending_marker_written_before_merge_and_cleared_on_merge_failure(
        self,
    ):
        """G: apply_update records a pending-refresh marker BEFORE the
        merge and drops it when the merge fails (no HEAD change)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / ".pf-update-pending"
            with unittest.mock.patch(
                "phantomorg.updater.subprocess.run",
                side_effect=[
                    unittest.mock.MagicMock(returncode=0, stdout="", stderr=""),
                    unittest.mock.MagicMock(returncode=0, stdout="", stderr=""),
                    unittest.mock.MagicMock(returncode=0, stdout="", stderr=""),
                    unittest.mock.MagicMock(returncode=0, stdout="", stderr=""),
                ],
            ):
                ok, message = apply_update(root, "v0.5.0")
            self.assertTrue(ok, message)
            self.assertEqual(marker.read_text(encoding="utf-8").strip(), "v0.5.0")
            # Merge fails: marker must be cleared so a later `po update`
            # does not run a pointless repair refresh.
            with unittest.mock.patch(
                "phantomorg.updater.subprocess.run",
                side_effect=[
                    unittest.mock.MagicMock(returncode=0, stdout="", stderr=""),
                    unittest.mock.MagicMock(returncode=0, stdout="", stderr=""),
                    unittest.mock.MagicMock(returncode=0, stdout="", stderr=""),
                    unittest.mock.MagicMock(returncode=1, stdout="", stderr="conflict"),
                ],
            ):
                ok, message = apply_update(root, "v0.5.0")
            self.assertFalse(ok)
            self.assertFalse(marker.exists())

    def test_run_update_repairs_pending_marker(self):
        """G: a stale pending marker (crash after merge, before refresh) is
        self-healed by the next `po update` — even when already on the
        latest version."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / ".pf-update-pending"
            marker.write_text("v0.5.0\n", encoding="utf-8")
            with (
                unittest.mock.patch(
                    "phantomorg.updater.is_git_checkout", return_value=True
                ),
                unittest.mock.patch(
                    "phantomorg.updater.remote_origin_repo",
                    return_value="owner/repo",
                ),
                unittest.mock.patch(
                    "phantomorg.updater.read_local_version",
                    return_value="0.5.0",
                ),
                unittest.mock.patch(
                    "phantomorg.updater.find_latest_release",
                    return_value=unittest.mock.MagicMock(
                        ok=True,
                        release=unittest.mock.MagicMock(
                            version="0.5.0",
                            tag="v0.5.0",
                            published_at=None,
                            body="",
                        ),
                    ),
                ),
                unittest.mock.patch(
                    "phantomorg.updater.refresh_venv",
                    return_value=(True, "venv dependencies refreshed"),
                ) as mock_refresh,
                unittest.mock.patch(
                    "phantomorg.updater.apply_update",
                    return_value=(True, "updated to v0.5.0"),
                ),
            ):
                code = run_update(root=root)
            self.assertEqual(code, EXIT_OK)
            self.assertFalse(marker.exists(), "marker cleared after repair")
            mock_refresh.assert_called_once_with(root)

    def test_run_update_check_only_warns_on_pending_marker(self):
        """G: --check never mutates — a pending marker only warns."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / ".pf-update-pending"
            marker.write_text("v0.5.0\n", encoding="utf-8")
            with (
                unittest.mock.patch(
                    "phantomorg.updater.is_git_checkout", return_value=True
                ),
                unittest.mock.patch(
                    "phantomorg.updater.remote_origin_repo",
                    return_value="owner/repo",
                ),
                unittest.mock.patch(
                    "phantomorg.updater.read_local_version",
                    return_value="0.5.0",
                ),
                unittest.mock.patch(
                    "phantomorg.updater.find_latest_release",
                    return_value=unittest.mock.MagicMock(
                        ok=True,
                        release=unittest.mock.MagicMock(
                            version="0.5.0",
                            tag="v0.5.0",
                            published_at=None,
                            body="",
                        ),
                    ),
                ),
                unittest.mock.patch(
                    "phantomorg.updater.refresh_venv",
                    return_value=(True, "venv dependencies refreshed"),
                ) as mock_refresh,
            ):
                code = run_update(check=True, root=root)
            self.assertEqual(code, EXIT_OK)
            self.assertTrue(marker.exists(), "--check must not mutate")
            mock_refresh.assert_not_called()

    def test_run_update_keeps_marker_when_refresh_fails(self):
        """G: a failed repair keeps the marker so the next run retries."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / ".pf-update-pending"
            marker.write_text("v0.5.0\n", encoding="utf-8")
            with (
                unittest.mock.patch(
                    "phantomorg.updater.is_git_checkout", return_value=True
                ),
                unittest.mock.patch(
                    "phantomorg.updater.remote_origin_repo",
                    return_value="owner/repo",
                ),
                unittest.mock.patch(
                    "phantomorg.updater.read_local_version",
                    return_value="0.5.0",
                ),
                unittest.mock.patch(
                    "phantomorg.updater.find_latest_release",
                    return_value=unittest.mock.MagicMock(
                        ok=True,
                        release=unittest.mock.MagicMock(
                            version="0.5.0",
                            tag="v0.5.0",
                            published_at=None,
                            body="",
                        ),
                    ),
                ),
                unittest.mock.patch(
                    "phantomorg.updater.refresh_venv",
                    return_value=(False, "pip install -e . failed"),
                ),
            ):
                code = run_update(root=root)
            self.assertEqual(code, EXIT_OK)
            self.assertTrue(
                marker.exists(), "failed repair must keep the marker for retry"
            )

    def test_run_update_clears_marker_after_successful_refresh(self):
        """G: after a real update the marker is cleared once the venv
        refresh succeeded."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                unittest.mock.patch(
                    "phantomorg.updater.is_git_checkout", return_value=True
                ),
                unittest.mock.patch(
                    "phantomorg.updater.remote_origin_repo",
                    return_value="owner/repo",
                ),
                unittest.mock.patch(
                    "phantomorg.updater.read_local_version",
                    side_effect=["0.4.19", "0.5.0"],
                ),
                unittest.mock.patch(
                    "phantomorg.updater.find_latest_release",
                    return_value=unittest.mock.MagicMock(
                        ok=True,
                        release=unittest.mock.MagicMock(
                            version="0.5.0",
                            tag="v0.5.0",
                            published_at=None,
                            body="",
                        ),
                    ),
                ),
                unittest.mock.patch(
                    "phantomorg.updater.apply_update",
                    return_value=(True, "updated to v0.5.0"),
                ),
                unittest.mock.patch(
                    "phantomorg.updater.refresh_venv",
                    return_value=(True, "venv dependencies refreshed"),
                ),
            ):
                # apply_update is mocked, so plant the marker the way the
                # real flow would leave it after a crash (merge done,
                # refresh never ran).
                marker = root / ".pf-update-pending"
                marker.write_text("v0.5.0\n", encoding="utf-8")
                code = run_update(root=root, force=True)
            self.assertEqual(code, EXIT_OK)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
