"""String catalog. Every user-facing sentence lives in <lang>.json, so Hebrew is a content task, not a code task."""

from __future__ import annotations

import json
from functools import cache
from importlib import resources

DEFAULT_LANG = "en"


@cache
def catalog(lang: str = DEFAULT_LANG) -> dict[str, str]:
    return json.loads((resources.files("lendbox.i18n") / f"{lang}.json").read_text(encoding="utf-8"))


def t(key: str, lang: str = DEFAULT_LANG, **values: object) -> str:
    """The sentence for `key`, with {placeholders} filled. A missing key raises: a typo must fail tests, not ship."""
    return catalog(lang)[key].format(**values)


def has(key: str, lang: str = DEFAULT_LANG) -> bool:
    return key in catalog(lang)
