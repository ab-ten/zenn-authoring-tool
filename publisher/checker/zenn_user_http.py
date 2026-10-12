"""初期化専用の read-only Zenn User API adapter を提供します。"""

from dataclasses import dataclass
import json
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


_USERNAME = re.compile(r"[A-Za-z0-9_-]+\Z")
_MAX_RESPONSE = 1024 * 1024


def validate_username(username: str) -> str:
  """URL path に安全な非空の username を返します。不正な入力では ValueError を送出します。

  副作用はありません。アカウントの存在は User API で確認します。
  """
  if not isinstance(username, str) or not _USERNAME.fullmatch(username):
    raise ValueError("Invalid Zenn username")
  return username


@dataclass(frozen=True)
class ZennUser:
  """検証済みの正の整数 ID と username を保持します。

  作成時に schema を検証し、不正な値では ValueError を送出します。副作用はありません。
  """

  id: int
  username: str

  def __post_init__(self):
    if type(self.id) is not int or self.id <= 0:
      raise ValueError("Invalid numeric Zenn user.id")
    validate_username(self.username)


def fetch_user(username: str) -> ZennUser:
  """username を認証不要の GET で一度解決し、検証済み ZennUser を返します。

  15 秒の timeout と response size 制限を設けます。HTTP 200 以外、通信失敗、
  不正 JSON/schema は OSError または ValueError を送出します。ファイルは変更しません。
  非公開 endpoint の schema に関する仮定はこの adapter に局所化します。
  """
  validate_username(username)
  request = Request(f"https://zenn.dev/api/users/{username}", method="GET",
                    headers={"Accept": "application/json", "User-Agent": "zenn-authoring-tool/0"})
  try:
    with urlopen(request, timeout=15) as response:
      if type(response.status) is not int or response.status != 200:
        raise ValueError(f"Zenn User API returned HTTP {response.status}")
      body = response.read(_MAX_RESPONSE + 1)
  except HTTPError as exc:
    raise ValueError(f"Zenn User API returned HTTP {exc.code}") from exc
  except URLError as exc:
    raise OSError("Cannot resolve Zenn user") from exc
  if len(body) > _MAX_RESPONSE:
    raise ValueError("Zenn User API response exceeds size limit")
  try:
    payload = json.loads(body)
  except (UnicodeError, json.JSONDecodeError) as exc:
    raise ValueError("Zenn User API returned invalid JSON") from exc
  user = payload.get("user") if isinstance(payload, dict) else None
  if not isinstance(user, dict):
    raise ValueError("Zenn User API response is missing a user object")
  return ZennUser(user.get("id"), user.get("username"))
