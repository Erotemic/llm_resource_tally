# SPDX-License-Identifier: Apache-2.0
"""Install / uninstall / update orchestration.

The mechanics live in focused modules: ``vendoring`` (source tree or deterministic zipapp),
``wiring_git`` (post-commit hook), ``wiring_agents`` (AGENTS.md), and ``wiring_claude``.
Network is only needed by the curl bootstrap, ``update``, or adding modeling to a minimal zipapp.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

from .config import register_backend
from .gitutil import git, repo_root
from .storage import set_storage_mode, storage_description, storage_mode
from .vendoring import (artifact_has_modeling, current_tool_format, infer_tool_format,
                         is_source_checkout_path, rel_dir, resolve_install_target, run_cmd,
                         shared_hooks_rel, vendor_source_into, vendor_zipapp_into)
from .version import CANONICAL_REPO, tool_version
from .wiring_agents import install_agents_block, uninstall_agents_block
from .wiring_claude import unwire_claude_hook, wire_claude_hook
from .wiring_common import chmod_x, git_config, read_text, strip_region
from .wiring_git import (HOOK_BEGIN, HOOK_END, configure_gitignore, ensure_hook_file,
                         ensure_tool_gitignore, hooks_dir_default, wire_hook)


def _same_target(root: str, rel: str, fmt: str) -> bool:
    current = rel_dir(root)
    return (current is not None
            and os.path.normpath(current) == os.path.normpath(rel)
            and current_tool_format() == fmt)


def cmd_install(args) -> None:
    root = repo_root()
    if getattr(args, "storage", None):
        set_storage_mode(args.storage, root)
    mode = storage_mode(root)
    try:
        fmt, rel = resolve_install_target(root, getattr(args, "dir", None),
                                          getattr(args, "tool_format", "auto"))
    except ValueError as exc:
        sys.exit(f"error: {exc}")
    if not rel:
        sys.exit("error: could not determine the tool path; pass --dir explicitly")

    vendor_msg = None
    if not _same_target(root, rel, fmt):
        try:
            if fmt == "zipapp":
                vendor_msg = vendor_zipapp_into(
                    root, rel, include_modeling=bool(getattr(args, "modeling", False)))
            else:
                vendor_msg = vendor_source_into(root, rel)
        except (OSError, ValueError) as exc:
            sys.exit(f"error: could not install {fmt} tool artifact: {exc}")

    hooks_rel = shared_hooks_rel(root, rel)
    ensure_hook_file(root, rel, hooks_rel)
    ensure_tool_gitignore(root, rel)
    run = run_cmd(rel)
    version = tool_version()
    hook_msg = wire_hook(root, rel, args.hook_mode, hooks_rel)
    ignore_msg = configure_gitignore(root, rel, mode)
    agents_msg = install_agents_block(root, run, version, args.agents_file, mode=mode)
    artifact_path = os.path.join(root, rel)
    if fmt == "zipapp":
        chmod_x(artifact_path)
    elif not is_source_checkout_path(root, rel):
        chmod_x(os.path.join(artifact_path, "__main__.py"))
    claude_msg = wire_claude_hook(root, rel) if args.claude else None

    modeling_msg = None
    if getattr(args, "modeling", False):
        from .modeling_bridge import ensure_modeling
        try:
            modeling_msg = ensure_modeling(root, rel)
        except Exception as exc:                         # network/extract failure is non-fatal
            modeling_msg = f"could not add modeling ({exc}); core install is unaffected"

    backends = register_backend(getattr(args, "backend", None))
    print(f"llm_resource_tally v{version} installed in {os.path.basename(root)} [{rel}]")
    print(f"  tool format: {fmt}")
    if vendor_msg:
        print(f"  vendored   : {vendor_msg}")
    print(f"  hook       : {hook_msg}")
    if ignore_msg:
        print(f"  .gitignore : {ignore_msg}")
    print(f"  {args.agents_file:<11}: {agents_msg}")
    if claude_msg:
        print(f"  claude hook: {claude_msg}")
    if modeling_msg:
        print(f"  modeling   : {modeling_msg}")
    elif fmt == "zipapp":
        flavor = "included" if artifact_has_modeling(root, rel) else "not included"
        print(f"  modeling   : {flavor}")
    print(f"  backends   : {', '.join(backends)}")
    print(f"  storage    : {mode} — {storage_description(root)}")
    if mode == "notes":
        print("  notes sync : fetch/push refs/notes/llm-resource-tally explicitly when sharing")
    print(f"commit the changes to share them; run `{run} reconcile && {run} rollup` at session end.")
    from .doctor import print_report
    print("doctor:")
    print_report(root, tool_path=artifact_path)


def cmd_uninstall(args) -> None:
    root = repo_root()
    rel = args.dir or rel_dir(root)
    msgs = []
    hp = git_config(root, "--get", "core.hooksPath")
    shared = shared_hooks_rel(root, rel) if rel else None
    if shared and hp and os.path.normpath(hp) == os.path.normpath(shared):
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
    for message in msgs:
        print(f"  - {message}")
    if msgs:
        print("  the .llm_resource_tally/ ledger and tool artifact were left in place.")


def cmd_update(args) -> None:
    """Re-vendor the latest source or zipapp, preserving the current artifact format."""
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
    fmt = infer_tool_format(root, rel)
    if fmt == "source" and is_source_checkout_path(root, rel):
        sys.exit("this tool is a source checkout/submodule — update it with git (for example "
                 "`git submodule update --remote`) or convert to `--tool-format zipapp`; "
                 "the bootstrap updater only replaces vendored artifacts.")
    modeling = "1" if artifact_has_modeling(root, rel) else "0"
    print(f"updating {rel} ({fmt}) from {repo}@{ref} ...")
    env = {**os.environ, "RT_REPO": repo, "RT_REF": ref, "RT_DIR": rel,
           "RT_TOOL_FORMAT": fmt, "RT_MODELING": modeling,
           "RT_STORAGE": storage_mode(root)}
    subprocess.run(f'{fetch} "{url}" | sh', shell=True, cwd=root, env=env, check=True)
