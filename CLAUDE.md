# CLAUDE.md

## What this is

`ghbackup.py` — a single-file, cross-platform disaster-recovery backup of a
GitHub account (owner: `dariushoule`). Captures the irreplaceable stuff if
GitHub ever becomes inaccessible: full git data **and** the rich metadata that
lives only on GitHub (issues, PRs, reviews, releases, etc.).

See `README.md` for full usage, options, scheduling, and restore steps.

## Key design decisions (settled — don't relitigate)

- **Runtime:** single-file Python 3 (stdlib only), shelling out to `gh` and
  `git`. Chosen for one codebase across Windows/Mac/Linux with no pip installs.
- **Auth:** uses the existing `gh` login. Token is read at runtime via
  `gh auth token` and passed to git as a per-command `http.extraheader` Basic
  auth header (GitHub git-over-HTTPS needs **Basic**, not Bearer). It is
  **never** persisted — saved repos' remote URLs are clean. No ssh dependency,
  so it works on a fresh box.
- **Scope:** owned repos (public + private) + gists. Forks and org repos are
  **excluded by default** (`--include-forks` exists; org support is a possible
  future `--orgs` flag since the token has `read:org`).
- **Formats:** git mirror clone (all refs) + a single-file `git bundle --all`
  per repo (offline-restorable); everything else as indented JSON.

## Incremental model

First run is full; later runs pull deltas. State per repo in
`metadata/.state.json` (`last_sync` + git refs fingerprint).

- Git: `git remote update` (only new objects).
- Bundle: rebuilt only when refs fingerprint changes.
- Issues + all comment buckets: GitHub `?since=` + merge by id.
- PR reviews: only re-fetched for PRs whose `updated_at` moved.
- Release assets: skipped if already present at same size.
- `--full` forces a from-scratch rebuild.

Known tradeoff: `since`-deltas can't see deletions → last-known copy is kept
(intended for DR).

## Gotchas for future edits

- `issues.json` includes PRs (GitHub's issues API behavior); `pulls.json` has
  PR-specific fields. This is intentional — keep raw fidelity.
- Per-repo failures are isolated and recorded in `manifest.json`; one bad repo
  must never abort the whole run.
- Backup output dirs are git-ignored — they contain private data. Never commit
  a backup destination.
