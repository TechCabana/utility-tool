# utils/scheduler.py
"""Scheduling a job with the OS: Windows Task Scheduler and macOS launchd.

Split out of utils/backup_utils.py (card 95) as a pure move; that module
re-exports everything here. Qt-free.

**Scheduling is handed to the OS, and the OS differs.** Configuration lives
in this app; the recurring trigger belongs to the platform. Windows uses
`schtasks.exe` and macOS uses `launchd`, both of which ship with the system,
so neither adds a dependency. Linux has no equivalent implemented and reports
that plainly rather than pretending a job is scheduled.

This is the one place the repo departs from CLAUDE.md §1's "no per-OS
branching" goal, by explicit owner decision, and the branching is confined to
the four functions at the bottom of this file.

**The macOS path is unverified.** It was written on a Windows machine and has
never been run on a Mac. The parts that can be tested without one - the plist
that gets written and the path it is written to - are covered by
tests/test_backup_utils.py; loading it with `launchctl` is not. A failure
there reports the real error rather than claiming success, which is the most
that can honestly be promised until someone runs it.
"""
import csv
import io
import os
import subprocess
import sys
from typing import Dict, List, Optional, Tuple


# Windows only: keep schtasks' console window from flashing over the app.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------------------
# Windows Task Scheduler
# ---------------------------------------------------------------------------

# Task Scheduler name prefixes. The legacy one is what tasks were registered
# under before the Safekeep rename; only utils/migrate.py asks for it, to
# remove an old task once its replacement has registered.
TASK_PREFIX = "Safekeep_Backup_"
LEGACY_TASK_PREFIX = "UtilityTool_Backup_"


def task_name(job_id: str, legacy: bool = False) -> str:
    """The scheduled task's name. Prefixed so every task this app owns is
    identifiable in Task Scheduler, and keyed on the job id so a rename never
    strands one. `legacy=True` gives the pre-rename name."""
    return f"{LEGACY_TASK_PREFIX if legacy else TASK_PREFIX}{job_id}"


def job_command(job_id: str) -> str:
    """The command line a scheduled task runs -- the headless CLI in main.py.

    `sys.executable` is the interpreter actually running, so a job created
    from the venv schedules the venv's python. A PyInstaller build sets
    `sys.frozen`, in which case the executable *is* the app and takes the flag
    directly.

    Packaging gap, stated rather than hidden: a task created while running
    from source hardcodes that interpreter and that checkout path, so moving
    or deleting either breaks the schedule silently (the task runs and fails).
    Re-creating the job after packaging fixes it; a packaged build is the real
    answer and is out of this card's scope.
    """
    return " ".join(f'"{part}"' if " " in part else part
                    for part in job_argv(job_id))


def job_argv(job_id: str) -> List[str]:
    """The same command as an argv list, which is what launchd wants.

    `schtasks` takes one quoted string; a launchd plist takes an array of
    arguments and does no shell parsing of its own. Building the list first
    and joining it for Windows keeps one definition of what actually runs.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "--run-backup-job", job_id]
    entry = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
    return [sys.executable, entry, "--run-backup-job", job_id]


def schtasks_args(job: Dict) -> Optional[List[str]]:
    """The full `schtasks /create` argv for `job`, or None for a manual job.

    Separated from the call that runs it so the argument construction -- the
    part with the interesting edge cases -- is testable without registering a
    real task on the machine running the tests.
    """
    schedule = job.get("schedule", "manual")
    if schedule not in ("daily", "weekly"):
        return None
    args = [
        "schtasks", "/create",
        "/tn", task_name(job["id"]),
        "/tr", job_command(job["id"]),
        "/sc", "WEEKLY" if schedule == "weekly" else "DAILY",
        "/st", job.get("at") or "20:00",
        "/f",  # replace an existing task of the same name rather than failing
    ]
    if schedule == "weekly":
        # schtasks needs an explicit day for a weekly task; Monday is the
        # default rather than a fourth field on the job form nobody asked for.
        args += ["/d", "MON"]
    return args


# ------------------------------
# macOS (launchd)
# ------------------------------
# A launchd label has to be unique per user and is conventionally reverse-DNS.
# The legacy prefix is the pre-rename label, used only by the migration.
LAUNCHD_PREFIX = "com.techcabana.safekeep.backup"
LEGACY_LAUNCHD_PREFIX = "com.techcabana.utilitytool.backup"

# launchd's own weekday numbering: 0 and 7 are both Sunday, 1 is Monday. The
# Windows path pins a weekly job to Monday, so this does too -- the two
# platforms must not disagree about which day "weekly" means.
LAUNCHD_MONDAY = 1


def launchd_label(job_id: str, legacy: bool = False) -> str:
    """The launchd label for `job_id`; `legacy=True` gives the pre-rename one."""
    return f"{LEGACY_LAUNCHD_PREFIX if legacy else LAUNCHD_PREFIX}.{job_id}"


def launchd_plist_path(job_id: str, legacy: bool = False) -> str:
    """Where the agent's plist lives. Per-user, so no admin rights needed."""
    return os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents",
                        f"{launchd_label(job_id, legacy)}.plist")


def launchd_plist(job: Dict) -> Optional[str]:
    """The plist XML for `job`, or None when the job has no schedule.

    Separated from the call that installs it for the same reason
    `schtasks_args` is: the interesting edge cases are in what gets written,
    and that part can be tested on any machine. Installing it cannot.
    """
    schedule = job.get("schedule", "manual")
    if schedule not in ("daily", "weekly"):
        return None

    at = job.get("at") or "20:00"
    try:
        hour, minute = (int(part) for part in at.split(":", 1))
    except ValueError:
        hour, minute = 20, 0

    interval = [("Hour", hour), ("Minute", minute)]
    if schedule == "weekly":
        interval.append(("Weekday", LAUNCHD_MONDAY))

    arguments = "".join(f"        <string>{_xml_escape(part)}</string>\n"
                        for part in job_argv(job["id"]))
    calendar = "".join(f"        <key>{key}</key><integer>{value}</integer>\n"
                       for key, value in interval)

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        f"    <key>Label</key>\n    <string>{launchd_label(job['id'])}</string>\n"
        f"    <key>ProgramArguments</key>\n    <array>\n{arguments}    </array>\n"
        f"    <key>StartCalendarInterval</key>\n    <dict>\n{calendar}    </dict>\n"
        # The app must not be held open by the scheduler, and a backup that
        # fails should not be respawned in a loop.
        "    <key>RunAtLoad</key>\n    <false/>\n"
        "</dict>\n"
        "</plist>\n"
    )


def _xml_escape(text: str) -> str:
    """Escape `text` for a plist <string> element."""
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;"))


def _launchd_register(job: Dict) -> Tuple[bool, str]:
    """Write the plist and ask launchd to load it."""
    path = launchd_plist_path(job["id"])
    body = launchd_plist(job)
    if body is None:
        _launchd_unregister(job["id"])
        return True, "No schedule: this job runs only when you press Run Now."
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
    except OSError as exc:
        return False, f"Could not write the launchd job at {path}: {exc}"

    # bootout first so a changed schedule replaces the running agent rather
    # than failing with "service already loaded".
    _launchctl(["bootout", _launchd_domain(), launchd_label(job["id"])])
    ok, output = _launchctl(["bootstrap", _launchd_domain(), path])
    if ok:
        return True, f"Scheduled with launchd ({launchd_label(job['id'])})."
    return False, output or "launchctl refused to load the job."


def _launchd_domain() -> str:
    """The per-user launchd domain the agents are loaded into."""
    return f"gui/{os.getuid()}" if hasattr(os, "getuid") else "gui/501"


def _launchd_unregister(job_id: str, legacy: bool = False) -> Tuple[bool, str]:
    """Unload the launchd agent for `job_id` and delete its plist."""
    ok, output = _launchctl(["bootout", _launchd_domain(), launchd_label(job_id, legacy)])
    try:
        os.remove(launchd_plist_path(job_id, legacy))
    except OSError:
        pass  # never registered, or already gone
    return ok, output


def _launchd_exists(job_id: str, legacy: bool = False) -> bool:
    """Whether a plist for `job_id` is installed."""
    return os.path.isfile(launchd_plist_path(job_id, legacy))


def _launchctl(args: List[str]) -> Tuple[bool, str]:
    """Run launchctl, never raising. Same contract as `_run` for schtasks."""
    try:
        completed = subprocess.run(["launchctl"] + args,
                                   capture_output=True, text=True)
    except (OSError, ValueError) as exc:
        return False, f"Could not call launchctl: {exc}"
    output = (completed.stdout or "").strip() or (completed.stderr or "").strip()
    return completed.returncode == 0, output


def _run(args: List[str]) -> Tuple[bool, str]:
    """Run a schtasks command, returning `(ok, output)`.

    Never raises: a machine without schtasks (macOS, Linux) or a policy that
    blocks it must leave the job configured and runnable by hand, not break
    the screen.
    """
    if os.name != "nt":
        return False, ("Windows Task Scheduler is not available on this "
                       "platform. Run this job manually with Run Now.")
    try:
        completed = subprocess.run(args, capture_output=True, text=True,
                                   creationflags=_NO_WINDOW)
    except (OSError, ValueError) as exc:
        return False, f"Could not call schtasks: {exc}"
    output = (completed.stdout or "").strip() or (completed.stderr or "").strip()
    return completed.returncode == 0, output


def register_task(job: Dict) -> Tuple[bool, str]:
    """Create or replace the OS scheduled task for `job`.

    A job whose schedule is "manual" has no task, so any existing one is
    removed -- switching a job to manual has to actually stop it running.
    """
    if sys.platform == "darwin":
        return _launchd_register(job)
    if os.name != "nt":
        return False, ("Scheduling is implemented for Windows and macOS only. "
                       "This job still runs when you press Run Now.")
    args = schtasks_args(job)
    if args is None:
        unregister_task(job["id"])
        return True, "No schedule: this job runs only when you press Run Now."
    return _run(args)


def unregister_task(job_id: str, legacy: bool = False) -> Tuple[bool, str]:
    """Delete the OS scheduled task for `job_id`. A job that never had one
    reports failure from schtasks, which the caller treats as "nothing to
    remove" rather than an error. `legacy=True` targets the pre-rename task."""
    if sys.platform == "darwin":
        return _launchd_unregister(job_id, legacy)
    return _run(["schtasks", "/delete", "/tn", task_name(job_id, legacy), "/f"])


def task_exists(job_id: str, legacy: bool = False) -> bool:
    """Whether the OS scheduler holds a task for `job_id` (or, with
    `legacy=True`, one under the pre-rename name)."""
    if sys.platform == "darwin":
        return _launchd_exists(job_id, legacy)
    ok, _ = _run(["schtasks", "/query", "/tn", task_name(job_id, legacy)])
    return ok


def list_scheduled_tasks() -> Tuple[List[str], str]:
    """Every OS scheduler entry this app owns, as `(names, error)`.

    Matched by naming prefix (current and pre-rename), so it works when the
    jobs file is unreadable and the job ids are unknown. Read-only: nothing is
    created, changed or removed. Never raises -- if the scheduler cannot be
    asked, `names` is empty and `error` says why ("" otherwise), so a caller
    can tell "none" from "could not list".

    The macOS branch lists the LaunchAgents plists and has never been run on a
    Mac.
    """
    if sys.platform == "darwin":
        folder = os.path.dirname(launchd_plist_path("x"))
        try:
            files = sorted(os.listdir(folder))
        except FileNotFoundError:
            return [], ""   # no LaunchAgents folder, so no agents
        except OSError as exc:
            return [], f"Could not list scheduled tasks: {exc}"
        return [name[:-len(".plist")] for name in files
                if name.endswith(".plist")
                and name.startswith((LAUNCHD_PREFIX, LEGACY_LAUNCHD_PREFIX))], ""
    ok, output = _run(["schtasks", "/query", "/fo", "csv", "/nh"])
    if not ok:
        return [], f"Could not list scheduled tasks: {output or 'schtasks failed.'}"
    names = []
    try:
        for row in csv.reader(io.StringIO(output)):
            # First column is the full task path, e.g. "\Safekeep_Backup_ab12cd34";
            # the same task can be listed once per trigger, so de-duplicate.
            name = row[0].lstrip("\\") if row else ""
            if name.startswith((TASK_PREFIX, LEGACY_TASK_PREFIX)) and name not in names:
                names.append(name)
    except csv.Error as exc:
        return [], f"Could not list scheduled tasks: {exc}"
    return names, ""


def scheduler_name() -> str:
    """What the OS calls its scheduler, for user-facing copy.

    The Backup screen used to say "Windows Task Scheduler" on every platform,
    including the one where that is not what happens.
    """
    if sys.platform == "darwin":
        return "launchd"
    if os.name == "nt":
        return "Windows Task Scheduler"
    return "the system scheduler"


def schedule_label(job: Dict) -> str:
    """How a job's schedule reads in the jobs table."""
    schedule = job.get("schedule", "manual")
    if schedule == "daily":
        return f"Daily at {job.get('at', '20:00')}"
    if schedule == "weekly":
        return f"Weekly, Mon at {job.get('at', '20:00')}"
    return "Manual only"


def sync_task(job: Dict) -> Tuple[bool, str]:
    """Bring the OS scheduler in line with the job as stored. The one entry
    point the UI calls after any add or edit."""
    return register_task(job)
