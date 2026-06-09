import os
from dataclasses import dataclass
from typing import Mapping


SERVICE_TOKEN_ENV = "MIROTHINKER_SERVICE_TOKEN"
SERVICE_TOKEN_HEADER = "X-MiroThinker-Service-Token"
USER_ID_HEADER = "X-Limira-User-Id"
USER_ROLE_HEADER = "X-Limira-User-Role"


class AuthError(Exception):
    def __init__(self, code: str, status: int = 401):
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class AuthContext:
    user_id: str
    is_admin: bool = False


def configured_service_token(service_token: str | None = None) -> str | None:
    return service_token if service_token is not None else os.getenv(SERVICE_TOKEN_ENV)


def authenticate_headers(
    headers: Mapping[str, str],
    service_token: str | None = None,
) -> AuthContext:
    expected_token = configured_service_token(service_token)
    if not expected_token:
        raise AuthError("service_token_not_configured", status=500)

    received_token = headers.get(SERVICE_TOKEN_HEADER)
    if received_token != expected_token:
        raise AuthError("invalid_service_token", status=401)

    user_id = (headers.get(USER_ID_HEADER) or "").strip()
    if not user_id:
        raise AuthError("missing_user_id", status=401)

    role = (headers.get(USER_ROLE_HEADER) or "").strip().lower()
    return AuthContext(user_id=user_id, is_admin=role == "admin")


def reject_body_user_id(payload: object) -> None:
    if isinstance(payload, dict) and "user_id" in payload:
        raise AuthError("user_id_must_come_from_trusted_header", status=400)
