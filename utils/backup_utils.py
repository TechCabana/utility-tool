# utils/backup_utils.py
"""Backup jobs: the one import the app uses for everything backup.

Qt-free by design (CLAUDE.md §3/§8) -- plain paths and plain dicts in, plain
dicts out. `tabs/backup_tab.py` runs the long ones from a QThread worker and
renders what comes back; nothing here knows that. `main.py --run-backup-job`
calls `run_job` directly, with no QApplication at all, which is what the
scheduled task invokes.

Storage is one JSON file in the OS app-data directory, via
`utils/fs_utils.app_dir()`. There is no first-run default set: a machine with no backup jobs has none, and the
Backup screen's EmptyState is the correct rendering of that.

**This module is a facade (card 95).** The code lives in:

- `utils/jobs_store.py` -- the jobs file, its lock, job CRUD, `record_run`
- `utils/versions.py`   -- version names, manifests, prune, sweep, restore
- `utils/copying.py`    -- tree walking, the hashing copy, `copy_tree`
- `utils/verify.py`     -- the "full" and "quick" checks of a finished copy
- `utils/preflight.py`  -- pre-flight checks and the sweep-then-retry
- `utils/runner.py`     -- `run_job` and `changes_since`
- `utils/scheduler.py`  -- Windows Task Scheduler and macOS launchd

Every name is re-exported here so `backup_utils.X` keeps working. **To
monkeypatch a name, patch it on the module that owns it** (e.g.
`utils.copying._hash_copy`, `utils.jobs_store.JOBS_FILE`): code reads its
own module's globals, so setting `backup_utils.X` changes nothing but this
facade's copy, and reading `backup_utils.X` after such a patch returns the
original value.
"""
import os  # noqa: F401  -- tests patch `backup_utils.os.<fn>` / `.sys.platform`
import sys  # noqa: F401
from datetime import datetime  # noqa: F401  -- tests subclass `backup_utils.datetime`

from utils.jobs_store import (  # noqa: F401
    _backup_path, DEFAULT_RETENTION, DEFAULT_VERIFY, get_job, _has_usable_backup,
    job_baseline_files, job_last_files, job_pruning_held, job_skipped,
    job_skipped_sample, job_verify_mode, JOBS_FILE, _jobs_file_lock, JOBS_FILENAME, JobsFileCorrupt,
    load_jobs, _locks_dir, new_job, _read_valid, record_precheck, record_run, remove_job,
    restore_jobs_from_backup, _save_job, save_job, save_jobs, SCHEDULES, VERIFY_MODES,
    _write_atomic,
)
from utils.versions import (  # noqa: F401
    baseline_files, _discard, is_sanity_drop, is_verified, job_root, _last_line, latest_version,
    _make_writable_and_retry, manifest_path, _MANIFEST_RE, MANIFEST_SUFFIX,
    _MANIFEST_TMP_RE, MANIFEST_VERSION, _PARTIAL_RE, prune_versions,
    pruning_held_sentence, read_manifest_summary, restore_candidates, restore_version, _rmtree,
    SANITY_DROP_FRACTION, sanity_baseline, sanity_drop_baseline, _STAMP, SWEEP_MAX_AGE_DAYS, sweep_stale, verified_versions, verify_mode, version_datetime, VERSION_FORMAT,
    version_names, version_path, _VERSION_RE, version_when,
)
from utils.copying import (  # noqa: F401
    BackupAborted, CHUNK, copy_tree, _copy_windows_attributes, _ERRNO_CAUSES,
    _FILE_ATTRIBUTE_NORMAL, _hash_copy, _iter_tree, _KEPT_ATTRIBUTES, _open_for_write,
    SHORTFALL_MIN_FILES, SHORTFALL_TOLERANCE, _source_gone, SourceUnavailable,
    TARGET_CAUSES, TargetWriteFailed, tree_totals, walk_tree, _WINERROR_CAUSES,
    _writing,
)
from utils.verify import (  # noqa: F401
    _copied_path, _manifest_files, _quick_sample, QUICK_SAMPLE_CAP,
    QUICK_SAMPLE_FRACTION, _quick_verify, _rehash, _rehash_matches, _verify_copy,
    VERIFY_NAMED,
)
from utils.preflight import (  # noqa: F401
    _free_bytes, HELD_LOW_SPACE_SENTENCE, LOW_SPACE_PREFIX, nearest_existing_ancestor, preflight,
    _preflight_and_sweep, _same_volume, SPACE_MARGIN, target_inside_source,
    target_volume, volume_id,
)
from utils.runner import (  # noqa: F401
    _backup, _busy_message, changes_since, CLAIM_ATTEMPTS, _claim_stamp,
    _job_lock_path, MTIME_TOLERANCE, _run_job, run_job, _with_sweep,
)
from utils.scheduler import (  # noqa: F401
    job_argv, job_command, _launchctl, _launchd_domain, _launchd_exists, launchd_label,
    LAUNCHD_MONDAY, launchd_plist, launchd_plist_path, LAUNCHD_PREFIX,
    _launchd_register, _launchd_unregister, LEGACY_LAUNCHD_PREFIX, LEGACY_TASK_PREFIX,
    _NO_WINDOW, register_task, _run, schedule_label, scheduler_name, schtasks_args,
    sync_task, task_exists, task_name, TASK_PREFIX, unregister_task, _xml_escape,
)
