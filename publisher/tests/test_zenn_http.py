"""No-network tests for the Zenn HTTP GET adapter."""

from io import BytesIO
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from checker.zenn_http import fetch_article


SLUG = "p0-create-test-article"


class FakeResponse:
  def __init__(self, status, body):
    self.status = status
    self.stream = BytesIO(body)

  def __enter__(self):
    return self

  def __exit__(self, *_):
    self.stream.close()

  def read(self, max_bytes):
    return self.stream.read(max_bytes)


class ZennHttpTests(unittest.TestCase):
  def test_200_json_decoded(self):
    doc = {"article": {"slug": SLUG}}
    with patch("checker.zenn_http.urlopen",
               return_value=FakeResponse(200, json.dumps(doc).encode("utf-8"))) as op:
      result = fetch_article(SLUG)
    self.assertEqual(result.status, 200)
    self.assertEqual(result.payload, doc)
    req = op.call_args.args[0]
    self.assertEqual(req.full_url, f"https://zenn.dev/api/articles/{SLUG}")
    self.assertEqual(req.get_method(), "GET")

  def test_404_is_returned_for_classification(self):
    err = HTTPError(f"https://zenn.dev/api/articles/{SLUG}", 404,
                    "Not Found", {}, None)
    with patch("checker.zenn_http.urlopen", side_effect=err):
      result = fetch_article(SLUG)
    self.assertEqual(result.status, 404)
    self.assertIsNone(result.payload)

  def test_503_is_returned_for_error_classification(self):
    err = HTTPError(f"https://zenn.dev/api/articles/{SLUG}", 503,
                    "Unavailable", {}, None)
    with patch("checker.zenn_http.urlopen", side_effect=err):
      self.assertEqual(fetch_article(SLUG).status, 503)

  def test_url_error_raises_io_error(self):
    with patch("checker.zenn_http.urlopen", side_effect=URLError("failed")):
      with self.assertRaises(OSError):
        fetch_article(SLUG)

  def test_bad_json_is_not_accepted(self):
    with patch("checker.zenn_http.urlopen", return_value=FakeResponse(200, b"not json")):
      with self.assertRaises(ValueError):
        fetch_article(SLUG)

  def test_invalid_slug_does_not_issue_request(self):
    with patch("checker.zenn_http.urlopen") as op:
      with self.assertRaises(ValueError):
        fetch_article("../../not-an-article")
    op.assert_not_called()

  def test_oversized_payload_is_rejected(self):
    with patch("checker.zenn_http._MAX_RESPONSE", 10):
      with patch("checker.zenn_http.urlopen", return_value=FakeResponse(200, b"x" * 11)):
        with self.assertRaises(ValueError):
          fetch_article(SLUG)


if __name__ == "__main__":
  unittest.main()
