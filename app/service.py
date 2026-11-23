"""业务编排：层位登记、记录批量订正、图的局部重算与勘误追踪。

核心一致性规则：
- 一个批次（add/correct/retract 混合）整体原子生效；
- 先在事务内模拟本批后的记录全集，成环则整体回滚，原记录与原图不动；
- 只重算改动前后涉及的层位组（顶点集合取并集），其他组的边不删除不重算，
  因此针对其他组的并发订正不会被本批阻塞读取，写锁仅在最后写入时短暂持有；
- 查询直接读取已落库的归约图（graph_edges / edge_support），不做临时计算。

勘误追踪规则：
- 按报告号 + 段落号登记已发表结论，每段填先后两个层位号；登记时必须能沿
  有效记录从前一个层位推到后一个，否则不予登记；
- 每批补录/更正/撤回，只复查改动前后涉及层位组内的段落，对照本批提交前后
  两次可达性快照：仅在"有依据/无依据"翻转时记一条变化（失去 lost / 恢复
  restored），两次状态相同即使依据路径换了也不记；
- 每条变化附批次号与本批改动的记录号；
- 批次号、批次记录、结论变化与现场记录、关系图在同一个事务内提交，
  批次失败（含成环）时全部保持原状。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from . import graph
from .db import db_session, init_db
from .schemas import BatchRequest


class CycleConflict(Exception):
    """本批改写会形成闭环，HTTP 409。"""

    def __init__(self, cycle: list[str], edges: list[dict]):
        self.cycle = cycle
        self.edges = edges


class ValidationFailure(Exception):
    """请求数据不满足前置条件，HTTP 422。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------- 层位登记 ----------------

def register_strata(stratum_numbers: list[str], db_path: str | None = None) -> dict:
    seen: set[str] = set()
    duplicates_in_request = [s for s in stratum_numbers if s in seen or seen.add(s)]
    if duplicates_in_request:
        raise ValidationFailure(
            f"请求中层位号重复: {sorted(set(duplicates_in_request))}"
        )
    registered: list[str] = []
    already_existed: list[str] = []
    with db_session(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for no in stratum_numbers:
                exists = conn.execute(
                    "SELECT 1 FROM strata WHERE stratum_no=?", (no,)
                ).fetchone()
                if exists:
                    already_existed.append(no)
                    continue
                conn.execute(
                    "INSERT INTO strata (stratum_no, group_id, created_at) "
                    "VALUES (?, NULL, ?)",
                    (no, _now()),
                )
                registered.append(no)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {"registered": registered, "already_existed": already_existed}


# ---------------- 批量订正 ----------------

def apply_batch(batch: BatchRequest, db_path: str | None = None) -> dict:
    operations = batch.operations

    # 1. 请求自身校验：编号唯一性与操作类型（不持锁，快速失败）
    add_nos = [op.record_no for op in operations if op.op == "add"]
    dup_in_req = {no for no in add_nos if add_nos.count(no) > 1}
    if dup_in_req:
        raise ValidationFailure(f"批次内新增记录号重复: {sorted(dup_in_req)}")
    touched_nos = {op.record_no for op in operations}
    if len(touched_nos) != len(operations):
        raise ValidationFailure("批次内同一记录号出现了多次操作")
    endpoint_nos: set[str] = set()
    for op in operations:
        if op.op in ("add", "correct"):
            endpoint_nos.add(op.before)
            endpoint_nos.add(op.after)
            if op.before == op.after:
                raise ValidationFailure(
                    f"记录 {op.record_no} 的先后层位不能相同（自环）"
                )

    # 2. 取写锁后做全部数据库校验与状态读取：并发批次按提交（拿到写锁）
    #    的顺序串行处理，后到批次看到的一定是先前批次已提交的状态。
    with db_session(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            now = _now()
            placeholders = ",".join("?" * len(touched_nos))
            existing = {
                row["record_no"]: row
                for row in conn.execute(
                    f"SELECT record_no, before_no, after_no, active "
                    f"FROM records WHERE record_no IN ({placeholders})",
                    tuple(touched_nos),
                )
            }
            missing = [
                no
                for no in sorted(endpoint_nos)
                if not conn.execute(
                    "SELECT 1 FROM strata WHERE stratum_no=?", (no,)
                ).fetchone()
            ]
            if missing:
                raise ValidationFailure(f"层位尚未登记: {missing}")

            for op in operations:
                row = existing.get(op.record_no)
                if op.op == "add" and row is not None:
                    raise ValidationFailure(f"记录号已存在: {op.record_no}")
                if op.op in ("correct", "retract") and row is None:
                    raise ValidationFailure(f"记录号不存在: {op.record_no}")
                if op.op == "retract" and row is not None and not row["active"]:
                    raise ValidationFailure(
                        f"记录已撤回，不能重复撤回: {op.record_no}"
                    )
                if op.op == "correct" and row is not None and not row["active"]:
                    raise ValidationFailure(f"记录已撤回，不能更正: {op.record_no}")

            # 3. 改动前后涉及的层位：受触碰记录端点（旧端点 ∪ 新端点）
            affected_vertices = set(endpoint_nos)
            for no in touched_nos:
                row = existing.get(no)
                if row is not None:
                    affected_vertices.add(row["before_no"])
                    affected_vertices.add(row["after_no"])

            old_group_ids = [
                r["group_id"]
                for r in conn.execute(
                    "SELECT DISTINCT group_id FROM strata "
                    "WHERE group_id IS NOT NULL AND stratum_no IN ("
                    + ",".join("?" * len(affected_vertices)) + ")",
                    tuple(affected_vertices),
                )
            ]

            # 4. 载入范围内全部记录（含已撤回），在内存中模拟本批
            record_rows = conn.execute(
                "SELECT record_no, before_no, after_no, active, created_at "
                "FROM records "
                "WHERE before_no IN (" + ",".join("?" * len(affected_vertices)) + ") "
                "OR after_no IN (" + ",".join("?" * len(affected_vertices)) + ")",
                tuple(affected_vertices) * 2,
            ).fetchall()
            records: dict[str, dict[str, Any]] = {
                r["record_no"]: {
                    "record_no": r["record_no"],
                    "before": r["before_no"],
                    "after": r["after_no"],
                    "active": bool(r["active"]),
                    "created_at": r["created_at"],
                }
                for r in record_rows
            }

            def _active_adjacency(
                snapshot: dict[str, dict[str, Any]],
                extra: set[str],
            ) -> tuple[set[str], dict[str, set[str]]]:
                triples = [
                    (r["record_no"], r["before"], r["after"])
                    for r in snapshot.values()
                    if r["active"]
                ]
                verts, adjacency, _ = graph.build_adjacency(
                    triples, extra_vertices=extra
                )
                return verts, adjacency

            # 5. 先在内存中模拟本批（不落库），确定本批真正涉及的层位组：
            #    触碰端点在模拟后的弱连通分量内的全部层位。
            for op in operations:
                if op.op == "add":
                    records[op.record_no] = {
                        "record_no": op.record_no,
                        "before": op.before,
                        "after": op.after,
                        "active": True,
                        "created_at": now,
                    }
                elif op.op == "correct":
                    records[op.record_no].update(
                        before=op.before, after=op.after, active=True
                    )
                else:  # retract
                    records[op.record_no]["active"] = False

            sim_vertices, sim_adjacency = _active_adjacency(
                records, affected_vertices
            )
            involved_components = [
                comp
                for comp in graph.weak_components(sim_vertices, sim_adjacency)
                if comp & affected_vertices
            ]
            involved_vertices: set[str] = set().union(
                *involved_components
            ) if involved_components else set(affected_vertices)
            # 旧组中可能存在模拟后变孤立的层位（如撤回唯一连线），
            # 它们不在模拟分量里，需经旧组层位补回
            if old_group_ids:
                marks = ",".join("?" * len(old_group_ids))
                for r in conn.execute(
                    f"SELECT stratum_no FROM strata WHERE group_id IN ({marks})",
                    tuple(old_group_ids),
                ):
                    involved_vertices.add(r["stratum_no"])

            # 若涉及范围扩大到旧组内其他层位，补载它们的记录，保证
            # 组内间接路径与结论可达性计算完整
            extra_loaded = involved_vertices - affected_vertices
            if extra_loaded:
                more_rows = conn.execute(
                    "SELECT record_no, before_no, after_no, active, created_at "
                    "FROM records "
                    "WHERE before_no IN ("
                    + ",".join("?" * len(extra_loaded)) + ") "
                    "OR after_no IN ("
                    + ",".join("?" * len(extra_loaded)) + ")",
                    tuple(extra_loaded) * 2,
                ).fetchall()
                for r in more_rows:
                    if r["record_no"] not in records:
                        records[r["record_no"]] = {
                            "record_no": r["record_no"],
                            "before": r["before_no"],
                            "after": r["after_no"],
                            "active": bool(r["active"]),
                            "created_at": r["created_at"],
                        }
            affected_vertices = involved_vertices

            # 6. 提交前快照：只复查改动前后涉及层位组内已登记的段落
            candidate_conclusions = conn.execute(
                "SELECT conclusion_id, report_no, paragraph_no, before_no, "
                "after_no, supported FROM conclusions "
                "WHERE before_no IN (" + ",".join("?" * len(affected_vertices)) + ") "
                "AND after_no IN (" + ",".join("?" * len(affected_vertices)) + ")",
                tuple(affected_vertices) * 2,
            ).fetchall()
            # 提交前记录集 = 模拟集撤销本批改动
            before_records = {
                no: dict(item) for no, item in records.items()
            }
            for op in operations:
                row = existing.get(op.record_no)
                if row is None:
                    before_records.pop(op.record_no, None)
                else:
                    before_records[op.record_no].update(
                        before=row["before_no"],
                        after=row["after_no"],
                        active=bool(row["active"]),
                    )
            _, before_adjacency = _active_adjacency(
                before_records, affected_vertices
            )
            before_supported = {
                row["conclusion_id"]: graph.is_reachable(
                    before_adjacency, row["before_no"], row["after_no"]
                )
                for row in candidate_conclusions
            }

            # 7. 本批现场记录正式落账（内存快照已在步骤 5 模拟完毕）
            for op in operations:
                if op.op == "add":
                    conn.execute(
                        "INSERT INTO records "
                        "(record_no, before_no, after_no, active, "
                        "created_at, updated_at, retracted_at) "
                        "VALUES (?, ?, ?, 1, ?, ?, NULL)",
                        (op.record_no, op.before, op.after, now, now),
                    )
                elif op.op == "correct":
                    conn.execute(
                        "UPDATE records SET before_no=?, after_no=?, "
                        "updated_at=? WHERE record_no=?",
                        (op.before, op.after, now, op.record_no),
                    )
                else:  # retract
                    conn.execute(
                        "UPDATE records SET active=0, updated_at=?, "
                        "retracted_at=? WHERE record_no=?",
                        (now, now, op.record_no),
                    )

            vertices, after_adjacency = _active_adjacency(
                records, affected_vertices
            )

            # 8. 闭环检查（以本批生效时的记录为准）
            cycle = graph.find_cycle(after_adjacency, vertices)
            if cycle is not None:
                edge_support = graph.build_adjacency(
                    [
                        (r["record_no"], r["before"], r["after"])
                        for r in records.values()
                        if r["active"]
                    ],
                    extra_vertices=affected_vertices,
                )[2]
                detailed = graph.cycle_with_records(cycle, edge_support)
                conn.rollback()
                raise CycleConflict(cycle, detailed)

            # 9. 批次与本批改动记录号先落账（提交后变化才能附批次号溯源）
            cur = conn.execute(
                "INSERT INTO batches (note, created_at) VALUES (?, ?)",
                (batch.note, now),
            )
            batch_id = cur.lastrowid
            conn.executemany(
                "INSERT INTO batch_records (batch_id, record_no, op) "
                "VALUES (?, ?, ?)",
                [(batch_id, op.record_no, op.op) for op in operations],
            )

            # 10. 对照提交前后两次状态，仅翻转（失去/恢复依据）才记变化
            conclusion_changes: list[dict] = []
            for row in sorted(candidate_conclusions, key=lambda r: r["conclusion_id"]):
                cid = row["conclusion_id"]
                old_supported = before_supported[cid]
                new_supported = graph.is_reachable(
                    after_adjacency, row["before_no"], row["after_no"]
                )
                if old_supported == new_supported:
                    # 两次状态相同，即使依据路径换了也不记
                    continue
                change_type = "lost" if old_supported else "restored"
                conn.execute(
                    "UPDATE conclusions SET supported=?, updated_at=? "
                    "WHERE conclusion_id=?",
                    (int(new_supported), now, cid),
                )
                change_cur = conn.execute(
                    "INSERT INTO conclusion_changes (conclusion_id, batch_id, "
                    "change_type, supported_before, supported_after, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        cid,
                        batch_id,
                        change_type,
                        int(old_supported),
                        int(new_supported),
                        now,
                    ),
                )
                conclusion_changes.append(
                    {
                        "change_id": change_cur.lastrowid,
                        "report_no": row["report_no"],
                        "paragraph_no": row["paragraph_no"],
                        "change_type": change_type,
                        "supported_before": old_supported,
                        "supported_after": new_supported,
                        "batch_id": batch_id,
                        "batch_record_numbers": sorted(touched_nos),
                        "created_at": now,
                    }
                )

            # 11. 弱连通分量（忽略方向的相连整组）+ 组内传递归约。
            #     本批后失去全部边的层位成为孤立点，不产生只有一个顶点的空组；
            #     它们的 group_id 在步骤 12 解绑后保持 NULL，下次并入关系时再成组。
            components = graph.weak_components(vertices, after_adjacency)
            new_groups: list[tuple[set[str], set[tuple[str, str]]]] = []
            for component in components:
                if len(component) == 1:
                    sole = next(iter(component))
                    if not after_adjacency.get(sole):
                        continue
                kept = graph.transitive_reduction(component, after_adjacency)
                new_groups.append((component, kept))
            _, _, edge_support = graph.build_adjacency(
                [
                    (r["record_no"], r["before"], r["after"])
                    for r in records.values()
                    if r["active"]
                ],
                extra_vertices=affected_vertices,
            )

            # 12. 拆除旧组（edge_support 经外键 ON DELETE CASCADE 随边自动删除；
            #     边 -> 组 -> 层位解绑顺序满足外键约束）
            if old_group_ids:
                marks = ",".join("?" * len(old_group_ids))
                conn.execute(
                    f"DELETE FROM graph_edges WHERE group_id IN ({marks})",
                    tuple(old_group_ids),
                )
                conn.execute(
                    f"DELETE FROM groups WHERE group_id IN ({marks})",
                    tuple(old_group_ids),
                )
                conn.execute(
                    "UPDATE strata SET group_id=NULL WHERE group_id IN ("
                    + marks + ")",
                    tuple(old_group_ids),
                )

            # 13. 写入新组（合并/拆分后的各组与本批一并生效）
            for component, kept_edges in new_groups:
                cur = conn.execute(
                    "INSERT INTO groups (created_at) VALUES (?)", (now,)
                )
                new_group_id = cur.lastrowid
                conn.executemany(
                    "UPDATE strata SET group_id=? WHERE stratum_no=?",
                    [(new_group_id, v) for v in component],
                )
                conn.executemany(
                    "INSERT INTO graph_edges (group_id, before_no, after_no) "
                    "VALUES (?, ?, ?)",
                    [(new_group_id, u, v) for u, v in sorted(kept_edges)],
                )
                conn.executemany(
                    "INSERT INTO edge_support (before_no, after_no, record_no) "
                    "VALUES (?, ?, ?)",
                    [
                        (u, v, rec_no)
                        for u, v in sorted(kept_edges)
                        for rec_no in edge_support[(u, v)]
                    ],
                )

            affected_payload = [
                _load_group(conn, gid)
                for gid in sorted(
                    {
                        row["group_id"]
                        for row in conn.execute(
                            "SELECT DISTINCT group_id FROM strata "
                            "WHERE group_id IS NOT NULL AND stratum_no IN ("
                            + ",".join("?" * len(affected_vertices)) + ")",
                            tuple(affected_vertices),
                        )
                    }
                )
            ]
            # 受影响组重算后会拿到新 group_id（合并/拆分），因此"未触碰组"
            # 要按重算后的实际组集合求差，而不是与旧组 id 比较
            affected_new_ids = {g["group_id"] for g in affected_payload}
            untouched = [
                r["group_id"]
                for r in conn.execute("SELECT group_id FROM groups ORDER BY group_id")
                if r["group_id"] not in affected_new_ids
            ]
            # 现场记录、批次、勘误变化与关系图一并提交；任一步失败全部回滚
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return {
        "batch_id": batch_id,
        "applied_operations": len(operations),
        "affected_groups": affected_payload,
        "untouched_groups": untouched,
        "conclusion_changes": conclusion_changes,
    }


# ---------------- 勘误追踪：结论登记 ----------------

def register_conclusion(
    report_no: str,
    paragraph_no: str,
    before: str,
    after: str,
    db_path: str | None = None,
) -> dict:
    """登记报告某段已发表结论（先后两个层位号）。

    登记时必须能沿当前有效记录从前一个层位推到后一个，否则拒绝登记。
    与批次共用写锁：并发登记/订正按提交顺序串行，已登记段落会被随后
    拿到锁生效的相关批次纳入复查。
    """
    if before == after:
        raise ValidationFailure("结论的先后层位不能相同")
    with db_session(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            now = _now()
            for no in (before, after):
                if not conn.execute(
                    "SELECT 1 FROM strata WHERE stratum_no=?", (no,)
                ).fetchone():
                    raise ValidationFailure(f"层位尚未登记: {no}")
            dup = conn.execute(
                "SELECT 1 FROM conclusions WHERE report_no=? AND paragraph_no=?",
                (report_no, paragraph_no),
            ).fetchone()
            if dup:
                raise ValidationFailure(
                    f"报告 {report_no} 段落 {paragraph_no} 的结论已登记，"
                    "不能重复登记"
                )

            active_rows = conn.execute(
                "SELECT record_no, before_no, after_no FROM records WHERE active=1"
            ).fetchall()
            _, adjacency, _ = graph.build_adjacency(
                [(r["record_no"], r["before_no"], r["after_no"]) for r in active_rows]
            )
            if not graph.is_reachable(adjacency, before, after):
                conn.rollback()
                raise ValidationFailure(
                    f"登记被拒绝：当前有效记录无法从层位 {before} 推出层位 {after}，"
                    "结论缺少依据"
                )
            cur = conn.execute(
                "INSERT INTO conclusions (report_no, paragraph_no, before_no, "
                "after_no, supported, registered_at, updated_at) "
                "VALUES (?, ?, ?, ?, 1, ?, ?)",
                (report_no, paragraph_no, before, after, now, now),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return {
        "conclusion_id": cur.lastrowid,
        "report_no": report_no,
        "paragraph_no": paragraph_no,
        "before": before,
        "after": after,
        "supported": True,
        "registered_at": now,
    }


# ---------------- 勘误追踪：状态与变化查询 ----------------

def _conclusion_row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "conclusion_id": row["conclusion_id"],
        "report_no": row["report_no"],
        "paragraph_no": row["paragraph_no"],
        "before": row["before_no"],
        "after": row["after_no"],
        "supported": bool(row["supported"]),
        "registered_at": row["registered_at"],
        "updated_at": row["updated_at"],
    }


def _load_changes_for_report(
    conn: sqlite3.Connection, report_no: str
) -> list[dict]:
    """某报告全部段落的历次变化，每条附批次号与本批改动的记录号。"""
    change_rows = conn.execute(
        "SELECT cc.change_id, cc.conclusion_id, c.paragraph_no, cc.change_type, "
        "cc.supported_before, cc.supported_after, cc.batch_id, cc.created_at "
        "FROM conclusion_changes cc "
        "JOIN conclusions c ON c.conclusion_id = cc.conclusion_id "
        "WHERE c.report_no=? ORDER BY cc.change_id",
        (report_no,),
    ).fetchall()
    changes: list[dict] = []
    for row in change_rows:
        record_numbers = [
            r["record_no"]
            for r in conn.execute(
                "SELECT record_no FROM batch_records WHERE batch_id=? "
                "ORDER BY record_no",
                (row["batch_id"],),
            )
        ]
        changes.append(
            {
                "change_id": row["change_id"],
                "report_no": report_no,
                "paragraph_no": row["paragraph_no"],
                "change_type": row["change_type"],
                "supported_before": bool(row["supported_before"]),
                "supported_after": bool(row["supported_after"]),
                "batch_id": row["batch_id"],
                "batch_record_numbers": record_numbers,
                "created_at": row["created_at"],
            }
        )
    return changes


def get_report_status(report_no: str, db_path: str | None = None) -> dict:
    """按报告号查各段当前状态与历次变化。"""
    with db_session(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM conclusions WHERE report_no=? "
            "ORDER BY paragraph_no, conclusion_id",
            (report_no,),
        ).fetchall()
        if not rows:
            raise ValidationFailure(f"报告 {report_no} 下没有登记任何段落结论")
        paragraphs = [_conclusion_row_to_dict(r) for r in rows]
        changes = _load_changes_for_report(conn, report_no)
    return {"report_no": report_no, "paragraphs": paragraphs, "changes": changes}


def get_conclusion(
    report_no: str, paragraph_no: str, db_path: str | None = None
) -> dict:
    """查单段当前状态与该段自己的历次变化。"""
    with db_session(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM conclusions WHERE report_no=? AND paragraph_no=?",
            (report_no, paragraph_no),
        ).fetchone()
        if row is None:
            raise ValidationFailure(
                f"报告 {report_no} 段落 {paragraph_no} 的结论未登记"
            )
        current = _conclusion_row_to_dict(row)
        change_rows = conn.execute(
            "SELECT change_id, change_type, supported_before, supported_after, "
            "batch_id, created_at FROM conclusion_changes "
            "WHERE conclusion_id=? ORDER BY change_id",
            (row["conclusion_id"],),
        ).fetchall()
        changes = []
        for cr in change_rows:
            record_numbers = [
                r["record_no"]
                for r in conn.execute(
                    "SELECT record_no FROM batch_records WHERE batch_id=? "
                    "ORDER BY record_no",
                    (cr["batch_id"],),
                )
            ]
            changes.append(
                {
                    "change_id": cr["change_id"],
                    "report_no": report_no,
                    "paragraph_no": paragraph_no,
                    "change_type": cr["change_type"],
                    "supported_before": bool(cr["supported_before"]),
                    "supported_after": bool(cr["supported_after"]),
                    "batch_id": cr["batch_id"],
                    "batch_record_numbers": record_numbers,
                    "created_at": cr["created_at"],
                }
            )
    return {"current": current, "changes": changes}


def list_batches(db_path: str | None = None) -> list[dict]:
    """列出全部批次及各批改动的记录号（溯源用）。"""
    with db_session(db_path) as conn:
        batch_rows = conn.execute(
            "SELECT batch_id, note, created_at FROM batches ORDER BY batch_id"
        ).fetchall()
        result = []
        for b in batch_rows:
            ops = [
                {"record_no": r["record_no"], "op": r["op"]}
                for r in conn.execute(
                    "SELECT record_no, op FROM batch_records "
                    "WHERE batch_id=? ORDER BY record_no",
                    (b["batch_id"],),
                )
            ]
            result.append(
                {
                    "batch_id": b["batch_id"],
                    "note": b["note"],
                    "created_at": b["created_at"],
                    "operations": ops,
                }
            )
    return result


def get_batch(batch_id: int, db_path: str | None = None) -> dict:
    with db_session(db_path) as conn:
        row = conn.execute(
            "SELECT batch_id, note, created_at FROM batches WHERE batch_id=?",
            (batch_id,),
        ).fetchone()
        if row is None:
            raise ValidationFailure(f"批次不存在: {batch_id}")
        ops = [
            {"record_no": r["record_no"], "op": r["op"]}
            for r in conn.execute(
                "SELECT record_no, op FROM batch_records WHERE batch_id=? "
                "ORDER BY record_no",
                (batch_id,),
            )
        ]
    return {
        "batch_id": row["batch_id"],
        "note": row["note"],
        "created_at": row["created_at"],
        "operations": ops,
    }


# ---------------- 查询（直接读已生效的图） ----------------

def _load_group(conn: sqlite3.Connection, group_id: int) -> dict:
    strata = [
        r["stratum_no"]
        for r in conn.execute(
            "SELECT stratum_no FROM strata WHERE group_id=? ORDER BY stratum_no",
            (group_id,),
        )
    ]
    edges: list[dict] = []
    edge_rows = conn.execute(
        "SELECT before_no, after_no FROM graph_edges "
        "WHERE group_id=? ORDER BY before_no, after_no",
        (group_id,),
    ).fetchall()
    for edge in edge_rows:
        records = [
            r["record_no"]
            for r in conn.execute(
                "SELECT record_no FROM edge_support "
                "WHERE before_no=? AND after_no=? ORDER BY record_no",
                (edge["before_no"], edge["after_no"]),
            )
        ]
        edges.append(
            {"before": edge["before_no"], "after": edge["after_no"],
             "record_numbers": records}
        )
    return {"group_id": group_id, "strata": strata, "edges": edges}


def get_group_for_stratum(stratum_no: str, db_path: str | None = None) -> dict:
    with db_session(db_path) as conn:
        row = conn.execute(
            "SELECT group_id FROM strata WHERE stratum_no=?", (stratum_no,)
        ).fetchone()
        if row is None:
            raise ValidationFailure(f"层位不存在: {stratum_no}")
        if row["group_id"] is None:
            return {
                "group_id": None,
                "strata": [stratum_no],
                "edges": [],
            }
        return _load_group(conn, row["group_id"])


def list_groups(db_path: str | None = None) -> list[dict]:
    with db_session(db_path) as conn:
        ids = [
            r["group_id"]
            for r in conn.execute(
                "SELECT group_id FROM groups ORDER BY group_id"
            )
        ]
        return [_load_group(conn, gid) for gid in ids]


def list_strata(db_path: str | None = None) -> list[dict]:
    with db_session(db_path) as conn:
        return [
            {"stratum_no": r["stratum_no"], "group_id": r["group_id"]}
            for r in conn.execute(
                "SELECT stratum_no, group_id FROM strata ORDER BY stratum_no"
            )
        ]


def list_records(
    active_only: bool, db_path: str | None = None
) -> list[dict]:
    sql = (
        "SELECT record_no, before_no, after_no, active, created_at, "
        "updated_at, retracted_at FROM records"
    )
    params: tuple = ()
    if active_only:
        sql += " WHERE active=1"
    sql += " ORDER BY record_no"
    with db_session(db_path) as conn:
        return [
            {
                "record_no": r["record_no"],
                "before": r["before_no"],
                "after": r["after_no"],
                "active": bool(r["active"]),
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "retracted_at": r["retracted_at"],
            }
            for r in conn.execute(sql, params)
        ]


def ensure_initialized(db_path: str | None = None) -> None:
    init_db(db_path)
