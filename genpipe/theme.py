"""Which colours this terminal gets, and why those and not others.

Stdlib only, like everything below `agent` -- see the package docstring.

--------------------------------------------------------------------------
WHAT WENT WRONG WITH ONE PALETTE
--------------------------------------------------------------------------
The previous round replaced colours that only contrast with ONE background
(cyan, ANSI 37) with colours that contrast with both (red, green, 90, and
bold). That fixed correctness -- nothing became invisible -- and it did not
fix legibility, because "works on both" and "works well on either" are not
the same claim. A single compromise palette is worse on each background than
a palette chosen for that background would be:

    light terminal   90 (bright black) and SGR 2 (dim) are rendered as a pale
                     grey by most light themes. Ratios near 2.5:1 -- present,
                     but tiring to read and easy to miss.
    dark terminal    the same two are rendered dark, so the quiet half of
                     every screen sits just above the background.

So there are two palettes now, and a rule for choosing between them.

--------------------------------------------------------------------------
PRECEDENCE, IN ORDER, AND IT IS SHORT ON PURPOSE
--------------------------------------------------------------------------
  1. NO_COLOR / TERM=dumb          -> no colour at all. Unchanged, and still
                                      checked first: somebody who has said
                                      "no colour" is not asking which one.
  2. GENPIPE_THEME=light|dark      -> exactly that. Authoritative, always
                                      wins, never second-guessed.
  3. GENPIPE_THEME=auto or unset   -> COLORFGBG, if it is there and parses.
  4. anything else                 -> SAFE: the background-agnostic palette
                                      this project already shipped.

WHY THE FALLBACK IS THE OLD PALETTE rather than a guess. A wrong guess here
does not degrade gracefully -- it paints light grey on white. The palette
that assumes nothing is the honest answer when nothing is known, so a person
who sets no variable sees exactly what they saw before, and the two tuned
palettes are opt-in. `GENPIPE_THEME` goes in .env beside GENPIPE_LLM_MODEL
and GENPIPE_USER (settings.load reads it), so persisting a choice costs one
line in a file that already exists and no new slash command.

WHY DETECTION IS ONE ENVIRONMENT VARIABLE AND NOTHING ELSE. The sequence
that actually asks a terminal for its background (OSC 11) requires writing to
the tty and reading a reply back, which is a hang waiting to happen over ssh,
under tmux, under screen, in CI, and against a pipe -- the exact list this
must not break. COLORFGBG is a read of an already-set variable: it cannot
hang, cannot block, and cannot fail. It is set by rxvt, konsole, and a few
others, absent everywhere else, and absence falls through to SAFE.

--------------------------------------------------------------------------
WHAT IS COLOURED AND WHAT DELIBERATELY IS NOT
--------------------------------------------------------------------------
EMPHASIS STAYS A WEIGHT. `primary` is bold in every palette, never an
explicit near-white or near-black, because that is the one role a wrong
theme could make invisible rather than merely faint. Bold cannot be wrong
about a background it has not been told about. The blast radius of guessing
wrong is therefore confined to furniture: quieter than intended, never gone.

COLOUR IS STILL NEVER THE ONLY CARRIER. Every state keeps its glyph and its
word (see display._MARKS). Strip every escape sequence and the screen says
the same things -- which is what makes all of the above a readability
question rather than a correctness one.

--------------------------------------------------------------------------
CONTRAST, MEASURED RATHER THAN EYEBALLED
--------------------------------------------------------------------------
Every value below was chosen against WCAG 2.1 relative-luminance ratios, and
the numbers are in the tables. Backgrounds validated against:

    dark    #1e1e1e (the common editor/terminal dark), #000000, #282c34
    light   #ffffff, #f5f5f5, #fdf6e3 (solarized light)

Every TEXT role clears 4.5:1 (AA) on all three of its backgrounds except
`faint`, which is furniture -- rules, box edges, the receipt line under a
choice -- and clears 3:1. The DNA roles are a depth ramp on a logo: the
front strand clears 4.5:1, the turn 3:1, and the strand drawn as being
BEHIND the axis is deliberately the lowest-contrast thing on the screen,
because that is what "further away" looks like. It carries no meaning.

A terminal's real background is whatever the person set it to, so none of
this is a guarantee. It is the difference between choosing and hoping.
"""
import os

# ---------------------------------------------------------------------------
# The palettes, as RGB. Ratios in the trailing comments are against the three
# backgrounds listed above, in the order given there.
# ---------------------------------------------------------------------------

DARK = {
    "secondary": (0xa8, 0xc0, 0xd8),   # 8.89  11.20  7.46
    "muted":     (0x9a, 0xa4, 0xb2),   # 6.61   8.33  5.55
    "faint":     (0x6f, 0x77, 0x83),   # 3.69   4.64  3.09
    "success":   (0x5f, 0xd1, 0x8a),   # 8.71  10.97  7.32
    "warning":   (0xe3, 0xb3, 0x41),   # 8.57  10.79  7.19
    "error":     (0xff, 0x7b, 0x72),   # 6.61   8.33  5.55
    "focus":     (0x79, 0xb8, 0xff),   # 8.03  10.11  6.74
    "dna_fg":    (0x7e, 0xe7, 0x87),   # 10.85 13.67  9.11
    "dna_mid":   (0x46, 0xb9, 0x5c),   # 6.63   8.35  5.57
    "dna_bg":    (0x32, 0x8f, 0x40),   # 4.08   5.14  3.43
    "dna_rung":  (0x88, 0x99, 0xad),   # 5.72   7.21  4.80
}

LIGHT = {
    "secondary": (0x2b, 0x4a, 0x72),   # 9.03   8.28  8.37
    "muted":     (0x56, 0x5f, 0x6b),   # 6.47   5.94  6.00
    "faint":     (0x76, 0x7e, 0x8a),   # 4.10   3.76  3.80
    "success":   (0x12, 0x7a, 0x37),   # 5.43   4.98  5.04
    # Brown, not yellow. Yellow is the one hue with no readable form on white:
    # ANSI 33 and every bright variant of it sit under 2:1 there, which is what
    # made the ONE state that is waiting on a person the hardest row to see.
    "warning":   (0x8a, 0x53, 0x00),   # 6.33   5.81  5.87
    "error":     (0xb3, 0x26, 0x1e),   # 6.54   6.00  6.06
    "focus":     (0x0a, 0x58, 0xca),   # 6.44   5.91  5.97
    "dna_fg":    (0x0b, 0x6b, 0x2d),   # 6.66   6.10  6.17
    "dna_mid":   (0x2b, 0x91, 0x49),   # 4.00   3.67  3.71
    # The receding strand, and the only value here under 3:1 anywhere. On a
    # light ground "further away" is LIGHTER, so the depth ramp has to run the
    # other way from the dark palette's. Decorative; carries nothing.
    "dna_bg":    (0x6c, 0xb8, 0x86),   # 2.38   2.18  2.20
    "dna_rung":  (0x4d, 0x64, 0x80),   # 6.09   5.58  5.64
}

# The background-agnostic palette, expressed in basic ANSI. This is what the
# app shipped before there were two, and it is what anybody who sets nothing
# still gets -- see the precedence note above.
SAFE = {
    "secondary": "\033[90m",
    "muted":     "\033[90m",
    "faint":     "\033[2m",
    "success":   "\033[32m",
    "warning":   "\033[1;33m",
    "error":     "\033[31m",
    "focus":     "\033[32m",
    "dna_fg":    "\033[1;32m",
    "dna_mid":   "\033[32m",
    "dna_bg":    "\033[2;32m",
    "dna_rung":  "\033[2;90m",
}

ROLES = tuple(SAFE)

NONE = "none"


def colour_wanted(env=None):
    """Should anything be coloured at all?

    NO_COLOR is the informal standard (no-color.org): any value, even empty,
    means do not emit colour. TERM=dumb is the older one. Both are honoured
    because the alternative is escape sequences landing in a log file, and a
    person who has set either has already said what they want.

    Deliberately NOT conditioned on isatty(). Output is piped constantly here
    -- `| tee`, `| less -R`, the test harness -- and stripping colour from a
    pipe would take it away from `less -R`, which handles it perfectly well.
    display.fit() and cells() already discount escape sequences when
    measuring, so a redirected screen keeps its layout either way.
    """
    env = os.environ if env is None else env
    if env.get("NO_COLOR") is not None:
        return False
    return env.get("TERM", "") != "dumb"


def _from_colorfgbg(value):
    """'light' / 'dark' / None, from a COLORFGBG value like '15;0' or '0;default;15'.

    The variable is "foreground;background" (sometimes with a middle field),
    both as ANSI colour numbers. The background is the LAST field, and the
    only question asked of it is whether it is one of the dark half of the
    basic sixteen. Anything unparseable answers None rather than guessing,
    which lands on SAFE.
    """
    parts = [p.strip() for p in str(value or "").split(";")]
    # At least two fields, and the last one non-empty. "15;" is a foreground
    # with the background missing, and reading its ONE field as a background is
    # how a light terminal gets a dark palette: the answer would be the
    # foreground colour, which is the opposite of what was asked.
    if len(parts) < 2 or not parts[-1]:
        return None
    back = parts[-1]
    if not back.isdigit():
        return None
    n = int(back)
    if n > 15:
        return None
    # 0-6 and 8 are the dark half (black, red, green, yellow, blue, magenta,
    # cyan, bright black). 7 and 9-15 are the light half.
    return "dark" if n in (0, 1, 2, 3, 4, 5, 6, 8) else "light"


def resolve(env=None):
    """Which palette this session gets: 'dark', 'light', 'safe' or 'none'.

    Pure: reads an environment mapping and returns a name. Never writes, never
    opens a terminal, never blocks.
    """
    env = os.environ if env is None else env
    if not colour_wanted(env):
        return NONE
    wanted = (env.get("GENPIPE_THEME") or "").strip().lower()
    if wanted in ("dark", "light"):
        return wanted
    if wanted in ("", "auto"):
        detected = _from_colorfgbg(env.get("COLORFGBG"))
        if detected:
            return detected
    return "safe"


def _truecolour(env):
    return (env.get("COLORTERM") or "").lower() in ("truecolor", "24bit")


def _256(env):
    term = (env.get("TERM") or "").lower()
    return "256color" in term or "direct" in term


def _xterm256(rgb):
    """The nearest xterm-256 index for an RGB triple.

    The 6x6x6 colour cube (16-231) and the 24-step grey ramp (232-255), both
    tried, nearest wins. Terminals that advertise 256 colours but not
    truecolour are still common over ssh, and this is a much closer answer
    than falling all the way back to the basic eight.
    """
    steps = (0, 95, 135, 175, 215, 255)

    def near(v, table):
        return min(range(len(table)), key=lambda i: abs(table[i] - v))

    idx = [near(v, steps) for v in rgb]
    cube = (16 + 36 * idx[0] + 6 * idx[1] + idx[2],
            tuple(steps[i] for i in idx))
    grey_level = near(sum(rgb) // 3, [8 + 10 * i for i in range(24)])
    grey = (232 + grey_level, (8 + 10 * grey_level,) * 3)

    def dist(pair):
        return sum((a - b) ** 2 for a, b in zip(rgb, pair[1]))

    return min((cube, grey), key=dist)[0]


def sgr(rgb, env=None):
    """The escape sequence for one RGB triple, at the best depth available."""
    env = os.environ if env is None else env
    r, g, b = rgb
    if _truecolour(env):
        return f"\033[38;2;{r};{g};{b}m"
    if _256(env):
        return f"\033[38;5;{_xterm256(rgb)}m"
    return None


def palette(env=None):
    """{role: escape sequence} for this session, plus the name under 'theme'.

    Falls back a role at a time rather than all at once: a terminal with no
    truecolour and no 256-colour support gets SAFE's basic-ANSI value for
    every role, which is exactly what it can render.
    """
    env = os.environ if env is None else env
    name = resolve(env)
    if name == NONE:
        out = {role: "" for role in ROLES}
    elif name == "safe":
        out = dict(SAFE)
    else:
        rgb = DARK if name == "dark" else LIGHT
        out = {role: (sgr(rgb[role], env) or SAFE[role]) for role in ROLES}
    out["theme"] = name
    return out
