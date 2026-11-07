#!/usr/bin/env bash
# run-demo.sh: play a small parallel-development story against the real `integrator` CLI and print the transcript.
#
#   docs/demo/run-demo.sh                              # transcript on stdout
#   docs/demo/run-demo.sh > docs/data/demo-session.txt # how the README's session was captured
#
# What is real: every `integrator` and `git` command in the transcript runs for real, in a throwaway git repository
# under a temporary directory. The directory is removed on exit (KEEP=1 keeps it, DEMO_DIR=<dir> picks where it
# goes). Nothing else is touched: git's own config files and the real home directory are ignored.
# What is stood in: the Claude Code sessions. Each role is a shell carrying the two variables Claude Code sets in
# its Bash tool (CLAUDE_CODE_SESSION_ID and CLAUDE_PID, here the pid of a `sleep` process), which is also how the
# test-suite plays them. The file edits a contributor's Edit tool would make are applied by this script and appear
# as "# ..." lines. The temporary directory is shown as ~/work.
# Needs: bash, git >= 2.40, python3 >= 3.11, jq.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
mkdir -p "${DEMO_DIR:-${TMPDIR:-/tmp}}"
WORK="$(mktemp -d "${DEMO_DIR:-${TMPDIR:-/tmp}}/integrator-demo.XXXXXX")"
REAL_WORK="$(cd "$WORK" && pwd -P)"
REPO="$REAL_WORK/csvtool"
cleanup() {
  for f in "$WORK"/pid-*; do [ -e "$f" ] && kill "$(cat "$f")" 2>/dev/null || true; done
  [ "${KEEP:-}" = 1 ] && echo "kept $WORK" >&2 || rm -rf "$WORK"
}
trap cleanup EXIT

# an isolated environment: no real home, no user git config, fixed identity and dates (so commit ids repeat)
export HOME="$WORK/home" GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 PYTHONDONTWRITEBYTECODE=1
export GIT_AUTHOR_NAME=Demo GIT_AUTHOR_EMAIL=demo@example.com GIT_COMMITTER_NAME=Demo GIT_COMMITTER_EMAIL=demo@example.com
export GIT_AUTHOR_DATE="2026-01-01T09:00:00+0000" GIT_COMMITTER_DATE="2026-01-01T09:00:00+0000"
mkdir -p "$HOME" "$WORK/bin"
ln -s "$ROOT/bin/integrator" "$WORK/bin/integrator"
ln -s "$ROOT/docs/demo/claude-event" "$WORK/bin/claude-event"
export PATH="$WORK/bin:$PATH"
unset CLAUDE_CODE_SESSION_ID CLAUDE_PID

# ------------------------------------------------------------------ the roles
for r in integrator csv-quotes csv-header cli-delim newcomer; do
  sleep 3600 & echo $! > "$WORK/pid-$r"; disown  # a live process for the role's CLAUDE_PID
done
role_dir() {  # a contributor works in its worktree once it has one; everyone else in the main checkout
  if [ -d "$REPO/.claude/worktrees/$1" ]; then echo "$REPO/.claude/worktrees/$1"; else echo "$REPO"; fi
}
as() {  # as ROLE 'COMMAND': show the command, run it as that role, show its output and a non-zero exit status
  local role=$1 cmd=$2 rc=0
  printf '[%s] $ %s\n' "$role" "$cmd"
  (cd "$(role_dir "$role")" &&
    CLAUDE_CODE_SESSION_ID="sess-$role" CLAUDE_PID="$(cat "$WORK/pid-$role")" bash -c "$cmd") 2>&1 || rc=$?
  [ "$rc" -eq 0 ] || printf '[exit %s]\n' "$rc"
}
note() { printf '# %s\n' "$*"; }
wt() { echo "$REPO/.claude/worktrees/$1"; }

# ------------------------------------------------------------------ the project
mkdir -p "$REPO/tests" && cd "$REPO"
git init -q -b main
cat > .integrator.toml <<'TOML'
check = "python3 -m unittest -q"
conflict_hint = "keep both sides: split_line() is the new splitter, read_rows() its only caller"
TOML
printf '__pycache__/\n' > .gitignore
cat > reader.py <<'PY'
"""Read a small CSV: one record per line, fields split on the delimiter."""


def read_rows(text, delimiter=","):
    rows = []
    for line in text.splitlines():
        rows.append(line.split(delimiter))
    return rows
PY
cat > cli.py <<'PY'
"""csvtool: print the rows of a CSV file as a table."""
import sys

from reader import read_rows


def main(argv):
    for row in read_rows(open(argv[1]).read()):
        print(" | ".join(row))


if __name__ == "__main__":
    main(sys.argv)
PY
touch tests/__init__.py
cat > tests/test_reader.py <<'PY'
import unittest

from reader import read_rows


class ReadRows(unittest.TestCase):
    def test_splits_on_commas(self):
        self.assertEqual(read_rows("a,b\n1,2"), [["a", "b"], ["1", "2"]])
PY
git add -A && git commit -qm "csvtool: read_rows, a small CLI and a test"

# ------------------------------------------------------------------ what each contributor's Edit tool would do
edit_quotes() {
  cd "$(wt csv-quotes)"
  cat > reader.py <<'PY'
"""Read a small CSV: one record per line, fields split on the delimiter."""


def split_line(line, delimiter=","):
    """Split one line, keeping delimiters that sit inside double quotes."""
    fields, field, quoted = [], "", False
    for ch in line:
        if ch == '"':
            quoted = not quoted
        elif ch == delimiter and not quoted:
            fields.append(field)
            field = ""
        else:
            field += ch
    fields.append(field)
    return fields


def read_rows(text, delimiter=","):
    rows = []
    for line in text.splitlines():
        rows.append(split_line(line, delimiter))
    return rows
PY
  cat > tests/test_quotes.py <<'PY'
import unittest

from reader import read_rows


class Quotes(unittest.TestCase):
    def test_comma_inside_quotes(self):
        self.assertEqual(read_rows('fig,"1,5 kg"'), [["fig", "1,5 kg"]])
PY
}
edit_header() {
  cd "$(wt csv-header)"
  cat > reader.py <<'PY'
"""Read a small CSV: one record per line, fields split on the delimiter."""


def read_rows(text, delimiter=",", skip_header=False):
    rows = []
    for line in text.splitlines()[1 if skip_header else 0:]:
        rows.append(line.split(delimiter))
    return rows
PY
  cat > tests/test_header.py <<'PY'
import unittest

from reader import read_rows


class Header(unittest.TestCase):
    def test_skip_header(self):
        self.assertEqual(read_rows("name,qty\nfig,3", skip_header=True), [["fig", "3"]])
PY
}
edit_cli() {
  cd "$(wt cli-delim)"
  cat > cli.py <<'PY'
"""csvtool: print the rows of a CSV file as a table."""
import sys

from reader import read_rows


def parse_args(argv):
    """Return (path, delimiter) from `cli.py FILE [--delimiter D]`."""
    delimiter = ","
    if "--delimiter" in argv:
        i = argv.index("--delimiter")
        delimiter = argv[i + 1]
        del argv[i:i + 2]
    return argv[1], delimiter


def main(argv):
    path, delimiter = parse_args(list(argv))
    for row in read_rows(open(path).read(), delimiter):
        print(" | ".join(row))


if __name__ == "__main__":
    main(sys.argv)
PY
  cat > tests/test_cli.py <<'PY'
import unittest

from cli import parse_args


class ParseArgs(unittest.TestCase):
    def test_delimiter_option(self):
        self.assertEqual(parse_args(["cli.py", "data.tsv", "--delimiter", "\t"]), ("data.tsv", "\t"))

    def test_default_delimiter(self):
        self.assertEqual(parse_args(["cli.py", "data.csv"]), ("data.csv", ","))
PY
}
resolve_reader() {  # the integrator's hand resolution of the conflict: both sides' intent kept
  cat > "$(wt integration)/reader.py" <<'PY'
"""Read a small CSV: one record per line, fields split on the delimiter."""


def split_line(line, delimiter=","):
    """Split one line, keeping delimiters that sit inside double quotes."""
    fields, field, quoted = [], "", False
    for ch in line:
        if ch == '"':
            quoted = not quoted
        elif ch == delimiter and not quoted:
            fields.append(field)
            field = ""
        else:
            field += ch
    fields.append(field)
    return fields


def read_rows(text, delimiter=",", skip_header=False):
    rows = []
    for line in text.splitlines()[1 if skip_header else 0:]:
        rows.append(split_line(line, delimiter))
    return rows
PY
}

# ------------------------------------------------------------------ the story
story() {
  note "csvtool is a tiny CSV reader. Three Claude Code sessions will change it at once; a fourth lands their work."
  as integrator 'cat .integrator.toml'
  as integrator 'integrator start --session integrator-csvtool'

  note "a session that opens the repo meanwhile is told, and cannot edit the main checkout"
  as newcomer 'claude-event session-start | integrator hook session-start'
  as newcomer 'claude-event pre-edit reader.py | integrator hook pre-edit | jq .hookSpecificOutput'

  note 'contributors join: `launch` makes the worktree and prints the command that starts its session'
  as integrator 'integrator launch contributor csv-quotes "Keep commas inside quoted fields" --print'
  as csv-quotes 'integrator join csv-quotes --session csv-quotes'
  as csv-quotes 'integrator scope "reader.py: new split_line(), read_rows() calls it; tests/test_quotes.py"'
  as csv-quotes 'integrator claim reader.py tests/test_quotes.py'
  as csv-header 'integrator join csv-header --task "Skip the header row on request" --session csv-header'
  as csv-header 'integrator scope "reader.py: read_rows() gets a skip_header parameter; tests/test_header.py"'
  as csv-header 'integrator claim reader.py tests/test_header.py'
  as integrator 'integrator grant csv-header reader.py --note "read_rows() only: a small edit, lands second"'
  as cli-delim 'integrator join cli-delim --task "Add a --delimiter option" --session cli-delim'
  as cli-delim 'integrator claim cli.py tests/test_cli.py'

  note "a claimed file is protected from the other sessions' Edit tool"
  as cli-delim 'claude-event pre-edit reader.py | integrator hook pre-edit | jq -r .hookSpecificOutput.permissionDecisionReason'

  note "the three sessions work, each in its own worktree (edits applied by this script)"
  ( edit_quotes ) ; as csv-quotes 'git add -A && git commit -qm "Keep commas inside quoted fields"'
  ( edit_header ) ; as csv-header 'git add -A && git commit -qm "read_rows can skip the header row"'
  ( edit_cli )    ; as cli-delim  'git add -A && git commit -qm "csvtool takes --delimiter"'
  note "before anything is submitted, the integrator trial-merges every branch against main and the others"
  as integrator 'integrator conflicts'

  note "each contributor checks its own commit, then submits it"
  as csv-quotes 'integrator check'
  as csv-quotes 'integrator submit --summary "Keep commas inside quoted fields"'
  as csv-header 'integrator check'
  as csv-header 'integrator submit --summary "read_rows can skip the header row"'
  as cli-delim 'integrator check'
  as cli-delim 'integrator submit --summary "csvtool takes --delimiter"'
  as integrator 'integrator status'

  note "the integrator lands the queue; #2 collides with #1 in reader.py"
  as integrator 'integrator land all'
  as integrator 'sed -n "/<<<<<<</,/>>>>>>>/p" .claude/worktrees/integration/reader.py'
  note "it keeps both sides' intent in its integration worktree (edit applied by this script)"
  resolve_reader
  as integrator 'git -C .claude/worktrees/integration add reader.py'
  as integrator 'integrator land --continue'
  as integrator 'integrator land --continue --verify'
  as integrator 'git log --oneline'

  note "the contributors hear about it at their next edit, sync, and leave; the integrator tidies"
  as cli-delim 'claude-event pre-edit cli.py | integrator hook pre-edit | jq -r .hookSpecificOutput.additionalContext'
  as cli-delim 'integrator status --me'
  as cli-delim 'integrator sync'
  as csv-quotes 'integrator leave'
  as csv-header 'integrator leave'
  as cli-delim 'integrator leave'
  as integrator 'integrator prune'
}

story | sed -e "s#$REAL_WORK#~/work#g" -e "s#$WORK#~/work#g"
