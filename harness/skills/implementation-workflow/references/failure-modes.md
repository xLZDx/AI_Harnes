# implementation-workflow: failure modes

Load on demand. Each line is a defect class seen in practice.

- Fixing the symptom at the call site while the root cause stays in the shared helper.
- A test that passes before and after the fix (it proves nothing).
- Scripted writes that flip CRLF/LF or re-encode the file - diff after every scripted write.
- Editing a file another session or agent is concurrently changing - inspect the real worktree first.
