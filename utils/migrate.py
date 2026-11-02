# utils/migrate.py
"""One-time migration from the app's pre-rename identity (UtilityTool) to
Safekeep.

Three things carried the old name and are moved across, each at most once:

- **Data.** Everything in the old app-data directory is COPIED into the new
  one. Nothing is moved or deleted: the old directory stays as a safety copy,
  and a file the new directory already holds is never overwritten.
- **Scheduled tasks.** Each daily/weekly job gets its task registered under
  the new name, and the old task is deleted only after that registration
  succeeds. A failed registration leaves the old task running and is reported.
- **Settings.** The old QSettings values are copied to the new ones when the
  new ones are still empty. The QSettings objects are built in main.py and
  passed in, so this module stays Qt-free (CLAUDE.md §3/§8).

Every scheduler and filesystem side effect is a parameter with a real
default, so tests drive the logic without touching the machine.
"""
import os
import shutil
from typing import Callable, Dict, List, Optional, Tuple

from utils import backup_utils
from utils.fs_utils import LEGACY_APP_NAME, app_dir, app_dir_for

# Written into the new app-data directory once a step has finished, so it
# never runs again. Data and tasks are marked separately: the headless path
# copies data but must not touch the scheduler, so the GUI's first start
# still has the task step to do.
DATA_MARKER = ".migrated-data"
TASKS_MARKER = ".migrated-tasks"


def _mark(directory: str, marker: str) -> None:
    """Record that a migration step finished in `directory`."""
    with open(os.path.join(directory, marker), "w", encoding="utf-8") as handle:
        handle.write("done\n")


def migrate_data(old_dir: Optional[str] = None, new_dir: Optional[str] = None) -> List[str]:
    """Copy the pre-rename app-data directory into the new one, once.

    Returns the relative paths copied (empty when there was nothing to do).
    Skipped entirely when the new directory already has a jobs file or the
    data marker, or when the old directory does not exist. Files already in
    the new directory are never overwritten. The marker is written only when
    every file copied, so a partial copy is retried on the next start -- safe
    because the retry, too, never overwrites.
    """
    old_dir = old_dir or app_dir_for(LEGACY_APP_NAME)
    new_dir = new_dir or app_dir()
    if (not os.path.isdir(old_dir)
            or os.path.exists(os.path.join(new_dir, DATA_MARKER))
            or os.path.exists(os.path.join(new_dir, backup_utils.JOBS_FILENAME))):
        return []

    copied, failed = [], False
    for folder, _dirs, files in os.walk(old_dir):
        for name in files:
            source = os.path.join(folder, name)
            rel = os.path.relpath(source, old_dir)
            destination = os.path.join(new_dir, rel)
            if os.path.exists(destination):
                continue
            try:
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                shutil.copy2(source, destination)
                copied.append(rel)
            except OSError as exc:
                failed = True
                print(f"[WARN] Could not copy '{source}' to '{destination}': {exc}")
    if not failed:
        _mark(new_dir, DATA_MARKER)
    return copied


def migrate_tasks(jobs: Optional[List[Dict]] = None,
                  old_dir: Optional[str] = None,
                  new_dir: Optional[str] = None,
                  register: Optional[Callable[[Dict], Tuple[bool, str]]] = None,
                  legacy_exists: Optional[Callable[[str], bool]] = None,
                  remove_legacy: Optional[Callable[[str], Tuple[bool, str]]] = None
                  ) -> List[Dict]:
    """Re-register every scheduled job under the new task name, once.

    Returns one `{"id", "name", "ok", "message"}` per daily/weekly job; manual
    jobs have no task and produce nothing. A damaged jobs file comes back as a
    single not-ok result rather than raising or reading as "no jobs". Order per job is the whole point:
    register the new task, and only if that succeeds delete the old one. A
    failed registration leaves the old task in place (the job keeps running
    under its old name) and comes back `ok=False`. A new task that registered
    beside an old one that would not delete is also `ok=False` -- both would
    fire.

    Runs only if the pre-rename directory exists (no old install means no
    old tasks) and the tasks marker is absent. The marker is written only
    when every job came back ok, so a failure is retried on the next start;
    registration replaces a task of the same name, so a retry is harmless.

    GUI startup only: the headless `--run-backup-job` path must not touch the
    scheduler. The macOS (launchd) branch goes through the same
    `backup_utils` functions and is unverified on a Mac, like the rest of the
    launchd path -- it is covered here only through injected callables.
    """
    old_dir = old_dir or app_dir_for(LEGACY_APP_NAME)
    new_dir = new_dir or app_dir()
    if not os.path.isdir(old_dir) or os.path.exists(os.path.join(new_dir, TASKS_MARKER)):
        return []

    if jobs is None:
        try:
            jobs = backup_utils.load_jobs()
        except backup_utils.JobsFileCorrupt as exc:
            # Not "no jobs": the scheduled tasks cannot be re-registered
            # until the file is restored. No marker is written, so the step
            # runs again on the next start.
            return [{"id": "", "name": backup_utils.JOBS_FILENAME,
                     "ok": False, "message": str(exc)}]
    register = register or backup_utils.register_task
    legacy_exists = legacy_exists or (lambda job_id: backup_utils.task_exists(job_id, legacy=True))
    remove_legacy = remove_legacy or (lambda job_id: backup_utils.unregister_task(job_id, legacy=True))

    results = []
    for job in jobs:
        if job.get("schedule") not in ("daily", "weekly"):
            continue
        job_id = job["id"]
        result = {"id": job_id, "name": job.get("name", job_id)}
        ok, message = register(job)
        if not ok:
            result.update(ok=False, message=(
                f"Could not register {backup_utils.task_name(job_id)}; the old task "
                f"{backup_utils.task_name(job_id, legacy=True)} was left in place: {message}"))
        elif not legacy_exists(job_id):
            result.update(ok=True, message="Registered; there was no old task to remove.")
        else:
            removed, detail = remove_legacy(job_id)
            result.update(ok=removed, message=(
                "Registered, and the old task was removed." if removed else
                f"Registered, but the old task {backup_utils.task_name(job_id, legacy=True)} "
                f"could not be removed and will also run: {detail}"))
        results.append(result)

    if all(r["ok"] for r in results):
        _mark(new_dir, TASKS_MARKER)
    return results


def migrate_settings(old, new) -> List[str]:
    """Copy every key from the pre-rename settings to the new ones, once.

    `old` and `new` are QSettings objects built by main.py (anything with
    `allKeys`, `value`, `setValue` and `sync` works, which is what the tests
    pass). Does nothing when `new` already holds a key -- that is both the
    "once" and the guarantee that a choice made in Safekeep is never replaced
    by an older one. Returns the keys copied.
    """
    if new.allKeys():
        return []
    keys = list(old.allKeys())
    for key in keys:
        new.setValue(key, old.value(key))
    new.sync()
    return keys
