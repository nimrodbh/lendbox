"""Staying awake on free hosting. Pure rules: no database needed."""

from __future__ import annotations

import os
import subprocess
import sys
import urllib.error

import pytest

from lendbox.web import app as web


@pytest.mark.parametrize(
    ("base_url", "enabled", "expected"),
    [
        ("https://lendbox.example", True, "https://lendbox.example/ping"),
        ("https://lendbox.example/", True, "https://lendbox.example/ping"),
        ("https://lendbox.example", False, None),   # switched off
        ("http://localhost:8000", True, None),      # a call that never leaves the machine wakes nobody
        ("http://127.0.0.1:8000", True, None),
        ("http://[::1]:8000", True, None),
        ("ftp://lendbox.example", True, None),      # only web addresses
        ("", True, None),
    ],
)
def test_keep_awake_url(base_url, enabled, expected):
    assert web.keep_awake_url(base_url, enabled=enabled) == expected


def test_the_app_calls_itself_before_the_host_would_put_it_to_sleep():
    host_sleeps_after = 15 * 60
    assert web.KEEP_AWAKE_EVERY_SECONDS < host_sleeps_after


def test_a_failed_ping_is_reported_not_raised(monkeypatch):
    def unreachable(url, timeout):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(web.urllib.request, "urlopen", unreachable)
    assert web._ping_self("https://lendbox.example/ping") is False


def test_a_good_ping_is_reported(monkeypatch):
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    asked: list[str] = []
    monkeypatch.setattr(web.urllib.request, "urlopen", lambda url, timeout: asked.append(url) or Response())
    assert web._ping_self("https://lendbox.example/ping") is True
    assert asked == ["https://lendbox.example/ping"]


@pytest.mark.parametrize(
    ("env", "base_url", "keep_awake"),
    [
        ({"LENDBOX_DEMO": "1", "RENDER_EXTERNAL_URL": "https://demo.example"}, "https://demo.example", True),
        ({"LENDBOX_DEMO": "1", "LENDBOX_BASE_URL": "https://mine.example/", "RENDER_EXTERNAL_URL": "https://x.example"},
         "https://mine.example", True),                       # an address set by hand wins over the host's
        ({"LENDBOX_DEMO": "1", "LENDBOX_KEEP_AWAKE": "0"}, "http://localhost:8000", False),  # can be switched off
        ({}, "http://localhost:8000", False),                 # a real deployment decides for itself
    ],
)
def test_public_address_and_keep_awake_come_from_the_environment(env, base_url, keep_awake):
    ours = ("LENDBOX_DEMO", "LENDBOX_BASE_URL", "LENDBOX_KEEP_AWAKE", "RENDER_EXTERNAL_URL")
    clean = {k: v for k, v in os.environ.items() if k not in ours}
    code = "import lendbox.web.app as a; print(a.BASE_URL, a.KEEP_AWAKE)"
    run = subprocess.run([sys.executable, "-c", code], check=True, env={**clean, **env}, capture_output=True, text=True)
    assert run.stdout.split() == [base_url, str(keep_awake)]
