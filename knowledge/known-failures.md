# MANTRA known-failure registry

Recurring failure classes injected into the prompt. Add an entry when a class is fixed.

Format:

## KF-N | short title
- symptom: what went wrong, observable
- rule: the concrete behavior the agent must follow
- date: YYYY-MM-DD

## KF-1 | editing a file that was never read
- symptom: edit without a prior read corrupted the file.
- rule: never call edit_file on a path you have not read in this session; the tool enforces this and will reject the edit.
- date: 2026-08-26

## KF-2 | entrypoint lost during bulk deletion
- symptom: removing demo code also removed the `if __name__ == "__main__"` guard, so the CLI exited silently with code 0.
- rule: after any deletion-based refactor, verify the module still has its intended entrypoint and run it once.
- date: 2026-08-26

## KF-3 | nested quotes in inline interpreter one-liners
- symptom: python -c "..." containing embedded single/double quotes or trailing-backslash raw strings fails to parse on Windows shells (unterminated string literal), burning a turn.
- rule: for any command needing more than trivial quoting, write a temporary script file and execute that instead of fighting shell escaping.
- date: 2026-08-26
