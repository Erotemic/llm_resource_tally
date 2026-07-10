# SPDX-License-Identifier: Apache-2.0
"""Install / uninstall / update orchestration.

The committed ``settings.json`` installation policy is canonical. Explicit install/update flags
replace that policy; omitted flags read it. This makes a fresh clone reconstruct the same storage
and artifact representation without machine-local git configuration.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

from .config import (installation_policy, read_settings, register_backend, set_installation_policy)
from .gitutil import git, repo_root
from .storage import storage_description
from .vendoring import (DEFAULT_SOURCE_DIR, DEFAULT_ZIPAPP_PATH, artifact_has_modeling,
                         current_tool_format, infer_tool_format, is_source_checkout_path, rel_dir,
                         resolve_install_target, run_cmd, shared_hooks_rel,
                         vendor_source_into, vendor_zipapp_into)
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


def _default_tool_path(fmt: str) -> str:
    return DEFAULT_ZIPAPP_PATH if fmt == "zipapp" else DEFAULT_SOURCE_DIR


def _resolved_policy(args, root: str) -> dict:
    stored = installation_policy(root)
    explicit_dir = getattr(args, "dir", None)
    explicit_format = getattr(args, "tool_format", None)
    if explicit_dir and explicit_format is None:
        fmt = infer_tool_format(root, explicit_dir)
    else:
        fmt = explicit_format or stored["tool_format"]
    mode = getattr(args, "storage", None) or stored["storage"]
    modeling_arg = getattr(args, "modeling", None)
    requested_dir = explicit_dir
    if requested_dir is None:
        if explicit_format and fmt != stored["tool_format"]:
            requested_dir = _default_tool_path(fmt)
        else:
            requested_dir = stored["tool_path"]
    fmt, rel = resolve_install_target(root, requested_dir, fmt)
    if modeling_arg is not None:
        modeling = bool(modeling_arg)
    else:
        raw_install = read_settings(root).get("installation")
        if isinstance(raw_install, dict) and isinstance(raw_install.get("modeling"), bool):
            modeling = bool(raw_install["modeling"])
        elif os.path.exists(os.path.join(root, rel)):
            # First policy initialization from an explicitly installed artifact: preserve what
            # is actually present rather than silently stripping optional functionality.
            modeling = artifact_has_modeling(root, rel)
        else:
            modeling = stored["modeling"]
    return {"storage": mode, "tool_format": fmt, "tool_path": rel,
            "modeling": modeling}


def _remove_obsolete_artifact(root: str, old_rel: str, new_rel: str) -> str | None:
    """Remove an old managed artifact unless this process is executing from it."""
    if not old_rel or os.path.normpath(old_rel) == os.path.normpath(new_rel):
        return None
    old = os.path.realpath(os.path.join(root, old_rel))
    invocation = rel_dir(root)
    if invocation:
        running = os.path.realpath(os.path.join(root, invocation))
        if running == old or running.startswith(old + os.sep):
            return f"left previous artifact {old_rel} in place because this command is running from it"
    if os.path.isdir(old):
        shutil.rmtree(old)
        return f"removed previous source artifact {old_rel}"
    if os.path.isfile(old):
        os.remove(old)
        return f"removed previous artifact {old_rel}"
    return None


def cmd_install(args) -> None:
    root = repo_root()
    old_policy = installation_policy(root)
    try:
        policy = _resolved_policy(args, root)
    except ValueError as exc:
        sys.exit(f"error: {exc}")
    fmt, rel = policy["tool_format"], policy["tool_path"]
    mode, modeling = policy["storage"], policy["modeling"]

    vendor_msg = None
    same = _same_target(root, rel, fmt)
    if same and artifact_has_modeling(root, rel) != modeling:
        sys.exit("error: changing modeling content in the running artifact is not safe; use "
                 "`update --modeling` or `update --no-modeling` so the replacement is built "
                 "before it is executed")
    if not same:
        try:
            if fmt == "zipapp":
                vendor_msg = vendor_zipapp_into(root, rel, include_modeling=modeling)
            else:
                vendor_msg = vendor_source_into(root, rel, include_modeling=modeling)
        except (OSError, ValueError) as exc:
            sys.exit(f"error: could not install {fmt} tool artifact: {exc}")

    # Policy/backends are written before gitignore wiring so ignored-mode index migration can
    # retain the final portable settings file rather than an intermediate version.
    set_installation_policy(root=root, **policy)
    backends = register_backend(getattr(args, "backend", None), root)

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

    cleanup_msg = _remove_obsolete_artifact(root, old_policy.get("tool_path", ""), rel)

    print(f"llm_resource_tally v{version} installed in {os.path.basename(root)} [{rel}]")
    print(f"  tool format: {fmt}")
    if vendor_msg:
        print(f"  vendored   : {vendor_msg}")
    if cleanup_msg:
        print(f"  cleanup    : {cleanup_msg}")
    print(f"  hook       : {hook_msg}")
    if ignore_msg:
        print(f"  .gitignore : {ignore_msg}")
    print(f"  {args.agents_file:<11}: {agents_msg}")
    if claude_msg:
        print(f"  claude hook: {claude_msg}")
    print(f"  modeling   : {'included' if artifact_has_modeling(root, rel) else 'not included'}")
    print(f"  backends   : {', '.join(backends)}")
    print(f"  storage    : {mode} — {storage_description(root)}")
    print("  policy     : .llm_resource_tally/settings.json")
    if mode == "notes":
        print("  notes sync : fetch/push refs/notes/llm-resource-tally explicitly when sharing")
    print(f"commit the policy and intended generated-file changes; run `{run} reconcile && "
          f"{run} rollup` at session end.")
    from .doctor import print_report
    print("doctor:")
    print_report(root, tool_path=artifact_path)


def cmd_uninstall(args) -> None:
    root = repo_root()
    rel = args.dir or installation_policy(root)["tool_path"]
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
        print("  the ledger, portable settings, and tool artifact were left in place.")


def cmd_update(args) -> None:
    """Fetch and reinstall using the stored policy, optionally replacing it."""
    root = repo_root()
    current_rel = rel_dir(root)
    if current_rel is None:
        sys.exit("this is a pip install — upgrade with `pip install -U llm_resource_tally` "
                 "then run `llm_resource_tally install`; repository policy will be reused")
    try:
        policy = _resolved_policy(args, root)
    except ValueError as exc:
        sys.exit(f"error: {exc}")
    repo = args.repo or CANONICAL_REPO
    ref = args.ref
    url = f"https://raw.githubusercontent.com/{repo}/{ref}/install.sh"
    fetch = ("curl -fsSL" if shutil.which("curl")
             else "wget -qO-" if shutil.which("wget") else None)
    if not fetch:
        sys.exit("error: need curl or wget to update.")
    if (policy["tool_format"] == "source"
            and is_source_checkout_path(root, current_rel)
            and os.path.normpath(current_rel) == os.path.normpath(policy["tool_path"])):
        sys.exit("this tool is the source checkout itself; update it with git or choose "
                 "`update --tool-format zipapp`")
    print(f"updating {policy['tool_path']} ({policy['tool_format']}, {policy['storage']}) "
          f"from {repo}@{ref} ...")
    env = {
        **os.environ,
        "RT_REPO": repo,
        "RT_REF": ref,
        "RT_DIR": policy["tool_path"],
        "RT_TOOL_FORMAT": policy["tool_format"],
        "RT_MODELING": "1" if policy["modeling"] else "0",
        "RT_STORAGE": policy["storage"],
    }
    subprocess.run(f'{fetch} "{url}" | sh', shell=True, cwd=root, env=env, check=True)
