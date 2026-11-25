"""Read-only device repository inspection and update previews."""

import hashlib
import json
import os
import re
import subprocess


class GitRepository:
  def __init__(self, path, updater_state=lambda: None):
    self.path = path
    self.updater_state = updater_state

  def run(self, *args, check=True):
    return subprocess.run(
      ["git", "--no-optional-locks", "--no-pager", "-c", "core.fsmonitor=false", *args],
      cwd=self.path, capture_output=True, text=True, encoding="utf-8", errors="replace",
      timeout=3, check=check, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    ).stdout

  def status(self):
    status = {
      "branch": self.run("symbolic-ref", "--quiet", "--short", "HEAD", check=False).strip() or None,
      "head": self.run("rev-parse", "HEAD").strip(),
      "upstream": self.run("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", check=False).strip() or None,
      "updaterState": self.updater_state(),
      "mutationEnabled": False,
    }
    try:
      changes = self.run("status", "--porcelain=v1", "-z", "--untracked-files=normal", "--ignore-submodules=none")
      status["dirty"] = bool(changes)
    except (OSError, subprocess.SubprocessError):
      # Older clients treat dirty as a boolean. Keep a conservative value.
      status["dirty"] = True
      status["dirtyKnown"] = False
    return status

  def branches(self):
    refs = self.run("for-each-ref", "--count=256", "--format=%(refname:lstrip=3)%09%(objectname)%09%(symref)", "refs/remotes/origin/")
    return [{"name": name, "commit": commit} for name, commit, symbolic in (line.split("\t") for line in refs.splitlines())
            if not symbolic]

  def log(self, limit, offset):
    rows = self.run("log", f"--max-count={limit + 1}", f"--skip={offset}", "--format=%H%x00%P%x00%aI%x00%s", "HEAD", "--")
    entries = []
    for row in rows.split("\n"):
      if row:
        commit, parents, timestamp, subject = row.split("\0", 3)
        entries.append({"commit": commit, "parents": parents.split(), "authorTime": timestamp, "subject": subject[:256]})
    return {"entries": entries[:limit], "nextCursor": offset + limit if len(entries) > limit else None}

  def plan(self, operation, branch, commit):
    branch_heads = {row["name"]: row["commit"] for row in self.branches()}
    if not isinstance(branch, str) or branch not in branch_heads:
      raise ValueError("branch must be an observed origin branch")
    branch_head = branch_heads[branch]
    if operation == "switch" and commit is None:
      commit = branch_head
    elif operation == "reset" and isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit):
      if self.run("cat-file", "-t", commit, check=False).strip() != "commit":
        raise ValueError("invalid reset commit")
      if self.run("merge-base", commit, branch_head, check=False).strip() != commit:
        raise ValueError("commit is outside the selected origin history")
    else:
      raise ValueError("invalid switch/reset request")

    current = self.status()
    plan = {"operation": operation, "branch": branch, "commit": commit, "branchHead": branch_head,
            "beforeHead": current["head"], "beforeBranch": current["branch"], "dirty": current["dirty"],
            "requiresRestart": True, "mutationEnabled": False}
    if not current.get("dirtyKnown", True):
      plan["dirtyKnown"] = False
    changed = self.run("diff", "--name-only", "-z", "--no-ext-diff", current["head"], commit, "--")
    plan["changedFileCount"] = changed.count("\0")
    plan["planDigest"] = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    return plan
