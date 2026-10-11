#!/usr/bin/env python3
"""One-way mirror of a local folder into Google Drive (via Google Drive for Desktop).

Watches SOURCE (default: ~/Documents/Development/BU) and copies every file that
is added or modified into DEST, the matching folder inside your Google Drive
mount. Google Drive for Desktop then uploads the change to the cloud.

  bu_drive_sync.py                   watch forever; auto-detect the Drive folder
  bu_drive_sync.py --once            one full pass, then exit
  bu_drive_sync.py --find-dest       print the Drive folder that would be used
  bu_drive_sync.py --mirror-deletes  also remove from Drive what is deleted or
                                     renamed away in SOURCE (off by default)
  bu_drive_sync.py --exclude .git    skip a name pattern (repeatable)
"""
from __future__ import annotations

import argparse
import fnmatch
import logging
import os
import shutil
import signal
import sys
import threading
import time
from pathlib import Path

try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:  # pragma: no cover
    sys.exit("Missing dependency 'watchdog'. Run install.sh, or: python3 -m pip install watchdog")

DEFAULT_SOURCE = Path.home() / "Documents" / "Development" / "BU"
DEFAULT_DRIVE_FOLDER = "BU"

# Never copied. Each pattern is matched against every path component.
DEFAULT_EXCLUDES = [
    ".DS_Store", "._*", ".Spotlight-V100", ".Trashes", ".fseventsd",
    "*.tmp", "*.swp", "*.swx", "~$*", ".~lock.*", "*.crdownload", "*.part",
]
# Never deleted from DEST by --mirror-deletes. Google-native documents exist
# locally only as these stubs; the real content lives in Drive.
PROTECTED_IN_DEST = [
    "*.gdoc", "*.gsheet", "*.gslides", "*.gdraw", "*.gform", "*.gmap",
    "*.gsite", "*.gjam", "*.gscript", "*.glink", "*.gtable", "*.gnote",
]

MTIME_TOLERANCE = 2.0          # seconds; cloud file systems round timestamps
SETTLE_SECONDS = 1.0           # let editors finish writing before we copy
FULL_SYNC_INTERVAL = 15 * 60   # safety-net pass in case an event was missed
COPY_RETRIES = 5
UNAVAILABLE_RETRY = 30         # seconds between retries when Drive is not mounted

log = logging.getLogger("bu-drive-sync")


# --------------------------------------------------------------------------- #
# Locating the Google Drive folder
# --------------------------------------------------------------------------- #
def find_drive_dest(folder_name: str) -> Path | None:
    """Return <Google Drive>/My Drive/<folder_name>, or None if Drive is not mounted.

    Prefers a mount where the folder already exists. If several Google accounts
    are mounted and none has the folder, the first mount is used.
    """
    home = Path.home()
    mounts: list[Path] = []
    cloud = home / "Library" / "CloudStorage"
    if cloud.is_dir():
        mounts += sorted(p for p in cloud.glob("GoogleDrive-*") if p.is_dir())
    mounts.append(Path("/Volumes/GoogleDrive"))  # Drive for Desktop before macOS 12.1
    candidates = [m / "My Drive" / folder_name for m in mounts if (m / "My Drive").is_dir()]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0] if candidates else None


def matches_any(rel: Path, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(part, pat) for part in rel.parts for pat in patterns)


def needs_copy(src: Path, dst: Path) -> bool:
    try:
        s = src.stat()
    except FileNotFoundError:
        return False
    try:
        d = dst.stat()
    except FileNotFoundError:
        return True
    return s.st_size != d.st_size or abs(s.st_mtime - d.st_mtime) > MTIME_TOLERANCE


# --------------------------------------------------------------------------- #
# The mirror itself
# --------------------------------------------------------------------------- #
class Mirror:
    def __init__(self, source: Path, dest: Path, excludes: list[str],
                 mirror_deletes: bool, dry_run: bool) -> None:
        self.source = source
        self.dest = dest
        self.excludes = excludes
        self.mirror_deletes = mirror_deletes
        self.dry_run = dry_run

    def ready(self) -> bool:
        return self.source.is_dir() and self.dest.is_dir()

    def rel(self, raw: str | bytes) -> Path | None:
        """Path relative to SOURCE, or None if it is the root, outside, or excluded."""
        p = Path(os.fsdecode(raw))
        for candidate in (p, Path(os.path.realpath(p))):
            try:
                rel = candidate.relative_to(self.source)
                break
            except ValueError:
                continue
        else:
            return None
        if rel == Path(".") or matches_any(rel, self.excludes):
            return None
        return rel

    # -- single paths ------------------------------------------------------ #
    def copy_file(self, rel: Path) -> bool:
        """Copy SOURCE/rel to DEST/rel if it differs. Returns True if a copy happened."""
        src, dst = self.source / rel, self.dest / rel
        if not needs_copy(src, dst):
            log.debug("up to date %s", rel)
            return False
        if self.dry_run:
            log.info("would copy %s", rel)
            return True
        for attempt in range(1, COPY_RETRIES + 1):
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(src, dst)
                except FileNotFoundError:
                    raise
                except OSError:
                    # Some cloud mounts refuse file flags or xattrs. Keep content and mtime.
                    shutil.copyfile(src, dst)
                    st = src.stat()
                    os.utime(dst, (st.st_atime, st.st_mtime))
                log.info("copied     %s", rel)
                return True
            except FileNotFoundError:
                return False  # source vanished mid-copy (editor temp file); nothing to do
            except OSError as exc:
                if attempt == COPY_RETRIES:
                    log.error("failed     %s: %s", rel, exc)
                    return False
                time.sleep(attempt)
        return False

    def delete(self, rel: Path) -> None:
        """Remove DEST/rel when SOURCE/rel is gone and --mirror-deletes is on."""
        if not self.mirror_deletes or rel == Path("."):
            return
        if matches_any(rel, PROTECTED_IN_DEST) or os.path.lexists(self.source / rel):
            return
        dst = self.dest / rel
        if not os.path.lexists(dst):
            return
        if self.dry_run:
            log.info("would delete %s", rel)
            return
        try:
            if dst.is_dir() and not dst.is_symlink():
                shutil.rmtree(dst)
            else:
                dst.unlink()
            log.info("deleted    %s", rel)
        except OSError as exc:
            log.error("delete failed %s: %s", rel, exc)

    # -- subtrees ---------------------------------------------------------- #
    def sync_tree(self, rel: Path) -> int:
        """Bring DEST/rel in line with SOURCE/rel. Returns the number of files copied."""
        root = self.source / rel
        copied = 0
        for dirpath, dirnames, filenames in os.walk(root):
            rel_dir = Path(dirpath).relative_to(self.source)
            dirnames[:] = sorted(d for d in dirnames if not matches_any(rel_dir / d, self.excludes))
            if not self.dry_run:
                for d in dirnames:  # keep empty folders too
                    try:
                        (self.dest / rel_dir / d).mkdir(parents=True, exist_ok=True)
                    except OSError as exc:
                        log.error("mkdir failed %s: %s", rel_dir / d, exc)
            for name in sorted(filenames):
                if not matches_any(Path(name), self.excludes) and self.copy_file(rel_dir / name):
                    copied += 1
        if self.mirror_deletes:
            self.prune(rel)
        return copied

    def prune(self, rel: Path) -> None:
        """Delete from DEST/rel whatever no longer exists in SOURCE/rel."""
        root = self.dest / rel
        if not root.is_dir() or not self.source.is_dir():
            return
        for dirpath, dirnames, filenames in os.walk(root, topdown=False):
            rel_dir = Path(dirpath).relative_to(self.dest)
            for name in filenames + dirnames:
                r = rel_dir / name
                if matches_any(r, self.excludes) or matches_any(r, PROTECTED_IN_DEST):
                    continue
                if not os.path.lexists(self.source / r):
                    self.delete(r)

    def reconcile(self, rel: Path) -> None:
        """Make DEST/rel match SOURCE/rel, whatever happened to it."""
        src = self.source / rel
        if src.is_dir():
            self.sync_tree(rel)
        elif src.is_file():
            self.copy_file(rel)
        elif not os.path.lexists(src):
            self.delete(rel)

    def full_sync(self) -> None:
        started = time.monotonic()
        copied = self.sync_tree(Path("."))
        log.info("full sync: %d file(s) copied in %.1fs", copied, time.monotonic() - started)


# --------------------------------------------------------------------------- #
# Watching for changes
# --------------------------------------------------------------------------- #
class Watcher(FileSystemEventHandler):
    """Collects changed paths; a worker thread applies them once they settle."""

    def __init__(self, mirror: Mirror, full_sync_interval: float) -> None:
        super().__init__()
        self.mirror = mirror
        self.full_sync_interval = full_sync_interval
        self.pending: dict[Path, float] = {}
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.failed = False

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory and event.event_type == "modified":
            return  # a child changed; the child reports its own event
        if event.event_type in ("opened", "closed_no_write"):
            return
        for raw in (event.src_path, getattr(event, "dest_path", "")):
            if not raw:
                continue
            rel = self.mirror.rel(raw)
            if rel is not None:
                with self.lock:
                    self.pending[rel] = time.monotonic()

    def run(self) -> None:
        try:
            self._run()
        except Exception:
            log.exception("worker crashed")
            self.failed = True
            self.stop.set()

    def _run(self) -> None:
        last_full = self._full_sync_when_ready()
        last_warning = 0.0
        while not self.stop.is_set():
            now = time.monotonic()
            with self.lock:
                ready = [p for p, t in self.pending.items() if now - t >= SETTLE_SECONDS]
                for p in ready:
                    del self.pending[p]

            if ready and not self.mirror.ready():
                with self.lock:  # keep the work for when Drive is back
                    for p in ready:
                        self.pending.setdefault(p, now)
                if now - last_warning > 600:
                    log.warning("source or destination unavailable (is Google Drive running?); "
                                "%d change(s) waiting", len(self.pending))
                    last_warning = now
                self.stop.wait(UNAVAILABLE_RETRY)
                continue

            for p in sorted(ready, key=lambda r: len(r.parts)):  # parents before children
                try:
                    self.mirror.reconcile(p)
                except Exception:
                    log.exception("could not sync %s", p)

            if now - last_full >= self.full_sync_interval:
                last_full = self._full_sync_when_ready() or last_full
            self.stop.wait(0.5)

    def _full_sync_when_ready(self) -> float:
        """Run a full pass once source and dest are both present. Returns when it ran, or 0."""
        while not self.stop.is_set():
            if self.mirror.ready():
                try:
                    self.mirror.full_sync()
                except Exception:
                    log.exception("full sync failed")
                return time.monotonic()
            log.warning("waiting for %s and %s to be available", self.mirror.source, self.mirror.dest)
            self.stop.wait(UNAVAILABLE_RETRY)
        return 0.0


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", type=Path, default=DEFAULT_SOURCE,
                    help=f"folder to watch (default: {DEFAULT_SOURCE})")
    ap.add_argument("--dest", type=Path, default=None,
                    help="folder to copy into (default: auto-detect <Google Drive>/My Drive/BU)")
    ap.add_argument("--drive-folder", default=DEFAULT_DRIVE_FOLDER, metavar="NAME",
                    help="folder name inside My Drive when auto-detecting (default: BU)")
    ap.add_argument("--exclude", action="append", default=[], metavar="PATTERN",
                    help="extra name pattern to skip; repeatable (e.g. --exclude .git)")
    ap.add_argument("--no-default-excludes", action="store_true",
                    help="do not skip the built-in junk patterns (.DS_Store, *.tmp, ...)")
    ap.add_argument("--mirror-deletes", action="store_true",
                    help="also delete from DEST what was deleted in SOURCE")
    ap.add_argument("--full-sync-interval", type=float, default=FULL_SYNC_INTERVAL,
                    metavar="SECONDS", help="safety-net full pass interval (default: 900)")
    ap.add_argument("--once", action="store_true", help="run one full sync and exit")
    ap.add_argument("--dry-run", action="store_true", help="log what would happen; change nothing")
    ap.add_argument("--find-dest", action="store_true",
                    help="print the auto-detected destination and exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap.parse_args(argv)


def resolve_dest(args: argparse.Namespace, wait: bool) -> Path | None:
    if args.dest is not None:
        dest = args.dest.expanduser()
    else:
        while True:
            dest = find_drive_dest(args.drive_folder)
            if dest is not None:
                break
            if not wait:
                log.error("Google Drive folder not found. Is Google Drive for Desktop installed "
                          "and signed in? Otherwise pass --dest explicitly.")
                return None
            log.warning("Google Drive not mounted yet; retrying in 60s")
            time.sleep(60)
    if not dest.is_dir():
        if not dest.parent.is_dir():
            log.error("destination's parent folder does not exist: %s", dest.parent)
            return None
        if args.dry_run:
            log.info("would create %s", dest)
            return dest
        dest.mkdir()
        log.info("created %s", dest)
    return dest.resolve()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S", stream=sys.stdout)

    if args.find_dest:
        dest = find_drive_dest(args.drive_folder)
        if dest is None:
            print("Google Drive for Desktop folder not found. Is Google Drive installed and "
                  "signed in?", file=sys.stderr)
            return 1
        print(dest)
        return 0

    source = args.source.expanduser()
    if not source.is_dir():
        log.error("source folder does not exist: %s", source)
        return 1
    source = source.resolve()

    dest = resolve_dest(args, wait=not args.once)
    if dest is None:
        return 1
    if source == dest or source in dest.parents or dest in source.parents:
        log.error("source and destination overlap: %s / %s", source, dest)
        return 1

    excludes = ([] if args.no_default_excludes else list(DEFAULT_EXCLUDES)) + args.exclude
    mirror = Mirror(source, dest, excludes, args.mirror_deletes, args.dry_run)
    log.info("mirroring %s -> %s%s%s", source, dest,
             " (deletes mirrored)" if args.mirror_deletes else "",
             " [dry run]" if args.dry_run else "")

    if args.once:
        mirror.full_sync()
        return 0

    watcher = Watcher(mirror, args.full_sync_interval)
    observer = Observer()
    observer.schedule(watcher, str(source), recursive=True)
    observer.daemon = True
    observer.start()
    worker = threading.Thread(target=watcher.run, name="sync-worker", daemon=True)
    worker.start()

    def on_signal(signum: int, _frame: object) -> None:
        log.info("stopping (signal %d)", signum)
        watcher.stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    exit_code = 0
    try:
        while not watcher.stop.is_set():
            watcher.stop.wait(1.0)
            if not observer.is_alive():
                log.error("file watcher stopped unexpectedly; exiting so launchd restarts it")
                exit_code = 1
                break
    finally:
        watcher.stop.set()
        observer.stop()
        observer.join(timeout=5)
        worker.join(timeout=10)
    return 1 if watcher.failed else exit_code


if __name__ == "__main__":
    sys.exit(main())
