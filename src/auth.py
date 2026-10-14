"""Authentication and role-based authorization.

* Passwords are hashed with scrypt (stdlib ``hashlib.scrypt``, per-user random salt); plaintext is never stored or logged.
* The signed-in user is held in a context variable for the duration of each request (one Streamlit script run).
  Service functions call :func:`require` so permissions are enforced in the backend, not only in the UI.
* Scripts, seeding and tests run under an explicit :data:`SYSTEM` principal via :func:`as_principal`.
* Demo accounts are created from environment variables (see ``.env.example``) — no credentials live in source code.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.exceptions import PermissionDenied, ValidationFailure
from src.models import User, utcnow

logger = logging.getLogger(__name__)

# scrypt parameters (memory-hard). n=2**14, r=8, p=1 ≈ 16 MiB; fine for an interactive login.
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _DKLEN = 2**14, 8, 1, 64
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 5
SESSION_IDLE_MINUTES = 30
MIN_PASSWORD_LENGTH = 8
USERNAME_RE = re.compile(r"^[a-z0-9_.-]{3,40}$")


class Role(StrEnum):
    BUSINESS_OWNER = "business_owner"
    USER = "user"
    CUSTOMER = "customer"  # public website visitor: never stored as a user account, never signs in to the workspace


STAFF_ROLES = (Role.BUSINESS_OWNER, Role.USER)


class Permission(StrEnum):
    VIEW_OPERATIONS = "view_operations"  # inquiries, quotes, customers, follow-ups
    EDIT_OPERATIONS = "edit_operations"  # create/edit inquiries, quotes, customers, follow-ups, suggestions
    USE_ASSISTANT = "use_assistant"
    VIEW_FINANCIALS = "view_financials"  # booked / quoted / pipeline values, Reports
    MANAGE_BOOKINGS = "manage_bookings"  # confirm, complete, cancel bookings
    EXPORT_DATA = "export_data"  # CSV exports
    ADMIN_DATA = "admin_data"  # sample data, reset, CSV import
    RUN_EVALUATIONS = "run_evaluations"  # AI Evaluation Lab (incl. paid live runs)
    VIEW_DIAGNOSTICS = "view_diagnostics"  # AI activity log, technical details
    MANAGE_SETTINGS = "manage_settings"  # AI connection test, user list
    SUBMIT_INQUIRY = "submit_inquiry"  # customer portal: submit a flight request / check its status with reference + email


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.BUSINESS_OWNER: frozenset(Permission),
    Role.USER: frozenset({Permission.VIEW_OPERATIONS, Permission.EDIT_OPERATIONS, Permission.USE_ASSISTANT}),
    Role.CUSTOMER: frozenset({Permission.SUBMIT_INQUIRY}),
}
ROLE_LABELS = {Role.BUSINESS_OWNER: "Business owner", Role.USER: "Team member", Role.CUSTOMER: "Customer"}


@dataclass(frozen=True)
class Principal:
    user_id: str | None
    username: str
    role: Role | None  # None = system
    display_name: str

    @property
    def is_system(self) -> bool:
        return self.role is None

    def can(self, permission: Permission) -> bool:
        return self.is_system or (self.role is not None and permission in ROLE_PERMISSIONS[self.role])


SYSTEM = Principal(user_id=None, username="system", role=None, display_name="System")
# The customer portal runs as this principal: it can submit a request and check one status, nothing else.
PORTAL = Principal(user_id=None, username="customer_portal", role=Role.CUSTOMER, display_name="Website visitor")
_current: ContextVar[Principal | None] = ContextVar("flightdesk_principal", default=None)


def set_principal(principal: Principal | None) -> None:
    _current.set(principal)


def current_principal() -> Principal | None:
    return _current.get()


@contextmanager
def as_principal(principal: Principal) -> Iterator[Principal]:
    token = _current.set(principal)
    try:
        yield principal
    finally:
        _current.reset(token)


def has_permission(permission: Permission) -> bool:
    p = _current.get()
    return p is not None and p.can(permission)


def require(permission: Permission) -> Principal:
    """Raise :class:`PermissionDenied` unless the current principal holds ``permission``."""
    p = _current.get()
    if p is None:
        raise PermissionDenied("Please sign in to continue.")
    if not p.can(permission):
        raise PermissionDenied("Your account doesn't have permission for that. Ask the business owner.")
    return p


# --------------------------------------------------------------------------- password hashing
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_DKLEN)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_hex, digest_hex = stored.split("$")
        if algo != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=int(n), r=int(r), p=int(p),
                                dklen=len(digest_hex) // 2)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


# --------------------------------------------------------------------------- users
def _validate(username: str, password: str) -> str:
    username = (username or "").strip().lower()
    if not USERNAME_RE.match(username):
        raise ValidationFailure("Usernames are 3-40 characters: letters, numbers, dot, dash or underscore.")
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise ValidationFailure(f"Passwords must be at least {MIN_PASSWORD_LENGTH} characters.")
    return username


def create_user(session: Session, username: str, password: str, role: Role, display_name: str | None = None) -> User:
    if Role(role) not in STAFF_ROLES:
        raise ValidationFailure("Only business-owner and team-member accounts can be created.")
    username = _validate(username, password)
    if session.scalar(select(User.id).where(func.lower(User.username) == username)):
        raise ValidationFailure(f"User '{username}' already exists.")
    user = User(username=username, password_hash=hash_password(password), role=Role(role).value,
                display_name=(display_name or username)[:80])
    session.add(user)
    session.flush()
    from src.services.audit_service import record_audit

    record_audit(session, "user_created", actor="system", entity_type="user", entity_id=user.id,
                 metadata={"username": username, "role": user.role})
    return user


def principal_for(user: User) -> Principal:
    return Principal(user_id=user.id, username=user.username, role=Role(user.role), display_name=user.display_name)


@dataclass
class LoginResult:
    ok: bool
    message: str
    principal: Principal | None = None


GENERIC_FAILURE = "That username and password don't match."


def authenticate(session: Session, username: str, password: str) -> LoginResult:
    """Verify credentials with lockout after repeated failures. Messages never reveal which part was wrong."""
    from src.services.audit_service import record_audit

    name = (username or "").strip().lower()
    user = session.scalar(select(User).where(func.lower(User.username) == name)) if name else None
    now = utcnow()
    if user is None:
        hash_password(password or "x")  # equalise timing with the success path
        record_audit(session, "login_failed", actor="system", metadata={"reason": "unknown_user"}, outcome="failure")
        return LoginResult(False, GENERIC_FAILURE)
    if not user.is_active:
        record_audit(session, "login_failed", actor="system", entity_type="user", entity_id=user.id,
                     metadata={"reason": "inactive"}, outcome="failure")
        return LoginResult(False, GENERIC_FAILURE)
    if user.locked_until and user.locked_until > now:
        minutes = max(1, int((user.locked_until - now).total_seconds() // 60) + 1)
        return LoginResult(False, f"Too many attempts. Try again in about {minutes} minute(s).")
    if not verify_password(password or "", user.password_hash):
        user.failed_attempts = (user.failed_attempts or 0) + 1
        if user.failed_attempts >= MAX_FAILED_ATTEMPTS:
            user.locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
            user.failed_attempts = 0
        record_audit(session, "login_failed", actor="system", entity_type="user", entity_id=user.id,
                     metadata={"reason": "bad_password"}, outcome="failure")
        return LoginResult(False, GENERIC_FAILURE)
    user.failed_attempts, user.locked_until, user.last_login_at = 0, None, now
    record_audit(session, "login_succeeded", actor="system", entity_type="user", entity_id=user.id,
                 metadata={"role": user.role})
    return LoginResult(True, "Signed in.", principal_for(user))


def load_principal(session: Session, user_id: str) -> Principal | None:
    """Re-read the user on every request so deactivation or role changes apply immediately."""
    user = session.get(User, user_id)
    if user is None or not user.is_active:
        return None
    return principal_for(user)


DEMO_ACCOUNT_ENV = [
    ("FLIGHTDESK_OWNER_USERNAME", "FLIGHTDESK_OWNER_PASSWORD", Role.BUSINESS_OWNER, "Business owner"),
    ("FLIGHTDESK_USER_USERNAME", "FLIGHTDESK_USER_PASSWORD", Role.USER, "Team member"),
]


def bootstrap_demo_users(session: Session) -> list[str]:
    """Create demo accounts from environment variables if they don't exist yet. Never overwrites passwords."""
    created = []
    for user_var, pass_var, role, label in DEMO_ACCOUNT_ENV:
        username, password = os.getenv(user_var, "").strip(), os.getenv(pass_var, "")
        if not username or not password:
            continue
        if session.scalar(select(User.id).where(func.lower(User.username) == username.lower())):
            continue
        try:
            create_user(session, username, password, role, display_name=label)
            created.append(username.lower())
        except ValidationFailure as exc:
            logger.warning("Demo account from %s not created: %s", user_var, exc)
    return created


def list_users(session: Session) -> list[User]:
    require(Permission.MANAGE_SETTINGS)
    return list(session.scalars(select(User).order_by(User.username)))


def change_password(session: Session, user_id: str, current_password: str, new_password: str) -> None:
    """Users may change their own password after re-entering the current one."""
    from src.services.audit_service import record_audit

    p = current_principal()
    if p is None or p.user_id != user_id:
        raise PermissionDenied("You can only change your own password.")
    user = session.get(User, user_id)
    if user is None or not verify_password(current_password, user.password_hash):
        raise ValidationFailure("Your current password is incorrect.")
    _validate(user.username, new_password)
    user.password_hash = hash_password(new_password)
    record_audit(session, "password_changed", actor="advisor", entity_type="user", entity_id=user_id)


def add_staff_account(session: Session, username: str, password: str, role: Role, display_name: str | None = None) -> User:
    """Owner-only: create a business-owner or team-member account from Settings."""
    require(Permission.MANAGE_SETTINGS)
    return create_user(session, username, password, role, display_name)


def set_user_active(session: Session, user_id: str, active: bool) -> None:
    """Owner-only: switch an account on or off. You can't switch off your own account."""
    from src.services.audit_service import record_audit

    p = require(Permission.MANAGE_SETTINGS)
    if p.user_id == user_id and not active:
        raise ValidationFailure("You can't switch off your own account.")
    user = session.get(User, user_id)
    if user is None:
        raise ValidationFailure("That account doesn't exist.")
    user.is_active = active
    record_audit(session, "user_activated" if active else "user_deactivated", actor="advisor", entity_type="user",
                 entity_id=user_id)
