#!/bin/sh
# llm_resource_tally installer — the `curl | sh` bootstrap.
#
#   curl -fsSL https://raw.githubusercontent.com/Erotemic/llm_resource_tally/main/install.sh | sh
#
# New installs default to one deterministic zipapp at `.llm_resource_tally/tool.pyz`.
# Use RT_TOOL_FORMAT=source for the historical source-tree representation.  The ledger is never
# part of the tool artifact and is never replaced by this bootstrap.
#
# Overrides:
#   RT_REPO=owner/name RT_REF=v1.2.3 RT_TOOL_FORMAT=zipapp RT_DIR=tools/rt.pyz sh install.sh
#   RT_MODELING=1 includes the optional energy/carbon estimator and bundled assumption packs.
set -eu

say()  { printf 'llm_resource_tally: %s\n' "$*" >&2; }
die()  { say "error: $*"; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

RT_REPO="${RT_REPO:-Erotemic/llm_resource_tally}"
RT_REF="${RT_REF:-main}"
RT_TOOL_FORMAT="${RT_TOOL_FORMAT:-zipapp}"
RT_MODELING="${RT_MODELING:-0}"
RT_STORAGE="${RT_STORAGE:-committed}"
case "$RT_TOOL_FORMAT" in zipapp|source) ;; *) die "RT_TOOL_FORMAT must be zipapp or source" ;; esac
case "$RT_STORAGE" in committed|ignored|notes) ;; *) die "RT_STORAGE must be committed, ignored, or notes" ;; esac
if [ -z "${RT_DIR+x}" ]; then
  if [ "$RT_TOOL_FORMAT" = "zipapp" ]; then
    RT_DIR=".llm_resource_tally/tool.pyz"
  else
    RT_DIR=".llm_resource_tally/tool"
  fi
fi

have git     || die "git is required"
have python3 || die "python3 is required"
have tar     || die "tar is required"
have curl || have wget || die "curl or wget is required"

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
DEST="$ROOT/$RT_DIR"
say "installing $RT_REPO@$RT_REF as $RT_TOOL_FORMAT into ${DEST}"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

url="https://github.com/$RT_REPO/archive/$RT_REF.tar.gz"
dl_fail="could not fetch $RT_REPO@$RT_REF — is the ref right? (tags/branches only; try RT_REF=main)"
if have curl; then
  curl -fsSL "$url" | tar -xz -C "$tmp" --strip-components=1 || die "$dl_fail"
else
  wget -qO- "$url" | tar -xz -C "$tmp" --strip-components=1 || die "$dl_fail"
fi
[ -d "$tmp/llm_resource_tally" ] || die "unexpected archive layout (no llm_resource_tally package)"

if [ "$RT_TOOL_FORMAT" = "zipapp" ]; then
  mkdir -p "$(dirname "$DEST")"
  if [ "$RT_MODELING" = "1" ]; then
    python3 -B "$tmp" build-zipapp --output "$DEST" --modeling
  else
    python3 -B "$tmp" build-zipapp --output "$DEST"
  fi
else
  mkdir -p "$DEST"
  ( cd "$tmp/llm_resource_tally" && tar -cf - --exclude='__pycache__' --exclude='*.pyc' . ) | \
    ( cd "$DEST" && tar -xf - )
  if [ "$RT_MODELING" != "1" ]; then
    rm -rf "$DEST/modeling"
  fi
  [ -f "$tmp/VERSION" ] && cp "$tmp/VERSION" "$DEST/VERSION"
  [ -f "$tmp/README.md" ] && cp "$tmp/README.md" "$DEST/README.md"
fi

if [ "$RT_MODELING" = "1" ]; then
  python3 -B "$DEST" install --dir "$RT_DIR" --tool-format "$RT_TOOL_FORMAT" \
    --storage "$RT_STORAGE" --modeling
else
  python3 -B "$DEST" install --dir "$RT_DIR" --tool-format "$RT_TOOL_FORMAT" \
    --storage "$RT_STORAGE"
fi

if [ "$RT_MODELING" = "1" ]; then
  say "included the modeling subpackage (estimate: energy/carbon/USD)."
else
  say "minimal install (measurement only). add modeling with: python3 $RT_DIR install --modeling"
fi
case "$RT_STORAGE" in
  committed) say "done. Review and commit $RT_DIR + AGENTS.md to share it." ;;
  ignored)   say "done. Accounting is local/gitignored; do not stage generated tally files." ;;
  notes)     say "done. Commit the tool + AGENTS.md; sync refs/notes/llm-resource-tally explicitly." ;;
esac
