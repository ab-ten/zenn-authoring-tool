"""ネットワークに接続せず User API の GET と schema 検証を確認します。"""

import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from checker import zenn_user_http as adapter
from test_zenn_http import FakeResponse


class ZennUserHttpTests(unittest.TestCase):
  def test_valid_response_is_bounded_get_without_credentials(self):
    payload = {'user': {'id': 12345, 'username': 'resolved_user', 'name': 'Example'}}
    with patch.object(adapter, 'urlopen', return_value=FakeResponse(200, json.dumps(payload).encode())) as run:
      user = adapter.fetch_user('requested_user')
    self.assertEqual(user, adapter.ZennUser(12345, 'resolved_user'))
    request = run.call_args.args[0]
    self.assertEqual(request.full_url, 'https://zenn.dev/api/users/requested_user')
    self.assertEqual(request.get_method(), 'GET')
    self.assertIsNone(request.data)
    self.assertNotIn('Authorization', request.headers)
    self.assertNotIn('Cookie', request.headers)
    self.assertEqual(run.call_args.kwargs['timeout'], 15)
    run.assert_called_once()

  def test_non_200_is_error(self):
    for status in (404, 403, 429, 500, 503):
      with self.subTest(status=status), patch.object(adapter, 'urlopen',
          side_effect=HTTPError('https://zenn.dev/api/users/example', status, 'failed', {}, None)):
        with self.assertRaisesRegex(ValueError, str(status)):
          adapter.fetch_user('example')
    with patch.object(adapter, 'urlopen', return_value=FakeResponse(302, b'')):
      with self.assertRaisesRegex(ValueError, '302'):
        adapter.fetch_user('example')

  def test_transport_and_timeout(self):
    for error in (URLError('private transport detail'), TimeoutError('timeout'), OSError('network')):
      with self.subTest(error=error), patch.object(adapter, 'urlopen', side_effect=error):
        with self.assertRaises(OSError):
          adapter.fetch_user('example')

  def test_invalid_json_and_encoding(self):
    for body in (b'not json', b'\xff'):
      with self.subTest(body=body), patch.object(adapter, 'urlopen', return_value=FakeResponse(200, body)):
        with self.assertRaisesRegex(ValueError, 'invalid JSON'):
          adapter.fetch_user('example')

  def test_invalid_schema_and_identity(self):
    payloads = [None, [], {}, {'user': None}, {'user': []}, {'user': {}},
                {'user': {'id': 1}}, {'user': {'username': 'example'}}]
    payloads.extend({'user': {'id': value, 'username': 'example'}}
                    for value in (True, False, 0, -1, 1.5, '1', None))
    payloads.extend({'user': {'id': 1, 'username': value}}
                    for value in ('', None, 1, '../unsafe', 'a/b', 'a?token=x', 'a\nb'))
    for payload in payloads:
      with self.subTest(payload=payload), patch.object(adapter, 'urlopen',
          return_value=FakeResponse(200, json.dumps(payload).encode())):
        with self.assertRaises(ValueError):
          adapter.fetch_user('example')

  def test_unsafe_username_does_not_request(self):
    with patch.object(adapter, 'urlopen') as run:
      for username in ('', '.', '..', '../x', 'a/b', 'a\\b', 'a?x', 'a#x', 'a%2fb', 'a b', 'a\nb', None):
        with self.subTest(username=username), self.assertRaises(ValueError):
          adapter.fetch_user(username)
      run.assert_not_called()

  def test_oversized_response_is_error(self):
    with patch.object(adapter, '_MAX_RESPONSE', 10), patch.object(adapter, 'urlopen',
        return_value=FakeResponse(200, b'x' * 11)):
      with self.assertRaisesRegex(ValueError, 'size limit'):
        adapter.fetch_user('example')
