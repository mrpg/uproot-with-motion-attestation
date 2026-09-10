# SPDX-License-Identifier: 0BSD

import json
from types import TracebackType
from typing import Any, Literal, Self, cast
from uuid import uuid4

import httpx
import pytest
import uproot.models as um
from fastapi import HTTPException
from starlette.requests import Request
from uproot.smithereens import PlayerType, SessionType
from uproot.types import PlayerIdentifier

import motion_attestation


class FakePlayer:
    def __init__(
        self,
        app: str = "test_app",
        name: str = "P1",
    ) -> None:
        object.__setattr__(self, "data", {"app": app, "show_page": 2})
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "__namespace__", ("player", "S1", name))

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


class FakeSession:
    def __init__(self, players: list[FakePlayer]) -> None:
        self.players = players
        self.name = "S1"


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


def test_assessments_are_derived_only_from_ledger_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid1 = PlayerIdentifier(sname="S1", uname="P1")
    pid2 = PlayerIdentifier(sname="S1", uname="P2")

    def entry(
        pid: PlayerIdentifier,
        score: float,
        cleared: bool,
    ) -> motion_attestation.MotionAttestationEntry:
        entry_type = cast(Any, motion_attestation.MotionAttestationEntry)
        return cast(
            motion_attestation.MotionAttestationEntry,
            entry_type(
                pid=pid,
                app_name="test_app",
                page_index=2,
                cleared=cleared,
                score=score,
                flags=[],
                duration_ms=5_000,
                signal_counts={"m": 1},
            ),
        )

    stored_entries = [
        (uuid4(), 1.0, entry(pid1, 0.8, True)),
        (uuid4(), 2.0, entry(pid2, 0.9, True)),
        (uuid4(), 3.0, entry(pid1, 0.3, False)),
        (uuid4(), 4.0, entry(pid1, 0.7, True)),
    ]

    def read_entries(
        session: SessionType,
        *,
        app_name: str | None = None,
        player: PlayerType | None = None,
    ) -> list[motion_attestation.StoredMotionAttestationEntry]:
        assert isinstance(session, FakeSession)
        assert app_name == "test_app"
        if player is None:
            return stored_entries
        return [stored for stored in stored_entries if stored[2].pid == pid1]

    monkeypatch.setattr(motion_attestation, "read_entries", read_entries)

    result = motion_attestation.assessments(
        cast(SessionType, FakeSession([])),
        app_name="test_app",
    )

    assert result == {
        pid1: motion_attestation.Assessment(
            checks=3,
            failed_checks=1,
            last_score=0.7,
            min_score=0.3,
        ),
        pid2: motion_attestation.Assessment(
            checks=1,
            failed_checks=0,
            last_score=0.9,
            min_score=0.9,
        ),
    }
    assert result[pid1].flagged is True
    assert result[pid2].flagged is False

    one = motion_attestation.assessment(
        cast(SessionType, FakeSession([])),
        cast(PlayerType, FakePlayer(name="P1")),
        app_name="test_app",
    )
    assert one == result[pid1]


async def test_init_returns_stateless_player_bound_challenge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    player = FakePlayer()
    monkeypatch.setenv("MOTION_ATTESTATION_URL", "http://127.0.0.1:35001")
    monkeypatch.setenv("MOTION_ATTESTATION_PROXY_KEY", "test-proxy-key")

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
        "test_app",
        cast(SessionType, FakeSession([player])),
        make_request("init"),
        cast(PlayerType, player),
    )

    assert response.status_code == 200
    result = json.loads(bytes(response.body))
    assert result["challengeId"] != "challenge"
    assert result["ttl"] == 60_000
    assert motion_attestation.unwrap_challenge(
        cast(PlayerType, player),
        "test_app",
        {"cid": result["challengeId"]},
    ) == {"cid": "challenge"}
    assert not any(key.startswith("motion_attestation") for key in player.data)


async def test_verify_records_summary_and_exposes_only_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    player = FakePlayer()
    monkeypatch.setenv("MOTION_ATTESTATION_URL", "http://127.0.0.1:35001")
    monkeypatch.setenv("MOTION_ATTESTATION_PROXY_KEY", "test-proxy-key")
    envelope = motion_attestation.challenge_envelope(
        "test_app",
        cast(PlayerType, player),
        "challenge",
    )
    payload = interaction_payload(envelope)
    body = json.dumps(payload).encode()

    async def sidecar_request(
        path: str,
        forwarded_body: bytes | None = None,
    ) -> tuple[int, dict[str, Any]]:
        assert path == "/interactions/verify"
        assert forwarded_body is not None
        forwarded_payload = json.loads(forwarded_body)
        assert forwarded_payload == payload | {"cid": "challenge"}
        return 200, {
            "analysis": {"internal": True},
            "cleared": True,
            "flags": [],
            "score": 0.875,
            "token": "internal-only",
        }

    monkeypatch.setattr(motion_attestation, "sidecar_request", sidecar_request)

    ledger_entry: dict[str, Any] = {}

    def ensure_log(session: SessionType) -> Any:
        assert session is fake_session
        return "motion-log"

    def add_entry(
        log: Any,
        forwarded_player: Any,
        entry_type: Any,
        **fields: Any,
    ) -> None:
        assert log == "motion-log"
        assert forwarded_player is player
        assert entry_type is motion_attestation.MotionAttestationEntry
        ledger_entry.update(fields)

    fake_session = cast(SessionType, FakeSession([player]))
    monkeypatch.setattr(motion_attestation, "ensure_log", ensure_log)
    monkeypatch.setattr(um, "add_entry", add_entry)

    response = await motion_attestation.handle_request(
        "test_app",
        fake_session,
        make_request("verify", body),
        cast(PlayerType, player),
    )

    assert response.status_code == 200
    assert json.loads(bytes(response.body)) == {
        "cleared": True,
        "flags": [],
        "score": 0.875,
    }

    assert ledger_entry["page_index"] == 2
    assert ledger_entry["score"] == 0.875
    assert ledger_entry["cleared"] is True
    assert ledger_entry["signal_counts"] == {
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
    assert "d" not in ledger_entry
    assert "cid" not in ledger_entry
    assert (
        not {
            "motion_attestation_checks",
            "motion_attestation_failed_checks",
            "motion_attestation_flagged",
            "motion_attestation_last_score",
            "motion_attestation_min_score",
            "motion_attestation_records",
        }
        & player.data.keys()
    )


async def test_verify_rejects_challenge_from_another_player(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = FakePlayer(name="P1")
    other = FakePlayer(name="P2")
    monkeypatch.setenv("MOTION_ATTESTATION_URL", "http://127.0.0.1:35001")
    monkeypatch.setenv("MOTION_ATTESTATION_PROXY_KEY", "test-proxy-key")
    envelope = motion_attestation.challenge_envelope(
        "test_app",
        cast(PlayerType, owner),
        "challenge",
    )
    body = json.dumps(interaction_payload(envelope)).encode()

    with pytest.raises(HTTPException) as excinfo:
        await motion_attestation.handle_request(
            "test_app",
            cast(SessionType, FakeSession([owner, other])),
            make_request("verify", body),
            cast(PlayerType, other),
        )

    assert excinfo.value.status_code == 400
    assert not any(key.startswith("motion_attestation") for key in other.data)


async def test_request_requires_authenticated_player() -> None:
    with pytest.raises(HTTPException) as excinfo:
        await motion_attestation.handle_request(
            "test_app",
            cast(SessionType, FakeSession([])),
            make_request("init"),
        )

    assert excinfo.value.status_code == 403


async def test_request_rejects_player_in_another_app() -> None:
    with pytest.raises(HTTPException) as excinfo:
        await motion_attestation.handle_request(
            "test_app",
            cast(SessionType, FakeSession([])),
            make_request("init"),
            cast(PlayerType, FakePlayer("another_app")),
        )

    assert excinfo.value.status_code == 403


@pytest.mark.parametrize("content_length", ["invalid", "-1"])
async def test_verify_rejects_invalid_content_length(content_length: str) -> None:
    player = FakePlayer()

    with pytest.raises(HTTPException) as excinfo:
        await motion_attestation.handle_request(
            "test_app",
            cast(SessionType, FakeSession([player])),
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
        "test_app",
        cast(SessionType, FakeSession([])),
        make_request("init"),
        cast(PlayerType, FakePlayer()),
    )

    assert response.status_code == 503
    assert json.loads(bytes(response.body)) == {
        "cleared": False,
        "error": "Attestation service unavailable",
    }
