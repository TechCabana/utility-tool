# tests/test_links_not_followed.py
# Card tiha5I8F: a symlink or junction is deliberately never followed (a junction
# to an ancestor would recurse until the disk fills), but it used to be dropped
# without a word, so a Documents folder holding a junction to data on another
# drive read "verified every copied file" while that data was in no version. The
# walk now counts and names them (relative paths, capped at five) in the result,
# the stored job, the run log (a count only) and the headless run, and the job
# reads "OK, N shortcut(s) not followed" like a skipped file does.
#
# Real junctions, made with the OS call `mklink /J` makes (no privilege needed);
# real symlinks where this account may create them (WinError 1314 skips cleanly).
# Every link a test makes is removed with rmdir/unlink on teardown (never a
# recursive delete through it) and the fixture asserts the folder it pointed at
# is exactly as it was. Faults are patched on the module that owns them.
import json
import os

import pytest

from utils import backup_utils, copying, fs_utils, jobs_store, runlog, runner

windows_only = pytest.mark.skipif(os.name != "nt", reason="junctions are Windows-only")
EXTENDED = "\\\\?\\"

REAL = ["real.txt", "docs/a.txt", "docs/b.txt"]
SECRET = "secret-inside-the-link-target.txt"


@pytest.fixture(autouse=True)
def isolated_jobs_file(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_store, "JOBS_FILE", str(tmp_path / "backup_jobs.json"))
    yield


def _snapshot(folder):
    """`{relative path: size}` of every file under `folder`, never entering a link."""
    found = {}
    if not os.path.isdir(folder):
        return None
    for current, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs
                   if not (os.path.islink(os.path.join(current, d))
                           or os.path.isjunction(os.path.join(current, d)))]
        for name in files:
            path = os.path.join(current, name)
            found[os.path.relpath(path, folder)] = os.path.getsize(path)
    return found


class Linker:
    """Makes links and removes them again, proving the targets were left alone."""

    def __init__(self):
        self.made = []       # (link as given to the OS, is_dir)
        self.guard = {}      # target -> its files when the link was made

    def _register(self, link, target, is_dir):
        self.made.append((link, is_dir))
        self.guard.setdefault(str(target), _snapshot(str(target)))

    def junction(self, link, target):
        """A Windows junction at `link` pointing at folder `target`."""
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
        self._register(str(link), target, True)

    def symlink(self, link, target, folder):
        """A symlink; skips the test when this account may not make one."""
        try:
            os.symlink(str(target), str(link), target_is_directory=folder)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"cannot create a symlink here (no privilege): {exc}")
        self._register(str(link), target, folder)

    def close(self):
        """Remove every link itself; return the targets that changed."""
        for link, is_dir in reversed(self.made):
            try:
                (os.rmdir if is_dir else os.unlink)(link)
            except OSError:
                pass
        return [t for t, before in self.guard.items() if _snapshot(t) != before]


@pytest.fixture
def linker():
    made = Linker()
    yield made
    assert made.close() == [], "a link's target was changed"
    for link, _ in made.made:
        assert not os.path.lexists(link), f"link left behind: {link}"


def make_source(root):
    """Three real files, one at the top and two in `docs/`."""
    (root / "docs").mkdir(parents=True)
    for rel in REAL:
        (root / rel).write_bytes(f"contents of {rel}".encode() * 40)
    return root


def make_elsewhere(root):
    """A folder the links will point at, holding files that must never be copied."""
    (root / "nested").mkdir(parents=True)
    (root / SECRET).write_bytes(b"keep out of the version" * 40)
    (root / "nested" / "deep-secret.txt").write_bytes(b"nor this" * 40)
    return root


def make_job(tmp_path, **options):
    """A saved job over `make_source`; `tmp_path/elsewhere` exists for links."""
    source = make_source(tmp_path / "src")
    make_elsewhere(tmp_path / "elsewhere")
    target = tmp_path / "dst"
    target.mkdir()
    job = backup_utils.new_job("Links", str(source), str(target), schedule="manual",
                               retention=3, **options)
    backup_utils.save_job(job)
    return job


@pytest.fixture
def job(tmp_path):
    return make_job(tmp_path)


def run_and_record(job, **kwargs):
    result = backup_utils.run_job(job, **kwargs)
    backup_utils.record_run(job["id"], result)
    return result, backup_utils.get_job(job["id"])


def version_files(job, result):
    """Sorted relative paths of every file in the promoted version."""
    root = backup_utils.version_path(job, result["version"])
    return sorted(os.path.relpath(os.path.join(c, f), root).replace("\\", "/")
                  for c, _, files in os.walk(root) for f in files)


def manifest_paths(result):
    with open(result["manifest"], encoding="utf-8") as handle:
        return sorted(e["path"] for e in map(json.loads, handle) if e["type"] == "file")


def run_log(job):
    """The job's run-log lines, oldest first."""
    return list(reversed(runlog.read_runs(job["id"])))


CARD = ("{n} shortcut folders point somewhere else and were not followed: {names}. "
        "Add those locations as their own backup.")


# --- the Done-when: a link is named, its contents stay out, a clean source says none -----

@windows_only
def test_a_junction_is_named_as_not_followed_and_its_contents_stay_out_of_the_version(
        tmp_path, job, linker):
    linker.junction(tmp_path / "src" / "Photos", tmp_path / "elsewhere")
    result, stored = run_and_record(job)

    assert result["ok"], result["message"]
    assert result["links_skipped"] == 1 and result["links_folders"] == 1
    assert result["links_sample"] == ["Photos"]
    assert ("1 shortcut folder points somewhere else and was not followed: Photos. "
            "Add that location as its own backup.") in result["message"]
    assert result["message"].endswith("as its own backup.")
    assert "verified every copied file" in result["message"]      # still true, and still said
    # Do not follow: the version and its checksum list hold only the real files.
    assert version_files(job, result) == sorted(REAL)
    assert manifest_paths(result) == sorted(REAL)
    assert SECRET not in open(result["manifest"], encoding="utf-8").read()
    assert not os.path.exists(os.path.join(
        backup_utils.version_path(job, result["version"]), "Photos"))
    # A link is not a file: the file count the sanity rules use is unchanged.
    assert result["files"] == len(REAL) and result["errors"] == [] and result["not_copied"] == 0
    assert stored["last_result"] == "ok"
    assert stored["last_links"] == 1 and stored["last_links_sample"] == ["Photos"]


def test_a_symlink_to_a_folder_is_named_too(tmp_path, job, linker):
    linker.symlink(tmp_path / "src" / "Photos", tmp_path / "elsewhere", folder=True)
    result = backup_utils.run_job(job)

    assert result["ok"] and result["links_skipped"] == 1 and result["links_folders"] == 1
    assert result["links_sample"] == ["Photos"]
    assert version_files(job, result) == sorted(REAL)


def test_a_symlink_to_a_file_is_a_shortcut_file_not_a_folder(tmp_path, job, linker):
    linker.symlink(tmp_path / "src" / "latest.txt",
                   tmp_path / "elsewhere" / SECRET, folder=False)
    result = backup_utils.run_job(job)

    assert result["ok"] and result["links_skipped"] == 1 and result["links_folders"] == 0
    assert result["links_sample"] == ["latest.txt"]
    assert ("1 shortcut file points somewhere else and was not followed: latest.txt. "
            "Add that location as its own backup.") in result["message"]
    assert version_files(job, result) == sorted(REAL)


def test_a_source_with_no_links_reports_none_and_the_same_path_can_report_one(
        tmp_path, job, linker):
    clean, stored = run_and_record(job)
    assert clean["ok"]
    assert clean["links_skipped"] == 0 and clean["links_sample"] == []
    assert clean["links_sentence"] == "" and "shortcut" not in clean["message"]
    assert "not followed" not in clean["message"]
    assert (stored["last_links"], stored["last_links_sample"]) == (0, [])
    assert run_log(job)[-1]["links"] == 0 and run_log(job)[-1]["outcome"] == "ok"

    if os.name != "nt":
        pytest.skip("positive control needs a junction")
    linker.junction(tmp_path / "src" / "Photos", tmp_path / "elsewhere")   # the control
    again, stored = run_and_record(job)
    assert again["links_skipped"] == 1 and stored["last_links"] == 1


# --- wording ----------------------------------------------------------------------------

@pytest.mark.parametrize("folders, files, names, total, expected", [
    (0, 0, [], 0, ""),
    (1, 0, ["Photos"], 1,
     "1 shortcut folder points somewhere else and was not followed: Photos. "
     "Add that location as its own backup."),
    (0, 1, ["a.lnk"], 1,
     "1 shortcut file points somewhere else and was not followed: a.lnk. "
     "Add that location as its own backup."),
    (2, 0, ["Archive", "Photos"], 2, CARD.format(n=2, names="Archive, Photos")),
    (1, 2, ["a", "b", "c"], 3,
     "1 shortcut folder and 2 shortcut files point somewhere else and were not "
     "followed: a, b, c. Add those locations as their own backup."),
    (7, 0, ["a", "b", "c", "d", "e"], 7,
     "7 shortcut folders point somewhere else and were not followed: a, b, c, d, e "
     "and 2 more. Add those locations as their own backup."),
])
def test_the_sentence_reads_like_the_card_for_every_mix(folders, files, names, total, expected):
    assert runner.links_sentence(folders, files, names, total) == expected
    assert backup_utils.links_sentence is runner.links_sentence


@windows_only
def test_two_folder_links_are_named_in_the_cards_wording(tmp_path, job, linker):
    linker.junction(tmp_path / "src" / "Photos", tmp_path / "elsewhere")
    linker.junction(tmp_path / "src" / "Archive", tmp_path / "elsewhere")
    result = backup_utils.run_job(job)

    assert result["links_skipped"] == 2 and result["links_folders"] == 2
    # Sorted by name, so the order is the same on every run and every machine.
    assert result["links_sample"] == ["Archive", "Photos"]
    assert CARD.format(n=2, names="Archive, Photos") in result["message"]
    assert result["links_sentence"] == CARD.format(n=2, names="Archive, Photos")


@windows_only
def test_the_names_are_capped_at_five_but_the_total_is_kept(tmp_path, job, linker):
    names = ["Zed", "alpha", "Beta", "gamma", "delta", "Epsilon", "eta"]
    for name in names:
        linker.junction(tmp_path / "src" / name, tmp_path / "elsewhere")
    result, stored = run_and_record(job)

    five = ["alpha", "Beta", "delta", "Epsilon", "eta"]            # case-insensitive order
    assert result["links_skipped"] == 7 and result["links_sample"] == five
    assert len(result["links_sample"]) == jobs_store.SKIPPED_SAMPLE_MAX == 5
    assert "alpha, Beta, delta, Epsilon, eta and 2 more." in result["message"]
    assert stored["last_links"] == 7 and stored["last_links_sample"] == five
    assert run_log(job)[-1]["links"] == 7


@windows_only
def test_names_are_relative_paths_never_absolute_or_the_target(tmp_path, job, linker):
    (tmp_path / "src" / "docs" / "deeper").mkdir()
    linker.junction(tmp_path / "src" / "docs" / "deeper" / "Photos", tmp_path / "elsewhere")
    result, stored = run_and_record(job)

    assert result["links_sample"] == ["docs/deeper/Photos"]       # forward slashes, from the source root
    blob = json.dumps([result, stored]) + open(jobs_store.JOBS_FILE, encoding="utf-8").read()
    assert str(tmp_path) not in blob and "elsewhere" not in blob and SECRET not in blob


@windows_only
def test_the_walk_that_names_a_link_never_lists_or_opens_it(tmp_path, job, linker, monkeypatch):
    linker.junction(tmp_path / "src" / "Photos", tmp_path / "elsewhere")
    real_scandir, listed = os.scandir, []

    def spy(path="."):
        listed.append(os.fspath(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", spy)
    result = backup_utils.run_job(job)

    assert result["links_sample"] == ["Photos"]
    assert listed and not [p for p in listed
                           if p.rstrip("\\").endswith(("Photos", "elsewhere", "nested"))]


# --- walking: where a link is and is not counted --------------------------------------------

@windows_only
def test_a_junction_to_an_ancestor_is_counted_once_and_never_recursed(tmp_path, job, linker):
    linker.junction(tmp_path / "src" / "docs" / "loop", tmp_path / "src")
    result = backup_utils.run_job(job)

    assert result["ok"], result["message"]
    assert result["links_skipped"] == 1 and result["links_sample"] == ["docs/loop"]
    assert result["files"] == len(REAL)
    assert version_files(job, result) == sorted(REAL)


@windows_only
def test_sort_order_is_the_same_whatever_order_the_os_lists_them(tmp_path, linker):
    source = make_source(tmp_path / "src")
    make_elsewhere(tmp_path / "elsewhere")
    # NTFS lists by upper-cased name ("AB", "ALPHA", "A_B"); the report sorts by
    # case-folded name ("a_b", "ab", "alpha"), so the two orders differ here.
    for name in ("alpha", "a_b", "ab"):
        linker.junction(source / name, tmp_path / "elsewhere")
    stats = {}
    copying.copy_tree(str(source), str(tmp_path / "out"), stats=stats)

    assert stats["links"] == 3 and stats["links_folders"] == 3
    assert stats["link_names"] == ["a_b", "ab", "alpha"]


@windows_only
def test_a_dangling_junction_is_still_named_and_does_not_break_the_run(tmp_path, job, linker):
    gone = tmp_path / "gone"
    gone.mkdir()
    linker.junction(tmp_path / "src" / "Old", gone)
    os.rmdir(gone)                                                # the target vanishes
    linker.guard.pop(str(gone))                                   # on purpose: nothing to protect
    result = backup_utils.run_job(job)

    assert result["ok"], result["message"]
    assert result["links_skipped"] == 1 and result["links_sample"] == ["Old"]


@windows_only
def test_a_link_inside_an_unreadable_folder_is_not_visited_so_not_counted(
        tmp_path, job, linker, monkeypatch):
    locked = tmp_path / "src" / "locked"
    locked.mkdir()
    linker.junction(locked / "Hidden", tmp_path / "elsewhere")
    real_scandir = os.scandir

    def deny(path="."):
        if os.fspath(path).rstrip("\\").endswith("locked"):
            raise PermissionError(13, "Permission denied", os.fspath(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", deny)
    result = backup_utils.run_job(job)
    assert result["ok"] and result["links_skipped"] == 0 and result["links_sample"] == []
    assert result["errors"] and result["errors"][0].startswith("locked:")

    monkeypatch.undo()                                            # positive control: readable
    assert backup_utils.run_job(job)["links_sample"] == ["locked/Hidden"]


@windows_only
def test_a_source_that_is_itself_a_junction_still_copies_and_reports_none(
        tmp_path, linker):
    real_source = make_source(tmp_path / "realsrc")
    make_elsewhere(tmp_path / "elsewhere")
    linker.junction(tmp_path / "srclink", real_source)
    target = tmp_path / "dst"
    target.mkdir()
    job = backup_utils.new_job("Root link", str(tmp_path / "srclink"), str(target),
                               schedule="manual", retention=3)
    backup_utils.save_job(job)
    result = backup_utils.run_job(job)

    # Unchanged behaviour: the root the user picked is read through, as it always was;
    # only links INSIDE the tree are declined, so nothing to report here.
    assert result["ok"], result["message"]
    assert result["files"] == len(REAL) and result["links_skipped"] == 0


def test_the_file_counts_the_sanity_rules_use_do_not_see_links(tmp_path, job, linker):
    if os.name != "nt":
        pytest.skip("needs a junction")
    linker.junction(tmp_path / "src" / "Photos", tmp_path / "elsewhere")
    assert copying.tree_totals(str(tmp_path / "src"))[0] == len(REAL)
    files, unreadable = copying.walk_tree(str(tmp_path / "src"))
    assert sorted(files) == sorted(REAL) and unreadable == 0


@windows_only
def test_a_version_made_from_a_source_with_links_restores_unchanged(tmp_path, job, linker):
    linker.junction(tmp_path / "src" / "Photos", tmp_path / "elsewhere")
    result = backup_utils.run_job(job)
    out = tmp_path / "out"
    out.mkdir()
    restored = backup_utils.restore_version(job, result["version"], str(out))

    assert restored["ok"], restored["message"]
    assert restored["files"] == len(REAL) and restored["verified"] is True
    assert sorted(os.path.relpath(os.path.join(c, f), restored["path"]).replace("\\", "/")
                  for c, _, files in os.walk(restored["path"]) for f in files) == sorted(REAL)
    assert "links_skipped" not in restored                        # a restore reports no links


@windows_only
def test_a_trailing_dot_name_and_a_path_over_260_characters_are_named_too(tmp_path, job, linker):
    source = str(tmp_path / "src")
    elsewhere = tmp_path / "elsewhere"
    linker.junction(EXTENDED + source + "\\Photos.", elsewhere)   # a name Win32 would trim
    deep = source
    for _ in range(3):
        deep += "\\" + "d" * 100
    os.makedirs(EXTENDED + deep)
    linker.junction(EXTENDED + deep + "\\LongLink", elsewhere)
    result = backup_utils.run_job(job)

    assert result["ok"], result["message"]
    assert result["links_skipped"] == 2
    long_name = "/".join(["d" * 100] * 3) + "/LongLink"
    assert result["links_sample"] == sorted(["Photos.", long_name], key=str.casefold)
    assert len(source) + len(long_name) > 260
    assert "Photos." in result["message"] and long_name in result["message"]


# --- the stored job ------------------------------------------------------------------------

def test_a_failed_run_stores_no_links(job):
    stored = backup_utils.get_job(job["id"])
    stored["last_links"], stored["last_links_sample"] = 3, ["x"]
    backup_utils.save_job(stored)
    backup_utils.record_run(job["id"], {"ok": False, "message": "boom", "links_skipped": 2,
                                        "links_sample": ["a"]})
    after = backup_utils.get_job(job["id"])
    assert after["last_links"] == 0 and after["last_links_sample"] == []


def test_the_stored_sample_keeps_five_strings_and_ignores_the_rest(job):
    backup_utils.record_run(job["id"], {
        "ok": True, "message": "m", "links_skipped": 9,
        "links_sample": ["a", 3, None, "b", "c", "d", "e", "f", "g"]})
    after = backup_utils.get_job(job["id"])
    assert after["last_links"] == 9
    assert after["last_links_sample"] == ["a", "b", "c", "d", "e"]


@pytest.mark.parametrize("junk", ["3", -2, None, True, 1.5, [1]])
def test_a_hand_edited_links_count_never_crashes_the_reader(junk):
    assert backup_utils.job_links({"last_links": junk}) == 0
    assert backup_utils.job_links_sample({"last_links_sample": junk}) == []


def test_a_junk_sample_is_filtered_and_capped_by_the_reader():
    sample = ["a", 1, "b", "c", "d", "e", "f", {}]
    assert backup_utils.job_links_sample({"last_links_sample": sample}) == ["a", "b", "c", "d", "e"]


def test_an_old_jobs_file_without_the_fields_reads_as_zero_and_plain_ok():
    old = {"id": "abc12345", "name": "Old", "last_run": "2026-01-01T00:00:00", "last_result": "ok"}
    assert backup_utils.job_links(old) == 0 and backup_utils.job_links_sample(old) == []
    pytest.importorskip("PySide6")
    from tabs.backup_tab import JobRow
    assert JobRow._status(None, old) == ("OK", "StatusOk")
    assert JobRow._warning_detail(old) == ""


def test_a_new_job_starts_with_no_links():
    fresh = backup_utils.new_job("n", "s", "t")
    assert fresh["last_links"] == 0 and fresh["last_links_sample"] == []


# --- the screen ----------------------------------------------------------------------------

def status_label(row):
    """The Status cell of a JobRow: the label with a Status* object name."""
    from PySide6 import QtWidgets
    found = [label for label in row.findChildren(QtWidgets.QLabel)
             if label.objectName().startswith("Status")]
    assert len(found) == 1, [(l.objectName(), l.text()) for l in found]
    return found[0]


@pytest.fixture
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def stored_job(**fields):
    """A finished-ok job record carrying `fields`."""
    return {"id": "abc12345", "name": "J", "last_run": "2026-11-20T02:00:00",
            "last_result": "ok", **fields}


@pytest.mark.parametrize("fields, text", [
    ({"last_links": 1}, "OK, 1 shortcut not followed"),
    ({"last_links": 2}, "OK, 2 shortcuts not followed"),
    ({"last_skipped": 1, "last_links": 2}, "OK, 1 skipped, 2 shortcuts not followed"),
    ({"last_changed": 1, "last_links": 1}, "OK, 1 changed during backup, 1 shortcut not followed"),
    ({"last_skipped": 1, "last_changed": 1, "last_links": 2, "last_pruning_held": True,
      "last_baseline_files": 10, "last_files": 2},
     "OK, 1 skipped, 1 changed during backup, 2 shortcuts not followed, pruning held"),
    ({"last_links": 1, "last_pruning_held": True, "last_baseline_files": 10, "last_files": 2},
     "OK, 1 shortcut not followed, pruning held"),
])
def test_the_status_text_for_every_combination_of_notes(qt_app, fields, text):
    from tabs.backup_tab import JobRow
    assert JobRow._status(None, stored_job(**fields)) == (text, "StatusWarn")


def test_the_status_cell_and_tooltip_for_shortcuts_in_the_warning_style(qt_app):
    from tabs.backup_tab import JobRow
    plain = status_label(JobRow(stored_job()))
    assert (plain.objectName(), plain.text(), plain.toolTip()) == ("StatusOk", "OK", "")   # control

    names = ["Archive", "Photos", "a/b", "c", "d"]
    label = status_label(JobRow(stored_job(last_links=6, last_links_sample=names)))

    assert label.objectName() == "StatusWarn"                     # the skipped warning's object name
    assert label.text() == "OK, 6 shortcuts not followed"
    tip = label.toolTip()
    assert tip.startswith(
        "These are shortcuts to somewhere else. Safekeep does not follow shortcuts, so "
        "what they point to is not in this backup. Add those locations as their own backup.")
    assert tip.splitlines()[1:] == names + ["and 1 more"]         # five names, then the rest
    assert label.accessibleDescription() == tip


def test_the_tooltip_of_a_run_with_several_notes_keeps_each_one(qt_app):
    from tabs.backup_tab import JobRow
    job = stored_job(last_skipped=1, last_skipped_sample=["x.txt"], last_changed=1,
                     last_changed_sample=["y.txt"], last_links=1, last_links_sample=["Photos"])
    tip = status_label(JobRow(job)).toolTip()
    assert "could not be read" in tip and "x.txt" in tip
    assert "may mix old and new parts" in tip and "y.txt" in tip
    assert "shortcuts to somewhere else" in tip and "Photos" in tip
    assert tip.index("x.txt") < tip.index("y.txt") < tip.index("Photos")


def test_a_failed_run_does_not_show_a_stale_shortcut_note(qt_app):
    from tabs.backup_tab import JobRow
    job = stored_job(last_result="failed: boom", last_links=2, last_links_sample=["a"])
    assert JobRow._status(None, job) == ("Failed", "StatusError")
    assert "shortcut" not in JobRow._warning_detail(job)


def test_the_tab_reads_a_run_with_shortcuts_as_a_warning(qt_app, job, monkeypatch):
    from utils import scheduler
    from tabs import backup_tab
    monkeypatch.setattr(scheduler, "_run", lambda args: (True, ""))
    tab = backup_tab.BackupTab()
    result = {"ok": True, "message": "Backed up 8 file(s) and verified", "links_skipped": 1,
              "links_sample": ["Photos"], "files": 8, "version": "20261120-020000"}
    tab._finish_run(job, result)
    assert tab.backup_status.objectName() == "StatusWarn"
    tab._finish_run(job, dict(result, links_skipped=0, links_sample=[]))
    assert tab.backup_status.objectName() == "Hint"               # positive control: nothing to flag


# --- the run log: a count, never a name -------------------------------------------------------

@windows_only
def test_the_written_log_line_carries_a_count_and_no_names_or_paths(tmp_path, job, linker):
    linker.junction(tmp_path / "src" / "Photos", tmp_path / "elsewhere")
    linker.junction(tmp_path / "src" / "docs" / "Archive", tmp_path / "elsewhere")
    backup_utils.run_job(job)
    with open(runlog.log_path(job["id"]), encoding="utf-8") as handle:
        raw = handle.read()
    line = json.loads(raw.splitlines()[-1])

    assert tuple(line) == runlog.LINE_KEYS and line["links"] == 2
    assert line["outcome"] == "ok_links" and "ok_links" in runlog.OUTCOMES
    assert "Photos" not in raw and "Archive" not in raw and "elsewhere" not in raw
    assert str(tmp_path) not in raw and SECRET not in raw
    assert "shortcut" not in line["message"] and line["message"].startswith("Backed up 3 file(s)")
    assert not line["message"].endswith(" ")


def test_the_outcome_ranks_below_skipped_changed_and_held_and_above_plain_ok():
    def outcome(**result):
        return runlog.build_line({}, {"ok": True, "links_skipped": 1, **result}, "manual",
                                 runlog.datetime.now(), runlog.datetime.now())["outcome"]
    assert outcome() == "ok_links"
    assert outcome(changed=1) == "ok_changed"
    assert outcome(errors=["a.txt: boom"], not_copied=1) == "ok_skipped"
    assert outcome(pruning_held=True, baseline_files=5) == "ok_pruning_held"
    assert outcome(links_skipped=0) == "ok"                       # control: no links, plain ok
    failed = runlog.build_line({}, {"ok": False, "message": "boom", "links_skipped": 4},
                               "manual", runlog.datetime.now(), runlog.datetime.now())
    assert failed["outcome"] == "failed" and failed["links"] == 0  # a failed run kept nothing


def test_the_logged_message_keeps_everything_but_the_sentence_that_names_links():
    sentence = runner.links_sentence(1, 0, ["Photos"], 1)
    message = "Backed up 3 file(s) and verified every copied file. " + sentence
    line = runlog.build_line({}, {"ok": True, "message": message, "links_skipped": 1,
                                  "links_sentence": sentence}, "manual",
                             runlog.datetime.now(), runlog.datetime.now())
    assert line["message"] == "Backed up 3 file(s) and verified every copied file."
    kept = runlog.build_line({}, {"ok": True, "message": message}, "manual",   # control
                             runlog.datetime.now(), runlog.datetime.now())
    assert "Photos" in kept["message"]


# --- the headless run -----------------------------------------------------------------------------

@windows_only
def test_the_headless_run_warns_on_stderr_and_exits_zero(tmp_path, job, linker, capsys):
    import main
    backup_utils.save_job(dict(job, last_precheck={"problems": 0}))   # skip the first-run pre-check
    for name in ("Photos", "Archive"):
        linker.junction(tmp_path / "src" / name, tmp_path / "elsewhere")
    code = main.run_backup_job_cli(job["id"])
    out, err = capsys.readouterr()

    assert code == 0 and "[OK] " in out
    warn = [l for l in err.splitlines() if "were not followed" in l]
    assert warn == ["[WARN] 2 shortcut(s) were not followed: Archive, Photos"]
    assert "were not copied" not in err and "changed while" not in err


@windows_only
def test_the_headless_warning_says_how_many_more(tmp_path, job, linker, capsys):
    import main
    backup_utils.save_job(dict(job, last_precheck={"problems": 0}))
    for name in "abcdefg":
        linker.junction(tmp_path / "src" / name, tmp_path / "elsewhere")
    assert main.run_backup_job_cli(job["id"]) == 0
    assert "[WARN] 7 shortcut(s) were not followed: a, b, c, d, e and 2 more" in capsys.readouterr().err


def test_a_clean_headless_run_prints_no_shortcut_warning(job, capsys):
    import main
    backup_utils.save_job(dict(job, last_precheck={"problems": 0}))
    assert main.run_backup_job_cli(job["id"]) == 0
    assert "not followed" not in capsys.readouterr().err


# --- notes together ------------------------------------------------------------------------------------

def lock_files(monkeypatch, names):
    """Make opening a source file called one of `names` fail for reading."""
    real_open = open

    def fake_open(path, mode="r", *args, **kwargs):
        if mode == "rb" and os.path.basename(str(path)) in names:
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(copying, "open", fake_open, raising=False)


@windows_only
def test_skipped_changed_and_shortcuts_in_one_run_are_all_stored_logged_and_said(
        tmp_path, job, linker, monkeypatch):
    linker.junction(tmp_path / "src" / "Photos", tmp_path / "elsewhere")
    lock_files(monkeypatch, {"b.txt"})                           # skipped, and retried once

    def keep_changing(src):
        if os.path.basename(src) == "a.txt":
            with open(src, "ab") as handle:
                handle.write(b"x")

    monkeypatch.setattr(copying, "_after_read", keep_changing)    # changes on both copies
    result, stored = run_and_record(job)

    assert result["ok"] and result["not_copied"] == 1 and result["changed"] == 1
    assert result["links_skipped"] == 1                          # once, though the retry pass ran
    assert stored["last_result"] == "ok"
    assert (stored["last_skipped"], stored["last_changed"], stored["last_links"]) == (1, 1, 1)
    line = run_log(job)[-1]
    assert (line["outcome"], line["skipped"], line["changed"], line["links"]) == ("ok_skipped", 1, 1, 1)
    pytest.importorskip("PySide6")
    from tabs.backup_tab import JobRow
    assert JobRow._status(None, stored) == (
        "OK, 1 skipped, 1 changed during backup, 1 shortcut not followed", "StatusWarn")
    tip = JobRow._warning_detail(stored)
    assert "b.txt" in tip and "docs/a.txt" in tip and "Photos" in tip


# --- Windows' own profile junctions are not something the user must act on -------------------------------

def test_profile_root_is_found_lexically_and_innermost_first():
    j = os.path.join
    sep = os.sep
    assert fs_utils.profile_root(j(sep, "Users", "alice", "Documents", "x")) == j(sep, "Users", "alice")
    assert fs_utils.profile_root(j(sep, "Users", "alice")) == j(sep, "Users", "alice")
    assert fs_utils.profile_root(j(sep, "Users")) is None
    assert fs_utils.profile_root(j(sep, "Data", "alice")) is None
    inner = j(sep, "Users", "alice", "Users", "bob", "x")
    assert fs_utils.profile_root(inner) == j(sep, "Users", "alice", "Users", "bob")


def make_profile(tmp_path):
    """A fake `Users/alice` profile with the real folders its legacy junctions point at."""
    profile = tmp_path / "Users" / "alice"
    (profile / "Documents").mkdir(parents=True)
    (profile / "AppData" / "Roaming").mkdir(parents=True)
    (profile / "Music").mkdir()
    (profile / "Documents" / "doc.txt").write_bytes(b"doc" * 40)
    (profile / "AppData" / "Roaming" / "roaming.txt").write_bytes(b"roam" * 40)
    (profile / "Music" / "song.mp3").write_bytes(b"song" * 40)
    return profile


def copy_stats(source, out):
    stats = {}
    copying.copy_tree(str(source), str(out), stats=stats)
    return stats


@windows_only
def test_windows_own_legacy_junctions_in_a_profile_are_not_counted(tmp_path, linker):
    profile = make_profile(tmp_path)
    linker.junction(profile / "Application Data", profile / "AppData" / "Roaming")
    linker.junction(profile / "Documents" / "My Music", profile / "Music")
    linker.junction(profile / "My Documents", profile / "Documents")
    stats = copy_stats(profile, tmp_path / "out")

    assert stats["links"] == 0 and stats["link_names"] == [] and stats["links_folders"] == 0
    copied = sorted(os.path.relpath(os.path.join(c, f), tmp_path / "out").replace("\\", "/")
                    for c, _, files in os.walk(tmp_path / "out") for f in files)
    assert copied == ["AppData/Roaming/roaming.txt", "Documents/doc.txt", "Music/song.mp3"]


@windows_only
def test_a_real_shortcut_in_the_same_profile_is_counted(tmp_path, linker):
    profile = make_profile(tmp_path)
    make_elsewhere(tmp_path / "elsewhere")
    linker.junction(profile / "Application Data", profile / "AppData" / "Roaming")   # legacy: hidden
    linker.junction(profile / "Documents" / "Photos", tmp_path / "elsewhere")        # real: named
    stats = copy_stats(profile, tmp_path / "out")

    assert stats["links"] == 1 and stats["link_names"] == ["Documents/Photos"]


@windows_only
def test_a_legacy_name_pointing_outside_the_profile_is_somebodys_real_shortcut(tmp_path, linker):
    profile = make_profile(tmp_path)
    make_elsewhere(tmp_path / "elsewhere")
    linker.junction(profile / "Templates", tmp_path / "elsewhere")
    stats = copy_stats(profile, tmp_path / "out")

    assert stats["links"] == 1 and stats["link_names"] == ["Templates"]


@windows_only
def test_the_same_name_outside_any_profile_is_counted(tmp_path, linker, monkeypatch):
    source = make_source(tmp_path / "plain")
    make_elsewhere(tmp_path / "elsewhere")
    linker.junction(source / "Application Data", tmp_path / "elsewhere")
    # pytest's own temp folder sits inside the real profile of whoever runs this,
    # so say explicitly that this tree is not in one.
    monkeypatch.setattr(fs_utils, "profile_root", lambda path: None)
    stats = copy_stats(source, tmp_path / "out")

    assert stats["links"] == 1 and stats["link_names"] == ["Application Data"]
    monkeypatch.undo()                                            # control: in a profile (the real one), same target inside it
    inside = make_source(tmp_path / "plain2")
    (inside / "Roaming").mkdir()
    linker.junction(inside / "Application Data", inside / "Roaming")
    assert copy_stats(inside, tmp_path / "out2")["links"] == 0


@windows_only
def test_the_legacy_rule_never_lists_the_junction_it_decides_about(tmp_path, linker, monkeypatch):
    profile = make_profile(tmp_path)
    linker.junction(profile / "Application Data", profile / "AppData" / "Roaming")
    real_scandir, listed = os.scandir, []

    def deny_link(path="."):
        listed.append(os.fspath(path))
        if os.fspath(path).rstrip("\\").endswith("Application Data"):
            raise PermissionError(5, "Access is denied", os.fspath(path))   # as Windows does
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", deny_link)
    stats = copy_stats(profile, tmp_path / "out")

    assert stats["links"] == 0                                    # decided without a listing
    assert not [p for p in listed if p.rstrip("\\").endswith("Application Data")]


class FakeEntry:
    """Just enough of a DirEntry for `is_legacy_profile_link`."""

    def __init__(self, name="Application Data", symlink=False, folder=True):
        self.name, self._symlink, self._folder = name, symlink, folder

    def is_symlink(self):
        return self._symlink

    def is_dir(self, follow_symlinks=True):
        return self._folder


@windows_only
@pytest.mark.parametrize("entry, expected", [
    (FakeEntry(), True),                                  # control: a legacy junction in a profile
    (FakeEntry(name="APPLICATION DATA"), True),           # Windows names ignore case
    (FakeEntry(symlink=True), False),                     # a symlink is somebody's own choice
    (FakeEntry(folder=False), False),                     # a file link is never one of these
    (FakeEntry(name="Photos"), False),                    # not in the legacy set
])
def test_the_legacy_rule_needs_a_junction_to_a_folder_with_a_legacy_name(tmp_path, entry, expected):
    profile = tmp_path / "Users" / "alice"
    assert fs_utils.is_legacy_profile_link(entry, str(profile / entry.name)) is expected


@windows_only
def test_the_legacy_rule_needs_a_profile_around_it(tmp_path, monkeypatch):
    path = str(tmp_path / "Users" / "alice" / "Application Data")
    assert fs_utils.is_legacy_profile_link(FakeEntry(), path) is True
    monkeypatch.setattr(fs_utils, "profile_root", lambda p: None)
    assert fs_utils.is_legacy_profile_link(FakeEntry(), path) is False


def test_a_legacy_rule_is_a_windows_rule(monkeypatch, tmp_path):
    path = str(tmp_path / "Users" / "alice" / "Application Data")
    monkeypatch.setattr(fs_utils.os, "name", "posix")
    assert fs_utils.is_legacy_profile_link(FakeEntry(), path) is False


# --- a file link, without needing a privilege to make a symlink -------------------------------------

@windows_only
def test_a_file_the_walk_treats_as_a_link_is_a_shortcut_file_beside_a_shortcut_folder(
        tmp_path, job, linker, monkeypatch):
    """Stands in for a file symlink (unprivileged accounts cannot make one): the
    walk's one link predicate says `latest.txt` is a link, so the engine's own
    kind check must call it a file, and the sentence must count the two apart."""
    linker.junction(tmp_path / "src" / "Photos", tmp_path / "elsewhere")
    (tmp_path / "src" / "latest.txt").write_bytes(b"pointed at somewhere else")
    real = copying._is_link_like
    monkeypatch.setattr(copying, "_is_link_like",
                        lambda entry: entry.name == "latest.txt" or real(entry))
    result = backup_utils.run_job(job)

    assert result["ok"] and result["links_skipped"] == 2 and result["links_folders"] == 1
    assert result["links_sample"] == ["latest.txt", "Photos"]
    assert ("1 shortcut folder and 1 shortcut file point somewhere else and were not "
            "followed: latest.txt, Photos. Add those locations as their own backup."
            ) in result["message"]
    assert "latest.txt" not in version_files(job, result)
