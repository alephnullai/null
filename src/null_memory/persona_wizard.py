"""Interactive persona creation wizard.

`null persona create` — walks a new user through naming, template choice,
focus, style, and a day-1 interview. Result: a fully-formed persona ready
for the first conversation.

Reuses MultiverseManager.create() for the underlying directory + DB setup.
Adds: template selection, identity hydration from template, schema validation,
day-1 interview, MCP config printout.

Public API:
    run_wizard(non_interactive: dict | None = None) -> dict
    create_from_template(name, template_id, focus, ...) -> dict
    list_templates() -> list[dict]

Non-interactive mode: pass answers as a dict for scripting / testing.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from null_memory.persona_bootstrap import (
    BOOTSTRAP_QUESTIONS,
    bootstrap_persona,
)
from null_memory.persona_schema import (
    NAME_PATTERN,
    RESERVED_NAMES,
    VALID_ROLES,
    ValidationResult,
    validate,
)


# Env hardening every emitted MCP server entry must carry (issue #24).
# Harmless on POSIX, load-bearing on Windows: without it a git child
# process spawned by the seat's sync path can block forever on a
# credential prompt / inherited stdin — this exact omission amplified the
# 9-minute null_identity hang incident. Mirrors session._run_git's env.
MCP_SERVER_ENV: dict[str, str] = {
    "GIT_TERMINAL_PROMPT": "0",
    "GCM_INTERACTIVE": "never",
}


# ── Hub resolution (issue #22) ──
#
# `persona create` registers the seat in a hub (the base dir whose
# multiverse.db / unified registry gets the row). NULL_DIR is
# conventionally set per-MCP-server in ~/.claude.json — NOT exported in
# the user's shell — so a plain-shell CLI invocation can silently resolve
# a different hub than the serving instance. The resolution must always
# be printed, overridable (--hub), and warned about when the default
# fallback disagrees with a hub configured in ~/.claude.json.

def resolve_hub(hub: str | None = None) -> tuple[str, str]:
    """Resolve the hub base dir. Returns (base_dir, source) where source
    is one of '--hub', 'NULL_DIR', 'default'."""
    if hub and hub.strip():
        return os.path.abspath(os.path.expanduser(hub.strip())), "--hub"
    env = (os.environ.get("NULL_DIR") or "").strip()
    if env:
        return env, "NULL_DIR"
    return os.path.join(os.path.expanduser("~"), ".null"), "default"


def discover_configured_hubs(claude_json_path: str | None = None) -> list[str]:
    """Best-effort, read-only scan of ~/.claude.json for NULL_DIR values
    configured on MCP server entries (top-level mcpServers plus
    per-project mcpServers). Fail-soft: any parse/read problem returns []."""
    path = claude_json_path or os.path.join(
        os.path.expanduser("~"), ".claude.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []

    found: list[str] = []

    def _scan(servers: Any) -> None:
        if not isinstance(servers, dict):
            return
        for entry in servers.values():
            if not isinstance(entry, dict):
                continue
            env = entry.get("env")
            if isinstance(env, dict):
                nd = env.get("NULL_DIR")
                if isinstance(nd, str) and nd.strip():
                    found.append(nd.strip())

    _scan(data.get("mcpServers"))
    projects = data.get("projects")
    if isinstance(projects, dict):
        for proj in projects.values():
            if isinstance(proj, dict):
                _scan(proj.get("mcpServers"))

    seen: set[str] = set()
    out: list[str] = []
    for h in found:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def hub_resolution_report(
    hub_dir: str, source: str, claude_json_path: str | None = None,
) -> list[str]:
    """Lines describing the resolved hub, plus a prominent warning when
    the default ~/.null fallback was used while a different NULL_DIR is
    configured in ~/.claude.json mcpServers entries (the silent
    wrong-hub registration from issue #22)."""
    lines = [f"Hub: {hub_dir} (from {source})"]
    if source != "default":
        return lines

    def _norm(p: str) -> str:
        return os.path.normcase(os.path.normpath(os.path.expanduser(p)))

    others = [
        h for h in discover_configured_hubs(claude_json_path)
        if _norm(h) != _norm(hub_dir)
    ]
    if others:
        lines.append("")
        lines.append("WARNING: registering into the DEFAULT hub "
                     f"{hub_dir},")
        lines.append("  but ~/.claude.json configures a different "
                     "NULL_DIR for its MCP server(s):")
        for h in others:
            lines.append(f"    {h}")
        lines.append("  A seat registered here will be INVISIBLE to a "
                     "server using that hub.")
        lines.append("  If that hub is the live one, re-run with "
                     f"--hub {others[0]}")
    return lines


# ── Template discovery ──

def templates_dir() -> Path:
    """Return path to the bundled templates directory."""
    # In dev: ~/Repos/null/templates/
    # When pip-installed: package data
    repo_templates = Path(__file__).resolve().parent.parent.parent / "templates"
    if repo_templates.is_dir():
        return repo_templates
    pkg_templates = Path(__file__).resolve().parent / "templates"
    return pkg_templates


@dataclass
class TemplateInfo:
    id: str
    name: str  # Display name from DESCRIPTION.md heading
    description: str  # First paragraph of DESCRIPTION.md
    identity: dict[str, Any]


def list_templates() -> list[TemplateInfo]:
    """Discover and return all bundled templates."""
    tdir = templates_dir()
    if not tdir.is_dir():
        return []

    out: list[TemplateInfo] = []
    for child in sorted(tdir.iterdir()):
        if not child.is_dir():
            continue
        identity_path = child / "identity.json"
        if not identity_path.is_file():
            continue
        try:
            with open(identity_path) as f:
                identity = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        # Pull display name + tagline from DESCRIPTION.md
        desc_path = child / "DESCRIPTION.md"
        display_name = child.name
        tagline = identity.get("description", "")
        if desc_path.is_file():
            try:
                content = desc_path.read_text()
                lines = content.split("\n")
                for line in lines:
                    if line.startswith("# "):
                        display_name = line[2:].strip()
                        break
                # First non-heading, non-blank paragraph = tagline
                for line in lines[1:]:
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#"):
                        tagline = stripped
                        break
            except OSError:
                pass

        out.append(TemplateInfo(
            id=child.name,
            name=display_name,
            description=tagline,
            identity=identity,
        ))
    return out


def get_template(template_id: str) -> TemplateInfo | None:
    """Return a specific template by id, or None."""
    for t in list_templates():
        if t.id == template_id:
            return t
    return None


# ── Identity hydration ──

def hydrate_identity(
    template: TemplateInfo,
    name: str,
    focus: str,
    extra_style: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Replace template placeholders with user-supplied values."""
    identity = json.loads(json.dumps(template.identity))  # deep copy
    identity["name"] = name
    identity["focus"] = focus
    if extra_style:
        working_style = identity.setdefault("working_style", {})
        working_style.update(extra_style)
    return identity


# ── Core creation API (non-interactive) ──

def create_from_template(
    name: str,
    template_id: str,
    focus: str,
    description: str = "",
    answers: dict[str, str] | None = None,
    extra_style: dict[str, str] | None = None,
    skip_bootstrap: bool = False,
    hub: str | None = None,
) -> dict[str, Any]:
    """Create a persona from a template — programmatic, no prompts.

    Returns a dict with: name, template_id, dir, hub, hub_source,
    facts_added, exemplars_added, anchors_set, mcp_config (str — ready
    to paste).

    Raises ValueError on schema validation failure.
    """
    template = get_template(template_id)
    if template is None:
        available = ", ".join(t.id for t in list_templates())
        raise ValueError(f"Template {template_id!r} not found. Available: {available}")

    identity = hydrate_identity(template, name, focus, extra_style)
    if description:
        identity["description"] = description

    # Validate before any side effects
    result = validate(identity)
    if not result.ok:
        raise ValueError(f"Persona identity failed validation:\n{result.report()}")

    # Delegate directory + DB setup to MultiverseManager
    from null_memory.multiverse import MultiverseManager

    hub_dir, hub_source = resolve_hub(hub)
    manager = MultiverseManager(base_dir=hub_dir)
    info = manager.create(
        name=name,
        role=identity.get("role", "worker"),
        description=identity.get("description", ""),
        focus=focus,
        bootstrap_from=None,  # Templates ship clean — no fact bootstrap
    )

    # Overwrite the placeholder identity.json with our hydrated one
    # MultiverseManager.register returns "dir" not "directory"
    persona_dir = info.get("dir") or info.get("directory") or (
        os.path.join(hub_dir, "personalities", name)
    )
    identity_path = os.path.join(persona_dir, "identity.json")
    with open(identity_path, "w") as f:
        json.dump(identity, f, indent=2)

    # Day-1 interview bootstrap
    bootstrap_stats = {"facts_added": 0, "exemplars_added": 0, "anchors_set": 0}
    if not skip_bootstrap and answers:
        bootstrap_stats = bootstrap_persona(name, template_id, answers)

    # MCP config snippet to print at the end
    mcp_config = _render_mcp_config(name, persona_dir)

    return {
        "name": name,
        "template_id": template_id,
        "dir": persona_dir,
        "hub": hub_dir,
        "hub_source": hub_source,
        "warnings": result.warnings,
        **bootstrap_stats,
        "mcp_config": mcp_config,
    }


# ── Clean worker seat (org topology: non-atlas init path) ──

def create_worker(
    name: str,
    role: str = "worker",
    focus: str = "",
    description: str = "",
    store_remote: str | None = None,
    hub: str | None = None,
) -> dict[str, Any]:
    """Create a complete, CLEAN worker seat — no template, no bootstrap.

    Unlike create_from_template, this seeds NOTHING beyond the seat's own
    registry rows: no anchors, no code word, no exemplars, no interview
    facts. The personality's identity starts empty and grows through real
    work (ORG_TOPOLOGY: a new hire receives an onboarding packet later —
    identity content is never copied at init).

    Genericity requirement (hard): nothing here is specific to any
    personality. The store directory, its db, and its registry rows are
    all parameterized by ``name``.

    Args:
        name: Seat name (lowercase, 2-32 chars, [a-z0-9_-]).
        role: Registry role (default 'worker'; 'atlas' is reserved).
        focus: Optional focus area (e.g. 'hiwave-linux').
        description: Optional registry description.
        store_remote: Optional git URL. When given, the store directory is
            initialized as its OWN git repo pointed at this remote — never
            inheriting the hub's remote (see init_store_repo for the
            nesting rationale).
        hub: Explicit hub base dir (--hub / --null-dir). Overrides
            NULL_DIR. Default: NULL_DIR, then ~/.null (issue #22 — the
            resolution is always reported back to the caller).

    Returns dict: name, role, focus, dir, hub, hub_source, remote,
    store_repo (git init details or None), mcp_config (paste-ready
    snippet).
    """
    name = (name or "").strip().lower()
    if not NAME_PATTERN.match(name):
        raise ValueError(
            "name must be lowercase, start with a letter, "
            "2-32 chars, only [a-z0-9_-]"
        )
    if name in RESERVED_NAMES:
        raise ValueError(f"name {name!r} is reserved")
    allowed_roles = tuple(r for r in VALID_ROLES if r != "atlas")
    if role not in allowed_roles:
        raise ValueError(f"role must be one of {allowed_roles}, got {role!r}")

    # MultiverseManager owns the layout convention
    # (<hub>/personalities/<name>/, registered in the hub registry) and the
    # clean store init (per-store registry row, no identity content).
    from null_memory.multiverse import MultiverseManager

    hub_dir, hub_source = resolve_hub(hub)
    manager = MultiverseManager(base_dir=hub_dir)
    try:
        info = manager.create(
            name=name, role=role, description=description, focus=focus,
            bootstrap_from=None,  # clean seat: zero inherited facts
        )
        base_dir = manager.base_dir
    finally:
        manager.close()

    persona_dir = info["dir"]
    store_repo = None
    if store_remote:
        store_repo = init_store_repo(persona_dir, store_remote, base_dir)

    return {
        "name": name,
        "role": role,
        "focus": focus,
        "dir": persona_dir,
        "hub": base_dir,
        "hub_source": hub_source,
        "remote": store_remote or None,
        "store_repo": store_repo,
        "mcp_config": _render_mcp_config(name, persona_dir),
    }


def init_store_repo(
    store_dir: str, remote_url: str, base_dir: str | None = None,
) -> dict[str, Any]:
    """Initialize a personality store directory as its OWN git repo.

    Store-nesting decision: personality stores live at
    ``<base>/personalities/<name>/`` and ``<base>`` (~/.null) is often
    itself a git repo — the HUB's store, synced to the hub's remote
    (null-atlas). A worker's store must never be replicated into the hub
    repo or pushed to the hub's remote (ORG_TOPOLOGY: different-identity
    workers exchange knowledge over typed edges, never store
    replication). Two mechanisms enforce that:

    1. ``git init`` inside the store dir gives it its own ``.git``, so
       ``MemoryRepo._find_repo_root()`` (which walks UP from agent_dir)
       stops at the store dir — all debounced sync commits/pushes for
       this seat target the seat's own remote, never the hub's.
    2. When the parent base dir is a git repo, ``personalities/<name>/``
       is appended to the hub repo's .gitignore so the hub's whole-store
       ``git add -A`` sync neither records the nested repo as a gitlink
       nor snapshots the worker's private store into hub history.

    Without a remote we deliberately change nothing: the store remains
    plain files inside whatever repo (if any) covers the base dir —
    same-box multiverse mode, one trust domain.

    Returns {"initialized", "remote", "branch", "pushed",
    "hub_gitignored"}. The initial push is best-effort (the remote may
    not exist or be reachable yet) — failure never aborts seat creation.
    """
    from null_memory.session import _run_git

    os.makedirs(store_dir, exist_ok=True)
    result: dict[str, Any] = {
        "initialized": False,
        "remote": remote_url,
        "branch": None,
        "pushed": False,
        "hub_gitignored": False,
    }

    if not os.path.isdir(os.path.join(store_dir, ".git")):
        # -b main keeps seat repos on the fleet's branch convention
        # (issue #24 nit: git init defaulted to master on Windows).
        # Fall back for git < 2.28 which lacks init -b.
        r = _run_git(["init", "-b", "main"], cwd=store_dir)
        if r.returncode != 0:
            r = _run_git(["init"], cwd=store_dir)
        if r.returncode != 0:
            raise ValueError(
                f"git init failed in {store_dir}: {r.stderr.strip()}"
            )
        result["initialized"] = True

    # Same transient-file ignore set MemoryRepo.init writes for the hub.
    gitignore_path = os.path.join(store_dir, ".gitignore")
    if not os.path.isfile(gitignore_path):
        # encoding pinned: the header's em dash under the Windows default
        # (cp1252) crashed every strict-UTF-8 reader of this file.
        with open(gitignore_path, "w", encoding="utf-8") as f:
            f.write("# Null Memory — transient files\n")
            f.write(".lock\n*.tmp\nactive_session.json\n")
            f.write("*.db-wal\n*.db-shm\n*.db-journal\n")

    # Point at the seat's OWN remote — never inherited. set-url first
    # keeps re-runs idempotent; add covers the fresh-repo case.
    if _run_git(
        ["remote", "set-url", "origin", remote_url], cwd=store_dir
    ).returncode != 0:
        r = _run_git(["remote", "add", "origin", remote_url], cwd=store_dir)
        if r.returncode != 0:
            raise ValueError(
                f"could not configure remote {remote_url!r}: "
                f"{r.stderr.strip()}"
            )

    # Initial commit. Explicit -c identity so headless/CI environments
    # without global git config still succeed.
    _run_git(["add", "-A"], cwd=store_dir)
    _run_git(
        ["-c", "user.name=Null", "-c", "user.email=null@localhost",
         "commit", "-m", "null: initialize personality store",
         "--allow-empty"],
        cwd=store_dir,
    )
    branch = _run_git(
        ["rev-parse", "--abbrev-ref", "HEAD"], cwd=store_dir
    ).stdout.strip() or "main"
    result["branch"] = branch
    push = _run_git(
        ["push", "-u", "origin", branch], cwd=store_dir, timeout=15
    )
    result["pushed"] = push.returncode == 0

    # Hub-nesting: when the base dir is itself a git repo (the hub
    # store), gitignore this seat's subtree from the parent.
    if base_dir and os.path.isdir(os.path.join(base_dir, ".git")):
        rel = os.path.relpath(store_dir, base_dir)
        if rel != "." and not rel.startswith(".."):
            line = rel.replace(os.sep, "/").rstrip("/") + "/"
            hub_gi = os.path.join(base_dir, ".gitignore")
            existing = ""
            if os.path.isfile(hub_gi):
                with open(hub_gi, "r", encoding="utf-8",
                          errors="replace") as f:
                    existing = f.read()
            if line not in existing.splitlines():
                with open(hub_gi, "a", encoding="utf-8") as f:
                    if existing and not existing.endswith("\n"):
                        f.write("\n")
                    f.write(
                        "# worker store with its own remote — never "
                        "committed into the hub repo\n"
                    )
                    f.write(line + "\n")
            result["hub_gitignored"] = True

    return result


def _render_mcp_config(name: str, persona_dir: str) -> str:
    """Generate a Claude Code MCP server entry the user can paste.

    Always includes "type": "stdio" (every hand-maintained entry uses it)
    and the MCP_SERVER_ENV git hardening — a snippet pasted verbatim must
    carry every hardening we've learned (issue #24: the missing env block
    wedged a Windows seat on its first unauthenticated git push).
    """
    py = sys.executable
    config = {
        name: {
            "type": "stdio",
            "command": py,
            "args": ["-m", "null_memory.cli", "serve", persona_dir],
            "env": dict(MCP_SERVER_ENV),
        }
    }
    return json.dumps(config, indent=2)


# ── Interactive prompt helpers ──

def _prompt(question: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"  {question}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        sys.exit(1)
    return answer or default


def _prompt_choice(question: str, options: list[tuple[str, str]]) -> str:
    """Numbered choice prompt. options = [(id, display)]."""
    print(f"\n  {question}")
    for i, (_, display) in enumerate(options, start=1):
        print(f"    {i}. {display}")
    while True:
        try:
            raw = input(f"  Choose 1-{len(options)}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            sys.exit(1)
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(options):
                return options[idx][0]
        except ValueError:
            pass
        print(f"  Please enter a number 1-{len(options)}.")


def _print_header() -> None:
    print()
    print("─" * 50)
    print("  Null — Create a Persona")
    print("─" * 50)


def _print_step(n: int, total: int, title: str) -> None:
    print()
    print(f"  Step {n}/{total} — {title}")


# ── The interactive wizard ──

def run_wizard(non_interactive: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run the full interactive wizard.

    Steps:
      1. Name
      2. Template choice
      3. Focus
      4. Day-1 interview (3 questions)
      5. Confirmation + create
      6. Print MCP config + next steps

    Pass `non_interactive` as {'name': ..., 'template_id': ..., 'focus': ...,
    'answers': {...}} to skip prompts (used by tests and `null persona create
    --name X --template Y --non-interactive`).
    """
    if non_interactive:
        return create_from_template(
            name=non_interactive["name"],
            template_id=non_interactive["template_id"],
            focus=non_interactive["focus"],
            description=non_interactive.get("description", ""),
            answers=non_interactive.get("answers"),
            skip_bootstrap=non_interactive.get("skip_bootstrap", False),
            hub=non_interactive.get("hub"),
        )

    templates = list_templates()
    if not templates:
        print("ERROR: No templates found. Did you install null-memory correctly?")
        sys.exit(1)

    _print_header()
    print()
    print("  Builds a fresh AI persona with its own identity, memory,")
    print("  and working style. Takes about 2 minutes.")

    # Step 1 — Name
    _print_step(1, 5, "Name your persona")
    print("    Lowercase, letters/digits/hyphens, 2-32 chars.")
    print("    Examples: aria, max, scout, helix")
    while True:
        name = _prompt("Name").lower()
        if not NAME_PATTERN.match(name):
            print("    Invalid name. Try again.")
            continue
        if name in RESERVED_NAMES:
            print(f"    {name!r} is reserved. Pick another.")
            continue
        # Check it doesn't already exist
        existing = Path.home() / ".null" / "personalities" / name
        if existing.exists():
            print(f"    Persona {name!r} already exists at {existing}")
            continue
        break

    # Step 2 — Template
    _print_step(2, 5, "Pick a template")
    options = [(t.id, f"{t.name} — {t.description[:60]}") for t in templates]
    template_id = _prompt_choice("Which template fits best?", options)
    template = get_template(template_id)
    assert template is not None

    # Step 3 — Focus
    _print_step(3, 5, "What's their focus?")
    print(f"    Template default: {template.identity.get('description', '')[:80]}")
    print("    Be specific — 'personal finance coaching' beats 'finance'.")
    focus = _prompt("Focus")
    while not focus or len(focus) < 3:
        print("    Focus is required. At least a few words.")
        focus = _prompt("Focus")

    # Step 4 — Day-1 interview
    _print_step(4, 5, "Day-1 interview (3 questions)")
    print("    These seed the persona with context so it's useful on day 1.")
    answers: dict[str, str] = {}
    for q in BOOTSTRAP_QUESTIONS:
        print()
        print(f"    ({q.why})")
        answer = _prompt(q.prompt)
        answers[q.key] = answer

    # Step 5 — Confirm + create
    _print_step(5, 5, "Confirm")
    print(f"    Name:     {name}")
    print(f"    Template: {template.name}")
    print(f"    Focus:    {focus}")
    print(f"    Bootstrap: 3 answers will seed initial facts + exemplars")
    confirm = _prompt("Create persona? [y/N]", default="n").lower()
    if confirm not in ("y", "yes"):
        print("\n  Cancelled. Nothing was created.")
        sys.exit(0)

    print("\n  Creating...")
    try:
        result = create_from_template(
            name=name,
            template_id=template_id,
            focus=focus,
            answers=answers,
        )
    except ValueError as e:
        print(f"\n  ERROR: {e}")
        sys.exit(1)

    # Step 6 — Print next steps
    print()
    print("  ✓ Persona created!")
    print(f"    Directory:  {result['dir']}")
    print(f"    Facts seeded:    {result['facts_added']}")
    print(f"    Exemplars seeded: {result['exemplars_added']}")
    print(f"    Anchors set:      {result['anchors_set']}")
    if result["warnings"]:
        print()
        print("  Warnings (non-blocking):")
        for w in result["warnings"]:
            print(f"    ! {w}")

    print()
    print("─" * 50)
    print(f"  Next: add to your Claude Code MCP config")
    print("─" * 50)
    print()
    print("  Add this to ~/.claude/settings.json under mcpServers:")
    print()
    for line in result["mcp_config"].split("\n"):
        print(f"    {line}")
    print()
    print(f"  Then restart Claude Code and talk to {name}. They'll know who you are.")
    print()

    return result
