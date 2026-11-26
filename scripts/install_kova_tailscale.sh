#!/usr/bin/env bash
set -euo pipefail

if [[ $# != 2 || ! ${2:-} =~ ^[0-9a-f]{64}$ || $(uname -m) != "aarch64" ]]; then
  echo "Usage on a parked ARM64 comma: $0 tailscale_VERSION_arm64.tgz EXPECTED_SHA256" >&2
  exit 2
fi
umask 077
archive=$(realpath -- "$1")
name=$(basename -- "$archive" .tgz)
if [[ ! $name =~ ^tailscale_[0-9]+\.[0-9]+\.[0-9]+_arm64$ ]]; then
  echo "Use the official Tailscale ARM64 static archive." >&2
  exit 2
fi
actual=$(sha256sum < "$archive")
if [[ ${actual%% *} != "$2" ]]; then
  echo "Tailscale archive checksum mismatch." >&2
  exit 1
fi

directory=/persist/kova
target=$directory/tailscale
if [[ -e $target || -L $target ]]; then
  echo "The Tailscale installation already exists." >&2
  exit 1
fi
mkdir -p -- "$directory"
temporary=$(mktemp -d "$directory/.tailscale-install.XXXXXX")
trap 'rm -f -- "$temporary/tailscale" "$temporary/tailscaled"; rmdir -- "$temporary" 2>/dev/null || true' EXIT
for binary in tailscale tailscaled; do
  tar -xOzf "$archive" "$name/$binary" > "$temporary/$binary"
  chmod 0700 "$temporary/$binary"
done
mv -T --no-clobber -- "$temporary" "$target"
test ! -d "$temporary"
echo "Tailscale is ready for owner registration in KovaLink."
