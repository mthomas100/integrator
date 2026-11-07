"""integrator: several Claude Code sessions develop one repo in parallel, and one of them lands everyone's work.

The INTEGRATOR (one per repo) owns the main branch and the main checkout. It lands what contributors submit:
a fast-forward when nothing moved, a rebase in its own integration worktree otherwise. It resolves conflicts
there, warns early when two branches head for the same lines, and runs the checks itself only when a landing
isn't covered by the contributor's own (a hand-resolved conflict, or main having changed the same files).
CONTRIBUTORS (any number) each work in their own worktree and branch, claim the paths they will change,
commit small units, check them and submit them.

State lives in <git common dir>/integrator/ (every worktree of the repo sees it; nothing is committed):
state.json, events.jsonl (what `watch` streams) and checks/ (check logs). Settings: .integrator.toml in the
main checkout (optional). See README.md beside bin/, and the integrator and contributor skills.

Integrator
  start --session NAME [--takeover]        register this session as the repo's integrator
  watch [--all] [--since N]                stream the events that need the integrator (run it under Monitor)
  land [next|all|ID...] [--verify] [--trust WHY] [--merge]
  land --continue [--verify|--trust WHY]   after resolving a conflict, or to decide a NEEDS-VERIFY
  land --abort                             drop the landing in progress (submissions stay queued)
  bounce ID WHY                            send a submission back to its contributor
  tell TOPIC|all TEXT                      a notice for contributors (shown at their next edit or status)
  grant TOPIC PATH                         let TOPIC edit PATH although someone else claimed it
  prune                                    remove worktrees of contributors who left with everything landed
  stop [--force]                           deregister
Contributor
  join TOPIC [--task T] [--session NAME]   make (or adopt) your worktree and register it
  scope TEXT                               your scope on the board, once you know it: paths, interfaces others
                                           use, shared resources (the others see it; the integrator wakes)
  claim PATH... [--shared] [--note N]      claim the paths you will change (exclusive unless --shared)
  release [PATH...|--all]
  check [--cmd CMD]                        run the repo's checks here; the result is kept for this commit's tree
  submit [--summary S] [--no-check WHY]
  sync [--withdraw]                        rebase your branch onto main
  ask TEXT                                 a question for the integrator (its watch wakes it)
  notices                                  your unread notices
  leave [--abandon WHY]
Both
  status [--me] [--json]                   the board (or just your part of it)
  conflicts                                predicted conflicts: each branch against main and against the others
  launch integrator [--name N] | launch contributor TOPIC TASK [--name N]   [--split|--tab|--bg|--print]
  hook session-start|pre-edit              Claude Code hooks (install.sh adds them)
Exit codes: 0 ok, 1 refused, 2 usage, 3 conflict, 4 needs verification, 5 verification failed.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent  # bin/
OK, REFUSED, USAGE, CONFLICT, NEEDS_VERIFY, VERIFY_FAILED = 0, 1, 2, 3, 4, 5
RESERVED = {"integration", "main", "all", "next"}
WAKE = {"submit", "ask", "join", "scope", "leave", "overlap", "claim-refused", "sync-conflict", "withdraw"}
NEEDS = {"overlap", "resolved", "unchecked"}
DEFAULTS: dict = {
    "main": "", "check": "", "worktree_root": ".claude/worktrees", "branch_prefix": "wt/",
    "shared": [], "trivial": ["*.md", "docs/**"], "setup": "",
    # Extra flags for the `claude` sessions that `launch` starts. Empty = Claude Code's normal permission prompts.
    # An unattended team needs its tools pre-approved; `--dangerously-skip-permissions` is the blunt way and is
    # an opt-in for an isolated machine or container only (see README, "Launching sessions").
    "launch_flags": "", "verify": {}, "quick": {}, "conflict_hint": "",
}
EMPTY: dict = {"v": 1, "repo": "", "main": "", "integrator": None, "contributors": {}, "claims": [], "subs": [],
               "checks": {}, "landing": None, "notices": [], "seen_conflicts": [],
               "seq": {"sub": 0, "claim": 0, "notice": 0, "event": 0}}


class Refused(Exception):
    def __init__(self, msg: str, code: int = REFUSED):
        super().__init__(msg)
        self.code = code


# ---------------------------------------------------------------- small helpers

def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def secs_since(t: str | None) -> float:
    if not t:
        return float("inf")
    try:
        return (dt.datetime.now().astimezone() - dt.datetime.fromisoformat(t)).total_seconds()
    except ValueError:
        return float("inf")


def ago(t: str | None) -> str:
    s = secs_since(t)
    if s == float("inf"):
        return "?"
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{int(s // n)}{unit} ago"
    return "just now"


def alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def env_session() -> tuple[str | None, int | None]:
    """The calling Claude Code session (its Bash tool sets both), or (None, None) for a person at a terminal."""
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID") or None
    pid = os.environ.get("CLAUDE_PID", "")
    return sid, (int(pid) if pid.isdigit() else None)


def real(p) -> str:
    return os.path.realpath(str(p))


def short(sha: str | None) -> str:
    return (sha or "")[:9]


# ---------------------------------------------------------------- git

GIT_ENV = {**os.environ, "GIT_EDITOR": "true", "GIT_TERMINAL_PROMPT": "0", "GIT_MERGE_AUTOEDIT": "no"}


def run(args: list[str], cwd, check: bool = False) -> subprocess.CompletedProcess:
    r = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, env=GIT_ENV)
    if check and r.returncode != 0:
        raise Refused(f"`{' '.join(args)}` failed in {cwd}:\n{(r.stderr or r.stdout).strip()[-1500:]}")
    return r


def git(cwd, *args: str) -> str:
    return run(["git", *args], cwd, check=True).stdout.strip()


def git_ok(cwd, *args: str) -> bool:
    return run(["git", *args], cwd).returncode == 0


def rev(cwd, ref: str) -> str:
    return git(cwd, "rev-parse", "--verify", "--quiet", ref + "^{commit}")


def tree_of(cwd, ref: str) -> str:
    return git(cwd, "rev-parse", ref + "^{tree}")


def is_ancestor(cwd, a: str, b: str) -> bool:
    return git_ok(cwd, "merge-base", "--is-ancestor", a, b)


def merge_base(cwd, a: str, b: str) -> str:
    return git(cwd, "merge-base", a, b)


def names(cwd, a: str, b: str) -> list[str]:
    return [x for x in git(cwd, "diff", "--name-only", "--no-renames", a, b).splitlines() if x]


def branch_exists(cwd, branch: str) -> bool:
    return git_ok(cwd, "show-ref", "--verify", "-q", f"refs/heads/{branch}")


def dirty_files(wt, untracked: bool = True) -> list[str]:
    if not Path(wt).is_dir():
        return []
    out = run(["git", "diff", "--name-only", "--no-renames", "HEAD"], wt).stdout.splitlines()
    if untracked:
        out += run(["git", "ls-files", "--others", "--exclude-standard"], wt).stdout.splitlines()
    return sorted({x for x in out if x})


def in_progress(wt) -> str | None:
    for name in ("rebase-merge", "rebase-apply"):
        p = Path(git(wt, "rev-parse", "--path-format=absolute", "--git-path", name))
        if p.exists():
            return "rebase"
    if git_ok(wt, "rev-parse", "-q", "--verify", "MERGE_HEAD"):
        return "merge"
    return None


def conflicted(wt) -> list[str]:
    return [x for x in run(["git", "diff", "--name-only", "--diff-filter=U"], wt).stdout.splitlines() if x]


def predict(cwd, a: str, b: str, base: str | None = None) -> list[str] | None:
    """The files a merge of a and b would conflict in ([] when it merges cleanly), None when git can't tell."""
    args = ["git", "merge-tree", "--write-tree", "--name-only", "--no-messages"]
    if base:
        args.append(f"--merge-base={base}")
    r = run(args + [a, b], cwd)
    if r.returncode == 0:
        return []
    if r.returncode == 1:
        return [x for x in r.stdout.splitlines()[1:] if x.strip()]
    return None


def worktrees(cwd) -> list[dict]:
    items, cur = [], {}
    for line in git(cwd, "worktree", "list", "--porcelain").splitlines() + [""]:
        if not line:
            if cur:
                items.append(cur)
            cur = {}
            continue
        k, _, v = line.partition(" ")
        cur[k] = v if v else True
    return items


# ---------------------------------------------------------------- claims (paths, directories ending in /, globs)

GLOB = set("*?[")


def is_glob(p: str) -> bool:
    return any(ch in GLOB for ch in p)


def literal_prefix(p: str) -> str:
    for i, ch in enumerate(p):
        if ch in GLOB:
            return p[:i]
    return p


def glob_re(pattern: str) -> re.Pattern:
    i, out = 0, []
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        elif pattern[i] == "[" and "]" in pattern[i + 1:]:
            j = pattern.index("]", i + 1)
            out.append(pattern[i:j + 1])
            i = j + 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def covers(claim: str, path: str) -> bool:
    """Does a claim cover this repo-relative path? A glob without a slash matches a file name anywhere."""
    if is_glob(claim):
        rx = glob_re(claim)
        return bool(rx.match(path) or ("/" not in claim and rx.match(path.rsplit("/", 1)[-1])))
    c = claim.rstrip("/")
    return path == c or path.startswith(c + "/")


def claims_overlap(a: str, b: str) -> bool:
    """Could two claims cover a common file? Conservative with globs (it may see overlaps that aren't)."""
    ga, gb = is_glob(a), is_glob(b)
    if not ga and not gb:
        a2, b2 = a.rstrip("/"), b.rstrip("/")
        return a2 == b2 or a2.startswith(b2 + "/") or b2.startswith(a2 + "/")
    if gb and not ga:
        a, b = b, a
        ga, gb = gb, ga
    if ga and not gb:
        b2 = b.rstrip("/")
        if covers(a, b2) or "/" not in a:
            return True
        pa = literal_prefix(a)
        return pa.startswith(b2 + "/") or (b2 + "/").startswith(pa)
    if "/" not in a or "/" not in b:
        return True
    pa, pb = literal_prefix(a), literal_prefix(b)
    return pa.startswith(pb) or pb.startswith(pa)


# ---------------------------------------------------------------- repo context, config, state

def load_config(root: Path) -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    p = root / ".integrator.toml"
    if p.exists():
        try:
            import tomllib  # type: ignore[import-not-found]
        except ImportError:
            try:
                import tomli as tomllib  # type: ignore[import-not-found,no-redef]
            except ImportError as e:
                raise Refused(f"{p} needs Python 3.11+ (tomllib) to read") from e
        data = tomllib.loads(p.read_text())
        unknown = set(data) - set(DEFAULTS)
        if unknown:
            print(f"integrator: ignoring unknown settings in {p.name}: {', '.join(sorted(unknown))}", file=sys.stderr)
        cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
    return cfg


class Ctx:
    def __init__(self, start: str | Path | None = None):
        start = Path(start or os.getcwd())
        r = run(["git", "rev-parse", "--show-toplevel"], start)
        if r.returncode != 0:
            raise Refused("not inside a git repository")
        self.top = Path(real(r.stdout.strip()))
        self.common = Path(real(git(start, "rev-parse", "--path-format=absolute", "--git-common-dir")))
        wts = worktrees(self.top)
        self.main_root = Path(real(wts[0]["worktree"]))  # the first entry is always the main worktree
        self.sd = self.common / "integrator"
        self.cfg = load_config(self.main_root)
        if not self.cfg["main"]:
            b = wts[0].get("branch")
            self.cfg["main"] = b.replace("refs/heads/", "", 1) if isinstance(b, str) else "main"
        self.main = self.cfg["main"]

    @property
    def iwt(self) -> Path:
        return Path(real(self.main_root / self.cfg["worktree_root"])) / "integration"

    def trivial(self, f: str) -> bool:
        return any(covers(p, f) for p in self.cfg["trivial"])

    def shared_path(self, p: str) -> bool:
        return any(s == p or covers(s, p.rstrip("/")) for s in self.cfg["shared"])


@contextlib.contextmanager
def locked(sd: Path):
    sd.mkdir(parents=True, exist_ok=True)
    with open(sd / "lock", "a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def read_state(sd: Path) -> dict:
    st = json.loads(json.dumps(EMPTY))
    p = sd / "state.json"
    if p.exists():
        st.update(json.loads(p.read_text()))
        for k, v in EMPTY["seq"].items():
            st["seq"].setdefault(k, v)
    return st


def write_state(sd: Path, st: dict) -> None:
    tmp = sd / f"state.json.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(st, indent=1, sort_keys=True))
    os.replace(tmp, sd / "state.json")


def update(ctx: Ctx, fn):
    with locked(ctx.sd):
        st = read_state(ctx.sd)
        out = fn(st)
        write_state(ctx.sd, st)
        return out


def load(ctx: Ctx) -> dict:
    return read_state(ctx.sd)  # writes replace the file atomically, so a read needs no lock


def emit(ctx: Ctx, st: dict, kind: str, who: str, text: str, **extra) -> None:
    """Append an event (call inside update(): the lock is held)."""
    st["seq"]["event"] += 1
    rec = {"n": st["seq"]["event"], "t": now(), "kind": kind, "who": who, "text": text, **extra}
    with open(ctx.sd / "events.jsonl", "a") as fh:
        fh.write(json.dumps(rec) + "\n")


def notify(st: dict, to: str, text: str, frm: str = "integrator") -> None:
    st["seq"]["notice"] += 1
    st["notices"].append({"id": st["seq"]["notice"], "to": to, "from": frm, "text": text, "t": now(), "seen": False})
    seen = [n for n in st["notices"] if n["seen"]]
    if len(seen) > 300:
        drop = {n["id"] for n in seen[:-300]}
        st["notices"] = [n for n in st["notices"] if n["id"] not in drop]


def take_notices(st: dict, topic: str) -> list[dict]:
    out = [n for n in st["notices"] if n["to"] == topic and not n["seen"]]
    for n in out:
        n["seen"] = True
    return out


def integ_live(st: dict) -> bool:
    i = st.get("integrator")
    if not i:
        return False
    if i.get("pid"):
        return alive(i["pid"])
    return secs_since(i.get("seen")) < 1800


def am_integrator(st: dict) -> bool:
    sid, _ = env_session()
    i = st.get("integrator")
    return bool(i and sid and i.get("session_id") == sid)


def need_integrator(st: dict) -> None:
    sid, _ = env_session()
    if am_integrator(st) or not sid:  # the integrator itself, or a person at a terminal
        return
    i = st.get("integrator")
    if not integ_live(st):
        raise Refused("no live integrator here. Become it with `integrator start --session \"<your ListAgents name>\"` "
                      "(the integrator skill), or submit and wait for one.")
    raise Refused(f"only the integrator ('{i['session']}') does this. As a contributor, submit (`integrator submit`) "
                  "or message it.")


def find_me(ctx: Ctx, st: dict) -> tuple[str | None, dict | None]:
    sid, _ = env_session()
    for t, c in st["contributors"].items():
        if real(c["worktree"]) == str(ctx.top):
            return t, c
    if sid:
        for t, c in st["contributors"].items():
            if c.get("session_id") == sid:
                return t, c
    return None, None


def need_me(ctx: Ctx, st: dict) -> tuple[str, dict]:
    t, c = find_me(ctx, st)
    if not t or c is None:
        raise Refused("this session isn't a registered contributor here: run `integrator join <topic>` first "
                      "(the contributor skill)")
    return t, c


def iname(st: dict) -> str:
    i = st.get("integrator") or {}
    return i.get("session") or "(no integrator)"


def resolve_path(ctx: Ctx, p: str, base: Path | None = None) -> str:
    """A path as given (absolute, or relative to the cwd) → repo-relative, with a trailing / for directories."""
    if is_glob(p):
        return p.lstrip("./") if p.startswith("./") else p
    raw = Path(p)
    absp = raw if raw.is_absolute() else Path(os.getcwd()) / raw
    root = base or ctx.top
    try:
        rel = Path(real(absp)).relative_to(real(root)).as_posix()
    except ValueError:
        rel = Path(os.path.normpath(p)).as_posix()
    if rel in ("", "."):
        raise Refused("claim a path inside the repo, not the whole repo", USAGE)
    if absp.is_dir() or p.endswith("/"):
        rel = rel.rstrip("/") + "/"
    return rel


def replay_from(ctx: Ctx, c: dict | None, head: str, main_tip: str) -> str:
    """Where a branch's own unlanded commits start: just after what already landed (when that landed rebased,
    so its commits aren't in main's history), else the merge base with main."""
    lu = (c or {}).get("landed_upto")
    if lu and git_ok(ctx.main_root, "cat-file", "-e", lu + "^{commit}") and is_ancestor(ctx.main_root, lu, head) \
            and not is_ancestor(ctx.main_root, lu, main_tip):
        return lu
    return merge_base(ctx.main_root, head, main_tip)


# ---------------------------------------------------------------- worktrees

def topic_ok(t: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,47}", t or "") or t in RESERVED:
        raise Refused(f"a topic is lowercase letters, digits, '.', '_' or '-' (at most 48), not one of "
                      f"{sorted(RESERVED)}: {t!r}", USAGE)
    return t


def wt_dir(ctx: Ctx, topic: str) -> Path:
    root = Path(real(ctx.main_root / ctx.cfg["worktree_root"]))
    inside = str(root).startswith(str(ctx.main_root) + os.sep)
    return root / (topic if inside else f"{ctx.main_root.name}-wt-{topic}")


def ensure_ignored(ctx: Ctx, path: Path) -> None:
    """A worktree folder inside the main checkout stays out of its git status (through .git/info/exclude)."""
    try:
        rel = Path(real(path)).relative_to(ctx.main_root) if path.exists() else path.relative_to(ctx.main_root)
    except ValueError:
        return
    if git_ok(ctx.main_root, "check-ignore", "-q", rel.as_posix() + "/"):
        return
    excl = ctx.common / "info" / "exclude"
    excl.parent.mkdir(exist_ok=True)
    with open(excl, "a") as fh:
        fh.write(f"/{rel.parent.as_posix()}/\n")


def branch_worktree(ctx: Ctx, branch: str) -> str | None:
    for w in worktrees(ctx.main_root):
        if w.get("branch") == f"refs/heads/{branch}":
            return real(w["worktree"])
    return None


def make_worktree(ctx: Ctx, topic: str) -> tuple[Path, str, bool]:
    branch = ctx.cfg["branch_prefix"] + topic
    have = branch_worktree(ctx, branch)
    if have:
        return Path(have), branch, False
    path = wt_dir(ctx, topic)
    if path.exists():
        raise Refused(f"{path} exists but holds no worktree on {branch}: move it away or pick another topic")
    ensure_ignored(ctx, path)
    if branch_exists(ctx.main_root, branch):
        git(ctx.main_root, "worktree", "add", str(path), branch)
    else:
        git(ctx.main_root, "worktree", "add", "-b", branch, str(path), ctx.main)
    if ctx.cfg["setup"]:
        r = subprocess.run(ctx.cfg["setup"], shell=True, cwd=path, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"warning: setup failed in {path}: {(r.stderr or r.stdout).strip()[-400:]}")
    return Path(real(path)), branch, True


def ensure_iwt(ctx: Ctx) -> Path:
    p = ctx.iwt
    for w in worktrees(ctx.main_root):
        if real(w["worktree"]) == real(p):
            return Path(real(p))
    if p.exists():
        raise Refused(f"{p} exists but isn't a worktree of this repo")
    ensure_ignored(ctx, p)
    git(ctx.main_root, "worktree", "add", "--detach", str(p), ctx.main)
    return Path(real(p))


def checkout_of(ctx: Ctx, branch: str) -> str | None:
    return branch_worktree(ctx, branch)


# ---------------------------------------------------------------- checks

def check_cmds(ctx: Ctx, files) -> list[str]:
    cmds = [ctx.cfg["check"]] if ctx.cfg["check"] else []
    for prefix, cmd in (ctx.cfg.get("verify") or {}).items():
        if any(covers(prefix, f) for f in files):
            cmds.append(cmd)
    return cmds


def run_checks(ctx: Ctx, cwd: Path, cmds: list[str], label: str) -> tuple[bool, Path, float, str]:
    tree = tree_of(cwd, "HEAD")
    logd = ctx.sd / "checks"
    logd.mkdir(parents=True, exist_ok=True)
    log = logd / f"{dt.datetime.now():%Y%m%d-%H%M%S}-{label}-{tree[:10]}.txt"
    t0, ok = time.time(), True
    with open(log, "w") as fh:
        for cmd in cmds:
            fh.write(f"$ {cmd}\n")
            fh.flush()
            r = subprocess.run(cmd, shell=True, cwd=str(cwd), stdout=fh, stderr=subprocess.STDOUT,
                               env={**os.environ, "INTEGRATOR_WORKTREE": str(cwd)})
            fh.write(f"[exit {r.returncode}]\n")
            fh.flush()
            if r.returncode != 0:
                ok = False
                break
    return ok, log, time.time() - t0, tree


def record_check(st: dict, tree: str, ok: bool, cmds: list[str], secs: float, log: Path, by: str) -> None:
    st["checks"][tree] = {"ok": ok, "cmds": cmds, "secs": round(secs), "log": str(log), "by": by, "t": now()}
    if len(st["checks"]) > 500:
        for k in sorted(st["checks"], key=lambda k: st["checks"][k]["t"])[:-500]:
            del st["checks"][k]


def conflict_hint(ctx: Ctx, files) -> None:
    """The repo's own advice for its usual conflicts (`conflict_hint` in .integrator.toml; {files} is filled in)."""
    h = ctx.cfg.get("conflict_hint") or ""
    if h:
        print("hint (this repo): " + h.replace("{files}", " ".join(files) or "<file>"))


def tail(path: Path, n: int = 20) -> str:
    try:
        return "\n".join(Path(path).read_text(errors="replace").splitlines()[-n:])
    except OSError:
        return ""


# ---------------------------------------------------------------- integrator commands

def cmd_start(ctx: Ctx, a) -> int:
    sid, pid = env_session()
    if sid and not a.session:
        raise Refused("pass your own session name, as ListAgents prints it (\"This session is <name>\"): "
                      "integrator start --session \"<name>\". Contributors message you by it.", USAGE)
    iwt = ensure_iwt(ctx)

    def fn(st):
        i = st.get("integrator")
        if i and integ_live(st) and i.get("session_id") != sid and not a.takeover:
            raise Refused(f"an integrator is already live here: '{i['session']}' (pid {i.get('pid')}). Message it, "
                          "or `integrator start --takeover` only if it is really gone.")
        same = bool(i and i.get("session_id") == sid)
        st["integrator"] = {"session": a.session or (i or {}).get("session"), "session_id": sid, "pid": pid,
                            "since": i["since"] if same and i else now(), "seen": now(), "worktree": str(iwt)}
        st["repo"], st["main"] = str(ctx.main_root), ctx.main
        emit(ctx, st, "start", "integrator", f"'{st['integrator']['session']}' integrates {ctx.main}")
        return st
    st = update(ctx, fn)
    dirty = dirty_files(ctx.main_root, untracked=False)
    print(f"INTEGRATOR STARTED for {ctx.main_root} ({ctx.main}@{short(rev(ctx.main_root, ctx.main))}) as "
          f"'{iname(st)}'. Integration worktree: {iwt}")
    if dirty:
        print(f"warning: the main checkout has uncommitted tracked edits ({', '.join(dirty[:6])}). Someone works "
              "there directly; landings that touch those files will stop until they commit or move to a worktree.")
    print("next: arm the watch (Monitor, command `integrator watch`, the longest timeout), then `integrator status`.")
    print("contributors: `integrator launch contributor <topic> \"<task>\"`, or tell a session: invoke the "
          "contributor skill.")
    return OK


def cmd_stop(ctx: Ctx, a) -> int:
    def fn(st):
        need_integrator(st)
        q = [s for s in st["subs"] if s["status"] == "queued"]
        if (q or st["landing"]) and not a.force:
            raise Refused(f"{len(q)} submissions wait{' and a landing is in progress' if st['landing'] else ''}: "
                          "land or bounce them first, or --force")
        st["integrator"] = None
        emit(ctx, st, "stop", "integrator", "the integrator stopped")
    update(ctx, fn)
    print("INTEGRATOR STOPPED. Contributors' worktrees and the queue stay as they are.")
    return OK


def fmt_event(rec: dict) -> str:
    return f"#{rec['n']} {rec['kind']} {rec['who']}: {rec['text']}"


def cmd_watch(ctx: Ctx, a) -> int:
    ctx.sd.mkdir(parents=True, exist_ok=True)
    p = ctx.sd / "events.jsonl"
    p.touch()
    pos = 0 if a.since is not None else p.stat().st_size
    kinds = None if a.all else WAKE
    print(f"watching {ctx.main_root.name}: {'every event' if a.all else ', '.join(sorted(WAKE))}", flush=True)
    buf, beat = "", 0.0
    while True:
        size = p.stat().st_size
        if size < pos:
            pos = 0
        if size > pos:
            with open(p) as fh:
                fh.seek(pos)
                buf += fh.read()
                pos = fh.tell()
            *lines, buf = buf.split("\n")
            for line in lines:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if a.since is not None and rec["n"] <= a.since:
                    continue
                if kinds is None or rec["kind"] in kinds:
                    print(fmt_event(rec), flush=True)
        if time.time() - beat > 60:  # keep the integrator's heartbeat fresh while it waits
            beat = time.time()
            with contextlib.suppress(Exception):
                def fn(st):
                    if am_integrator(st):
                        st["integrator"]["seen"] = now()
                update(ctx, fn)
        time.sleep(1)


def sub_by_id(st: dict, sid: int) -> dict:
    for s in st["subs"]:
        if s["id"] == sid:
            return s
    raise Refused(f"no submission #{sid}")


def parse_id(x: str) -> int:
    try:
        return int(str(x).lstrip("#"))
    except ValueError as e:
        raise Refused(f"not a submission id: {x}", USAGE) from e


def classify(ctx: Ctx, st: dict, sub: dict, tip: str) -> dict:
    """How far the contributor's own check covers what lands. Only overlap, resolved and unchecked need more."""
    chk = st["checks"].get(sub["tree"])
    passed = bool(chk and chk.get("ok"))
    if passed and tree_of(ctx.main_root, tip) == sub["tree"]:
        return {"risk": "exact", "why": "lands the very tree its check passed on"}
    files = set(sub["files"])
    if not passed:
        if all(ctx.trivial(f) for f in files):
            return {"risk": "trivial", "why": "docs only, no check needed"}
        return {"risk": "unchecked", "why": "no passing check (" + (sub.get("no_check") or "none recorded") + ")"}
    differ = sorted(files & set(names(ctx.main_root, sub["head"], tip)))
    if not differ:
        return {"risk": "disjoint", "why": "main moved, in other files only"}
    if all(ctx.trivial(f) for f in differ):
        return {"risk": "trivial", "why": "main also changed " + ", ".join(differ[:4]) + " (docs only)"}
    more = " …" if len(differ) > 6 else ""
    return {"risk": "overlap", "why": "main also changed " + ", ".join(differ[:6]) + more, "files": differ}


def save_landing(ctx: Ctx, landing: dict | None) -> None:
    def fn(st):
        st["landing"] = landing
    update(ctx, fn)


def cmd_land(ctx: Ctx, a) -> int:
    st = load(ctx)
    need_integrator(st)
    if a.abort:
        return land_abort(ctx, st)
    if a.cont:
        return land_continue(ctx, st, a)
    if st["landing"]:
        L = st["landing"]
        raise Refused(f"a landing is in progress (#{', #'.join(map(str, L['ids']))}: {L['state']}). Finish it with "
                      "`integrator land --continue [--verify|--trust WHY]` or drop it with `--abort`.")
    queued = [s for s in st["subs"] if s["status"] == "queued"]
    if not queued:
        print("NOTHING TO LAND: the queue is empty.")
        return OK
    want = a.ids or ["next"]
    if want == ["next"]:
        ids = [queued[0]["id"]]
    elif want == ["all"]:
        ids = [s["id"] for s in queued]
    else:
        ids = [parse_id(x) for x in want]
        for i in ids:
            if sub_by_id(st, i)["status"] != "queued":
                raise Refused(f"#{i} is {sub_by_id(st, i)['status']}, not queued")
    iwt = ensure_iwt(ctx)
    if in_progress(iwt):
        raise Refused(f"the integration worktree {iwt} has a {in_progress(iwt)} in progress outside any landing: "
                      f"finish or abort it (git -C {iwt} rebase --abort)")
    if dirty_files(iwt, untracked=False):
        raise Refused(f"the integration worktree {iwt} has edits outside any landing: clean it "
                      f"(git -C {iwt} checkout -f --detach {ctx.main})")
    base = rev(ctx.main_root, ctx.main)
    git(iwt, "switch", "--detach", base)
    landing = {"ids": ids, "base": base, "applied": [], "risk": {}, "state": "applying", "at": None,
               "merge": bool(a.merge), "t": now(), "conflict_files": []}
    save_landing(ctx, landing)
    return land_apply(ctx, iwt, landing, a)


def land_apply(ctx: Ctx, iwt: Path, landing: dict, a) -> int:
    st = load(ctx)
    for i in landing["ids"]:
        if i in landing["applied"]:
            continue
        sub = sub_by_id(st, i)
        c = st["contributors"].get(sub["topic"])
        pre = rev(iwt, "HEAD")
        upstream = replay_from(ctx, c, sub["head"], pre)
        if not landing["merge"] and git(iwt, "rev-list", "--merges", f"{upstream}..{sub['head']}"):
            run(["git", "switch", "--detach", landing["base"]], iwt)
            save_landing(ctx, None)
            raise Refused(f"#{i} contains merge commits: ask '{sub['topic']}' to `integrator sync` (a linear "
                          "branch), or land it with --merge")
        if landing["merge"]:
            r = run(["git", "merge", "--no-ff", "-m", f"Merge #{i} ({sub['branch']}): {sub['summary']}",
                     sub["head"]], iwt)
        else:
            r = run(["git", "rebase", "--onto", pre, upstream, sub["head"]], iwt)
        if r.returncode != 0:
            files = conflicted(iwt)
            if not files:
                if in_progress(iwt):
                    run(["git", "rebase" if in_progress(iwt) == "rebase" else "merge", "--abort"], iwt)
                run(["git", "switch", "--detach", landing["base"]], iwt)
                save_landing(ctx, None)
                raise Refused(f"replaying #{i} failed:\n{(r.stderr or r.stdout).strip()[-1200:]}")
            landing.update(state="conflict", at=i, conflict_files=files)
            save_landing(ctx, landing)

            def fn(st2):
                emit(ctx, st2, "conflict", "integrator", f"#{i} ({sub['topic']}) conflicts in {', '.join(files)}")
            update(ctx, fn)
            print(f"CONFLICT landing #{i} ({sub['topic']}: {sub['summary']}) in: {', '.join(files)}")
            conflict_hint(ctx, files)
            print(f"  resolve in {iwt}: keep both sides' intent (main's side: `git -C {iwt} log -p {pre} -3 -- <file>`;"
                  f" theirs: the submission summary and `git -C {iwt} log -p {sub['head']} -3 -- <file>`), `git add`,")
            print("  then `integrator land --continue` (a hand resolution needs `--verify` or `--trust WHY`), or "
                  "`integrator land --abort` and `integrator bounce` it to its author.")
            return CONFLICT
        landing["applied"].append(i)
        landing["risk"][str(i)] = classify(ctx, st, sub, rev(iwt, "HEAD"))
        save_landing(ctx, landing)
    return land_decide(ctx, iwt, landing, a)


def land_decide(ctx: Ctx, iwt: Path, landing: dict, a) -> int:
    needs = {i: r for i, r in landing["risk"].items() if r["risk"] in NEEDS}
    verified = "contributor"
    if a.verify:
        st = load(ctx)
        files = sorted({f for i in landing["applied"] for f in sub_by_id(st, i)["files"]})
        cmds = check_cmds(ctx, files)
        if not cmds:
            raise Refused("no check configured (`check` in .integrator.toml)")
        print(f"verifying in {iwt}: {' && '.join(cmds)}", flush=True)
        ok, log, secs, tree = run_checks(ctx, iwt, cmds, "integration")

        def fn(st2):
            record_check(st2, tree, ok, cmds, secs, log, "integrator")
        update(ctx, fn)
        if not ok:
            landing["state"] = "verify-failed"
            save_landing(ctx, landing)
            print(f"VERIFY FAILED ({secs:.0f} s) for #{', #'.join(map(str, landing['applied']))}. Log: {log}")
            print(tail(log))
            print("Fix it here and commit (then `land --continue --verify`), or `land --abort` and bounce the "
                  "submission that broke it.")
            return VERIFY_FAILED
        verified = f"integrator ({secs:.0f} s)"
        print(f"VERIFY OK ({secs:.0f} s)")
    elif needs and not a.trust:
        landing["state"] = "needs-verify"
        save_landing(ctx, landing)
        print("NEEDS-VERIFY: the contributors' own checks don't cover what would land:")
        for i, r in needs.items():
            print(f"  #{i}: {r['risk']}: {r['why']}")
        print("Choose: `integrator land --continue --verify` (run the checks in the integration worktree, once for "
              "the batch), `--continue --trust \"<why it's safe>\"`, or `--abort` and bounce it for a sync and "
              "re-check by its author.")
        return NEEDS_VERIFY
    elif needs:
        verified = f"trusted: {a.trust}"
    if not a.verify:  # quick per-path checks run on every landing that touches their paths, trivial ones too
        st = load(ctx)
        files = sorted({f for i in landing["applied"] for f in sub_by_id(st, i)["files"]})
        quick = [cmd for prefix, cmd in (ctx.cfg.get("quick") or {}).items() if any(covers(prefix, f) for f in files)]
        if quick:
            ok, log, secs, _ = run_checks(ctx, iwt, quick, "quick")
            if not ok:
                landing["state"] = "verify-failed"
                save_landing(ctx, landing)
                print(f"QUICK CHECK FAILED ({secs:.0f} s): {' && '.join(quick)}. Log: {log}")
                print(tail(log))
                print("Fix it here and commit (then `land --continue`), or `land --abort` and bounce it.")
                return VERIFY_FAILED
            verified += f", quick checks OK ({secs:.0f} s)"
    return land_finish(ctx, iwt, landing, verified)


def land_finish(ctx: Ctx, iwt: Path, landing: dict, verified: str) -> int:
    cur = rev(ctx.main_root, ctx.main)
    if cur != landing["base"]:  # someone committed to main outside the protocol: replay our result onto it
        print(f"main moved outside the protocol ({short(landing['base'])} → {short(cur)}); replaying onto it")
        r = run(["git", "rebase", "--onto", cur, landing["base"]], iwt)
        if r.returncode != 0:
            landing.update(state="conflict", at=None, conflict_files=conflicted(iwt), base=cur)
            save_landing(ctx, landing)
            print(f"CONFLICT with main's new commits in: {', '.join(conflicted(iwt))}. Resolve in {iwt}, `git add`, "
                  "then `integrator land --continue --verify`.")
            return CONFLICT
        landing["base"] = cur
        save_landing(ctx, landing)
    tip = rev(iwt, "HEAD")
    where = checkout_of(ctx, ctx.main)
    if where:
        r = run(["git", "merge", "--ff-only", tip], where)
        if r.returncode != 0:
            landing["state"] = "ff-blocked"
            save_landing(ctx, landing)
            print(f"BLOCKED: {ctx.main} is checked out in {where} and git won't fast-forward it there:")
            print((r.stderr or r.stdout).strip()[-800:])
            print("Usually uncommitted edits in that checkout touch the same files: ask whoever made them to commit "
                  "or move to a worktree, then `integrator land --continue`.")
            return REFUSED
    else:
        git(ctx.main_root, "update-ref", f"refs/heads/{ctx.main}", tip, landing["base"])

    # Who else must hear about it: contributors whose branch, uncommitted edits or claims touch what landed.
    st0 = load(ctx)
    landed_files: set[str] = set()
    for i in landing["applied"]:
        landed_files |= set(sub_by_id(st0, i)["files"])
    submitters = {sub_by_id(st0, i)["topic"] for i in landing["applied"]}
    touched: dict[str, list[str]] = {}
    for t, c in st0["contributors"].items():
        if t in submitters or c.get("status") == "left" or not branch_exists(ctx.main_root, c["branch"]):
            continue
        head = rev(ctx.main_root, c["branch"])
        mine = set(dirty_files(c["worktree"]))
        mb = merge_base(ctx.main_root, head, landing["base"])
        mine |= set(names(ctx.main_root, mb, head))
        hit = sorted(f for f in landed_files if f in mine or any(
            k["topic"] == t and covers(k["path"], f) for k in st0["claims"]))
        if hit:
            touched[t] = hit

    def fn(st):
        lines = []
        for i in landing["applied"]:
            sub = sub_by_id(st, i)
            risk = landing["risk"][str(i)]["risk"]
            rebased = not is_ancestor(ctx.main_root, sub["head"], tip)
            sub["status"] = "landed"
            sub["landed"] = {"tip": tip, "t": now(), "risk": risk, "verified": verified, "rebased": rebased}
            c = st["contributors"].get(sub["topic"])
            if c:
                c["landed_upto"] = sub["head"]
                if rebased:
                    c["sync"] = {"reason": f"#{i} landed rebased", "t": now()}
            notify(st, sub["topic"], f"#{i} landed on {ctx.main} (main is now {short(tip)}, {risk})."
                   + (" It was rebased: run `integrator sync` before you submit again." if rebased else ""))
            lines.append((sub, risk, rebased))
        for t, hit in touched.items():
            c = st["contributors"].get(t)
            if c is None:
                continue
            c["sync"] = {"reason": "main changed " + ", ".join(hit[:5]), "t": now()}
            notify(st, t, f"main moved: {', '.join(hit[:5])} changed (landed #{', #'.join(map(str, landing['applied']))})"
                   ". Run `integrator sync` at your next stopping point, before you go on in those files.")
        st["landing"] = None
        emit(ctx, st, "land", "integrator", f"#{', #'.join(map(str, landing['applied']))} → {ctx.main}@{short(tip)} "
             f"({verified})")
        return lines
    lines = update(ctx, fn)
    print(f"LANDED on {ctx.main} → {short(tip)} ({verified})")
    st = load(ctx)
    for sub, risk, rebased in lines:
        c = st["contributors"].get(sub["topic"], {})
        print(f"  #{sub['id']} {sub['topic']}: {sub['summary']} [{risk}{', rebased' if rebased else ''}]")
        print(f"    tell: SendMessage to '{c.get('session') or sub['topic']}': \"#{sub['id']} landed as "
              f"{short(tip)}.{' It was rebased: run `integrator sync` before your next submit.' if rebased else ''}\"")
    for t, hit in touched.items():
        c = st["contributors"].get(t, {})
        print(f"  tell: SendMessage to '{c.get('session') or t}': \"main moved under you: {', '.join(hit[:5])} "
              "changed. Run `integrator sync` at your next stopping point.\"")
    return OK


def land_continue(ctx: Ctx, st: dict, a) -> int:
    landing = st["landing"]
    if not landing:
        raise Refused("no landing in progress")
    iwt = ensure_iwt(ctx)
    if landing["state"] == "conflict":
        left = conflicted(iwt)
        if left:
            raise Refused(f"still conflicted: {', '.join(left)}. Resolve them in {iwt} (edit, `git add`), then "
                          "`integrator land --continue` again.")
        kind = in_progress(iwt)
        if kind == "rebase":
            r = run(["git", "-c", "core.editor=true", "rebase", "--continue"], iwt)
            if r.returncode != 0:
                files = conflicted(iwt)
                if files:
                    landing["conflict_files"] = sorted(set(landing["conflict_files"]) | set(files))
                    save_landing(ctx, landing)
                    print(f"CONFLICT again (a later commit of #{landing['at']}) in: {', '.join(files)}. Resolve, "
                          "`git add`, `integrator land --continue`.")
                    conflict_hint(ctx, files)
                    return CONFLICT
                raise Refused((r.stderr or r.stdout).strip()[-1200:])
        elif kind == "merge":
            git(iwt, "commit", "--no-edit")
        if landing.get("at") is not None:
            i = landing["at"]
            landing["applied"].append(i)
            landing["risk"][str(i)] = {"risk": "resolved", "why": "conflicts resolved by the integrator in "
                                       + ", ".join(landing["conflict_files"]), "files": landing["conflict_files"]}
        landing.update(state="applying", at=None)
        save_landing(ctx, landing)
        if landing.get("base") and rev(ctx.main_root, ctx.main) != landing["base"]:
            pass  # land_finish replays onto the new main
        return land_apply(ctx, iwt, landing, a)
    if landing["state"] == "ff-blocked":
        return land_finish(ctx, iwt, landing, f"trusted: {a.trust}" if a.trust else "as decided before")
    return land_decide(ctx, iwt, landing, a)


def land_abort(ctx: Ctx, st: dict) -> int:
    landing = st["landing"]
    iwt = ensure_iwt(ctx)
    kind = in_progress(iwt)
    if kind:
        run(["git", kind, "--abort"], iwt)
    run(["git", "checkout", "-f", "--detach", ctx.main], iwt)
    save_landing(ctx, None)
    ids = ", #".join(map(str, landing["ids"])) if landing else ""
    print(f"ABORTED{' the landing of #' + ids if ids else ' (nothing was in progress)'}; the submissions stay queued. "
          "Bounce one with `integrator bounce ID \"<why>\"` if its author should take it from here.")
    return OK


def cmd_bounce(ctx: Ctx, a) -> int:
    i = parse_id(a.id)

    def fn(st):
        need_integrator(st)
        if st["landing"] and i in st["landing"]["ids"]:
            raise Refused(f"#{i} is in the landing in progress: `integrator land --abort` first")
        sub = sub_by_id(st, i)
        if sub["status"] != "queued":
            raise Refused(f"#{i} is {sub['status']}")
        sub.update(status="bounced", reason=a.why, bounced=now())
        notify(st, sub["topic"], f"#{i} came back from the integrator: {a.why}. Fix it, `integrator check`, "
               "`integrator submit` again.")
        emit(ctx, st, "bounce", "integrator", f"#{i} ({sub['topic']}): {a.why}")
        return sub, st["contributors"].get(sub["topic"], {})
    sub, c = update(ctx, fn)
    print(f"BOUNCED #{i} to {sub['topic']}.")
    print(f"  tell: SendMessage to '{c.get('session') or sub['topic']}': \"#{i} bounced: {a.why}\"")
    return OK


def cmd_tell(ctx: Ctx, a) -> int:
    def fn(st):
        need_integrator(st)
        to = list(st["contributors"]) if a.topic == "all" else [a.topic]
        for t in to:
            if t not in st["contributors"]:
                raise Refused(f"no contributor {t}")
            notify(st, t, a.text)
        return to
    to = update(ctx, fn)
    print(f"TOLD {', '.join(to)} (they see it at their next edit or `integrator status --me`).")
    return OK


def cmd_grant(ctx: Ctx, a) -> int:
    path = resolve_path(ctx, a.path, ctx.main_root)

    def fn(st):
        need_integrator(st)
        if a.topic not in st["contributors"]:
            raise Refused(f"no contributor {a.topic}")
        st["seq"]["claim"] += 1
        st["claims"].append({"id": st["seq"]["claim"], "topic": a.topic, "path": path, "mode": "granted",
                             "note": a.note or "granted by the integrator", "since": now()})
        holders = sorted({k["topic"] for k in st["claims"] if k["topic"] != a.topic and claims_overlap(k["path"], path)})
        notify(st, a.topic, f"the integrator granted you {path}" + (f" (also claimed by {', '.join(holders)}: keep the "
               "change small and submit it soon)" if holders else ""))
        for h in holders:
            notify(st, h, f"the integrator let {a.topic} edit {path}, which you claimed: sync after it lands.")
        emit(ctx, st, "grant", "integrator", f"{a.topic} may edit {path}")
        return holders
    holders = update(ctx, fn)
    print(f"GRANTED {path} to {a.topic}" + (f" (overlaps {', '.join(holders)}, told)" if holders else ""))
    return OK


def fully_landed(ctx: Ctx, c: dict, main_tip: str) -> bool:
    if not branch_exists(ctx.main_root, c["branch"]):
        return True
    head = rev(ctx.main_root, c["branch"])
    if c.get("landed_upto") == head or is_ancestor(ctx.main_root, head, main_tip):
        return True
    return not any(x.startswith("+") for x in git(ctx.main_root, "cherry", main_tip, head).splitlines())


def cmd_prune(ctx: Ctx, a) -> int:
    st = load(ctx)
    need_integrator(st)
    tip = rev(ctx.main_root, ctx.main)
    removed, kept = [], []
    for t, c in list(st["contributors"].items()):
        gone = c.get("status") == "left" or (c.get("pid") and not alive(c["pid"]))
        if not gone:
            continue
        if dirty_files(c["worktree"]) or not fully_landed(ctx, c, tip) or \
                any(s["topic"] == t and s["status"] == "queued" for s in st["subs"]):
            kept.append(f"{t} (unlanded or uncommitted work: never deleted; ask the owner)")
            continue
        if Path(c["worktree"]).exists():
            r = run(["git", "worktree", "remove", c["worktree"]], ctx.main_root)
            if r.returncode != 0:
                kept.append(f"{t} ({(r.stderr or '').strip()[:120]})")
                continue
        if branch_exists(ctx.main_root, c["branch"]):
            run(["git", "branch", "-D", c["branch"]], ctx.main_root)
        removed.append(t)

        def fn(st2, t=t):
            st2["contributors"].pop(t, None)
            st2["claims"] = [k for k in st2["claims"] if k["topic"] != t]
            emit(ctx, st2, "prune", "integrator", f"removed {t}'s worktree (all landed)")
        update(ctx, fn)
    print(f"PRUNED {', '.join(removed) or 'nothing'}" + ("; kept " + "; ".join(kept) if kept else ""))
    return OK


# ---------------------------------------------------------------- contributor commands

def cmd_join(ctx: Ctx, a) -> int:
    topic = topic_ok(a.topic)
    sid, pid = env_session()
    created = False
    if ctx.top == ctx.main_root:
        wt, branch, created = make_worktree(ctx, topic)
    else:
        wt = ctx.top
        if real(wt) == real(ctx.iwt):
            raise Refused("this is the integrator's own worktree")
        b = run(["git", "symbolic-ref", "-q", "--short", "HEAD"], wt).stdout.strip()
        if not b:
            branch = ctx.cfg["branch_prefix"] + topic
            git(wt, "switch", "-c", branch)
        else:
            branch = b
        if branch == ctx.main:
            raise Refused(f"this worktree has {ctx.main} checked out: contributors work on their own branch")

    def fn(st):
        old = st["contributors"].get(topic)
        if old and real(old["worktree"]) != real(wt):
            raise Refused(f"topic {topic} belongs to the worktree {old['worktree']}: pick another topic")
        for t, c in st["contributors"].items():
            if t != topic and real(c["worktree"]) == real(wt):
                raise Refused(f"this worktree is registered as {t}: `integrator join {t}`")
        if old and old.get("session_id") and sid and old["session_id"] != sid and alive(old.get("pid")):
            raise Refused(f"a live session ('{old.get('session')}') already works in {wt}: two sessions in one "
                          "worktree overwrite each other. Pick another topic.")
        c = old or {"topic": topic, "joined": now(), "landed_upto": None, "sync": None}
        c.update(worktree=str(wt), branch=branch, status="active", seen=now(),
                 task=a.task or c.get("task") or "", session=a.session or c.get("session") or "",
                 session_id=sid or c.get("session_id"), pid=pid or c.get("pid"))
        st["contributors"][topic] = c
        emit(ctx, st, "join", topic, f"{branch} in {wt}" + (f": {c['task']}" if c["task"] else "")
             + (f" (session '{c['session']}')" if c["session"] else ""))
        return c, integ_live(st), iname(st)
    c, live, name = update(ctx, fn)
    print(f"JOINED as {topic}: worktree {wt} on {branch}{' (new, from ' + ctx.main + ')' if created else ''}.")
    if sid and not c["session"]:
        print("warning: no --session: the integrator can't message you. Rerun with --session \"<your ListAgents name>\".")
    if live:
        print(f"integrator: '{name}' (SendMessage to it; it lands your submissions).")
    else:
        print("warning: no live integrator yet. You can claim, check and submit; your submissions wait in the queue.")
    if created and str(ctx.top) == str(ctx.main_root):
        print(f"next: move your session into it (Claude Code: EnterWorktree with path {wt}) and edit only there.")
    print("then: `integrator claim <paths you'll change>`, work in small commits, `integrator check`, `integrator submit`.")
    return OK


def cmd_scope(ctx: Ctx, a) -> int:
    """Put this contributor's scope on the board: what it will change, which interfaces others use, which shared
    resources it needs. The integrator wakes on it and looks for neighbours; the others see it in `status`."""
    text = " ".join(a.text.split())
    if not text:
        raise Refused("say what you'll change: `integrator scope \"<paths; interfaces others use; shared resources>\"`",
                      USAGE)

    def fn(st):
        topic, c = need_me(ctx, st)
        c.update(scope=text, scope_t=now())
        emit(ctx, st, "scope", topic, text)
        others = [(t, o) for t, o in st["contributors"].items() if t != topic and o.get("status") != "left"]
        return topic, others, iname(st), integ_live(st)
    topic, others, name, live = update(ctx, fn)
    print(f"SCOPE for {topic} is on the board" + (f"; '{name}' wakes on it." if live else " (no live integrator yet)."))
    if others:
        print("Also working now. If your work meets one of theirs (a file, an interface, one feature from two sides, "
              "a shared resource), message its session directly to agree the seam, then tell the integrator what you "
              "agreed in one line:")
        for t, o in others:
            what = o.get("scope") or o.get("task") or "(no scope yet)"
            print(f"  {t} ('{o.get('session') or '?'}'): {what}")
    print("Claim the paths too (`integrator claim`), and run `integrator scope` again if it changes much.")
    return OK


def cmd_claim(ctx: Ctx, a) -> int:
    paths = [resolve_path(ctx, p) for p in a.paths]

    def fn(st):
        if a.for_topic:
            need_integrator(st)
            topic = a.for_topic
            if topic not in st["contributors"]:
                raise Refused(f"no contributor {topic}")
        else:
            topic, _ = need_me(ctx, st)
        got, refused = [], []
        for p in paths:
            mode = "shared" if a.shared or ctx.shared_path(p) else "exclusive"
            clash = [k for k in st["claims"] if k["topic"] != topic and claims_overlap(k["path"], p)
                     and (mode == "exclusive" or k["mode"] == "exclusive")]
            if clash:
                refused.append((p, clash))
                continue
            st["claims"] = [k for k in st["claims"] if not (k["topic"] == topic and k["path"] == p)]
            st["seq"]["claim"] += 1
            st["claims"].append({"id": st["seq"]["claim"], "topic": topic, "path": p, "mode": mode,
                                 "note": a.note or "", "since": now()})
            for k in st["claims"]:
                if k["topic"] != topic and claims_overlap(k["path"], p):
                    notify(st, k["topic"], f"{topic} shares {p} with you ({mode}). Keep edits there small.", topic)
            got.append((p, mode))
        for p, clash in refused:
            emit(ctx, st, "claim-refused", topic, f"wants {p}, held by " + ", ".join(
                f"{k['topic']} ({k['mode']} {k['path']})" for k in clash))
        if got:
            emit(ctx, st, "claim", topic, ", ".join(f"{p} ({m})" for p, m in got))
        return topic, got, refused, iname(st)
    topic, got, refused, name = update(ctx, fn)
    for p, m in got:
        print(f"CLAIMED {p} ({m}) for {topic}")
    for p, clash in refused:
        who = ", ".join(f"{k['topic']} ({k['mode']} {k['path']}{': ' + k['note'] if k['note'] else ''})" for k in clash)
        print(f"REFUSED {p}: held by {who}. Don't edit around it: ask the integrator ('{name}') to arbitrate (it can "
              "split the files, order the work or `grant` you the path), and work elsewhere meanwhile.")
    return REFUSED if refused else OK


def cmd_release(ctx: Ctx, a) -> int:
    def fn(st):
        if a.for_topic:
            need_integrator(st)
            topic = a.for_topic
        else:
            topic, _ = need_me(ctx, st)
        want = {resolve_path(ctx, p) for p in a.paths}
        before = len(st["claims"])
        st["claims"] = [k for k in st["claims"] if not (k["topic"] == topic and (a.all or k["path"] in want))]
        n = before - len(st["claims"])
        if n:
            emit(ctx, st, "release", topic, f"released {n} claims")
        return n
    print(f"RELEASED {update(ctx, fn)} claims.")
    return OK


def cmd_check(ctx: Ctx, a) -> int:
    st = load(ctx)
    topic, c = find_me(ctx, st)
    wt = ctx.top
    tracked = dirty_files(wt, untracked=False)
    if tracked:
        raise Refused(f"commit first: the check is kept for a commit's tree, and these files differ from HEAD: "
                      f"{', '.join(tracked[:8])}")
    untracked = dirty_files(wt)
    if untracked:
        print(f"warning: untracked files take part in this check but won't land: {', '.join(untracked[:6])}")
    tip = rev(ctx.main_root, ctx.main)
    head = rev(wt, "HEAD")
    files = names(ctx.main_root, replay_from(ctx, c, head, tip), head)
    cmds = [a.check_cmd] if a.check_cmd else check_cmds(ctx, files)
    if not cmds:
        raise Refused("no check configured: set `check` in .integrator.toml, or pass --cmd \"<command>\"")
    print(f"checking {short(head)} in {wt}: {' && '.join(cmds)}", flush=True)
    ok, log, secs, tree = run_checks(ctx, wt, cmds, topic or "check")

    def fn(st2):
        record_check(st2, tree, ok, cmds, secs, log, topic or "?")
        if topic and topic in st2["contributors"]:
            st2["contributors"][topic]["seen"] = now()
    update(ctx, fn)
    if ok:
        print(f"CHECK PASS ({secs:.0f} s, tree {tree[:10]}). Log: {log}")
        return OK
    print(f"CHECK FAIL ({secs:.0f} s). Log: {log}\n{tail(log)}")
    return REFUSED


def cmd_submit(ctx: Ctx, a) -> int:
    st = load(ctx)
    topic, c = need_me(ctx, st)
    wt = Path(c["worktree"])
    if in_progress(wt):
        raise Refused(f"a {in_progress(wt)} is in progress here: finish it first")
    tracked = dirty_files(wt, untracked=False)
    if tracked:
        raise Refused(f"commit first (or stash): {', '.join(tracked[:8])}")
    untracked = dirty_files(wt)
    head, tree = rev(wt, "HEAD"), tree_of(wt, "HEAD")
    tip = rev(ctx.main_root, ctx.main)
    up = replay_from(ctx, c, head, tip)
    n = int(git(wt, "rev-list", "--count", f"{up}..{head}"))
    if n == 0:
        raise Refused(f"nothing to submit: no commits of yours beyond {ctx.main}")
    files = names(ctx.main_root, up, head)
    chk = st["checks"].get(tree)
    if not a.no_check:
        if not chk:
            raise Refused("no check on this commit: run `integrator check` first (or `--no-check \"<why>\"`, for a "
                          "docs-only change)")
        if not chk["ok"]:
            raise Refused(f"the check failed on this commit (log {chk['log']}): fix it, commit, check again")
    pred = predict(ctx.main_root, tip, head, base=up)
    pred_txt = "clean" if pred == [] else ("unknown" if pred is None else "conflicts with main in " + ", ".join(pred))
    foreign = sorted({f"{f} ({k['topic']})" for f in files for k in st["claims"]
                      if k["topic"] != topic and k["mode"] == "exclusive" and covers(k["path"], f)})
    summary = a.summary or git(wt, "log", "-1", "--format=%s", head)

    def fn(st2):
        st2["seq"]["sub"] += 1
        i = st2["seq"]["sub"]
        for s in st2["subs"]:
            if s["topic"] == topic and s["status"] == "queued":
                s.update(status="superseded", by=i)
        st2["subs"].append({"id": i, "topic": topic, "branch": c["branch"], "head": head, "tree": tree, "base": up,
                            "files": files, "commits": n, "summary": summary, "no_check": a.no_check or None,
                            "status": "queued", "t": now(), "predicted": pred_txt})
        cc = st2["contributors"][topic]
        cc["seen"], cc["sync"] = now(), cc.get("sync")
        emit(ctx, st2, "submit", topic, f"#{i}: {summary} ({n} commits, {len(files)} files; {pred_txt})"
             + (f"; touches others' claims: {', '.join(foreign)}" if foreign else ""))
        return i, integ_live(st2), iname(st2)
    i, live, name = update(ctx, fn)
    print(f"SUBMITTED #{i}: {summary} ({n} commits, {len(files)} files, check "
          f"{'skipped: ' + a.no_check if a.no_check else 'PASS'}). Predicted landing: {pred_txt}.")
    if untracked:
        print(f"note: untracked files stay behind: {', '.join(untracked[:6])}")
    if foreign:
        print(f"warning: it touches paths others claimed: {', '.join(foreign)}. The integrator decides.")
    if live:
        print(f"tell the integrator: SendMessage to '{name}': \"submitted #{i}: {summary}\" (its watch has it too).")
    else:
        print("no live integrator: #{i} waits in the queue.".replace("{i}", str(i)))
    print("Carry on with your next unit; the integrator tells you when it lands or if it needs you.")
    return OK


def cmd_sync(ctx: Ctx, a) -> int:
    st = load(ctx)
    topic, c = need_me(ctx, st)
    wt = Path(c["worktree"])
    queued = [s for s in st["subs"] if s["topic"] == topic and s["status"] == "queued"]
    if queued and not a.withdraw:
        raise Refused(f"your #{queued[-1]['id']} waits in the queue: syncing now would rewrite what it points at. "
                      "Let it land first, or `integrator sync --withdraw` and submit again after the sync.")
    tip = rev(ctx.main_root, ctx.main)
    kind = in_progress(wt)
    if kind == "rebase":
        left = conflicted(wt)
        if left:
            raise Refused(f"still conflicted: {', '.join(left)}. Resolve (edit, `git add`), then `integrator sync`.")
        r = run(["git", "-c", "core.editor=true", "rebase", "--continue"], wt)
        if r.returncode != 0:
            print(f"SYNC CONFLICT again in: {', '.join(conflicted(wt)) or '?'}. Resolve, `git add`, `integrator sync`.")
            conflict_hint(ctx, conflicted(wt))
            return CONFLICT
    elif kind:
        raise Refused(f"a {kind} is in progress here: finish or abort it first")
    else:
        head = rev(wt, "HEAD")
        up = replay_from(ctx, c, head, tip)
        if up == tip or (is_ancestor(wt, tip, head) and up == merge_base(wt, head, tip)):
            def fn0(st2):
                st2["contributors"][topic]["sync"] = None
            update(ctx, fn0)
            print(f"UP TO DATE with {ctx.main}@{short(tip)}.")
            return OK
        r = run(["git", "rebase", "--autostash", "--onto", tip, up], wt)
        if r.returncode != 0:
            files = conflicted(wt)

            def fn1(st2):
                emit(ctx, st2, "sync-conflict", topic, "syncing conflicts in " + ", ".join(files))
            update(ctx, fn1)
            print(f"SYNC CONFLICT in: {', '.join(files) or (r.stderr or '').strip()[-300:]}")
            conflict_hint(ctx, files)
            print(f"Resolve here, keeping both intents (main's side: `git log -p {short(tip)} -3 -- <file>`), "
                  "`git add`, then `integrator sync` again. Unsure what the other side meant? Ask the integrator.")
            return CONFLICT
    n = int(git(wt, "rev-list", "--count", f"{tip}..HEAD"))

    def fn(st2):
        cc = st2["contributors"][topic]
        cc.update(landed_upto=None, sync=None, seen=now())
        for s in st2["subs"]:
            if s["topic"] == topic and s["status"] == "queued":
                s.update(status="withdrawn", reason="synced")
                emit(ctx, st2, "withdraw", topic, f"#{s['id']} withdrawn to sync")
        emit(ctx, st2, "sync", topic, f"onto {ctx.main}@{short(tip)}, {n} commits on top")
    update(ctx, fn)
    print(f"SYNCED onto {ctx.main}@{short(tip)}: {n} commits of yours on top. Run `integrator check` again before "
          "your next submit (the tree changed).")
    return OK


def cmd_ask(ctx: Ctx, a) -> int:
    def fn(st):
        topic, _ = find_me(ctx, st)
        emit(ctx, st, "ask", topic or "someone", a.text)
        return iname(st), integ_live(st)
    name, live = update(ctx, fn)
    print(f"ASKED ({'its watch wakes it' if live else 'no live integrator: it waits'}). For a conversation, "
          f"SendMessage to '{name}'.")
    return OK


def cmd_notices(ctx: Ctx, a) -> int:
    def fn(st):
        topic, _ = need_me(ctx, st)
        return take_notices(st, topic)
    out = update(ctx, fn)
    for n in out:
        print(f"[{ago(n['t'])}] {n['from']}: {n['text']}")
    if not out:
        print("no unread notices")
    return OK


def cmd_leave(ctx: Ctx, a) -> int:
    st = load(ctx)
    topic, c = need_me(ctx, st)
    tip = rev(ctx.main_root, ctx.main)
    dirty = dirty_files(c["worktree"], untracked=False)
    landed = fully_landed(ctx, c, tip)
    queued = [s for s in st["subs"] if s["topic"] == topic and s["status"] == "queued"]
    if (dirty or not landed or queued) and not a.abandon:
        why = []
        if dirty:
            why.append(f"uncommitted edits ({', '.join(dirty[:5])})")
        if queued:
            why.append(f"#{queued[-1]['id']} still queued")
        elif not landed:
            why.append("commits that haven't landed (submit them)")
        raise Refused("not yet: " + "; ".join(why) + ". Or `integrator leave --abandon \"<why>\"` (the work stays in the "
                      "worktree; nothing is deleted).")

    def fn(st2):
        cc = st2["contributors"][topic]
        cc.update(status="left", left=now(), abandon=a.abandon or None)
        st2["claims"] = [k for k in st2["claims"] if k["topic"] != topic]
        for s in st2["subs"]:
            if s["topic"] == topic and s["status"] == "queued":
                s.update(status="withdrawn", reason=a.abandon)
        emit(ctx, st2, "leave", topic, "all landed" if landed and not dirty else f"abandoned: {a.abandon}")
    update(ctx, fn)
    print(f"LEFT. Claims released. The integrator's `integrator prune` removes {c['worktree']} once you're out of it "
          "(Claude Code: ExitWorktree with action \"keep\").")
    if landed and not dirty and not a.abandon:
        # The flag closes a session, not a topic: while the same session still works on another topic, closing it
        # would lose that work.
        sid = c.get("session_id")
        same = [t for t, o in st["contributors"].items() if t != topic and sid and o.get("session_id") == sid]
        still = [t for t in same if st["contributors"][t].get("status") != "left"]
        if still:
            print(f"Your session still works on {', '.join(still)}: don't send the 🏁 line yet. It comes when "
                  f"{'that leaves' if len(still) == 1 else 'they leave'} too.")
            return OK
        topics = [t for t in same if not st["contributors"][t].get("abandon")] + [topic]
        mine = [s for s in st["subs"] if s["topic"] in topics and s["status"] == "landed"]
        ids = ", ".join(f"#{s['id']} → {short((s.get('landed') or {}).get('tip'))}" for s in mine) or "nothing to land"
        print("Once you've stopped everything you started (servers, simulators, locks, background checks), end your "
              "last message to the user with this line on its own, so they know this session can be closed:")
        print(f"🏁 SAFE TO CLOSE: {', '.join(topics)}, all landed ({ids})")
    return OK


# ---------------------------------------------------------------- both

def cmd_status(ctx: Ctx, a) -> int:
    st = load(ctx)
    if a.json:
        print(json.dumps(st, indent=1))
        return OK
    tip = rev(ctx.main_root, ctx.main)
    i = st.get("integrator")
    if a.me:
        return status_me(ctx, st, tip)
    print(f"integrator board for {ctx.main_root}: {ctx.main}@{short(tip)}")
    if i:
        live = "live" if integ_live(st) else "NOT RUNNING (its session ended)"
        print(f"  integrator: '{i['session']}' {live} (pid {i.get('pid')}), since {ago(i['since'])}, "
              f"worktree {i.get('worktree')}")
    else:
        print("  integrator: none. Start one: `integrator launch integrator`, or the integrator skill in a session.")
    dirty = dirty_files(ctx.main_root, untracked=False)
    if dirty:
        print(f"  main checkout: uncommitted tracked edits: {', '.join(dirty[:6])}")
    L = st["landing"]
    if L:
        print(f"  landing #{', #'.join(map(str, L['ids']))}: {L['state']}"
              + (f" (conflict in {', '.join(L['conflict_files'])})" if L["state"] == "conflict" else ""))
    q = [s for s in st["subs"] if s["status"] == "queued"]
    print(f"queue ({len(q)}):")
    for s in q:
        chk = st["checks"].get(s["tree"])
        c = st["contributors"].get(s["topic"])
        up = replay_from(ctx, c, s["head"], tip)
        p = predict(ctx.main_root, tip, s["head"], base=up)
        ptxt = "clean" if p == [] else ("?" if p is None else "CONFLICT in " + ", ".join(p))
        ctxt = ("skipped: " + s["no_check"]) if s.get("no_check") else (
            f"PASS ({chk['secs']} s)" if chk and chk["ok"] else "FAIL" if chk else "none")
        print(f"  #{s['id']} {s['topic']}: {s['summary']} ({s['commits']} commits, {len(s['files'])} files) "
              f"check {ctxt}, {ago(s['t'])} → {ptxt}")
    print(f"contributors ({sum(1 for c in st['contributors'].values() if c.get('status') != 'left')}):")
    for t, c in st["contributors"].items():
        if c.get("status") == "left":
            print(f"  {t}: left {ago(c.get('left'))}; `integrator prune` tears its worktree down")
            continue
        state = "live" if alive(c.get("pid")) else ("no session yet" if not c.get("pid") else "SESSION GONE")
        info = ""
        if branch_exists(ctx.main_root, c["branch"]):
            head = rev(ctx.main_root, c["branch"])
            own = sum(1 for x in git(ctx.main_root, "cherry", tip, head).splitlines() if x.startswith("+"))
            behind = int(git(ctx.main_root, "rev-list", "--count", f"{head}..{tip}"))
            info = f"{own} unlanded, {behind} behind, {len(dirty_files(c['worktree']))} uncommitted"
        cl = ", ".join(f"{k['path']}({k['mode'][0]})" for k in st["claims"] if k["topic"] == t and k["mode"] != "auto")
        auto = sum(1 for k in st["claims"] if k["topic"] == t and k["mode"] == "auto")
        sync = f"  SYNC NEEDED ({c['sync']['reason']})" if c.get("sync") else ""
        print(f"  {t}: {c['branch']} {state}, {info}; session '{c.get('session') or '?'}'; claims {cl or '-'}"
              f"{f' +{auto} edited' if auto else ''}{sync}")
        if c.get("task"):
            print(f"      task: {c['task']}")
        if c.get("scope"):
            print(f"      scope ({ago(c.get('scope_t'))}): {c['scope']}")
    recent = [s for s in st["subs"] if s["status"] in ("landed", "bounced")][-4:]
    if recent:
        print("recent: " + "; ".join(f"#{s['id']} {s['topic']} {s['status']} {ago((s.get('landed') or {}).get('t') or s.get('bounced'))}"
                                    for s in recent))
    return OK


def status_me(ctx: Ctx, st: dict, tip: str) -> int:
    topic, c = need_me(ctx, st)
    print(f"you: {topic}, {c['branch']} in {c['worktree']} (session '{c.get('session') or '?'}')")
    if integ_live(st):
        print(f"integrator: '{iname(st)}' live: SendMessage to it")
    else:
        print("integrator: not running; your submissions wait")
    head = rev(ctx.main_root, c["branch"])
    up = replay_from(ctx, c, head, tip)
    own = int(git(ctx.main_root, "rev-list", "--count", f"{up}..{head}"))
    behind = int(git(ctx.main_root, "rev-list", "--count", f"{head}..{tip}"))
    print(f"branch: {own} commits to land, {behind} behind {ctx.main}, uncommitted: "
          f"{', '.join(dirty_files(c['worktree'])[:6]) or 'none'}")
    mine = [k for k in st["claims"] if k["topic"] == topic]
    print("claims: " + (", ".join(f"{k['path']} ({k['mode']})" for k in mine) or "none"))
    for s in st["subs"]:
        if s["topic"] == topic and s["status"] in ("queued", "bounced"):
            print(f"#{s['id']} {s['status']}: {s['summary']}" + (f" ({s.get('reason')})" if s["status"] == "bounced" else ""))
    if c.get("sync"):
        print(f"SYNC NEEDED: {c['sync']['reason']} → `integrator sync`")

    def fn(st2):
        return take_notices(st2, topic)
    for n in update(ctx, fn):
        print(f"notice [{ago(n['t'])}] {n['from']}: {n['text']}")
    return OK


def cmd_conflicts(ctx: Ctx, a) -> int:
    st = load(ctx)
    tip = rev(ctx.main_root, ctx.main)
    act = []
    for t, c in st["contributors"].items():
        if c.get("status") == "left" or not branch_exists(ctx.main_root, c["branch"]):
            continue
        head = rev(ctx.main_root, c["branch"])
        up = replay_from(ctx, c, head, tip)
        act.append((t, c, head, up, set(names(ctx.main_root, up, head)), set(dirty_files(c["worktree"]))))
    found: list[tuple[str, str]] = []
    for t, c, head, up, files, dirty in act:
        p = predict(ctx.main_root, tip, head, base=up) if files else []
        if p:
            found.append(("CONFLICT", f"{t} × {ctx.main}: {', '.join(p)} (the integrator resolves it at landing; "
                                      f"syncing now keeps it small)"))
        hot = sorted(dirty & set(names(ctx.main_root, merge_base(ctx.main_root, head, tip), tip)))
        if hot:
            found.append(("OVERLAP", f"{t} edits (uncommitted) files main changed since its base: {', '.join(hot[:5])}"
                                     f" → `integrator sync` soon"))
    for x in range(len(act)):
        for y in range(x + 1, len(act)):
            t1, _, h1, _, f1, d1 = act[x]
            t2, _, h2, _, f2, d2 = act[y]
            shared = (f1 | d1) & (f2 | d2)
            if not shared:
                continue
            p = predict(ctx.main_root, h1, h2) if (f1 & f2) else []
            if p:
                found.append(("CONFLICT", f"{t1} × {t2}: {', '.join(p)}. Order them: one lands first, the other "
                                          "syncs right after."))
            else:
                found.append(("OVERLAP", f"{t1} × {t2} both change {', '.join(sorted(shared)[:5])} "
                                         f"({'merges cleanly so far' if f1 & f2 else 'uncommitted on one side'})"))
    for t, c, head, up, files, dirty in act:
        for f in sorted(files | dirty):
            for k in st["claims"]:
                if k["topic"] != t and k["mode"] == "exclusive" and covers(k["path"], f):
                    found.append(("CLAIM", f"{t} changed {f}, which {k['topic']} claimed ({k['path']})"))
    keys = [f"{kind}:{text}" for kind, text in found]

    def fn(st2):
        new = [k for k in keys if k not in st2["seen_conflicts"]]
        st2["seen_conflicts"] = keys
        for k in new:
            if k.startswith(("CONFLICT", "CLAIM")):
                emit(ctx, st2, "predicted", "conflicts", k)
        return set(new)
    new = update(ctx, fn)
    for (kind, text), k in zip(found, keys):
        print(f"{kind:8} {text}{'  [new]' if k in new else ''}")
    if not found:
        print(f"no predicted conflicts among {len(act)} branches")
    return REFUSED if any(k == "CONFLICT" for k, _ in found) else OK


# ---------------------------------------------------------------- launching sessions

BOOT_INTEGRATOR = ("You are this repo's integrator. Follow your instructions (the integrator skill): register with "
                   "`integrator start`, arm the watch, then show me the board.")


def boot_contributor(topic: str, task: str, wt: Path, branch: str, integrator: str) -> str:
    return (f"You are contributor '{topic}'. A live integrator session ('{integrator}') lands everyone's work in this "
            "repo. First invoke the contributor skill (Skill tool, skill \"contributor\") and follow it. You are already "
            f"in your own worktree {wt} on branch {branch}; confirm your registration with "
            f"`integrator join {topic} --session \"{topic}\"`, claim the paths you'll change, then do this task:\n\n{task}")


def cmd_launch(ctx: Ctx, a) -> int:
    flags = shlex.split(ctx.cfg["launch_flags"])
    st = load(ctx)
    if a.role == "integrator":
        name = a.name or f"integrator-{ctx.main_root.name}"
        cwd, argv = ctx.main_root, ["claude", "--agent", "integrator", "-n", name, *flags, BOOT_INTEGRATOR]
    else:
        if not a.topic or not a.task:
            raise Refused("launch contributor TOPIC \"TASK\"", USAGE)
        topic = topic_ok(a.topic)
        wt, branch, _ = make_worktree(ctx, topic)
        name = a.name or topic

        def fn(st2):
            c = st2["contributors"].get(topic) or {"topic": topic, "joined": now(), "landed_upto": None, "sync": None}
            c.update(worktree=str(wt), branch=branch, status="active", seen=now(), task=a.task, session=name)
            st2["contributors"][topic] = c
            emit(ctx, st2, "join", topic, f"{branch} in {wt}: {a.task} (launching session '{name}')")
        update(ctx, fn)
        cwd = wt
        argv = ["claude", "-n", name, *flags, boot_contributor(topic, a.task, wt, branch, iname(st))]
    line = f"cd {shlex.quote(str(cwd))} && " + " ".join(shlex.quote(x) for x in argv)
    if a.print:
        print(line)
        return OK
    if a.bg:
        r = subprocess.run([argv[0], "--bg", *argv[1:]], cwd=str(cwd), capture_output=True, text=True)
        print((r.stdout or r.stderr).strip())
        print(f"LAUNCHED '{name}' in the background (`claude agents` lists it, `claude attach <id>` opens it).")
        return OK if r.returncode == 0 else REFUSED
    src = os.environ.get("ITERM_SESSION_ID", "").split(":")[-1]
    script = HERE / "iterm-open.applescript"
    r = subprocess.run(["osascript", str(script), src, line, name, "tab" if a.tab else "split"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"couldn't open an iTerm pane ({(r.stderr or '').strip()[:200]}). Run this yourself:\n{line}")
        return REFUSED
    print(f"LAUNCHED '{name}' in a new iTerm {'tab' if a.tab else 'pane'} ({cwd}).")
    return OK


# ---------------------------------------------------------------- Claude Code hooks (fail open, stay fast)

def find_repo(path: Path) -> tuple[Path, Path] | None:
    """(worktree root, git common dir) for a path, without running git."""
    p = path
    while not p.exists() and p != p.parent:
        p = p.parent
    p = p if p.is_dir() else p.parent
    for d in [p, *p.parents]:
        g = d / ".git"
        if g.is_dir():
            return d, g
        if g.is_file():
            txt = g.read_text().strip()
            if not txt.startswith("gitdir:"):
                return None
            gd = Path(txt[7:].strip())
            gd = gd if gd.is_absolute() else (d / gd)
            cf = gd / "commondir"
            common = (gd / cf.read_text().strip()) if cf.exists() else gd
            return d, Path(real(common))
    return None


def hook_out(event: str, *, deny: str | None = None, context: str | None = None) -> None:
    out: dict = {"hookEventName": event}
    if deny:
        out.update(permissionDecision="deny", permissionDecisionReason=deny)
    if context:
        out["additionalContext"] = context
    print(json.dumps({"hookSpecificOutput": out}))


def hook_session_start(data: dict) -> None:
    found = find_repo(Path(data.get("cwd") or os.getcwd()))
    if not found or not (found[1] / "integrator" / "state.json").exists():
        return
    wt_root, common = found
    sd = common / "integrator"
    st = read_state(sd)
    if not integ_live(st):
        return
    i, sid = st["integrator"], data.get("session_id")
    if i.get("session_id") == sid:
        print("You are this repo's integrator (resumed). Run `integrator status`, re-arm the watch (Monitor on "
              "`integrator watch`), and carry on (the integrator skill).")
        return
    me = next((t for t, c in st["contributors"].items()
               if c.get("session_id") == sid or real(c["worktree"]) == real(wt_root)), None)
    if me:
        with locked(sd):
            st = read_state(sd)
            notes = take_notices(st, me)
            write_state(sd, st)
        extra = "".join(f"\n- {n['from']}: {n['text']}" for n in notes)
        print(f"You are contributor '{me}' of this repo; its integrator is '{i['session']}' (SendMessage to it). "
              f"Invoke the contributor skill if you haven't, and `integrator status --me`.{extra}")
        return
    print(f"An integrator session ('{i['session']}', its SendMessage address) is running for this repo "
          f"({st.get('repo')}): it lands everyone's work on {st.get('main')}. Before you edit anything in this repo, "
          "invoke the contributor skill: it gives you your own worktree (`integrator join <topic>`), path claims and "
          "the submit flow. Edits to the main checkout are refused while it runs (gitignored files are fine).")


def hook_pre_edit(data: dict) -> None:
    ti = data.get("tool_input") or {}
    fp = ti.get("file_path") or ti.get("notebook_path")
    if not fp:
        return
    path = Path(fp) if os.path.isabs(fp) else Path(data.get("cwd") or os.getcwd()) / fp
    found = find_repo(path)
    if not found:
        return
    wt_root, common = found
    sd = common / "integrator"
    if not (sd / "state.json").exists():
        return
    st = read_state(sd)
    if not integ_live(st):
        return
    i, sid = st["integrator"], data.get("session_id")
    if i.get("session_id") == sid:
        return
    try:
        rel = Path(real(path) if path.exists() else str(path)).relative_to(real(wt_root)).as_posix()
    except ValueError:
        rel = os.path.relpath(path, wt_root)
    if rel.startswith(".git/") or rel == ".git":
        return
    if real(wt_root) == real(st.get("repo") or ""):
        if git_ok(wt_root, "check-ignore", "-q", rel):
            return
        hook_out("PreToolUse", deny=(
            f"The integrator ('{i['session']}') owns the main checkout {wt_root} while it runs, so nobody edits it "
            "directly (edits there block its landings). Work in your own worktree: invoke the contributor skill, run "
            "`integrator join <topic> --session \"<your ListAgents name>\"`, move into the worktree it prints "
            "(EnterWorktree), and make this edit there. Gitignored files here are fine."))
        return
    owner = next((t for t, c in st["contributors"].items() if real(c["worktree"]) == real(wt_root)), None)
    # A lead and its subagents share one session id and may each own a topic: the edit belongs to the topic whose
    # worktree it is in when that topic is this session's, else to any topic of this session.
    mine_ids = [t for t, c in st["contributors"].items() if sid and c.get("session_id") == sid]
    me = owner if owner in mine_ids else next(iter(mine_ids), None)
    if owner is None:
        if real(wt_root) == real(i.get("worktree") or ""):
            hook_out("PreToolUse", deny=f"{wt_root} is the integrator's own worktree.")
            return
        marker = sd / "reminded" / str(sid)
        if secs_since(dt.datetime.fromtimestamp(marker.stat().st_mtime).astimezone().isoformat()
                      if marker.exists() else None) > 3600:
            marker.parent.mkdir(exist_ok=True)
            marker.touch()
            hook_out("PreToolUse", context=(
                f"This worktree isn't registered with the repo's integrator ('{i['session']}'), so it can't land what "
                "you make here. Invoke the contributor skill and run `integrator join <topic>` in this worktree."))
        return
    oc = st["contributors"][owner]
    if me and me != owner:
        hook_out("PreToolUse", deny=f"{rel} is in {owner}'s worktree ({wt_root}). Edit only your own worktree.")
        return
    if not me and oc.get("session_id") and oc["session_id"] != sid and alive(oc.get("pid")):
        hook_out("PreToolUse", deny=f"{wt_root} belongs to the live contributor '{owner}' (session "
                 f"'{oc.get('session')}'). Two sessions in one worktree overwrite each other: `integrator join` your own.")
        return
    mine = [k for k in st["claims"] if k["topic"] == owner and covers(k["path"], rel)]
    others = [k for k in st["claims"] if k["topic"] != owner and covers(k["path"], rel)]
    if any(k["mode"] == "exclusive" for k in others) and not any(k["mode"] == "granted" for k in mine):
        k = next(k for k in others if k["mode"] == "exclusive")
        task = (st["contributors"].get(k["topic"]) or {}).get("task") or ""
        hook_out("PreToolUse", deny=(
            f"{rel} is claimed by '{k['topic']}' ({k['path']}{': ' + task if task else ''}). Editing it too would make "
            f"a conflict. Ask the integrator ('{i['session']}', SendMessage) to arbitrate: it can order the work, split "
            f"the files, or `integrator grant {owner} {rel}`. Work on something else meanwhile."))
        return
    with locked(sd):
        st = read_state(sd)
        msgs = []
        if not mine:
            st["seq"]["claim"] += 1
            st["claims"].append({"id": st["seq"]["claim"], "topic": owner, "path": rel, "mode": "auto",
                                 "note": "edited", "since": now()})
            holders = sorted({k["topic"] for k in others})
            if holders:
                msgs.append(f"Heads-up: {rel} is also being changed by {', '.join(holders)}. Keep this edit small and "
                            "submit it soon; the integrator has been told.")
                for h in holders:
                    notify(st, h, f"{owner} is also editing {rel}: keep your edits there small and submit soon.", owner)
                st["seq"]["event"] += 1
                with open(sd / "events.jsonl", "a") as fh:
                    fh.write(json.dumps({"n": st["seq"]["event"], "t": now(), "kind": "overlap", "who": owner,
                                         "text": f"edits {rel}, also changed by {', '.join(holders)}"}) + "\n")
        for n in take_notices(st, owner):
            msgs.append(f"From {n['from']}: {n['text']}")
        st["contributors"][owner]["seen"] = now()
        write_state(sd, st)
    if msgs:
        hook_out("PreToolUse", context="integrator: " + "\n".join(msgs))


def cmd_hook(a) -> int:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        data = {}
    try:
        if a.event == "session-start":
            hook_session_start(data)
        elif a.event == "pre-edit":
            hook_pre_edit(data)
    except Exception as e:  # a hook must never break the session: fail open, leave a trace
        with contextlib.suppress(OSError):
            with open(Path.home() / ".integrator-hook-errors.txt", "a") as fh:
                fh.write(f"{now()} {a.event}: {type(e).__name__}: {e}\n")
    return OK


# ---------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="integrator", description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd")
    p = sp.add_parser("start"); p.add_argument("--session"); p.add_argument("--takeover", action="store_true")
    p = sp.add_parser("stop"); p.add_argument("--force", action="store_true")
    p = sp.add_parser("watch"); p.add_argument("--all", action="store_true"); p.add_argument("--since", type=int)
    p = sp.add_parser("land"); p.add_argument("ids", nargs="*"); p.add_argument("--verify", action="store_true")
    p.add_argument("--trust"); p.add_argument("--merge", action="store_true")
    p.add_argument("--continue", dest="cont", action="store_true"); p.add_argument("--abort", action="store_true")
    p = sp.add_parser("bounce"); p.add_argument("id"); p.add_argument("why")
    p = sp.add_parser("tell"); p.add_argument("topic"); p.add_argument("text")
    p = sp.add_parser("grant"); p.add_argument("topic"); p.add_argument("path"); p.add_argument("--note")
    sp.add_parser("prune")
    p = sp.add_parser("join"); p.add_argument("topic"); p.add_argument("--task"); p.add_argument("--session")
    p = sp.add_parser("claim"); p.add_argument("paths", nargs="+"); p.add_argument("--shared", action="store_true")
    p.add_argument("--note"); p.add_argument("--for", dest="for_topic")
    p = sp.add_parser("release"); p.add_argument("paths", nargs="*"); p.add_argument("--all", action="store_true")
    p.add_argument("--for", dest="for_topic")
    p = sp.add_parser("check"); p.add_argument("--cmd", dest="check_cmd")
    p = sp.add_parser("submit"); p.add_argument("--summary"); p.add_argument("--no-check", dest="no_check")
    p = sp.add_parser("sync"); p.add_argument("--withdraw", action="store_true")
    p = sp.add_parser("ask"); p.add_argument("text")
    p = sp.add_parser("scope"); p.add_argument("text")
    sp.add_parser("notices")
    p = sp.add_parser("leave"); p.add_argument("--abandon")
    p = sp.add_parser("status"); p.add_argument("--me", action="store_true"); p.add_argument("--json", action="store_true")
    sp.add_parser("conflicts")
    p = sp.add_parser("launch"); p.add_argument("role", choices=["integrator", "contributor"])
    p.add_argument("topic", nargs="?"); p.add_argument("task", nargs="?"); p.add_argument("--name")
    for f in ("--split", "--tab", "--bg", "--print"):
        p.add_argument(f, action="store_true")
    p = sp.add_parser("hook"); p.add_argument("event", choices=["session-start", "pre-edit"])
    sp.add_parser("help")
    return ap


COMMANDS = {"start": cmd_start, "stop": cmd_stop, "watch": cmd_watch, "land": cmd_land, "bounce": cmd_bounce,
            "tell": cmd_tell, "grant": cmd_grant, "prune": cmd_prune, "join": cmd_join, "claim": cmd_claim,
            "scope": cmd_scope, "release": cmd_release, "check": cmd_check, "submit": cmd_submit, "sync": cmd_sync, "ask": cmd_ask,
            "notices": cmd_notices, "leave": cmd_leave, "status": cmd_status, "conflicts": cmd_conflicts,
            "launch": cmd_launch}


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    if a.cmd == "hook":
        return cmd_hook(a)
    if a.cmd in (None, "help"):
        print(__doc__)
        return OK
    try:
        return COMMANDS[a.cmd](Ctx(), a)
    except Refused as e:
        print(f"REFUSED: {e}" if e.code == REFUSED else str(e), file=sys.stderr)
        return e.code
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
