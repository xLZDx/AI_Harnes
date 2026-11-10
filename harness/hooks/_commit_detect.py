"""Shared helper for PreToolUse gates: is `git commit` really executed by a shell command?

A plain regex over the command text also fires when `git commit` is only mentioned inside a quoted
string or a heredoc body (for example a Python script or a test that contains the words). A real
commit sits at command position. Stdlib only.
"""
import re

COMMAND_POS_COMMIT_RE = re.compile(
    r"(?:^|[;&|\n(`]|\b(?:then|do|else)\b)\s*(?:\w+=\S+\s+)*git\s+commit\b", re.IGNORECASE)
HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n[ \t]*\2[ \t]*(?=\n|$)", re.DOTALL)
QUOTED_RE = re.compile(r"\"(?:\\.|[^\"\\])*\"|'[^']*'", re.DOTALL)


def is_commit_command(command: str) -> bool:
    stripped = HEREDOC_RE.sub("<<HEREDOC", command)
    stripped = QUOTED_RE.sub('""', stripped)
    return bool(COMMAND_POS_COMMIT_RE.search(stripped))
