# SPDX-License-Identifier: 0BSD
"""Authenticated Uproot bridge to the local motion-attestation sidecar."""

import json
import logging
import os
import time
from typing import Any

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response
from uproot.smithereens import PlayerType

ACTION_HEADER = "X-Motion-Attestation-Action"
BODY_LIMIT_BYTES = 512 * 1024
RECORD_LIMIT = 64
REQUEST_TIMEOUT_SECONDS = 3.0
SIGNAL_KEYS = ("m", "c", "k", "s", "tc", "ac", "gy", "or", "ev", "bc")
LOGGER = logging.getLogger(__name__)


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


def bind_challenge(player: PlayerType, result: dict[str, Any]) -> None:
    challenge_id = result.get("challengeId")
    ttl = result.get("ttl")
    if not isinstance(challenge_id, str) or not challenge_id:
        raise RuntimeError("motion-attestation returned an invalid challenge")
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or ttl <= 0:
        raise RuntimeError("motion-attestation returned an invalid challenge TTL")

    with player:
        player.motion_attestation_challenge_id = challenge_id


def consume_challenge(player: PlayerType, payload: dict[str, Any]) -> None:
    challenge_id = payload.get("cid")
    if not isinstance(challenge_id, str) or not challenge_id:
        raise HTTPException(status_code=400, detail="Invalid attestation challenge")

    with player:
        expected_id = player.get("motion_attestation_challenge_id")
        if challenge_id != expected_id:
            raise HTTPException(status_code=400, detail="Invalid attestation challenge")

        player.motion_attestation_challenge_id = None


def record_result(
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

    interaction_data = request_data.get("d", {})
    if not isinstance(interaction_data, dict):
        interaction_data = {}

    duration_ms = interaction_data.get("dur")
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, (int, float)):
        duration_ms = None

    flags = result.get("flags", [])
    if not isinstance(flags, list):
        flags = []

    with player:
        if player.get("app") != app_name:
            return

        record = {
            "cleared": cleared,
            "duration_ms": duration_ms,
            "flags": [str(flag)[:500] for flag in flags[:32]],
            "page_index": player.get("show_page"),
            "score": float(score),
            "signal_counts": {
                key: len(interaction_data.get(key, []))
                for key in SIGNAL_KEYS
                if isinstance(interaction_data.get(key), list)
            },
            "verified_at_ms": int(time.time() * 1000),
        }

        stored_records = player.get("motion_attestation_records", [])
        records = list(stored_records) if isinstance(stored_records, list) else []
        records.append(record)
        player.motion_attestation_records = records[-RECORD_LIMIT:]

        checks = int(player.get("motion_attestation_checks", 0)) + 1
        failed_checks = int(player.get("motion_attestation_failed_checks", 0))
        if not cleared:
            failed_checks += 1

        previous_minimum = player.get("motion_attestation_min_score")
        minimum = (
            float(score)
            if previous_minimum is None
            else min(float(previous_minimum), float(score))
        )

        player.motion_attestation_checks = checks
        player.motion_attestation_failed_checks = failed_checks
        player.motion_attestation_flagged = failed_checks > 0
        player.motion_attestation_last_score = float(score)
        player.motion_attestation_min_score = minimum


def browser_result(action: str, result: dict[str, Any]) -> dict[str, Any]:
    fields = (
        ("challengeId", "ttl", "error")
        if action == "init"
        else ("cleared", "score", "flags", "error")
    )
    return {key: result[key] for key in fields if key in result}


async def handle_request(
    app_name: str,
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
                bind_challenge(player, result)
        else:
            body, payload = await request_payload(request)
            consume_challenge(player, payload)
            status_code, result = await sidecar_request(
                "/interactions/verify",
                body,
            )
            record_result(player, app_name, payload, result)
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
