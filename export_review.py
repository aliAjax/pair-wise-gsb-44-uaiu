"""数据出境审查：出境资料、发送判定与存储各自负责。

- 出境资料：export_reviews 表（目的地、接收方、标准合同号、影响评估号、监护人同意）
- 判定：evaluate_send_gate 纯函数，只读资料，给出发送入口开关和缺口清单
- 存储：ExportReviewStore 负责表结构、审查编号和查询，不做判定
"""
from __future__ import annotations

import sqlite3
from typing import Any, Iterable

# 高风险目的地清单是内置模板，部署时按政策替换，不构成法律意见
DEFAULT_HIGH_RISK_DESTINATIONS = frozenset({"US", "GB", "IN", "VN", "PH"})

GAP_DEACTIVATED = "同意已撤回，审查已停用"
GAP_LEGAL_HOLD = "法律保留未解除"
GAP_MINOR_GUARDIAN = "未成年人缺监护人同意"
GAP_CONTRACT_MISSING = "高风险目的地缺标准合同编号"
GAP_ASSESSMENT_MISSING = "高风险目的地缺影响评估编号"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS export_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_no TEXT NOT NULL UNIQUE,
    location_id INTEGER NOT NULL REFERENCES data_locations(id),
    request_id INTEGER NOT NULL REFERENCES requests(id),
    subject_id INTEGER NOT NULL REFERENCES data_subjects(id),
    destination TEXT NOT NULL,
    recipient TEXT NOT NULL,
    high_risk INTEGER NOT NULL DEFAULT 0,
    standard_contract_no TEXT,
    impact_assessment_no TEXT,
    guardian_consent_ref TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    sent_at TEXT,
    deactivated_at TEXT,
    deactivate_reason TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_export_reviews_active
    ON export_reviews(location_id, destination) WHERE status='active';
CREATE INDEX IF NOT EXISTS idx_export_reviews_subject ON export_reviews(subject_id, status);
"""


def _text(value: Any) -> str:
    return (value or "").strip()


def evaluate_send_gate(location: Any, subject: Any, review: Any) -> dict[str, Any]:
    """发送判定：返回发送入口是否开放及缺口清单，不触碰存储。"""
    gaps: list[str] = []
    if review["status"] != "active":
        gaps.append(GAP_DEACTIVATED)
    if location["legal_hold"]:
        gaps.append(GAP_LEGAL_HOLD)
    if subject["is_minor"] and not _text(review["guardian_consent_ref"]):
        gaps.append(GAP_MINOR_GUARDIAN)
    if review["high_risk"]:
        if not _text(review["standard_contract_no"]):
            gaps.append(GAP_CONTRACT_MISSING)
        if not _text(review["impact_assessment_no"]):
            gaps.append(GAP_ASSESSMENT_MISSING)
    return {"sendable": not gaps, "gaps": gaps}


class ExportReviewStore:
    """出境审查的存储：表结构、审查编号和查询，不做判定。"""

    def __init__(self, high_risk_destinations: Iterable[str] | None = None):
        source = high_risk_destinations if high_risk_destinations is not None else DEFAULT_HIGH_RISK_DESTINATIONS
        self.high_risk = {str(d).strip().upper() for d in source}

    def schema(self) -> str:
        return SCHEMA_SQL

    def is_high_risk(self, destination: str) -> bool:
        return destination.strip().upper() in self.high_risk

    def _next_review_no(self, conn: sqlite3.Connection) -> str:
        row = conn.execute("SELECT COALESCE(MAX(id),0)+1 AS n FROM export_reviews").fetchone()
        return "ER-%06d" % row["n"]

    def get(self, conn: sqlite3.Connection, review_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM export_reviews WHERE id=?", (review_id,)).fetchone()

    def active_review(self, conn: sqlite3.Connection, location_id: int, destination: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM export_reviews WHERE location_id=? AND destination=? AND status='active'",
            (location_id, destination.strip().upper()),
        ).fetchone()

    def insert(self, conn: sqlite3.Connection, *, location_id: int, request_id: int, subject_id: int,
               destination: str, recipient: str, high_risk: bool, standard_contract_no: str,
               impact_assessment_no: str, guardian_consent_ref: str, created_by: str, now: str) -> sqlite3.Row:
        review_no = self._next_review_no(conn)
        cur = conn.execute(
            """INSERT INTO export_reviews(review_no,location_id,request_id,subject_id,destination,recipient,
               high_risk,standard_contract_no,impact_assessment_no,guardian_consent_ref,created_by,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (review_no, location_id, request_id, subject_id, destination, recipient, int(bool(high_risk)),
             _text(standard_contract_no) or None, _text(impact_assessment_no) or None,
             _text(guardian_consent_ref) or None, created_by, now),
        )
        return self.get(conn, cur.lastrowid)

    def update_active(self, conn: sqlite3.Connection, review_id: int, *, recipient: str,
                      standard_contract_no: str, impact_assessment_no: str,
                      guardian_consent_ref: str) -> sqlite3.Row:
        """重复提交时补齐资料，沿用原审查编号；留空的字段保留原值。"""
        conn.execute(
            """UPDATE export_reviews SET recipient=?,
               standard_contract_no=COALESCE(?,standard_contract_no),
               impact_assessment_no=COALESCE(?,impact_assessment_no),
               guardian_consent_ref=COALESCE(?,guardian_consent_ref),
               version=version+1
               WHERE id=? AND status='active'""",
            (recipient, _text(standard_contract_no) or None, _text(impact_assessment_no) or None,
             _text(guardian_consent_ref) or None, review_id),
        )
        return self.get(conn, review_id)

    def for_request(self, conn: sqlite3.Connection, request_id: int) -> list[sqlite3.Row]:
        return self.for_requests(conn, [request_id])

    def for_requests(self, conn: sqlite3.Connection, request_ids: Iterable[int]) -> list[sqlite3.Row]:
        ids = [int(i) for i in request_ids]
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        return conn.execute(
            """SELECT er.*, dl.system_name, dl.data_category FROM export_reviews er
               JOIN data_locations dl ON dl.id=er.location_id
               WHERE er.request_id IN (%s) ORDER BY er.id""" % marks,
            ids,
        ).fetchall()

    def deactivate_for_subject(self, conn: sqlite3.Connection, subject_id: int,
                               reason: str, now: str) -> list[sqlite3.Row]:
        rows = conn.execute(
            "SELECT * FROM export_reviews WHERE subject_id=? AND status='active'", (subject_id,),
        ).fetchall()
        conn.execute(
            """UPDATE export_reviews SET status='deactivated',deactivated_at=?,deactivate_reason=?,version=version+1
               WHERE subject_id=? AND status='active'""",
            (now, reason, subject_id),
        )
        return rows

    def mark_sent(self, conn: sqlite3.Connection, review_id: int, now: str) -> sqlite3.Row:
        conn.execute("UPDATE export_reviews SET sent_at=?,version=version+1 WHERE id=?", (now, review_id))
        return self.get(conn, review_id)
