# SPDX-License-Identifier: 0BSD
"""Authenticated Uproot bridge to the local motion-attestation sidecar."""

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

import httpx
import uproot.models as um
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response
from uproot.smithereens import PlayerType, SessionType
from uproot.types import ModelIdentifier, PlayerIdentifier, identify

ACTION_HEADER = "X-Motion-Attestation-Action"
BODY_LIMIT_BYTES = 512 * 1024
LOG_FIELD = "motion_attestation_log"
REQUEST_TIMEOUT_SECONDS = 3.0
SIGNAL_KEYS = ("m", "c", "k", "s", "tc", "ac", "gy", "or", "ev", "bc")
LOGGER = logging.getLogger(__name__)


class MotionAttestationEntry(metaclass=um.Entry):
    """One immutable, page-level analyzer verdict."""

    pid: PlayerIdentifier
    app_name: str
    page_index: int | None
    cleared: bool
    score: float
    flags: list[str]
    duration_ms: float | None
    signal_counts: dict[str, int]


StoredMotionAttestationEntry = tuple[UUID, float, MotionAttestationEntry]


@dataclass(frozen=True)
class Assessment:
    """A dynamic projection of a participant's ledger entries."""

    checks: int = 0
    failed_checks: int = 0
    last_score: float | None = None
    min_score: float | None = None

    @property
    def flagged(self) -> bool:
        return self.failed_checks > 0


def is_request(request: Request) -> bool:
    """Return whether an app API request belongs to this integration."""
    return request.headers.get(ACTION_HEADER) is not None


def sidecar_settings() -> tuple[str, str]:
    try:
        url = os.environ["MOTION_ATTESTATION_URL"]
        proxy_key = os.environ["MOTION_ATTESTATION_PROXY_KEY"]
    except KeyError as error:
        raise RuntimeError(f"{error.args[0]} is not configured") from error

    return url.rstrip("/"), proxy_key


async def sidecar_request(
    path: str,
    body: bytes | None = None,
) -> tuple[int, dict[str, Any]]:
    url, proxy_key = sidecar_settings()
    headers = {
        "Accept": "application/json",
        "X-Motion-Proxy-Key": proxy_key,
    }
    if body is not None:
        headers["Content-Type"] = "application/json"

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT_SECONDS,
        trust_env=False,
    ) as client:
        response = await client.post(f"{url}{path}", content=body, headers=headers)

    try:
        payload = response.json()
    except ValueError as error:
        raise RuntimeError("motion-attestation returned invalid JSON") from error

    if not isinstance(payload, dict):
        raise TypeError("motion-attestation returned an invalid response")

    return response.status_code, payload


def require_player(player: PlayerType | None, app_name: str) -> PlayerType:
    if player is None:
        raise HTTPException(status_code=403, detail="Player authentication required")

    with player:
        if player.get("app") != app_name:
            raise HTTPException(status_code=403, detail="Invalid player app")

    return player


async def request_payload(request: Request) -> tuple[bytes, dict[str, Any]]:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared_length = int(content_length)
        except ValueError as error:
            raise HTTPException(
                status_code=400,
                detail="Invalid Content-Length",
            ) from error

        if declared_length < 0:
            raise HTTPException(status_code=400, detail="Invalid Content-Length")
        if declared_length > BODY_LIMIT_BYTES:
            raise HTTPException(
                status_code=413,
                detail="Attestation payload is too large",
            )

    body = await request.body()
    if len(body) > BODY_LIMIT_BYTES:
        raise HTTPException(
            status_code=413,
            detail="Attestation payload is too large",
        )

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise HTTPException(status_code=400, detail="Invalid JSON") from error

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid attestation payload")

    return body, payload


def player_identifier(player: PlayerType) -> PlayerIdentifier:
    pid = identify(player)
    if not isinstance(pid, PlayerIdentifier):
        raise TypeError("player does not have a player identifier")
    return pid


def challenge_message(
    app_name: str,
    pid: PlayerIdentifier,
    challenge_id: str,
) -> bytes:
    return json.dumps(
        [app_name, pid.sname, pid.uname, challenge_id],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()


def challenge_envelope(
    app_name: str,
    player: PlayerType,
    challenge_id: str,
) -> str:
    """Bind a sidecar challenge to a player without storing player state."""
    _, proxy_key = sidecar_settings()
    pid = player_identifier(player)
    challenge = base64.urlsafe_b64encode(challenge_id.encode()).rstrip(b"=")
    signature = hmac.digest(
        proxy_key.encode(),
        challenge_message(app_name, pid, challenge_id),
        hashlib.sha256,
    )
    encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=")
    return f"{challenge.decode()}.{encoded_signature.decode()}"


def bind_challenge(
    player: PlayerType,
    app_name: str,
    result: dict[str, Any],
) -> None:
    challenge_id = result.get("challengeId")
    ttl = result.get("ttl")
    if not isinstance(challenge_id, str) or not challenge_id:
        raise RuntimeError("motion-attestation returned an invalid challenge")
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or ttl <= 0:
        raise RuntimeError("motion-attestation returned an invalid challenge TTL")

    result["challengeId"] = challenge_envelope(app_name, player, challenge_id)


def unwrap_challenge(
    player: PlayerType,
    app_name: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    envelope = payload.get("cid")
    if not isinstance(envelope, str) or not envelope:
        raise HTTPException(status_code=400, detail="Invalid attestation challenge")

    try:
        encoded_challenge, encoded_signature = envelope.split(".")
        challenge_id = base64.b64decode(
            encoded_challenge + "=" * (-len(encoded_challenge) % 4),
            altchars=b"-_",
            validate=True,
        ).decode()
        signature = base64.b64decode(
            encoded_signature + "=" * (-len(encoded_signature) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail="Invalid attestation challenge",
        ) from None

    _, proxy_key = sidecar_settings()
    expected_signature = hmac.digest(
        proxy_key.encode(),
        challenge_message(app_name, player_identifier(player), challenge_id),
        hashlib.sha256,
    )
    if not challenge_id or not hmac.compare_digest(signature, expected_signature):
        raise HTTPException(status_code=400, detail="Invalid attestation challenge")

    return payload | {"cid": challenge_id}


def get_log(session: SessionType) -> ModelIdentifier | None:
    """Return this session's attestation ledger, if it has been created."""
    log = session.get(LOG_FIELD)
    if log is None:
        return None
    if not isinstance(log, ModelIdentifier):
        raise TypeError(f"{LOG_FIELD} is not a model identifier")
    return log


def ensure_log(session: SessionType) -> ModelIdentifier:
    """Create the session ledger lazily, on its first valid verdict."""
    log = get_log(session)
    if log is not None:
        return log

    log = um.create_model(session, tag="motion-attestation")
    with session:
        session.motion_attestation_log = log
    return log


def read_entries(
    session: SessionType,
    *,
    app_name: str | None = None,
    player: PlayerType | None = None,
) -> list[StoredMotionAttestationEntry]:
    """Read ledger entries, optionally filtered by app and participant."""
    log = get_log(session)
    if log is None:
        return []

    filters: dict[str, Any] = {}
    if app_name is not None:
        filters["app_name"] = app_name
    if player is not None:
        filters["pid"] = player_identifier(player)

    return um.filter_entries(log, MotionAttestationEntry, **filters)


def update_assessment(
    current: Assessment,
    entry: MotionAttestationEntry,
) -> Assessment:
    """Return the projection produced by appending one entry."""
    return Assessment(
        checks=current.checks + 1,
        failed_checks=current.failed_checks + (not entry.cleared),
        last_score=entry.score,
        min_score=(
            entry.score
            if current.min_score is None
            else min(current.min_score, entry.score)
        ),
    )


def assessment(
    session: SessionType,
    player: PlayerType,
    *,
    app_name: str | None = None,
) -> Assessment:
    """Dynamically assess one participant from the append-only ledger."""
    result = Assessment()
    for _, _, entry in read_entries(session, app_name=app_name, player=player):
        result = update_assessment(result, entry)
    return result


def assessments(
    session: SessionType,
    *,
    app_name: str | None = None,
) -> dict[PlayerIdentifier, Assessment]:
    """Dynamically assess all recorded participants in one ledger scan."""
    result: dict[PlayerIdentifier, Assessment] = {}
    for _, _, entry in read_entries(session, app_name=app_name):
        result[entry.pid] = update_assessment(
            result.get(entry.pid, Assessment()),
            entry,
        )
    return result


def record_result(
    session: SessionType,
    player: PlayerType,
    app_name: str,
    request_data: dict[str, Any],
    result: dict[str, Any],
) -> None:
    score = result.get("score")
    cleared = result.get("cleared")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return
    if not isinstance(cleared, bool):
        return

    numeric_score = float(score)

    interaction_data = request_data.get("d", {})
    if not isinstance(interaction_data, dict):
        interaction_data = {}

    duration_ms = interaction_data.get("dur")
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, (int, float)):
        duration_ms = None

    flags = result.get("flags", [])
    if not isinstance(flags, list):
        flags = []

    safe_flags = [str(flag)[:500] for flag in flags[:32]]

    with player:
        if player.get("app") != app_name:
            return

        page_index = player.get("show_page")

    um.add_entry(
        ensure_log(session),
        cast(PlayerIdentifier, player),
        MotionAttestationEntry,
        app_name=app_name,
        page_index=page_index if isinstance(page_index, int) else None,
        cleared=cleared,
        score=numeric_score,
        flags=safe_flags,
        duration_ms=float(duration_ms) if duration_ms is not None else None,
        signal_counts={
            key: len(interaction_data.get(key, []))
            for key in SIGNAL_KEYS
            if isinstance(interaction_data.get(key), list)
        },
    )


def browser_result(action: str, result: dict[str, Any]) -> dict[str, Any]:
    fields = (
        ("challengeId", "ttl", "error")
        if action == "init"
        else ("cleared", "score", "flags", "error")
    )
    return {key: result[key] for key in fields if key in result}


async def handle_request(
    app_name: str,
    session: SessionType,
    request: Request,
    player: PlayerType | None = None,
) -> Response:
    player = require_player(player, app_name)
    if request.method != "POST":
        raise HTTPException(status_code=405, detail="Method not allowed")

    action = request.headers.get(ACTION_HEADER)
    if action not in {"init", "verify"}:
        raise HTTPException(status_code=400, detail="Invalid attestation action")

    try:
        if action == "init":
            status_code, result = await sidecar_request("/interactions/init")
            if status_code < 400:
                bind_challenge(player, app_name, result)
        else:
            _, payload = await request_payload(request)
            sidecar_payload = unwrap_challenge(player, app_name, payload)
            status_code, result = await sidecar_request(
                "/interactions/verify",
                json.dumps(sidecar_payload, separators=(",", ":")).encode(),
            )
            record_result(session, player, app_name, payload, result)
    except HTTPException:
        raise
    except (httpx.HTTPError, RuntimeError, TypeError, ValueError):
        LOGGER.exception("motion-attestation sidecar request failed")
        return JSONResponse(
            {"cleared": False, "error": "Attestation service unavailable"},
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )

    return JSONResponse(
        browser_result(action, result),
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


async def api2(
    app_name: str,
    session: SessionType,
    request: Request,
    player: PlayerType | None = None,
) -> Response:
    """Alias with the conventional Uproot endpoint name."""
    return await handle_request(app_name, session, request, player)
