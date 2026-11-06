"""JSON schemas for the Look At This tools."""

from __future__ import annotations

_FIELDS = ["screenshot", "accessibility", "url", "selection"]

GRAB = {
    "type": "object",
    "properties": {
        "app": {
            "type": "string",
            "description": (
                "Capture this window instead of the one beneath Hermes: a process name "
                "(chrome.exe) or any substring of the window title."
            ),
        },
        "fields": {
            "type": "array",
            "items": {"type": "string", "enum": _FIELDS},
            "description": (
                "Which fields to include. Default: all four. The app and window title "
                "are always included so the model knows what it is looking at."
            ),
        },
    },
    "additionalProperties": False,
}

STATUS = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

CLEAR = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}
