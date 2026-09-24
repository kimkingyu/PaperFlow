"""Conservative journal identity matching; no network or disk side effects."""
from __future__ import annotations

import re
import unicodedata
import uuid

from .models import JournalError


def normalize_name(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").casefold()
    return " ".join(value.replace("\u2013", "-").replace("\u2014", "-").split())


def normalize_issn(value: str) -> str:
    compact = re.sub(r"[\s-]", "", value or "").upper()
    if not re.fullmatch(r"\d{7}[\dX]", compact):
        raise JournalError("INVALID_ISSN", "ISSN 格式无效，应为四位-四位")
    digits = [int(c) for c in compact[:7]] + [10 if compact[-1] == "X" else int(compact[-1])]
    if sum(v * (8 - i) for i, v in enumerate(digits)) % 11:
        raise JournalError("INVALID_ISSN", "ISSN 校验位无效")
    return compact[:4] + "-" + compact[4:]


def identity_id(title: str, kind: str, issns=None) -> str:
    key = "issn:" + sorted(issns)[0] if issns else "title:" + kind + ":" + normalize_name(title)
    return "j_" + uuid.uuid5(uuid.NAMESPACE_URL, key).hex[:24]
