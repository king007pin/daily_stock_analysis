# -*- coding: utf-8 -*-
"""The offline suite must stay offline whatever a test does to the environment.

``conftest`` isolates the developer's real ``.env`` two ways: it points ``ENV_FILE`` at a
path that does not exist, and it undoes any of that file's keys that a test leaks into
``os.environ``. Both are needed. Around a dozen test files pop ``ENV_FILE`` instead of
restoring it, and at least one loads the repo ``.env`` straight into the process, which
no ``ENV_FILE`` setting can undo.

Nothing failed when that broke. On 2026-09-01 three opt-in flags were switched on in that
``.env`` and the suite began making real network calls — 12 minutes instead of 210
seconds, still green. These tests run in file order and pin both repairs.
"""

import os
from pathlib import Path

import pytest

ISOLATED_SUFFIX = "__absent__.env"
LEAKY_KEY = "DECISION_OUTCOME_BENCHMARK_ENABLED"

_REPO_ENV = Path(__file__).resolve().parent.parent / ".env"
# CI has no .env, so there is nothing to leak and nothing for the guard to undo.
_KEY_IS_LOCAL = _REPO_ENV.exists() and any(
    line.strip().startswith(f"{LEAKY_KEY}=") for line in _REPO_ENV.read_text(encoding="utf-8").splitlines()
)


def test_a_test_may_clear_env_file_the_way_a_teardown_does():
    os.environ.pop("ENV_FILE", None)
    assert "ENV_FILE" not in os.environ


def test_the_next_test_still_sees_the_isolated_env_file():
    """If this fails, every test from here on reads the real .env."""
    assert os.environ.get("ENV_FILE", "").endswith(ISOLATED_SUFFIX)


@pytest.mark.skipif(not _KEY_IS_LOCAL, reason="no local .env defines this key; nothing can leak")
def test_a_test_may_leak_a_repo_env_key_into_the_process():
    os.environ[LEAKY_KEY] = "true"
    assert os.environ[LEAKY_KEY] == "true"


@pytest.mark.skipif(not _KEY_IS_LOCAL, reason="no local .env defines this key; nothing can leak")
def test_the_next_test_does_not_inherit_the_leaked_key():
    """The environment outranks ENV_FILE, so this is the leak that actually went online."""
    assert os.environ.get(LEAKY_KEY) is None
