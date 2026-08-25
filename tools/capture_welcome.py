#!/usr/bin/env python3
"""Print the welcome screen exactly as the app draws it, plus the waiting
prompt, with \x00 marking where the caret sits.

The prompt block is rebuilt from ui.py's own constants rather than copied as
text: _MARK, the rule and the empty-line hint all come from the module that
draws them, so a change to any of them shows up in the next picture instead of
quietly making it a lie.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("COLUMNS", "106")
os.environ.setdefault("LINES", "40")

from genpipe import display, ui   # noqa: E402  (after COLUMNS is set)

display.banner(source=os.environ.get("GENPIPE_LLM_SOURCE", "anthropic"),
               model=os.environ.get("GENPIPE_MODEL", "claude-sonnet-5"))
display.welcome()

span = ui.span_for(int(os.environ["COLUMNS"]))
rule = " " + display.DIM + "─" * span + display.RESET
print()
print(rule)
print(f"  {display.BOLD}{display.GREEN}{ui._MARK}{display.RESET} \x00")
print(rule)
print(f"    {display.DIM}type a task, or{display.RESET}"
      f" {display.GREEN}/{display.RESET}"
      f"{display.DIM} for commands · tab completes · "
      f"↑ for history{display.RESET}")
