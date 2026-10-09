"""ローカル状態の検証・適用に共通の例外を定義します。"""


class LocalApplyError(ValueError):
  """ローカル状態が安全な検証・適用の条件を満たしていません。"""
