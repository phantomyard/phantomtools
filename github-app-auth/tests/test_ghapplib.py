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
    def test_get_token_prefers_env(self, mock_exists, mock_stat):
        """An already-exported GITHUB_TOKEN short-circuits the file read."""
        with mock.patch.dict(os.environ, {'GITHUB_TOKEN': 'ghs_env'}, clear=True):
            self.assertEqual(ghapplib.get_token(), 'ghs_env')
        mock_exists.assert_not_called()
        mock_stat.assert_not_called()

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


if __name__ == '__main__':
    unittest.main()
