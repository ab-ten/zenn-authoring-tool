"""日時の構文、意味値、分類済み operation と parser 統合を検証します。"""

from dataclasses import replace
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from converter.models import ArticlePlanningResult
from converter.planning import plan_articles
from converter.publication_dates import validate_publication_dates
from converter.source_parser import JST, parse_published_at, parse_source
from test_source_parser import ID, SLUG, FRONT, source


NOW = datetime(2026, 10, 11, tzinfo=JST)


class PublicationDateTests(unittest.TestCase):
  def ledger(self, state=None):
    return {"articles": {} if state is None else {ID: {
      "state": state, "slug": SLUG, "successful_release": 3}}, "pending_next_state": {}}

  def validate(self, parsed, prior=None, snapshot=None):
    snapshots = {} if snapshot is None else {ID: snapshot}
    plan = plan_articles([parsed.record], self.ledger(prior), snapshots)
    validate_publication_dates([parsed], plan, snapshots, now=NOW)
    return plan

  def test_absent_date_only_and_date_time(self):
    self.assertIsNone(parse_source(source()).published_at)
    for value in ("2026-10-11", "2026-10-11 00:00", '"2026-10-11"', "'2026-10-11 00:00'"):
      with self.subTest(value=value):
        self.assertEqual(parse_source(source(date=value)).published_at, NOW)

  def test_invalid_dates_times_and_formats(self):
    for value in ("2026-02-29", "2026-13-01", "0000-01-01", "2026-10-11 24:00",
                  "2026-10-11 12:60", "2026-1-01", "2026-10-11T00:00",
                  "2026-10-11 00:00:00", "2026-10-11T00:00:00+09:00",
                  "2026/10/11", "null", "123", "2026-10-11 0:00"):
      for state in ("active", "hold", "erased"):
        with self.subTest(value=value, state=state), self.assertRaises(ValueError):
          parse_source(source(date=value, state=state))

  def test_date_parser_rejects_nonstring_and_explicit_yaml_timestamp(self):
    for value in (None, 1, NOW, True):
      with self.subTest(value=value), self.assertRaises(ValueError):
        parse_published_at(value)
    with self.assertRaises(ValueError):
      parse_source(source(date="!!timestamp 2026-10-11"))

  def test_create_and_recreate_past_current_future(self):
    snapshot = parse_source(source(release=3)).record.candidate_content
    for prior in (None, "erased"):
      for date in (None, "2026-10-10", "2026-10-11", "2026-10-11 00:01"):
        parsed = parse_source(source(release=1 if prior is None else 4, date=date))
        # 同一 content の RECREATE は N のままです。
        if prior is not None and date is None:
          parsed = parse_source(source(release=3))
        with self.subTest(prior=prior, date=date):
          if date == "2026-10-11 00:01":
            with self.assertRaisesRegex(ValueError, "Future"):
              self.validate(parsed, prior, snapshot if prior else None)
          else:
            result = self.validate(parsed, prior, snapshot if prior else None)
            self.assertEqual(result.articles[0].operation, "CREATE" if prior is None else "RECREATE")

  def test_update_same_added_removed_and_changed(self):
    for old, new, valid in ((None, None, True), ("2026-10-11", "2026-10-11 00:00", True),
                            ("2026-10-11", "2026-10-11", True),
                            (None, "2026-10-11", False), ("2026-10-11", None, False),
                            ("2026-10-11", "2026-10-12", False)):
      snapshot = parse_source(source(date=old, release=3)).record.candidate_content
      parsed = parse_source(source(date=new, release=4, body="Changed body\n"))
      with self.subTest(old=old, new=new):
        if valid:
          self.assertEqual(self.validate(parsed, "active", snapshot).articles[0].operation, "UPDATE")
        else:
          with self.assertRaisesRegex(ValueError, "Established"):
            self.validate(parsed, "active", snapshot)

  def test_active_noop_is_validated(self):
    parsed = parse_source(source(date="2026-10-11", release=3))
    self.assertEqual(self.validate(parsed, "active", parsed.record.candidate_content).articles, ())
    # 分類済み no-op の日時検証自体にも snapshot 比較が存在することを確認します。
    changed_snapshot = parse_source(source(date="2026-10-12", release=3)).record.candidate_content
    with self.assertRaisesRegex(ValueError, "Established"):
      validate_publication_dates([parsed], ArticlePlanningResult(), {ID: changed_snapshot}, now=NOW)

  def test_delete_and_hold_skip_transition_validation(self):
    snapshot = parse_source(source(date="2026-10-10", release=3)).record.candidate_content
    for state in ("erased", "hold"):
      with self.subTest(state=state):
        parsed = parse_source(source(state=state, date="2099-01-01", release=3))
        plan = self.validate(parsed, "active", snapshot)
        self.assertEqual(tuple(e.operation for e in plan.articles),
                         ("DELETE",) if state == "erased" else ())

  def test_injected_now_requires_timezone_and_compares_instants(self):
    parsed = parse_source(source(date="2026-10-11"))
    plan = plan_articles([parsed.record], self.ledger(), {})
    validate_publication_dates([parsed], plan, {}, now=NOW.astimezone(timezone.utc))
    with self.assertRaises(ValueError):
      validate_publication_dates([parsed], plan, {}, now=NOW.replace(tzinfo=None))

  def test_parser_planner_integration_classifies_once(self):
    from converter import planning
    snapshot = parse_source(source(release=3)).record.candidate_content
    for body, release, expected in (("Body\n", 3, ()), ("Changed\n", 4, ("UPDATE",))):
      parsed = parse_source(source(body=body, release=release))
      with self.subTest(expected=expected), patch(
          "converter.planning._classify", wraps=planning._classify) as classify:
        result = self.validate(parsed, "active", snapshot)
        self.assertEqual(tuple(e.operation for e in result.articles), expected)
        classify.assert_called_once()
    with self.assertRaises(ValueError):
      self.validate(parse_source(source(body="Changed\n", release=3)), "active", snapshot)

  def test_plan_source_correspondence_and_missing_metadata(self):
    parsed = parse_source(source())
    plan = plan_articles([parsed.record], self.ledger(), {})
    with self.assertRaises(ValueError):
      validate_publication_dates([], plan, {}, now=NOW)
    different = replace(parsed, record=replace(parsed.record, candidate_content="different"))
    with self.assertRaises(ValueError):
      validate_publication_dates([different], plan, {}, now=NOW)
    with self.assertRaises(ValueError):
      validate_publication_dates([parsed, parsed], plan, {}, now=NOW)


if __name__ == "__main__":
  unittest.main()
