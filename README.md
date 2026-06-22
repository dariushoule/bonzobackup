# BonzoBackup

A single-file, cross-platform disaster-recovery backup for your entire GitHub
account. It captures the stuff that's irreplaceable if GitHub ever becomes
inaccessible to you: not just code, but all the metadata that lives only
on GitHub's servers.

It leans entirely on the official **`gh`** CLI for auth and API access, so there
are no tokens to manage and no Python dependencies to install.

## Quickstart

```bash
gh auth login                       # once, if you haven't already
python3 ghbackup.py --out ./backup  # full backup; re-run any time (incremental)
```

Add `--no-assets` to skip large release binaries.

## What it backs up

For every repo you **own** (public + private; forks skipped by default), plus
all your **gists**:

| Data | How | Format |
|------|-----|--------|
| Full git history (**all** branches, tags, notes) | `git clone --mirror` | bare git repo |
| Offline restore artifact | `git bundle --all` | single `.bundle` file |
| Wiki | mirror of `<repo>.wiki.git` | bare git repo |
| Issues + comments | REST | JSON |
| Pull requests + comments + reviews | REST | JSON |
| Commit comments | REST | JSON |
| Labels, milestones | REST | JSON |
| Releases + **downloadable assets** (binaries) | REST | JSON + files |
| Repo settings / topics / description | REST | JSON |
| Discussions | GraphQL (best-effort) | JSON |
| Gists (incl. secret) | mirror clone + metadata | git + JSON |

Everything non-git is plain, indented JSON, so it stays greppable and
restorable forever.

## Requirements

- [`gh`](https://cli.github.com/), authenticated once via `gh auth login`
- `git`
- Python 3.8+

## Usage

```bash
python3 ghbackup.py --out ./backup
```

Backups are incremental, so subsequent runs are faster.

### Options

| Flag | Effect |
|------|--------|
| `--out DIR` | Where to write the backup (required). |
| `--repo NAME` | Back up only this repo (repeatable). Skips gists. |
| `--include-forks` | Include forked repos (off by default). |
| `--no-gists` | Skip gists. |
| `--no-bundles` | Skip the `.bundle` files (mirror repos are still saved). |
| `--no-wiki` | Skip wikis. |
| `--no-assets` | Skip downloading release binaries (can be large). |
| `--no-discussions` | Skip discussions. |
| `--full` | Ignore saved state and re-fetch everything from scratch. |

## How "incremental" works

The first run is a full backup. Every run after that only pulls **deltas**:

- **Git data:** `git remote update` fetches only new objects.
- **Bundle:** regenerated only when the repo's refs actually changed
  (fingerprint check), so unchanged repos cost nothing.
- **Issues + all comment types:** fetched with GitHub's `?since=` filter and
  merged into the existing copy by id.
- **PR reviews:** only re-fetched for PRs whose `updated_at` moved.
- **Release assets:** skipped if already downloaded at the same size.

Per-repo sync state lives in `metadata/.state.json`. Use `--full` to rebuild.

> **Caveat:** `since`-based deltas can't detect *deletions*. A deleted issue or
> comment simply won't appear in the delta, so the last-known copy is kept. For
> disaster recovery that's the desired behavior (keep, don't lose).

## Output layout

```
backup/
├── manifest.json                      # run summary: counts, options, errors, warnings
├── repos/
│   └── <repo>/
│       ├── <repo>.bundle              # single-file, offline-restorable
│       ├── git/
│       │   ├── <repo>.git/            # full mirror (all branches/tags)
│       │   └── <repo>.wiki.git/       # wiki, if any
│       └── metadata/
│           ├── repo.json  issues.json  pulls.json  pull_reviews.json
│           ├── issue_comments.json  pull_review_comments.json  commit_comments.json
│           ├── labels.json  milestones.json  releases.json  discussions.json
│           ├── assets/<tag>/<file>    # release binaries
│           └── .state.json            # incremental sync state
└── gists/
    └── <id>/  (gist.json + git/<id>.git/)
```

> Note: `issues.json` includes pull requests too, since that's how GitHub's
> issues API works. `pulls.json` has the PR-specific fields.

## Scheduling (so it just runs)

**macOS / Linux (cron), weekly, Sundays 3am:**

```cron
0 3 * * 0 /usr/bin/python3 /path/to/ghbackup.py --out /path/to/backup >> /path/to/backup/cron.log 2>&1
```

**Windows (Task Scheduler):**

```powershell
$action  = New-ScheduledTaskAction -Execute "python" -Argument "C:\tools\ghbackup.py --out D:\gh-backup"
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 3am
Register-ScheduledTask -TaskName "GitHub Backup" -Action $action -Trigger $trigger
```

The process exits non-zero and records details in `manifest.json` if any repo
fails, so your scheduler/log will surface problems.

## Security notes

- Auth uses your existing `gh` login. The token is read at runtime via
  `gh auth token` and passed to git as a per-command header. It is **never**
  written into the backup (the saved repos' remote URLs are clean).
- Your backup still contains private source and private issue/PR text. Store it
  somewhere you trust (encrypted disk, etc.).
