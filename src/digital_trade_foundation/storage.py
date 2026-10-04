"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS delegations (
    delegation_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    liaison_actor_id TEXT REFERENCES actors(actor_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS representatives (
    representative_id TEXT PRIMARY KEY,
    delegation_id TEXT NOT NULL REFERENCES delegations(delegation_id),
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    display_name TEXT NOT NULL,
    clearance TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS resources (
    resource_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    kind TEXT NOT NULL CHECK(kind IN ('interpreter', 'room', 'observer')),
    label TEXT NOT NULL,
    capacity INTEGER NOT NULL CHECK(capacity >= 1),
    sensitivity TEXT NOT NULL,
    site_id TEXT REFERENCES sites(site_id),
    languages_json TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS resource_blocks (
    block_id TEXT PRIMARY KEY,
    resource_id TEXT NOT NULL REFERENCES resources(resource_id),
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recusals (
    recusal_id TEXT PRIMARY KEY,
    representative_id TEXT NOT NULL REFERENCES representatives(representative_id),
    scope_type TEXT NOT NULL CHECK(scope_type IN ('representative', 'organization')),
    scope_value TEXT NOT NULL,
    reason TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(representative_id, scope_type, scope_value)
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    session_type TEXT NOT NULL,
    sensitivity TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    venue_id TEXT NOT NULL REFERENCES resources(resource_id),
    capacity INTEGER NOT NULL CHECK(capacity >= 1),
    languages_json TEXT NOT NULL,
    exclusive_group TEXT,
    status TEXT NOT NULL CHECK(status IN ('scheduled', 'confirmed', 'concluded', 'cancelled')),
    version INTEGER NOT NULL CHECK(version >= 1),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session_versions (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    version INTEGER NOT NULL,
    fields_json TEXT NOT NULL,
    changed_by TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    PRIMARY KEY(session_id, version)
);
CREATE TABLE IF NOT EXISTS session_resources (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    resource_id TEXT NOT NULL REFERENCES resources(resource_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(session_id, resource_id)
);
CREATE TABLE IF NOT EXISTS session_exclusions (
    session_id_a TEXT NOT NULL REFERENCES sessions(session_id),
    session_id_b TEXT NOT NULL REFERENCES sessions(session_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(session_id_a, session_id_b),
    CHECK(session_id_a < session_id_b)
);
CREATE TABLE IF NOT EXISTS seats (
    seat_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    representative_id TEXT NOT NULL REFERENCES representatives(representative_id),
    delegation_id TEXT NOT NULL REFERENCES delegations(delegation_id),
    state TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    waitlist_rank INTEGER,
    hold_expires_at TEXT,
    linked_seat_id TEXT REFERENCES seats(seat_id),
    replaces_seat_id TEXT REFERENCES seats(seat_id),
    authorization_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(session_id, representative_id)
);
CREATE TABLE IF NOT EXISTS seat_versions (
    seat_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    representative_id TEXT NOT NULL,
    delegation_id TEXT NOT NULL,
    state TEXT NOT NULL,
    waitlist_rank INTEGER,
    hold_expires_at TEXT,
    linked_seat_id TEXT,
    replaces_seat_id TEXT,
    authorization_id TEXT,
    changed_by TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(seat_id, version)
);
CREATE TABLE IF NOT EXISTS authorizations (
    authorization_id TEXT PRIMARY KEY,
    seat_id TEXT NOT NULL REFERENCES seats(seat_id),
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    from_representative_id TEXT NOT NULL REFERENCES representatives(representative_id),
    to_representative_id TEXT NOT NULL REFERENCES representatives(representative_id),
    delegation_id TEXT NOT NULL REFERENCES delegations(delegation_id),
    issued_by TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    revoked_at TEXT,
    revoked_by TEXT
);
CREATE TABLE IF NOT EXISTS materials (
    material_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    title TEXT NOT NULL,
    sensitivity TEXT NOT NULL,
    content_hash TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS material_seats (
    material_id TEXT NOT NULL REFERENCES materials(material_id),
    seat_id TEXT NOT NULL REFERENCES seats(seat_id),
    granted INTEGER NOT NULL CHECK(granted IN (0, 1)),
    source TEXT NOT NULL CHECK(source IN ('seat', 'authorization')),
    commitment_version INTEGER NOT NULL,
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(material_id, seat_id)
);
CREATE TABLE IF NOT EXISTS occurrence_facts (
    fact_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    fact_type TEXT NOT NULL CHECK(fact_type IN ('checked_in', 'material_accessed', 'meeting_concluded')),
    representative_id TEXT REFERENCES representatives(representative_id),
    seat_id TEXT REFERENCES seats(seat_id),
    detail_json TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invitation_decisions (
    request_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    representative_id TEXT NOT NULL REFERENCES representatives(representative_id),
    decision TEXT NOT NULL CHECK(decision IN ('invited', 'waitlisted', 'rejected')),
    conflicts_json TEXT NOT NULL,
    seat_id TEXT,
    created_at TEXT NOT NULL
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)
        # 多线程 HTTP 服务共享单连接：用进程内互斥保证事务不交错，
        # 与 BEGIN IMMEDIATE 共同保证重复确认/同时发布只有一个生效安排。
        self._tx_lock = threading.Lock()

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交；同一进程内事务串行执行。"""

        self._tx_lock.acquire()
        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            self._tx_lock.release()
            raise
        else:
            self.connection.commit()
            self._tx_lock.release()

    @contextmanager
    def reading(self) -> Iterator[sqlite3.Connection]:
        """获取与写事务互斥的只读访问。"""

        self._tx_lock.acquire()
        try:
            yield self.connection
        finally:
            self._tx_lock.release()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
