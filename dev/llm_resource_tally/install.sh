#!/bin/sh
# llm_resource_tally installer — the `curl | sh` bootstrap.
#
#   curl -fsSL https://raw.githubusercontent.com/Erotemic/llm_resource_tally/main/install.sh | sh
#
# What it does (the ONLY network-dependent step):
#   1. resolve the target repo root (or cwd),
#   2. fetch the pinned version's tarball and vendor the CODE into dev/llm_resource_tally/
#      (never data/ — that's the committed ledger),
#   3. hand off to the offline installer:  llm_resource_tally.py install
#      (wires the git post-commit hook + a managed block in AGENTS.md).
#
# The vendored copy is the source of truth: once it lands and is committed, everything
# works with zero network. This script is also shipped INSIDE the folder, so re-running
# `dev/llm_resource_tally/install.sh` re-fetches + re-installs (an update). To re-wire a repo
# that already has the folder, no network is needed — just run:
#   python3 dev/llm_resource_tally/llm_resource_tally.py install
#
# Override anything via env: RT_REPO=owner/name RT_REF=v1.2.3 RT_DIR=tools/rt sh install.sh
set -eu

RT_REPO="${RT_REPO:-Erotemic/llm_resource_tally}"  # canonical source (owner/name)
RT_REF="${RT_REF:-main}"                           # tag/branch/sha; pin with RT_REF=v1.2.3
RT_DIR="${RT_DIR:-dev/llm_resource_tally}"         # where to vendor, relative to repo root

say()  { printf 'llm_resource_tally: %s\n' "$*" >&2; }
die()  { say "error: $*"; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

have git     || die "git is required"
have python3 || die "python3 is required"
have tar     || die "tar is required"
have curl || have wget || die "curl or wget is required"

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
DEST="$ROOT/$RT_DIR"
say "installing $RT_REPO@$RT_REF into ${DEST}"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

url="https://github.com/$RT_REPO/archive/$RT_REF.tar.gz"
dl_fail="could not fetch $RT_REPO@$RT_REF — is the ref right? (tags/branches only; try RT_REF=main)"
if have curl; then
  curl -fsSL "$url" | tar -xz -C "$tmp" --strip-components=1 || die "$dl_fail"
else
  wget -qO- "$url" | tar -xz -C "$tmp" --strip-components=1 || die "$dl_fail"
fi
[ -f "$tmp/llm_resource_tally.py" ] || die "unexpected archive layout (no llm_resource_tally.py at root of $RT_REPO@$RT_REF)"

# Vendor CODE only. data/ is intentionally excluded so an update never clobbers the
# host repo's ledger.
mkdir -p "$DEST" "$DEST/hooks"
for f in llm_resource_tally.py VERSION README.md install.sh; do
  [ -f "$tmp/$f" ] && cp "$tmp/$f" "$DEST/$f"
done
[ -d "$tmp/hooks" ] && cp -R "$tmp/hooks/." "$DEST/hooks/"
chmod +x "$DEST/llm_resource_tally.py" "$DEST/install.sh" 2>/dev/null || true
[ -f "$DEST/hooks/post-commit" ] && chmod +x "$DEST/hooks/post-commit"

# Offline from here on: wire hooks + AGENTS.md, stamp the version.
python3 "$DEST/llm_resource_tally.py" install --dir "$RT_DIR"

say "done. Review & commit dev/llm_resource_tally + AGENTS.md to share it."
