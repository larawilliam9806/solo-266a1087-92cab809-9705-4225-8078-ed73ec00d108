"""请求/响应模型。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# ---------- 请求 ----------

class StrataRegisterRequest(BaseModel):
    stratum_numbers: list[str] = Field(min_length=1)


class AddOperation(BaseModel):
    op: Literal["add"]
    record_no: str
    before: str
    after: str


class CorrectOperation(BaseModel):
    op: Literal["correct"]
    record_no: str
    before: str
    after: str


class RetractOperation(BaseModel):
    op: Literal["retract"]
    record_no: str


class BatchRequest(BaseModel):
    operations: list[AddOperation | CorrectOperation | RetractOperation] = Field(
        min_length=1
    )
    note: str | None = None


class ConclusionRegisterRequest(BaseModel):
    report_no: str = Field(min_length=1)
    paragraph_no: str = Field(min_length=1)
    before: str = Field(min_length=1)
    after: str = Field(min_length=1)


# ---------- 响应 ----------

class StrataRegisterResponse(BaseModel):
    registered: list[str]
    already_existed: list[str]


class RecordOut(BaseModel):
    record_no: str
    before: str
    after: str
    active: bool
    created_at: str
    updated_at: str
    retracted_at: str | None


class EdgeOut(BaseModel):
    before: str
    after: str
    record_numbers: list[str]


class GroupOut(BaseModel):
    # 孤立层位（尚未出现在任何关系中）不属于任何组，group_id 为 None
    group_id: int | None
    strata: list[str]
    edges: list[EdgeOut]


class BatchResponse(BaseModel):
    batch_id: int
    applied_operations: int
    affected_groups: list[GroupOut]
    untouched_groups: list[int]
    # 本批前后依据状态发生翻转的段落（状态相同即使依据换了也不列入）
    conclusion_changes: list["ConclusionChangeOut"]


class CycleEdge(BaseModel):
    before: str
    after: str
    record_numbers: list[str]


class CycleErrorResponse(BaseModel):
    detail: str
    cycle: list[str]
    edges: list[CycleEdge]


# ---------- 勘误追踪 ----------

class ConclusionOut(BaseModel):
    conclusion_id: int
    report_no: str
    paragraph_no: str
    before: str
    after: str
    supported: bool
    registered_at: str
    updated_at: str


class ConclusionChangeOut(BaseModel):
    change_id: int
    report_no: str
    paragraph_no: str
    change_type: Literal["lost", "restored"]
    supported_before: bool
    supported_after: bool
    batch_id: int
    # 触发本批改动的记录号
    batch_record_numbers: list[str]
    created_at: str


class ConclusionRegisterResponse(BaseModel):
    conclusion_id: int
    report_no: str
    paragraph_no: str
    before: str
    after: str
    supported: bool
    registered_at: str


class ReportStatusOut(BaseModel):
    report_no: str
    paragraphs: list[ConclusionOut]
    changes: list[ConclusionChangeOut]


class BatchRecordOut(BaseModel):
    record_no: str
    op: Literal["add", "correct", "retract"]


class BatchOut(BaseModel):
    batch_id: int
    note: str | None
    created_at: str
    operations: list[BatchRecordOut]
