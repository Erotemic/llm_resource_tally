# SPDX-License-Identifier: Apache-2.0
"""Install / uninstall / update — orchestration only. The mechanics live in focused modules:
`vendoring` (copy the package in), `wiring_git` (post-commit hook), `wiring_agents`
(AGENTS.md), `wiring_claude` (.claude/settings.json). Network is only needed by the curl
bootstrap (install.sh) and `update`."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

from .config import register_backend
from .gitutil import git, repo_root
from .vendoring import (DEFAULT_VENDOR_DIR, is_pip_install, rel_dir, run_cmd, vendor_into)
from .version import CANONICAL_REPO, tool_version
from .wiring_agents import install_agents_block, uninstall_agents_block
from .wiring_claude import unwire_claude_hook, wire_claude_hook
from .wiring_common import chmod_x, git_config, read_text, strip_region
from .wiring_git import (HOOK_BEGIN, HOOK_END, ensure_hook_file, ensure_tool_gitignore,
                         hooks_dir_default, wire_hook)


def cmd_install(args) -> None:
    root = repo_root()
    if is_pip_install():
        rel = args.dir or DEFAULT_VENDOR_DIR
        vendor_msg = vendor_into(root, rel)
    else:
        rel = args.dir or rel_dir(root)
        vendor_msg = None
    ensure_hook_file(root, rel)           # guarantee the shared hook script exists
    ensure_tool_gitignore(root, rel)      # keep the tool's __pycache__ out of the host repo
    run = run_cmd(rel)
    version = tool_version()
    hook_msg = wire_hook(root, rel, args.hook_mode)
    agents_msg = install_agents_block(root, run, version, args.agents_file)
    chmod_x(os.path.join(root, rel, "__main__.py"))
    claude_msg = wire_claude_hook(root, rel) if args.claude else None
    modeling_msg = None
    if getattr(args, "modeling", False):
        from .modeling_bridge import ensure_modeling
        try:
            modeling_msg = ensure_modeling(root, rel)
        except Exception as e:                         # network/extract failure is non-fatal
            modeling_msg = f"could not add modeling ({e}); core install is unaffected"
    backends = register_backend(getattr(args, "backend", None))
    print(f"llm_resource_tally v{version} installed in {os.path.basename(root)} [{rel}]")
    if vendor_msg:
        print(f"  vendored   : {vendor_msg}")
    print(f"  hook       : {hook_msg}")
    print(f"  {args.agents_file:<11}: {agents_msg}")
    if claude_msg:
        print(f"  claude hook: {claude_msg}")
    if modeling_msg:
        print(f"  modeling   : {modeling_msg}")
    print(f"  backends   : {', '.join(backends)} (passive hook records these; "
          "edit .llm_resource_tally/settings.json to change)")
    print("  ledger     : .llm_resource_tally/ledger/ at repo root (committed; data never touched by install)")
    print(f"commit the changes to share them; run `{run} reconcile && {run} rollup` at session end.")
    from .doctor import print_report
    print("doctor:")
    print_report(root)


def cmd_uninstall(args) -> None:
    root = repo_root()
    rel = args.dir or rel_dir(root)
    msgs = []
    hp = git_config(root, "--get", "core.hooksPath")
    if rel and hp and os.path.normpath(hp) == os.path.normpath(f"{rel}/hooks"):
        git("config", "--unset", "core.hooksPath", cwd=root)
        msgs.append(f"unset core.hooksPath ({hp})")
    hd = (hp if os.path.isabs(hp) else os.path.join(root, hp)) if hp else hooks_dir_default(root)
    hook = os.path.join(hd, "post-commit")
    if os.path.exists(hook):
        text = read_text(hook)
        if HOOK_BEGIN in text:
            s = text.index(HOOK_BEGIN)
            stripped = strip_region(text, s, HOOK_END)
            if stripped.strip() in ("", "#!/usr/bin/env bash", "#!/bin/sh"):
                os.remove(hook)
                msgs.append(f"removed {os.path.relpath(hook, root)}")
            else:
                with open(hook, "w", encoding="utf-8") as fh:
                    fh.write(stripped)
                msgs.append(f"stripped managed block from {os.path.relpath(hook, root)}")
    agents_removed = uninstall_agents_block(root, args.agents_file)
    if agents_removed:
        msgs.append(agents_removed)
    claude_removed = unwire_claude_hook(root)
    if claude_removed:
        msgs.append(claude_removed)
    print("llm_resource_tally uninstalled:" if msgs else "nothing to uninstall.")
    for m in msgs:
        print(f"  - {m}")
    if msgs:
        print("  the .llm_resource_tally/ ledger and the package files were left in place.")


def cmd_update(args) -> None:
    """Re-vendor the latest version from the canonical repo, then re-run install. Needs
    network. The pinned vendored copy keeps working if this fails or the host is gone."""
    root = repo_root()
    rel = rel_dir(root)
    if rel is None:
        sys.exit("this is a pip install — upgrade with `pip install -U llm_resource_tally` "
                 "then re-run `llm_resource_tally install` to re-vendor.")
    repo = args.repo or CANONICAL_REPO
    ref = args.ref
    url = f"https://raw.githubusercontent.com/{repo}/{ref}/install.sh"
    fetch = ("curl -fsSL" if shutil.which("curl")
             else "wget -qO-" if shutil.which("wget") else None)
    if not fetch:
        sys.exit("error: need curl or wget to update.")
    print(f"updating {rel} from {repo}@{ref} ...")
    env = {**os.environ, "RT_REPO": repo, "RT_REF": ref, "RT_DIR": rel}
    subprocess.run(f'{fetch} "{url}" | sh', shell=True, cwd=root, env=env, check=True)
