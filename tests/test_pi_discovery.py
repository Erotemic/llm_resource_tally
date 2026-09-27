# SPDX-License-Identifier: Apache-2.0
"""Pi discovery regression tests."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
PKG = REPO / "llm_resource_tally"
sys.path.insert(0, str(REPO))
from llm_resource_tally.backends.pi import (  # noqa: E402
    LAYOUT_DEFAULT_ROOT,
    LAYOUT_EXPLICIT_DIR,
    default_sessions_dir,
    pi_munged_project_dir,
    resolve_sessions,
)

from pi_support import (  # noqa: E402
    PI,
    _neutralize_pi_env,  # noqa: F401
    init_repo,
    pi_assistant,
    pi_header,
    pi_usage,
    set_mtime,
    write_session,
)

def test_resolve_sessions_precedence_and_layout(tmp_path, monkeypatch):
    sessions = str(tmp_path / "tally-sessions")
    agent_env = str(tmp_path / "agent-env")
    global_dir = str(tmp_path / "agent-global")
    repo = tmp_path / "repo"
    init_repo(repo)

    # 1. tally's own override wins — an explicit dir (Pi would write files directly in it).
    monkeypatch.setenv("PI_SESSIONS_DIR", sessions)
    assert resolve_sessions() == (sessions, LAYOUT_EXPLICIT_DIR)
    monkeypatch.delenv("PI_SESSIONS_DIR")
    # 2. Pi's env var next — also explicit. And it beats any sessionDir setting (Pi's own
    #    precedence: --session-dir / PI_CODING_AGENT_SESSION_DIR, then settings).
    monkeypatch.setenv("PI_CODING_AGENT_SESSION_DIR", agent_env)
    (repo / ".pi").mkdir()
    (repo / ".pi" / "settings.json").write_text(json.dumps({"sessionDir": "my-sessions"}))
    monkeypatch.chdir(repo)
    assert resolve_sessions() == (agent_env, LAYOUT_EXPLICIT_DIR)
    monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR")
    # 3. project .pi/settings.json sessionDir (relative -> resolved against the cwd, which
    #    here is the repo root) — explicit. Pi loads it from <cwd>/.pi/settings.json.
    assert resolve_sessions() == (os.path.normpath(str(repo / "my-sessions")), LAYOUT_EXPLICIT_DIR)
    (repo / ".pi" / "settings.json").unlink()
    # 4. global <agent-dir>/settings.json sessionDir (absolute kept as-is) — explicit.
    monkeypatch.setenv("PI_CODING_AGENT_DIR", global_dir)
    Path(global_dir).mkdir(parents=True, exist_ok=True)
    (Path(global_dir) / "settings.json").write_text(json.dumps({"sessionDir": str(tmp_path / "global-sessions")}))
    assert resolve_sessions() == (str(tmp_path / "global-sessions"), LAYOUT_EXPLICIT_DIR)
    # 4b. a RELATIVE global sessionDir resolves against the cwd, NOT the agent dir: Pi
    #     normalizes the value (tilde/expand) but leaves it relative, and the session fs
    #     layer then resolves it against the process working directory.
    (Path(global_dir) / "settings.json").write_text(json.dumps({"sessionDir": "relative-sessions"}))
    assert resolve_sessions() == (os.path.normpath(str(repo / "relative-sessions")), LAYOUT_EXPLICIT_DIR)
    (Path(global_dir) / "settings.json").unlink()
    # 5. no explicit source: the default root (encoded-cwd children underneath).
    assert resolve_sessions() == (os.path.join(global_dir, "sessions"), LAYOUT_DEFAULT_ROOT)
    # 6. agent dir default when the env var is absent: ~/.pi/agent/sessions.
    monkeypatch.delenv("PI_CODING_AGENT_DIR")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert resolve_sessions() == (os.path.join(str(tmp_path / "home"), ".pi", "agent", "sessions"), LAYOUT_DEFAULT_ROOT)
    assert default_sessions_dir() == os.path.join(str(tmp_path / "home"), ".pi", "agent", "sessions")

def test_find_transcript_explicit_dir(tmp_path, monkeypatch):
    """An explicit session dir holds the JSONLs DIRECTLY: no encoded-cwd child is created or
    required; a session whose cwd is a repo subdirectory is still found (via its header),
    and a session belonging to another repo in the same dir is rejected."""
    repo = tmp_path / "repo"
    init_repo(repo)  # discovery anchors on the process cwd's repo root
    monkeypatch.chdir(repo)
    repo = str(repo)
    other = str(tmp_path / "other")
    os.makedirs(other)
    sessions = str(tmp_path / "sessions")
    a = Path(sessions) / "2026-07-01T09-00-00-000Z_11111111-0000-0000-0000-000000000001.jsonl"
    b = Path(sessions) / "2026-07-01T10-00-00-000Z_22222222-0000-0000-0000-000000000002.jsonl"
    s = Path(sessions) / "2026-07-01T11-00-00-000Z_33333333-0000-0000-0000-000000000003.jsonl"
    o = Path(sessions) / "2026-07-01T12-00-00-000Z_44444444-0000-0000-0000-000000000004.jsonl"
    write_session(a, [pi_header("11111111-0000-0000-0000-000000000001", "2026-07-01T09:00:00.000Z", repo),
                      pi_assistant("a1", "2026-07-01T09:01:00.000Z", "m", "p", pi_usage(1, 1))])
    write_session(b, [pi_header("22222222-0000-0000-0000-000000000002", "2026-07-01T10:00:00.000Z", repo),
                      pi_assistant("b1", "2026-07-01T10:01:00.000Z", "m", "p", pi_usage(1, 1))])
    write_session(s, [pi_header("33333333-0000-0000-0000-000000000003", "2026-07-01T11:00:00.000Z", os.path.join(repo, "sub")),
                      pi_assistant("s1", "2026-07-01T11:01:00.000Z", "m", "p", pi_usage(1, 1))])
    write_session(o, [pi_header("44444444-0000-0000-0000-000000000004", "2026-07-01T12:00:00.000Z", other),
                      pi_assistant("o1", "2026-07-01T12:01:00.000Z", "m", "p", pi_usage(1, 1))])
    set_mtime(a, 1_000_000)
    set_mtime(b, 2_000_000)
    set_mtime(s, 3_000_000)
    set_mtime(o, 4_000_000)
    monkeypatch.setenv("PI_SESSIONS_DIR", sessions)
    assert resolve_sessions() == (sessions, LAYOUT_EXPLICIT_DIR)

    # most-recent-modified wins (b is newer than a; the subdir-cwd session s is newest of all)
    assert PI.find_transcript(sessions, None, strict=True) == str(s)
    # by session id
    assert PI.find_transcript(sessions, "22222222-0000-0000-0000-000000000002", strict=True) == str(b)
    assert PI.find_transcript(sessions, "nope", strict=True) is None
    # the other repo's session is never in the discovery set
    assert all(os.path.realpath(p) != os.path.realpath(str(o)) for p in PI.session_transcripts(sessions))
    # PI_SESSION_FILE: the exact session, even when its cwd is another tree (trusted hint).
    monkeypatch.setenv("PI_SESSION_FILE", str(o))
    assert PI.find_transcript(sessions, None, strict=True) == str(o)
    monkeypatch.setenv("PI_SESSION_FILE", str(a))
    assert PI.find_transcript(sessions, None, strict=True) == str(a)
    # When Pi supplies both exact-session hints, they must agree. A stale session-file env
    # value falls back to ordinary repo discovery instead of silently misattributing it.
    monkeypatch.setenv("PI_SESSION_ID", "22222222-0000-0000-0000-000000000002")
    assert PI.find_transcript(sessions, None, strict=True) == str(s)
    monkeypatch.setenv("PI_SESSION_ID", "")
    # a caller-requested session id that disagrees with the env file also falls through.
    assert PI.find_transcript(sessions, "22222222-0000-0000-0000-000000000002", strict=True) == str(b)
    monkeypatch.delenv("PI_SESSION_FILE")
    # strict mode with no sessions at all: None, no exit
    empty = str(tmp_path / "empty")
    os.makedirs(empty)
    monkeypatch.setenv("PI_SESSIONS_DIR", empty)
    assert PI.find_transcript(empty, None, strict=True) is None

def test_find_transcript_default_layout(tmp_path, monkeypatch):
    """Default storage: files live in per-cwd ``--<encoded-cwd>--`` children under the agent
    dir's ``sessions`` root (a repo subdirectory gets its own ``--<repo>-<sub>--`` child);
    another repo's encoded dir is never scanned."""
    repo = tmp_path / "repo"
    init_repo(repo)
    monkeypatch.chdir(repo)
    repo = str(repo)
    other = str(tmp_path / "other")
    os.makedirs(other)
    agent_dir = str(tmp_path / "agent")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", agent_dir)
    sessions = os.path.join(agent_dir, "sessions")
    assert resolve_sessions() == (sessions, LAYOUT_DEFAULT_ROOT)
    assert default_sessions_dir() == sessions

    a = Path(sessions) / pi_munged_project_dir(repo) / "2026-07-01T09-00-00-000Z_11111111-0000-0000-0000-000000000001.jsonl"
    s = Path(sessions) / (pi_munged_project_dir(repo)[:-1] + "sub--") / "2026-07-01T11-00-00-000Z_33333333-0000-0000-0000-000000000003.jsonl"
    o = Path(sessions) / pi_munged_project_dir(other) / "2026-07-01T12-00-00-000Z_44444444-0000-0000-0000-000000000004.jsonl"
    write_session(a, [pi_header("11111111-0000-0000-0000-000000000001", "2026-07-01T09:00:00.000Z", repo),
                      pi_assistant("a1", "2026-07-01T09:01:00.000Z", "m", "p", pi_usage(1, 1))])
    write_session(s, [pi_header("33333333-0000-0000-0000-000000000003", "2026-07-01T11:00:00.000Z", os.path.join(repo, "sub")),
                      pi_assistant("s1", "2026-07-01T11:01:00.000Z", "m", "p", pi_usage(1, 1))])
    write_session(o, [pi_header("44444444-0000-0000-0000-000000000004", "2026-07-01T12:00:00.000Z", other),
                      pi_assistant("o1", "2026-07-01T12:01:00.000Z", "m", "p", pi_usage(1, 1))])
    set_mtime(a, 1_000_000)
    set_mtime(s, 2_000_000)
    set_mtime(o, 3_000_000)

    found = PI.session_transcripts(sessions)
    assert sorted(os.path.basename(p) for p in found) == [a.name, s.name]  # o never in the set
    # newest repo session wins (the subdir session s is newer than a)
    assert PI.find_transcript(sessions, None, strict=True) == str(s)

def test_explicit_session_dir_env_and_setting(tmp_path, monkeypatch):
    """Both remaining explicit sources put files DIRECTLY in the named dir (no encoded-cwd
    child beneath it), and a foreign-cwd file in that same dir is rejected."""
    repo = tmp_path / "repo"
    init_repo(repo)
    monkeypatch.chdir(str(repo))
    repo = str(repo)
    other = str(tmp_path / "other")
    os.makedirs(other)

    # (a) Pi's own env var: PI_CODING_AGENT_SESSION_DIR (mirrors the --session-dir flag).
    env_dir = str(tmp_path / "pi-sessions")
    monkeypatch.setenv("PI_CODING_AGENT_SESSION_DIR", env_dir)
    a = Path(env_dir) / "2026-07-01T09-00-00-000Z_11111111-0000-0000-0000-000000000001.jsonl"
    o = Path(env_dir) / "2026-07-01T12-00-00-000Z_44444444-0000-0000-0000-000000000004.jsonl"
    write_session(a, [pi_header("11111111-0000-0000-0000-000000000001", "2026-07-01T09:00:00.000Z", repo),
                      pi_assistant("a1", "2026-07-01T09:01:00.000Z", "m", "p", pi_usage(1, 1))])
    write_session(o, [pi_header("44444444-0000-0000-0000-000000000004", "2026-07-01T12:00:00.000Z", other),
                      pi_assistant("o1", "2026-07-01T12:01:00.000Z", "m", "p", pi_usage(1, 1))])
    found = PI.session_transcripts(default_sessions_dir())
    assert [os.path.basename(p) for p in found] == [a.name]  # foreign-cwd file rejected
    monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR")

    # (b) the sessionDir setting (project scope): a relative value resolves against the
    #     invocation's cwd (here the repo root, where Pi's <cwd>/.pi/settings.json lives);
    #     files are written directly in it.
    repo_p = tmp_path / "repo"
    (repo_p / ".pi").mkdir()
    (repo_p / ".pi" / "settings.json").write_text(json.dumps({"sessionDir": "my-sessions"}))
    sd = str(repo_p / "my-sessions")
    b = Path(sd) / "2026-07-01T10-00-00-000Z_22222222-0000-0000-0000-000000000002.jsonl"
    write_session(b, [pi_header("22222222-0000-0000-0000-000000000002", "2026-07-01T10:00:00.000Z", repo),
                      pi_assistant("b1", "2026-07-01T10:01:00.000Z", "m", "p", pi_usage(1, 1))])
    assert resolve_sessions() == (sd, LAYOUT_EXPLICIT_DIR)
    found = PI.session_transcripts(default_sessions_dir())
    assert [os.path.basename(p) for p in found] == [b.name]
    (repo_p / ".pi" / "settings.json").unlink()

def test_resolve_sessions_project_settings_from_subdirectory_cwd(tmp_path, monkeypatch):
    """The project settings lookup is ``<cwd>/.pi/settings.json`` — the invocation's working
    directory, NOT the git superproject root: a session started from a repo subdirectory
    whose subdirectory carries its own ``.pi/settings.json`` gets that file (winning over
    one at the repo root), with a relative ``sessionDir`` resolved against the same cwd. The
    explicit ``cwd`` argument makes the same behavior testable without chdir."""
    repo = tmp_path / "repo"
    init_repo(repo)
    sub = repo / "sub"
    sub.mkdir()
    (repo / ".pi").mkdir()
    (repo / ".pi" / "settings.json").write_text(json.dumps({"sessionDir": "root-level"}))
    (sub / ".pi").mkdir()
    (sub / ".pi" / "settings.json").write_text(json.dumps({"sessionDir": "sub-sessions"}))
    agent_dir = str(tmp_path / "agent")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", agent_dir)

    # from the subdirectory: the subdirectory's own .pi file wins, relative to the cwd:
    monkeypatch.chdir(sub)
    assert resolve_sessions() == (os.path.normpath(str(sub / "sub-sessions")), LAYOUT_EXPLICIT_DIR)
    # ...same result through the explicit cwd argument (no chdir involved):
    monkeypatch.chdir(tmp_path)
    assert resolve_sessions(cwd=str(sub)) == (os.path.normpath(str(sub / "sub-sessions")), LAYOUT_EXPLICIT_DIR)
    # from the repo root: the root-level file wins there instead:
    monkeypatch.chdir(repo)
    assert resolve_sessions() == (os.path.normpath(str(repo / "root-level")), LAYOUT_EXPLICIT_DIR)

    # and header containment in discovery is a separate anchor: invoked from the
    # subdirectory, a session whose header cwd IS the subdirectory is found under the
    # subdirectory-resolved explicit dir (the containment check still uses the repo root).
    sd = str(sub / "sub-sessions")
    os.makedirs(sd)
    a = Path(sd) / "2026-07-01T09-00-00-000Z_11111111-0000-0000-000000000001.jsonl"
    write_session(a, [pi_header("11111111-0000-0000-0000-000000000001", "2026-07-01T09:00:00.000Z", str(sub)),
                      pi_assistant("a1", "2026-07-01T09:01:00.000Z", "m", "p", pi_usage(1, 1))])
    monkeypatch.chdir(sub)
    found = PI.session_transcripts(resolve_sessions()[0])
    assert [os.path.basename(p) for p in found] == [a.name]

def test_oneoff_session_dir_not_discoverable_but_reachable(tmp_path, monkeypatch):
    """Discovery boundary: Pi can park a session in a one-off ``--session-dir`` (or an
    extension-chosen dir) that no env var, no settings file, and no default root points at —
    not reconstructible after Pi exits, so default discovery finds nothing. The supported
    escape hatches: an explicit ``--projects-dir`` (or tally's ``PI_SESSIONS_DIR``) pointed
    at the actual dir, and ``$PI_SESSION_FILE``, which bypasses discovery entirely no matter
    how Pi chose its dir."""
    repo = tmp_path / "repo"
    init_repo(repo)
    repo = str(repo)
    monkeypatch.chdir(repo)
    agent_dir = str(tmp_path / "agent")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", agent_dir)
    # no PI_SESSIONS_DIR / PI_CODING_AGENT_SESSION_DIR / sessionDir anywhere: the resolver
    # lands on the default root, which holds nothing.
    default_root, layout = resolve_sessions()
    assert layout == LAYOUT_DEFAULT_ROOT
    assert default_root == os.path.join(agent_dir, "sessions")
    oneoff = str(tmp_path / "one-off")
    os.makedirs(oneoff)
    a = Path(oneoff) / "2026-07-01T09-00-00-000Z_11111111-0000-0000-000000000001.jsonl"
    write_session(a, [pi_header("11111111-0000-0000-0000-000000000001", "2026-07-01T09:00:00.000Z", repo),
                      pi_assistant("a1", "2026-07-01T09:01:00.000Z", "m", "p", pi_usage(1, 1))])
    # 1. default discovery cannot reach the one-off dir:
    assert PI.session_transcripts(default_root) == []
    # 2. an explicit --projects-dir at the actual dir can:
    assert PI.find_transcript(oneoff, None, strict=True) == str(a)
    # 3. ...and so can tally's own PI_SESSIONS_DIR override pointed at the same dir:
    monkeypatch.setenv("PI_SESSIONS_DIR", oneoff)
    assert resolve_sessions() == (oneoff, LAYOUT_EXPLICIT_DIR)
    assert [os.path.basename(p) for p in PI.session_transcripts(default_sessions_dir())] == [a.name]
    monkeypatch.delenv("PI_SESSIONS_DIR")
    # 4. $PI_SESSION_FILE bypasses discovery entirely:
    monkeypatch.setenv("PI_SESSION_FILE", str(a))
    assert PI.find_transcript(default_root, None, strict=True) == str(a)
