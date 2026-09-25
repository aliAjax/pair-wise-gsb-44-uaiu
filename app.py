"""Personal data-rights request workflow service."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "privacy_requests.db"
REQUEST_TYPES = {"access", "correction", "deletion", "withdraw_consent", "restriction"}
OPEN_STATUSES = {"received", "verifying", "processing", "extended", "response_ready"}
FINAL_STATUSES = {"fulfilled", "rejected", "duplicate"}

EXPORT_CONSENT_PURPOSE = "cross_border_transfer"
RISK_LEVELS = {"standard", "high"}
GAP_MESSAGES = {
    "legal_hold": "存在法律保留，禁止出境发送",
    "minor_guardian_consent": "未成年人缺少监护人出境同意",
    "consent_withdrawn": "数据主体已撤回出境同意",
    "missing_standard_contract_no": "高风险目的地缺少标准合同号",
    "missing_assessment_no": "高风险目的地缺少影响评估号",
}


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_time(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise DomainError("日期格式无效") from exc
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def clean_actor(actor: str) -> str:
    actor = (actor or "").strip()
    if not actor:
        raise DomainError("缺少操作人")
    return actor


def require_role(role: str, allowed: set[str], action: str) -> None:
    if role not in allowed:
        raise DomainError("角色无权执行：%s" % action, 403)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_export_materials(request_id: int, destination_code: str, recipient: str,
                           destination: dict[str, Any], locations: list[sqlite3.Row],
                           subject: dict[str, Any], consent: dict[str, Any] | None) -> dict[str, Any]:
    """出境资料装配：只负责汇总目的地、接收方、数据位置和主体同意事实，不做通过/阻断判定。"""
    if not destination_code.strip() or not recipient.strip():
        raise DomainError("目的地和境外接收方不能为空")
    if destination["code"] != destination_code.strip().upper():
        raise DomainError("目的地登记记录不一致", 500)
    if destination["risk_level"] not in RISK_LEVELS:
        raise DomainError("目的地风险等级无效", 500)
    routed = [dict(row) for row in locations if row["destination_code"] == destination["code"]
              and (row["recipient"] or "").strip() == recipient.strip()]
    if not routed:
        raise DomainError("该目的地/接收方尚未定位任何数据位置", 409)
    return {
        "request_id": request_id,
        "destination_code": destination["code"],
        "destination_name": destination["name"],
        "risk_level": destination["risk_level"],
        "recipient": recipient.strip(),
        "location_ids": [row["id"] for row in routed],
        "systems": sorted({row["system_name"] for row in routed}),
        "legal_hold_location_ids": [row["id"] for row in routed if row["legal_hold"]],
        "subject_id": subject["id"],
        "is_minor": bool(subject["is_minor"]),
        "guardian_consent": bool(subject["is_minor"] and consent and consent["status"] == "granted"
                                 and consent["consenter_kind"] == "guardian"),
        "consent_status": consent["status"] if consent else "none",
    }


def evaluate_export(materials: dict[str, Any], standard_contract_no: str | None,
                    impact_assessment_no: str | None) -> dict[str, Any]:
    """出境判定：纯函数，输入资料与高风险文件号，输出是否放行及缺口清单，不读写存储。"""
    gaps: list[str] = []
    if materials["legal_hold_location_ids"]:
        gaps.append("legal_hold")
    if materials["is_minor"] and not materials["guardian_consent"]:
        gaps.append("minor_guardian_consent")
    if materials["consent_status"] == "withdrawn":
        gaps.append("consent_withdrawn")
    if materials["risk_level"] == "high":
        if not (standard_contract_no or "").strip():
            gaps.append("missing_standard_contract_no")
        if not (impact_assessment_no or "").strip():
            gaps.append("missing_assessment_no")
    return {
        "decision": "approved" if not gaps else "blocked",
        "gaps": gaps,
        "gap_messages": [GAP_MESSAGES[code] for code in gaps],
        "send_enabled": not gaps,
    }


class PrivacyRequestService:
    def __init__(self, db_path: str | os.PathLike[str] = DEFAULT_DB):
        self.db_path = str(db_path)
        self._init_schema()
        self.export_store = ExportReviewStore(self.db_path)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jurisdictions (
                    code TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    response_days INTEGER NOT NULL,
                    max_extension_days INTEGER NOT NULL,
                    minor_guardian_required INTEGER NOT NULL,
                    agent_authority_required INTEGER NOT NULL,
                    updated_by TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS data_subjects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subject_ref TEXT NOT NULL UNIQUE,
                    region TEXT NOT NULL,
                    is_minor INTEGER NOT NULL DEFAULT 0,
                    contact_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_no TEXT NOT NULL UNIQUE,
                    subject_id INTEGER NOT NULL REFERENCES data_subjects(id),
                    request_type TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'received',
                    jurisdiction TEXT NOT NULL REFERENCES jurisdictions(code),
                    requester_kind TEXT NOT NULL,
                    agent_authority_ref TEXT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    duplicate_of INTEGER REFERENCES requests(id),
                    submitted_at TEXT NOT NULL,
                    due_date TEXT NOT NULL,
                    original_due_date TEXT NOT NULL,
                    extension_days INTEGER NOT NULL DEFAULT 0,
                    verified_at TEXT,
                    verified_by TEXT,
                    assigned_to TEXT,
                    denial_reason TEXT,
                    response_summary TEXT,
                    created_by TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS data_locations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id INTEGER NOT NULL REFERENCES requests(id),
                    system_name TEXT NOT NULL,
                    data_category TEXT NOT NULL,
                    owner_team TEXT NOT NULL,
                    contains_third_party INTEGER NOT NULL DEFAULT 0,
                    legal_hold INTEGER NOT NULL DEFAULT 0,
                    retention_exception INTEGER NOT NULL DEFAULT 0,
                    third_party_exception INTEGER NOT NULL DEFAULT 0,
                    destination_code TEXT,
                    recipient TEXT,
                    status TEXT NOT NULL DEFAULT 'located',
                    note TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(request_id,system_name,data_category)
                );
                CREATE TABLE IF NOT EXISTS timeline (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id INTEGER REFERENCES requests(id),
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_requests_due ON requests(status,due_date);
                CREATE INDEX IF NOT EXISTS idx_requests_subject ON requests(subject_id,request_type,submitted_at);
                """
            )
            self._migrate_locations_for_export(conn)
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_locations_destination
                   ON data_locations(destination_code,recipient) WHERE destination_code IS NOT NULL""")

    def _migrate_locations_for_export(self, conn: sqlite3.Connection) -> None:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(data_locations)").fetchall()}
        if "destination_code" not in cols:
            conn.execute("ALTER TABLE data_locations ADD COLUMN destination_code TEXT")
        if "recipient" not in cols:
            conn.execute("ALTER TABLE data_locations ADD COLUMN recipient TEXT")

    def _audit(self, conn: sqlite3.Connection, request_id: int | None, actor: str,
               action: str, details: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO timeline(request_id,actor,action,details,created_at) VALUES(?,?,?,?,?)",
            (request_id, actor, action, json.dumps(details, ensure_ascii=False, sort_keys=True), utcnow()),
        )

    def configure_jurisdiction(self, actor: str, role: str, code: str, name: str,
                               response_days: int, max_extension_days: int,
                               minor_guardian_required: bool = True,
                               agent_authority_required: bool = True) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"supervisor"}, "配置地区规则")
        code, name = code.strip().upper(), name.strip()
        try:
            response_days, max_extension_days = int(response_days), int(max_extension_days)
        except (TypeError, ValueError) as exc:
            raise DomainError("时限必须是整数") from exc
        if not code or not name or not 1 <= response_days <= 180 or not 0 <= max_extension_days <= 180:
            raise DomainError("地区规则参数无效")
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO jurisdictions(code,name,response_days,max_extension_days,minor_guardian_required,agent_authority_required,updated_by,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(code) DO UPDATE SET name=excluded.name,response_days=excluded.response_days,
                   max_extension_days=excluded.max_extension_days,minor_guardian_required=excluded.minor_guardian_required,
                   agent_authority_required=excluded.agent_authority_required,updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
                (code, name, response_days, max_extension_days, int(bool(minor_guardian_required)), int(bool(agent_authority_required)), actor, utcnow()),
            )
            self._audit(conn, None, actor, "jurisdiction.configured", {"code": code})
            return dict(conn.execute("SELECT * FROM jurisdictions WHERE code=?", (code,)).fetchone())

    def create_subject(self, actor: str, role: str, subject_ref: str, region: str,
                       is_minor: bool, contact: str) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"intake", "privacy_officer"}, "建立数据主体索引")
        if not subject_ref.strip() or not region.strip() or not contact.strip():
            raise DomainError("主体编号、地区和联系方式不能为空")
        with self.connect() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO data_subjects(subject_ref,region,is_minor,contact_hash,created_at) VALUES(?,?,?,?,?)",
                    (subject_ref.strip(), region.strip().upper(), int(bool(is_minor)), sha256_text(contact.strip().lower()), utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("数据主体索引已存在", 409) from exc
            self._audit(conn, None, actor, "subject.created", {"subject_ref": subject_ref.strip()})
            return dict(conn.execute("SELECT id,subject_ref,region,is_minor,created_at FROM data_subjects WHERE id=?", (cur.lastrowid,)).fetchone())

    def create_request(self, actor: str, role: str, request_no: str, subject_id: int,
                       request_type: str, idempotency_key: str, requester_kind: str = "self",
                       agent_authority_ref: str | None = None) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"intake", "privacy_officer"}, "创建权利请求")
        request_type = request_type.strip().lower()
        requester_kind = requester_kind.strip().lower()
        if request_type not in REQUEST_TYPES or requester_kind not in {"self", "guardian", "authorized_agent"}:
            raise DomainError("请求类型或申请人类型无效")
        if not request_no.strip() or not idempotency_key.strip():
            raise DomainError("请求编号和幂等键不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM requests WHERE idempotency_key=?", (idempotency_key.strip(),)).fetchone()
            if existing:
                return {"idempotent": True, "request": dict(existing)}
            subject = conn.execute("SELECT * FROM data_subjects WHERE id=?", (subject_id,)).fetchone()
            if not subject:
                raise DomainError("数据主体不存在", 404)
            jurisdiction = conn.execute("SELECT * FROM jurisdictions WHERE code=?", (subject["region"],)).fetchone()
            if not jurisdiction:
                raise DomainError("数据主体所在地区尚未配置处理规则", 409)
            if subject["is_minor"] and jurisdiction["minor_guardian_required"] and requester_kind != "guardian":
                raise DomainError("未成年人请求必须由监护人提出", 403)
            if requester_kind == "authorized_agent" and jurisdiction["agent_authority_required"] and not (agent_authority_ref or "").strip():
                raise DomainError("授权代理必须提供有效授权引用", 403)
            now_dt = datetime.now(timezone.utc)
            duplicate = conn.execute(
                """SELECT * FROM requests WHERE subject_id=? AND request_type=? AND status IN ('received','verifying','processing','extended','response_ready')
                   ORDER BY id DESC LIMIT 1""",
                (subject_id, request_type),
            ).fetchone()
            duplicate_of = None
            if duplicate:
                elapsed = (now_dt - parse_time(duplicate["submitted_at"])).total_seconds()
                if 0 <= elapsed <= 30 * 86400:
                    duplicate_of = duplicate["id"]
            now = now_dt.isoformat(timespec="seconds")
            due = (now_dt + timedelta(days=jurisdiction["response_days"])).isoformat(timespec="seconds")
            status = "duplicate" if duplicate_of else "received"
            try:
                cur = conn.execute(
                    """INSERT INTO requests(request_no,subject_id,request_type,status,jurisdiction,requester_kind,agent_authority_ref,
                       idempotency_key,duplicate_of,submitted_at,due_date,original_due_date,created_by,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (request_no.strip(), subject_id, request_type, status, subject["region"], requester_kind,
                     (agent_authority_ref or "").strip() or None, idempotency_key.strip(), duplicate_of,
                     now, due, due, actor, now),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("请求编号已存在", 409) from exc
            request_id = cur.lastrowid
            self._audit(conn, request_id, actor, "request.created", {"type": request_type, "duplicate_of": duplicate_of})
            if duplicate_of:
                self._audit(conn, duplicate_of, actor, "request.duplicate_detected", {"new_request": request_no.strip()})
            return {"idempotent": False, "request": dict(conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone())}

    def _request(self, conn: sqlite3.Connection, request_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
        if not row:
            raise DomainError("权利请求不存在", 404)
        return row

    def verify_identity(self, actor: str, role: str, request_id: int, expected_version: int,
                        identity_evidence_ref: str) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"privacy_officer", "supervisor"}, "核验身份")
        if not identity_evidence_ref.strip():
            raise DomainError("身份核验引用不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            req = self._request(conn, request_id)
            if req["status"] != "received":
                raise DomainError("当前请求不能核验身份", 409)
            if req["version"] != int(expected_version):
                raise DomainError("请求已变化，请刷新后重试", 409)
            conn.execute(
                "UPDATE requests SET status='processing',verified_at=?,verified_by=?,version=version+1,updated_at=? WHERE id=? AND version=?",
                (utcnow(), actor, utcnow(), request_id, expected_version),
            )
            self._audit(conn, request_id, actor, "identity.verified", {"evidence_ref": identity_evidence_ref.strip()})
            return dict(self._request(conn, request_id))

    def assign_request(self, actor: str, role: str, request_id: int, assignee: str,
                       expected_version: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"supervisor"}, "分配请求")
        if not assignee.strip():
            raise DomainError("处理人不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            req = self._request(conn, request_id)
            if req["status"] not in {"processing", "extended"}:
                raise DomainError("当前请求不能分配", 409)
            if req["version"] != int(expected_version):
                raise DomainError("请求已变化，请刷新后重试", 409)
            conn.execute("UPDATE requests SET assigned_to=?,version=version+1,updated_at=? WHERE id=? AND version=?", (assignee.strip(), utcnow(), request_id, expected_version))
            self._audit(conn, request_id, actor, "request.assigned", {"assignee": assignee.strip()})
            return dict(self._request(conn, request_id))

    def _can_process(self, actor: str, role: str, req: sqlite3.Row, action: str) -> None:
        if role == "supervisor":
            return
        if role == "privacy_officer" and req["assigned_to"] == actor:
            return
        raise DomainError("只有被指派的隐私处理人员可以%s" % action, 403)

    def add_data_location(self, actor: str, role: str, request_id: int, system_name: str,
                          data_category: str, owner_team: str) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"privacy_officer", "supervisor"}, "定位数据")
        if not system_name.strip() or not data_category.strip() or not owner_team.strip():
            raise DomainError("系统、数据类别和负责团队不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            req = self._request(conn, request_id)
            if req["status"] not in {"processing", "extended"}:
                raise DomainError("请求当前不能定位数据", 409)
            self._can_process(actor, role, req, "定位数据")
            try:
                cur = conn.execute(
                    """INSERT INTO data_locations(request_id,system_name,data_category,owner_team,created_at,updated_at)
                       VALUES(?,?,?,?,?,?)""",
                    (request_id, system_name.strip(), data_category.strip(), owner_team.strip(), utcnow(), utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("同一系统的数据类别已经登记", 409) from exc
            self._audit(conn, request_id, actor, "location.added", {"system": system_name.strip(), "category": data_category.strip()})
            return dict(conn.execute("SELECT * FROM data_locations WHERE id=?", (cur.lastrowid,)).fetchone())

    def classify_location(self, actor: str, role: str, location_id: int,
                          contains_third_party: bool, legal_hold: bool,
                          retention_exception: bool, note: str = "",
                          third_party_exception: bool = False) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"privacy_officer", "supervisor"}, "分类数据位置")
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM data_locations WHERE id=?", (location_id,)).fetchone()
            if not row:
                raise DomainError("数据位置不存在", 404)
            req = self._request(conn, row["request_id"])
            self._can_process(actor, role, req, "分类数据位置")
            if req["status"] not in {"processing", "extended"}:
                raise DomainError("请求当前不能分类数据", 409)
            if row["status"] != "located":
                raise DomainError("数据位置已经分类", 409)
            if third_party_exception and not contains_third_party:
                raise DomainError("不存在第三方数据时不能使用第三方例外", 409)
            if req["request_type"] == "deletion" and (legal_hold or retention_exception):
                status = "blocked"
            elif req["request_type"] == "access" and contains_third_party and not (third_party_exception or note.strip()):
                status = "needs_redaction"
            else:
                status = "classified"
            conn.execute(
                """UPDATE data_locations SET contains_third_party=?,legal_hold=?,retention_exception=?,third_party_exception=?,status=?,note=?,version=version+1,updated_at=?
                   WHERE id=? AND status='located'""",
                (int(bool(contains_third_party)), int(bool(legal_hold)), int(bool(retention_exception)),
                 int(bool(third_party_exception)), status, note.strip(), utcnow(), location_id),
            )
            self._audit(conn, row["request_id"], actor, "location.classified", {"location_id": location_id, "status": status})
            return dict(conn.execute("SELECT * FROM data_locations WHERE id=?", (location_id,)).fetchone())

    def register_location_destination(self, actor: str, role: str, location_id: int,
                                       destination_code: str, recipient: str,
                                       expected_version: int) -> dict[str, Any]:
        """出境资料：为一条已定位数据登记目的地与境外接收方（判定不在此层）。"""
        actor = clean_actor(actor)
        require_role(role, {"privacy_officer", "supervisor"}, "登记出境目的地")
        destination_code = destination_code.strip().upper()
        recipient = recipient.strip()
        if not destination_code or not recipient:
            raise DomainError("目的地代码和境外接收方不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM data_locations WHERE id=?", (location_id,)).fetchone()
            if not row:
                raise DomainError("数据位置不存在", 404)
            req = self._request(conn, row["request_id"])
            self._can_process(actor, role, req, "登记出境目的地")
            if req["status"] not in {"processing", "extended"}:
                raise DomainError("请求当前不能登记出境目的地", 409)
            if row["version"] != int(expected_version):
                raise DomainError("数据位置已变化，请刷新后重试", 409)
            conn.execute(
                "UPDATE data_locations SET destination_code=?,recipient=?,version=version+1,updated_at=? WHERE id=? AND version=?",
                (destination_code, recipient, utcnow(), location_id, expected_version),
            )
            self._audit(conn, row["request_id"], actor, "location.destination_registered",
                        {"location_id": location_id, "destination": destination_code, "recipient": recipient})
            return dict(conn.execute("SELECT * FROM data_locations WHERE id=?", (location_id,)).fetchone())

    def extend_request(self, actor: str, role: str, request_id: int, days: int,
                       reason: str, expected_version: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"supervisor"}, "延期处理")
        if not reason.strip():
            raise DomainError("延期原因不能为空")
        try:
            days = int(days)
        except (TypeError, ValueError) as exc:
            raise DomainError("延期天数必须是整数") from exc
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            req = self._request(conn, request_id)
            if req["status"] not in {"processing", "extended"}:
                raise DomainError("当前请求不能延期", 409)
            if req["version"] != int(expected_version):
                raise DomainError("请求已变化，请刷新后重试", 409)
            jurisdiction = conn.execute("SELECT * FROM jurisdictions WHERE code=?", (req["jurisdiction"],)).fetchone()
            if req["extension_days"] > 0:
                raise DomainError("每个请求只能延期一次", 409)
            if days <= 0 or days > jurisdiction["max_extension_days"]:
                raise DomainError("延期天数超出地区上限", 409)
            due = (parse_time(req["due_date"]) + timedelta(days=days)).isoformat(timespec="seconds")
            conn.execute(
                "UPDATE requests SET status='extended',due_date=?,extension_days=?,version=version+1,updated_at=? WHERE id=? AND version=?",
                (due, days, utcnow(), request_id, expected_version),
            )
            self._audit(conn, request_id, actor, "request.extended", {"days": days, "reason": reason.strip(), "due_date": due})
            return dict(self._request(conn, request_id))

    def prepare_response(self, actor: str, role: str, request_id: int,
                         expected_version: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"privacy_officer", "supervisor"}, "准备回复")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            req = self._request(conn, request_id)
            self._can_process(actor, role, req, "准备回复")
            if req["status"] not in {"processing", "extended"}:
                raise DomainError("当前请求不能准备回复", 409)
            if req["version"] != int(expected_version):
                raise DomainError("请求已变化，请刷新后重试", 409)
            locations = conn.execute("SELECT * FROM data_locations WHERE request_id=?", (request_id,)).fetchall()
            if not locations:
                raise DomainError("尚未登记任何数据位置，不能回复", 409)
            unresolved = [row["id"] for row in locations if row["status"] in {"located", "needs_redaction"}]
            if unresolved:
                raise DomainError("仍有数据位置未分类或未完成去标识", 409)
            if req["request_type"] == "deletion":
                blocked = [row["id"] for row in locations if row["status"] == "blocked" or row["legal_hold"] or row["retention_exception"]]
                if blocked:
                    raise DomainError("存在法律保留或保存义务，不能执行删除", 409)
            if req["request_type"] == "access":
                bad = [row["id"] for row in locations if row["contains_third_party"] and not row["third_party_exception"] and not row["note"].strip()]
                if bad:
                    raise DomainError("第三方数据尚未完成去标识或例外说明", 409)
            conn.execute("UPDATE requests SET status='response_ready',version=version+1,updated_at=? WHERE id=? AND version=?", (utcnow(), request_id, expected_version))
            self._audit(conn, request_id, actor, "response.prepared", {"location_count": len(locations)})
            return dict(self._request(conn, request_id))

    def fulfill_request(self, actor: str, role: str, request_id: int, response_summary: str,
                        expected_version: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"privacy_officer", "supervisor"}, "完成请求")
        if not response_summary.strip():
            raise DomainError("回复摘要不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            req = self._request(conn, request_id)
            self._can_process(actor, role, req, "完成请求")
            if req["status"] != "response_ready":
                raise DomainError("请求尚未完成回复准备", 409)
            if req["version"] != int(expected_version):
                raise DomainError("请求已变化，请刷新后重试", 409)
            conn.execute(
                "UPDATE requests SET status='fulfilled',response_summary=?,version=version+1,updated_at=? WHERE id=? AND version=?",
                (response_summary.strip(), utcnow(), request_id, expected_version),
            )
            self._audit(conn, request_id, actor, "request.fulfilled", {"summary": response_summary.strip()})
            return dict(self._request(conn, request_id))

    def reject_request(self, actor: str, role: str, request_id: int, reason: str,
                       expected_version: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"privacy_officer", "supervisor"}, "拒绝请求")
        if not reason.strip():
            raise DomainError("拒绝理由不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            req = self._request(conn, request_id)
            self._can_process(actor, role, req, "拒绝请求")
            if req["status"] not in OPEN_STATUSES:
                raise DomainError("当前请求不能拒绝", 409)
            if req["version"] != int(expected_version):
                raise DomainError("请求已变化，请刷新后重试", 409)
            conn.execute("UPDATE requests SET status='rejected',denial_reason=?,version=version+1,updated_at=? WHERE id=? AND version=?", (reason.strip(), utcnow(), request_id, expected_version))
            self._audit(conn, request_id, actor, "request.rejected", {"reason": reason.strip()})
            return dict(self._request(conn, request_id))

    def _visibility(self, actor: str, role: str, conn: sqlite3.Connection) -> list[sqlite3.Row]:
        if role in {"supervisor", "auditor"}:
            return conn.execute("SELECT * FROM requests ORDER BY due_date,id").fetchall()
        if role == "privacy_officer":
            return conn.execute("SELECT * FROM requests WHERE assigned_to=? ORDER BY due_date,id", (actor,)).fetchall()
        if role == "intake":
            return conn.execute("SELECT * FROM requests WHERE created_by=? ORDER BY id DESC", (actor,)).fetchall()
        return []

    def get_request(self, actor: str, role: str, request_id: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        with self.connect() as conn:
            req = self._request(conn, request_id)
            if role in {"supervisor", "auditor"}:
                pass
            elif role == "privacy_officer" and req["assigned_to"] == actor:
                pass
            elif role == "intake" and req["created_by"] == actor:
                pass
            else:
                raise DomainError("无权查看该权利请求", 403)
            locations = [dict(r) for r in conn.execute("SELECT * FROM data_locations WHERE request_id=? ORDER BY id", (request_id,)).fetchall()]
            timeline = [dict(r) for r in conn.execute("SELECT * FROM timeline WHERE request_id=? ORDER BY id", (request_id,)).fetchall()]
            export_reviews = self.export_store.list_for_requests(conn, [request_id])
            export_gate = self._export_gate(conn, request_id, export_reviews)
            return {"request": dict(req), "locations": locations, "timeline": timeline,
                    "export_reviews": export_reviews, "export_gate": export_gate}

    def _export_gate(self, conn: sqlite3.Connection, request_id: int,
                     reviews: list[dict[str, Any]]) -> dict[str, Any]:
        channels = []
        for item in reviews:
            if item["status"] in {"approved", "blocked"}:
                channels.append({
                    "review_no": item["review_no"], "destination_code": item["destination_code"],
                    "recipient": item["recipient"], "status": item["status"],
                    "gaps": item["gaps"], "gap_messages": item["gap_messages"],
                    "send_enabled": item["status"] == "approved",
                })
        unregistered = [
            {"location_id": r["id"], "system_name": r["system_name"]}
            for r in conn.execute(
                "SELECT id,system_name FROM data_locations WHERE request_id=? AND destination_code IS NULL",
                (request_id,)).fetchall()
        ]
        return {
            "send_enabled": bool(channels) and not unregistered
            and all(c["send_enabled"] for c in channels),
            "channels": channels,
            "unregistered_locations": unregistered,
        }

    def queue(self, actor: str, role: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = self._visibility(actor, role, conn)
        now = datetime.now(timezone.utc)
        result = []
        for row in rows:
            item = dict(row)
            item["overdue"] = parse_time(item["due_date"]) < now and item["status"] in OPEN_STATUSES
            result.append(item)
        return result

    def state(self, actor: str = "", role: str = "viewer") -> dict[str, Any]:
        with self.connect() as conn:
            rows = self._visibility(actor, role, conn)
            requests = []
            locations = []
            request_ids = []
            for row in rows:
                item = dict(row)
                item["overdue"] = parse_time(item["due_date"]) < datetime.now(timezone.utc) and item["status"] in OPEN_STATUSES
                requests.append(item)
                request_ids.append(row["id"])
                locations.extend(dict(r) for r in conn.execute("SELECT * FROM data_locations WHERE request_id=? ORDER BY id", (row["id"],)).fetchall())
            timeline = [dict(r) for r in conn.execute("SELECT * FROM timeline ORDER BY id DESC LIMIT 300").fetchall()]
            jurisdictions = [dict(r) for r in conn.execute("SELECT * FROM jurisdictions ORDER BY code").fetchall()]
            export_destinations = self.export_store.list_destinations(conn)
            export_reviews = self.export_store.list_for_requests(conn, request_ids)
        return {"requests": requests, "locations": locations, "timeline": timeline,
                "jurisdictions": jurisdictions, "export_destinations": export_destinations,
                "export_reviews": export_reviews, "access_limited": not bool(requests)}

    def seed_demo(self) -> dict[str, Any]:
        with self.connect() as conn:
            if conn.execute("SELECT COUNT(*) AS c FROM requests").fetchone()["c"]:
                return {"seeded": False, "reason": "已有数据"}
        self.configure_jurisdiction("sup-demo", "supervisor", "CN", "中国", 30, 30, True, True)
        subject = self.create_subject("intake-demo", "intake", "SUBJ-DEMO-001", "CN", False, "demo@example.test")
        req = self.create_request("intake-demo", "intake", "PR-DEMO-001", subject["id"], "access", "demo-idem-001")["request"]
        req = self.verify_identity("officer1", "privacy_officer", req["id"], req["version"], "ID-DEMO")
        req = self.assign_request("sup-demo", "supervisor", req["id"], "officer1", req["version"])
        crm = self.add_data_location("officer1", "privacy_officer", req["id"], "CRM", "profile", "customer")
        support = self.add_data_location("officer1", "privacy_officer", req["id"], "SUPPORT", "messages", "service")
        crm = self.classify_location("officer1", "privacy_officer", crm["id"], False, False, False)
        support = self.classify_location("officer1", "privacy_officer", support["id"], False, False, False)
        self.register_location_destination("officer1", "privacy_officer", crm["id"], "SG",
                                           "Acme SG Pte Ltd", crm["version"])
        self.register_location_destination("officer1", "privacy_officer", support["id"], "US-HIGH",
                                           "Acme US Inc", support["version"])
        export = ExportReviewService(self.db_path)
        export.configure_destination("sup-demo", "supervisor", "SG", "新加坡节点", "SG", "standard")
        export.configure_destination("sup-demo", "supervisor", "US-HIGH", "美国分析节点", "US", "high")
        export.submit_review("officer1", "privacy_officer", req["id"], "SG", "Acme SG Pte Ltd")
        blocked = export.submit_review("officer1", "privacy_officer", req["id"], "US-HIGH", "Acme US Inc")
        reused = export.submit_review(
            "officer1", "privacy_officer", req["id"], "US-HIGH", "Acme US Inc",
            "SCC-DEMO-0001", "PIA-DEMO-0001", blocked["version"])
        return {"seeded": True, "request_id": req["id"], "subject_id": subject["id"],
                "blocked_review_no": blocked["review_no"], "approved_review_no": reused["review_no"]}


class ExportReviewStore:
    """出境审查存储层：只管目的地登记、主体同意和审查记录的持久化，不含判定规则。"""

    def __init__(self, db_path: str | os.PathLike[str] = DEFAULT_DB):
        self.db_path = str(db_path)
        self.init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS export_destinations (
                    code TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    country TEXT NOT NULL,
                    risk_level TEXT NOT NULL CHECK(risk_level IN ('standard','high')),
                    updated_by TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS subject_export_consents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subject_id INTEGER NOT NULL REFERENCES data_subjects(id),
                    purpose TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('granted','withdrawn')),
                    consenter_kind TEXT NOT NULL CHECK(consenter_kind IN ('self','guardian')),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    withdrawn_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_export_consents_subject
                    ON subject_export_consents(subject_id,purpose,id);
                CREATE TABLE IF NOT EXISTS export_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    review_no TEXT NOT NULL UNIQUE,
                    request_id INTEGER NOT NULL REFERENCES requests(id),
                    destination_code TEXT NOT NULL REFERENCES export_destinations(code),
                    recipient TEXT NOT NULL,
                    standard_contract_no TEXT,
                    impact_assessment_no TEXT,
                    status TEXT NOT NULL CHECK(status IN ('approved','blocked','inactive')),
                    gaps TEXT NOT NULL DEFAULT '[]',
                    basis TEXT NOT NULL,
                    deactivated_reason TEXT,
                    deactivated_at TEXT,
                    created_by TEXT NOT NULL,
                    updated_by TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_export_reviews_active
                    ON export_reviews(request_id,destination_code,recipient)
                    WHERE status IN ('approved','blocked');
                """
            )

    def audit(self, conn: sqlite3.Connection, request_id: int | None, actor: str,
              action: str, details: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO timeline(request_id,actor,action,details,created_at) VALUES(?,?,?,?,?)",
            (request_id, actor, action, json.dumps(details, ensure_ascii=False, sort_keys=True), utcnow()),
        )

    def upsert_destination(self, conn: sqlite3.Connection, code: str, name: str, country: str,
                           risk_level: str, actor: str) -> dict[str, Any]:
        now = utcnow()
        conn.execute(
            """INSERT INTO export_destinations(code,name,country,risk_level,updated_by,updated_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(code) DO UPDATE SET name=excluded.name,country=excluded.country,
               risk_level=excluded.risk_level,updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
            (code, name, country, risk_level, actor, now),
        )
        return dict(conn.execute("SELECT * FROM export_destinations WHERE code=?", (code,)).fetchone())

    def get_destination(self, conn: sqlite3.Connection, code: str) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM export_destinations WHERE code=?", (code,)).fetchone()

    def list_destinations(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM export_destinations ORDER BY risk_level,code").fetchall()]

    def current_consent(self, conn: sqlite3.Connection, subject_id: int) -> sqlite3.Row | None:
        return conn.execute(
            """SELECT * FROM subject_export_consents WHERE subject_id=? AND purpose=?
               ORDER BY id DESC LIMIT 1""",
            (subject_id, EXPORT_CONSENT_PURPOSE),
        ).fetchone()

    def add_consent(self, conn: sqlite3.Connection, subject_id: int, status: str,
                    consenter_kind: str, actor: str) -> dict[str, Any]:
        now = utcnow()
        withdrawn = now if status == "withdrawn" else None
        cur = conn.execute(
            """INSERT INTO subject_export_consents(subject_id,purpose,status,consenter_kind,created_by,created_at,withdrawn_at)
               VALUES(?,?,?,?,?,?,?)""",
            (subject_id, EXPORT_CONSENT_PURPOSE, status, consenter_kind, actor, now, withdrawn),
        )
        return dict(conn.execute("SELECT * FROM subject_export_consents WHERE id=?", (cur.lastrowid,)).fetchone())

    def find_active_review(self, conn: sqlite3.Connection, request_id: int,
                           destination_code: str, recipient: str) -> sqlite3.Row | None:
        return conn.execute(
            """SELECT * FROM export_reviews WHERE request_id=? AND destination_code=? AND recipient=?
               AND status IN ('approved','blocked')""",
            (request_id, destination_code, recipient),
        ).fetchone()

    def next_review_no(self, conn: sqlite3.Connection) -> str:
        row = conn.execute("SELECT COUNT(*) AS c FROM export_reviews").fetchone()
        return "ER-%05d" % (row["c"] + 1)

    def insert_review(self, conn: sqlite3.Connection, review_no: str, request_id: int,
                      destination_code: str, recipient: str, standard_contract_no: str | None,
                      impact_assessment_no: str | None, status: str, gaps: list[str],
                      basis: dict[str, Any], actor: str) -> dict[str, Any]:
        now = utcnow()
        cur = conn.execute(
            """INSERT INTO export_reviews(review_no,request_id,destination_code,recipient,standard_contract_no,
               impact_assessment_no,status,gaps,basis,created_by,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (review_no, request_id, destination_code, recipient, standard_contract_no,
             impact_assessment_no, status, json.dumps(gaps, ensure_ascii=False),
             json.dumps(basis, ensure_ascii=False, sort_keys=True), actor, now, now),
        )
        return dict(conn.execute("SELECT * FROM export_reviews WHERE id=?", (cur.lastrowid,)).fetchone())

    def update_review(self, conn: sqlite3.Connection, review_id: int, standard_contract_no: str | None,
                      impact_assessment_no: str | None, status: str, gaps: list[str],
                      basis: dict[str, Any], actor: str, expected_version: int) -> None:
        cur = conn.execute(
            """UPDATE export_reviews SET standard_contract_no=?,impact_assessment_no=?,status=?,gaps=?,basis=?,
               updated_by=?,updated_at=?,version=version+1 WHERE id=? AND version=?""",
            (standard_contract_no, impact_assessment_no, status,
             json.dumps(gaps, ensure_ascii=False), json.dumps(basis, ensure_ascii=False, sort_keys=True),
             actor, utcnow(), review_id, expected_version),
        )
        if cur.rowcount == 0:
            raise DomainError("审查记录已变化，请刷新后重试", 409)

    def deactivate_approved(self, conn: sqlite3.Connection, subject_id: int, reason: str) -> list[int]:
        rows = conn.execute(
            """SELECT er.id FROM export_reviews er JOIN requests r ON er.request_id=r.id
               WHERE r.subject_id=? AND er.status='approved'""",
            (subject_id,),
        ).fetchall()
        ids = [row["id"] for row in rows]
        if ids:
            conn.execute(
                "UPDATE export_reviews SET status='inactive',deactivated_reason=?,deactivated_at=?,updated_at=? WHERE id IN (%s)"
                % ",".join("?" for _ in ids),
                (reason, utcnow(), utcnow(), *ids),
            )
        return ids

    def list_for_requests(self, conn: sqlite3.Connection, request_ids: list[int]) -> list[dict[str, Any]]:
        if not request_ids:
            return []
        placeholders = ",".join("?" for _ in request_ids)
        rows = conn.execute(
            "SELECT * FROM export_reviews WHERE request_id IN (%s) ORDER BY id" % placeholders,
            request_ids,
        ).fetchall()
        return [self.serialize(row) for row in rows]

    def get_review(self, conn: sqlite3.Connection, review_id: int) -> dict[str, Any] | None:
        row = conn.execute("SELECT * FROM export_reviews WHERE id=?", (review_id,)).fetchone()
        return self.serialize(row) if row else None

    @staticmethod
    def serialize(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["gaps"] = json.loads(item["gaps"] or "[]")
        item["basis"] = json.loads(item["basis"] or "{}")
        item["gap_messages"] = [GAP_MESSAGES.get(code, code) for code in item["gaps"]]
        item["send_enabled"] = item["status"] == "approved"
        return item


class ExportReviewService:
    """出境审查服务层：编排资料装配与纯判定，结果交存储层；自身不拼 SQL 规则。"""

    def __init__(self, db_path: str | os.PathLike[str] = DEFAULT_DB):
        self.db_path = str(db_path)
        self.store = ExportReviewStore(self.db_path)

    def _connect(self) -> sqlite3.Connection:
        return self.store.connect()

    def configure_destination(self, actor: str, role: str, code: str, name: str,
                              country: str, risk_level: str) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"supervisor"}, "登记出境目的地")
        code, name, country, risk_level = code.strip().upper(), name.strip(), country.strip(), risk_level.strip().lower()
        if not code or not name or not country or risk_level not in RISK_LEVELS:
            raise DomainError("目的地代码、名称、国家和有效风险等级不能为空")
        with self._connect() as conn:
            destination = self.store.upsert_destination(conn, code, name, country, risk_level, actor)
            self.store.audit(conn, None, actor, "export.destination_configured",
                             {"code": code, "risk_level": risk_level})
            return destination

    def list_destinations(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return self.store.list_destinations(conn)

    def grant_consent(self, actor: str, role: str, subject_id: int,
                      consenter_kind: str) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"intake", "privacy_officer", "supervisor"}, "登记出境同意")
        consenter_kind = consenter_kind.strip().lower()
        if consenter_kind not in {"self", "guardian"}:
            raise DomainError("同意人类型无效")
        with self._connect() as conn:
            subject = conn.execute("SELECT * FROM data_subjects WHERE id=?", (subject_id,)).fetchone()
            if not subject:
                raise DomainError("数据主体不存在", 404)
            if not subject["is_minor"] and consenter_kind == "guardian":
                raise DomainError("成年主体只能由本人同意", 409)
            consent = self.store.add_consent(conn, subject_id, "granted", consenter_kind, actor)
            self.store.audit(conn, None, actor, "export.consent_granted",
                             {"subject_id": subject_id, "consenter_kind": consenter_kind})
            return consent

    def withdraw_consent(self, actor: str, role: str, subject_id: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"intake", "privacy_officer", "supervisor"}, "撤回出境同意")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            subject = conn.execute("SELECT * FROM data_subjects WHERE id=?", (subject_id,)).fetchone()
            if not subject:
                raise DomainError("数据主体不存在", 404)
            consent = self.store.add_consent(conn, subject_id, "withdrawn",
                                             "guardian" if subject["is_minor"] else "self", actor)
            deactivated = self.store.deactivate_approved(
                conn, subject_id, "consent_withdrawn")
            for review_id in deactivated:
                self.store.audit(conn, None, actor, "export.review_deactivated",
                                 {"review_id": review_id, "reason": "consent_withdrawn", "subject_id": subject_id})
            self.store.audit(conn, None, actor, "export.consent_withdrawn",
                             {"subject_id": subject_id, "deactivated_reviews": deactivated})
            return {"consent": consent, "deactivated_review_ids": deactivated}

    def _materials(self, conn: sqlite3.Connection, request_id: int,
                   destination_code: str, recipient: str) -> tuple[sqlite3.Row, sqlite3.Row, dict[str, Any], dict[str, Any]]:
        req = conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
        if not req:
            raise DomainError("权利请求不存在", 404)
        destination = self.store.get_destination(conn, destination_code)
        if not destination:
            raise DomainError("目的地尚未登记，请先由主管配置", 409)
        subject = conn.execute("SELECT * FROM data_subjects WHERE id=?", (req["subject_id"],)).fetchone()
        locations = conn.execute("SELECT * FROM data_locations WHERE request_id=?", (request_id,)).fetchall()
        consent = self.store.current_consent(conn, subject["id"])
        materials = build_export_materials(
            request_id, destination_code, recipient, dict(destination), locations,
            dict(subject), dict(consent) if consent else None)
        return req, subject, dict(destination), materials

    def submit_review(self, actor: str, role: str, request_id: int, destination_code: str,
                      recipient: str, standard_contract_no: str | None = None,
                      impact_assessment_no: str | None = None,
                      expected_version: int | None = None) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"privacy_officer", "supervisor"}, "提交数据出境审查")
        destination_code = destination_code.strip().upper()
        recipient = recipient.strip()
        standard_contract_no = (standard_contract_no or "").strip() or None
        impact_assessment_no = (impact_assessment_no or "").strip() or None
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            req = conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
            if not req:
                raise DomainError("权利请求不存在", 404)
            if role == "privacy_officer" and req["assigned_to"] != actor:
                raise DomainError("只有被指派的隐私处理人员可以提交出境审查", 403)
            if req["status"] not in {"processing", "extended"}:
                raise DomainError("请求当前不能提交出境审查", 409)
            req, _subject, destination, materials = self._materials(conn, request_id, destination_code, recipient)
            decision = evaluate_export(materials, standard_contract_no, impact_assessment_no)
            basis = {"materials": materials, "decision": decision}
            existing = self.store.find_active_review(conn, request_id, destination_code, recipient)
            if existing:
                review_no = existing["review_no"]
                if expected_version is not None and existing["version"] != int(expected_version):
                    raise DomainError("审查记录已变化，请刷新后重试", 409)
                self.store.update_review(conn, existing["id"], standard_contract_no, impact_assessment_no,
                                         decision["decision"], decision["gaps"], basis, actor,
                                         int(expected_version if expected_version is not None else existing["version"]))
                action = "export.review_reused"
                review_id = existing["id"]
            else:
                review_no = self.store.next_review_no(conn)
                saved = self.store.insert_review(conn, review_no, request_id, destination_code, recipient,
                                                 standard_contract_no, impact_assessment_no,
                                                 decision["decision"], decision["gaps"], basis, actor)
                action = "export.review_approved" if decision["decision"] == "approved" else "export.review_blocked"
                review_id = saved["id"]
            self.store.audit(conn, request_id, actor, action,
                             {"review_no": review_no, "destination": destination_code,
                              "recipient": recipient, "decision": decision["decision"], "gaps": decision["gaps"]})
            result = self.store.get_review(conn, review_id)
            result["destination"] = destination
            return result

    def gate(self, actor: str, role: str, request_id: int) -> dict[str, Any]:
        """发送入口：按当前每份有效审查返回是否放行及缺口，不修改任何记录。"""
        with self._connect() as conn:
            req = conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
            if not req:
                raise DomainError("权利请求不存在", 404)
            if role in {"supervisor", "auditor"}:
                pass
            elif role == "privacy_officer" and req["assigned_to"] == actor:
                pass
            elif role == "intake" and req["created_by"] == actor:
                pass
            else:
                raise DomainError("无权查看该出境发送状态", 403)
            rows = conn.execute(
                "SELECT * FROM export_reviews WHERE request_id=? AND status IN ('approved','blocked') ORDER BY id",
                (request_id,),
            ).fetchall()
            channels = []
            for row in rows:
                item = self.store.serialize(row)
                if item["status"] == "blocked":
                    item["send_enabled"] = False
                channels.append({k: item[k] for k in (
                    "id", "review_no", "destination_code", "recipient", "status",
                    "gaps", "gap_messages", "send_enabled")})
            unregistered = [
                {"location_id": r["id"], "system_name": r["system_name"]}
                for r in conn.execute(
                    "SELECT id,system_name FROM data_locations WHERE request_id=? AND destination_code IS NULL",
                    (request_id,)).fetchall()
            ]
            return {
                "request_id": request_id,
                "send_enabled": all(c["send_enabled"] for c in channels) and bool(channels) and not unregistered,
                "channels": channels,
                "unregistered_locations": unregistered,
            }

    def get_review_history(self, actor: str, role: str, review_id: int) -> dict[str, Any]:
        with self._connect() as conn:
            review = self.store.get_review(conn, review_id)
            if not review:
                raise DomainError("出境审查记录不存在", 404)
            req = conn.execute("SELECT * FROM requests WHERE id=?", (review["request_id"],)).fetchone()
            if role in {"supervisor", "auditor"}:
                pass
            elif role == "privacy_officer" and req["assigned_to"] == actor:
                pass
            elif role == "intake" and req["created_by"] == actor:
                pass
            else:
                raise DomainError("无权查看该审查记录", 403)
            review["destination"] = dict(self.store.get_destination(conn, review["destination_code"]))
            review["request_no"] = req["request_no"]
            return review


class ApiHandler(BaseHTTPRequestHandler):
    service: PrivacyRequestService
    export_service: ExportReviewService

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _headers(self) -> tuple[str, str]:
        return self.headers.get("X-User", ""), self.headers.get("X-Role", "viewer")

    def _json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2_000_000:
            raise DomainError("请求体过大", 413)
        if not length:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DomainError("请求体不是有效 JSON") from exc
        if not isinstance(value, dict):
            raise DomainError("JSON 请求体必须是对象")
        return value

    def do_GET(self) -> None:
        try:
            path = urlparse(self.path).path
            if path in {"/", "/index.html"}:
                body = (ROOT / "static" / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            actor, role = self._headers()
            if path == "/health":
                self._send(200, {"status": "ok", "service": "privacy-requests"})
            elif path == "/api/state":
                self._send(200, self.service.state(actor, role))
            elif path == "/api/queue":
                self._send(200, {"queue": self.service.queue(actor, role)})
            elif path.startswith("/api/export-reviews/"):
                self._send(200, self.export_service.get_review_history(actor, role, int(path.split("/")[3])))
            elif path.startswith("/api/requests/"):
                parts = path.split("/")
                if len(parts) == 5 and parts[4] == "export-gate":
                    self._send(200, self.export_service.gate(actor, role, int(parts[3])))
                else:
                    self._send(200, self.service.get_request(actor, role, int(parts[3])))
            else:
                self._send(404, {"error": "接口不存在"})
        except DomainError as exc:
            self._send(exc.status, {"error": str(exc)})
        except (ValueError, IndexError) as exc:
            self._send(400, {"error": str(exc)})

    def do_POST(self) -> None:
        try:
            path, data, (actor, role) = urlparse(self.path).path, self._json(), self._headers()
            if path == "/api/jurisdictions":
                result = self.service.configure_jurisdiction(actor, role, **data)
            elif path == "/api/subjects":
                result = self.service.create_subject(actor, role, **data)
            elif path == "/api/requests":
                result = self.service.create_request(actor, role, **data)
            elif path == "/api/requests/verify":
                result = self.service.verify_identity(actor, role, **data)
            elif path == "/api/requests/assign":
                result = self.service.assign_request(actor, role, **data)
            elif path == "/api/locations":
                result = self.service.add_data_location(actor, role, **data)
            elif path == "/api/locations/classify":
                result = self.service.classify_location(actor, role, **data)
            elif path == "/api/locations/register-destination":
                result = self.service.register_location_destination(actor, role, **data)
            elif path == "/api/export-destinations":
                result = self.export_service.configure_destination(actor, role, **data)
            elif path == "/api/export-consents/grant":
                result = self.export_service.grant_consent(actor, role, **data)
            elif path == "/api/export-consents/withdraw":
                result = self.export_service.withdraw_consent(actor, role, **data)
            elif path == "/api/export-reviews":
                result = self.export_service.submit_review(actor, role, **data)
            elif path == "/api/requests/extend":
                result = self.service.extend_request(actor, role, **data)
            elif path == "/api/requests/prepare":
                result = self.service.prepare_response(actor, role, **data)
            elif path == "/api/requests/fulfill":
                result = self.service.fulfill_request(actor, role, **data)
            elif path == "/api/requests/reject":
                result = self.service.reject_request(actor, role, **data)
            else:
                raise DomainError("接口不存在", 404)
            self._send(201, result)
        except DomainError as exc:
            self._send(exc.status, {"error": str(exc)})
        except (KeyError, TypeError, ValueError) as exc:
            self._send(400, {"error": "请求参数错误: %s" % exc})
        except Exception as exc:
            self._send(500, {"error": "服务器内部错误", "detail": str(exc)})

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def serve(service: PrivacyRequestService, host: str, port: int) -> None:
    ApiHandler.service = service
    ApiHandler.export_service = ExportReviewService(service.db_path)
    server = ThreadingHTTPServer((host, port), ApiHandler)
    print("Privacy request service listening on http://%s:%s" % (host, port))
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="个人数据权利请求处理服务")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8210)
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--seed", action="store_true")
    args = parser.parse_args()
    service = PrivacyRequestService(args.db)
    if args.init:
        print(json.dumps(service.seed_demo() if args.seed else {"initialized": True, "db": args.db}, ensure_ascii=False))
        return
    serve(service, args.host, args.port)


if __name__ == "__main__":
    main()
