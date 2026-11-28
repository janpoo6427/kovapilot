"""Refresh remote refs and preview changes to the device checkout."""

from functools import partial
import hashlib
import json
import os
import re
import subprocess
from time import monotonic

from openpilot.kovapilot.config.git_update_request import ACTIVE_PHASES, PLAN_FIELDS, describe_request, is_pull


# Leave headroom before the existing Link client's eight-second timeout.
PREVIEW_TIMEOUT = 7.0
FETCH_TIMEOUT = 4.5


class GitFetchError(OSError):
  pass


class GitPlanChanged(ValueError):
  pass


class GitApplyBlocked(ValueError):
  pass


class GitMutationDisabled(GitApplyBlocked):
  """This change needs Park or offroad (or this client is not authorized)."""


class GitRepository:
  def __init__(self, path, updater_state=lambda: None, *, maintenance_state=lambda: None,
               apply_allowed=lambda: False, pull_allowed=None, enqueue=None):
    self.path = path
    self.updater_state = updater_state
    self.maintenance_state = maintenance_state
    self.apply_allowed = apply_allowed
    # A pull (newest commit of the installed branch) uses this rule instead of
    # apply_allowed when given: the gateway passes the updater checks without
    # the vehicle check (janpoo6427 2026-11-28). None keeps apply_allowed.
    self.pull_allowed = pull_allowed
    self.enqueue = enqueue

  def pending(self):
    state = describe_request(self.maintenance_state())
    return state is not None and state["phase"] in ACTIVE_PHASES

  def can_apply(self, authorized, pull=False):
    allowed = self.pull_allowed if pull and self.pull_allowed is not None else self.apply_allowed
    return authorized and self.enqueue is not None and not self.pending() and allowed()

  def run(self, *args, check=True, timeout=3, deadline=None):
    if deadline is not None:
      timeout = min(timeout, deadline - monotonic())
      if timeout <= 0:
        raise TimeoutError("Git preview reached its deadline")
    return subprocess.run(
      ["git", "--no-optional-locks", "--no-pager", "-c", "core.fsmonitor=false", *args],
      cwd=self.path, capture_output=True, text=True, encoding="utf-8", errors="replace",
      timeout=timeout, check=check, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    ).stdout

  def _head(self, run):
    return {
      "branch": run("symbolic-ref", "--quiet", "--short", "HEAD", check=False).strip() or None,
      "head": run("rev-parse", "HEAD").strip(),
    }

  def _worktree(self, run):
    try:
      changes = run("status", "--porcelain=v1", "-z", "--untracked-files=normal", "--ignore-submodules=none")
      return {"dirty": bool(changes)}
    except (OSError, subprocess.SubprocessError):
      # Older clients treat dirty as a boolean. Keep a conservative value.
      return {"dirty": True, "dirtyKnown": False}

  def status(self, authorized=False):
    pending = describe_request(self.maintenance_state())
    enabled = self.can_apply(authorized)
    return {
      **self._head(self.run),
      "upstream": self.run("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", check=False).strip() or None,
      "updaterState": self.updater_state(),
      "mutationEnabled": enabled,
      "mutationBlock": None if enabled or self.enqueue is None else ("parkedOrUpdaterBusy" if authorized else "authorization"),
      "pullEnabled": self.can_apply(authorized, pull=True),
      "maintenance": pending,
      **({"dirty": True, "dirtyKnown": False} if self.pending() else self._worktree(self.run)),
    }

  def _fetch_origin(self, run):
    try:
      run("fetch", "--atomic", "--prune", "--no-tags", "--no-recurse-submodules", "--no-auto-maintenance",
          "--no-write-fetch-head", "--refmap=", "origin", "+refs/heads/*:refs/remotes/origin/*", timeout=FETCH_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
      raise GitFetchError("could not refresh origin") from exc

  def branches(self):
    return self._fresh_branches(partial(self.run, deadline=monotonic() + PREVIEW_TIMEOUT))

  def _fresh_branches(self, run):
    if not self.pending():
      self._fetch_origin(run)
    refs = run("for-each-ref", "--count=256", "--format=%(refname:lstrip=3)%09%(objectname)%09%(symref)", "refs/remotes/origin/")
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

  def plan(self, operation, branch, commit, authorized=False):
    switch = operation == "switch" and commit is None
    reset = operation == "reset" and isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit)
    if not isinstance(branch, str) or not branch or not (switch or reset):
      raise ValueError("invalid switch/reset request")

    run = partial(self.run, deadline=monotonic() + PREVIEW_TIMEOUT)
    branch_heads = {row["name"]: row["commit"] for row in self._fresh_branches(run)}
    if branch not in branch_heads:
      raise ValueError("branch must be an observed origin branch")
    branch_head = branch_heads[branch]
    if switch:
      commit = branch_head
    else:
      if run("cat-file", "-t", commit, check=False).strip() != "commit":
        raise ValueError("invalid reset commit")
      if run("merge-base", commit, branch_head, check=False).strip() != commit:
        raise ValueError("commit is outside the selected origin history")

    current = self._head(run)
    pull = is_pull({"operation": operation, "branch": branch, "beforeBranch": current["branch"]})
    changed = run("diff", "--name-only", "-z", "--no-ext-diff", current["head"], commit, "--")
    plan = {"operation": operation, "branch": branch, "commit": commit, "branchHead": branch_head,
            "beforeHead": current["head"], "beforeBranch": current["branch"],
            "requiresRestart": True, "mutationEnabled": self.can_apply(authorized, pull=pull),
            **({"dirty": True, "dirtyKnown": False} if self.pending() else self._worktree(run))}
    plan["changedFileCount"] = changed.count("\0")
    plan["planDigest"] = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    return plan

  def apply(self, operation, branch, commit, digest):
    if not self.can_apply(True, pull=True):
      raise GitApplyBlocked("maintenance needs an idle updater")
    plan = self.plan(operation, branch, commit, authorized=True)
    pull = is_pull(plan)
    if not self.can_apply(True, pull=pull):
      raise GitMutationDisabled("maintenance needs Park or offroad and an idle updater")
    if plan["dirty"] or not plan.get("dirtyKnown", True):
      raise GitApplyBlocked("working tree must be known and clean")
    if plan["planDigest"] != digest:
      raise GitPlanChanged("Git preview changed")
    if operation == "reset" and branch != plan["beforeBranch"]:
      raise GitApplyBlocked("reset must stay on the installed branch")
    if not self.can_apply(True, pull=pull):
      raise GitApplyBlocked("maintenance needs Park or offroad and an idle updater")
    job = {**{key: plan[key] for key in PLAN_FIELDS}, "phase": "queued"}
    self.enqueue(job)
    return {"maintenance": describe_request(job)}
