#!/usr/bin/env bash
# KovaPilot bootstrap v1. Put this executable at the root of the installer branch.
# This file deliberately contains no vehicle-control or settings-reset commands.
set -Eeuo pipefail
umask 022

readonly REPO='https://github.com/janpoo6427/kovapilot.git'
readonly BRANCH='release'
readonly ROOT='/data'
readonly LIVE="$ROOT/openpilot"
readonly STATE="$ROOT/kovapilot-bootstrap"
readonly STAGE="$STATE/staging"
readonly BACKUP="$STATE/bootstrap-backup"
export GIT_TERMINAL_PROMPT=0

fail() { printf '\n[KovaPilot] ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '[KovaPilot] %s\n' "$*"; }
on_exit() {
  local rc=$?
  if (( rc != 0 )); then
    printf '\n[KovaPilot] Installation stopped (exit %s). No automatic reboot.\n' "$rc" >&2
    printf 'Keep parked. Check the error and network/free space before retrying. Log: %s/install.log\n' "$STATE" >&2
  fi
}
trap on_exit EXIT

main() {
  [[ -f /AGNOS ]] || fail 'AGNOS device required.'
  [[ "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)" == "$LIVE" ]] || fail 'Run only as /data/openpilot/launch_openpilot.sh.'
  for tool in git python3 flock timeout tee sync df awk grep mv bash; do
    command -v "$tool" >/dev/null || fail "Required command missing: $tool"
  done
  [[ ! -L "$STATE" ]] || fail 'State directory must not be a symlink.'
  mkdir -p "$STATE"
  exec 9>"$STATE/install.lock"
  flock -n 9 || fail 'Another bootstrap installation is running.'
  exec 7>&1 8>&2
  exec > >(tee -a "$STATE/install.log") 2>&1
  log "Bootstrap v1: $REPO ($BRANCH)"
  log 'Initial download can take a while. Progress is in this log; no custom graphical progress UI is included.'

  [[ ! -L "$LIVE" && ! -L "$STAGE" && ! -L "$BACKUP" ]] || fail 'Unexpected symlink; refusing replacement.'
  [[ ! -e "$BACKUP" ]] || fail 'A previous bootstrap backup exists; inspect it before reinstalling.'
  # A fixed working directory is resumable. Never delete pre-existing files.
  if [[ -e "$STAGE" ]]; then
    [[ -d "$STAGE/.git" ]] || fail 'Staging exists but is not a checkout; inspect it manually.'
    [[ "$(git -C "$STAGE" remote get-url origin)" == "$REPO" ]] || fail 'Unexpected staging repository.'
    [[ "$(git -C "$STAGE" branch --show-current)" == "$BRANCH" ]] || fail 'Unexpected staging branch.'
  else
    # 12 GiB is a conservative preflight, not a guaranteed model/build requirement.
    local available
    available=$(df -Pk "$ROOT" | awk 'END {print $4}')
    (( available >= 12 * 1024 * 1024 )) || fail 'At least 12 GiB free space is required before starting.'
    log 'Downloading the main repository (30-minute timeout).'
    timeout --kill-after=30 1800 git -c filter.lfs.process= -c filter.lfs.smudge=cat -c filter.lfs.required=false \
      clone --depth 1 --single-branch --branch "$BRANCH" "$REPO" "$STAGE"
  fi

  cd "$STAGE"
  # Do not fetch a newer commit on retry: finish the initially selected revision.
  log "Selected commit: $(git rev-parse HEAD)"
  [[ "$(git rev-parse HEAD)" == "$(git rev-parse "origin/$BRANCH")" ]] || fail 'Staging HEAD does not match its downloaded branch.'
  log 'Downloading submodules (30-minute timeout).'
  timeout --kill-after=30 1800 git submodule update --init --recursive
  [[ -z "$(git submodule status --recursive | grep -E '^[+U-]' || true)" ]] || fail 'Submodule revision mismatch.'
  [[ -x ./tools/lfs.sh && -f ./lfs.py ]] || fail 'Expected repository LFS client missing.'
  log 'Downloading model/LFS files (60-minute timeout).'
  timeout --kill-after=30 3600 ./tools/lfs.sh pull

  log 'Checking required files, tracked LFS SHA-256/size, and checkout cleanliness.'
  python3 - <<'PY'
import hashlib
import pathlib
import subprocess

root = pathlib.Path.cwd()
for name in ('launch_openpilot.sh', 'launch_chffrplus.sh', 'launch_env.sh',
             'openpilot/system/manager/manager.py'):
    p = root / name
    if not p.is_file() or p.is_symlink():
        raise SystemExit(f'Missing or unexpected launch file: {name}')
if not (root / 'openpilot/kovapilot').is_dir():
    raise SystemExit('KovaPilot package missing')

def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args])

# Verify every tracked LFS object in the main repo and initialized submodules.
repos = [root]
repos += [root / pathlib.Path(p.decode()) for p in git(root, 'submodule', 'foreach', '--quiet', '--recursive', 'printf "%s\\n" "$displaypath"').splitlines()]
count = 0
for repo in repos:
    result = subprocess.run(['git', '-C', str(repo), 'grep', '--cached', '-l', '-z',
                             '^version https://git-lfs.github.com/spec/v1$'], capture_output=True)
    if result.returncode not in (0, 1):
        raise SystemExit(f'LFS index scan failed: {repo}')
    for raw in filter(None, result.stdout.split(b'\0')):
        name = raw.decode()
        blob = git(repo, 'show', ':' + name)
        if not blob.startswith(b'version https://git-lfs.github.com/spec/v1\n'):
            continue
        fields = dict(line.split(b' ', 1) for line in blob.splitlines())
        oid = fields[b'oid'].decode()
        if not oid.startswith('sha256:'):
            raise SystemExit(f'Unsupported LFS object: {name}')
        path = repo / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size != int(fields[b'size']):
            raise SystemExit(f'LFS file missing/size mismatch: {path}')
        h = hashlib.sha256()
        with path.open('rb') as f:
            for block in iter(lambda: f.read(1024 * 1024), b''):
                h.update(block)
        if h.hexdigest() != oid[len('sha256:'):]:
            raise SystemExit(f'LFS SHA-256 mismatch: {path}')
        count += 1
    if git(repo, 'status', '--porcelain', '--untracked-files=no').strip():
        raise SystemExit(f'Tracked modifications in staging: {repo}')
print(f'Checked {count} LFS files across {len(repos)} repositories')
PY
  [[ -x ./launch_openpilot.sh && -x ./launch_chffrplus.sh ]] || fail 'Main launch scripts must be executable.'
  bash -n ./launch_openpilot.sh
  bash -n ./launch_chffrplus.sh
  git branch --set-upstream-to="origin/$BRANCH" "$BRANCH"
  git rev-parse HEAD > "$STATE/prepared-commit.txt"
  sync

  log 'Atomically exchanging bootstrap and prepared checkout.'
  cd "$ROOT"
  # Linux renameat2(RENAME_EXCHANGE) avoids the power-loss gap of two mv calls.
  # No unsafe fallback: unsupported kernels/filesystems stop before replacement.
  python3 - "$LIVE" "$STAGE" <<'PY'
import ctypes
import os
import sys

live, stage = sys.argv[1:]
libc = ctypes.CDLL(None, use_errno=True)
renameat2 = libc.renameat2
renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
renameat2.restype = ctypes.c_int
if renameat2(-100, os.fsencode(live), -100, os.fsencode(stage), 2) != 0:
    err = ctypes.get_errno()
    raise OSError(err, os.strerror(err))
try:
    for parent in {os.path.dirname(live), os.path.dirname(stage)}:
        fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
except OSError:
    print('ERROR: exchange ALREADY succeeded, but sync failed. Main checkout is now '
          '/data/openpilot; do not assume rollback.', file=sys.stderr)
    raise
PY
  # At this point LIVE is the real application, STAGE holds the tiny bootstrap.
  if ! mv -T "$STAGE" "$BACKUP"; then
    log "Backup rename failed; bootstrap is retained at $STAGE. Main checkout is already installed."
  fi
  git -C "$LIVE" rev-parse HEAD > "$STATE/installed-commit.txt"
  sync
  log 'Repository handoff complete. Starting KovaPilot; build/AGNOS checks belong to its launcher.'
  cd "$LIVE"
  flock -u 9
  exec 9>&-
  trap - EXIT
  # Restore original output so application logs do not grow the bootstrap log.
  exec 1>&7 2>&8 7>&- 8>&-
  exec ./launch_openpilot.sh
}

# Parsing the complete function before executing it permits safe self-replacement.
main "$@"
