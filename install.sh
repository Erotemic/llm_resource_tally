#!/bin/sh
# llm_resource_tally installer — the `curl | sh` bootstrap.
#
# Repository policy is read from .llm_resource_tally/settings.json. Environment variables are
# explicit overrides and are persisted back into that file by the installed tool.
set -eu

say()  { printf 'llm_resource_tally: %s\n' "$*" >&2; }
die()  { say "error: $*"; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

have git     || die "git is required"
have python3 || die "python3 is required"
have tar     || die "tar is required"
have curl || have wget || die "curl or wget is required"

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
POLICY_FILE="$ROOT/.llm_resource_tally/settings.json"

# Emit shell-quoted, validated policy values. This lets a clone with ignored generated state
# reconstruct the same storage mode and artifact format from its one committed settings file.
eval "$(python3 - "$POLICY_FILE" <<'PY'
import json
import os
import shlex
import sys

path = sys.argv[1]
defaults = {
    "storage": "committed",
    "tool_format": "zipapp",
    "tool_path": ".llm_resource_tally/tool.pyz",
    "modeling": False,
}
try:
    with open(path, encoding="utf-8") as file:
        data = json.load(file)
except (OSError, ValueError):
    data = {}
raw = data.get("installation") if isinstance(data, dict) else {}
raw = raw if isinstance(raw, dict) else {}
fmt = raw.get("tool_format")
if fmt not in {"zipapp", "source"}:
    fmt = defaults["tool_format"]
storage = raw.get("storage")
if storage not in {"committed", "ignored", "notes"}:
    storage = defaults["storage"]
modeling = raw.get("modeling")
if not isinstance(modeling, bool):
    modeling = defaults["modeling"]
tool = raw.get("tool_path")
if not isinstance(tool, str):
    tool = ""
tool = os.path.normpath(tool.strip()) if tool.strip() else ""
default_tool = ".llm_resource_tally/tool.pyz" if fmt == "zipapp" else ".llm_resource_tally/tool"
invalid = (not tool or os.path.isabs(tool) or tool in {".", "..", ".llm_resource_tally"}
           or tool.startswith(".." + os.sep)
           or (fmt == "zipapp" and not tool.endswith(".pyz"))
           or (fmt == "source" and tool.endswith(".pyz")))
if invalid:
    tool = default_tool
values = {
    "POLICY_STORAGE": storage,
    "POLICY_TOOL_FORMAT": fmt,
    "POLICY_TOOL_PATH": tool,
    "POLICY_MODELING": "1" if modeling else "0",
}
for key, value in values.items():
    print(f"{key}={shlex.quote(value)}")
PY
)"

RT_REPO="${RT_REPO:-Erotemic/llm_resource_tally}"
RT_REF="${RT_REF:-main}"
format_explicit=0
[ "${RT_TOOL_FORMAT+x}" = x ] && format_explicit=1
RT_TOOL_FORMAT="${RT_TOOL_FORMAT:-$POLICY_TOOL_FORMAT}"
RT_STORAGE="${RT_STORAGE:-$POLICY_STORAGE}"
RT_MODELING="${RT_MODELING:-$POLICY_MODELING}"
case "$RT_TOOL_FORMAT" in zipapp|source) ;; *) die "RT_TOOL_FORMAT must be zipapp or source" ;; esac
case "$RT_STORAGE" in committed|ignored|notes) ;; *) die "RT_STORAGE must be committed, ignored, or notes" ;; esac
case "$RT_MODELING" in 0|1) ;; *) die "RT_MODELING must be 0 or 1" ;; esac

if [ -z "${RT_DIR+x}" ]; then
  if [ "$format_explicit" = 0 ] || [ "$RT_TOOL_FORMAT" = "$POLICY_TOOL_FORMAT" ]; then
    RT_DIR="$POLICY_TOOL_PATH"
  elif [ "$RT_TOOL_FORMAT" = "zipapp" ]; then
    RT_DIR=".llm_resource_tally/tool.pyz"
  else
    RT_DIR=".llm_resource_tally/tool"
  fi
fi
case "$RT_DIR" in
  /*|.|..|../*|.llm_resource_tally) die "RT_DIR must be a dedicated repository-relative path" ;;
esac
case "$RT_TOOL_FORMAT:$RT_DIR" in
  zipapp:*.pyz) ;;
  zipapp:*) die "zipapp RT_DIR must end in .pyz" ;;
  source:*.pyz) die "source RT_DIR must be a directory" ;;
esac

DEST="$ROOT/$RT_DIR"
OLD_DEST="$ROOT/$POLICY_TOOL_PATH"
say "installing $RT_REPO@$RT_REF as $RT_TOOL_FORMAT into ${DEST}"
say "policy: storage=$RT_STORAGE modeling=$RT_MODELING"

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
  rm -rf "$DEST"
  mkdir -p "$DEST"
  if [ "$RT_MODELING" = "1" ]; then
    ( cd "$tmp/llm_resource_tally" && tar -cf - --exclude='__pycache__' --exclude='*.pyc' . ) | \
      ( cd "$DEST" && tar -xf - )
  else
    ( cd "$tmp/llm_resource_tally" && tar -cf - --exclude='__pycache__' --exclude='*.pyc' \
        --exclude='./modeling' . ) | ( cd "$DEST" && tar -xf - )
  fi
  [ -f "$tmp/VERSION" ] && cp "$tmp/VERSION" "$DEST/VERSION"
  [ -f "$tmp/README.md" ] && cp "$tmp/README.md" "$DEST/README.md"
fi

model_flag="--no-modeling"
[ "$RT_MODELING" = "1" ] && model_flag="--modeling"
python3 -B "$DEST" install --dir "$RT_DIR" --tool-format "$RT_TOOL_FORMAT" \
  --storage "$RT_STORAGE" "$model_flag"

# A network update executes the newly built artifact, so it can safely clean the old managed
# representation after hooks/settings have migrated to the new path.
if [ "$POLICY_TOOL_PATH" != "$RT_DIR" ] && [ -e "$OLD_DEST" ]; then
  rm -rf "$OLD_DEST"
  say "removed previous tool artifact $POLICY_TOOL_PATH"
fi

if [ "$RT_MODELING" = "1" ]; then
  say "included the modeling subpackage (estimate: energy/carbon/USD)."
else
  say "minimal install (measurement only)."
fi
case "$RT_STORAGE" in
  committed) say "done. Commit settings, tool, AGENTS.md, and intended accounting state." ;;
  ignored)   say "done. Commit .llm_resource_tally/settings.json; generated state stays ignored." ;;
  notes)     say "done. Commit settings/tool/AGENTS.md; sync refs/notes/llm-resource-tally explicitly." ;;
esac
