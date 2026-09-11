# Docs are available at https://uproot.science/
# Examples are available at https://github.com/mrpg/uproot-examples
#
# This example app is under the 0BSD license. You can use it freely and build on it
# without any limitations and without any attribution. However, these two lines must be
# preserved in any uproot app (the license file is automatically installed in projects):
#
# Third-party dependencies:
# - uproot: LGPL v3+, see ../uproot_license.txt

from fastapi import Request
from fastapi.responses import Response
from uproot.fields import *
from uproot.smithereens import *
from uproot.types import PlayerIdentifier

from motion_attestation import Assessment
from motion_attestation import assessments as motion_attestation_assessments
from motion_attestation import handle_request as handle_motion_attestation

DESCRIPTION = "Prisoner's dilemma"
SUGGESTED_MULTIPLE = 2


class C:
    PAYOFF_MATRIX = {
        (True, True): 10,
        (True, False): 0,
        (False, True): 15,
        (False, False): 3,
    }


class Instructions(Page):
    pass


class GroupPlease(GroupCreatingWait):
    group_size = 2


class Dilemma(Page):
    fields = dict(
        cooperate=RadioField(
            label="Do you wish to cooperate?",
            choices=[(True, "Yes"), (False, "No")],
        ),
    )


class Sync(SynchronizingWait):
    @classmethod
    def all_here(page, group: GroupType) -> None:
        for player in group.players:
            other = player.other_in_group
            player.payoff = C.PAYOFF_MATRIX[player.cooperate, other.cooperate]


class Results(Page):
    pass


async def api2(
    session: SessionType,
    request: Request,
    player: PlayerType | None = None,
) -> Response:
    """Give the project-wide browser adapter an authenticated app endpoint."""
    return await handle_motion_attestation(__name__, session, request, player)


def digest(session: SessionType) -> dict[str, Any]:
    """Return only participant identifiers and motion-attestation summaries."""
    rows = []
    checked_participants = 0
    flagged_participants = 0
    total_checks = 0
    player_assessments = motion_attestation_assessments(
        session,
        app_name=__name__,
    )

    for player in session.players:
        pid = PlayerIdentifier(sname=session.name, uname=player.name)
        attestation = player_assessments.get(pid, Assessment())

        if attestation.checks:
            checked_participants += 1
        if attestation.flagged:
            flagged_participants += 1

        total_checks += attestation.checks
        rows.append(
            (
                player.name,
                attestation.checks,
                attestation.failed_checks,
                attestation.last_score,
                attestation.mean_score,
                attestation.flagged,
            )
        )

    return {
        "checked_participants": checked_participants,
        "flagged_participants": flagged_participants,
        "rows": rows,
        "total_checks": total_checks,
        "total_participants": len(rows),
    }


def pipeline(session: SessionType) -> list[dict[str, Any]]:
    rows = []
    player_assessments = motion_attestation_assessments(
        session,
        app_name=__name__,
    )

    for group in session.groups(app=__name__):
        players = group.players
        player1, player2 = players

        for member_id, player in enumerate(players):
            other = player2 if member_id == 0 else player1
            player_data = player.within(app=__name__)
            other_data = other.within(app=__name__)
            cooperate = player_data.get("cooperate")
            other_cooperate = other_data.get("cooperate")
            pid = PlayerIdentifier(sname=session.name, uname=player.name)
            attestation = player_assessments.get(pid, Assessment())

            rows.append(
                {
                    "session": session.name,
                    "group": group.name,
                    "uname": player.name,
                    "member_id": member_id,
                    "cooperate": cooperate,
                    "other_uname": other.name,
                    "other_cooperate": other_cooperate,
                    "payoff": player_data.get("payoff"),
                    "motion_attestation_checks": attestation.checks,
                    "motion_attestation_failed_checks": attestation.failed_checks,
                    "motion_attestation_mean_score": attestation.mean_score,
                }
            )

    return rows


page_order = [
    Instructions,
    GroupPlease,
    Dilemma,
    Sync,
    Results,
]
