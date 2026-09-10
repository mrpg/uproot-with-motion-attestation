# SPDX-License-Identifier: 0BSD

from types import TracebackType
from typing import Any, Literal, Self, cast

import pytest
from fastapi.responses import Response
from starlette.requests import Request
from uproot.smithereens import PlayerType, SessionType
from uproot.types import PlayerIdentifier

import motion_attestation
import prisoners_dilemma


class FakePlayer:
    def __init__(self, name: str) -> None:
        object.__setattr__(self, "data", {})
        object.__setattr__(self, "name", name)

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
        self.name = "S1"
        self.players = players


async def test_app_api2_accepts_uproot_dispatch_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    player = cast(PlayerType, FakePlayer("P1"))
    session = cast(SessionType, FakeSession([]))
    request = cast(Request, object())

    async def handle_motion_attestation(
        app_name: str,
        forwarded_session: SessionType,
        forwarded_request: Request,
        forwarded_player: PlayerType | None,
    ) -> Response:
        assert app_name == "prisoners_dilemma"
        assert forwarded_session is session
        assert forwarded_request is request
        assert forwarded_player is player
        return Response(status_code=204)

    monkeypatch.setattr(
        prisoners_dilemma,
        "handle_motion_attestation",
        handle_motion_attestation,
    )

    response = await prisoners_dilemma.api2(
        session=session,
        request=request,
        player=player,
    )

    assert response.status_code == 204


def test_digest_contains_only_attestation_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_session = cast(
        SessionType,
        FakeSession([FakePlayer("P1"), FakePlayer("P2"), FakePlayer("P3")]),
    )

    def assessments(
        session: SessionType,
        *,
        app_name: str | None = None,
    ) -> dict[PlayerIdentifier, motion_attestation.Assessment]:
        assert session is fake_session
        assert app_name == "prisoners_dilemma"
        return {
            PlayerIdentifier(sname="S1", uname="P1"): motion_attestation.Assessment(
                checks=2,
                last_score=0.9,
                min_score=0.8,
            ),
            PlayerIdentifier(sname="S1", uname="P2"): motion_attestation.Assessment(
                checks=3,
                failed_checks=1,
                last_score=0.7,
                min_score=0.3,
            ),
        }

    monkeypatch.setattr(
        prisoners_dilemma,
        "motion_attestation_assessments",
        assessments,
    )

    assert prisoners_dilemma.digest(fake_session) == {
        "checked_participants": 2,
        "flagged_participants": 1,
        "rows": [
            ("P1", 2, 0, 0.9, 0.8, False),
            ("P2", 3, 1, 0.7, 0.3, True),
            ("P3", 0, 0, None, None, False),
        ],
        "total_checks": 5,
        "total_participants": 3,
    }
