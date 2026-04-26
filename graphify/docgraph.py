"""doc-graph branch management — CI-driven incremental knowledge graph on a dedicated branch.

Workflow:
  1. Developer creates MR from feature branch → release branch.
  2. Leader reviews and merges the MR in GitLab.
  3. GitLab CI fires a pipeline on the release branch push.
  4. `graphify doc-graph update` runs in that pipeline:
       - Restores graphify-out/ from the doc-graph branch (graph history).
       - Runs an incremental AST rebuild for changed files (no LLM needed).
       - Pushes the updated graphify-out/ back to the doc-graph branch.

The doc-graph branch holds ONLY graphify-out/ — no source code.
Keeps merge conflicts impossible: developers never touch graph.json directly.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, check=check)


def _branch_exists_remote(root: Path, branch: str) -> bool:
    r = _git(["ls-remote", "--heads", "origin", branch], cwd=root, check=False)
    return bool(r.stdout.strip())


def _branch_exists_local(root: Path, branch: str) -> bool:
    r = _git(["rev-parse", "--verify", branch], cwd=root, check=False)
    return r.returncode == 0


def setup(root: Path = Path("."), branch: str = "doc-graph") -> None:
    """Create the doc-graph branch if it doesn't already exist.

    Creates an orphan branch (no source code, only graphify-out/) and pushes it
    to origin. Safe to call multiple times — exits early if branch already exists.
    """
    root = root.resolve()

    if _branch_exists_remote(root, branch) or _branch_exists_local(root, branch):
        print(f"[graphify doc-graph] Branch '{branch}' already exists. Nothing to do.")
        return

    print(f"[graphify doc-graph] Creating orphan branch '{branch}'...")

    with tempfile.TemporaryDirectory() as tmp:
        wt_path = Path(tmp) / "doc-graph-init"

        # git worktree add --orphan requires git >= 2.36; fall back to plumbing if older
        r = _git(["worktree", "add", "--orphan", "-b", branch, str(wt_path)], cwd=root, check=False)
        if r.returncode != 0:
            _setup_via_plumbing(root, branch)
            return

        try:
            _git(["reset", "--hard"], cwd=wt_path, check=False)
            out_dir = wt_path / "graphify-out"
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / ".gitkeep").write_text("", encoding="utf-8")
            _git(["add", "."], cwd=wt_path)
            _git(["commit", "-m", "chore: initialize doc-graph branch"], cwd=wt_path)
            _git(["push", "origin", branch], cwd=wt_path)
            print(f"[graphify doc-graph] Pushed orphan branch '{branch}' to origin.")
        finally:
            _git(["worktree", "remove", "--force", str(wt_path)], cwd=root, check=False)
            _git(["branch", "-D", branch], cwd=root, check=False)


def _setup_via_plumbing(root: Path, branch: str) -> None:
    """Fallback for git < 2.36: create an empty orphan branch via low-level git commands."""
    # Create an empty tree object
    r = _git(["hash-object", "-t", "tree", "--stdin"], cwd=root, check=False)
    if r.returncode != 0:
        # Provide empty content via stdin manually
        proc = subprocess.run(
            ["git", "hash-object", "-t", "tree", "--stdin"],
            input="",
            cwd=root,
            capture_output=True,
            text=True,
        )
        empty_tree = proc.stdout.strip()
    else:
        empty_tree = r.stdout.strip()

    if not empty_tree:
        raise RuntimeError("Could not create empty tree object. Ensure git is installed.")

    commit = _git(
        ["commit-tree", empty_tree, "-m", "chore: initialize doc-graph branch"],
        cwd=root,
    ).stdout.strip()

    _git(["push", "origin", f"{commit}:refs/heads/{branch}"], cwd=root)
    print(f"[graphify doc-graph] Pushed orphan branch '{branch}' to origin (via plumbing).")


def restore(root: Path = Path("."), branch: str = "doc-graph") -> bool:
    """Restore graphify-out/ from the doc-graph branch into the working directory.

    Called at the start of the CI job to resume from the previous graph state so
    that incremental updates can read manifest.json and graph.json.

    Returns True if files were restored, False if the branch or graphify-out/ is new.
    """
    root = root.resolve()

    r = _git(["fetch", "origin", branch], cwd=root, check=False)
    if r.returncode != 0:
        print(f"[graphify doc-graph] Branch '{branch}' not found on origin — starting with empty graph.")
        return False

    with tempfile.TemporaryDirectory() as tmp:
        wt_path = Path(tmp) / "restore-wt"
        r = _git(
            ["worktree", "add", "--detach", str(wt_path), f"origin/{branch}"],
            cwd=root,
            check=False,
        )
        if r.returncode != 0:
            print(f"[graphify doc-graph] Could not checkout '{branch}' — starting with empty graph.")
            return False

        try:
            src = wt_path / "graphify-out"
            dst = root / "graphify-out"
            if not src.exists():
                print(f"[graphify doc-graph] No graphify-out/ on '{branch}' yet — starting fresh.")
                return False
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
            print(f"[graphify doc-graph] Restored graphify-out/ from '{branch}'.")
            return True
        finally:
            _git(["worktree", "remove", "--force", str(wt_path)], cwd=root, check=False)


def push(root: Path = Path("."), branch: str = "doc-graph", message: str = "graph: incremental update") -> bool:
    """Commit the updated graphify-out/ to the doc-graph branch and push to origin.

    Uses a temporary git worktree so the current branch (release) is untouched.
    Returns True on success.
    """
    root = root.resolve()
    src_out = root / "graphify-out"

    if not src_out.exists():
        print("[graphify doc-graph] No graphify-out/ found — nothing to push.")
        return False

    has_remote = _branch_exists_remote(root, branch)
    if has_remote:
        _git(["fetch", "origin", branch], cwd=root, check=False)

    with tempfile.TemporaryDirectory() as tmp:
        wt_path = Path(tmp) / "push-wt"

        if has_remote:
            r = _git(
                ["worktree", "add", "--detach", str(wt_path), f"origin/{branch}"],
                cwd=root,
                check=False,
            )
        else:
            r = subprocess.CompletedProcess(args=[], returncode=1)

        if r.returncode != 0:
            # Branch doesn't exist remotely — create orphan worktree
            r2 = _git(
                ["worktree", "add", "--orphan", "-b", f"_docgraph_push_{os.getpid()}", str(wt_path)],
                cwd=root,
                check=False,
            )
            if r2.returncode != 0:
                print("[graphify doc-graph] Could not create push worktree. Run `graphify doc-graph setup` first.")
                return False

        try:
            wt_out = wt_path / "graphify-out"
            if wt_out.exists():
                shutil.rmtree(wt_out)
            shutil.copytree(src_out, wt_out)

            _git(["add", "graphify-out/"], cwd=wt_path)

            # Skip commit if nothing changed
            diff = _git(["diff", "--cached", "--quiet"], cwd=wt_path, check=False)
            if diff.returncode == 0:
                print("[graphify doc-graph] Graph unchanged — nothing to push.")
                return True

            _git(["commit", "-m", message], cwd=wt_path)
            _git(["push", "origin", f"HEAD:refs/heads/{branch}"], cwd=wt_path)
            print(f"[graphify doc-graph] Pushed updated graph to '{branch}'.")
            return True
        finally:
            _git(["worktree", "remove", "--force", str(wt_path)], cwd=root, check=False)
            # Clean up temp local branch if we created one
            _git(["branch", "-D", f"_docgraph_push_{os.getpid()}"], cwd=root, check=False)


def update(
    root: Path = Path("."),
    branch: str = "doc-graph",
    release_branch: str = "release",
) -> bool:
    """Full CI update flow: restore graph state → incremental rebuild → push to doc-graph.

    Called by the GitLab CI job after an MR is merged into the release branch.
    Reads changed files from GitLab CI environment variables automatically.
    """
    from graphify.watch import _rebuild_code

    root = root.resolve()

    # Determine which files changed (GitLab CI env vars or git diff fallback)
    changed_raw = _detect_changed_files(root)
    if changed_raw:
        os.environ["GRAPHIFY_CHANGED"] = changed_raw
        n = len([f for f in changed_raw.splitlines() if f.strip()])
        print(f"[graphify doc-graph] {n} changed file(s) detected.")
    else:
        print("[graphify doc-graph] No changed files — performing full rebuild.")

    # Build a human-readable commit message using MR context
    mr_iid = os.environ.get("CI_MERGE_REQUEST_IID")
    sha = (os.environ.get("CI_COMMIT_SHORT_SHA") or os.environ.get("CI_COMMIT_SHA", ""))[:8]
    if mr_iid:
        message = f"graph: update from MR !{mr_iid} ({sha})"
    else:
        message = f"graph: incremental update ({sha})" if sha else "graph: incremental update"

    # Step 1: restore previous graph state from doc-graph
    restore(root, branch)

    # Step 2: incremental AST rebuild (no LLM, fast)
    ok = _rebuild_code(root)
    if not ok:
        print("[graphify doc-graph] Rebuild failed — aborting push.")
        return False

    # Step 3: push updated graphify-out/ back to doc-graph
    return push(root, branch, message)


def _detect_changed_files(root: Path) -> str:
    """Return newline-separated list of changed files, from CI env or git diff."""
    # Honour manual override
    manual = os.environ.get("GRAPHIFY_CHANGED", "").strip()
    if manual:
        return manual

    before = os.environ.get("CI_COMMIT_BEFORE_SHA", "").strip()
    current = os.environ.get("CI_COMMIT_SHA", "HEAD").strip()

    if before and before != "0" * 40:
        r = subprocess.run(
            ["git", "diff", "--name-only", f"{before}..{current}"],
            cwd=root,
            capture_output=True,
            text=True,
        )
        if r.returncode == 0:
            return r.stdout.strip()

    # Fallback: diff against HEAD~1
    r = subprocess.run(
        ["git", "diff", "--name-only", "HEAD~1", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    return r.stdout.strip() if r.returncode == 0 else ""


def status(root: Path = Path("."), branch: str = "doc-graph") -> str:
    """Check whether the doc-graph branch exists locally and on origin."""
    root = root.resolve()
    local = _branch_exists_local(root, branch)
    remote = _branch_exists_remote(root, branch)

    lines = [f"branch: {branch}"]
    lines.append(f"  local:  {'exists' if local else 'not found'}")
    lines.append(f"  remote: {'exists' if remote else 'not found'}")

    if remote:
        r = _git(["log", "--oneline", "-5", f"origin/{branch}"], cwd=root, check=False)
        _git(["fetch", "origin", branch], cwd=root, check=False)
        r = _git(["log", "--oneline", "-5", f"origin/{branch}"], cwd=root, check=False)
        if r.returncode == 0 and r.stdout.strip():
            lines.append("  recent commits:")
            for line in r.stdout.strip().splitlines():
                lines.append(f"    {line}")

    return "\n".join(lines)


# Standalone CI helper script — generated into the user's own repo.
# Depends only on the published `graphify` package (graphify.watch._rebuild_code).
# No dependency on docgraph.py or any unreleased graphify module.
_CI_SCRIPT = '''\
#!/usr/bin/env python3
"""Graphify doc-graph CI update script.

Generated by: graphify doc-graph gen-ci
Commit this file to your repo. The GitLab CI job calls it directly,
so it works with any published version of the graphify package.

Dependencies: graphify (pip install graphify)
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _git(args, cwd, check=True):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, check=check)


def restore(root, branch):
    """Restore graphify-out/ from the doc-graph branch into the working directory."""
    r = _git(["fetch", "origin", branch], cwd=root, check=False)
    if r.returncode != 0:
        print(f"[graphify] Branch \\'{branch}\\' not found on origin — starting with empty graph.")
        return False
    with tempfile.TemporaryDirectory() as tmp:
        wt = Path(tmp) / "restore"
        r = _git(["worktree", "add", "--detach", str(wt), f"origin/{branch}"], cwd=root, check=False)
        if r.returncode != 0:
            print(f"[graphify] Could not checkout \\'{branch}\\' — starting with empty graph.")
            return False
        try:
            src = wt / "graphify-out"
            dst = root / "graphify-out"
            if not src.exists():
                print(f"[graphify] No graphify-out/ on \\'{branch}\\' yet — starting fresh.")
                return False
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
            print(f"[graphify] Restored graphify-out/ from \\'{branch}\\'.")
            return True
        finally:
            _git(["worktree", "remove", "--force", str(wt)], cwd=root, check=False)


def push(root, branch, message):
    """Commit the updated graphify-out/ to the doc-graph branch and push."""
    src = root / "graphify-out"
    if not src.exists():
        print("[graphify] No graphify-out/ — nothing to push.")
        return False

    has_remote = bool(
        _git(["ls-remote", "--heads", "origin", branch], cwd=root, check=False).stdout.strip()
    )
    if has_remote:
        _git(["fetch", "origin", branch], cwd=root, check=False)

    pid = os.getpid()
    with tempfile.TemporaryDirectory() as tmp:
        wt = Path(tmp) / "push"
        if has_remote:
            r = _git(["worktree", "add", "--detach", str(wt), f"origin/{branch}"], cwd=root, check=False)
        else:
            r = subprocess.CompletedProcess([], 1)
        if r.returncode != 0:
            tmp_br = f"_graphify_push_{pid}"
            r = _git(["worktree", "add", "--orphan", "-b", tmp_br, str(wt)], cwd=root, check=False)
            if r.returncode != 0:
                print("[graphify] Cannot create push worktree. Run \\'graphify doc-graph setup\\' first.")
                return False
        try:
            wt_out = wt / "graphify-out"
            if wt_out.exists():
                shutil.rmtree(wt_out)
            shutil.copytree(src, wt_out)
            _git(["add", "graphify-out/"], cwd=wt)
            if _git(["diff", "--cached", "--quiet"], cwd=wt, check=False).returncode == 0:
                print("[graphify] Graph unchanged — nothing to push.")
                return True
            _git(["commit", "-m", message], cwd=wt)
            _git(["push", "origin", f"HEAD:refs/heads/{branch}"], cwd=wt)
            print(f"[graphify] Pushed updated graph to \\'{branch}\\'.")
            return True
        finally:
            _git(["worktree", "remove", "--force", str(wt)], cwd=root, check=False)
            _git(["branch", "-D", f"_graphify_push_{pid}"], cwd=root, check=False)


def detect_changed(root):
    """Return newline-separated list of changed files from CI env or git diff."""
    manual = os.environ.get("GRAPHIFY_CHANGED", "").strip()
    if manual:
        return manual
    before = os.environ.get("CI_COMMIT_BEFORE_SHA", "").strip()
    current = os.environ.get("CI_COMMIT_SHA", "HEAD").strip()
    if before and before != "0" * 40:
        r = subprocess.run(
            ["git", "diff", "--name-only", f"{before}..{current}"],
            cwd=root, capture_output=True, text=True,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    r = subprocess.run(
        ["git", "diff", "--name-only", "HEAD~1", "HEAD"],
        cwd=root, capture_output=True, text=True,
    )
    return r.stdout.strip() if r.returncode == 0 else ""


def main():
    import argparse
    p = argparse.ArgumentParser(description="Graphify doc-graph CI update")
    p.add_argument("--graph-branch", default="doc-graph")
    p.add_argument("--release-branch", default="release")
    args = p.parse_args()

    root = Path(".").resolve()

    changed = detect_changed(root)
    if changed:
        os.environ["GRAPHIFY_CHANGED"] = changed
        n = len([f for f in changed.splitlines() if f.strip()])
        print(f"[graphify] {n} changed file(s) detected.")
    else:
        print("[graphify] No changed files detected — full rebuild.")

    mr_iid = os.environ.get("CI_MERGE_REQUEST_IID")
    sha = (os.environ.get("CI_COMMIT_SHORT_SHA") or os.environ.get("CI_COMMIT_SHA", ""))[:8]
    message = (
        f"graph: update from MR !{mr_iid} ({sha})" if mr_iid
        else f"graph: incremental update ({sha})" if sha
        else "graph: incremental update"
    )

    restore(root, args.graph_branch)

    from graphify.watch import _rebuild_code
    if not _rebuild_code(root):
        print("[graphify] Rebuild failed — aborting push.")
        sys.exit(1)

    if not push(root, args.graph_branch, message):
        sys.exit(1)


if __name__ == "__main__":
    main()
'''

def _branch_rule(release_branch: str) -> str:
    """Return the GitLab CI `if:` condition for the given release branch spec.

    Supports three forms:
      - Exact name  "release"        →  $CI_COMMIT_BRANCH == "release"
      - Prefix      "release/"       →  $CI_COMMIT_BRANCH =~ /^release\\//
      - Glob        "release/*"      →  $CI_COMMIT_BRANCH =~ /^release\\//
    """
    import re as _re
    # Strip trailing wildcard so "release/*" and "release/" both become a prefix check
    prefix = release_branch.rstrip("*")
    if prefix.endswith("/") or "*" in release_branch:
        escaped = _re.escape(prefix)
        return f"$CI_COMMIT_BRANCH =~ /^{escaped}/ && $CI_PIPELINE_SOURCE == \"push\""
    return f'$CI_COMMIT_BRANCH == "{release_branch}" && $CI_PIPELINE_SOURCE == "push"'


_GITLAB_CI_TEMPLATE = """\
# ─── Graphify: Knowledge Graph Auto-Update ──────────────────────────────────
# Fires after every MR merged into '{release_branch_desc}'.
# Incrementally updates graphify-out/ on the '{graph_branch}' branch.
#
# Setup (one-time):
#   1. Run: graphify doc-graph setup
#      (creates the orphan '{graph_branch}' branch on origin)
#   2. Commit the generated .ci/graphify_update.py to your repo.
#   3. Add a GitLab CI/CD variable: GRAPHIFY_CI_TOKEN
#      (Project Access Token, Developer role, write_repository scope)
# ────────────────────────────────────────────────────────────────────────────

graphify-update-knowledge-graph:
  stage: .post
  rules:
    - if: {branch_rule}
      when: on_success
  image: python:3.11-slim
  variables:
    GIT_DEPTH: "0"       # full history needed for accurate diff
    GIT_STRATEGY: clone
  before_script:
    - pip install graphify --quiet
    - git config user.name "Graphify Bot"
    - git config user.email "graphify-bot@noreply"
    # Authenticate so the job can push to the {graph_branch} branch
    - >
      git remote set-url origin
      "https://graphify-bot:${{GRAPHIFY_CI_TOKEN}}@${{CI_SERVER_HOST}}/${{CI_PROJECT_PATH}}.git"
  script:
    - python3 .ci/graphify_update.py
        --graph-branch {graph_branch}
  artifacts:
    paths:
      - graphify-out/GRAPH_REPORT.md
    expire_in: 7 days
"""


def gen_gitlab_ci(
    release_branch: str = "release",
    graph_branch: str = "doc-graph",
    output: Path | None = None,
    script_dir: Path | None = None,
) -> str:
    """Generate the GitLab CI job config and the standalone update script.

    Writes two files into the user's own repository:
      - <script_dir>/graphify_update.py  — self-contained helper, no unpublished imports
      - <output>                          — .gitlab-ci.yml snippet (appended if file exists)

    Both files must be committed to the repo so the CI job can find them.
    Only `pip install graphify` (any published version) is needed at CI runtime.
    """
    branch_desc = release_branch.rstrip("*") + "*" if ("/" in release_branch or "*" in release_branch) else release_branch
    ci_content = _GITLAB_CI_TEMPLATE.format(
        release_branch_desc=branch_desc,
        branch_rule=_branch_rule(release_branch),
        graph_branch=graph_branch,
    )

    # Write the standalone update script
    resolved_script_dir = script_dir or Path(".ci")
    resolved_script_dir.mkdir(parents=True, exist_ok=True)
    script_path = resolved_script_dir / "graphify_update.py"
    script_path.write_text(_CI_SCRIPT, encoding="utf-8")
    script_path.chmod(0o755)
    print(f"[graphify doc-graph] Written standalone script → {script_path}")
    print(f"[graphify doc-graph] Commit {script_path} to your repository.")

    # Append the CI job to .gitlab-ci.yml (or just print it)
    if output:
        existing = output.read_text(encoding="utf-8") if output.exists() else ""
        marker = "graphify-update-knowledge-graph:"
        if marker in existing:
            print(f"[graphify doc-graph] '{marker}' already in {output} — skipping.")
        else:
            with open(output, "a", encoding="utf-8") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                f.write("\n" + ci_content)
            print(f"[graphify doc-graph] Appended CI job → {output}")
            print(f"[graphify doc-graph] Commit {output} to your repository.")

    return ci_content
