"""Read JSON the same way everywhere, and find values PostgreSQL cannot store.

Fetch, resume and load all read server answers with `loads`, so a byte-order
mark, broken UTF-8 or a deeply nested answer gets the same verdict everywhere.

Python's JSON reader accepts some values that PostgreSQL refuses: NaN and
Infinity, the NUL character, and lone surrogate escapes such as "\\ud800".
`unstorable` finds them, so a scene holding one is refused with a reason
instead of crashing the load. `storable_copy` makes a copy that can be kept
as evidence in the reject table.
"""

from __future__ import annotations

import json
import math
import unicodedata
from typing import Any

MAX_TEXT = 300  # server text shown in messages is cut to this length


def loads(data: bytes | str) -> Any:
    """Parse JSON from bytes or text. Raises ValueError for anything unreadable."""
    text = data.decode("utf-8") if isinstance(data, bytes) else data  # UnicodeDecodeError is a ValueError
    if text.startswith("﻿"):
        text = text[1:]  # RFC 8259 allows a reader to ignore a byte-order mark
    try:
        return json.loads(text)
    except RecursionError:
        raise ValueError("the JSON is nested too deeply to read") from None


def _text_problem(text: str) -> str | None:
    if "\x00" in text:
        return "contains a NUL character"
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return "contains a broken character (a lone surrogate)"
    return None


def unstorable(value: Any, path: str = "$") -> str | None:
    """Where the first value is that PostgreSQL cannot store, or None if there is none."""
    stack = [(value, path)]
    while stack:  # a loop, not recursion, so deep values cannot overflow the stack
        current, where = stack.pop()
        if isinstance(current, float) and not math.isfinite(current):
            return f"{where} is {current}, which PostgreSQL cannot store"
        if isinstance(current, str):
            problem = _text_problem(current)
            if problem:
                return f"{where} {problem}"
        elif isinstance(current, dict):
            for key, item in current.items():
                problem = _text_problem(key) if isinstance(key, str) else None
                if problem:
                    return f"a key in {where} {problem}"
                stack.append((item, f"{where}.{printable(str(key), 40)}"))
        elif isinstance(current, list):
            stack.extend((item, f"{where}[{index}]") for index, item in enumerate(current))
    return None


def _storable_text(text: str) -> str:
    return text.replace("\x00", "\\u0000").encode("utf-8", "backslashreplace").decode("utf-8")


def storable_copy(value: Any) -> Any:
    """A copy PostgreSQL can store: NaN and Infinity become text, NUL and lone surrogates become escapes."""
    root: list[Any] = [None]
    stack: list[tuple[Any, Any, Any]] = [(value, root, 0)]
    while stack:
        current, parent, slot = stack.pop()
        if isinstance(current, float) and not math.isfinite(current):
            parent[slot] = str(current)
        elif isinstance(current, str):
            parent[slot] = _storable_text(current)
        elif isinstance(current, dict):
            copy: dict[str, Any] = {}
            parent[slot] = copy
            for key, item in current.items():
                new_key = _storable_text(key) if isinstance(key, str) else key
                copy[new_key] = None
                stack.append((item, copy, new_key))
        elif isinstance(current, list):
            items: list[Any] = [None] * len(current)
            parent[slot] = items
            stack.extend((item, items, index) for index, item in enumerate(current))
        else:
            parent[slot] = current
    return root[0]


def printable(text: str, limit: int = MAX_TEXT) -> str:
    """Server text made safe for a terminal or a log: control characters become visible escapes."""
    out = []
    for char in text[:limit]:
        if unicodedata.category(char)[0] == "C" and char != " ":
            out.append(f"\\x{ord(char):02x}" if ord(char) < 0x100 else f"\\u{ord(char):04x}")
        else:
            out.append(char)
    return "".join(out) + ("..." if len(text) > limit else "")
