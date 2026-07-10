#!/bin/sh
# llm_resource_tally installer — the curl | sh bootstrap.
#
# Repository policy is read from .llm_resource_tally/settings.json. Environment variables are
# explicit overrides and are persisted back into that file by the installed tool. The executable
# path is invariant: .llm_resource_tally/tool is either a source directory or a Python zipapp.
set -eu

usage() {
  printf '%s\n' \
    'Usage: install.sh [--help]' \
    '' \
    'Install llm_resource_tally into the current Git repository.' \
    '' \
    'The repository-owned policy in .llm_resource_tally/settings.json supplies the' \
    'normal defaults. These environment variables may override it for this run and' \
    'are persisted by the installed tool:' \
    '' \
    '  RT_REPO         source repository (default: Erotemic/llm_resource_tally)' \
    '  RT_REF          source branch or tag (default: main)' \
    '  RT_TOOL_FORMAT  zipapp or source' \
    '  RT_STORAGE      committed, ignored, or notes' \
    '  RT_MODELING     0 for measurement only, 1 to include modeling' \
    '' \
    'Options:' \
    '  -h, --help      show this help and exit without changing anything' \
    '' \
    'Examples:' \
    '  sh install.sh' \
    '  RT_TOOL_FORMAT=source sh install.sh' \
    '  RT_STORAGE=ignored RT_MODELING=1 sh install.sh'
}

say()  { printf 'llm_resource_tally: %s\n' "$*" >&2; }
die()  { say "error: $*"; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1 (try --help)"
      ;;
  esac
done

have git     || die "git is required"
have python3 || die "python3 is required"
have tar     || die "tar is required"
have curl || have wget || die "curl or wget is required"

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
POLICY_FILE="$ROOT/.llm_resource_tally/settings.json"
TOOL_PATH=".llm_resource_tally/tool"

# Emit shell-quoted, validated policy values. The artifact path is fixed and therefore does not
# need format-dependent reconstruction.
eval "$(python3 - "$POLICY_FILE" <<'PY'
import json
import shlex
import sys

path = sys.argv[1]
defaults = {
    "storage": "committed",
    "tool_format": "zipapp",
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
values = {
    "POLICY_STORAGE": storage,
    "POLICY_TOOL_FORMAT": fmt,
    "POLICY_MODELING": "1" if modeling else "0",
}
for key, value in values.items():
    print(f"{key}={shlex.quote(value)}")
PY
)"

RT_REPO="${RT_REPO:-Erotemic/llm_resource_tally}"
RT_REF="${RT_REF:-main}"
RT_TOOL_FORMAT="${RT_TOOL_FORMAT:-$POLICY_TOOL_FORMAT}"
RT_STORAGE="${RT_STORAGE:-$POLICY_STORAGE}"
RT_MODELING="${RT_MODELING:-$POLICY_MODELING}"
case "$RT_TOOL_FORMAT" in zipapp|source) ;; *) die "RT_TOOL_FORMAT must be zipapp or source" ;; esac
case "$RT_STORAGE" in committed|ignored|notes) ;; *) die "RT_STORAGE must be committed, ignored, or notes" ;; esac
case "$RT_MODELING" in 0|1) ;; *) die "RT_MODELING must be 0 or 1" ;; esac

say "installing $RT_REPO@$RT_REF as $RT_TOOL_FORMAT at $TOOL_PATH"
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

model_flag="--no-modeling"
[ "$RT_MODELING" = "1" ] && model_flag="--modeling"
if [ "$RT_TOOL_FORMAT" = "zipapp" ]; then
  bootstrap="$tmp/bootstrap-tool"
  if [ "$RT_MODELING" = "1" ]; then
    python3 -B "$tmp" build-zipapp --output "$bootstrap" --modeling
  else
    python3 -B "$tmp" build-zipapp --output "$bootstrap"
  fi
  python3 -B "$bootstrap" install --tool-format zipapp --storage "$RT_STORAGE" "$model_flag"
else
  python3 -B "$tmp" install --tool-format source --storage "$RT_STORAGE" "$model_flag"
fi

# Remove the only pre-invariant artifact name after the new canonical artifact is active.
rm -rf "$ROOT/.llm_resource_tally/tool.pyz"

if [ "$RT_MODELING" = "1" ]; then
  say "included the modeling subpackage (estimate: energy/carbon/USD)."
else
  say "minimal install (measurement only)."
fi
say "invoke with: python3 $TOOL_PATH"
case "$RT_STORAGE" in
  committed) say "done. Commit settings, tool, AGENTS.md, and intended accounting state." ;;
  ignored)   say "done. Commit .llm_resource_tally/settings.json; generated state stays ignored." ;;
  notes)     say "done. Commit settings/tool/AGENTS.md; sync refs/notes/llm-resource-tally explicitly." ;;
esac
