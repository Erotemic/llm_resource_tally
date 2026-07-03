#!/bin/sh
# llm_resource_tally installer — the `curl | sh` bootstrap.
#
#   curl -fsSL https://raw.githubusercontent.com/Erotemic/llm_resource_tally/main/install.sh | sh
#
# What it does (the ONLY network-dependent step):
#   1. resolve the target repo root (or cwd),
#   2. fetch the pinned version's tarball and vendor the PACKAGE into dev/llm_resource_tally/
#      (code only — never the ledger),
#   3. hand off to the offline installer:  python3 dev/llm_resource_tally install
#      (wires the git post-commit hook + a managed block in AGENTS.md).
#
# The vendored copy is the source of truth: once it lands and is committed, everything
# works with zero network. To re-wire a repo that already has the package, no network is
# needed — just run:  python3 dev/llm_resource_tally install
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
[ -d "$tmp/llm_resource_tally" ] || die "unexpected archive layout (no llm_resource_tally/ package at root of $RT_REPO@$RT_REF)"

# Vendor the PACKAGE (code only; no __pycache__). The ledger lives in .llm_resource_tally/
# at the repo root and is never touched here. Stamp VERSION so the vendored copy knows its
# version offline.
mkdir -p "$DEST"
( cd "$tmp/llm_resource_tally" && tar -cf - --exclude='__pycache__' --exclude='*.pyc' . ) | ( cd "$DEST" && tar -xf - )
[ -f "$tmp/VERSION" ] && cp "$tmp/VERSION" "$DEST/VERSION"
[ -f "$tmp/README.md" ] && cp "$tmp/README.md" "$DEST/README.md"

# Offline from here on: run the vendored package to wire hooks + AGENTS.md.
python3 "$DEST" install --dir "$RT_DIR"

say "done. Review & commit $RT_DIR + AGENTS.md to share it."
