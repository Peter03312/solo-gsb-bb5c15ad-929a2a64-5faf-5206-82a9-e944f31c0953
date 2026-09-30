"""正式修订的关系结论变化追踪。

每次成功的正式断言批次、以及有净变更的推演并入，都会产生一个正式修订 N。
本模块在提交事务内计算修订 N-1 -> N 之间“关系结论或最优证据路径”发生变化
的地层对并物化到 revision_changes，供客户端按稳定游标分页读取：

- 同一地层对按编号固定顺序（字典序，stratum_a < stratum_b）只记录一次；
- 关系不变但最优支撑路径改变同样记录；
- 变为未定（undetermined）时该侧路径为空（NULL）；
- 历史修订（本功能上线前已存在）在首次查询时按断言的
  added_revision / retracted_revision 重建当时状态并回填，
  后来撤回的证据在旧侧仍按当时（生效）状态解释；
- 失败批次、推演内编辑、无净变更并入不递增正式修订号，因而不产生变化记录；
- 变化记录与产生它的批次在同一事务提交，分页读取不会看到半批结果。
"""
from __future__ import annotations

import base64
import json
from typing import Any, Dict, List, Optional, Sequence, Tuple

from psycopg2.extras import Json

from .db import db_conn
from .derivation import RELATION_LABELS, Assertion, Derivation
from .errors import ApiError

UNDETERMINED = "undetermined"

# 变化记录条目：(stratum_a, stratum_b, old_relation, old_path, new_relation, new_path)
PairChange = Tuple[str, str, str, Optional[List[str]], str, Optional[List[str]]]


# ------------------------------------------------------------------ 状态重建
def _assertions_at(cur, revision: int) -> List[Assertion]:
    """重建指定正式修订号时刻的生效断言集合（断言只增不删，撤回只置状态）。"""
    cur.execute(
        "SELECT evidence_id, stratum_a, stratum_b, relation FROM assertions "
        "WHERE added_revision <= %s AND (retracted_revision IS NULL OR retracted_revision > %s) "
        "ORDER BY evidence_id",
        (revision, revision),
    )
    return [Assertion(evidence_id=r[0], a=r[1], b=r[2], relation=r[3]) for r in cur.fetchall()]


def _strata_at(cur, revision: int) -> List[str]:
    """指定正式修订号时刻已登记的地层（历史数据迁移时 added_revision 记为 0）。"""
    cur.execute(
        "SELECT stratum_id FROM strata WHERE COALESCE(added_revision, 0) <= %s ORDER BY stratum_id",
        (revision,),
    )
    return [r[0] for r in cur.fetchall()]


# ------------------------------------------------------------------ 变化计算（纯函数，便于测试）
def diff_pair_changes(
    strata_before: Sequence[str],
    strata_after: Sequence[str],
    assertions_before: Sequence[Assertion],
    assertions_after: Sequence[Assertion],
) -> List[PairChange]:
    """比较相邻两个正式修订的状态，返回发生变化的地层对列表。

    地层对按编号字典序固定方向（stratum_a < stratum_b）且只出现一次；
    关系不变但最优证据路径改变也计入；修订前尚未登记的地层对旧侧按未定处理；
    未定一侧路径为 None。
    """
    before_set = set(strata_before)
    d_before = Derivation(assertions_before)
    d_after = Derivation(assertions_after)
    ordered = sorted(strata_after)
    changes: List[PairChange] = []
    for i, x in enumerate(ordered):
        for y in ordered[i + 1 :]:
            if x in before_set and y in before_set:
                old_relation, old_path = d_before.relation(x, y)
            else:
                # 修订前尚未登记的地层对：旧侧视为未定（路径为空）
                old_relation, old_path = UNDETERMINED, None
            new_relation, new_path = d_after.relation(x, y)
            if old_relation == new_relation and old_path == new_path:
                continue
            changes.append((x, y, old_relation, old_path, new_relation, new_path))
    return changes


# ------------------------------------------------------------------ 物化
def record_revision_changes(cur, revision: int) -> int:
    """计算并物化指定正式修订相对上一修订的关系变化（幂等）。返回变化条数。

    须在产生该修订的批次事务内调用（与批次同事务提交），或在历史回填时调用。
    """
    changes = diff_pair_changes(
        _strata_at(cur, revision - 1),
        _strata_at(cur, revision),
        _assertions_at(cur, revision - 1),
        _assertions_at(cur, revision),
    )
    for x, y, old_relation, old_path, new_relation, new_path in changes:
        cur.execute(
            "INSERT INTO revision_changes "
            "(revision, stratum_a, stratum_b, old_relation, old_path, new_relation, new_path) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (revision, stratum_a, stratum_b) DO NOTHING",
            (
                revision,
                x,
                y,
                old_relation,
                Json(old_path) if old_path is not None else None,
                new_relation,
                Json(new_path) if new_path is not None else None,
            ),
        )
    # 标记该修订已计算（区分“无净变化”与“尚未计算”）
    cur.execute(
        "INSERT INTO revision_change_marks (revision) VALUES (%s) ON CONFLICT DO NOTHING",
        (revision,),
    )
    return len(changes)


def _backfill(cur, max_revision: int) -> None:
    """对缺失变化记录的正式修订（含本功能上线前的历史修订）按需重建并物化。

    重建基于不可变历史（added_revision / retracted_revision 一旦写入不再变化），
    结果确定；并发回填由 ON CONFLICT DO NOTHING 保证幂等。
    """
    if max_revision < 1:
        return
    cur.execute("SELECT revision FROM revision_change_marks WHERE revision <= %s", (max_revision,))
    done = {r[0] for r in cur.fetchall()}
    for revision in range(1, max_revision + 1):
        if revision not in done:
            record_revision_changes(cur, revision)


# ------------------------------------------------------------------ 游标
def _encode_cursor(payload: Dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(token: str) -> Dict[str, Any]:
    """解析分页游标；无效游标返回 400 INVALID_CURSOR。"""
    try:
        padded = token + "=" * (-len(token) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        max_revision = payload["max_revision"]
        last = payload["last"]
        if isinstance(max_revision, bool) or not isinstance(max_revision, int) or max_revision < 0:
            raise ValueError
        if not (
            isinstance(last, list)
            and len(last) == 3
            and not isinstance(last[0], bool)
            and isinstance(last[0], int)
            and isinstance(last[1], str)
            and isinstance(last[2], str)
        ):
            raise ValueError
        return {"max_revision": max_revision, "last": (last[0], last[1], last[2])}
    except Exception:
        raise ApiError(400, "INVALID_CURSOR", "分页游标无效或已损坏", {"cursor": token}) from None


# ------------------------------------------------------------------ 查询
def _side_payload(
    relation: str,
    path: Optional[List[str]],
    at_revision: int,
    info: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """展开一侧（旧/新）的结论。证据状态按该侧对应修订时刻解释：
    后来撤回的证据在旧侧仍显示当时的生效状态。"""
    detail: List[Dict[str, Any]] = []
    for eid in path or []:
        a = info.get(eid)
        if a is None:
            continue
        retracted = a["retracted_revision"] is not None and a["retracted_revision"] <= at_revision
        detail.append(
            {
                "evidence_id": eid,
                "stratum_a": a["stratum_a"],
                "stratum_b": a["stratum_b"],
                "relation": a["relation"],
                "status": "retracted" if retracted else "active",
            }
        )
    return {
        "relation": relation,
        "relation_label": RELATION_LABELS[relation],
        "evidence_path": list(path) if path is not None else None,
        "path_detail": detail,
    }


def list_changes(cursor: Optional[str], limit: int) -> Dict[str, Any]:
    """分页读取正式修订间的关系结论变化。

    首轮（无游标）固定当前正式修订号为本轮上界；后续页凭游标沿用同一上界，
    并发提交产生的新修订不影响本轮分页（不漏项、不重复、不读半批）。
    """
    with db_conn() as conn:
        with conn.cursor() as cur:
            if cursor is None:
                cur.execute("SELECT revision FROM revision_counter WHERE id = TRUE")
                max_revision = cur.fetchone()[0]
                last_key = None
            else:
                payload = _decode_cursor(cursor)
                max_revision = payload["max_revision"]
                last_key = payload["last"]

            _backfill(cur, max_revision)

            sql = (
                "SELECT revision, stratum_a, stratum_b, old_relation, old_path, new_relation, new_path "
                "FROM revision_changes WHERE revision <= %s"
            )
            params: List[Any] = [max_revision]
            if last_key is not None:
                sql += " AND (revision, stratum_a, stratum_b) > (%s, %s, %s)"
                params.extend(last_key)
            sql += " ORDER BY revision, stratum_a, stratum_b LIMIT %s"
            params.append(limit + 1)
            cur.execute(sql, params)
            rows = cur.fetchall()

            has_more = len(rows) > limit
            page = rows[:limit]

            # 批量取本页涉及证据的修订区间，用于按“当时状态”展开 path_detail
            eids = set()
            for r in page:
                for path in (r[4], r[6]):
                    if path:
                        eids.update(path)
            info: Dict[str, Dict[str, Any]] = {}
            if eids:
                cur.execute(
                    "SELECT evidence_id, stratum_a, stratum_b, relation, added_revision, retracted_revision "
                    "FROM assertions WHERE evidence_id = ANY(%s)",
                    (list(eids),),
                )
                for row in cur.fetchall():
                    info[row[0]] = {
                        "stratum_a": row[1],
                        "stratum_b": row[2],
                        "relation": row[3],
                        "added_revision": row[4],
                        "retracted_revision": row[5],
                    }

    changes_out: List[Dict[str, Any]] = []
    for revision, a, b, old_relation, old_path, new_relation, new_path in page:
        changes_out.append(
            {
                "revision": revision,
                "stratum_a": a,
                "stratum_b": b,
                "old": _side_payload(old_relation, old_path, revision - 1, info),
                "new": _side_payload(new_relation, new_path, revision, info),
            }
        )
    next_cursor = None
    if has_more:
        last = page[-1]
        next_cursor = _encode_cursor({"max_revision": max_revision, "last": [last[0], last[1], last[2]]})
    return {"max_revision": max_revision, "changes": changes_out, "next_cursor": next_cursor}
