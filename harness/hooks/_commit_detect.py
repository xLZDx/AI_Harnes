"""Shared helper for PreToolUse gates: is `git commit` / `git push` really executed by a shell command?

A plain regex over the command text also fires when the words are only mentioned inside a quoted
string or a heredoc (for example a Python script or a test that contains them). A real commit or push
sits at command position, possibly behind a wrapper (`time`, `env X=1`, `if`, `{`, `!`) or inside the
argument of a shell (`bash -c "git push"`). Detection errs on the side of "this is a push": a missed
push skips the review gate, a false alarm only asks for a receipt. Stdlib only.
"""
import re

_PREFIX = (
    r"(?:^|[;&|\n(`{!]|\b(?:then|do|else|elif|if|while|until|time|exec|sudo|command|nohup|env)\b)\s*"
    r"(?:\w+=\S+\s+)*"
)
COMMAND_POS_COMMIT_RE = re.compile(_PREFIX + r"git\s+commit\b", re.IGNORECASE)
_GIT_OPTS = (
    r"(?:(?:-[Cc]\s+(?:\"[^\"]*\"|'[^']*'|\S+)|--no-pager|--git-dir=\S+|--work-tree=\S+|-P|--no-optional-locks"
    r"|--literal-pathspecs|--bare)\s+)*"
)
COMMAND_POS_PUSH_RE = re.compile(_PREFIX + r"git(?:\.exe)?\s+" + _GIT_OPTS + r"push\b", re.IGNORECASE)
# any `git ... push` anywhere, quoted or not: the loose raw test used before the precise ones
LOOSE_PUSH_RE = re.compile(r"\bgit(?:\.exe)?\b[^\n;|&]*?\bpush\b", re.IGNORECASE)
# a shell or interpreter that can run a quoted command string
WRAPPER_RE = re.compile(
    r"(?:^|[;&|\n(`{!]|\s)(?:\S*[\\/])?(?:bash|sh|zsh|dash|pwsh|powershell|cmd|iex|invoke-expression|eval|ssh|xargs"
    r"|sudo|exec|env|nohup|start|start-process|wsl|python3?|node)(?:\.exe)?\b",
    re.IGNORECASE)
HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n[ \t]*\2[ \t]*(?=\n|$)", re.DOTALL)
QUOTED_RE = re.compile(r"\"(?:\\.|[^\"\\])*\"|'[^']*'", re.DOTALL)


def is_commit_command(command: str) -> bool:
    stripped = HEREDOC_RE.sub("<<HEREDOC", command)
    stripped = QUOTED_RE.sub('""', stripped)
    return bool(COMMAND_POS_COMMIT_RE.search(stripped))


def is_push_command(command: str) -> bool:
    """True when `git push` is executed, or may be: at command position (also behind a wrapper word or
    after an apostrophe that confuses quote stripping), or quoted but handed to a shell/interpreter."""
    no_heredoc = HEREDOC_RE.sub("<<HEREDOC", command)
    if COMMAND_POS_PUSH_RE.search(no_heredoc):
        return True
    stripped = QUOTED_RE.sub('""', no_heredoc)
    if COMMAND_POS_PUSH_RE.search(stripped):
        return True
    return bool(LOOSE_PUSH_RE.search(no_heredoc) and WRAPPER_RE.search(no_heredoc))
