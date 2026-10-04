"""会谈编排服务的 HTTP/JSON 路由边界。

所有写入沿用 request_id 幂等语义与 X-Actor-Id 履职身份；
查询接口在领域层按角色裁剪，调用方只能看到履职所需内容。
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .scheduling import SchedulingService


def _serialize(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, (list, tuple)):
        return [_serialize(item) for item in value]
    return value


def route_scheduling(service: SchedulingService, method: str, path: str, body: dict[str, Any] | None,
                     headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]] | None:
    """返回 (status, payload)；路径不属于编排层时返回 None。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    segments = [segment for segment in parsed.path.split("/") if segment]
    query = parse_qs(parsed.query)
    actor_id = headers.get("X-Actor-Id", "")

    def actor(value: dict[str, Any] | None = None) -> dict[str, Any]:
        data = dict(value or body)
        data["actor_id"] = actor_id
        return data

    try:
        if method == "POST" and parsed.path == "/delegations":
            result = service.register_delegation(**actor())
            return 200 if result.get("replayed") else 201, result
        if method == "POST" and parsed.path == "/representatives":
            return 201, service.register_representative(**actor())
        if method == "POST" and parsed.path == "/representatives/clearance":
            return 200, service.update_clearance(**actor())
        if method == "POST" and parsed.path == "/resources":
            return 201, service.register_resource(**actor())
        if method == "POST" and parsed.path == "/resource-blocks":
            return 201, service.add_resource_block(**actor())
        if method == "POST" and parsed.path == "/recusals":
            return 201, service.add_recusal(**actor())
        if method == "POST" and parsed.path == "/sessions":
            return 201, service.create_session(**actor())
        if method == "POST" and parsed.path == "/session-exclusions":
            return 201, service.add_exclusion(**actor())
        if method == "POST" and parsed.path == "/invitations":
            result = service.invite(**actor())
            return 200 if result.get("replayed") else 201, result
        if method == "POST" and parsed.path == "/invitations/respond":
            return 200, service.respond_invitation(**actor())
        if method == "POST" and parsed.path == "/seats/withdraw":
            return 200, service.withdraw(**actor())
        if method == "POST" and parsed.path == "/seats/delegate":
            return 200, service.delegate(**actor())
        if method == "POST" and parsed.path == "/authorizations/revoke":
            return 200, service.revoke_delegation(**actor())
        if method == "POST" and parsed.path == "/maintenance/run-due":
            return 200, service.run_due(**actor())
        if method == "POST" and parsed.path == "/sessions/reschedule":
            return 200, service.reschedule(**actor())
        if method == "POST" and parsed.path == "/sessions/conclude":
            return 200, service.conclude_session(**actor())
        if method == "POST" and parsed.path == "/check-ins":
            return 201, service.check_in(**actor())
        if method == "POST" and parsed.path == "/materials":
            return 201, service.upload_material(**actor())
        if method == "POST" and parsed.path == "/materials/access":
            return 200, service.access_material(**actor())

        if method == "GET" and len(segments) == 2 and segments[0] == "decisions":
            return 200, service.get_decision(segments[1], actor_id=actor_id)
        if method == "GET" and len(segments) == 3 and segments[0] == "sessions" and segments[2] == "view":
            return 200, _serialize(service.session_view(segments[1], actor_id=actor_id))
        if method == "GET" and len(segments) == 3 and segments[0] == "sessions" \
                and segments[2] == "authorizations":
            return 200, {"items": _serialize(service.list_authorizations(segments[1], actor_id=actor_id))}
        if method == "GET" and len(segments) == 3 and segments[0] == "representatives" \
                and segments[2] == "schedule":
            return 200, {"items": _serialize(service.my_schedule(segments[1], actor_id=actor_id))}
        if method == "GET" and len(segments) == 3 and segments[0] == "resources" \
                and segments[2] == "schedule":
            return 200, {"items": _serialize(service.resource_schedule(segments[1], actor_id=actor_id))}
        if method == "GET" and len(segments) == 3 and segments[0] == "seats" and segments[2] == "history":
            return 200, {"items": _serialize(service.seat_history(segments[1], actor_id=actor_id))}
        if method == "GET" and parsed.path == "/conflicts/preview":
            session_id = query.get("session_id", [""])[0]
            representative_id = query.get("representative_id", [""])[0]
            if not session_id or not representative_id:
                raise ValidationError("session_id 与 representative_id 不能为空")
            return 200, {"items": service.preview_conflicts(
                actor_id=actor_id, session_id=session_id, representative_id=representative_id)}
        return None
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}
