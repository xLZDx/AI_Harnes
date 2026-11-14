"""Shared helper for PreToolUse gates: is `git commit` / `git push` really executed by a shell command?

A plain regex over the command text also fires when the words are only mentioned inside a quoted
string or a heredoc (for example a commit message or a test that contains them). A real commit or push
sits at command position, possibly behind a wrapper word (`time`, `env X=1`, `if`, `{`, `!`, `xargs`,
`cmd /c`), with a path or options in front (`/usr/bin/git`, `"C:/Program Files/Git/bin/git.exe"`,
`git -C dir`, any `--long-option`), inside the argument of a shell (`bash -c "git push"`), inside a script
that an interpreter runs (`python -c "os.system('git push')"`) or in the body of a heredoc fed to a shell.
Detection errs on the side of "this is a push": a missed push skips the review gate, a false alarm only asks
for a receipt. A subcommand that is quoted or built by the shell (`git "push"`, `git $(echo push)`) and an
alias defined on the command line (`git -c alias.p=push p`) count as a push. This is a guard for honest
sessions, not a sandbox: the authority for the remote is the pre-push git hook. Stdlib only.
"""
import re

# options (and one value) a wrapper word may carry between itself and git: `xargs -n1`, `sudo -u x`, `nice -n 10`, `start /b`
_WOPTS = r"(?:\s+[-/]\S+(?:\s+(?!git\b|-|/)\w\S*)?){0,6}"  # bounded: no catastrophic backtracking on long input
_PREFIX = (
    r"(?:^|[;&|\n(`{!]|\b(?:then|do|else|elif|if|while|until|time|exec|command|nohup)\b"
    r"|\b(?:sudo|env|xargs|start|wsl|nice)\b" + _WOPTS +
    r"|\btimeout\s+\S+|\bcmd(?:\.exe)?\s+(?:/[a-z]\s+)*/c\b"
    r"|\b(?:powershell|pwsh)(?:\.exe)?\s+(?:-\w+(?:\s+(?!git\b)[^-\s]\S*)?\s+){0,6}-c(?:ommand)?\b)\s*"
    r"(?:\w+=\S+\s+)*"
)
_GIT = r"(?:\S*[\\/])?git(?:\.exe)?"
_VAL = r"(?:\"[^\"]*\"|'[^']*'|\S+)"
_GIT_OPTS = (
    r"(?:(?:-[Cc]\s+" + _VAL + r"|--git-dir\s+\S+|--work-tree\s+\S+|--namespace\s+\S+|-p|-P"
    r"|--[\w-]+(?:=" + _VAL + r")?)\s+){0,12}"
)
COMMAND_POS_COMMIT_RE = re.compile(_PREFIX + _GIT + r"\s+" + _GIT_OPTS + r"commit\b", re.IGNORECASE)
COMMAND_POS_PUSH_RE = re.compile(_PREFIX + _GIT + r"\s+" + _GIT_OPTS + r"push\b", re.IGNORECASE)
# a subcommand that is quoted or built by the shell: could be anything, including push
OBFUSCATED_RE = re.compile(_PREFIX + _GIT + r"\s+" + _GIT_OPTS + r"(?:\"[^\"]*\"|'[^']*'|\S*[$`(]\S*)", re.IGNORECASE)
# an alias defined on the command line can turn any word into a push
ALIAS_RE = re.compile(_PREFIX + _GIT + r"\s+(?:\S+\s+)*?-c\s+[\"']?alias\.", re.IGNORECASE)
# a quoted git executable (a space in its path forces the quotes), then options and push
QUOTED_GIT_PUSH_RE = re.compile(
    r"(?:^|[;&|\n(`{!]|&\s)\s*[\"'](?:[^\"'\n]*[\\/])?git(?:\.exe)?[\"']\s+" + _GIT_OPTS + r"push\b", re.IGNORECASE)
# a shell or interpreter that can run a quoted command string
WRAPPER_RE = re.compile(
    r"(?:^|[;&|\n(`{!]|\s)(?:\S*[\\/])?(?:bash|sh|zsh|dash|pwsh|powershell|cmd|iex|invoke-expression|eval|ssh|xargs"
    r"|sudo|exec|env|nohup|start|start-process|saps|wsl|py|python[\d.]*|pypy[\d.]*|node)(?:\.exe)?\b",
    re.IGNORECASE)
# a push piped into a shell or interpreter (`echo git push | sh`, `cat <<EOF | bash`)
PIPE_TO_SHELL_RE = re.compile(
    r"\|\s*(?:\S*[\\/])?(?:bash|sh|zsh|dash|pwsh|powershell|cmd|py|python[\d.]*|node|iex|invoke-expression|xargs)(?:\.exe)?\b",
    re.IGNORECASE)
# Start-Process git -ArgumentList push
START_PROCESS_RE = re.compile(
    r"\b(?:start-process|saps)\b[^\n;|&]{0,200}?\bgit(?:\.exe)?\b[^\n;|&]{0,200}?\bpush\b", re.IGNORECASE)
# eval / iex on something the shell builds (`$c='git push'; iex $c`)
EVAL_RE = re.compile(r"(?:^|[;&|\n(`{!]|\s)(?:iex|invoke-expression|eval)\s", re.IGNORECASE)
LOOSE_PUSH_RE = re.compile(r"\bgit(?:\.exe)?\b[^\n;|&]{0,300}?\bpush\b", re.IGNORECASE)
INTERPRETER_RE = re.compile(r"(?:^|\s)(?:\S*[\\/])?(?:py|python[\d.]*|pypy[\d.]*|node|perl|ruby|php|deno|bun)(?:\.exe)?\s", re.IGNORECASE)
SPAWN_RE = re.compile(r"system|popen|subprocess|\brun\s*\(|\bcall\s*\(|check_output|check_call|exec|spawn|child_process|`", re.IGNORECASE)
HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n[ \t]*\2[ \t]*(?=\n|$)", re.DOTALL)
QUOTED_RE = re.compile(r"\"(?:\\.|[^\"\\])*\"|'[^']*'", re.DOTALL)
_SEGMENT_SPLIT_RE = re.compile(r"[;&|\n]")


def _strip_heredocs(command: str) -> str:
    """Blank heredoc bodies (usually prose such as a commit message), except where the body may be code:
    a heredoc fed to a shell or interpreter keeps its body; `<<` inside quotes is no heredoc at all, except the
    mandated `$(cat <<'EOF' ...)` commit-message shape, which is blanked."""
    def repl(m):
        head = command[command.rfind("\n", 0, m.start()) + 1:m.start()]
        if re.search(r"\$\(\s*cat\s*$", head):
            return "<<HEREDOC"
        if head.count('"') % 2 or head.count("'") % 2:
            return m.group(0)
        first_line = m.group(0).split("\n", 1)[0]
        if WRAPPER_RE.search(head) or INTERPRETER_RE.search(head) or PIPE_TO_SHELL_RE.search(first_line):
            return m.group(0)
        return "<<HEREDOC"
    return HEREDOC_RE.sub(repl, command)


def is_commit_command(command: str) -> bool:
    stripped = _strip_heredocs(command)
    stripped = QUOTED_RE.sub('""', stripped)
    return bool(COMMAND_POS_COMMIT_RE.search(stripped))


def is_push_command(command: str) -> bool:
    """True when `git push` is executed, or may be:
    - at command position in the text with heredocs and quoted strings blanked (wrapper words, paths and
      any git options allowed), or a quoted/obfuscated subcommand, or a command-line alias;
    - a quoted path to git.exe followed by push;
    - a quoted string holding a push at command position that follows a shell or interpreter word in the same
      segment (`bash -c "git push"`), or a script handed to an interpreter that starts a process and names
      git push; a commit message or a print that only mentions a push is not flagged;
    - an odd number of quote characters: then the unblanked text is tested at command position."""
    command = command.replace("\\\n", " ").replace("\\'", "")
    no_heredoc = _strip_heredocs(command)
    stripped = QUOTED_RE.sub('""', no_heredoc)
    if (COMMAND_POS_PUSH_RE.search(stripped) or QUOTED_GIT_PUSH_RE.search(no_heredoc)
            or OBFUSCATED_RE.search(stripped) or ALIAS_RE.search(no_heredoc)):
        return True
    if START_PROCESS_RE.search(no_heredoc):  # on the unblanked text: the argument list is usually quoted
        return True
    if EVAL_RE.search(stripped) and LOOSE_PUSH_RE.search(no_heredoc):
        return True
    if PIPE_TO_SHELL_RE.search(stripped) and LOOSE_PUSH_RE.search(no_heredoc):
        return True
    for hm in HEREDOC_RE.finditer(no_heredoc):  # a script fed to an interpreter that starts a process naming git push
        first_line = hm.group(0).split("\n", 1)[0]
        body = hm.group(0)[len(first_line):]
        head = no_heredoc[no_heredoc.rfind("\n", 0, hm.start()) + 1:hm.start()]
        if INTERPRETER_RE.search(head + first_line + " ") and LOOSE_PUSH_RE.search(body) and SPAWN_RE.search(body):
            return True
    for m in QUOTED_RE.finditer(no_heredoc):
        inner = m.group(0)[1:-1]
        before = _SEGMENT_SPLIT_RE.split(no_heredoc[:m.start()])[-1]
        if COMMAND_POS_PUSH_RE.search(inner) and WRAPPER_RE.search(before):
            return True
        if INTERPRETER_RE.search(before) and LOOSE_PUSH_RE.search(inner) and SPAWN_RE.search(inner):
            return True
    if stripped.count('"') % 2 or stripped.count("'") % 2:  # a quote that never closes is left in the blanked text
        return bool(COMMAND_POS_PUSH_RE.search(no_heredoc))
    return False
