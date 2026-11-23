"""勘误追踪 API 端到端测试。

覆盖：
- 结论登记须能沿有效记录从先前层位推出后层位，否则 422；
- 批次只复查涉及层位组内段落，状态翻转才记变化，依据换而状态不变不记；
- 失去依据 lost 与恢复依据 restored 全链路，变化附批次号与本批记录号；
- 成环/校验失败时现场记录与勘误（批次、变化）一并回滚；
- 报告/段落/批次查询；
- 登记与订正并发时按提交顺序处理。
"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("ARCH_DB_PATH", str(db_path))
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


def _register(client, *numbers):
    resp = client.post("/strata", json={"stratum_numbers": list(numbers)})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _batch(client, operations, expected=201, note=None):
    resp = client.post(
        "/records/batch",
        json={"operations": operations, **({"note": note} if note else {})},
    )
    assert resp.status_code == expected, resp.text
    return resp.json()


def _chain(client):
    """L1->L2->L3 两条记录，外加独立组 L4->L5。"""
    _register(client, "L1", "L2", "L3", "L4", "L5")
    _batch(client, [
        {"op": "add", "record_no": "R1", "before": "L1", "after": "L2"},
        {"op": "add", "record_no": "R2", "before": "L2", "after": "L3"},
    ])
    _batch(client, [
        {"op": "add", "record_no": "RX", "before": "L4", "after": "L5"},
    ])


def _conclusion(client, report, para, before, after, expected=201):
    resp = client.post(
        "/conclusions",
        json={
            "report_no": report,
            "paragraph_no": para,
            "before": before,
            "after": after,
        },
    )
    assert resp.status_code == expected, resp.text
    return resp.json()


# ---------- 登记 ----------

def test_register_conclusion_requires_derivable_path(client):
    _chain(client)
    # L1 可经 R1、R2 间接推出 L3
    body = _conclusion(client, "RP-1", "P1", "L1", "L3")
    assert body["supported"] is True
    assert body["conclusion_id"] > 0

    # 反向不可达：登记被拒绝
    _conclusion(client, "RP-1", "P2", "L3", "L1", expected=422)


def test_register_rejects_unknown_strata_and_self_loop(client):
    _chain(client)
    _conclusion(client, "RP-1", "P9", "L1", "L9", expected=422)
    _conclusion(client, "RP-1", "P8", "L1", "L1", expected=422)


def test_duplicate_report_paragraph_rejected(client):
    _chain(client)
    _conclusion(client, "RP-1", "P1", "L1", "L2")
    _conclusion(client, "RP-1", "P1", "L1", "L2", expected=422)
    # 同段号在不同报告下允许
    _conclusion(client, "RP-2", "P1", "L1", "L2")


# ---------- 失去依据 ----------

def test_retract_breaks_basis_and_records_lost_change(client):
    _chain(client)
    _conclusion(client, "RP-1", "P1", "L1", "L3")
    _conclusion(client, "RP-1", "P2", "L1", "L2")

    # 撤回 R2：L1->L3 的间接路径断开，P1 失去依据；P1 变化附批次号与 R2
    body = _batch(client, [{"op": "retract", "record_no": "R2"}], note="现场订正")
    assert len(body["conclusion_changes"]) == 1
    change = body["conclusion_changes"][0]
    assert change["paragraph_no"] == "P1"
    assert change["change_type"] == "lost"
    assert change["supported_before"] is True
    assert change["supported_after"] is False
    assert change["batch_record_numbers"] == ["R2"]
    batch_id = change["batch_id"]

    # 当前状态：P1 无依据，P2 仍有依据
    status = client.get("/reports/RP-1").json()
    supported = {p["paragraph_no"]: p["supported"] for p in status["paragraphs"]}
    assert supported == {"P1": False, "P2": True}

    # 历次变化带批次号与本批记录号
    assert len(status["changes"]) == 1
    assert status["changes"][0]["batch_id"] == batch_id
    assert status["changes"][0]["batch_record_numbers"] == ["R2"]


def test_unrelated_group_conclusion_not_rechecked(client):
    _chain(client)
    # 结论挂在 L4->L5 组上
    _conclusion(client, "RP-9", "PX", "L4", "L5")
    # 批次只动 L1/L2/L3 组，L4/L5 组的段落不应被复查、不产生变化
    body = _batch(client, [{"op": "retract", "record_no": "R2"}])
    assert body["conclusion_changes"] == []
    status = client.get("/reports/RP-9").json()
    assert status["paragraphs"][0]["supported"] is True
    assert status["changes"] == []


def test_same_status_even_if_basis_path_changes_not_recorded(client):
    _chain(client)
    # P1 依赖 L1->L3 的间接路径（R1+R2）
    _conclusion(client, "RP-1", "P1", "L1", "L3")
    # 补一条直接记录 R3：依据路径变了（现在直达），但"有依据"状态不变 → 不记变化
    body = _batch(client, [
        {"op": "add", "record_no": "R3", "before": "L1", "after": "L3"},
    ])
    assert body["conclusion_changes"] == []
    status = client.get("/reports/RP-1").json()
    assert status["paragraphs"][0]["supported"] is True
    assert status["changes"] == []

    # 再撤回 R2：仍可经 R3 直达，状态依旧不变，仍不记变化
    body = _batch(client, [{"op": "retract", "record_no": "R2"}])
    assert body["conclusion_changes"] == []


# ---------- 恢复依据 ----------

def test_restore_basis_records_restored_change(client):
    _chain(client)
    _conclusion(client, "RP-1", "P1", "L1", "L3")
    lost_batch = _batch(client, [{"op": "retract", "record_no": "R2"}])
    assert lost_batch["conclusion_changes"][0]["change_type"] == "lost"

    # 重新补录一条 L2->L3 记录，路径恢复
    restored = _batch(client, [
        {"op": "add", "record_no": "R4", "before": "L2", "after": "L3"},
    ])
    assert len(restored["conclusion_changes"]) == 1
    change = restored["conclusion_changes"][0]
    assert change["change_type"] == "restored"
    assert change["supported_before"] is False
    assert change["supported_after"] is True
    assert change["batch_record_numbers"] == ["R4"]

    # 报告视角看到完整的失去 → 恢复两次变化，各自附批次号与记录号
    status = client.get("/reports/RP-1").json()
    assert [c["change_type"] for c in status["changes"]] == ["lost", "restored"]
    assert status["paragraphs"][0]["supported"] is True
    batch_ids = [c["batch_id"] for c in status["changes"]]
    assert batch_ids[0] < batch_ids[1]


def test_correct_record_can_lose_and_restore(client):
    _chain(client)
    _conclusion(client, "RP-1", "P1", "L1", "L3")
    # 更正 R2 使链路断开（L2->L4 与 L3 无关），P1 失去依据
    body = _batch(client, [
        {"op": "correct", "record_no": "R2", "before": "L2", "after": "L4"},
    ])
    assert body["conclusion_changes"][0]["change_type"] == "lost"
    # 更正回来，依据恢复
    body = _batch(client, [
        {"op": "correct", "record_no": "R2", "before": "L2", "after": "L3"},
    ])
    assert body["conclusion_changes"][0]["change_type"] == "restored"


# ---------- 查询 ----------

def test_paragraph_detail_and_batch_trace_endpoints(client):
    _chain(client)
    _conclusion(client, "RP-1", "P1", "L1", "L3")
    lost = _batch(client, [{"op": "retract", "record_no": "R2"}], note="n1")
    lost_batch_id = lost["conclusion_changes"][0]["batch_id"]

    detail = client.get("/reports/RP-1/paragraphs/P1").json()
    assert detail["current"]["supported"] is False
    assert len(detail["changes"]) == 1
    assert detail["changes"][0]["batch_id"] == lost_batch_id

    # 批次列表与单批溯源：列出本批改动的记录号与操作
    batches = client.get("/batches").json()
    assert any(b["batch_id"] == lost_batch_id for b in batches)
    one = client.get(f"/batches/{lost_batch_id}").json()
    assert one["note"] == "n1"
    assert one["operations"] == [{"record_no": "R2", "op": "retract"}]

    assert client.get("/reports/NOPE").status_code == 422
    assert client.get("/batches/9999").status_code == 422
    assert client.get("/reports/RP-1/paragraphs/NOPE").status_code == 422


# ---------- 原子性：失败时现场记录与勘误都保持原状 ----------

def test_failed_batch_leaves_records_and_errata_untouched(client):
    _chain(client)
    _conclusion(client, "RP-1", "P1", "L1", "L3")

    batches_before = len(client.get("/batches").json())
    # 成环批次：L3->L1 与现有 L1->L2->L3 成环，整批驳回
    _batch(client, [
        {"op": "add", "record_no": "R9", "before": "L3", "after": "L1"},
    ], expected=409)

    # 现场记录保持原状
    active = {
        r["record_no"]
        for r in client.get("/records", params={"active_only": True}).json()
    }
    assert "R9" not in active
    # 没有产生新批次，也没有任何勘误变化，段落仍有依据
    assert len(client.get("/batches").json()) == batches_before
    status = client.get("/reports/RP-1").json()
    assert status["paragraphs"][0]["supported"] is True
    assert status["changes"] == []


def test_validation_failure_is_fully_rolled_back(client):
    _chain(client)
    _conclusion(client, "RP-1", "P1", "L1", "L2")
    batches_before = len(client.get("/batches").json())
    # 撤回不存在的记录：422，批次不落账
    _batch(client, [{"op": "retract", "record_no": "MISSING"}], expected=422)
    assert len(client.get("/batches").json()) == batches_before
    assert client.get("/reports/RP-1").json()["changes"] == []


# ---------- 并发：登记与订正按提交顺序处理 ----------

def test_concurrent_registration_and_correction_serialized(client):
    _chain(client)
    barrier = threading.Barrier(2)
    outcomes: list[dict] = []

    def register():
        barrier.wait()
        resp = client.post(
            "/conclusions",
            json={
                "report_no": "RP-C",
                "paragraph_no": "P1",
                "before": "L1",
                "after": "L3",
            },
        )
        outcomes.append({"kind": "register", "status": resp.status_code})

    def retract():
        barrier.wait()
        resp = client.post(
            "/records/batch",
            json={"operations": [{"op": "retract", "record_no": "R2"}]},
        )
        outcomes.append(
            {"kind": "batch", "status": resp.status_code, "body": resp.json()}
        )

    t1 = threading.Thread(target=register)
    t2 = threading.Thread(target=retract)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    register_outcome = next(o for o in outcomes if o["kind"] == "register")
    batch_outcome = next(o for o in outcomes if o["kind"] == "batch")
    assert batch_outcome["status"] in (200, 201)

    if register_outcome["status"] in (200, 201):
        # 登记先于撤回拿到写锁：已登记段落必须被随后生效的撤回批次复查到，
        # 最终无依据并留下一次 lost 变化
        status = client.get("/reports/RP-C").json()
        assert status["paragraphs"][0]["supported"] is False
        assert len(status["changes"]) == 1
        assert status["changes"][0]["change_type"] == "lost"
    else:
        # 撤回先提交：路径已断，登记按规则被拒绝（422），且撤回批次没有
        # 任何段落可复查；随后补回路径再登记应当成功
        assert register_outcome["status"] == 422
        assert batch_outcome["body"]["conclusion_changes"] == []
        _batch(client, [
            {"op": "add", "record_no": "R4", "before": "L2", "after": "L3"},
        ])
        retry = _conclusion(client, "RP-C", "P1", "L1", "L3")
        assert retry["supported"] is True
        # 登记之后生效的相关批次仍会纳入该段：再撤回 R4，记录 lost
        body = _batch(client, [{"op": "retract", "record_no": "R4"}])
        assert body["conclusion_changes"][0]["paragraph_no"] == "P1"
        assert body["conclusion_changes"][0]["change_type"] == "lost"


def test_concurrent_batches_keep_change_history_consistent(client):
    _chain(client)
    _conclusion(client, "RP-1", "P1", "L1", "L3")
    errors: list[Exception] = []

    def worker(ops):
        try:
            _batch(client, ops)
        except Exception as exc:  # pragma: no cover - 仅失败时报告
            errors.append(exc)

    # 两个批次针对同一层位组并发：撤回 R2 与补直达线，写锁串行化后
    # 变化历史仍须自洽（最多一次失去，且最终状态可由历史推出）
    t1 = threading.Thread(target=worker, args=(
        [{"op": "retract", "record_no": "R2"}],
    ))
    t2 = threading.Thread(target=worker, args=(
        [{"op": "add", "record_no": "R3", "before": "L1", "after": "L3"}],
    ))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert errors == []

    status = client.get("/reports/RP-1").json()
    changes = status["changes"]
    # 同一状态不能连续翻转两次；变化类型必须严格交替
    types = [c["change_type"] for c in changes]
    assert all(a != b for a, b in zip(types, types[1:]))
    # 每条变化都能溯源到批次与记录号
    for c in changes:
        assert c["batch_id"] > 0 and c["batch_record_numbers"]
