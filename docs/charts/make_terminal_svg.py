#!/usr/bin/env python3
"""Turn a captured terminal session into an animated SVG (stdlib only).

    docs/charts/make_terminal_svg.py docs/data/demo-session.txt docs/media/demo.svg \\
        --title "integrator: ..." --pick "integrator conflicts" --pick "integrator land all" ...

The input is the transcript docs/demo/run-demo.sh prints:
    [role] $ command       a command, run as that role
    ...                    its output, line by line
    [exit N]               printed when the command failed
    # text                 an annotation, shown before the next command
Without --pick every block is drawn; each --pick SUBSTRING keeps the next block (in transcript order) whose command
contains SUBSTRING, together with the annotations just before it. Nothing is reworded: long lines are wrapped at
--cols on word boundaries, as a terminal of that width would show them, and highlighting only colours text.

The SVG types each command out, shows its output a moment later, and loops. Every animation sits inside
`@media (prefers-reduced-motion: no-preference)`, and the finished transcript is the static default, so viewers who
ask for less motion, and renderers that ignore CSS animation (rsvg-convert, thumbnailers), see the last frame.

    --static                       draw the finished screen with no animation (for stills).

    --frames --fps 15 --rows 28    instead write one static SVG per video frame into the directory `out`: a terminal
                                   that scrolls like a real one (the last --rows rows). Turn the frames into a video
                                   with rsvg-convert and ffmpeg.
"""
import argparse
import html
import re
import sys
from pathlib import Path

NBSP = " "
BG, BAR, FG, DIM, CMD = "#15171c", "#23262e", "#c9ced8", "#7d8594", "#ffffff"
GREEN, RED, AMBER, CYAN, YELLOW, BLUE = "#7fd1a0", "#f2777a", "#f0b45e", "#6cc6d9", "#f6d365", "#7aa2f7"
ROLE_COLOR = {"integrator": AMBER, "csv-quotes": CYAN, "csv-header": "#c792ea", "cli-delim": GREEN, "newcomer": BLUE}
# (regex, colour): group 1 is coloured, the rest of the row keeps the default colour
RULES = [
    (r"^(CONFLICT landing|CONFLICT|REFUSED|\[exit \d+\]|QUICK CHECK FAILED|VERIFY FAILED|BLOCKED)", RED),
    (r"^(NEEDS-VERIFY|warning|CLAIM|OVERLAP)", AMBER),
    (r"^(LANDED|VERIFY OK|CHECK PASS|SUBMITTED #\d+|CLAIMED|JOINED|GRANTED|SYNCED|PRUNED|LEFT\.|INTEGRATOR STARTED|SCOPE for \S+)", GREEN),
    (r"^(hint \(this repo\):)", CYAN),
    (r"^(\U0001f3c1 SAFE TO CLOSE)", YELLOW),
    (r"(\[exact\]|\[disjoint(?:, rebased)?\])", GREEN),
    (r"(\[resolved(?:, rebased)?\])", AMBER),
    (r"(\"permissionDecision\": \"deny\")", RED),
    (r"^(<<<<<<<|=======|>>>>>>>)", RED),
]


class Block:
    def __init__(self, role, cmd, notes):
        self.role, self.cmd, self.notes, self.out = role, cmd, notes, []


def parse(text):
    """The transcript as blocks: annotations, the command, its output."""
    blocks, notes, cur = [], [], None
    for line in text.splitlines():
        m = re.match(r"^\[([\w-]+)\] \$ (.*)$", line)
        if m:
            cur = Block(m.group(1), m.group(2), notes)
            notes = []
            blocks.append(cur)
        elif line.startswith("# "):  # run-demo.sh prints annotations so; no command in the demo prints such a line
            notes.append(line[2:])
        elif cur is not None:
            cur.out.append(line)
    return blocks


def pick(blocks, patterns):
    if not patterns:
        return blocks
    out, i = [], 0
    for pat in patterns:
        j = next((k for k in range(i, len(blocks)) if pat in blocks[k].cmd), None)
        if j is None:
            sys.exit(f"--pick {pat!r}: no later block has that in its command")
        out.append(blocks[j])
        i = j + 1
    return out


def wrap(line, cols, hang):
    """Break `line` at spaces into rows of at most `cols` characters, as a terminal of that width would show it.
    Continuation rows are indented by `hang`."""
    rows, indent, lead = [], "", len(line) - len(line.lstrip())
    while len(indent) + len(line) > cols:
        room = cols - len(indent)
        cut = line.rfind(" ", lead + 1, room + 1)
        if cut == -1:  # nothing to break at: cut hard
            cut = room
        rows.append(indent + line[:cut].rstrip())
        line = line[cut:].lstrip()
        indent, lead = " " * hang, 0
    rows.append(indent + line)
    return rows


def spans(text, base):
    """[(text, colour)] for one row: the regex highlights over a base colour."""
    marks = sorted((m.start(1), m.end(1), col) for rx, col in RULES for m in re.finditer(rx, text))
    out, pos = [], 0
    for a, b, col in marks:
        if a < pos:
            continue
        if a > pos:
            out.append((text[pos:a], base))
        out.append((text[a:b], col))
        pos = b
    if pos < len(text):
        out.append((text[pos:], base))
    return out or [("", base)]


class Row:
    """One terminal row: coloured parts, when it appears, and how long its last characters take to type.
    The first `lead` characters (a prompt) are there from the start; dur == 0 means the row appears whole."""

    def __init__(self, parts, start, dur=0.0, lead=0):
        self.parts, self.start, self.dur, self.lead = parts, start, dur, lead

    @property
    def text(self):
        return "".join(t for t, _ in self.parts)


def layout(blocks, cols, speed=1.0, cps=55.0):
    """Rows with their start times. `speed` > 1 plays the same session faster (typing and pauses alike)."""
    rows, t = [], 0.6 / speed
    for b in blocks:
        for note in b.notes:
            for k, piece in enumerate(wrap(note, cols - 2, 2)):
                rows.append(Row([(("# " if k == 0 else "  ") + piece, DIM)], t))
            t += 0.5 / speed
        prompt = f"[{b.role}] $ "
        for k, piece in enumerate(wrap(prompt + b.cmd, cols, len(prompt))):
            lead = len(prompt) if k == 0 else 0
            dur = (len(piece) - lead) / (cps * speed)
            parts = [(prompt, ROLE_COLOR.get(b.role, FG)), (piece[lead:], CMD)] if k == 0 else [(piece, CMD)]
            rows.append(Row(parts, t, dur, lead))
            t += dur
        t += 0.35 / speed
        n_out = 0
        for line in b.out:
            for piece in wrap(line, cols, len(line) - len(line.lstrip()) + 2):
                rows.append(Row(spans(piece, FG), t))
                t += 0.04 / speed
                n_out += 1
        t += min(0.9 + 0.11 * n_out, 3.6) / speed  # time to read it
    return rows, t


def esc(s):
    return html.escape(s, quote=False).replace(" ", NBSP)


def text_el(parts, x, y, cls=""):
    inner = "".join(f'<tspan fill="{c}">{esc(t)}</tspan>' for t, c in parts if t)
    return f'<text{cls} x="{x:.1f}" y="{y:.1f}">{inner}</text>'


def window(width, height, title, body, css=""):
    label = html.escape(title, quote=True)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" xml:space="preserve" width="{width:.0f}" height="{height:.0f}" viewBox="0 0 {width:.0f} {height:.0f}" role="img" aria-label="{label}">
<style>
text{{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;font-size:FS;white-space:pre}}
{css}
</style>
<rect width="100%" height="100%" rx="10" fill="{BG}"/>
<rect width="100%" height="30" rx="10" fill="{BAR}"/><rect y="20" width="100%" height="10" fill="{BAR}"/>
<circle cx="18" cy="15" r="5.5" fill="#ff5f57"/><circle cx="36" cy="15" r="5.5" fill="#febc2e"/><circle cx="54" cy="15" r="5.5" fill="#28c840"/>
<text x="{width / 2:.0f}" y="19.5" fill="{DIM}" text-anchor="middle" style="font-size:12px">{html.escape(title)}</text>
{body}
</svg>
"""


def keyframes(i, r, cycle):
    """The CSS for row i: hidden until its moment, then shown (typed, for a command) until the cycle ends."""
    p0 = r.start / cycle * 100
    if not r.dur:
        return f"@keyframes t{i}{{0%,{p0:.3f}%{{visibility:hidden}}{p0 + 0.001:.3f}%,100%{{visibility:visible}}}}"
    p1 = (r.start + r.dur) / cycle * 100
    n, hidden = max(len(r.text) - r.lead, 1), 100.0 * (1 - r.lead / len(r.text))
    return (f"@keyframes t{i}{{0%,{p0:.3f}%{{visibility:hidden;clip-path:inset(0 100% 0 0)}}"
            f"{p0 + 0.001:.3f}%{{visibility:visible;clip-path:inset(0 {hidden:.2f}% 0 0);animation-timing-function:steps({n})}}"
            f"{p1:.3f}%,100%{{visibility:visible;clip-path:inset(0 0 0 0)}}}}")


def frame_parts(r, t):
    """The parts of row r visible at time t (None before it appears); a command is cut where its typing has got to."""
    if r.start > t:
        return None
    if not r.dur or t >= r.start + r.dur:
        return r.parts
    left = r.lead + int((t - r.start) / r.dur * (len(r.text) - r.lead)) + 1
    shown = []
    for txt, col in r.parts:
        shown.append((txt[:left], col))
        left = max(0, left - len(txt))
    return shown


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("transcript")
    ap.add_argument("out", help="output .svg, or with --frames the directory for the frame files")
    ap.add_argument("--title", default="terminal")
    ap.add_argument("--pick", action="append", default=[])
    ap.add_argument("--cols", type=int, default=112)
    ap.add_argument("--fs", type=float, default=12.5, help="font size in px")
    ap.add_argument("--speed", type=float, default=1.0, help="play faster than the default pace (2 = twice as fast)")
    ap.add_argument("--hold", type=float, default=5.0, help="seconds the finished screen stays before the loop restarts")
    ap.add_argument("--static", action="store_true", help="no animation: just the finished screen")
    ap.add_argument("--frames", action="store_true")
    ap.add_argument("--fps", type=float, default=15)
    ap.add_argument("--rows", type=int, default=28, help="visible rows of the scrolling terminal with --frames")
    a = ap.parse_args()

    ch, lh, pad, top = a.fs * 0.602, a.fs * 1.4, 18.0, 30.0
    blocks = pick(parse(Path(a.transcript).read_text()), a.pick)
    rows, t_end = layout(blocks, a.cols, a.speed)
    width = pad * 2 + a.cols * ch
    cycle = t_end + a.hold

    if a.frames:
        out = Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        height = top + pad + a.rows * lh + 4
        n = int(cycle * a.fps)
        for f in range(n):
            vis = [p for p in (frame_parts(r, f / a.fps) for r in rows) if p is not None]
            body = [text_el(p, pad, top + pad + (i + 1) * lh - 4) for i, p in enumerate(vis[max(0, len(vis) - a.rows):])]
            (out / f"f{f:04d}.svg").write_text(window(width, height, a.title, "\n".join(body)).replace("FS", f"{a.fs}px"))
        print(f"wrote {n} frames to {out} ({cycle:.1f} s at {a.fps:g} fps, {len(rows)} rows)")
        return

    height = top + pad + len(rows) * lh + 16
    cls = (lambda i: "") if a.static else (lambda i: f' class="r{i}"')
    body = [text_el(r.parts, pad, top + pad + (i + 1) * lh - 4, cls(i)) for i, r in enumerate(rows)]
    css = [] if a.static else [f"{keyframes(i, r, cycle)}.r{i}{{animation:t{i} {cycle:.2f}s linear infinite}}" for i, r in enumerate(rows)]
    style = "" if a.static else "@media (prefers-reduced-motion:no-preference){\n" + "\n".join(css) + "\n}"
    svg = window(width, height, a.title, "\n".join(body), style).replace("FS", f"{a.fs}px")
    Path(a.out).write_text(svg)
    note = "static" if a.static else f"{t_end:.1f} s of animation + {a.hold:g} s hold"
    print(f"wrote {a.out}: {len(rows)} rows, {note}, {len(svg) / 1024:.0f} KB")


if __name__ == "__main__":
    main()
