#!/usr/bin/env python3
"""
ghbackup.py — Disaster-recovery backup of your GitHub account.

Backs up, into a local directory, for every repo you own (plus your gists):
  * Full git data via `git clone --mirror`  (ALL branches, tags, notes)
  * A single-file `git bundle --all`         (offline, restore-anywhere artifact)
  * Wiki git repo (if present)
  * Issues + comments, PRs + comments + reviews, commit comments
  * Labels, milestones, releases + downloadable release assets
  * Raw repo metadata (settings, topics, description)
  * Discussions (best-effort, via GraphQL)

Everything non-git is stored as plain JSON so it stays greppable and restorable
forever. Re-running updates existing mirrors instead of recloning, so it's cheap
to schedule.

Requirements: Python 3.8+, `gh` (authenticated: `gh auth login`), and `git`.
No pip installs. Runs on Windows, macOS, and Linux.

Usage:
    python ghbackup.py --out ./backup
    python ghbackup.py --out D:\\gh-backup --no-assets        # skip large binaries
    python ghbackup.py --out ./backup --repo my-one-repo      # just one repo
"""

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

TOOL_VERSION = "1.0.0"

# Collected non-fatal problems, surfaced in the manifest at the end.
WARNINGS = []


def log(msg):
    print(f"  {msg}", flush=True)


def warn(msg):
    WARNINGS.append(msg)
    print(f"  ! {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# Subprocess helpers
# --------------------------------------------------------------------------- #

def _redact(cmd):
    """Render a command for display, hiding any auth header so the token never
    lands in an error message, the manifest, or a log."""
    out = []
    for arg in cmd:
        if "extraheader" in arg.lower() or "authorization" in arg.lower():
            out.append("http.https://github.com/.extraheader=<redacted>")
        else:
            out.append(arg)
    return " ".join(out)


def run(cmd, check=True, retries=2):
    """Run a command, returning stdout as text. Retries on failure."""
    last = None
    for attempt in range(retries + 1):
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if proc.returncode == 0:
            return proc.stdout.decode("utf-8", "replace")
        last = proc.stderr.decode("utf-8", "replace").strip()
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    if check:
        raise RuntimeError(f"command failed: {_redact(cmd)}\n    {last}")
    return None


def gh_token():
    tok = run(["gh", "auth", "token"]).strip()
    if not tok:
        raise RuntimeError("could not get a token from `gh auth token`")
    return tok


def gh_api(path, paginate=True):
    """Call the GitHub REST API through gh. Auto-paginates array endpoints."""
    cmd = ["gh", "api"]
    if paginate:
        # ensure a large page size so we make as few calls as possible
        sep = "&" if "?" in path else "?"
        if "per_page=" not in path:
            path = f"{path}{sep}per_page=100"
        cmd.append("--paginate")
    cmd.append(path)
    return run(cmd, check=False)


def gh_api_json(path, paginate=True):
    """Call the REST API and parse JSON. Returns None on failure (e.g. 404)."""
    out = gh_api(path, paginate=paginate)
    if out is None:
        return None
    out = out.strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        warn(f"could not parse JSON from {path}: {e}")
        return None


# --------------------------------------------------------------------------- #
# Filesystem helpers
# --------------------------------------------------------------------------- #

def write_json(path: Path, data):
    """Atomic JSON write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def safe_name(name: str) -> str:
    """Make a string safe to use as a single path component on all OSes."""
    bad = '<>:"/\\|?*'
    out = "".join("_" if c in bad else c for c in name).strip(". ")
    return out or "_"


def read_json(path: Path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def add_query(path: str, key: str, value: str) -> str:
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}{key}={value}"


def merge_by_id(existing_list, delta_list, id_field="id"):
    """Merge a delta into an existing list, keyed by id. Returns a sorted list."""
    by_id = {item[id_field]: item for item in existing_list if id_field in item}
    for item in delta_list:
        if id_field in item:
            by_id[item[id_field]] = item
    return sorted(by_id.values(), key=lambda x: x[id_field])


# --------------------------------------------------------------------------- #
# Git mirror / bundle
# --------------------------------------------------------------------------- #

def git_auth_args(token):
    # GitHub's git-over-HTTPS expects Basic auth (base64 of "x-access-token:<tok>").
    # Sent only for github.com, only on this command line via `-c` — never written
    # to the repo's config, so the token is not persisted into the backup.
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return ["-c", f"http.https://github.com/.extraheader=Authorization: Basic {basic}"]


def mirror_repo(clone_url, dest: Path, token):
    """Mirror-clone (or update) a bare repo at `dest`."""
    auth = git_auth_args(token)
    if (dest / "HEAD").exists() or (dest / "config").exists():
        # Existing mirror -> refresh it.
        run(["git", *auth, "-C", str(dest), "remote", "update", "--prune"], check=True)
    else:
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        run(["git", *auth, "clone", "--mirror", clone_url, str(dest)], check=True)


def download_to_file(cmd, dest: Path, retries=2):
    """Run a command and stream its stdout straight to `dest` (atomic, low-memory)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(retries + 1):
        with open(tmp, "wb") as f:
            proc = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE)
        if proc.returncode == 0:
            os.replace(tmp, dest)
            return True
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    tmp.unlink(missing_ok=True)
    return False


def bundle_repo(mirror_dir: Path, bundle_path: Path):
    """Write a single-file `--all` bundle from a mirror dir."""
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = bundle_path.with_suffix(".bundle.tmp")
    run(["git", "-C", str(mirror_dir), "bundle", "create", str(tmp), "--all"], check=True)
    os.replace(tmp, bundle_path)


# --------------------------------------------------------------------------- #
# Discussions (GraphQL, best-effort)
# --------------------------------------------------------------------------- #

DISCUSSIONS_QUERY = """
query($owner:String!, $name:String!, $cursor:String) {
  repository(owner:$owner, name:$name) {
    discussions(first:50, after:$cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title body createdAt updatedAt url
        author { login }
        category { name }
        comments(first:100) {
          nodes {
            body createdAt author { login }
            replies(first:100) { nodes { body createdAt author { login } } }
          }
        }
      }
    }
  }
}
"""


def fetch_discussions(owner, name):
    nodes, cursor = [], None
    while True:
        cmd = ["gh", "api", "graphql",
               "-f", f"query={DISCUSSIONS_QUERY}",
               "-F", f"owner={owner}", "-F", f"name={name}"]
        if cursor:
            cmd += ["-F", f"cursor={cursor}"]
        out = run(cmd, check=False)
        if out is None:
            return None  # discussions likely disabled; caller ignores
        data = json.loads(out)
        d = (((data or {}).get("data") or {}).get("repository") or {}).get("discussions")
        if not d:
            return None
        nodes.extend(d["nodes"])
        if d["pageInfo"]["hasNextPage"]:
            cursor = d["pageInfo"]["endCursor"]
        else:
            return nodes


# --------------------------------------------------------------------------- #
# Per-repo backup
# --------------------------------------------------------------------------- #

def backup_repo(repo, root: Path, token, opts, run_started_iso):
    owner = repo["owner"]["login"]
    name = repo["name"]
    full = repo["full_name"]

    rdir = root / "repos" / safe_name(name)
    meta = rdir / "metadata"
    rdir.mkdir(parents=True, exist_ok=True)

    # Per-repo incremental state: last successful sync time + git fingerprint.
    state = {} if opts.full else read_json(meta / ".state.json", {})
    last_sync = state.get("last_sync")  # ISO string, or None on first/full run
    mode = "delta" if last_sync else "full"
    log(f"repo {full}  [{mode}]")

    # --- git data (always a delta fetch once the mirror exists) ----------- #
    mirror = rdir / "git" / f"{safe_name(name)}.git"
    mirror_repo(repo["clone_url"], mirror, token)
    refs_out = run(["git", "-C", str(mirror), "for-each-ref",
                    "--format=%(objectname) %(refname)"], check=False) or ""
    new_fp = hashlib.sha256(refs_out.encode("utf-8")).hexdigest()
    if not opts.no_bundles and refs_out.strip():  # empty repo has no refs to bundle
        bundle_path = rdir / f"{safe_name(name)}.bundle"
        # Only rebuild the bundle if the git refs actually changed.
        if opts.full or new_fp != state.get("refs_fingerprint") or not bundle_path.exists():
            bundle_repo(mirror, bundle_path)

    # --- wiki (separate git repo) ----------------------------------------- #
    if repo.get("has_wiki") and not opts.no_wiki:
        wiki_url = repo["clone_url"][:-4] + ".wiki.git"  # strip ".git", add ".wiki.git"
        wiki_dir = rdir / "git" / f"{safe_name(name)}.wiki.git"
        try:
            mirror_repo(wiki_url, wiki_dir, token)
        except RuntimeError:
            # has_wiki is true even when the wiki was never created -> ignore.
            shutil.rmtree(wiki_dir, ignore_errors=True)

    # --- always-save raw repo object -------------------------------------- #
    write_json(meta / "repo.json", repo)

    # --- incremental collections (GitHub `since=` + merge by id) ---------- #
    # These endpoints filter by updated_at, so we fetch only what changed and
    # merge into the existing copy (deletions can't be detected -> we keep the
    # last-known record, which is what we want for disaster recovery).
    incremental = {
        "issues.json":               f"repos/{full}/issues?state=all",  # incl. PRs
        "issue_comments.json":       f"repos/{full}/issues/comments",
        "pull_review_comments.json": f"repos/{full}/pulls/comments",
        "commit_comments.json":      f"repos/{full}/comments",
    }
    for fname, path in incremental.items():
        if last_sync:
            path = add_query(path, "since", last_sync)
        delta = gh_api_json(path) or []
        if last_sync:
            delta = merge_by_id(read_json(meta / fname, []), delta)
        write_json(meta / fname, delta)

    # --- full-refetch collections (no `since` support; all small) --------- #
    full_collections = {
        "labels.json":     f"repos/{full}/labels",
        "milestones.json": f"repos/{full}/milestones?state=all",
        "pulls.json":      f"repos/{full}/pulls?state=all",
        "releases.json":   f"repos/{full}/releases",
    }
    data = {}
    for fname, path in full_collections.items():
        result = gh_api_json(path) or []
        data[fname] = result
        write_json(meta / fname, result)

    # --- PR reviews (per-PR; only re-fetch PRs that changed) -------------- #
    reviews = {} if opts.full else read_json(meta / "pull_reviews.json", {})
    for pr in data.get("pulls.json", []):
        num = str(pr["number"])
        # Skip PRs untouched since our last sync -> avoids a call per old PR.
        if last_sync and num in reviews and (pr.get("updated_at") or "") <= last_sync:
            continue
        r = gh_api_json(f"repos/{full}/pulls/{pr['number']}/reviews")
        if r:
            reviews[num] = r
    write_json(meta / "pull_reviews.json", reviews)

    # Recompute issue/pull counts from the (merged) files for the manifest.
    issues_total = len(read_json(meta / "issues.json", []))

    # --- release assets (binaries, not in git) ---------------------------- #
    if not opts.no_assets:
        for rel in data.get("releases.json", []):
            tag = safe_name(rel.get("tag_name") or str(rel.get("id")))
            for asset in rel.get("assets", []):
                dest = meta / "assets" / tag / safe_name(asset["name"])
                if dest.exists() and dest.stat().st_size == asset.get("size", -1):
                    continue  # already downloaded, unchanged
                cmd = ["gh", "api", "-H", "Accept: application/octet-stream",
                       f"repos/{full}/releases/assets/{asset['id']}"]
                if not download_to_file(cmd, dest):
                    warn(f"failed to download asset {asset['name']} from {full}")

    # --- discussions (best-effort) ---------------------------------------- #
    if repo.get("has_discussions") and not opts.no_discussions:
        try:
            disc = fetch_discussions(owner, name)
            if disc is not None:
                write_json(meta / "discussions.json", disc)
        except Exception as e:  # noqa: BLE001  (best-effort, never fatal)
            warn(f"discussions fetch failed for {full}: {e}")

    # --- persist incremental state (only on success) ---------------------- #
    write_json(meta / ".state.json", {
        "last_sync": run_started_iso,
        "refs_fingerprint": new_fp,
    })

    return {
        "full_name": full,
        "private": repo.get("private"),
        "mode": mode,
        "issues": issues_total,
        "pulls": len(data.get("pulls.json", [])),
        "releases": len(data.get("releases.json", [])),
    }


# --------------------------------------------------------------------------- #
# Gists
# --------------------------------------------------------------------------- #

def backup_gists(root: Path, token):
    gists = gh_api_json("gists")
    if not gists:
        return []
    summaries = []
    for g in gists:
        gid = g["id"]
        log(f"gist {gid}")
        gdir = root / "gists" / gid
        write_json(gdir / "gist.json", g)
        try:
            mirror_repo(g["git_pull_url"], gdir / "git" / f"{gid}.git", token)
        except RuntimeError as e:
            warn(f"gist {gid} clone failed: {e}")
        summaries.append({
            "id": gid,
            "description": g.get("description"),
            "public": g.get("public"),
            "files": list((g.get("files") or {}).keys()),
        })
    return summaries


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def list_owned_repos(include_forks):
    repos = gh_api_json("user/repos?affiliation=owner")
    if not repos:
        return []
    if not include_forks:
        repos = [r for r in repos if not r.get("fork")]
    return repos


def main():
    ap = argparse.ArgumentParser(description="Disaster-recovery backup of your GitHub account.")
    ap.add_argument("--out", required=True, help="Output directory for the backup.")
    ap.add_argument("--repo", action="append", default=[],
                    help="Only back up this repo name (repeatable).")
    ap.add_argument("--include-forks", action="store_true", help="Include forked repos.")
    ap.add_argument("--no-gists", action="store_true", help="Skip gists.")
    ap.add_argument("--no-bundles", action="store_true", help="Skip .bundle files.")
    ap.add_argument("--no-wiki", action="store_true", help="Skip wikis.")
    ap.add_argument("--no-assets", action="store_true", help="Skip release asset downloads.")
    ap.add_argument("--no-discussions", action="store_true", help="Skip discussions.")
    ap.add_argument("--full", action="store_true",
                    help="Ignore saved state and re-fetch everything from scratch.")
    opts = ap.parse_args()

    # Fail fast if tools are missing.
    for tool in ("gh", "git"):
        if not shutil.which(tool):
            sys.exit(f"error: `{tool}` not found on PATH. Install it and retry.")

    root = Path(opts.out).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)

    # Stamp the run BEFORE fetching, so next run's `since` window can't miss
    # anything updated while this run was in flight (merges are idempotent).
    run_started_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    token = gh_token()
    who = gh_api_json("user", paginate=False) or {}
    print(f"Backing up GitHub account '{who.get('login')}' -> {root}\n")

    repos = list_owned_repos(opts.include_forks)
    if opts.repo:
        wanted = set(opts.repo)
        repos = [r for r in repos if r["name"] in wanted]
    print(f"Found {len(repos)} repo(s) to back up.\n")

    repo_results, repo_errors = [], []
    for r in repos:
        try:
            repo_results.append(backup_repo(r, root, token, opts, run_started_iso))
        except Exception as e:  # noqa: BLE001  (isolate per-repo failures)
            warn(f"repo {r['full_name']} FAILED: {e}")
            repo_errors.append({"full_name": r["full_name"], "error": str(e)})

    gist_results = []
    if not opts.no_gists and not opts.repo:
        gist_results = backup_gists(root, token)

    manifest = {
        "tool": "ghbackup.py",
        "tool_version": TOOL_VERSION,
        "account": who.get("login"),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "repo_count": len(repo_results),
        "gist_count": len(gist_results),
        "options": {k: v for k, v in vars(opts).items()},
        "repos": repo_results,
        "gists": gist_results,
        "errors": repo_errors,
        "warnings": WARNINGS,
    }
    write_json(root / "manifest.json", manifest)

    print(f"\nDone. {len(repo_results)} repo(s), {len(gist_results)} gist(s).")
    if repo_errors or WARNINGS:
        print(f"Completed with {len(repo_errors)} error(s) and {len(WARNINGS)} warning(s); "
              f"see manifest.json.")
        sys.exit(1)


if __name__ == "__main__":
    main()
