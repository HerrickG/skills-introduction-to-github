# BU → Google Drive mirror

Keeps the `BU` folder in your Google Drive in step with `~/Documents/Development/BU`.
Whenever a file is added or changed locally, it is copied into Drive within a
second or two. It runs in the background from login, survives reboots, and
never writes back the other way.

## How it works

Google Drive for Desktop mounts your Drive on the Mac at
`~/Library/CloudStorage/GoogleDrive-<your account>/My Drive`. Anything written
into that folder is uploaded by Drive itself. So this tool only has to copy
files from one local folder to another:

1. `bu_drive_sync.py` watches the source folder using macOS file events.
2. When a file changes it waits one second for the write to finish, then copies
   it (content and modification time) to the same relative path under
   `My Drive/BU`.
3. Every 15 minutes it also walks the whole folder and copies anything that
   differs in size or modification time, in case an event was missed.
4. A `launchd` agent starts it at login and restarts it if it ever exits.

## Requirements

- macOS.
- Google Drive for Desktop, installed and signed in to the account that owns
  the `BU` folder. (Already true if you see `Google Drive` in the Finder sidebar.)
- `python3`. If `python3 --version` fails in Terminal, run
  `xcode-select --install` first.

## Install

```sh
cd bu-drive-sync
./install.sh
```

The installer:

- creates a private Python environment under
  `~/Library/Application Support/bu-drive-sync`,
- finds your Drive `BU` folder (and tells you the path it picked),
- runs one full copy in the foreground so you can watch it and answer any
  "allow access to Documents" prompt macOS shows,
- writes `~/Library/LaunchAgents/local.bu-drive-sync.plist` and starts it.

Re-run `./install.sh` at any time to pick up a newer script or change options.

### Options

Flags given to `install.sh` are passed through to the sync program and baked
into the agent:

| Flag | Effect |
| --- | --- |
| `--mirror-deletes` | Also delete from Drive when you delete or rename away a file locally. Off by default, so Drive only ever gains or updates files. |
| `--exclude PATTERN` | Skip any file or folder whose name matches (repeatable). Example: `--exclude .git --exclude node_modules`. |
| `--full-sync-interval SECONDS` | Change the safety-net pass (default 900). |

Environment variables for the installer:

| Variable | Default |
| --- | --- |
| `BU_SOURCE` | `~/Documents/Development/BU` |
| `BU_DEST` | auto-detected `.../My Drive/BU` |

Examples:

```sh
./install.sh --mirror-deletes
./install.sh --exclude .git
BU_DEST="$HOME/Library/CloudStorage/GoogleDrive-you@example.com/My Drive/BU" ./install.sh
```

## Check it is working

```sh
tail -f ~/Library/Logs/bu-drive-sync.log        # live log
launchctl list | grep bu-drive-sync             # a PID in the first column means running
```

Then create or edit a file in `~/Documents/Development/BU`. A `copied ...` line
should appear in the log, the file should appear in `My Drive/BU` in Finder,
and Drive's menu bar icon should show it uploading.

## Things to know

- **Deletes and renames.** By default nothing is ever removed from Drive. A
  renamed file shows up under its new name and the old copy stays. Add
  `--mirror-deletes` if you want Drive to match the local folder exactly.
  Google-native documents (`.gdoc`, `.gsheet`, ...) sitting in the Drive folder
  are never deleted either way.
- **Skipped files.** `.DS_Store`, resource forks (`._*`), editor temp and swap
  files (`*.tmp`, `*.swp`, `~$*`, `.~lock.*`), and partial downloads are not
  copied. Add your own with `--exclude`.
- **Git repositories.** Syncing `.git` folders into Drive works but is noisy and
  Drive occasionally mangles them. If the folder holds git checkouts and you
  only care about the working files, install with `--exclude .git`.
- **First run.** The initial pass copies everything, so a large folder takes a
  while. Later passes only touch files that differ.
- **One way only.** Edits made in Drive do not come back. On the next full pass
  the local version wins and overwrites the Drive copy.

## Troubleshooting

**"Google Drive folder not found"** when installing. Drive for Desktop is not
installed, not signed in, or uses an unusual path. Find the folder in Finder,
then run the installer with `BU_DEST="<that path>" ./install.sh`.

**"Operation not permitted" in the log.** macOS is blocking the background
process from reading `~/Documents`. Open System Settings → Privacy & Security →
Full Disk Access, click +, and add the Python binary the installer printed at
the end (under `/Library/Developer/CommandLineTools/...` or Homebrew). Then
`./install.sh` again.

**Nothing in the log for a change you made.** Check the agent is running with
`launchctl list | grep bu-drive-sync`. If it is not, `./install.sh` restarts it
and prints any error.

**Copied, but not visible on drive.google.com.** The copy into the local Drive
folder is done; Drive for Desktop is still uploading. Click its menu bar icon
to see the queue.

## Uninstall

```sh
./uninstall.sh            # stop and remove the login agent
./uninstall.sh --purge    # also delete the Python environment and log
```

Files already copied into Drive are left alone.

## Running by hand

```sh
python3 -m pip install -r requirements.txt
python3 bu_drive_sync.py --once               # one full pass, then exit
python3 bu_drive_sync.py --once --dry-run     # show what would be copied
python3 bu_drive_sync.py                      # watch in the foreground (Ctrl-C to stop)
python3 bu_drive_sync.py --help
```
