"""Keep legal drafts structurally complete, concise and free of private contact data.

These text-only checks need neither the application nor a database. They do not
establish legal sufficiency; the drafts still need human legal review.
"""
from __future__ import annotations

import re
from html import unescape
from pathlib import Path

import pytest

_LEGAL = Path(__file__).resolve().parents[1] / "lendbox" / "web" / "templates" / "legal"
_PAGES = ("terms", "privacy", "guidelines")
_SECTIONS = {
    "terms": (
        "what-lendbox-is", "your-account", "listing", "borrowing", "money-and-deposits", "prohibited",
        "safety", "reviews", "ending", "liability", "changes", "contact",
    ),
    "privacy": ("what-we-store", "who-sees-what", "why", "how-long", "your-rights", "security", "contact"),
    "guidelines": ("listing", "deposits", "requests", "handover", "returns", "messages", "problems", "reviews"),
}
_LIMITS = {"terms": 1400, "privacy": 1000, "guidelines": 700}
_JINJA = re.compile(r"{%.*?%}|{{.*?}}|{#.*?#}", re.DOTALL)


def _read(page: str) -> str:
    """Read a draft relative to the integrated repository root."""
    return (_LEGAL / f"{page}.html").read_text(encoding="utf-8")


def _words(source: str) -> list[str]:
    """Count visible text words, omitting HTML markup and Jinja expressions."""
    without_jinja = _JINJA.sub(" ", source)
    without_comments = re.sub(r"<!--.*?-->", " ", without_jinja, flags=re.DOTALL)
    visible = unescape(re.sub(r"<[^>]*>", " ", without_comments))
    return re.findall(r"\b\w+(?:['-]\w+)*\b", visible)


@pytest.mark.parametrize("page", _PAGES)
def test_page_extends_base_template(page: str) -> None:
    assert re.match(r'\s*{%\s*extends\s+"base\.html"\s*%}', _read(page))


@pytest.mark.parametrize("page", _PAGES)
def test_page_defines_title_and_content_blocks(page: str) -> None:
    source = _read(page)
    assert len(re.findall(r"{%\s*block\s+title\s*%}", source)) == 1
    assert len(re.findall(r"{%\s*block\s+content\s*%}", source)) == 1
    assert len(re.findall(r"{%\s*endblock\s*%}", source)) == 2


@pytest.mark.parametrize("page", _PAGES)
def test_content_is_wrapped_in_narrow_stack(page: str) -> None:
    assert re.search(r'{%\s*block\s+content\s*%}\s*<div class="narrow stack">', _read(page))


@pytest.mark.parametrize("page,section", [(page, section) for page in _PAGES for section in _SECTIONS[page]])
def test_required_section_has_a_unique_h2_id(page: str, section: str) -> None:
    source = _read(page)
    assert len(re.findall(rf'<h2\s+id="{re.escape(section)}"\s*>[^<]+</h2>', source)) == 1
    assert len(re.findall(rf'\bid="{re.escape(section)}"', source)) == 1


@pytest.mark.parametrize("page", _PAGES)
def test_section_order_matches_specification(page: str) -> None:
    assert re.findall(r'<h2\s+id="([^"]+)"', _read(page)) == list(_SECTIONS[page])


@pytest.mark.parametrize("page", _PAGES)
def test_page_contains_no_em_or_en_dash(page: str) -> None:
    assert not {"\u2013", "\u2014"} & set(_read(page))


@pytest.mark.parametrize("page", _PAGES)
def test_page_contains_no_http(page: str) -> None:
    assert "http" not in _read(page).lower()


@pytest.mark.parametrize("page", _PAGES)
def test_page_contains_no_phone_number_pattern(page: str) -> None:
    assert re.search(r"\d{7,}", _read(page)) is None


@pytest.mark.parametrize("page", _PAGES)
def test_page_contains_no_links(page: str) -> None:
    assert re.search(r"<\s*a\b|\bhref\s*=", _read(page), flags=re.IGNORECASE) is None


@pytest.mark.parametrize("page", ("terms", "privacy"))
def test_contact_email_is_a_setting_placeholder(page: str) -> None:
    source = _read(page)
    assert "{{ contact_email }}" in source
    assert "@" not in source


@pytest.mark.parametrize("page", _PAGES)
def test_visible_word_count_is_within_limit(page: str) -> None:
    count = len(_words(_read(page)))
    assert 0 < count <= _LIMITS[page], f"{page}: {count} words exceeds {_LIMITS[page]}"


@pytest.mark.parametrize("page", _PAGES)
def test_first_paragraph_is_one_summary_notice_before_sections(page: str) -> None:
    source = _read(page)
    paragraph = re.search(r'<p\b[^>]*>.*?</p>', source, flags=re.DOTALL)
    assert paragraph is not None
    assert paragraph.group().startswith('<p class="notice info">')
    assert len(re.findall(r"<p\b", paragraph.group())) == 1
    assert len(_words(paragraph.group())) > 0
    assert paragraph.end() < source.index('<h2 id="')


@pytest.mark.parametrize("page", _PAGES)
def test_page_ends_with_last_updated_placeholder(page: str) -> None:
    assert re.search(
        r'<p class="small muted">Last updated {{ updated }}</p>\s*</div>\s*{%\s*endblock\s*%}\s*$',
        _read(page),
    )
