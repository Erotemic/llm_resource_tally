# SPDX-License-Identifier: Apache-2.0
"""Effective repository policy and safe policy transitions.

This module deliberately sits above the JSON settings helpers in :mod:`config`.  It owns the
repository effects implied by installation policy: storage migration, data-layout creation,
managed ignore rules, and storage-dependent agent guidance.  Installing the executable and wiring
hooks remain separate concerns.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

from .config import (
    CANONICAL_TOOL_PATH,
    DEFAULT_INSTALLATION,
    STORAGE_MODES,
    TOOL_FORMATS,
    installation_policy,
    publication_policy,
    read_settings,
    registered_backends,
    resolve_repository_path,
    set_installation_policy,
    set_publication_policy,
    settings_path,
)
from .gitutil import git, repo_root
from .ledger import (
    ensure_data_dir,
    ensure_published_layout,
    local_shard_paths,
    published_ledger_dir,
    published_totals_path,
)
from .publish import drain_spool_to_notes, publish_local
from .storage import local_data_dir, notes_ref
from .vendoring import run_cmd
from .version import tool_version
from .wiring_agents import AGENTS_BEGIN_RE, AGENTS_END, install_agents_block
from .wiring_common import read_text
from .wiring_git import configure_gitignore

TRACKED_ACCOUNTING_GLOBS = (
    ".llm_resource_tally/ledger/*.jsonl",
    ".llm_resource_tally/resource-ledger.jsonl",
    ".llm_resource_tally/lifetime-totals.json",
)


@dataclass(frozen=True)
class PolicyApplication:
    """Observable result of applying repository installation policy."""

    old_policy: dict
    new_policy: dict
    policy_changed: bool
    storage_changed: bool
    drain_message: str | None = None
    ignore_message: str | None = None
    agents_message: str | None = None

    @property
    def changed(self) -> bool:
        return self.policy_changed or self.storage_changed


@dataclass(frozen=True)
class PublicationApplication:
    """Observable result of changing durable publication destinations."""

    old_policy: dict
    new_policy: dict
    changed: bool
    ignore_message: str | None = None
    agents_message: str | None = None


def _raw_installation(root: str) -> dict:
    raw = read_settings(root).get("installation")
    return dict(raw) if isinstance(raw, dict) else {}


def validate_repository_settings(root: str) -> None:
    """Refuse to modify a malformed or structurally incompatible policy file."""
    path = settings_path(root)
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        rel = os.path.relpath(path, root)
        raise ValueError(f"{rel} is not valid JSON at line {exc.lineno}, column {exc.colno}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{os.path.relpath(path, root)} must contain a JSON object")
    installation = data.get("installation")
    if installation is not None and not isinstance(installation, dict):
        raise ValueError("settings installation policy must be a JSON object")
    publication = data.get("publication")
    if publication is not None and not isinstance(publication, dict):
        raise ValueError("settings publication policy must be a JSON object")
    if isinstance(publication, dict):
        for key in ("append_ledger_dir", "lifetime_totals_path"):
            value = publication.get(key)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"settings publication {key} must be a non-empty path string")


def _tracked_accounting_exists(root: str) -> bool:
    try:
        tracked = git("ls-files", "--", *TRACKED_ACCOUNTING_GLOBS, cwd=root).splitlines()
    except subprocess.CalledProcessError:
        return False
    return any(line.strip() for line in tracked)


def effective_storage(root: str) -> tuple[str, str]:
    """Return effective storage and its provenance: settings, inferred, or default."""
    raw = _raw_installation(root)
    if raw.get("storage") in STORAGE_MODES:
        return raw["storage"], "settings"
    if _tracked_accounting_exists(root):
        # Repositories installed before portable policy existed used committed storage.  Treat
        # tracked ledger/report evidence as authoritative so an upgrade never silently freezes it.
        return "committed", "inferred from tracked accounting"
    return DEFAULT_INSTALLATION["storage"], "default"


def effective_repository_config(root: str | None = None) -> dict:
    """Return concise effective policy plus provenance suitable for CLI display."""
    root = root or repo_root()
    raw_data = read_settings(root)
    raw = raw_data.get("installation")
    raw = raw if isinstance(raw, dict) else {}
    normalized = installation_policy(root)
    storage, storage_source = effective_storage(root)
    normalized["storage"] = storage

    sources = {"storage": storage_source}
    sources["tool_format"] = (
        "settings" if raw.get("tool_format") in TOOL_FORMATS else "default"
    )
    sources["tool_path"] = (
        "settings" if raw.get("tool_path") == CANONICAL_TOOL_PATH else "default"
    )
    sources["modeling"] = "settings" if isinstance(raw.get("modeling"), bool) else "default"

    raw_backends = raw_data.get("backends")
    backends = registered_backends(root)
    sources["recorder_backends"] = (
        "settings"
        if isinstance(raw_backends, list)
        and any(isinstance(name, str) for name in raw_backends)
        else "default"
    )
    raw_notes_ref = raw.get("notes_ref")
    effective_notes_ref = notes_ref(root)
    sources["notes_ref"] = (
        "settings"
        if isinstance(raw_notes_ref, str) and raw_notes_ref.startswith("refs/notes/")
        else "default"
    )
    raw_publication = raw_data.get("publication")
    raw_publication = raw_publication if isinstance(raw_publication, dict) else {}
    publication = publication_policy(root)
    for key in ("append_ledger_dir", "lifetime_totals_path"):
        sources[key] = (
            "settings"
            if isinstance(raw_publication.get(key), str) and raw_publication[key].strip()
            else "default"
        )
    return {
        "settings_file": os.path.relpath(settings_path(root), root),
        "storage": normalized["storage"],
        "tool_format": normalized["tool_format"],
        "tool_path": normalized["tool_path"],
        "modeling": normalized["modeling"],
        "recorder_backends": backends,
        "notes_ref": effective_notes_ref,
        "append_ledger_dir": publication["append_ledger_dir"],
        "lifetime_totals_path": publication["lifetime_totals_path"],
        "sources": sources,
    }


def effective_installation_policy(root: str | None = None) -> dict:
    config = effective_repository_config(root)
    return {
        "storage": config["storage"],
        "tool_format": config["tool_format"],
        "tool_path": config["tool_path"],
        "modeling": config["modeling"],
    }


def _policy_is_explicit(root: str, policy: dict) -> bool:
    raw = _raw_installation(root)
    return all(raw.get(key) == value for key, value in policy.items())


def _drain_local_spool(root: str, old_mode: str, new_mode: str) -> str | None:
    """Safely incorporate pending local rows before changing the selected storage backend."""
    if old_mode != "local" or new_mode == "local" or not local_shard_paths(root):
        return None
    if new_mode == "notes":
        moved = drain_spool_to_notes(root)
        return (
            f"moved {moved} pending local row(s) into {notes_ref(root)} before switching"
            if moved
            else None
        )
    path, rows, _skipped, _reports = publish_local(root)
    if path is None:
        return None
    return f"published {rows} pending local row(s) to {os.path.relpath(path, root)} before switching"


def _has_managed_agents_block(root: str, agents_file: str) -> bool:
    path = os.path.join(root, agents_file)
    if not os.path.isfile(path):
        return False
    text = read_text(path)
    match = AGENTS_BEGIN_RE.search(text)
    return bool(match and AGENTS_END in text[match.end() :])


def _publication_generated_paths(root: str) -> tuple[tuple[str, bool], ...]:
    return ((published_ledger_dir(root), True), (published_totals_path(root), False))


def apply_publication_policy(
    root: str,
    requested_policy: dict,
    *,
    agents_file: str = "AGENTS.md",
) -> PublicationApplication:
    """Apply durable ledger/report destinations without migrating historical files.

    Existing worktree shards remain readable after redirection. The configured append destination
    receives new durable rows; callers may move older external stores explicitly if they later
    redirect from one external location to another.
    """
    validate_repository_settings(root)
    old_policy = publication_policy(root)
    new_policy = dict(old_policy)
    new_policy.update(requested_policy)
    for key in ("append_ledger_dir", "lifetime_totals_path"):
        value = new_policy.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"publication {key} must be a non-empty path string")

    changed = new_policy != old_policy
    if not changed:
        return PublicationApplication(old_policy, new_policy, False)

    # Fail early on impossible parent directories before making the portable setting authoritative.
    ledger_dir = resolve_repository_path(new_policy["append_ledger_dir"], root)
    totals_path = resolve_repository_path(new_policy["lifetime_totals_path"], root)
    os.makedirs(ledger_dir, exist_ok=True)
    os.makedirs(os.path.dirname(totals_path), exist_ok=True)
    set_publication_policy(root=root, **new_policy)
    ensure_published_layout(root)

    install = effective_installation_policy(root)
    ignore_message = configure_gitignore(
        root,
        install["tool_path"],
        install["storage"],
        generated_paths=_publication_generated_paths(root),
    )
    agents_message = None
    if _has_managed_agents_block(root, agents_file):
        agents_message = install_agents_block(
            root,
            run_cmd(install["tool_path"]),
            tool_version(),
            agents_file,
            mode=install["storage"],
        )
    return PublicationApplication(
        old_policy=old_policy,
        new_policy=new_policy,
        changed=True,
        ignore_message=ignore_message,
        agents_message=agents_message,
    )


def apply_installation_policy(
    root: str,
    requested_policy: dict,
    *,
    agents_file: str = "AGENTS.md",
    install_agents_guidance: bool = False,
) -> PolicyApplication:
    """Apply repository policy through the one authoritative transition path.

    Pending local rows are drained *before* the portable policy changes.  If that required
    migration fails, settings and managed files remain on the old mode and the command can be
    retried.  Artifact installation and hook wiring are intentionally not performed here.
    """
    validate_repository_settings(root)
    old_policy = effective_installation_policy(root)
    new_policy = dict(requested_policy)
    if new_policy.get("storage") not in STORAGE_MODES:
        raise ValueError(
            f"unknown storage mode {new_policy.get('storage')!r}; choose from {', '.join(STORAGE_MODES)}"
        )
    if new_policy.get("tool_format") not in TOOL_FORMATS:
        raise ValueError(f"unknown tool format {new_policy.get('tool_format')!r}")
    if new_policy.get("tool_path") != CANONICAL_TOOL_PATH:
        raise ValueError(f"tool path is fixed at {CANONICAL_TOOL_PATH!r}")
    new_policy["modeling"] = bool(new_policy.get("modeling"))

    storage_changed = old_policy["storage"] != new_policy["storage"]
    policy_changed = old_policy != new_policy or not _policy_is_explicit(root, new_policy)
    if not policy_changed and not install_agents_guidance:
        return PolicyApplication(old_policy, new_policy, False, False)

    drain_message = _drain_local_spool(
        root, old_policy["storage"], new_policy["storage"]
    )
    if storage_changed and old_policy["storage"] == "local":
        # The local directory is entirely generated state.  Once every pending row has reached
        # its durable destination, remove the now-obsolete spool and rollup so removing its ignore
        # rule cannot leave empty generated files as worktree noise.
        local_dir = local_data_dir(root)
        if os.path.isdir(local_dir):
            shutil.rmtree(local_dir)
    set_installation_policy(root=root, **new_policy)
    # Materialize publication defaults on install/transition so the path object is discoverable
    # in the committed settings file even before a repository customizes either destination.
    set_publication_policy(root=root, **publication_policy(root))
    ensure_data_dir(root)
    if new_policy["storage"] == "local":
        # Local recording is separate from the tracked publication layout.  Preserve/create the
        # latter so existing history stays available and future `publish` has a stable target.
        ensure_published_layout(root)

    ignore_message = configure_gitignore(
        root,
        new_policy["tool_path"],
        new_policy["storage"],
        generated_paths=_publication_generated_paths(root),
    )
    agents_message = None
    if install_agents_guidance or _has_managed_agents_block(root, agents_file):
        agents_message = install_agents_block(
            root,
            run_cmd(new_policy["tool_path"]),
            tool_version(),
            agents_file,
            mode=new_policy["storage"],
        )
    return PolicyApplication(
        old_policy=old_policy,
        new_policy=new_policy,
        policy_changed=policy_changed,
        storage_changed=storage_changed,
        drain_message=drain_message,
        ignore_message=ignore_message,
        agents_message=agents_message,
    )


def _display_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)


def cmd_config_show(args) -> None:
    root = repo_root()
    try:
        validate_repository_settings(root)
        config = effective_repository_config(root)
    except (OSError, ValueError) as exc:
        sys.exit(f"error: could not read repository policy: {exc}")
    if args.json:
        print(json.dumps(config, indent=2, sort_keys=True))
        return
    print(f"settings_file: {config['settings_file']}")
    for key in (
        "storage",
        "tool_format",
        "tool_path",
        "modeling",
        "recorder_backends",
        "notes_ref",
        "append_ledger_dir",
        "lifetime_totals_path",
    ):
        print(f"{key}: {_display_value(config[key])} ({config['sources'][key]})")


def print_policy_application(result: PolicyApplication, agents_file: str = "AGENTS.md") -> None:
    old = result.old_policy["storage"]
    new = result.new_policy["storage"]
    if not result.changed:
        print(f"repository configuration unchanged: storage is already {new}")
        return
    print(f"repository configuration updated: storage {old} -> {new}")
    print("  policy     : .llm_resource_tally/settings.json")
    if result.drain_message:
        print(f"  drained    : {result.drain_message}")
    if result.ignore_message:
        print(f"  .gitignore : {result.ignore_message}")
    if result.agents_message:
        print(f"  {agents_file:<11}: {result.agents_message}")


def print_publication_application(
    result: PublicationApplication, agents_file: str = "AGENTS.md"
) -> None:
    if not result.changed:
        print("repository publication configuration unchanged")
        return
    print("repository publication configuration updated:")
    for key in ("append_ledger_dir", "lifetime_totals_path"):
        old = result.old_policy[key]
        new = result.new_policy[key]
        if old != new:
            print(f"  {key}: {old} -> {new}")
    print("  policy     : .llm_resource_tally/settings.json")
    if result.ignore_message:
        print(f"  .gitignore : {result.ignore_message}")
    if result.agents_message:
        print(f"  {agents_file:<11}: {result.agents_message}")


def cmd_config_set(args) -> None:
    root = repo_root()
    requested_publication = {}
    if args.append_ledger_dir is not None:
        requested_publication["append_ledger_dir"] = args.append_ledger_dir
    if args.lifetime_totals_path is not None:
        requested_publication["lifetime_totals_path"] = args.lifetime_totals_path
    if args.storage is None and not requested_publication:
        sys.exit(
            "error: config set requires --storage, --append-ledger-dir, or --lifetime-totals-path"
        )

    publication_result = None
    try:
        if requested_publication:
            publication_result = apply_publication_policy(
                root, requested_publication, agents_file=args.agents_file
            )
        policy_result = None
        if args.storage is not None:
            policy = effective_installation_policy(root)
            policy["storage"] = args.storage
            policy_result = apply_installation_policy(root, policy, agents_file=args.agents_file)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        if publication_result is not None:
            print_publication_application(publication_result, args.agents_file)
        sys.exit(f"error: could not apply repository policy: {exc}")

    if publication_result is not None:
        print_publication_application(publication_result, args.agents_file)
    if policy_result is not None:
        print_policy_application(policy_result, args.agents_file)
