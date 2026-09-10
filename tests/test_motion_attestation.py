# SPDX-License-Identifier: 0BSD

import json
from types import TracebackType
from typing import Any, Literal, Self, cast

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request
from uproot.smithereens import PlayerType

import motion_attestation


class FakePlayer:
    def __init__(self, app: str = "prisoners_dilemma") -> None:
        object.__setattr__(self, "data", {"app": app, "show_page": 2})

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        return False

    def __getattr__(self, name: str) -> Any:
        try:
            return self.data[name]
        except KeyError as error:
            raise AttributeError(name) from error

    def __setattr__(self, name: str, value: Any) -> None:
        self.data[name] = value

    def get(self, name: str, default: Any = None) -> Any:
        return self.data.get(name, default)


def make_request(
    action: str,
    body: bytes = b"",
    *,
    content_length: str | None = None,
) -> Request:
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}

        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "headers": [
                (
                    b"content-length",
                    (
                        content_length if content_length is not None else str(len(body))
                    ).encode(),
                ),
                (b"x-motion-attestation-action", action.encode()),
            ],
        },
        receive,
    )


def interaction_payload(challenge_id: str = "challenge") -> dict[str, Any]:
    return {
        "cid": challenge_id,
        "d": {
            "m": [[1, 2, 3]],
            "c": [],
            "k": [],
            "s": [],
            "tc": [],
            "ac": [],
            "gy": [],
            "or": [],
            "ev": [[0, 3]],
            "bc": [],
            "dur": 5_000,
        },
        "ts": 1_000,
    }


async def test_init_binds_sidecar_challenge_to_player(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    player = FakePlayer()

    async def sidecar_request(
        path: str,
        body: bytes | None = None,
    ) -> tuple[int, dict[str, Any]]:
        assert path == "/interactions/init"
        assert body is None
        return 200, {
            "challengeId": "challenge",
            "scriptUrl": "/not-exposed.js",
            "ttl": 60_000,
        }

    monkeypatch.setattr(motion_attestation, "sidecar_request", sidecar_request)

    response = await motion_attestation.handle_request(
        "prisoners_dilemma",
        make_request("init"),
        cast(PlayerType, player),
    )

    assert response.status_code == 200
    assert json.loads(bytes(response.body)) == {
        "challengeId": "challenge",
        "ttl": 60_000,
    }
    assert player.motion_attestation_challenge_id == "challenge"


async def test_verify_records_summary_and_exposes_only_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    player = FakePlayer()
    player.motion_attestation_challenge_id = "challenge"
    payload = interaction_payload()
    body = json.dumps(payload).encode()

    async def sidecar_request(
        path: str,
        forwarded_body: bytes | None = None,
    ) -> tuple[int, dict[str, Any]]:
        assert path == "/interactions/verify"
        assert forwarded_body == body
        return 200, {
            "analysis": {"internal": True},
            "cleared": True,
            "flags": [],
            "score": 0.875,
            "token": "internal-only",
        }

    monkeypatch.setattr(motion_attestation, "sidecar_request", sidecar_request)

    response = await motion_attestation.handle_request(
        "prisoners_dilemma",
        make_request("verify", body),
        cast(PlayerType, player),
    )

    assert response.status_code == 200
    assert json.loads(bytes(response.body)) == {
        "cleared": True,
        "flags": [],
        "score": 0.875,
    }
    assert player.motion_attestation_challenge_id is None
    assert player.motion_attestation_checks == 1
    assert player.motion_attestation_failed_checks == 0

    record = player.motion_attestation_records[0]
    assert record["page_index"] == 2
    assert record["signal_counts"] == {
        "m": 1,
        "c": 0,
        "k": 0,
        "s": 0,
        "tc": 0,
        "ac": 0,
        "gy": 0,
        "or": 0,
        "ev": 1,
        "bc": 0,
    }
    assert "d" not in record
    assert "cid" not in record


async def test_verify_rejects_challenge_from_another_player() -> None:
    player = FakePlayer()
    player.motion_attestation_challenge_id = "expected"
    body = json.dumps(interaction_payload("different")).encode()

    with pytest.raises(HTTPException) as excinfo:
        await motion_attestation.handle_request(
            "prisoners_dilemma",
            make_request("verify", body),
            cast(PlayerType, player),
        )

    assert excinfo.value.status_code == 400
    assert player.motion_attestation_challenge_id == "expected"


async def test_request_requires_authenticated_player() -> None:
    with pytest.raises(HTTPException) as excinfo:
        await motion_attestation.handle_request(
            "prisoners_dilemma",
            make_request("init"),
        )

    assert excinfo.value.status_code == 403


async def test_request_rejects_player_in_another_app() -> None:
    with pytest.raises(HTTPException) as excinfo:
        await motion_attestation.handle_request(
            "prisoners_dilemma",
            make_request("init"),
            cast(PlayerType, FakePlayer("another_app")),
        )

    assert excinfo.value.status_code == 403


@pytest.mark.parametrize("content_length", ["invalid", "-1"])
async def test_verify_rejects_invalid_content_length(content_length: str) -> None:
    player = FakePlayer()
    player.motion_attestation_challenge_id = "challenge"

    with pytest.raises(HTTPException) as excinfo:
        await motion_attestation.handle_request(
            "prisoners_dilemma",
            make_request("verify", b"{}", content_length=content_length),
            cast(PlayerType, player),
        )

    assert excinfo.value.status_code == 400


async def test_sidecar_failure_is_a_bounded_fail_open_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def sidecar_request(
        path: str,
        body: bytes | None = None,
    ) -> tuple[int, dict[str, Any]]:
        raise httpx.ConnectError("sidecar unavailable")

    monkeypatch.setattr(motion_attestation, "sidecar_request", sidecar_request)

    response = await motion_attestation.handle_request(
        "prisoners_dilemma",
        make_request("init"),
        cast(PlayerType, FakePlayer()),
    )

    assert response.status_code == 503
    assert json.loads(bytes(response.body)) == {
        "cleared": False,
        "error": "Attestation service unavailable",
    }
