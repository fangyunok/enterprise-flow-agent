"""Business invariants against real SQLite transactions and seed data."""
from __future__ import annotations

import concurrent.futures
import tempfile
import unittest
from pathlib import Path

from enterprise_flow.schemas import Principal
from enterprise_flow.service import DomainError, EnterpriseService


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "business.sqlite"
        self.service = EnterpriseService(self.path)
        self.service.seed_demo()
        self.alice = self.service.authenticate_demo("alice")
        self.bob = self.service.authenticate_demo("bob")
        self.diana = self.service.authenticate_demo("diana")
        self.fields = {"order_ids": ["alice-hotel-001", "alice-train-001"], "cost_center": "CC-ALPHA-OPS"}

    def assert_error(self, code, operation):
        with self.assertRaises(DomainError) as raised:
            operation()
        self.assertEqual(raised.exception.code, code)

    def draft(self):
        return self.service.create_draft(self.alice, self.fields)

    def confirm(self, draft):
        return self.service.confirm_draft(self.alice, draft["draft_id"], draft["version"], draft["content_hash"])

    def test_amounts_sources_and_real_nights(self):
        result = self.service.evaluate_expense(self.alice, {**self.fields, "start_date": "2026-10-09", "end_date": "2026-10-11", "destination": "广州"})
        self.assertEqual((result["total_cents"], result["eligible_cents"], result["excess_cents"]), (129000, 123000, 6000))
        self.assertEqual({s["policy_id"] for s in result["sources"]}, {"alpha-hotel-current", "alpha-train"})
        hotel = next(item for item in result["items"] if item["kind"] == "hotel")
        self.assertEqual(len(hotel["breakdown"]), 2)

    def test_stay_crossing_policy_change_splits_by_night(self):
        result = self.service.evaluate_expense(self.alice, {"order_ids": ["alice-hotel-old"], "cost_center": "CC-ALPHA-OPS"})
        self.assertEqual(result["eligible_cents"], 110000)
        self.assertEqual([b["policy_version"] for b in result["items"][0]["breakdown"]], ["2026-01", "2026-01", "2026-10"])

    def test_department_exception_and_tenant_are_independent(self):
        bob = self.service.evaluate_expense(self.bob, {"order_ids": ["bob-hotel-001"], "cost_center": "CC-ALPHA-ENG"})
        diana = self.service.evaluate_expense(self.diana, {"order_ids": ["diana-hotel-001"], "cost_center": "CC-BETA-OPS"})
        self.assertEqual(bob["eligible_cents"], 86000)
        self.assertEqual(diana["eligible_cents"], 60000)
        self.assertTrue(all(p["tenant_id"] == "beta" for p in diana["sources"]))

    def test_acl_applies_before_policy_ranking_and_order_access(self):
        self.assertTrue(all(row["tenant_id"] == "alpha" and row["department_scope"] != "engineering" for row in self.service.search_policies(self.alice, "住宿")))
        self.assertTrue(all(row["owner_id"] == "alice" for row in self.service.list_orders(self.alice)))
        self.assert_error("order_not_found", lambda: self.service.evaluate_expense(self.alice, {**self.fields, "order_ids": ["bob-hotel-001"]}))
        draft = self.draft()
        self.assert_error("draft_not_found", lambda: self.service.get_draft(self.bob, draft["draft_id"]))

    def test_forged_principal_and_forbidden_fields_rejected(self):
        forged = Principal(user_id="alice", tenant_id="beta", department_id="operations", display_name=self.alice.display_name)
        self.assert_error("identity_invalid", lambda: self.service.list_orders(forged))
        self.assert_error("invalid_input", lambda: self.service.create_draft(self.alice, {**self.fields, "owner_id": "bob"}))
        self.assert_error("cost_center_denied", lambda: self.service.evaluate_expense(self.alice, {**self.fields, "cost_center": "CC-BETA-OPS"}))

    def test_invalid_receipt_cancelled_and_mismatched_dates(self):
        for order_id in ["alice-no-receipt", "alice-cancelled"]:
            self.assert_error("order_ineligible", lambda: self.service.evaluate_expense(self.alice, {**self.fields, "order_ids": [order_id]}))
        self.assert_error("order_scope_mismatch", lambda: self.service.evaluate_expense(self.alice, {**self.fields, "start_date": "2026-10-10"}))

    def test_duplicate_orders_and_boolean_versions_rejected(self):
        self.assert_error("invalid_input", lambda: self.service.create_draft(self.alice, {**self.fields, "order_ids": ["alice-train-001"] * 2}))
        draft = self.draft()
        self.assert_error("invalid_version", lambda: self.service.confirm_draft(self.alice, draft["draft_id"], True))

    def test_invalid_order_date_filters_fail_with_domain_error(self):
        self.assert_error("invalid_date", lambda: self.service.list_orders(self.alice, start_date="bad"))
        self.assert_error("invalid_date", lambda: self.service.list_orders(self.alice, start_date="2026-10-11", end_date="2026-10-09"))

    def test_submit_requires_explicit_confirmation(self):
        draft = self.draft()
        self.assert_error("confirmation_required", lambda: self.service.submit_draft(self.alice, draft["draft_id"], 1, "key-1"))

    def test_edit_increments_version_and_invalidates_confirmation(self):
        draft = self.confirm(self.draft())
        updated = self.service.edit_draft(self.alice, draft["draft_id"], {"notes": "修改说明"}, 1)
        self.assertEqual(updated["version"], 2)
        self.assertIsNone(updated["confirmation"])
        self.assert_error("stale_version", lambda: self.service.submit_draft(self.alice, draft["draft_id"], 1, "key"))
        self.assert_error("confirmation_required", lambda: self.service.submit_draft(self.alice, draft["draft_id"], 2, "key"))

    def test_preconfirmation_source_change_rejected(self):
        draft = self.draft()
        with self.service.database.transaction(write=True) as connection:
            connection.execute("UPDATE policies SET content=content || ' 修改条款。' WHERE policy_id='alpha-hotel-current'")
        self.assert_error("stale_draft", lambda: self.confirm(draft))

    def test_policy_change_after_confirmation_blocks_submission(self):
        draft = self.confirm(self.draft())
        with self.service.database.transaction(write=True) as connection:
            connection.execute("UPDATE policies SET cap_cents=30000 WHERE policy_id='alpha-hotel-current'")
        self.assert_error("stale_draft", lambda: self.service.submit_draft(self.alice, draft["draft_id"], 1, "key"))

    def test_order_change_after_confirmation_blocks_submission(self):
        draft = self.confirm(self.draft())
        with self.service.database.transaction(write=True) as connection:
            connection.execute("UPDATE orders SET amount_cents=90000 WHERE order_id='alice-hotel-001'")
        self.assert_error("stale_draft", lambda: self.service.submit_draft(self.alice, draft["draft_id"], 1, "key"))

    def test_policy_conflict_is_not_arbitrarily_resolved(self):
        with self.service.database.transaction(write=True) as connection:
            connection.execute("INSERT INTO policies SELECT 'conflicting-policy',tenant_id,department_scope,kind,city,version,effective_from,effective_to,clause_id,title,content,cap_cents FROM policies WHERE policy_id='alpha-hotel-current'")
        self.assert_error("policy_conflict", lambda: self.draft())

    def test_same_and_different_keys_never_duplicate_application(self):
        draft = self.confirm(self.draft())
        first = self.service.submit_draft(self.alice, draft["draft_id"], 1, "original")
        self.assertEqual(first, self.service.submit_draft(self.alice, draft["draft_id"], 1, "original"))
        self.assertEqual(first, self.service.submit_draft(self.alice, draft["draft_id"], 1, "different"))
        self.assert_error("already_submitted", lambda: self.service.edit_draft(self.alice, draft["draft_id"], {"notes": "change"}, 1))

    def test_concurrent_submissions_one_row_and_one_identifier(self):
        draft = self.confirm(self.draft())
        def submit(index):
            service = EnterpriseService(self.path)
            return service.submit_draft(self.alice, draft["draft_id"], 1, "parallel-" + str(index))["submission_id"]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            identifiers = list(executor.map(submit, range(8)))
        self.assertEqual(len(set(identifiers)), 1)
        with self.service.database.transaction() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM submissions").fetchone()[0], 1)

    def test_reusing_key_for_different_draft_rejected(self):
        first, second = self.confirm(self.draft()), self.confirm(self.draft())
        self.service.submit_draft(self.alice, first["draft_id"], 1, "shared")
        self.assert_error("idempotency_conflict", lambda: self.service.submit_draft(self.alice, second["draft_id"], 1, "shared"))

    def test_preferences_require_confirmation_and_are_isolated(self):
        self.assert_error("preference_confirmation_required", lambda: self.service.set_preference(self.alice, "cost_center", "CC-ALPHA-OPS", confirmed=False))
        self.service.set_preference(self.alice, "cost_center", "CC-ALPHA-OPS")
        self.assertEqual(self.service.get_preferences(self.bob), [])
        result = self.service.evaluate_expense(self.alice, {"order_ids": ["alice-train-001"]})
        self.assertEqual(result["input"]["cost_center"], "CC-ALPHA-OPS")
        explicit = self.service.evaluate_expense(self.alice, {"order_ids": ["alice-train-001"], "cost_center": "CC-ALPHA-GENERAL"})
        self.assertEqual(explicit["input"]["cost_center"], "CC-ALPHA-GENERAL")
        self.service.delete_preference(self.alice, "cost_center")
        self.assertEqual(self.service.get_preferences(self.alice), [])

    def test_restart_preserves_draft_preferences_and_result(self):
        self.service.set_preference(self.alice, "cost_center", "CC-ALPHA-OPS")
        draft = self.confirm(self.draft())
        result = self.service.submit_draft(self.alice, draft["draft_id"], 1, "before-crash")
        restarted = EnterpriseService(self.path)
        restarted.seed_demo()
        self.assertEqual(restarted.get_preferences(self.alice)[0]["value"], "CC-ALPHA-OPS")
        self.assertEqual(restarted.submit_draft(self.alice, draft["draft_id"], 1, "before-crash"), result)

    def test_task_owner_and_request_id_isolation(self):
        original = self.service.create_run(self.alice, "run-1", "fixture", "message", request_id="same")
        self.assertEqual(original, self.service.create_run(self.alice, "run-2", "fixture", "message", request_id="same"))
        self.assert_error("request_conflict", lambda: self.service.create_run(self.alice, "run-3", "fixture", "other", request_id="same"))
        self.assert_error("run_not_found", lambda: self.service.get_run(self.bob, "run-1"))
        self.assert_error("invalid_run_update", lambda: self.service.update_run(self.alice, "run-1", owner_id="bob"))


if __name__ == "__main__":
    unittest.main()
