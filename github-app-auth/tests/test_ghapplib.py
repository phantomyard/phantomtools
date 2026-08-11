import unittest
import unittest.mock as mock
import os
import sys
import json
import io
import urllib.error

# Add bin dir to sys.path so we can import ghapplib
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'bin')))
import ghapplib

class TestGhappLib(unittest.TestCase):

    @mock.patch('os.path.isfile')
    @mock.patch('os.access')
    @mock.patch('shutil.which')
    def test_get_real_git_order(self, mock_which, mock_access, mock_isfile):
        # 1. Test REAL_GIT env var
        with mock.patch.dict(os.environ, {'REAL_GIT': '/path/to/real/git'}):
            mock_isfile.return_value = True
            mock_access.return_value = True
            self.assertEqual(ghapplib.get_real_git(), '/path/to/real/git')

        # 2. Test /usr/bin/git fallback
        with mock.patch.dict(os.environ, {}, clear=True):
            def isfile_side_effect(path):
                return path == '/usr/bin/git'
            mock_isfile.side_effect = isfile_side_effect
            mock_access.return_value = True
            self.assertEqual(ghapplib.get_real_git(), '/usr/bin/git')

    @mock.patch('os.stat')
    @mock.patch('os.path.exists')
    @mock.patch('builtins.open', new_callable=mock.mock_open, read_data='export GITHUB_TOKEN="ghs_test_token"\n')
    def test_get_token_from_file(self, mock_file, mock_exists, mock_stat):
        mock_exists.return_value = True
        mock_stat.return_value = mock.Mock(st_mode=0o100600)  # -rw-------
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(ghapplib.get_token(), 'ghs_test_token')

    @mock.patch('sys.stderr', new_callable=io.StringIO)
    @mock.patch('os.stat')
    @mock.patch('os.path.exists')
    @mock.patch('builtins.open', new_callable=mock.mock_open, read_data='export GITHUB_TOKEN="ghs_test_token"\n')
    def test_get_token_refuses_loose_permissions(self, mock_file, mock_exists, mock_stat, mock_stderr):
        """A group/world-readable token file is a leak — refuse to read it."""
        mock_exists.return_value = True
        mock_stat.return_value = mock.Mock(st_mode=0o100644)  # -rw-r--r--
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(ghapplib.get_token(), '')
        mock_file.assert_not_called()  # never opened the file
        self.assertIn('too open', mock_stderr.getvalue())

    @mock.patch('os.stat')
    @mock.patch('os.path.exists')
    @mock.patch('builtins.open', new_callable=mock.mock_open, read_data='export GITHUB_TOKEN="ghs_file"\n')
    def test_get_token_file_wins_over_env(self, mock_file, mock_exists, mock_stat):
        """The on-disk token is the source of truth: a long-lived process holds
        a stale GITHUB_TOKEN in its env, so the file must win over it."""
        mock_exists.return_value = True
        mock_stat.return_value = mock.Mock(st_mode=0o100600)  # -rw-------
        with mock.patch.dict(os.environ, {'GITHUB_TOKEN': 'ghs_stale_env'}, clear=True):
            self.assertEqual(ghapplib.get_token(), 'ghs_file')

    @mock.patch('os.path.exists', return_value=False)
    def test_get_token_falls_back_to_env_without_file(self, mock_exists):
        """Fresh install / CI: no ~/.github_env yet, so the env is the fallback."""
        with mock.patch.dict(os.environ, {'GITHUB_TOKEN': 'ghs_env'}, clear=True):
            self.assertEqual(ghapplib.get_token(), 'ghs_env')

    @mock.patch('os.stat')
    @mock.patch('os.path.exists', return_value=True)
    @mock.patch('builtins.open', new_callable=mock.mock_open, read_data='# no token line here\n')
    def test_get_token_falls_back_to_env_when_file_lacks_token(self, mock_file, mock_exists, mock_stat):
        """A file present but without a GITHUB_TOKEN line still falls back to env."""
        mock_stat.return_value = mock.Mock(st_mode=0o100600)  # -rw-------
        with mock.patch.dict(os.environ, {'GITHUB_TOKEN': 'ghs_env'}, clear=True):
            self.assertEqual(ghapplib.get_token(), 'ghs_env')

    @mock.patch('urllib.request.urlopen')
    def test_api_request_success(self, mock_urlopen):
        # Mock response
        mock_resp = mock.MagicMock()
        mock_resp.read.return_value = json.dumps({'sha': 'test_sha'}).encode('utf-8')
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        client = ghapplib.GitHubAppClient('owner', 'repo', 'token', 'git')
        resp = client.api_request('GET', 'git/refs/heads/main')
        
        self.assertEqual(resp['sha'], 'test_sha')
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.get_header('Authorization'), 'Bearer token')

    @mock.patch('ghapplib.run_git')
    @mock.patch('urllib.request.urlopen')
    def test_upload_tree_reuse(self, mock_urlopen, mock_run_git):
        client = ghapplib.GitHubAppClient('owner', 'repo', 'token', 'git')
        
        # Mock GET tree success (tree already exists)
        mock_resp = mock.MagicMock()
        mock_resp.read.return_value = json.dumps({'sha': 'existing_sha'}).encode('utf-8')
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        sha = client.upload_tree('existing_sha')
        
        self.assertEqual(sha, 'existing_sha')
        self.assertIn('existing_sha', client.remote_object_cache)
        # Should NOT have called ls-tree
        mock_run_git.assert_not_called()

    @mock.patch('ghapplib.run_git')
    @mock.patch('urllib.request.urlopen')
    def test_upload_tree_new(self, mock_urlopen, mock_run_git):
        client = ghapplib.GitHubAppClient('owner', 'repo', 'token', 'git')
        
        # 1. GET tree fails with 404
        mock_err = urllib.error.HTTPError('url', 404, 'Not Found', {}, io.BytesIO(b'{}'))
        
        # 2. POST blob success
        mock_blob_resp = mock.MagicMock()
        mock_blob_resp.read.return_value = json.dumps({'sha': 'blob_sha'}).encode('utf-8')
        mock_blob_resp.__enter__.return_value = mock_blob_resp
        
        # 3. POST tree success
        mock_tree_resp = mock.MagicMock()
        mock_tree_resp.read.return_value = json.dumps({'sha': 'new_tree_sha'}).encode('utf-8')
        mock_tree_resp.__enter__.return_value = mock_tree_resp

        mock_urlopen.side_effect = [mock_err, mock_blob_resp, mock_tree_resp]

        # Mock ls-tree output
        mock_run_git.side_effect = [
            mock.Mock(stdout='100644 blob blob_sha\tfile.txt\n'), # ls-tree
            mock.Mock(stdout=b'file content') # cat-file
        ]

        sha = client.upload_tree('local_sha')
        
        self.assertEqual(sha, 'new_tree_sha')
        self.assertEqual(mock_run_git.call_count, 2)
        # Check that metadata (100644) was preserved in POST tree
        last_post_data = json.loads(mock_urlopen.call_args_list[-1][0][0].data)
        self.assertEqual(last_post_data['tree'][0]['mode'], '100644')

    @mock.patch('ghapplib.run_git')
    @mock.patch('urllib.request.urlopen')
    def test_upload_tree_422_falls_through(self, mock_urlopen, mock_run_git):
        """GitHub returns 422 (not 404) for unknown tree SHAs on git/trees.
        Regression: the fast-path used to only catch 404 and crashed on 422."""
        client = ghapplib.GitHubAppClient('owner', 'repo', 'token', 'git')

        # 1. GET tree fails with 422 (unknown SHA on remote)
        mock_err = urllib.error.HTTPError(
            'url', 422, 'Unprocessable Entity', {}, io.BytesIO(b'{}')
        )

        # 2. POST tree success (empty tree path — no blobs to upload)
        mock_tree_resp = mock.MagicMock()
        mock_tree_resp.read.return_value = json.dumps({'sha': 'new_tree_sha'}).encode('utf-8')
        mock_tree_resp.__enter__.return_value = mock_tree_resp

        mock_urlopen.side_effect = [mock_err, mock_tree_resp]

        # ls-tree returns empty → triggers the empty-tree POST branch
        mock_run_git.return_value = mock.Mock(stdout='')

        sha = client.upload_tree('local_sha')

        self.assertEqual(sha, 'new_tree_sha')
        # Both calls happened: the failed GET, then the rebuild POST
        self.assertEqual(mock_urlopen.call_count, 2)

    @mock.patch('ghapplib.run_git')
    @mock.patch('urllib.request.urlopen')
    def test_upload_tree_other_http_error_propagates(self, mock_urlopen, mock_run_git):
        """Non-404/422 errors (e.g. 500, 403) should still bubble up."""
        client = ghapplib.GitHubAppClient('owner', 'repo', 'token', 'git')

        mock_err = urllib.error.HTTPError(
            'url', 500, 'Server Error', {}, io.BytesIO(b'{}')
        )
        mock_urlopen.side_effect = mock_err

        with self.assertRaises(urllib.error.HTTPError):
            client.upload_tree('local_sha')
        # Should NOT have tried to rebuild via ls-tree
        mock_run_git.assert_not_called()

    @mock.patch('ghapplib.run_git')
    @mock.patch('urllib.request.urlopen')
    def test_upload_tree_incremental_changed_only(self, mock_urlopen, mock_run_git):
        """With a base tree on the remote, only changed blobs are uploaded and
        deletions are sent as sha=None — no full re-upload of the whole tree."""
        client = ghapplib.GitHubAppClient('owner', 'repo', 'token', 'git')

        # 1. GET target tree → 404 (not on remote yet)
        target_miss = urllib.error.HTTPError('url', 404, 'Not Found', {}, io.BytesIO(b'{}'))
        # 2. GET base tree → success (base is on remote)
        base_ok = mock.MagicMock()
        base_ok.read.return_value = json.dumps({'sha': 'base_tree'}).encode('utf-8')
        base_ok.__enter__.return_value = base_ok
        # 3. POST blob for the one modified file
        blob_resp = mock.MagicMock()
        blob_resp.read.return_value = json.dumps({'sha': 'new_blob'}).encode('utf-8')
        blob_resp.__enter__.return_value = blob_resp
        # 4. POST tree → reconstructs the exact target tree
        tree_resp = mock.MagicMock()
        tree_resp.read.return_value = json.dumps({'sha': 'target_tree'}).encode('utf-8')
        tree_resp.__enter__.return_value = tree_resp

        mock_urlopen.side_effect = [target_miss, base_ok, blob_resp, tree_resp]

        diff = (":100644 100644 aaa bbb M\x00file.txt\x00"
                ":100644 000000 ccc 0000000000000000000000000000000000000000 D\x00gone.txt\x00")
        mock_run_git.side_effect = [
            mock.Mock(stdout=diff),            # diff-tree
            mock.Mock(stdout=b'new content'),  # cat-file for file.txt
        ]

        sha = client.upload_tree('target_tree', base_tree_sha='base_tree')

        self.assertEqual(sha, 'target_tree')
        # Only ONE blob uploaded despite there being a whole repo behind it.
        post_tree_data = json.loads(mock_urlopen.call_args_list[-1][0][0].data)
        self.assertEqual(post_tree_data['base_tree'], 'base_tree')
        by_path = {e['path']: e for e in post_tree_data['tree']}
        self.assertEqual(by_path['file.txt']['sha'], 'new_blob')
        self.assertIsNone(by_path['gone.txt']['sha'])  # deletion

    @mock.patch('ghapplib.run_git')
    @mock.patch('urllib.request.urlopen')
    def test_upload_tree_incremental_base_missing_falls_back(self, mock_urlopen, mock_run_git):
        """If the base tree isn't on the remote, fall back to a full rebuild."""
        client = ghapplib.GitHubAppClient('owner', 'repo', 'token', 'git')

        target_miss = urllib.error.HTTPError('url', 404, 'Not Found', {}, io.BytesIO(b'{}'))
        base_miss = urllib.error.HTTPError('url', 404, 'Not Found', {}, io.BytesIO(b'{}'))
        blob_resp = mock.MagicMock()
        blob_resp.read.return_value = json.dumps({'sha': 'blob_sha'}).encode('utf-8')
        blob_resp.__enter__.return_value = blob_resp
        tree_resp = mock.MagicMock()
        tree_resp.read.return_value = json.dumps({'sha': 'full_tree'}).encode('utf-8')
        tree_resp.__enter__.return_value = tree_resp

        # GET target → 404, GET base → 404, then full rebuild: POST blob, POST tree
        mock_urlopen.side_effect = [target_miss, base_miss, blob_resp, tree_resp]
        mock_run_git.side_effect = [
            mock.Mock(stdout='100644 blob blob_sha\tfile.txt\n'),  # ls-tree (full path)
            mock.Mock(stdout=b'file content'),                      # cat-file
        ]

        sha = client.upload_tree('target_tree', base_tree_sha='base_tree')

        self.assertEqual(sha, 'full_tree')
        # Full-rebuild POST has no base_tree key.
        post_tree_data = json.loads(mock_urlopen.call_args_list[-1][0][0].data)
        self.assertNotIn('base_tree', post_tree_data)

    @mock.patch('urllib.request.urlopen')
    def test_list_installation_repositories_single_page(self, mock_urlopen):
        # Mock response
        mock_resp = mock.MagicMock()
        mock_resp.read.return_value = json.dumps({
            'repositories': [{'full_name': 'org/repo1', 'clone_url': 'https://github.com/org/repo1.git'}]
        }).encode('utf-8')
        mock_resp.headers = {}
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        repos = ghapplib.list_installation_repositories('token')
        
        self.assertEqual(len(repos), 1)
        self.assertEqual(repos[0]['full_name'], 'org/repo1')
        mock_urlopen.assert_called_once()

    @mock.patch('urllib.request.urlopen')
    def test_list_installation_repositories_paginated(self, mock_urlopen):
        # Mock 1st page
        mock_resp1 = mock.MagicMock()
        mock_resp1.read.return_value = json.dumps({
            'repositories': [{'full_name': 'org/repo1'}]
        }).encode('utf-8')
        mock_resp1.headers = {'Link': '<https://api.github.com/installation/repositories?page=2>; rel="next"'}
        mock_resp1.__enter__.return_value = mock_resp1

        # Mock 2nd page
        mock_resp2 = mock.MagicMock()
        mock_resp2.read.return_value = json.dumps({
            'repositories': [{'full_name': 'org/repo2'}]
        }).encode('utf-8')
        mock_resp2.headers = {}
        mock_resp2.__enter__.return_value = mock_resp2

        mock_urlopen.side_effect = [mock_resp1, mock_resp2]

        repos = ghapplib.list_installation_repositories('token')
        
        self.assertEqual(len(repos), 2)
        self.assertEqual(repos[0]['full_name'], 'org/repo1')
        self.assertEqual(repos[1]['full_name'], 'org/repo2')
        self.assertEqual(mock_urlopen.call_count, 2)

    def test_parse_owner_repo(self):
        cases = {
            'https://github.com/org/repo.git': ('org', 'repo'),
            'https://github.com/org/repo': ('org', 'repo'),
            'git@github.com:org/repo.git': ('org', 'repo'),
            'https://github.com/org/sub.repo.name.git': ('org', 'sub.repo.name'),
        }
        for url, expected in cases.items():
            self.assertEqual(ghapplib.parse_owner_repo(url), expected, url)
        self.assertIsNone(ghapplib.parse_owner_repo('https://gitlab.com/org/repo.git'))

    @mock.patch('urllib.request.urlopen')
    def test_create_pull_request_success(self, mock_urlopen):
        mock_resp = mock.MagicMock()
        mock_resp.read.return_value = json.dumps({
            'number': 7,
            'html_url': 'https://github.com/org/repo/pull/7',
        }).encode('utf-8')
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        client = ghapplib.GitHubAppClient('org', 'repo', 'token', 'git')
        resp = client.create_pull_request('feature', 'main', 'My PR', body='hi')

        self.assertEqual(resp['number'], 7)
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.get_method(), 'POST')
        self.assertTrue(req.full_url.endswith('/repos/org/repo/pulls'))
        payload = json.loads(req.data)
        self.assertEqual(payload['head'], 'feature')
        self.assertEqual(payload['base'], 'main')
        self.assertEqual(payload['title'], 'My PR')
        self.assertEqual(payload['body'], 'hi')

    @mock.patch('sys.stderr', new_callable=io.StringIO)
    @mock.patch('urllib.request.urlopen')
    def test_create_pull_request_403_hints_permission(self, mock_urlopen, mock_stderr):
        """A 403/404 should still raise, but print a clear permission hint so the
        next bot doesn't go install gh out of confusion."""
        mock_urlopen.side_effect = urllib.error.HTTPError(
            'url', 403, 'Forbidden', {}, io.BytesIO(b'{}'))

        client = ghapplib.GitHubAppClient('org', 'repo', 'token', 'git')
        with self.assertRaises(urllib.error.HTTPError):
            client.create_pull_request('feature', 'main', 'My PR')

        self.assertIn('Pull requests', mock_stderr.getvalue())

    @mock.patch('urllib.request.urlopen')
    def test_get_default_branch(self, mock_urlopen):
        mock_resp = mock.MagicMock()
        mock_resp.read.return_value = json.dumps({'default_branch': 'trunk'}).encode('utf-8')
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        client = ghapplib.GitHubAppClient('org', 'repo', 'token', 'git')
        self.assertEqual(client.get_default_branch(), 'trunk')
        # Empty endpoint must hit the repo resource with no trailing slash.
        req = mock_urlopen.call_args[0][0]
        self.assertTrue(req.full_url.endswith('/repos/org/repo'))


class TestSelfHeal(unittest.TestCase):
    """Token expiry tracking + the one-shot refresh-on-401 self-heal."""

    def test_get_token_expiry_from_env(self):
        # No file present, so the env is the fallback source.
        with mock.patch('os.path.exists', return_value=False):
            with mock.patch.dict(os.environ,
                                 {'GITHUB_TOKEN_EXPIRES_AT': '2030-01-01T00:00:00Z'},
                                 clear=True):
                exp = ghapplib.get_token_expiry()
        self.assertIsNotNone(exp)
        self.assertEqual(exp.year, 2030)
        # 'Z' must be parsed as UTC, not dropped.
        self.assertIsNotNone(exp.tzinfo)

    @mock.patch('os.path.exists', return_value=True)
    @mock.patch('builtins.open', new_callable=mock.mock_open,
                read_data='export GITHUB_TOKEN_EXPIRES_AT="2031-01-01T00:00:00Z"\n')
    def test_get_token_expiry_file_wins_over_env(self, mock_file, mock_exists):
        """File expiry must win over a stale env copy, matching get_token()."""
        with mock.patch.dict(os.environ,
                             {'GITHUB_TOKEN_EXPIRES_AT': '2020-01-01T00:00:00Z'},
                             clear=True):
            exp = ghapplib.get_token_expiry()
        self.assertIsNotNone(exp)
        self.assertEqual(exp.year, 2031)

    def test_get_token_expiry_unknown(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch('os.path.exists', return_value=False):
                self.assertIsNone(ghapplib.get_token_expiry())

    def test_get_token_expiry_bad_value(self):
        with mock.patch('os.path.exists', return_value=False):
            with mock.patch.dict(os.environ,
                                 {'GITHUB_TOKEN_EXPIRES_AT': 'not-a-date'},
                                 clear=True):
                self.assertIsNone(ghapplib.get_token_expiry())

    def test_token_is_expired_past(self):
        with mock.patch.dict(os.environ,
                             {'GITHUB_TOKEN_EXPIRES_AT': '2000-01-01T00:00:00Z'},
                             clear=True):
            self.assertTrue(ghapplib.token_is_expired())

    def test_token_is_expired_future(self):
        with mock.patch.dict(os.environ,
                             {'GITHUB_TOKEN_EXPIRES_AT': '2999-01-01T00:00:00Z'},
                             clear=True):
            self.assertFalse(ghapplib.token_is_expired())

    def test_token_is_expired_unknown_is_false(self):
        """Unknown expiry must NOT trigger a refresh — we don't act on a hunch."""
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch('os.path.exists', return_value=False):
                self.assertFalse(ghapplib.token_is_expired())

    @mock.patch('ghapplib.get_token', return_value='ghs_fresh')
    @mock.patch('ghapplib.subprocess.run')
    @mock.patch('os.access', return_value=True)
    @mock.patch('os.path.isfile', return_value=True)
    def test_refresh_token_success(self, _isfile, _access, mock_run, _get):
        with mock.patch.dict(os.environ, {'GITHUB_TOKEN': 'stale'}, clear=True):
            token = ghapplib.refresh_token()
            # Stale process env must be cleared so get_token reads fresh.
            self.assertNotIn('GITHUB_TOKEN', os.environ)
        self.assertEqual(token, 'ghs_fresh')
        mock_run.assert_called_once()

    @mock.patch('os.path.isfile', return_value=False)
    def test_refresh_token_missing_script(self, _isfile):
        self.assertEqual(ghapplib.refresh_token(), '')

    @mock.patch('ghapplib.refresh_token')
    @mock.patch('ghapplib.get_token', return_value='')
    @mock.patch('ghapplib.token_is_expired', return_value=False)
    def test_ensure_token_refreshes_when_empty(self, _exp, _get, mock_refresh):
        mock_refresh.return_value = 'ghs_new'
        self.assertEqual(ghapplib.ensure_token(), 'ghs_new')
        mock_refresh.assert_called_once()

    @mock.patch('ghapplib.refresh_token')
    @mock.patch('ghapplib.get_token', return_value='ghs_old')
    @mock.patch('ghapplib.token_is_expired', return_value=True)
    def test_ensure_token_refreshes_when_expired(self, _exp, _get, mock_refresh):
        mock_refresh.return_value = 'ghs_new'
        self.assertEqual(ghapplib.ensure_token(), 'ghs_new')
        mock_refresh.assert_called_once()

    @mock.patch('ghapplib.refresh_token')
    @mock.patch('ghapplib.get_token', return_value='ghs_live')
    @mock.patch('ghapplib.token_is_expired', return_value=False)
    def test_ensure_token_no_refresh_when_healthy(self, _exp, _get, mock_refresh):
        self.assertEqual(ghapplib.ensure_token(), 'ghs_live')
        mock_refresh.assert_not_called()

    @mock.patch('ghapplib.refresh_token', return_value='ghs_new')
    @mock.patch('urllib.request.urlopen')
    def test_api_request_refreshes_on_401(self, mock_urlopen, mock_refresh):
        err = urllib.error.HTTPError('url', 401, 'Unauthorized', {}, io.BytesIO(b'{}'))
        ok = mock.MagicMock()
        ok.read.return_value = json.dumps({'ok': True}).encode('utf-8')
        ok.__enter__.return_value = ok
        mock_urlopen.side_effect = [err, ok]

        client = ghapplib.GitHubAppClient('o', 'r', 'ghs_dead', 'git')
        resp = client.api_request('GET', '')

        self.assertEqual(resp['ok'], True)
        mock_refresh.assert_called_once()
        # Client adopted the refreshed token, and the retry carried it.
        self.assertEqual(client.token, 'ghs_new')
        retry_req = mock_urlopen.call_args_list[-1][0][0]
        self.assertEqual(retry_req.get_header('Authorization'), 'Bearer ghs_new')

    @mock.patch('ghapplib.refresh_token', return_value='')
    @mock.patch('urllib.request.urlopen')
    def test_api_request_401_refresh_fails_raises(self, mock_urlopen, mock_refresh):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            'url', 401, 'Unauthorized', {}, io.BytesIO(b'{}'))
        client = ghapplib.GitHubAppClient('o', 'r', 'ghs_dead', 'git')
        with self.assertRaises(urllib.error.HTTPError):
            client.api_request('GET', '')
        mock_refresh.assert_called_once()

    @mock.patch('ghapplib.refresh_token')
    @mock.patch('urllib.request.urlopen')
    def test_api_request_403_does_not_refresh(self, mock_urlopen, mock_refresh):
        """403 is a permission problem — refreshing the token won't help, so
        the narrow self-heal must leave it alone."""
        mock_urlopen.side_effect = urllib.error.HTTPError(
            'url', 403, 'Forbidden', {}, io.BytesIO(b'{}'))
        client = ghapplib.GitHubAppClient('o', 'r', 'ghs_live', 'git')
        with self.assertRaises(urllib.error.HTTPError):
            client.api_request('GET', '')
        mock_refresh.assert_not_called()

    @mock.patch('ghapplib.refresh_token', return_value='ghs_new')
    @mock.patch('urllib.request.urlopen')
    def test_list_repos_refreshes_on_401(self, mock_urlopen, mock_refresh):
        err = urllib.error.HTTPError('url', 401, 'Unauthorized', {}, io.BytesIO(b'{}'))
        ok = mock.MagicMock()
        ok.read.return_value = json.dumps(
            {'repositories': [{'full_name': 'o/r'}]}).encode('utf-8')
        ok.headers = {}
        ok.__enter__.return_value = ok
        mock_urlopen.side_effect = [err, ok]

        repos = ghapplib.list_installation_repositories('ghs_dead')

        self.assertEqual(repos[0]['full_name'], 'o/r')
        mock_refresh.assert_called_once()
        retry_req = mock_urlopen.call_args_list[-1][0][0]
        self.assertEqual(retry_req.get_header('Authorization'), 'Bearer ghs_new')


class TestEnsureUserSystemdEnv(unittest.TestCase):
    def test_respects_existing_xdg_runtime_dir(self):
        env = {"XDG_RUNTIME_DIR": "/run/user/1000"}
        ready, auto_set, rt, reason = ghapplib.ensure_user_systemd_env(
            env=env, dir_exists=lambda p: True)
        self.assertTrue(ready)
        self.assertFalse(auto_set)
        self.assertEqual(rt, "/run/user/1000")
        self.assertIsNone(reason)
        # must not invent a DBUS address when XDG was already set
        self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", env)

    def test_derives_runtime_dir_when_unset_and_dir_exists(self):
        env = {"USER": "bot"}
        ready, auto_set, rt, reason = ghapplib.ensure_user_systemd_env(
            env=env, uid=1234, dir_exists=lambda p: True)
        self.assertTrue(ready)
        self.assertTrue(auto_set)
        self.assertEqual(rt, "/run/user/1234")
        self.assertEqual(env["XDG_RUNTIME_DIR"], "/run/user/1234")
        self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"],
                         "unix:path=/run/user/1234/bus")
        self.assertIsNone(reason)

    def test_not_ready_when_runtime_dir_missing(self):
        env = {"USER": "bot"}
        ready, auto_set, rt, reason = ghapplib.ensure_user_systemd_env(
            env=env, uid=1234, dir_exists=lambda p: False)
        self.assertFalse(ready)
        self.assertFalse(auto_set)
        self.assertIsNone(rt)
        self.assertIn("/run/user/1234", reason)
        self.assertIn("enable-linger bot", reason)
        # nothing mutated when we couldn't set up the bus
        self.assertNotIn("XDG_RUNTIME_DIR", env)

    def test_preserves_existing_dbus_address(self):
        env = {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/custom/bus"}
        ghapplib.ensure_user_systemd_env(
            env=env, uid=7, dir_exists=lambda p: True)
        self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"], "unix:path=/custom/bus")
        self.assertEqual(env["XDG_RUNTIME_DIR"], "/run/user/7")

    def test_empty_xdg_runtime_dir_is_treated_as_unset(self):
        env = {"XDG_RUNTIME_DIR": ""}
        ready, auto_set, rt, _ = ghapplib.ensure_user_systemd_env(
            env=env, uid=5, dir_exists=lambda p: True)
        self.assertTrue(ready)
        self.assertTrue(auto_set)
        self.assertEqual(rt, "/run/user/5")


class TestWrapperDiscovery(unittest.TestCase):
    """`github-app-auth list` derives its output from these two helpers."""

    def test_summary_from_python_docstring(self):
        text = '#!/usr/bin/env python3\n"""\nDo a thing via the API.\nUsage: x\n"""\n'
        self.assertEqual(ghapplib.wrapper_summary(text), "Do a thing via the API.")

    def test_summary_from_bash_banner(self):
        text = ("#!/usr/bin/env bash\n"
                "# ===========================\n"
                "# Pull via App auth\n"
                "# ===========================\n")
        self.assertEqual(ghapplib.wrapper_summary(text), "Pull via App auth")

    def test_summary_skips_shebang_and_blanks(self):
        text = "#!/bin/bash\n\n# Real description here\n"
        self.assertEqual(ghapplib.wrapper_summary(text), "Real description here")

    def test_summary_empty_when_no_header(self):
        self.assertEqual(ghapplib.wrapper_summary("#!/bin/bash\nset -e\n"), "")

    def test_list_wrappers_enumerates_executables_and_hides_plumbing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            def write(name, body, executable=True):
                p = os.path.join(d, name)
                with open(p, "w") as f:
                    f.write(body)
                if executable:
                    os.chmod(p, 0o755)
            write("git-push-as-app", '#!/usr/bin/env python3\n"""Push it."""\n')
            write("github-token.sh", "#!/bin/bash\n# prints a token\n")  # hidden
            write("ghapplib.py", '"""lib"""\n', executable=False)        # not exec
            write("notes.txt", "just text\n", executable=False)          # not exec

            result = dict(ghapplib.list_wrappers(d))
            self.assertIn("git-push-as-app", result)
            self.assertEqual(result["git-push-as-app"], "Push it.")
            self.assertNotIn("github-token.sh", result)  # in _DISCOVERY_HIDDEN
            self.assertNotIn("ghapplib.py", result)      # not executable
            self.assertNotIn("notes.txt", result)        # not executable

    def test_list_wrappers_missing_dir_returns_empty(self):
        self.assertEqual(ghapplib.list_wrappers("/no/such/dir/xyz"), [])


class TestDrift(unittest.TestCase):
    """Wrapper-drift detection — the pure logic plus a real temp-dir tree."""

    def test_drift_marker_is_stable(self):
        self.assertEqual(
            ghapplib.drift_marker("bot-inbox", "bot-inbox"),
            "<!-- report-drift:bot-inbox/bot-inbox -->")

    def test_unified_drift_identical_is_empty(self):
        self.assertEqual(ghapplib.unified_drift("a\nb\n", "a\nb\n", "x", "y"), "")

    def test_unified_drift_shows_change(self):
        diff = ghapplib.unified_drift(
            "line1\nold\n", "line1\nnew\n", "installed", "repo")
        self.assertIn("-old", diff)
        self.assertIn("+new", diff)
        self.assertIn("installed", diff)
        self.assertIn("repo", diff)

    def _make_repo(self):
        import tempfile
        root = tempfile.mkdtemp()
        # A proper tool: bin/ + install.sh.
        tool = os.path.join(root, "mytool")
        bindir = os.path.join(tool, "bin")
        os.makedirs(bindir)
        with open(os.path.join(tool, "install.sh"), "w") as f:
            f.write("#!/bin/bash\n")
        with open(os.path.join(bindir, "wrap"), "w") as f:
            f.write("#!/bin/bash\necho hi\n")
        # A dir that looks tool-ish but has no install.sh — must be skipped.
        nope = os.path.join(root, "notatool", "bin")
        os.makedirs(nope)
        with open(os.path.join(nope, "ghost"), "w") as f:
            f.write("x\n")
        return root

    def test_discover_skips_dirs_without_install_sh(self):
        root = self._make_repo()
        found = ghapplib.discover_tool_wrappers(root)
        names = [(t, s) for (t, s, _p) in found]
        self.assertIn(("mytool", "wrap"), names)
        self.assertNotIn(("notatool", "ghost"), names)

    def test_classify_missing_ok_drift_foreign(self):
        import tempfile
        root = self._make_repo()
        repo_wrap = os.path.join(root, "mytool", "bin", "wrap")
        local_bin = tempfile.mkdtemp()
        installed = os.path.join(local_bin, "wrap")

        # missing
        self.assertEqual(
            ghapplib.classify_wrapper(repo_wrap, installed)[0], "missing")
        # ok via symlink into repo
        os.symlink(repo_wrap, installed)
        self.assertEqual(
            ghapplib.classify_wrapper(repo_wrap, installed)[0], "ok")
        # foreign symlink
        os.remove(installed)
        other = os.path.join(local_bin, "other")
        with open(other, "w") as f:
            f.write("not ours\n")
        os.symlink(other, installed)
        self.assertEqual(
            ghapplib.classify_wrapper(repo_wrap, installed)[0], "foreign")
        # drift: regular file that differs
        os.remove(installed)
        with open(installed, "w") as f:
            f.write("#!/bin/bash\necho EDITED\n")
        self.assertEqual(
            ghapplib.classify_wrapper(repo_wrap, installed)[0], "drift")
        # ok: regular file identical to repo
        with open(installed, "w") as f:
            f.write("#!/bin/bash\necho hi\n")
        self.assertEqual(
            ghapplib.classify_wrapper(repo_wrap, installed)[0], "ok")

    def test_scan_drift_populates_diff(self):
        import tempfile
        root = self._make_repo()
        local_bin = tempfile.mkdtemp()
        with open(os.path.join(local_bin, "wrap"), "w") as f:
            f.write("#!/bin/bash\necho DRIFTED\n")
        records = ghapplib.scan_drift(root, local_bin)
        rec = next(r for r in records if r["script"] == "wrap")
        self.assertEqual(rec["status"], "drift")
        self.assertIn("DRIFTED", rec["diff"])
        self.assertIn("hi", rec["diff"])

    def test_find_open_issue_by_marker_skips_prs(self):
        client = ghapplib.GitHubAppClient("o", "r", "tok", "/usr/bin/git")
        marker = "<!-- report-drift:mytool/wrap -->"
        fake = [
            {"number": 1, "body": "unrelated"},
            {"number": 2, "body": f"x {marker} y", "pull_request": {}},  # a PR
            {"number": 3, "body": f"has {marker} here"},
        ]
        with mock.patch.object(client, "api_request", return_value=fake):
            hit = client.find_open_issue_by_marker(marker)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["number"], 3)

    def test_create_issue_posts_payload(self):
        client = ghapplib.GitHubAppClient("o", "r", "tok", "/usr/bin/git")
        with mock.patch.object(client, "api_request",
                               return_value={"number": 7}) as m:
            client.create_issue("title", "body", labels=["drift"])
        m.assert_called_once()
        args, _ = m.call_args
        self.assertEqual(args[0], "POST")
        self.assertEqual(args[1], "issues")
        self.assertEqual(args[2]["title"], "title")
        self.assertEqual(args[2]["labels"], ["drift"])


def _http_error(code, body=b'{"message":"boom"}'):
    return urllib.error.HTTPError(
        "https://api.github.com/x", code, "Reason", {}, io.BytesIO(body))


class TestQuietProbeCodes(unittest.TestCase):
    """Expected-miss probes must raise without printing `API error: …`.

    The noise is not cosmetic: git-push-as-app probes for a tree, a base tree
    and a not-yet-existing ref on every push, so an unconditional diagnostic
    prints errors on a completely healthy run — and trains the reader to skim
    past the one that actually matters.
    """

    def _client(self):
        return ghapplib.GitHubAppClient("o", "r", "ghs_x", "/usr/bin/git")

    def _call(self, code, **kwargs):
        """Run api_request against a urlopen that raises `code`; return stderr."""
        client = self._client()
        err = io.StringIO()
        with mock.patch('urllib.request.urlopen', side_effect=_http_error(code)), \
             mock.patch('sys.stderr', err):
            with self.assertRaises(urllib.error.HTTPError):
                client.api_request("GET", "git/trees/deadbeef", **kwargs)
        return err.getvalue()

    def test_quiet_code_is_silent(self):
        for code in (404, 422):
            with self.subTest(code=code):
                self.assertEqual(self._call(code, quiet_codes=(404, 422)), "")

    def test_unexpected_code_still_shouts(self):
        # The whole point: quieting the probes must not quiet a real failure.
        out = self._call(500, quiet_codes=(404, 422))
        self.assertIn("API error: 500", out)
        self.assertIn("boom", out)

    def test_403_not_quieted_by_404_probe(self):
        # A permission problem on a probe is real news — App lacks Contents:read.
        self.assertIn("API error: 403", self._call(403, quiet_codes=(404,)))

    def test_default_is_loud(self):
        # Callers that pass nothing keep the old behaviour verbatim.
        self.assertIn("API error: 404", self._call(404))

    def test_quiet_leaves_body_unread_for_caller(self):
        # We skip e.read() on a quiet code; prove the body survives so a caller
        # that wants to inspect it still can.
        client = self._client()
        exc = _http_error(422, b'{"message":"unresolvable"}')
        with mock.patch('urllib.request.urlopen', side_effect=exc), \
             mock.patch('sys.stderr', io.StringIO()):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                client.api_request("GET", "git/trees/x", quiet_codes=(422,))
        self.assertIn(b"unresolvable", ctx.exception.read())

    def test_quiet_codes_survive_token_refresh(self):
        # A 401 refreshes the token and retries once; the retry must inherit
        # quiet_codes or the second attempt goes loud for no reason.
        client = self._client()
        err = io.StringIO()
        with mock.patch('urllib.request.urlopen',
                        side_effect=[_http_error(401), _http_error(404)]), \
             mock.patch.object(ghapplib, 'refresh_token', return_value="ghs_new"), \
             mock.patch('sys.stderr', err):
            with self.assertRaises(urllib.error.HTTPError):
                client.api_request("GET", "git/refs/heads/new", quiet_codes=(404,))
        self.assertNotIn("API error: 404", err.getvalue())

    # --- the call sites, so the wiring cannot silently regress -------------

    def test_tree_existence_probe_is_quiet(self):
        """upload_tree's fast-path probe 422s for any local-only tree SHA."""
        client = self._client()
        err = io.StringIO()
        with mock.patch.object(client, 'api_request',
                              side_effect=_http_error(422)) as api, \
             mock.patch.object(client, '_upload_tree_full',
                              return_value="rebuilt") as full, \
             mock.patch('sys.stderr', err):
            self.assertEqual(client.upload_tree("deadbeef"), "rebuilt")
        full.assert_called_once()
        _, kwargs = api.call_args
        self.assertEqual(kwargs.get("quiet_codes"), (404, 422))

    def test_base_tree_probe_is_quiet(self):
        client = self._client()
        with mock.patch.object(client, 'api_request',
                              side_effect=_http_error(404)) as api:
            with self.assertRaises(urllib.error.HTTPError):
                client._upload_tree_incremental("tree", "base")
        _, kwargs = api.call_args
        self.assertEqual(kwargs.get("quiet_codes"), (404, 422))

    def test_push_wrapper_ref_probes_are_quiet(self):
        """Both `git/refs/heads/{branch}` probes in git-push-as-app pass 404.

        Source-level assertion, not behavioural: those two calls live inside
        main() behind argv parsing and a live token, so a unit test cannot
        reach them. Crude, but it fails loudly if someone drops the kwarg and
        brings back `API error: 404` on every push to a new branch.
        """
        path = os.path.join(os.path.dirname(__file__), '..', 'bin',
                            'git-push-as-app')
        with open(path) as fh:
            src = fh.read()
        probes = [ln for ln in src.splitlines()
                  if 'api_request("GET", f"git/refs/heads/' in ln]
        self.assertEqual(len(probes), 2, f"probe count changed: {probes}")
        for ln in probes:
            idx = src.index(ln)
            # quiet_codes may sit on the following continuation line.
            window = src[idx:idx + len(ln) + 120]
            self.assertIn("quiet_codes=(404,)", window)


if __name__ == '__main__':
    unittest.main()
