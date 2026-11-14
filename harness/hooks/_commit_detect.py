"""Shared helper for PreToolUse gates: is `git commit` / `git push` really executed by a shell command?

A plain regex over the command text also fires when the words are only mentioned inside a quoted
string or a heredoc (for example a commit message or a test that contains them). A real commit or push
sits at command position, possibly behind a wrapper word (`time`, `env X=1`, `if`, `{`, `!`), with a path
or options in front (`/usr/bin/git`, `"C:/Program Files/Git/bin/git.exe"`, `git -C dir`), or inside the
argument of a shell (`bash -c "git push"`). Detection errs on the side of "this is a push": a missed push
skips the review gate, a false alarm only asks for a receipt. Stdlib only.
"""
import re

_PREFIX = (
    r"(?:^|[;&|\n(`{!]|\b(?:then|do|else|elif|if|while|until|time|exec|sudo|command|nohup|env)\b)\s*"
    r"(?:\w+=\S+\s+)*"
)
_GIT = r"(?:\S*[\\/])?git(?:\.exe)?"
_GIT_OPTS = (
    r"(?:(?:-[Cc]\s+(?:\"[^\"]*\"|'[^']*'|\S+)|--no-pager|-p|--paginate|--git-dir(?:=|\s+)\S+"
    r"|--work-tree(?:=|\s+)\S+|--namespace(?:=|\s+)\S+|--exec-path(?:=\S+)?|-P|--no-optional-locks"
    r"|--literal-pathspecs|--bare)\s+)*"
)
COMMAND_POS_COMMIT_RE = re.compile(_PREFIX + r"git\s+commit\b", re.IGNORECASE)
COMMAND_POS_PUSH_RE = re.compile(_PREFIX + _GIT + r"\s+" + _GIT_OPTS + r"push\b", re.IGNORECASE)
# a quoted path to git.exe (a space in the path forces the quotes), then options and push
QUOTED_GIT_PUSH_RE = re.compile(
    r"(?:^|[;&|\n(`{!]|&\s)\s*[\"'][^\"'\n]*[\\/]git(?:\.exe)?[\"']\s+" + _GIT_OPTS + r"push\b", re.IGNORECASE)
# a shell or interpreter that can run a quoted command string
WRAPPER_RE = re.compile(
    r"(?:^|[;&|\n(`{!]|\s)(?:\S*[\\/])?(?:bash|sh|zsh|dash|pwsh|powershell|cmd|iex|invoke-expression|eval|ssh|xargs"
    r"|sudo|exec|env|nohup|start|start-process|wsl|python3?|node)(?:\.exe)?\b",
    re.IGNORECASE)
HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n[ \t]*\2[ \t]*(?=\n|$)", re.DOTALL)
QUOTED_RE = re.compile(r"\"(?:\\.|[^\"\\])*\"|'[^']*'", re.DOTALL)
_SEGMENT_SPLIT_RE = re.compile(r"[;&|\n]")


def is_commit_command(command: str) -> bool:
    stripped = HEREDOC_RE.sub("<<HEREDOC", command)
    stripped = QUOTED_RE.sub('""', stripped)
    return bool(COMMAND_POS_COMMIT_RE.search(stripped))


def is_push_command(command: str) -> bool:
    """True when `git push` is executed, or may be:
    - at command position in the text with heredocs and quoted strings blanked (wrapper words, paths and
      git options allowed);
    - a quoted path to git.exe followed by push;
    - a quoted string that itself holds a push at command position AND follows a shell or interpreter word
      in the same command segment (`bash -c "git push"`, `ssh host 'git push'`): a commit message that only
      mentions a push never follows such a word;
    - an odd number of quote characters (an apostrophe in prose can pair with a later quote and hide a real
      push): then the unblanked text is tested at command position."""
    no_heredoc = HEREDOC_RE.sub("<<HEREDOC", command)
    stripped = QUOTED_RE.sub('""', no_heredoc)
    if COMMAND_POS_PUSH_RE.search(stripped) or QUOTED_GIT_PUSH_RE.search(no_heredoc):
        return True
    for m in QUOTED_RE.finditer(no_heredoc):
        inner = m.group(0)[1:-1]
        if COMMAND_POS_PUSH_RE.search(inner):
            before = _SEGMENT_SPLIT_RE.split(no_heredoc[:m.start()])[-1]
            if WRAPPER_RE.search(before):
                return True
    if no_heredoc.count('"') % 2 or no_heredoc.count("'") % 2:
        return bool(COMMAND_POS_PUSH_RE.search(no_heredoc))
    return False
