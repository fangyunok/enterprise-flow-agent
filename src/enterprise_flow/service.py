"""Authorized business operations with atomic, version-bound submission."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from pydantic import ValidationError

from .database import Database
from .schemas import DraftInput, Principal, canonical_json


class DomainError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code, self.message, self.status_code = code, message, status_code


def _now():
    return datetime.now(timezone.utc).isoformat()


def _hash(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _version(value):
    if type(value) is not int or value < 1:
        raise DomainError("invalid_version", "版本必须是正整数。")


class EnterpriseService:
    def __init__(self, database_path: Path):
        self.database = Database(database_path)
        self.database_path = self.database.path

    def seed_demo(self):
        from .resources import data_path
        seed = json.loads(data_path("demo_seed.json").read_text(encoding="utf-8"))
        columns = {
            "users": "user_id,tenant_id,department_id,display_name",
            "cost_centers": "code,tenant_id,department_scope,label",
            "policies": "policy_id,tenant_id,department_scope,kind,city,version,effective_from,effective_to,clause_id,title,content,cap_cents",
            "orders": "order_id,owner_id,tenant_id,kind,city,start_date,end_date,amount_cents,currency,receipt_valid,status",
        }
        with self.database.transaction(write=True) as connection:
            for table, names in columns.items():
                keys = names.split(",")
                for row in seed[table]:
                    connection.execute(f"INSERT OR IGNORE INTO {table} ({names}) VALUES ({','.join('?' for _ in keys)})", [row[key] for key in keys])
        return {"dataset_version": seed["dataset_version"], "synthetic": True}

    def list_demo_users(self):
        with self.database.transaction() as connection:
            return [dict(row) for row in connection.execute("SELECT user_id,tenant_id,department_id,display_name FROM users WHERE active=1 ORDER BY user_id")]

    def authenticate_demo(self, user_id: str) -> Principal:
        with self.database.transaction() as connection:
            row = connection.execute("SELECT * FROM users WHERE user_id=? AND active=1", (user_id,)).fetchone()
            if row is None:
                raise DomainError("identity_not_found", "该身份不存在。", 401)
            return Principal(**{key: row[key] for key in Principal.model_fields})

    def _principal(self, connection, principal):
        if not isinstance(principal, Principal):
            raise DomainError("identity_invalid", "身份上下文无效。", 401)
        row = connection.execute("SELECT * FROM users WHERE user_id=? AND active=1", (principal.user_id,)).fetchone()
        if row is None or any(row[key] != getattr(principal, key) for key in Principal.model_fields):
            raise DomainError("identity_invalid", "身份上下文已失效。", 401)

    def _event(self, connection, principal, action, resource_id, details):
        connection.execute("INSERT INTO events(owner_id,action,resource_id,details_json,at_utc) VALUES (?,?,?,?,?)", (principal.user_id, action, resource_id, canonical_json(details), _now()))

    def _policy_rows(self, connection, principal, business_date):
        return [dict(row) for row in connection.execute(
            "SELECT * FROM policies WHERE tenant_id=? AND department_scope IN (?, '*') AND effective_from<=? AND (effective_to IS NULL OR effective_to>?) ORDER BY policy_id",
            (principal.tenant_id, principal.department_id, business_date, business_date),
        )]

    def search_policies(self, principal, query="", trip_date="2026-10-09"):
        try:
            business_date = date.fromisoformat(str(trip_date)).isoformat()
        except ValueError:
            raise DomainError("invalid_date", "制度查询日期需要 YYYY-MM-DD。") from None
        if not isinstance(query, str) or len(query) > 500:
            raise DomainError("invalid_query", "查询最多 500 个字符。")
        with self.database.transaction() as connection:
            self._principal(connection, principal)
            rows = self._policy_rows(connection, principal, business_date)
        # Scope/date filtering occurs in SQL before any ranking/model context.
        terms = set(re.findall(r"[a-zA-Z]+|[\u4e00-\u9fff]", query.casefold()))
        if terms:
            for row in rows:
                text = (row["title"] + row["content"] + row["kind"]).casefold()
                row["retrieval_score"] = sum(term in text for term in terms)
            rows = sorted(rows, key=lambda row: (-row["retrieval_score"], row["policy_id"]))
            rows = [row for row in rows if row["retrieval_score"] > 0]
        return rows[:10]

    def list_cost_centers(self, principal):
        with self.database.transaction() as connection:
            self._principal(connection, principal)
            return [dict(row) for row in connection.execute("SELECT * FROM cost_centers WHERE tenant_id=? AND department_scope IN (?, '*') ORDER BY code", (principal.tenant_id, principal.department_id))]

    def list_orders(self, principal, start_date=None, end_date=None):
        try:
            start = date.fromisoformat(str(start_date)).isoformat() if start_date else None
            end = date.fromisoformat(str(end_date)).isoformat() if end_date else None
        except (ValueError, TypeError):
            raise DomainError("invalid_date", "订单查询日期需要 YYYY-MM-DD。") from None
        if start and end and end < start:
            raise DomainError("invalid_date", "结束日期不能早于开始日期。")
        with self.database.transaction() as connection:
            self._principal(connection, principal)
            rows = [dict(row) for row in connection.execute("SELECT * FROM orders WHERE owner_id=? AND tenant_id=? ORDER BY order_id LIMIT 50", (principal.user_id, principal.tenant_id))]
        if start:
            rows = [row for row in rows if row["start_date"] >= start]
        if end:
            rows = [row for row in rows if row["end_date"] <= end]
        return rows

    def _input(self, payload):
        try:
            return DraftInput.model_validate(payload).model_dump(mode="json")
        except (ValidationError, ValueError, TypeError):
            raise DomainError("invalid_input", "费用输入无效；需要不重复的本人订单 ID 和合法日期。") from None

    def _select_policy(self, connection, principal, kind, city, day):
        matching = [row for row in self._policy_rows(connection, principal, day) if row["kind"] == kind and row["city"] in {city, "*"}]
        if not matching:
            raise DomainError("insufficient_evidence", "所选订单日期和城市没有适用制度。", 422)
        rank = lambda row: (row["department_scope"] == principal.department_id, row["city"] == city)
        best = max(map(rank, matching))
        candidates = [row for row in matching if rank(row) == best]
        if len(candidates) != 1:
            raise DomainError("policy_conflict", "相同适用范围存在冲突制度，需要人工核对。", 409)
        return candidates[0]

    def _calculate(self, connection, principal, fields):
        self._principal(connection, principal)
        center = fields.get("cost_center")
        if center is None:
            preference = connection.execute("SELECT value FROM preferences WHERE owner_id=? AND key='cost_center'", (principal.user_id,)).fetchone()
            center = preference["value"] if preference else None
        if center is None:
            raise DomainError("missing_cost_center", "请选择成本中心，或先明确保存常用成本中心。", 422)
        if not connection.execute("SELECT 1 FROM cost_centers WHERE code=? AND tenant_id=? AND department_scope IN (?, '*')", (center, principal.tenant_id, principal.department_id)).fetchone():
            raise DomainError("cost_center_denied", "该成本中心不在当前身份的允许范围。", 403)
        fields = {**fields, "cost_center": center}
        items, sources = [], {}
        for order_id in fields["order_ids"]:
            order_row = connection.execute("SELECT * FROM orders WHERE order_id=? AND owner_id=? AND tenant_id=?", (order_id, principal.user_id, principal.tenant_id)).fetchone()
            if order_row is None:
                raise DomainError("order_not_found", "订单不存在或不属于当前员工。", 404)
            order = dict(order_row)
            if order["currency"] != "CNY" or order["status"] != "completed" or order["receipt_valid"] != 1:
                raise DomainError("order_ineligible", "订单需要已完成、有效票据和 CNY 币种。", 422)
            if fields.get("destination") and fields["destination"] != order["city"]:
                raise DomainError("order_scope_mismatch", "订单城市与本次输入不一致。", 422)
            if fields.get("start_date") and order["start_date"] < fields["start_date"] or fields.get("end_date") and order["end_date"] > fields["end_date"]:
                raise DomainError("order_scope_mismatch", "订单日期不在本次输入范围内。", 422)
            start, end = date.fromisoformat(order["start_date"]), date.fromisoformat(order["end_date"])
            if order["kind"] == "hotel":
                nights = (end - start).days
                if not 1 <= nights <= 31:
                    raise DomainError("invalid_stay", "住宿需要 1–31 个实际入住晚数。", 422)
                base, remainder = divmod(order["amount_cents"], nights)
                allocations = [base + int(index < remainder) for index in range(nights)]
            elif order["kind"] == "train" and start == end:
                nights, allocations = 1, [order["amount_cents"]]
            else:
                raise DomainError("unsupported_order", "当前流程仅处理住宿与单日铁路订单。", 422)
            breakdown, eligible = [], 0
            for index, amount in enumerate(allocations):
                day = (start + timedelta(days=index)).isoformat()
                policy = self._select_policy(connection, principal, order["kind"], order["city"], day)
                cap = policy["cap_cents"]
                if order["kind"] == "hotel" and type(cap) is not int:
                    raise DomainError("invalid_policy", "住宿条款缺少结构化限额。", 422)
                allowed = amount if cap is None else min(amount, cap)
                eligible += allowed
                sources[policy["policy_id"]] = policy
                breakdown.append({"date": day, "amount_cents": amount, "eligible_cents": allowed, "policy_id": policy["policy_id"], "clause_id": policy["clause_id"], "policy_version": policy["version"]})
            items.append({**order, "eligible_cents": eligible, "excess_cents": order["amount_cents"] - eligible, "breakdown": breakdown})
        result = {"input": fields, "items": items, "total_cents": sum(item["amount_cents"] for item in items), "eligible_cents": sum(item["eligible_cents"] for item in items), "currency": "CNY", "sources": [sources[key] for key in sorted(sources)]}
        result["excess_cents"] = result["total_cents"] - result["eligible_cents"]
        result["content_hash"] = _hash(result)
        return result

    def evaluate_expense(self, principal, payload):
        fields = self._input(payload)
        with self.database.transaction() as connection:
            return self._calculate(connection, principal, fields)

    def _draft_row(self, connection, principal, draft_id):
        self._principal(connection, principal)
        row = connection.execute("SELECT * FROM drafts WHERE draft_id=? AND owner_id=? AND tenant_id=?", (draft_id, principal.user_id, principal.tenant_id)).fetchone()
        if row is None:
            raise DomainError("draft_not_found", "草稿不存在或不属于当前员工。", 404)
        return row

    def _draft_view(self, connection, row):
        result = json.loads(row["result_json"])
        confirmation = connection.execute("SELECT * FROM confirmations WHERE draft_id=? AND version=? AND content_hash=?", (row["draft_id"], row["version"], row["content_hash"])).fetchone()
        submission = connection.execute("SELECT * FROM submissions WHERE draft_id=?", (row["draft_id"],)).fetchone()
        return {**result, "draft_id": row["draft_id"], "owner_id": row["owner_id"], "version": row["version"], "status": row["status"], "created_at": row["created_at"], "updated_at": row["updated_at"], "confirmation": dict(confirmation) if confirmation else None, "submission": dict(submission) if submission else None}

    def get_draft(self, principal, draft_id):
        with self.database.transaction() as connection:
            return self._draft_view(connection, self._draft_row(connection, principal, draft_id))

    def list_drafts(self, principal):
        with self.database.transaction() as connection:
            self._principal(connection, principal)
            rows = connection.execute("SELECT * FROM drafts WHERE owner_id=? AND tenant_id=? ORDER BY created_at DESC LIMIT 50", (principal.user_id, principal.tenant_id)).fetchall()
            return [self._draft_view(connection, row) for row in rows]

    def create_draft(self, principal, payload, request_key=None):
        fields = self._input(payload)
        if request_key is not None and (not isinstance(request_key, str) or not 1 <= len(request_key) <= 200):
            raise DomainError("invalid_request_key", "草稿请求标识无效。")
        request_hash = _hash(fields)
        with self.database.transaction(write=True) as connection:
            self._principal(connection, principal)
            if request_key is not None:
                existing = connection.execute("SELECT * FROM drafts WHERE owner_id=? AND request_key=?", (principal.user_id, request_key)).fetchone()
                if existing:
                    if existing["request_hash"] != request_hash:
                        raise DomainError("request_conflict", "同一草稿请求标识不能复用不同输入。", 409)
                    return self._draft_view(connection, existing)
            result = self._calculate(connection, principal, fields)
            draft_id, at = "draft-" + uuid.uuid4().hex, _now()
            connection.execute("INSERT INTO drafts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (draft_id, principal.user_id, principal.tenant_id, 1, "draft", canonical_json(result["input"]), canonical_json(result), result["content_hash"], request_key, request_hash, at, at))
            self._event(connection, principal, "draft_created", draft_id, {"version": 1})
            return self._draft_view(connection, self._draft_row(connection, principal, draft_id))

    def edit_draft(self, principal, draft_id, payload, expected_version):
        _version(expected_version)
        if not isinstance(payload, dict):
            raise DomainError("invalid_input", "草稿修改需要 JSON 对象。")
        with self.database.transaction(write=True) as connection:
            row = self._draft_row(connection, principal, draft_id)
            if row["status"] == "submitted":
                raise DomainError("already_submitted", "已提交申请不可修改。", 409)
            if row["version"] != expected_version:
                raise DomainError("stale_version", "草稿已变化，请重新查看当前版本。", 409)
            fields = self._input({**json.loads(row["input_json"]), **payload})
            result = self._calculate(connection, principal, fields)
            connection.execute("UPDATE drafts SET version=version+1,status='draft',input_json=?,result_json=?,content_hash=?,updated_at=? WHERE draft_id=?", (canonical_json(result["input"]), canonical_json(result), result["content_hash"], _now(), draft_id))
            self._event(connection, principal, "draft_updated", draft_id, {"version": expected_version + 1})
            return self._draft_view(connection, self._draft_row(connection, principal, draft_id))

    def _current_snapshot(self, connection, principal, row):
        calculated = self._calculate(connection, principal, json.loads(row["input_json"]))
        if calculated["content_hash"] != row["content_hash"]:
            raise DomainError("stale_draft", "订单或制度已经变化，请重算草稿并重新确认。", 409)

    def confirm_draft(self, principal, draft_id, expected_version, expected_hash=None):
        _version(expected_version)
        with self.database.transaction(write=True) as connection:
            row = self._draft_row(connection, principal, draft_id)
            if row["status"] == "submitted":
                raise DomainError("already_submitted", "该草稿已经提交。", 409)
            if row["version"] != expected_version or expected_hash is not None and expected_hash != row["content_hash"]:
                raise DomainError("stale_version", "显示的草稿快照已经变化，请重新查看。", 409)
            self._current_snapshot(connection, principal, row)
            connection.execute("INSERT OR IGNORE INTO confirmations VALUES (?,?,?,?,?)", (draft_id, expected_version, principal.user_id, row["content_hash"], _now()))
            connection.execute("UPDATE drafts SET status='confirmed',updated_at=? WHERE draft_id=?", (_now(), draft_id))
            self._event(connection, principal, "draft_confirmed", draft_id, {"version": expected_version})
            return self._draft_view(connection, self._draft_row(connection, principal, draft_id))

    def submit_draft(self, principal, draft_id, expected_version, idempotency_key):
        _version(expected_version)
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key.strip()) <= 200:
            raise DomainError("invalid_idempotency_key", "提交需要 1–200 字符的幂等键。")
        request_hash = _hash({"draft_id": draft_id, "expected_version": expected_version})
        with self.database.transaction(write=True) as connection:
            row = self._draft_row(connection, principal, draft_id)
            cached = connection.execute("SELECT * FROM idempotency WHERE owner_id=? AND key=?", (principal.user_id, idempotency_key)).fetchone()
            if cached:
                if cached["request_hash"] != request_hash:
                    raise DomainError("idempotency_conflict", "同一幂等键不能用于不同提交内容。", 409)
                return dict(connection.execute("SELECT * FROM submissions WHERE submission_id=?", (cached["submission_id"],)).fetchone())
            if row["version"] != expected_version:
                raise DomainError("stale_version", "提交版本不是当前草稿版本。", 409)
            existing = connection.execute("SELECT * FROM submissions WHERE draft_id=?", (draft_id,)).fetchone()
            if existing:
                result = dict(existing)
            else:
                self._current_snapshot(connection, principal, row)
                confirmation = connection.execute("SELECT 1 FROM confirmations WHERE draft_id=? AND owner_id=? AND version=? AND content_hash=?", (draft_id, principal.user_id, expected_version, row["content_hash"])).fetchone()
                if not confirmation or row["status"] != "confirmed":
                    raise DomainError("confirmation_required", "提交前必须明确确认当前草稿及来源。", 409)
                result = {"submission_id": "EF-" + uuid.uuid4().hex[:16].upper(), "draft_id": draft_id, "tenant_id": principal.tenant_id, "owner_id": principal.user_id, "approved_version": expected_version, "content_hash": row["content_hash"], "created_at": _now()}
                connection.execute("INSERT INTO submissions VALUES (?,?,?,?,?,?,?)", tuple(result.values()))
                connection.execute("UPDATE drafts SET status='submitted',updated_at=? WHERE draft_id=?", (_now(), draft_id))
                self._event(connection, principal, "expense_submitted", draft_id, {"submission_id": result["submission_id"]})
            connection.execute("INSERT INTO idempotency VALUES (?,?,?,?)", (principal.user_id, idempotency_key, request_hash, result["submission_id"]))
            return result

    def get_preferences(self, principal):
        with self.database.transaction() as connection:
            self._principal(connection, principal)
            return [dict(row) for row in connection.execute("SELECT key,value,version,confirmed_at FROM preferences WHERE owner_id=? ORDER BY key", (principal.user_id,))]

    def set_preference(self, principal, key, value, confirmed=True):
        if key != "cost_center" or not isinstance(value, str) or confirmed is not True:
            raise DomainError("preference_confirmation_required", "仅支持员工明确确认的成本中心偏好。")
        with self.database.transaction(write=True) as connection:
            self._principal(connection, principal)
            if not connection.execute("SELECT 1 FROM cost_centers WHERE code=? AND tenant_id=? AND department_scope IN (?, '*')", (value, principal.tenant_id, principal.department_id)).fetchone():
                raise DomainError("cost_center_denied", "该成本中心不在允许范围。", 403)
            connection.execute("INSERT INTO preferences VALUES (?, ?, ?, 1, ?) ON CONFLICT(owner_id,key) DO UPDATE SET value=excluded.value,version=preferences.version+1,confirmed_at=excluded.confirmed_at", (principal.user_id, key, value, _now()))
            self._event(connection, principal, "preference_saved", key, {"value": value})
        return next(row for row in self.get_preferences(principal) if row["key"] == key)

    def delete_preference(self, principal, key):
        if key != "cost_center":
            raise DomainError("invalid_preference", "不支持该偏好字段。")
        with self.database.transaction(write=True) as connection:
            self._principal(connection, principal)
            connection.execute("DELETE FROM preferences WHERE owner_id=? AND key=?", (principal.user_id, key))
            self._event(connection, principal, "preference_deleted", key, {})
        return {"deleted": True, "key": key}

    def create_run(self, principal, run_id, mode, input_message, request_id=None):
        if mode not in {"fixture", "qwen", "api"} or not isinstance(input_message, str) or not 1 <= len(input_message.strip()) <= 4000:
            raise DomainError("invalid_run", "模式或任务文本无效。")
        if request_id is not None and (not isinstance(request_id, str) or not 1 <= len(request_id) <= 200):
            raise DomainError("invalid_request_id", "任务请求标识无效。")
        with self.database.transaction(write=True) as connection:
            self._principal(connection, principal)
            if request_id:
                existing = connection.execute("SELECT * FROM runs WHERE owner_id=? AND request_id=?", (principal.user_id, request_id)).fetchone()
                if existing:
                    if existing["message"] != input_message or existing["mode"] != mode:
                        raise DomainError("request_conflict", "同一任务请求标识不能复用不同输入。", 409)
                    return json.loads(existing["record_json"])
            record = {"run_id": run_id, "owner_id": principal.user_id, "tenant_id": principal.tenant_id, "mode": mode, "message": input_message, "status": "created", "stage": "created", "draft_id": None, "fields": {}, "questions": [], "result": None, "error": None, "created_at": _now(), "updated_at": _now()}
            connection.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?)", (run_id, principal.user_id, principal.tenant_id, mode, input_message, request_id, canonical_json(record)))
            return record

    def _run_row(self, connection, principal, run_id):
        self._principal(connection, principal)
        row = connection.execute("SELECT * FROM runs WHERE run_id=? AND owner_id=? AND tenant_id=?", (run_id, principal.user_id, principal.tenant_id)).fetchone()
        if not row:
            raise DomainError("run_not_found", "任务不存在或不属于当前员工。", 404)
        return row

    def get_run(self, principal, run_id):
        with self.database.transaction() as connection:
            return json.loads(self._run_row(connection, principal, run_id)["record_json"])

    def list_runs(self, principal):
        with self.database.transaction() as connection:
            self._principal(connection, principal)
            return [json.loads(row[0]) for row in connection.execute("SELECT record_json FROM runs WHERE owner_id=? AND tenant_id=? LIMIT 50", (principal.user_id, principal.tenant_id))]

    def update_run(self, principal, run_id, **changes):
        if set(changes) - {"status", "stage", "draft_id", "result", "error", "questions", "fields"}:
            raise DomainError("invalid_run_update", "不能修改任务身份或初始输入。")
        with self.database.transaction(write=True) as connection:
            record = json.loads(self._run_row(connection, principal, run_id)["record_json"])
            record.update(changes, updated_at=_now())
            connection.execute("UPDATE runs SET record_json=? WHERE run_id=?", (canonical_json(record), run_id))
            return record
