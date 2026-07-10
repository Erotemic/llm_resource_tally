#!/bin/sh
# llm_resource_tally installer — the `curl | sh` bootstrap.
#
#   curl -fsSL https://raw.githubusercontent.com/Erotemic/llm_resource_tally/main/install.sh | sh
#
# What it does (the ONLY network-dependent step):
#   1. resolve the target repo root (or cwd),
#   2. fetch the pinned version's tarball and vendor the MINIMAL measurement package into
#      .llm_resource_tally/tool/ (code only — never the ledger). The optional `modeling/`
#      subpackage (estimate: energy/carbon/USD) is left out so the vendored footprint stays
#      tiny; add it any time with `python3 .llm_resource_tally/tool install --modeling`, or
#      pass RT_MODELING=1 here to include it now.
#   3. hand off to the offline installer:  python3 .llm_resource_tally/tool install
#      (wires the git post-commit hook + a managed block in AGENTS.md).
#
# The vendored copy is the source of truth: once it lands and is committed, everything
# works with zero network. To re-wire a repo that already has the package, no network is
# needed — just run:  python3 .llm_resource_tally/tool install
#
# Override anything via env: RT_REPO=owner/name RT_REF=v1.2.3 RT_DIR=tools/rt sh install.sh
set -eu

RT_REPO="${RT_REPO:-Erotemic/llm_resource_tally}"  # canonical source (owner/name)
RT_REF="${RT_REF:-main}"                           # tag/branch/sha; pin with RT_REF=v1.2.3
RT_DIR="${RT_DIR:-.llm_resource_tally/tool}"       # where to vendor, relative to repo root
RT_MODELING="${RT_MODELING:-0}"                    # 1 = also vendor the optional modeling subpackage
RT_STORAGE="${RT_STORAGE:-committed}"                # committed | ignored | notes
case "$RT_STORAGE" in committed|ignored|notes) ;; *) die "RT_STORAGE must be committed, ignored, or notes" ;; esac

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
# Keep the bare install minimal: drop the optional modeling subpackage unless opted in. The
# core measurement tool needs none of it; `install --modeling` (or RT_MODELING=1) adds it back.
if [ "$RT_MODELING" != "1" ]; then
  rm -rf "$DEST/modeling"
fi
[ -f "$tmp/VERSION" ] && cp "$tmp/VERSION" "$DEST/VERSION"
[ -f "$tmp/README.md" ] && cp "$tmp/README.md" "$DEST/README.md"

# Offline from here on: run the vendored package to wire hooks + AGENTS.md.
python3 -B "$DEST" install --dir "$RT_DIR" --storage "$RT_STORAGE"

if [ "$RT_MODELING" = "1" ]; then
  say "included the modeling subpackage (estimate: energy/carbon/USD)."
else
  say "minimal install (measurement only). add modeling with: python3 $RT_DIR install --modeling"
fi
case "$RT_STORAGE" in
  committed) say "done. Review & commit $RT_DIR + AGENTS.md to share it." ;;
  ignored)   say "done. Accounting is local/gitignored; do not stage generated tally files." ;;
  notes)     say "done. Commit the tool + AGENTS.md; sync refs/notes/llm-resource-tally explicitly." ;;
esac
