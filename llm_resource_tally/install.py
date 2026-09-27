# SPDX-License-Identifier: Apache-2.0
"""Install / uninstall / update orchestration.

The committed ``settings.json`` installation policy is canonical. The tool always lives at
``.llm_resource_tally/tool``; that path is a Python package directory in source mode and a ZIP
archive in zipapp mode. Therefore ``python3 .llm_resource_tally/tool`` is format-invariant.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

from .config import (
    CANONICAL_TOOL_PATH,
    ZIPAPP_TOOL_FORMATS,
    read_settings,
    register_backend,
)
from .doctor import print_report
from .gitutil import git, repo_root
from .storage import storage_description
from .vendoring import (
    artifact_has_modeling,
    cleanup_legacy_artifacts,
    current_tool_format,
    is_source_checkout_path,
    rel_dir,
    replace_managed_artifact,
    resolve_install_target,
    run_cmd,
    staging_path,
    vendor_source_into,
    vendor_zipapp_into,
)
from .repository_config import (
    apply_installation_policy,
    effective_installation_policy,
    print_policy_application,
    validate_repository_settings,
)
from .version import CANONICAL_REPO, tool_version
from .wiring_agents import uninstall_agents_block
from .wiring_claude import unwire_claude_hook, wire_claude_hook
from .wiring_common import chmod_x, git_config, read_text, strip_region
from .wiring_git import (
    HOOK_BEGIN,
    HOOK_END,
    is_legacy_tally_hookspath,
    effective_hooks_dir,
    ensure_tool_gitignore,
    hooks_dir_default,
    wire_hook,
)


def _same_target(root: str, rel: str, fmt: str) -> bool:
    current = rel_dir(root)
    return (
        current is not None
        and os.path.normpath(current) == os.path.normpath(rel)
        and current_tool_format() == fmt
    )



def _resolved_policy(args, root: str) -> dict:
    validate_repository_settings(root)
    stored = effective_installation_policy(root)
    fmt = getattr(args, "tool_format", None) or stored["tool_format"]
    mode = getattr(args, "storage", None) or stored["storage"]
    fmt, rel = resolve_install_target(root, None, fmt)
    modeling_arg = getattr(args, "modeling", None)
    if modeling_arg is not None:
        modeling = bool(modeling_arg)
    else:
        raw_install = read_settings(root).get("installation")
        if isinstance(raw_install, dict) and isinstance(raw_install.get("modeling"), bool):
            modeling = bool(raw_install["modeling"])
        elif os.path.exists(os.path.join(root, rel)):
            modeling = artifact_has_modeling(root, rel)
        else:
            modeling = stored["modeling"]
    return {"storage": mode, "tool_format": fmt, "tool_path": rel, "modeling": modeling}


def _build_staged_artifact(root: str, fmt: str, modeling: bool) -> tuple[str, str]:
    staged = staging_path(root)
    try:
        if fmt in ZIPAPP_TOOL_FORMATS:
            message = vendor_zipapp_into(
                root, staged, include_modeling=modeling, tool_format=fmt
            )
            if not os.path.isfile(staged):
                raise ValueError("zipapp builder did not produce a file")
            chmod_x(staged)
        else:
            message = vendor_source_into(root, staged, include_modeling=modeling)
            if not os.path.isfile(os.path.join(staged, "__main__.py")):
                raise ValueError("source builder did not produce __main__.py")
            chmod_x(os.path.join(staged, "__main__.py"))
        if artifact_has_modeling(root, staged) != modeling:
            raise ValueError("staged artifact modeling content does not match the requested policy")
        return staged, message
    except BaseException:
        if os.path.isdir(staged):
            shutil.rmtree(staged, ignore_errors=True)
        else:
            try:
                os.remove(staged)
            except OSError:
                pass
        raise


def cmd_install(args) -> None:
    root = repo_root()
    try:
        policy = _resolved_policy(args, root)
    except ValueError as exc:
        sys.exit(f"error: {exc}")
    fmt, rel = policy["tool_format"], policy["tool_path"]
    mode, modeling = policy["storage"], policy["modeling"]

    vendor_msg = None
    swap_msg = None
    same = _same_target(root, rel, fmt)
    current_matches = (
        os.path.exists(os.path.join(root, rel))
        and (
            (fmt in ZIPAPP_TOOL_FORMATS and os.path.isfile(os.path.join(root, rel)))
            or (fmt == "source" and os.path.isdir(os.path.join(root, rel)))
        )
        and artifact_has_modeling(root, rel) == modeling
    )
    if not (same and current_matches):
        try:
            staged, vendor_msg = _build_staged_artifact(root, fmt, modeling)
            swap_msg = replace_managed_artifact(root, staged, rel)
        except (OSError, ValueError) as exc:
            sys.exit(f"error: could not install {fmt} tool artifact: {exc}")

    # Policy application owns storage migration, data layout, ignore rules, and managed guidance.
    # It runs only after the requested artifact is available, but drains pending local rows before
    # changing the portable policy.
    try:
        policy_result = apply_installation_policy(
            root, policy, agents_file=args.agents_file, install_agents_guidance=True
        )
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        sys.exit(f"error: could not apply repository policy: {exc}")
    backends = register_backend(getattr(args, "backend", None), root)

    ensure_tool_gitignore(root, rel)
    run = run_cmd(rel)
    version = tool_version()
    hook_msg = wire_hook(root, rel, args.hook_mode)
    artifact_path = os.path.join(root, rel)
    if fmt in ZIPAPP_TOOL_FORMATS:
        chmod_x(artifact_path)
    elif not is_source_checkout_path(root, rel):
        chmod_x(os.path.join(artifact_path, "__main__.py"))
    claude_msg = wire_claude_hook(root, rel) if args.claude else None
    cleanup_msgs = cleanup_legacy_artifacts(root)

    print(f"llm_resource_tally v{version} installed in {os.path.basename(root)} [{rel}]")
    print(f"  tool format: {fmt}")
    print(f"  invocation : {run}")
    if policy_result.drain_message:
        print(f"  drained    : {policy_result.drain_message}")
    if vendor_msg:
        print(f"  built      : {vendor_msg}")
    if swap_msg:
        print(f"  swapped    : {swap_msg}")
    for message in cleanup_msgs:
        print(f"  cleanup    : {message}")
    print(f"  hook       : {hook_msg}")
    if policy_result.ignore_message:
        print(f"  .gitignore : {policy_result.ignore_message}")
    if policy_result.agents_message:
        print(f"  {args.agents_file:<11}: {policy_result.agents_message}")
    if claude_msg:
        print(f"  claude hook: {claude_msg}")
    print(f"  modeling   : {'included' if artifact_has_modeling(root, rel) else 'not included'}")
    print(f"  backends   : {', '.join(backends)}")
    print(f"  storage    : {mode} — {storage_description(root)}")
    print("  policy     : .llm_resource_tally/settings.json")
    if mode == "local":
        print(f"  publish    : `{run} publish` appends local rows to the configured durable ledger")
    elif mode == "notes":
        print("  notes sync : fetch/push refs/notes/llm-resource-tally explicitly when sharing")
    print(
        f"commit the policy and intended install changes; run `{run} reconcile && "
        f"{run} rollup` at session end when available."
    )
    print("doctor:")
    print_report(root, tool_path=artifact_path)


def cmd_uninstall(args) -> None:
    root = repo_root()
    msgs = []
    hp = git_config(root, "--get", "core.hooksPath")
    if is_legacy_tally_hookspath(root, hp):
        git("config", "--unset", "core.hooksPath", cwd=root)
        msgs.append(f"unset legacy tally-owned core.hooksPath ({hp})")
        hp = ""
    hd = effective_hooks_dir(root, hp) if hp else hooks_dir_default(root)
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
        sys.exit(
            "this is a pip install — upgrade with `pip install -U llm_resource_tally` "
            "then run `llm_resource_tally install`; repository policy will be reused"
        )
    try:
        policy = _resolved_policy(args, root)
    except ValueError as exc:
        sys.exit(f"error: {exc}")
    repo = args.repo or CANONICAL_REPO
    ref = args.ref
    url = f"https://raw.githubusercontent.com/{repo}/{ref}/install.sh"
    fetch = "curl -fsSL" if shutil.which("curl") else "wget -qO-" if shutil.which("wget") else None
    if not fetch:
        sys.exit("error: need curl or wget to update.")
    if (
        policy["tool_format"] == "source"
        and is_source_checkout_path(root, current_rel)
        and os.path.normpath(current_rel) == os.path.normpath(policy["tool_path"])
    ):
        sys.exit(
            "this tool is the source checkout itself; update it with git or choose "
            "`update --tool-format zipapp`"
        )
    if args.storage is not None:
        current_policy = effective_installation_policy(root)
        current_policy["storage"] = args.storage
        try:
            config_result = apply_installation_policy(root, current_policy)
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            sys.exit(f"error: could not apply repository policy: {exc}")
        print_policy_application(config_result)
        policy = _resolved_policy(args, root)
    print(
        f"updating {CANONICAL_TOOL_PATH} ({policy['tool_format']}, {policy['storage']}) from {repo}@{ref} ..."
    )
    env = {
        **os.environ,
        "RT_REPO": repo,
        "RT_REF": ref,
        "RT_TOOL_FORMAT": policy["tool_format"],
        "RT_MODELING": "1" if policy["modeling"] else "0",
        "RT_STORAGE": policy["storage"],
    }
    subprocess.run(f'{fetch} "{url}" | sh', shell=True, cwd=root, env=env, check=True)
