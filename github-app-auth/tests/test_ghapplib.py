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

    @mock.patch('os.path.exists')
    @mock.patch('builtins.open', new_callable=mock.mock_open, read_data='export GITHUB_TOKEN="ghs_test_token"\n')
    def test_get_token_from_file(self, mock_file, mock_exists):
        mock_exists.return_value = True
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(ghapplib.get_token(), 'ghs_test_token')

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

if __name__ == '__main__':
    unittest.main()
