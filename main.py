#!/usr/bin/env python
# SPDX-License-Identifier: 0BSD
#
# Third-party dependencies:
# - uproot: LGPL v3+, see ./uproot_license.txt
import uproot.deployment as upd
from uproot.cli import cli
from uproot.rooms import room
from uproot.server import load_config, uproot_server

upd.project_metadata(created="2026-09-10", uproot="0.5.1")

# Load your app configs here
# Examples are available at https://github.com/mrpg/uproot-examples

load_config(uproot_server, config="study01", apps=["prisoners_dilemma"])

# Create admin

upd.ADMINS["admin"] = upd.auto_login()  # Leave as-is to enable auto login

# Create room if it does not exist

upd.DEFAULT_ROOMS.append(
    room(
        "test",
        config="study01",
        open=True,
    )
)

# Other settings

upd.LANGUAGE = "en"

# Run uproot (leave this as-is)

if __name__ == "__main__":
    cli()
