#!/usr/bin/env bash
# Install integrator for every Claude Code session on this machine (idempotent):
#   ~/.local/bin/integrator, the skills integrator + contributor, the agents integrator + contributor,
#   and two hooks in ~/.claude/settings.json (backed up first):
#     SessionStart                          `integrator hook session-start`  tells a new session an integrator runs here
#     PreToolUse Edit|Write|MultiEdit|NotebookEdit  `integrator hook pre-edit`  keeps edits out of the main checkout and
#                                                                               others' claims; delivers notices
#   Both do nothing (about 30-50 ms, Python's start-up) in repos without a live integrator, and fail open.
#   ./install.sh --uninstall   removes all of it again.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
BIN="$HOME/.local/bin"; SK="$HOME/.claude/skills"; AG="$HOME/.claude/agents"; SET="$HOME/.claude/settings.json"
# The hooks call this checkout wherever it lives; moving it means running ./install.sh again.
HOOK="\"$HERE/bin/integrator\" hook"
mode="${1:-install}"
mkdir -p "$BIN" "$SK" "$AG"
link() { if [[ -e "$2" && ! -L "$2" ]]; then echo "skip $2: exists and isn't a link" >&2; else ln -sfn "$1" "$2"; echo "linked $2"; fi; }
unlink_ours() { [[ -L "$1" && "$(readlink "$1")" == "$HERE"* ]] && rm "$1" && echo "removed $1" || true; }
if [[ "$mode" == "--uninstall" ]]; then
  for p in "$BIN/integrator" "$SK/integrator" "$SK/contributor" "$AG/integrator.md" "$AG/contributor.md"; do unlink_ours "$p"; done
else
  link "$HERE/bin/integrator" "$BIN/integrator"
  link "$HERE/skills/integrator" "$SK/integrator"
  link "$HERE/skills/contributor" "$SK/contributor"
  link "$HERE/agents/integrator.md" "$AG/integrator.md"
  link "$HERE/agents/contributor.md" "$AG/contributor.md"
fi
[[ -f "$SET" ]] || echo '{}' > "$SET"
cp "$SET" "$SET.bak-integrator-$(date +%Y%m%d-%H%M%S)"
python3 - "$SET" "$mode" "$HOOK" <<'PY'
import json, sys
path, mode, hook = sys.argv[1], sys.argv[2], sys.argv[3]
d = json.load(open(path))
hooks = d.setdefault("hooks", {})
want = {"SessionStart": (None, f"{hook} session-start"),
        "PreToolUse": ("Edit|Write|MultiEdit|NotebookEdit", f"{hook} pre-edit")}
changed = False
for ev, (matcher, cmd) in want.items():
    groups = hooks.setdefault(ev, [])
    for g in groups:  # drop ours (any older form) first
        before = len(g.get("hooks", []))
        g["hooks"] = [h for h in g.get("hooks", []) if "/bin/integrator\" hook" not in h.get("command", "")]
        changed |= len(g["hooks"]) != before
    hooks[ev] = groups = [g for g in groups if g.get("hooks")]
    if mode != "--uninstall":
        g = {"hooks": [{"type": "command", "command": cmd, "timeout": 10}]}
        if matcher:
            g["matcher"] = matcher
        groups.append(g)
        changed = True
    if not groups:
        del hooks[ev]
json.dump(d, open(path, "w"), indent=2)
open(path, "a").write("\n")
print(("removed" if mode == "--uninstall" else "installed") + " the integrator hooks in " + path)
PY
