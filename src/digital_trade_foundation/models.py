"""定义基础服务在模块边界使用的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Actor:
    """表示具有明确角色的后台操作者。"""

    actor_id: str
    display_name: str
    role: str
    organization_id: str
    active: bool


@dataclass(frozen=True)
class Site:
    """表示数字贸易合作机构下的业务场所。"""

    site_id: str
    organization_id: str
    name: str
    timezone_name: str
    version: int


@dataclass(frozen=True)
class DomainRecord:
    """表示已经持久化的领域资料记录。"""

    record_id: str
    site_id: str
    category: str
    external_key: str
    payload: dict[str, Any]
    created_by: str
    created_at: str


@dataclass(frozen=True)
class WriteReceipt:
    """描述一次幂等写入的稳定结果。"""

    request_id: str
    resource_type: str
    resource_id: str
    replayed: bool


@dataclass(frozen=True)
class Delegation:
    """代表团（机构）在本届活动中的登记信息与联络人。"""

    delegation_id: str
    organization_id: str
    name: str
    liaison_actor_id: str
    active: bool


@dataclass(frozen=True)
class Representative:
    """以代表团和机构关联为基础登记的参会代表。"""

    representative_id: str
    delegation_id: str
    organization_id: str
    display_name: str
    active: bool


@dataclass(frozen=True)
class Eligibility:
    """代表参与某场会话的资格核验结论。"""

    representative_id: str
    eligible: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class Language:
    """译员的工作语言能力。"""

    language_code: str
    level: str


@dataclass(frozen=True)
class Resource:
    """可被预订的保障资源：译员、保密会议室或合规观察员。"""

    resource_id: str
    kind: str
    label: str
    capacity: int
    site_id: str | None
    languages: tuple[Language, ...]
    active: bool


@dataclass(frozen=True)
class Session:
    """一场会谈场次的全部编排参数。"""

    session_id: str
    title: str
    session_type: str
    sensitivity: str
    starts_at: str
    ends_at: str
    timezone_name: str
    venue_id: str
    capacity: int
    language_codes: tuple[str, ...]
    exclusive_group: str | None
    status: str
    version: int
    commitment_version: int


@dataclass(frozen=True)
class Commitment:
    """席位的一条版本化承诺：邀请、候补、接受、转授权、退出或改期。"""

    seat_id: str
    session_id: str
    representative_id: str
    delegation_id: str
    state: str
    version: int
    waitlist_rank: int | None
    hold_expires_at: str | None
    linked_seat_id: str | None
    replaces_seat_id: str | None
    authorization_id: str | None
    changed_by: str
    reason: str
    created_at: str


@dataclass(frozen=True)
class Seat:
    """席位于当前生效版本的聚合视图。"""

    seat_id: str
    session_id: str
    representative_id: str
    delegation_id: str
    state: str
    version: int
    waitlist_rank: int | None
    hold_expires_at: str | None
    linked_seat_id: str | None
    replaces_seat_id: str | None
    authorization_id: str | None


@dataclass(frozen=True)
class SeatHistoryItem:
    """席位历次变更中的一个版本。"""

    version: int
    state: str
    representative_id: str
    delegation_id: str
    waitlist_rank: int | None
    hold_expires_at: str | None
    linked_seat_id: str | None
    replaces_seat_id: str | None
    authorization_id: str | None
    changed_by: str
    reason: str
    created_at: str


@dataclass(frozen=True)
class Authorization:
    """一次转授权：原始代表把自身有效席位转给替代代表。"""

    authorization_id: str
    seat_id: str
    session_id: str
    from_representative_id: str
    to_representative_id: str
    delegation_id: str
    issued_by: str
    issued_at: str
    revoked_at: str | None
    active: bool


@dataclass(frozen=True)
class MaterialGrant:
    """一份材料对一条席位的访问授权及其当前状态。"""

    material_id: str
    seat_id: str
    sensitivity: str
    granted: bool
    source: str
    commitment_version: int


@dataclass(frozen=True)
class OccurrenceFact:
    """会谈结束后不可覆盖的既成事实。"""

    fact_id: str
    session_id: str
    fact_type: str
    representative_id: str | None
    seat_id: str | None
    detail: dict[str, Any]
    recorded_by: str
    recorded_at: str


@dataclass(frozen=True)
class SessionChange:
    """场次编排参数的一次版本变更。"""

    version: int
    changed_by: str
    changed_at: str
    fields: dict[str, Any]


@dataclass(frozen=True)
class SessionView:
    """联络组从任一场次反查得到的完整编排视图。"""

    session: Session
    participants: list[dict[str, Any]]
    resources: list[dict[str, Any]]
    materials: list[dict[str, Any]]
    facts: list[dict[str, Any]]
    confidentiality_basis: list[dict[str, Any]]
    history: list[dict[str, Any]]


@dataclass(frozen=True)
class MyScheduleItem:
    """代表本人视角下的一条席位安排。"""

    session_id: str
    title: str
    starts_at: str
    ends_at: str
    timezone_name: str
    seat_id: str
    state: str
    waitlist_rank: int | None
    hold_expires_at: str | None
    effective: bool
    replaces_seat_id: str | None
    authorization_id: str | None
    materials: list[dict[str, Any]]


@dataclass(frozen=True)
class ResourceScheduleItem:
    """资源保障方视角下的一条预订。"""

    session_id: str
    title: str
    starts_at: str
    ends_at: str
    timezone_name: str
    sensitivity: str
    language_codes: tuple[str, ...]
    held: bool
    booked: bool
