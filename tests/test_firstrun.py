"""Tests for the welcome panel's "show once" flag.

The behaviour is trivial; the reason it has tests is that the failure mode is
invisible. A flag that never gets written means the welcome greets the user
on every single launch, and nothing in the app would report that as an error.
"""
import os

from utils import firstrun


def test_a_fresh_profile_has_not_seen_it(tmp_path):
    assert firstrun.seen(str(tmp_path)) is False


def test_marking_it_seen_persists(tmp_path):
    firstrun.mark_seen(str(tmp_path))
    assert firstrun.seen(str(tmp_path)) is True
    # Persisted as a file, not in memory: a second look from scratch agrees.
    assert os.path.exists(firstrun.marker_path(str(tmp_path)))


def test_marking_twice_is_harmless(tmp_path):
    firstrun.mark_seen(str(tmp_path))
    firstrun.mark_seen(str(tmp_path))
    assert firstrun.seen(str(tmp_path)) is True


def test_reset_brings_it_back(tmp_path):
    firstrun.mark_seen(str(tmp_path))
    firstrun.reset(str(tmp_path))
    assert firstrun.seen(str(tmp_path)) is False


def test_reset_on_a_profile_that_never_saw_it_does_not_raise(tmp_path):
    firstrun.reset(str(tmp_path))  # must not raise


def test_an_unwritable_directory_does_not_raise(tmp_path):
    # Dismissing the panel must never fail out of a click handler. The cost
    # of a failed write is that the panel returns once more, which is a mild
    # annoyance; raising would be worse than the thing it guards against.
    firstrun.mark_seen(str(tmp_path / "does-not-exist"))
    assert firstrun.seen(str(tmp_path / "does-not-exist")) is False


def test_the_marker_lives_beside_the_rest_of_the_app_data(tmp_path):
    # The whole point of a file over a registry key: deleting the app-data
    # folder resets the app, including this.
    path = firstrun.marker_path(str(tmp_path))
    assert os.path.dirname(path) == str(tmp_path)
    assert os.path.basename(path) == firstrun.MARKER_NAME
