"""Security policy: what a read-only role may run, and where web_fetch may go."""

from __future__ import annotations

import pytest

from guild.tools.policy import blocked_url, readonly_allowed


@pytest.mark.parametrize(
    "cmd",
    [
        "ls & touch pwned",  # single & is a separator in sh and cmd.exe
        "ls -la\ntouch pwned",  # newline is a separator
        "ls -la\r\ntouch pwned",
        "find . -delete",
        "find . -name '*.py' -exec rm {} +",
        "find . -fprint pwned",
        "git branch -D main",
        "git branch -m main other",
        "git branch newbranch",
        "sort -o app.py app.py",
        "sort --output=app.py app.py",
        "uniq app.py pwned",
        "tree -o pwned",
        "git diff --output=pwned",
        "git log --output=pwned",
        "git grep -O sh x",
        "rg --pre ./evil x",
        "ruff check --add-noqa",
        "pytest --basetemp=.",
        "pytest -p evil",
        "git diff --ext-diff",
        "echo x > app.py",
        "python -c \"open('a','w').write('x')\"",
        "sed -i s/a/b/ app.py",
        "git reset --hard",
        "pip install requests",
        "del app.py",
        "pytest -q; rm -rf src",
        "ruff check --fix .",
    ],
)
def test_readonly_allowlist_refuses_bypasses(cmd):
    assert not readonly_allowed(cmd), cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "pytest -q",
        "python -m pytest -q tests/test_app.py",
        "pytest -q | tail -20",
        "git log --oneline -5",
        "git branch",
        "git branch -a",
        "git branch --show-current",
        "find . -name '*.py'",
        "sort app.py",
        "uniq -c app.py",
        "git diff HEAD~1",
        "ls -la",
        "git log --oneline",
        "rg TODO src",
    ],
)
def test_readonly_allowlist_still_allows_inspection(cmd):
    assert readonly_allowed(cmd), cmd


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:7331/api/state",
        "http://localhost:11434/api/tags",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/",
        "http://192.168.1.1/admin",
        "file:///etc/passwd",
        "ftp://example.com/x",
    ],
)
def test_web_fetch_refuses_local_and_private_addresses(url):
    assert blocked_url(url), url
