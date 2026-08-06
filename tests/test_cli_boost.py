"""`tests/test_cli_boost.py` -- Unit tests for agent-saga boost CLI command.
"""

import argparse
from agent_saga.cli import _cmd_boost, build_parser


def test_cli_boost_command():
    parser = build_parser()
    args = parser.parse_args(["boost"])
    assert args.command == "boost"
    res = _cmd_boost(args)
    assert res == 0
