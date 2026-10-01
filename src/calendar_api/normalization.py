from __future__ import annotations

import re

from .errors import ValidationError


def normalize_contact(channel: str, value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if channel == "email":
        if value.count("@") != 1 or any(char.isspace() for char in value):
            raise ValidationError("submitter email is invalid")
        return value.lower()

    compact = re.sub(r"[\s().-]", "", value)
    if not re.fullmatch(r"\+[1-9][0-9]{7,14}", compact):
        raise ValidationError("submitter SMS number must be in E.164 format")
    return compact
