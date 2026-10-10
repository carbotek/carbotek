"""Shared iterator segment grammar for template loop planning."""

import re

LOOP_SEGMENT = re.compile(
    r"(?P<field>[^[]*)\[\s*(?P<name>[A-Za-z_][\w.]*?)(?P<suffix>\+1|\+\+)?(?P<repeat>\*[\w.]+)?\s*"
    r"(?:,(?P<filters>[^\]]*))?\]"
)
