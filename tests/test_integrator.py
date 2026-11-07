"""End-to-end tests: a throwaway repo, an integrator session and contributor sessions, all driven through the CLI.

Run: python3 -m unittest discover -s tests -v
Sessions are told apart the way Claude Code tells them apart: CLAUDE_CODE_SESSION_ID and CLAUDE_PID.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CLI = Path(__file__).resolve().parent.parent / "bin" / "integrator"
LIVE = str(os.getpid())  # a pid that is alive for the whole test


def dead_pid() -> str:
    p = subprocess.Popen(["true"])
    p.wait()
    return str(p.pid)


class Repo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(os.path.realpath(self.tmp.name)) / "proj"
        self.root.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "GIT_"))}
        self.env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                        GIT_COMMITTER_EMAIL="t@t", HOME=self.tmp.name)
        self.sh("git init -q -b main")
        (self.root / "a.txt").write_text("alpha\none\ntwo\nthree\n")
        (self.root / "b.txt").write_text("beta\n")
        (self.root / "check.sh").write_text("#!/bin/sh\n! grep -q BROKEN *.txt\n")
        (self.root / "check.sh").chmod(0o755)
        (self.root / ".integrator.toml").write_text('check = "./check.sh"\nshared = ["NOTES.md"]\n')
        self.sh("git add -A && git commit -qm init")

    def tearDown(self):
        self.tmp.cleanup()

    def sh(self, cmd, cwd=None):
        r = subprocess.run(cmd, shell=True, cwd=cwd or self.root, env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, f"{cmd}\n{r.stdout}\n{r.stderr}")
        return r.stdout.strip()

    def cli(self, *args, who=None, pid=LIVE, cwd=None, code=0, stdin=None):
        env = dict(self.env)
        if who:
            env["CLAUDE_CODE_SESSION_ID"] = who
            env["CLAUDE_PID"] = pid
        r = subprocess.run([sys.executable, str(CLI), *args], cwd=cwd or self.root, env=env, capture_output=True,
                           text=True, input=stdin)
        out = r.stdout + r.stderr
        if code is not None:
            self.assertEqual(r.returncode, code, f"integrator {' '.join(args)} → {r.returncode}\n{out}")
        return out

    def state(self):
        return json.loads((self.root / ".git" / "integrator" / "state.json").read_text())

    def rev(self, ref, cwd=None):
        return self.sh(f"git rev-parse {ref}", cwd)

    # helpers for the usual moves
    def start(self):
        return self.cli("start", "--session", "integ", who="I")

    def join(self, topic, who):
        out = self.cli("join", topic, "--session", topic, who=who)
        return Path(self.state()["contributors"][topic]["worktree"]), out

    def commit(self, wt, name, text, msg):
        (wt / name).write_text(text)
        self.sh(f"git add -A && git commit -qm {msg!r}", wt)

    def submit(self, wt, who):
        self.cli("check", who=who, cwd=wt)
        return self.cli("submit", who=who, cwd=wt)


class TestRoles(Repo):
    def test_start_once_takeover_when_dead(self):
        self.start()
        out = self.cli("start", "--session", "other", who="X", code=1)
        self.assertIn("already live", out)
        st = self.state()
        st["integrator"]["pid"] = int(dead_pid())
        (self.root / ".git" / "integrator" / "state.json").write_text(json.dumps(st))
        self.cli("start", "--session", "other", who="X")
        self.assertEqual(self.state()["integrator"]["session"], "other")

    def test_start_needs_session_name_from_claude(self):
        self.assertIn("--session", self.cli("start", who="I", code=2))

    def test_only_the_integrator_lands(self):
        self.start()
        self.assertIn("only the integrator", self.cli("land", who="C", code=1))

    def test_join_makes_worktree_and_ignores_it(self):
        self.start()
        wt, out = self.join("feat", "A")
        self.assertTrue((wt / "a.txt").exists())
        self.assertEqual(self.sh("git branch --show-current", wt), "wt/feat")
        self.assertIn("EnterWorktree", out)
        self.assertEqual(self.sh("git status --porcelain"), "")  # .claude/worktrees is excluded locally

    def test_join_refuses_second_live_session_in_one_worktree(self):
        self.start()
        wt, _ = self.join("feat", "A")
        self.assertIn("live session", self.cli("join", "feat", who="B", cwd=wt, code=1))


class TestClaims(Repo):
    def test_exclusive_shared_and_config_shared(self):
        self.start()
        wa, _ = self.join("a", "A")
        wb, _ = self.join("b", "B")
        self.cli("claim", "a.txt", who="A", cwd=wa)
        self.assertIn("REFUSED a.txt", self.cli("claim", "a.txt", who="B", cwd=wb, code=1))
        self.cli("claim", "NOTES.md", who="A", cwd=wa)  # config says shared
        self.cli("claim", "NOTES.md", who="B", cwd=wb)
        modes = {(k["topic"], k["path"]): k["mode"] for k in self.state()["claims"]}
        self.assertEqual(modes[("a", "NOTES.md")], "shared")
        self.assertEqual(modes[("b", "NOTES.md")], "shared")

    def test_glob_and_directory_overlap(self):
        self.start()
        wa, _ = self.join("a", "A")
        wb, _ = self.join("b", "B")
        self.cli("claim", "src/", who="A", cwd=wa)
        self.cli("claim", "src/**/x.py", who="B", cwd=wb, code=1)
        self.cli("claim", "tests/test_*.py", who="B", cwd=wb)


class TestLanding(Repo):
    def test_fast_forward_when_nothing_moved(self):
        self.start()
        wt, _ = self.join("feat", "A")
        self.commit(wt, "c.txt", "gamma\n", "add c")
        self.assertIn("SUBMITTED #1", self.submit(wt, "A"))
        out = self.cli("land", "next", who="I")
        self.assertIn("LANDED", out)
        self.assertIn("[exact]", out)
        self.assertEqual(self.rev("main"), self.rev("HEAD", wt))
        self.assertTrue((self.root / "c.txt").exists())  # the main checkout moved with it

    def test_submit_needs_a_passing_check(self):
        self.start()
        wt, _ = self.join("feat", "A")
        self.commit(wt, "c.txt", "BROKEN\n", "broken")
        self.assertIn("no check", self.cli("submit", who="A", cwd=wt, code=1))
        self.cli("check", who="A", cwd=wt, code=1)
        self.assertIn("failed", self.cli("submit", who="A", cwd=wt, code=1))
        self.cli("submit", "--no-check", "trying", who="A", cwd=wt)

    def test_rebased_disjoint_lands_without_rerunning_checks(self):
        self.start()
        wa, _ = self.join("a", "A")
        wb, _ = self.join("b", "B")
        self.commit(wa, "c.txt", "gamma\n", "a: add c")
        self.commit(wb, "d.txt", "delta\n", "b: add d")
        self.submit(wa, "A")
        self.submit(wb, "B")
        self.cli("land", "next", who="I")
        out = self.cli("land", "next", who="I")
        self.assertIn("[disjoint, rebased]", out)
        self.assertTrue((self.root / "c.txt").exists() and (self.root / "d.txt").exists())
        self.assertTrue(self.state()["contributors"]["b"]["sync"])

    def test_overlap_needs_verify_then_verify_lands(self):
        self.start()
        wa, _ = self.join("a", "A")
        wb, _ = self.join("b", "B")
        self.commit(wa, "a.txt", "alpha\nONE\ntwo\nthree\n", "a: one")
        self.commit(wb, "a.txt", "alpha\none\ntwo\nTHREE\n", "b: three")
        self.submit(wa, "A")
        self.submit(wb, "B")
        self.cli("land", "all", who="I", code=4)
        self.assertEqual(self.state()["landing"]["state"], "needs-verify")
        out = self.cli("land", "--continue", "--verify", who="I")
        self.assertIn("VERIFY OK", out)
        self.assertEqual((self.root / "a.txt").read_text(), "alpha\nONE\ntwo\nTHREE\n")
        self.assertEqual([s["status"] for s in self.state()["subs"]], ["landed", "landed"])

    def test_conflict_resolved_by_the_integrator(self):
        self.start()
        wa, _ = self.join("a", "A")
        wb, _ = self.join("b", "B")
        self.commit(wa, "a.txt", "alpha\nA-side\ntwo\nthree\n", "a: one")
        self.commit(wb, "a.txt", "alpha\nB-side\ntwo\nthree\n", "b: one")
        self.submit(wa, "A")
        self.submit(wb, "B")
        self.cli("land", "next", who="I")
        out = self.cli("land", "next", who="I", code=3)
        self.assertIn("CONFLICT", out)
        iwt = Path(self.state()["integrator"]["worktree"])
        (iwt / "a.txt").write_text("alpha\nA-side\nB-side\ntwo\nthree\n")
        self.sh("git add a.txt", iwt)
        self.cli("land", "--continue", who="I", code=4)  # a hand resolution needs a decision
        self.assertIn("resolved", self.state()["landing"]["risk"]["2"]["risk"])
        self.cli("land", "--continue", "--trust", "one-line union", who="I")
        self.assertEqual((self.root / "a.txt").read_text(), "alpha\nA-side\nB-side\ntwo\nthree\n")

    def test_abort_keeps_the_queue(self):
        self.start()
        wa, _ = self.join("a", "A")
        wb, _ = self.join("b", "B")
        self.commit(wa, "a.txt", "x\n", "a")
        self.commit(wb, "a.txt", "y\n", "b")
        self.submit(wa, "A")
        self.submit(wb, "B")
        self.cli("land", "all", who="I", code=3)
        self.cli("land", "--abort", who="I")
        self.assertIsNone(self.state()["landing"])
        self.assertEqual([s["status"] for s in self.state()["subs"]], ["queued", "queued"])
        self.cli("bounce", "2", "conflicts with #1: sync after it lands", who="I")
        self.assertIn("bounced", self.cli("status", "--me", who="B", cwd=wb))

    def test_sync_after_a_rebased_landing_replays_only_new_commits(self):
        self.start()
        wa, _ = self.join("a", "A")
        wb, _ = self.join("b", "B")
        self.commit(wb, "d.txt", "delta\n", "b: d")
        self.submit(wb, "B")
        self.cli("land", who="I")
        self.commit(wa, "c.txt", "gamma\n", "a: c")
        self.submit(wa, "A")
        self.cli("land", who="I")  # rebased onto b's work
        self.commit(wa, "e.txt", "eps\n", "a: e (after submitting)")
        self.cli("sync", who="A", cwd=wa)
        log = self.sh("git log --format=%s main..HEAD", wa)
        self.assertEqual(log, "a: e (after submitting)")
        self.submit(wa, "A")
        self.assertIn("[exact]", self.cli("land", who="I"))

    def test_sync_refuses_while_queued(self):
        self.start()
        wa, _ = self.join("a", "A")
        self.commit(wa, "c.txt", "gamma\n", "a: c")
        self.submit(wa, "A")
        self.assertIn("waits in the queue", self.cli("sync", who="A", cwd=wa, code=1))

    def test_resubmit_supersedes(self):
        self.start()
        wa, _ = self.join("a", "A")
        self.commit(wa, "c.txt", "gamma\n", "a: c")
        self.submit(wa, "A")
        self.commit(wa, "e.txt", "eps\n", "a: e")
        self.submit(wa, "A")
        self.assertEqual([s["status"] for s in self.state()["subs"]], ["superseded", "queued"])

    def test_quick_checks_guard_trivial_landings(self):
        (self.root / ".integrator.toml").write_text('check = "./check.sh"\n[quick]\n"docs/" = "! grep -rq BAD docs"\n')
        self.sh("git commit -qam 'quick rule'")
        self.start()
        wa, _ = self.join("a", "A")
        (wa / "docs").mkdir()
        self.commit(wa, "docs/x.md", "BAD\n", "docs: bad")
        self.cli("submit", "--no-check", "docs only", who="A", cwd=wa)
        self.assertIn("QUICK CHECK FAILED", self.cli("land", who="I", code=5))
        self.cli("land", "--abort", who="I")
        self.commit(wa, "docs/x.md", "good\n", "docs: good")
        self.cli("submit", "--no-check", "docs only", who="A", cwd=wa)
        self.assertIn("quick checks OK", self.cli("land", who="I"))

    def test_main_moved_outside_the_protocol(self):
        self.start()
        wa, _ = self.join("a", "A")
        self.commit(wa, "c.txt", "gamma\n", "a: c")
        self.submit(wa, "A")
        (self.root / "b.txt").write_text("beta by hand\n")
        self.sh("git commit -qam 'a person commits on main'")
        self.assertIn("LANDED", self.cli("land", who="I"))
        self.assertEqual(self.sh("git log -1 --format=%s main~1"), "a person commits on main")


class TestPrediction(Repo):
    def test_conflicts_between_branches(self):
        self.start()
        wa, _ = self.join("a", "A")
        wb, _ = self.join("b", "B")
        self.commit(wa, "a.txt", "alpha\nA\ntwo\nthree\n", "a")
        self.commit(wb, "a.txt", "alpha\nB\ntwo\nthree\n", "b")
        out = self.cli("conflicts", who="I", code=1)
        self.assertIn("CONFLICT a × b: a.txt", out)
        self.assertIn("[new]", out)
        self.assertNotIn("[new]", self.cli("conflicts", who="I", code=1))


class TestHooks(Repo):
    def hook(self, event, who, path=None, cwd=None):
        data = {"session_id": who, "cwd": str(cwd or self.root)}
        if path:
            data["tool_input"] = {"file_path": str(path)}
        out = self.cli("hook", event, stdin=json.dumps(data))
        return json.loads(out) if out.strip().startswith("{") else out

    def test_main_checkout_is_closed_while_integrator_runs(self):
        self.assertEqual(self.hook("pre-edit", "C", self.root / "a.txt"), "")  # no integrator: nothing happens
        self.start()
        r = self.hook("pre-edit", "C", self.root / "a.txt")
        self.assertEqual(r["hookSpecificOutput"]["permissionDecision"], "deny")
        (self.root / ".gitignore").write_text("scratch/\n")
        self.assertEqual(self.hook("pre-edit", "C", self.root / "scratch" / "x.txt"), "")
        self.assertEqual(self.hook("pre-edit", "I", self.root / "a.txt"), "")  # the integrator may

    def test_claims_are_enforced_and_edits_auto_claim(self):
        self.start()
        wa, _ = self.join("a", "A")
        wb, _ = self.join("b", "B")
        self.cli("claim", "a.txt", who="A", cwd=wa)
        r = self.hook("pre-edit", "B", wb / "a.txt", wb)
        self.assertEqual(r["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("claimed by 'a'", r["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual(self.hook("pre-edit", "B", wb / "b.txt", wb), "")
        self.assertIn(("b", "b.txt", "auto"), {(k["topic"], k["path"], k["mode"]) for k in self.state()["claims"]})
        r = self.hook("pre-edit", "A", wa / "b.txt", wa)  # soft overlap: allowed, with a heads-up
        self.assertIn("also being changed by b", r["hookSpecificOutput"]["additionalContext"])
        self.cli("grant", "b", "a.txt", who="I")
        self.assertNotIn("deny", json.dumps(self.hook("pre-edit", "B", wb / "a.txt", wb)))

    def test_one_session_with_several_worktrees_edits_each_of_them(self):
        """A lead and its subagents share one session id, and each subagent joins its own topic (seen in a real
        run). Every edit was matched to the first topic with that id, so the others were refused in their own
        worktrees."""
        self.start()
        wx, _ = self.join("x", "L")
        wy, _ = self.join("y", "L")
        wb, _ = self.join("b", "B")
        self.assertNotIn("deny", json.dumps(self.hook("pre-edit", "L", wx / "x.txt", wx)))
        self.assertNotIn("deny", json.dumps(self.hook("pre-edit", "L", wy / "y.txt", wy)))
        r = self.hook("pre-edit", "B", wy / "y.txt", wy)  # another session in y's worktree is still refused
        self.assertEqual(r["hookSpecificOutput"]["permissionDecision"], "deny")
        r = self.hook("pre-edit", "L", wb / "b.txt", wb)  # and so is L in a worktree none of its topics own
        self.assertEqual(r["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_notices_arrive_at_the_next_edit(self):
        self.start()
        wa, _ = self.join("a", "A")
        self.cli("tell", "a", "please sync", who="I")
        r = self.hook("pre-edit", "A", wa / "a.txt", wa)
        self.assertIn("please sync", r["hookSpecificOutput"]["additionalContext"])
        r = self.hook("pre-edit", "A", wa / "a.txt", wa)
        self.assertEqual(r, "")  # delivered once

    def test_session_start_cue(self):
        self.assertEqual(self.hook("session-start", "C"), "")
        self.start()
        self.assertIn("invoke the contributor skill", self.hook("session-start", "C"))
        self.assertIn("You are this repo's integrator", self.hook("session-start", "I"))

    def test_hook_fails_open_on_garbage(self):
        out = self.cli("hook", "pre-edit", stdin="not json")
        self.assertEqual(out, "")


class TestConflictHint(Repo):
    def test_the_repos_hint_follows_every_conflict(self):
        toml = self.root / ".integrator.toml"
        toml.write_text(toml.read_text() + 'conflict_hint = "keep both: ./keep-both {files}"\n')
        self.sh("git commit -qam hint")
        self.start()
        wa, _ = self.join("a", "A")
        wb, _ = self.join("b", "B")
        self.commit(wb, "a.txt", "alpha\nONE-b\ntwo\nthree\n", "b: one")
        self.submit(wb, "B")
        self.cli("land", who="I")
        self.commit(wa, "a.txt", "alpha\nONE-a\ntwo\nthree\n", "a: one")
        out = self.cli("sync", who="A", cwd=wa, code=None)
        self.assertIn("SYNC CONFLICT in: a.txt", out)
        self.assertIn("hint (this repo): keep both: ./keep-both a.txt", out)
        self.sh("git checkout --theirs a.txt && git add a.txt", wa)  # (any resolution; the hint is what's tested)
        self.cli("sync", who="A", cwd=wa, code=None)


class TestScope(Repo):
    def test_scope_goes_on_the_board_wakes_the_integrator_and_names_the_neighbours(self):
        self.start()
        wa, _ = self.join("a", "A")
        wb, _ = self.join("b", "B")
        out = self.cli("scope", "b.txt; adds beta(), which a calls", who="B", cwd=wb)
        self.assertIn("SCOPE for b is on the board", out)
        self.assertIn("a ('a'): (no scope yet)", out)  # the others, by session, even before they report
        self.assertEqual(self.state()["contributors"]["b"]["scope"], "b.txt; adds beta(), which a calls")
        events = [json.loads(x) for x in (self.root / ".git" / "integrator" / "events.jsonl").read_text().splitlines()]
        self.assertEqual((events[-1]["kind"], events[-1]["who"]), ("scope", "b"))
        out = self.cli("scope", "a.txt;   calls beta()", who="A", cwd=wa)
        self.assertIn("b ('b'): b.txt; adds beta(), which a calls", out)  # a finds its neighbour itself
        board = self.cli("status")
        self.assertIn("scope (just now): a.txt; calls beta()", board)  # whitespace folded to one line
        self.assertIn("scope (just now): b.txt; adds beta(), which a calls", board)
        self.cli("scope", "  ", who="A", cwd=wa, code=2)  # an empty scope is a usage error
        self.cli("scope", "x", who="Z", code=1)  # only a registered contributor has a scope


class TestLeave(Repo):
    def test_leave_only_when_landed_then_prune(self):
        self.start()
        wa, _ = self.join("a", "A")
        self.commit(wa, "c.txt", "gamma\n", "a: c")
        self.assertIn("haven't landed", self.cli("leave", who="A", cwd=wa, code=1))
        self.submit(wa, "A")
        self.cli("land", who="I")
        out = self.cli("leave", who="A", cwd=wa)
        self.assertIn("🏁 SAFE TO CLOSE: a, all landed (#1 → ", out)  # the line the contributor ends on
        self.assertIn("PRUNED a", self.cli("prune", who="I"))
        self.assertFalse(wa.exists())
        wb, _ = self.join("b", "B")
        self.commit(wb, "d.txt", "delta\n", "b: d")
        out = self.cli("leave", "--abandon", "the owner stopped it", who="B", cwd=wb)
        self.assertNotIn("SAFE TO CLOSE", out)  # unlanded work is never safe to close

    def test_one_session_two_topics_flags_only_when_the_last_leaves(self):
        self.start()
        w1, _ = self.join("t1", "S")
        w2, _ = self.join("t2", "S")  # the same session takes a second topic
        self.commit(w1, "c.txt", "gamma\n", "t1: c")
        self.submit(w1, "S")
        self.cli("land", who="I")
        out = self.cli("leave", who="S", cwd=w1)
        self.assertIn("still works on t2", out)
        self.assertNotIn("SAFE TO CLOSE", out)  # closing the session now would lose t2
        self.commit(w2, "d.txt", "delta\n", "t2: d")
        self.submit(w2, "S")
        self.cli("land", who="I")
        out = self.cli("leave", who="S", cwd=w2)
        self.assertIn("🏁 SAFE TO CLOSE: t1, t2, all landed (#1 → ", out)
        self.assertIn(", #2 → ", out)


if __name__ == "__main__":
    unittest.main()
