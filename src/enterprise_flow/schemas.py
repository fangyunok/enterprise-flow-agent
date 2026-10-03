"""Strict contracts; identity and amounts come from the application."""
from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Principal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    user_id: str
    tenant_id: str
    department_id: str
    display_name: str


class DraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    order_ids: list[str] = Field(min_length=1, max_length=20)
    cost_center: str | None = Field(default=None, min_length=1, max_length=80)
    notes: str = Field(default="", max_length=2000)
    start_date: date | None = None
    end_date: date | None = None
    destination: str | None = Field(default=None, min_length=1, max_length=80)

    @model_validator(mode="after")
    def validate_selection(self):
        if len(set(self.order_ids)) != len(self.order_ids) or any(not value.strip() for value in self.order_ids):
            raise ValueError("Order IDs must be nonblank and unique")
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("end_date must not precede start_date")
        return self


def canonical_json(value: Any) -> str:
    import json
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
