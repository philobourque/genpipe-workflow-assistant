#!/usr/bin/env python
"""Which palette a terminal gets, and whether the colours in it can be read.

Two halves, and the second is the one worth having:

  PRECEDENCE   NO_COLOR first, then an explicit GENPIPE_THEME, then best-effort
               detection, then the background-agnostic fallback. Pure function
               of an environment mapping, so every branch is one call.

  CONTRAST     every colour in both tuned palettes, measured against the
               backgrounds theme.py says it was chosen for, using the WCAG 2.1
               relative-luminance formula. This is what stops the palette
               drifting back to "looks fine on my machine": a value edited to
               something prettier and less legible fails here rather than on
               somebody's laptop three weeks later.

Stdlib only. No agent stack, no cluster, no terminal.

Run:  python tests/test_theme.py
"""
import sys

from harness import Report

from genpipe import theme

# The backgrounds theme.py's docstring commits to. Kept here as the thing the
# numbers are asserted against, so the claim in the documentation and the check
# in CI are the same list.
DARK_BACKGROUNDS = ("#1e1e1e", "#000000", "#282c34")
LIGHT_BACKGROUNDS = ("#ffffff", "#f5f5f5", "#fdf6e3")

# WCAG AA for body text. `faint` is furniture -- rules, box edges, a receipt
# line -- and is held to the 3:1 non-text threshold instead.
AA = 4.5
FURNITURE = 3.0

# The helix. A depth ramp on a logo rather than text: the front strand is held
# to AA because it is the figure, the turn to 3:1, and the strand drawn as
# being BEHIND the axis is deliberately the faintest thing on the screen --
# that is what further away looks like, and it carries no meaning.
DEPTH = {"dna_fg": AA, "dna_mid": FURNITURE, "dna_bg": 0.0, "dna_rung": AA}


def _luminance(rgb):
    chans = []
    for v in rgb:
        v /= 255.0
        chans.append(v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4)
    return 0.2126 * chans[0] + 0.7152 * chans[1] + 0.0722 * chans[2]


def _hex(text):
    text = text.lstrip("#")
    return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))


def ratio(rgb, background):
    a, b = _luminance(rgb), _luminance(_hex(background))
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def main():
    r = Report("the light and dark palettes")

    # ------------------------------------------------------------------ #
    r.section("no colour beats every other consideration")
    for env in ({"NO_COLOR": "1"}, {"NO_COLOR": ""},
                {"NO_COLOR": "1", "GENPIPE_THEME": "dark"},
                {"TERM": "dumb"}, {"TERM": "dumb", "GENPIPE_THEME": "light"}):
        r.check(f"{env} asks for none", theme.resolve(env) == theme.NONE, env)
    r.check("and NO_COLOR is honoured even when it is empty, per no-color.org",
            not theme.colour_wanted({"NO_COLOR": ""}))
    blank = theme.palette({"NO_COLOR": "1"})
    r.check("every role is the empty string",
            all(blank[role] == "" for role in theme.ROLES), blank)

    # ------------------------------------------------------------------ #
    r.section("an explicit choice is authoritative and is never second-guessed")
    for wanted in ("dark", "light"):
        for extra in ({}, {"COLORFGBG": "0;15"}, {"COLORFGBG": "15;0"}):
            env = dict(extra, GENPIPE_THEME=wanted)
            r.check(f"GENPIPE_THEME={wanted} wins over {extra or 'nothing'}",
                    theme.resolve(env) == wanted, env)
    r.check("case and surrounding space do not matter",
            theme.resolve({"GENPIPE_THEME": "  DARK "}) == "dark")

    # ------------------------------------------------------------------ #
    r.section("detection is best-effort and cannot hang")
    r.check("a dark COLORFGBG background is read as dark",
            theme.resolve({"COLORFGBG": "15;0"}) == "dark")
    r.check("a light one as light",
            theme.resolve({"COLORFGBG": "0;15"}) == "light")
    r.check("the three-field form konsole writes is handled",
            theme.resolve({"COLORFGBG": "0;default;15"}) == "light")
    for junk in ("", "nonsense", "15;", "1;2;3;banana", "0;256"):
        r.check(f"{junk!r} is not guessed at",
                theme.resolve({"COLORFGBG": junk}) == "safe", junk)
    r.check("auto with nothing to go on falls back rather than guessing",
            theme.resolve({"GENPIPE_THEME": "auto"}) == "safe")
    r.check("and so does an unset variable",
            theme.resolve({}) == "safe")
    # The whole reason detection is one variable: everything else needs a reply
    # from the terminal, and this list is what must not break.
    r.check("nothing is read from a terminal, so ssh/tmux/CI/pipes are safe",
            "OSC" not in theme.__doc__.replace("OSC 11", "")
            and "\033]11" not in open(theme.__file__).read())

    # ------------------------------------------------------------------ #
    r.section("the fallback is the palette that assumes nothing")
    safe = theme.palette({})
    r.check("which is basic ANSI, not RGB",
            all("38;2;" not in safe[role] for role in theme.ROLES), safe)
    r.check("muted is 90, the one grey legible against black and white alike",
            safe["muted"] == "\033[90m")
    r.check("and faint is still SGR 2, exactly as it always was",
            safe["faint"] == "\033[2m")

    # ------------------------------------------------------------------ #
    r.section("colour depth degrades a role at a time, never all at once")
    true = theme.palette({"GENPIPE_THEME": "dark", "COLORTERM": "truecolor"})
    r.check("truecolour gets 24-bit values",
            true["muted"].startswith("\033[38;2;"), true["muted"])
    x256 = theme.palette({"GENPIPE_THEME": "dark", "TERM": "xterm-256color"})
    r.check("256-colour gets an indexed approximation, not the basic eight",
            x256["muted"].startswith("\033[38;5;"), x256["muted"])
    plain = theme.palette({"GENPIPE_THEME": "dark", "TERM": "vt100"})
    r.check("and a terminal with neither gets what it can actually render",
            plain["muted"] == theme.SAFE["muted"], plain["muted"])
    r.check("every role has a value in every palette",
            all(set(theme.palette(e)) >= set(theme.ROLES)
                for e in ({}, {"GENPIPE_THEME": "dark"},
                          {"GENPIPE_THEME": "light"}, {"NO_COLOR": "1"})))
    r.check("and the palette says which one it is",
            [theme.palette(e)["theme"] for e in
             ({}, {"GENPIPE_THEME": "dark"}, {"GENPIPE_THEME": "light"},
              {"NO_COLOR": "1"})] == ["safe", "dark", "light", "none"])

    # ------------------------------------------------------------------ #
    #  The half that is not about plumbing.
    # ------------------------------------------------------------------ #
    for label, palette, backgrounds in (("dark", theme.DARK, DARK_BACKGROUNDS),
                                        ("light", theme.LIGHT, LIGHT_BACKGROUNDS)):
        r.section(f"contrast — the {label} palette, against {' '.join(backgrounds)}")
        for role in theme.ROLES:
            want = DEPTH.get(role, FURNITURE if role == "faint" else AA)
            worst = min(ratio(palette[role], bg) for bg in backgrounds)
            r.check(f"{role:<10} {worst:5.2f}:1  (needs {want}:1)",
                    worst >= want,
                    f"{palette[role]} is {worst:.2f}:1 on its worst background")

        # A ramp that does not separate is not a ramp. Front, turn and back
        # must be three visibly different distances on the palette's own
        # primary background -- and on a light ground the order INVERTS,
        # because further away is lighter there.
        primary = backgrounds[0]
        front, turn, back = (ratio(palette[k], primary)
                             for k in ("dna_fg", "dna_mid", "dna_bg"))
        r.check(f"the helix reads as three depths ({front:.1f} / {turn:.1f} / {back:.1f})",
                front > turn > back and front - back >= 1.5,
                (front, turn, back))

    # ------------------------------------------------------------------ #
    r.section("the two palettes really are different, which is the point")
    dark = theme.palette({"GENPIPE_THEME": "dark", "COLORTERM": "truecolor"})
    light = theme.palette({"GENPIPE_THEME": "light", "COLORTERM": "truecolor"})
    r.check("no role has the same value in both",
            not any(dark[role] == light[role] for role in theme.ROLES))
    # The specific complaint this round started from, stated as a number: the
    # quiet text has to be quiet, not absent.
    for role in ("secondary", "muted"):
        d = min(ratio(theme.DARK[role], bg) for bg in DARK_BACKGROUNDS)
        l = min(ratio(theme.LIGHT[role], bg) for bg in LIGHT_BACKGROUNDS)
        r.check(f"{role} clears AA on the background it was chosen for, both ways"
                f"  (dark {d:.1f}:1, light {l:.1f}:1)", d >= AA and l >= AA)

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
