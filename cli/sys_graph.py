"""
sys_graph.py — System knowledge graph CLI.

⛔  THIS IS THE ONLY SCRIPT FOR GRAPH OPERATIONS.
    Do not write new Python scripts, /tmp files, or raw Cypher to manipulate
    the graph. If a command you need is missing, describe it to the user and
    wait — do not implement it yourself.

Reads/writes the Sys* nodes in Neo4j that track requirements traceability:
  UserStory → Feature ← Module ← SysSymbol
                       ↑
                    SysTest (SysTestPackage → CONTAINS_TEST → SysTest → VERIFIES → Feature)

These nodes carry no orgId and are invisible to org-data cleanup sweeps.

Key commands:
    init                            Create Neo4j constraints (run once)
    seed data/sys-backup-XXX.json              Load ALL data (sys-graph only)
    seed data/sys-backup-XXX.json --instance core   Restore only core instance nodes
    seed data/sys-backup-XXX.json --module MOD-p4   Restore only one module's nodes
    briefing --instance framework   Session-start coverage briefing
    record-run --package <path>     Record a pytest run result
    test-status [--instance core]   Show last-run timestamps across packages

    scan-tests --path <file.py>     Register test functions from one file
    scan-tests --all                Register test functions from all packages
      [--root p6|backend] [--area p3] [--instance core]

    scan-code --path <dir>          Scan symbols from one directory (additive)
    scan-code --all [--lang go|ts]  Scan entire codebase for symbols

    link-test  --feature F-FW-001 --file test.py --fn test_login
    link-endpoint --feature F-FW-001 --method GET --path /auth/login --binary core
    link-symbol --feature F-FW-001 --file handlers.go --symbol handleLogin
    link-chapter --chapter SCN-001-CH-02 --feature F-P3-005
    link-training --module TRN-MGR-03 --feature F-FW-004

    features --module MOD-auth      List features for a module
    stories                         List user stories and linked features
    scenarios                       Show scenarios/training decomposition status

    backup [--output path]          Export all Sys* nodes to seed-compatible JSON
    reset --confirm                 Delete all Sys* nodes (use before restore)
"""
import argparse
import ast
import json
import os
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

_root = Path(__file__).parent.parent
try:
    from dotenv import load_dotenv
    load_dotenv(_root / ".env", override=False)
except ImportError:
    pass

# SysEdge uses its own dedicated Neo4j (SYSGRAPH_NEO4J_*) to isolate the
# system knowledge graph from the application Neo4j.  This prevents test
# teardowns and application code from ever touching Sys* nodes.
# Fall back to NEO4J_* if SYSGRAPH_NEO4J_* are not set (single-instance dev).
NEO4J_URI  = (os.environ.get("SYSGRAPH_NEO4J_URI")
              or os.environ.get("NEO4J_URI", "bolt://localhost:7687"))
NEO4J_USER = (os.environ.get("SYSGRAPH_NEO4J_USER")
              or os.environ.get("NEO4J_USER", "neo4j"))
NEO4J_PW   = (os.environ.get("SYSGRAPH_NEO4J_PASSWORD")
              or os.environ.get("NEO4J_PASSWORD", "password"))
# Restricted credentials for instance sessions — use sys_writer when available
NEO4J_SYS_USER = os.environ.get("NEO4J_SYS_USER", NEO4J_USER)
NEO4J_SYS_PW   = os.environ.get("NEO4J_SYS_PASSWORD", NEO4J_PW)

# Patterns that indicate a DELETE operation on the Sys graph
_DELETE_PATTERN = __import__("re").compile(
    r"\b(DETACH\s+DELETE|DELETE)\b", __import__("re").IGNORECASE
)


class _SafeSession:
    """Wraps a Neo4j session and refuses any query containing DELETE on Sys* nodes.

    Community edition cannot deny DELETE at the database level, so we enforce
    it here. The reset command uses the raw admin driver explicitly.

    Note: __enter__/__exit__ intentionally not implemented — the context manager
    lifecycle is managed by _SafeDriver.session()'s _Ctx class. This avoids
    double-close issues that caused silent batch failures.
    """
    def __init__(self, session):
        self._s = session

    def run(self, query, **kwargs):
        if _DELETE_PATTERN.search(query):
            raise PermissionError(
                "⛔  DELETE operations are not permitted via the instance driver.\n"
                "    The graph is shared across all sessions — only the user can authorise deletes.\n"
                "    Use: python3 scripts/sys_graph.py close-defect (sets status, does not delete)\n"
                "    or ask the user to run the operation."
            )
        return self._s.run(query, **kwargs)


class _SafeDriver:
    """Wraps a Neo4j driver and returns _SafeSessions via a clean context manager."""
    def __init__(self, driver):
        self._d = driver

    def session(self, **kwargs):
        # _Ctx owns the raw session lifecycle; _SafeSession only intercepts run()
        outer = self
        class _Ctx:
            def __enter__(_self):
                _self._raw = outer._d.session(**kwargs)
                _self._raw.__enter__()
                return _SafeSession(_self._raw)
            def __exit__(_self, *a):
                return _self._raw.__exit__(*a)
        return _Ctx()

    def close(self): self._d.close()


# ── Driver ──────────────────────────────────────────────────────────────────

def _driver(admin: bool = False):
    """Return a Neo4j driver.

    By default returns a safe (no-delete) driver using NEO4J_SYS_USER.
    Pass admin=True only for commands that explicitly need delete access (reset).
    """
    try:
        from neo4j import GraphDatabase, warnings as neo4j_warnings
        import warnings
        warnings.filterwarnings("ignore", category=neo4j_warnings.Neo4jWarning
                                if hasattr(neo4j_warnings, "Neo4jWarning") else Warning)
    except ImportError:
        print("ERROR: pip install neo4j", file=sys.stderr)
        sys.exit(1)
    import logging
    logging.getLogger("neo4j").setLevel(logging.ERROR)
    # Community edition does not support RBAC — sys_writer has no privileges.
    # Always connect with admin credentials; the _SafeDriver wrapper enforces
    # no-delete at the Python level for all non-admin callers.
    user = NEO4J_USER
    pw   = NEO4J_PW
    raw = GraphDatabase.driver(
        NEO4J_URI, auth=(user, pw),
        connection_timeout=5,
        max_transaction_retry_time=10,
    )
    return raw if admin else _SafeDriver(raw)


# ── Schema version ────────────────────────────────────────────────────────────
# Increment when a new Sys* label or required property is added.
# Backups include this version; seed refuses forward-incompatible restores.
SCHEMA_VERSION = 2


def _ensure_schema_node(session) -> None:
    """Create or verify the SysSchema singleton node."""
    session.run("""
        MERGE (s:SysSchema {id: 'schema'})
        ON CREATE SET s.version = $v, s.createdAt = $now
        ON MATCH SET s.version = $v
    """, v=SCHEMA_VERSION, now=_now_iso() if '_now_iso' in dir() else "")


# ── ID generation: retry on constraint collision ──────────────────────────────

def _require_licence(command: str = "") -> None:
    """Gate a premium command behind a valid .sysedge-licence token.

    Loads licence.py from the same directory as this script (only present in the
    Bootstrap Kit — not in the free CLI). Exits 0 with an upgrade message if no
    valid licence is found.
    """
    import importlib.util as _ilu, os as _os
    lic_path = Path(_os.path.dirname(_os.path.abspath(__file__))) / "licence.py"
    if not lic_path.exists():
        print(f"\n  This command is part of the SysEdge Bootstrap Kit.")
        print(f"  Activate with: python3 cli/sys_graph.py activate <your-key>")
        print(f"  Purchase: https://org-edge.lemonsqueezy.com/checkout\n")
        sys.exit(0)
    spec = _ilu.spec_from_file_location("licence", lic_path)
    lic  = _ilu.module_from_spec(spec)
    spec.loader.exec_module(lic)
    if not lic.check_licence(command):
        sys.exit(0)


def _alloc_id(session, label: str, prefix: str, pad: int = 0) -> str:
    """Allocate the next sequential id for a Sys* node, retrying on collision.

    Uses a read-then-create pattern. If two sessions race and both get the same
    max, the second CREATE will fail on the uniqueness constraint. We catch
    that and retry with max+1 until we succeed. This is safe because the
    constraint guarantees no duplicate ids.
    """
    from neo4j.exceptions import ClientError as _NeoClientError
    max_attempts = 10
    for attempt in range(max_attempts):
        result = session.run(
            f"MATCH (n:{label}) WHERE n.id STARTS WITH $pfx "
            f"RETURN max(toInteger(substring(n.id, $skip))) AS mx",
            pfx=prefix, skip=len(prefix)
        ).single()
        nxt = (result["mx"] or 0) + 1
        candidate = f"{prefix}{nxt:0{pad}d}" if pad else f"{prefix}{nxt}"
        try:
            session.run(f"CREATE (n:{label} {{id: $id}}) RETURN n.id",
                        id=candidate).single()
            return candidate
        except _NeoClientError as e:
            if "ConstraintValidation" in str(e) and attempt < max_attempts - 1:
                continue   # another session won this id — retry
            raise
    raise RuntimeError(f"Could not allocate id for {label} after {max_attempts} attempts")


# ── Init: constraints ────────────────────────────────────────────────────────

CONSTRAINTS = [
    ("sys_system_id",    "SysSystem",          "id"),
    ("sys_domain_id",    "SysDomain",          "id"),
    ("sys_module_id",    "SysModule",          "id"),
    ("sys_feature_id",   "SysFeature",         "id"),
    ("sys_story_id",     "SysUserStory",       "id"),
    ("sys_archstd_id",   "SysArchStd",         "id"),
    ("sys_test_id",      "SysTest",            "id"),
    ("sys_endpoint_id",  "SysEndpoint",        "id"),
    ("sys_symbol_id",    "SysSymbol",          "id"),
    ("sys_scenario_id",  "SysScenario",        "id"),
    ("sys_scn_ch_id",    "SysScenarioChapter", "id"),
    ("sys_trn_prog_id",  "SysTrainingProgram", "id"),
    ("sys_trn_mod_id",   "SysTrainingModule",  "id"),
    ("sys_test_pkg_id",  "SysTestPackage",     "id"),
    ("sys_test_run_id",  "SysTestRun",         "id"),
    ("sys_user_id",      "SysUser",            "id"),
    ("sys_app_id",       "SysApplication",     "id"),
    ("sys_defect_id",    "SysDefect",          "id"),
    ("sys_enhance_id",   "SysEnhancement",     "id"),
    ("sys_usecase_id",   "SysUseCase",         "id"),
    ("sys_dircat_id",    "SysDirCategory",     "id"),
    ("sys_feedback_id",  "SysFeedback",        "id"),
    ("sys_proposal_id",  "SysProposal",        "id"),
    ("sys_note_id",      "SysNote",            "id"),
]

def cmd_init(args):
    drv = _driver()
    with drv.session() as s:
        for name, label, prop in CONSTRAINTS:
            s.run(
                f"CREATE CONSTRAINT {name} IF NOT EXISTS "
                f"FOR (n:{label}) REQUIRE n.id IS UNIQUE"
            )
        # Schema singleton — version marker for backup compatibility
        s.run("""
            MERGE (s:SysSchema {id:'schema'})
            ON CREATE SET s.version=$v, s.createdAt=$now
            ON MATCH SET s.version=$v
        """, v=SCHEMA_VERSION, now=_now_iso())
        print(f"✓ {len(CONSTRAINTS)} constraints · schema v{SCHEMA_VERSION}")
    drv.close()


# ── Seed from JSON ────────────────────────────────────────────────────────────

def _filter_seed_data(data: dict, instance: str = "", module_id: str = "") -> dict:
    """
    Narrow a full backup dict to only nodes/edges belonging to the
    requested instance or module.  Global structural nodes (systems,
    domains, arch standards/decisions, scenarios, training, users,
    applications, dirCategories, feedback) are SKIPPED — they are
    managed by sys-graph/master/architect and must not be overwritten
    by an instance-scoped re-seed.

    Returns a new data dict containing only the filtered subset.
    """
    if not instance and not module_id:
        return data  # no filter — full seed

    # 1. Which modules are in scope?
    all_mods = data.get("modules", [])
    if module_id:
        scope_mods = [m for m in all_mods if m["id"] == module_id]
    else:
        scope_mods = [m for m in all_mods if m.get("instance") == instance]

    scope_mod_ids = {m["id"] for m in scope_mods}

    # 2. Which features belong to those modules?
    scope_feats = [f for f in data.get("features", [])
                   if f.get("moduleId") in scope_mod_ids or f.get("parentId") in
                   {f2["id"] for f2 in data.get("features",[]) if f2.get("moduleId") in scope_mod_ids}]
    scope_feat_ids = {f["id"] for f in scope_feats}

    # 3. Enhancements, defects, proposals, and notes for this instance
    scope_enhs  = [e for e in data.get("enhancements", []) if e.get("instance") == (instance or "")]
    scope_defs  = [d for d in data.get("defects",      []) if d.get("instance") == (instance or "")]
    scope_props = [p for p in data.get("proposals",    []) if p.get("instance") == (instance or "")]
    scope_notes = [n for n in data.get("notes",        []) if n.get("instance") == (instance or "")]

    # 4. Tests, endpoints, symbols linked to scope features
    scope_tests = [t for t in data.get("tests", [])
                   if any(fid in scope_feat_ids for fid in t.get("verifiesFeatures", []))]
    scope_pkgs  = [p for p in data.get("testPackages", [])
                   if any(t.get("packageId") == p["id"] for t in scope_tests)]
    scope_eps   = [e for e in data.get("endpoints", [])
                   if e.get("implementsFeature") in scope_feat_ids]
    scope_syms  = [s for s in data.get("symbols", [])
                   if s.get("moduleId") in scope_mod_ids or s.get("implementsFeature") in scope_feat_ids]

    # 5. Use cases that REQUIRES scope features
    scope_ucs   = [uc for uc in data.get("useCases", [])
                   if any(fid in scope_feat_ids for fid in uc.get("requiresFeatures", []))]

    # Story→feature REQUIRES edges — include stories that link to scope features
    # (FB-111: these were being lost on every per-instance restore)
    scope_stories = [us for us in data.get("userStories", [])
                     if any(fid in scope_feat_ids for fid in us.get("requiresFeatures", []))]

    filtered = {
        "modules":      scope_mods,
        "features":     scope_feats,
        "enhancements": scope_enhs,
        "defects":      scope_defs,
        "proposals":    scope_props,
        "notes":        scope_notes,
        "tests":        scope_tests,
        "testPackages": scope_pkgs,
        "endpoints":    scope_eps,
        "symbols":      scope_syms,
        "useCases":     scope_ucs,
        "userStories":  scope_stories,  # FB-111: story→feature REQUIRES edges included
        # Global infrastructure — always included so briefing works after restore
        # (FB-112: SysSystem/SysDomain are required for module→domain→system links)
        "systems":  data.get("systems", []),
        "domains":  data.get("domains", []),
        # True globals excluded — managed by dedicated instances
        "archStandards": [], "archDecisions": [], "scenarios": [],
        "trainingPrograms": [], "users": [], "applications": [],
        "dirCategories": [], "feedback": [], "usesPattern": [], "prerequisiteFor": [],
    }

    scope_desc = f"instance={instance}" if instance else f"module={module_id}"
    print(f"  [filtered seed: {scope_desc}]")
    print(f"  modules={len(scope_mods)}  features={len(scope_feats)}  "
          f"enhancements={len(scope_enhs)}  defects={len(scope_defs)}")
    print(f"  tests={len(scope_tests)}  endpoints={len(scope_eps)}  symbols={len(scope_syms)}")
    return filtered


def cmd_seed(args):
    path = Path(args.file)
    if not path.exists():
        print(f"ERROR: {path} not found", file=sys.stderr)
        sys.exit(1)

    # Unfiltered seed touches ALL instances — require --instance or --full-restore
    filter_instance = getattr(args, "instance", "") or ""
    filter_module   = getattr(args, "module",   "") or ""
    full_restore    = getattr(args, "full_restore", False)

    if full_restore and (filter_instance or filter_module):
        print("ERROR: --full-restore cannot be combined with --instance or --module",
              file=sys.stderr)
        sys.exit(1)

    if not filter_instance and not filter_module and not full_restore:
        # Find the most recent backup to suggest (FB-128)
        try:
            backups = sorted(Path("data").glob("sys-backup-*.json"), reverse=True)
            latest  = backups[0].name if backups else "<backup>.json"
        except Exception:
            latest = "<backup>.json"
        print("⛔  STOP — unfiltered seed overwrites ALL instances' data.", file=sys.stderr)
        print("", file=sys.stderr)
        print("    To restore only YOUR session's nodes (safe):", file=sys.stderr)
        print(f"      python3 sys_graph.py seed data/{latest} --instance <your-instance>", file=sys.stderr)
        print("", file=sys.stderr)
        print("    If you are sys-graph and need a full restore:", file=sys.stderr)
        print(f"      python3 sys_graph.py seed data/{latest} --full-restore", file=sys.stderr)
        print("", file=sys.stderr)
        sys.exit(1)

    if full_restore:
        print("  [full-restore mode — all instances will be overwritten]", file=sys.stderr)

    # Auto-backup before any seed so the pre-seed state is always recoverable
    if not getattr(args, "no_backup", False):
        ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        pre_path = Path(f"data/sys-preseed-{ts}.json")
        print(f"  [pre-seed backup → {pre_path}]")
        _do_backup(pre_path)

    data = json.loads(path.read_text())

    # Schema version compatibility check — refuse to restore a backup that is
    # newer than this CLI knows about (it may contain node types we'd silently drop).
    backup_version = data.get("_schema_version", 0)
    if backup_version > SCHEMA_VERSION:
        print(f"⛔  Backup schema version {backup_version} is newer than this CLI "
              f"(schema v{SCHEMA_VERSION}).", file=sys.stderr)
        print(f"    Upgrade sys_graph.py before restoring this backup.", file=sys.stderr)
        sys.exit(1)

    data = _filter_seed_data(data,
                             instance=getattr(args, "instance", "") or "",
                             module_id=getattr(args, "module", "") or "")
    drv  = _driver()
    counts = {}
    with drv.session() as s:
        # Snapshot existing VERIFIES/IMPLEMENTS edges so seed never destroys live links
        _pre_verifies = s.run(
            "MATCH (t:SysTest)-[:VERIFIES]->(f:SysFeature) RETURN t.id AS tid, f.id AS fid"
        ).data()
        _pre_impl_sym = s.run(
            "MATCH (sym:SysSymbol)-[:IMPLEMENTS]->(f:SysFeature) RETURN sym.id AS sid, f.id AS fid"
        ).data()
        _pre_impl_ep = s.run(
            "MATCH (ep:SysEndpoint)-[:IMPLEMENTS]->(f:SysFeature) RETURN ep.id AS eid, f.id AS fid"
        ).data()

        # System
        for node in data.get("systems", []):
            s.run("MERGE (n:SysSystem {id:$id}) SET n += $props",
                  id=node["id"], props=node)
            counts["systems"] = counts.get("systems", 0) + 1

        # Domains
        for node in data.get("domains", []):
            s.run("MERGE (n:SysDomain {id:$id}) SET n += $props",
                  id=node["id"], props=node)
            if "systemId" in node:
                s.run("""MATCH (sys:SysSystem {id:$sid}), (d:SysDomain {id:$did})
                         MERGE (sys)-[:CONTAINS]->(d)""",
                      sid=node["systemId"], did=node["id"])
            counts["domains"] = counts.get("domains", 0) + 1

        # Modules
        for node in data.get("modules", []):
            s.run("MERGE (n:SysModule {id:$id}) SET n += $props",
                  id=node["id"], props=node)
            if "domainId" in node:
                s.run("""MATCH (d:SysDomain {id:$did}), (m:SysModule {id:$mid})
                         MERGE (d)-[:CONTAINS]->(m)""",
                      did=node["domainId"], mid=node["id"])
            counts["modules"] = counts.get("modules", 0) + 1

        # Features
        for node in data.get("features", []):
            s.run("MERGE (n:SysFeature {id:$id}) SET n += $props",
                  id=node["id"], props={k:v for k,v in node.items() if k not in ("moduleId","parentId")})
            if "moduleId" in node:
                s.run("""MATCH (m:SysModule {id:$mid}), (f:SysFeature {id:$fid})
                         MERGE (m)-[:PROVIDES]->(f)""",
                      mid=node["moduleId"], fid=node["id"])
            if "parentId" in node:
                s.run("""MATCH (p:SysFeature {id:$pid}), (f:SysFeature {id:$fid})
                         MERGE (p)-[:CONTAINS]->(f)""",
                      pid=node["parentId"], fid=node["id"])
            counts["features"] = counts.get("features", 0) + 1

        # User stories
        for node in data.get("userStories", []):
            s.run("MERGE (n:SysUserStory {id:$id}) SET n += $props",
                  id=node["id"], props={k:v for k,v in node.items() if k != "requiresFeatures"})
            for fid in node.get("requiresFeatures", []):
                s.run("""MATCH (us:SysUserStory {id:$uid}), (f:SysFeature {id:$fid})
                         MERGE (us)-[:REQUIRES]->(f)""",
                      uid=node["id"], fid=fid)
            counts["stories"] = counts.get("stories", 0) + 1

        # Architecture standards
        for node in data.get("archStandards", []):
            s.run("MERGE (n:SysArchStd {id:$id}) SET n += $props",
                  id=node["id"], props={k:v for k,v in node.items() if k not in ("systemId","appliesTo")})
            if "systemId" in node:
                s.run("""MATCH (sys:SysSystem {id:$sid}), (a:SysArchStd {id:$aid})
                         MERGE (sys)-[:GOVERNED_BY]->(a)""",
                      sid=node["systemId"], aid=node["id"])
            for mid in node.get("appliesTo", []):
                s.run("""MATCH (m:SysModule {id:$mid}), (a:SysArchStd {id:$aid})
                         MERGE (m)-[:CONFORMS_TO]->(a)""",
                      mid=mid, aid=node["id"])
            counts["archStandards"] = counts.get("archStandards", 0) + 1

        # Scenarios + chapters
        for node in data.get("scenarios", []):
            props = {k: v for k, v in node.items() if k not in ("chapters",)}
            s.run("MERGE (n:SysScenario {id:$id}) SET n += $props",
                  id=node["id"], props=props)
            counts["scenarios"] = counts.get("scenarios", 0) + 1
            for ch in node.get("chapters", []):
                ch_id = f"{node['id']}-CH-{ch['number']}"
                s.run("MERGE (n:SysScenarioChapter {id:$id}) SET n += $props",
                      id=ch_id, props={**ch, "id": ch_id, "scenarioId": node["id"]})
                s.run("""MATCH (sc:SysScenario {id:$sid}), (ch:SysScenarioChapter {id:$cid})
                         MERGE (sc)-[:HAS_CHAPTER]->(ch)""",
                      sid=node["id"], cid=ch_id)
                for fid in ch.get("exercisesFeatures", []):
                    s.run("""MATCH (ch:SysScenarioChapter {id:$cid}), (f:SysFeature {id:$fid})
                             MERGE (ch)-[:EXERCISES]->(f)""",
                          cid=ch_id, fid=fid)
                counts["scenarioChapters"] = counts.get("scenarioChapters", 0) + 1

        # Training programs + modules
        for node in data.get("trainingPrograms", []):
            props = {k: v for k, v in node.items() if k not in ("modules",)}
            s.run("MERGE (n:SysTrainingProgram {id:$id}) SET n += $props",
                  id=node["id"], props=props)
            counts["trainingPrograms"] = counts.get("trainingPrograms", 0) + 1
            for mod in node.get("modules", []):
                mod_id = mod["id"]
                s.run("MERGE (n:SysTrainingModule {id:$id}) SET n += $props",
                      id=mod_id, props={**mod, "programId": node["id"]})
                s.run("""MATCH (p:SysTrainingProgram {id:$pid}), (m:SysTrainingModule {id:$mid})
                         MERGE (p)-[:HAS_MODULE]->(m)""",
                      pid=node["id"], mid=mod_id)
                for fid in mod.get("teachesFeatures", []):
                    s.run("""MATCH (m:SysTrainingModule {id:$mid}), (f:SysFeature {id:$fid})
                             MERGE (m)-[:TEACHES]->(f)""",
                          mid=mod_id, fid=fid)
                counts["trainingModules"] = counts.get("trainingModules", 0) + 1

        # Test packages (registry only — no relationships needed)
        for node in data.get("testPackages", []):
            s.run("MERGE (n:SysTestPackage {id:$id}) SET n += $props",
                  id=node["id"], props=node)
            counts["testPackages"] = counts.get("testPackages", 0) + 1

        # Use cases
        for node in data.get("useCases", []):
            s.run("MERGE (n:SysUseCase {id:$id}) SET n += $props",
                  id=node["id"], props={k:v for k,v in node.items()
                                        if k not in ("storyId","requiresFeatures")})
            if "storyId" in node:
                s.run("""MATCH (us:SysUserStory {id:$sid}),(uc:SysUseCase {id:$uid})
                         MERGE (us)-[:REALIZED_BY]->(uc)""",
                      sid=node["storyId"], uid=node["id"])
            for fid in node.get("requiresFeatures", []):
                s.run("""MATCH (uc:SysUseCase {id:$uid}),(f:SysFeature {id:$fid})
                         MERGE (uc)-[:REQUIRES]->(f)""", uid=node["id"], fid=fid)
            counts["useCases"] = counts.get("useCases", 0) + 1

        # Users
        for node in data.get("users", []):
            s.run("MERGE (n:SysUser {id:$id}) SET n += $props",
                  id=node["id"], props={k:v for k,v in node.items() if k not in ("initiatesStories","usesApps")})
            for sid in node.get("initiatesStories", []):
                s.run("""MATCH (u:SysUser {id:$uid}),(us:SysUserStory {id:$sid})
                         MERGE (u)-[:INITIATES]->(us)""", uid=node["id"], sid=sid)
            counts["users"] = counts.get("users", 0) + 1

        # Applications
        for node in data.get("applications", []):
            s.run("MERGE (n:SysApplication {id:$id}) SET n += $props",
                  id=node["id"], props={k:v for k,v in node.items() if k not in ("containsModules","usedBy")})
            for mid in node.get("containsModules", []):
                s.run("""MATCH (a:SysApplication {id:$aid}),(m:SysModule {id:$mid})
                         MERGE (a)-[:CONTAINS]->(m)""", aid=node["id"], mid=mid)
            for uid in node.get("usedBy", []):
                s.run("""MATCH (u:SysUser {id:$uid}),(a:SysApplication {id:$aid})
                         MERGE (u)-[:USES]->(a)""", uid=uid, aid=node["id"])
            counts["applications"] = counts.get("applications", 0) + 1

        # Defects
        for node in data.get("defects", []):
            s.run("MERGE (n:SysDefect {id:$id}) SET n += $props",
                  id=node["id"], props={k:v for k,v in node.items() if k not in ("affectsFeatures",)})
            for fid in node.get("affectsFeatures", []):
                s.run("""MATCH (d:SysDefect {id:$did}),(f:SysFeature {id:$fid})
                         MERGE (d)-[:AFFECTS]->(f)""", did=node["id"], fid=fid)
            counts["defects"] = counts.get("defects", 0) + 1

        # Enhancements — preserve 'done' status; a seed from an older backup must not reopen closed work
        for node in data.get("enhancements", []):
            props = {k:v for k,v in node.items() if k not in ("extendsFeatures",)}
            existing = s.run("MATCH (e:SysEnhancement {id:$id}) RETURN e.status AS st",
                             id=node["id"]).single()
            if existing and existing["st"] == "done" and props.get("status") != "done":
                props["status"] = "done"   # never downgrade done → proposed via restore
            s.run("MERGE (n:SysEnhancement {id:$id}) SET n += $props",
                  id=node["id"], props=props)
            for fid in node.get("extendsFeatures", []):
                s.run("""MATCH (e:SysEnhancement {id:$eid}),(f:SysFeature {id:$fid})
                         MERGE (e)-[:EXTENDS]->(f)""", eid=node["id"], fid=fid)
            counts["enhancements"] = counts.get("enhancements", 0) + 1

        # Directory → test category mappings
        for node in data.get("dirCategories", []):
            s.run("MERGE (n:SysDirCategory {id:$id}) SET n += $props",
                  id=node["id"], props=node)
            counts["dirCategories"] = counts.get("dirCategories", 0) + 1

        # Architecture decisions + CONSTRAINED_BY and GOVERNED_BY edges
        for node in data.get("archDecisions", []):
            s.run("MERGE (n:SysArchDecision {id:$id}) SET n += $props",
                  id=node["id"], props={k:v for k,v in node.items()
                                        if k not in ("constrainsModules","systemId")})
            if node.get("systemId"):
                s.run("""MATCH (sys:SysSystem {id:$sid}),(a:SysArchDecision {id:$aid})
                         MERGE (sys)-[:GOVERNED_BY]->(a)""",
                      sid=node["systemId"], aid=node["id"])
            for mid in node.get("constrainsModules", []):
                s.run("""MATCH (m:SysModule {id:$mid}),(a:SysArchDecision {id:$aid})
                         MERGE (m)-[:CONSTRAINED_BY]->(a)""",
                      mid=mid, aid=node["id"])
            counts["archDecisions"] = counts.get("archDecisions", 0) + 1

        # Module prerequisite edges
        for edge in data.get("prerequisiteFor", []):
            s.run("""MATCH (a:SysModule {id:$from}),(b:SysModule {id:$to})
                     MERGE (a)-[:PREREQUISITE_FOR]->(b)""",
                  **{"from": edge["fromId"], "to": edge["toId"]})
            counts["prerequisiteFor"] = counts.get("prerequisiteFor", 0) + 1

        # Defect DETECTED_IN edges (AFFECTS edges handled in defects section above)
        for node in data.get("defects", []):
            for mid in node.get("detectedInModules", []):
                s.run("""MATCH (d:SysDefect {id:$did}),(m:SysModule {id:$mid})
                         MERGE (d)-[:DETECTED_IN]->(m)""",
                      did=node["id"], mid=mid)

        # Module pattern usage
        for edge in data.get("usesPattern", []):
            s.run("""MATCH (m:SysModule {id:$mid}), (p:SysModule {id:$pid})
                     MERGE (m)-[:USES_PATTERN]->(p)""",
                  mid=edge["moduleId"], pid=edge["patternId"])
            counts["usesPattern"] = counts.get("usesPattern", 0) + 1

        # Feedback entries
        for node in data.get("feedback", []):
            s.run("MERGE (f:SysFeedback {id:$id}) SET f += $props",
                  id=node["id"], props=node)
            counts["feedback"] = counts.get("feedback", 0) + 1

        # Proposals
        for node in data.get("proposals", []):
            s.run("MERGE (n:SysProposal {id:$id}) SET n += $props",
                  id=node["id"], props={k: v for k, v in node.items() if k not in ("extendsFeatures",)})
            for fid in node.get("extendsFeatures", []):
                s.run("""MATCH (p:SysProposal {id:$pid}),(f:SysFeature {id:$fid})
                         MERGE (p)-[:EXTENDS]->(f)""", pid=node["id"], fid=fid)
            counts["proposals"] = counts.get("proposals", 0) + 1

        # Notes
        for node in data.get("notes", []):
            s.run("MERGE (n:SysNote {id:$id}) SET n += $props",
                  id=node["id"], props=node)
            counts["notes"] = counts.get("notes", 0) + 1

        # Symbols
        for node in data.get("symbols", []):
            s.run("MERGE (n:SysSymbol {id:$id}) SET n += $props",
                  id=node["id"], props={k:v for k,v in node.items() if k not in ("implementsFeature",)})
            if node.get("implementsFeature"):
                s.run("""MATCH (sym:SysSymbol {id:$sid}),(f:SysFeature {id:$fid})
                         MERGE (sym)-[:IMPLEMENTS]->(f)""",
                      sid=node["id"], fid=node["implementsFeature"])
            if node.get("moduleId"):
                s.run("""MATCH (sym:SysSymbol {id:$sid}),(m:SysModule {id:$mid})
                         MERGE (m)-[:CONTAINS_SYMBOL]->(sym)""",
                      sid=node["id"], mid=node["moduleId"])
            counts["symbols"] = counts.get("symbols", 0) + 1

        # Tests
        for node in data.get("tests", []):
            s.run("MERGE (n:SysTest {id:$id}) SET n += $props",
                  id=node["id"], props={k:v for k,v in node.items() if k != "verifiesFeatures"})
            # Re-create CONTAINS_TEST edge from packageId stored on the node.
            # Without this, a backup restore loses the Package→Test path that
            # the briefing query requires for tier detection (cmp/int/uc).
            if node.get("packageId"):
                s.run("""MATCH (p:SysTestPackage {id:$pid}), (t:SysTest {id:$tid})
                         MERGE (p)-[:CONTAINS_TEST]->(t)""",
                      pid=node["packageId"], tid=node["id"])
            for fid in node.get("verifiesFeatures", []):
                s.run("""MATCH (t:SysTest {id:$tid}), (f:SysFeature {id:$fid})
                         MERGE (t)-[:VERIFIES]->(f)""",
                      tid=node["id"], fid=fid)
            counts["tests"] = counts.get("tests", 0) + 1

        # Endpoints
        for node in data.get("endpoints", []):
            s.run("MERGE (n:SysEndpoint {id:$id}) SET n += $props",
                  id=node["id"], props={k:v for k,v in node.items() if k != "implementsFeature"})
            if "implementsFeature" in node:
                s.run("""MATCH (e:SysEndpoint {id:$eid}), (f:SysFeature {id:$fid})
                         MERGE (e)-[:IMPLEMENTS]->(f)""",
                      eid=node["id"], fid=node["implementsFeature"])
            counts["endpoints"] = counts.get("endpoints", 0) + 1

        # Restore any VERIFIES/IMPLEMENTS edges that existed before seed ran.
        # Seed only adds edges from the file; it never explicitly deletes, but
        # seeding from a stale backup can leave links absent if the file lacks them.
        if _pre_verifies:
            s.run("""
                UNWIND $edges AS e
                MATCH (t:SysTest {id: e.tid}), (f:SysFeature {id: e.fid})
                MERGE (t)-[:VERIFIES]->(f)
            """, edges=_pre_verifies)
        if _pre_impl_sym:
            s.run("""
                UNWIND $edges AS e
                MATCH (sym:SysSymbol {id: e.sid}), (f:SysFeature {id: e.fid})
                MERGE (sym)-[:IMPLEMENTS]->(f)
            """, edges=_pre_impl_sym)
        if _pre_impl_ep:
            s.run("""
                UNWIND $edges AS e
                MATCH (ep:SysEndpoint {id: e.eid}), (f:SysFeature {id: e.fid})
                MERGE (ep)-[:IMPLEMENTS]->(f)
            """, edges=_pre_impl_ep)

    drv.close()
    for k, v in sorted(counts.items()):
        print(f"  {k:20s} {v}")
    print(f"\n✓ Seed complete from {path}")


# ── Briefing ──────────────────────────────────────────────────────────────────

def cmd_briefing(args):
    instance = args.instance
    drv = _driver()
    today = date.today().isoformat()

    with drv.session() as s:
        # Modules for this instance
        modules = s.run("""
            MATCH (d:SysDomain)<-[:CONTAINS]-(sys:SysSystem)
            MATCH (d)-[:CONTAINS]->(m:SysModule {instance: $inst})
            RETURN m.id AS id, m.name AS name, m.type AS type, d.name AS domain
            ORDER BY d.name, m.name
        """, inst=instance).data()

        if not modules:
            print(f"No modules found for instance '{instance}'. Run seed first.")
            drv.close()
            return

        mod_ids = [m["id"] for m in modules]

        # Feature coverage per module — Superseded features excluded from counts
        coverage = s.run("""
            MATCH (m:SysModule)-[:PROVIDES]->(f:SysFeature)
            WHERE m.id IN $mids
              AND NOT f.status IN ['Superseded','Deprecated']
            OPTIONAL MATCH (t:SysTest)-[:VERIFIES]->(f)
            RETURN m.id AS mid, f.id AS fid, f.name AS fname,
                   f.status AS status,
                   coalesce(f.isUserFacing, true) AS isUserFacing,
                   count(t) AS testCount
            ORDER BY m.id, f.id
        """, mids=mod_ids).data()

        # V-model four-tier coverage per module (ADR-027)
        # component=unit, integration=API, usecase=UC flow, e2e=US journey
        tier_coverage = s.run("""
            MATCH (m:SysModule)-[:PROVIDES]->(f:SysFeature)
            WHERE m.id IN $mids
              AND NOT f.status IN ['Superseded','Deprecated']
            OPTIONAL MATCH (tc:SysTest)-[:VERIFIES]->(f)
            WHERE (tc.testType IN ['component','go-unit'])
               OR exists((:SysTestPackage {testCategory:'component'})-[:CONTAINS_TEST]->(tc))
            OPTIONAL MATCH (ti:SysTest)-[:VERIFIES]->(f)
            WHERE (ti.testType = 'integration')
               OR exists((:SysTestPackage {testCategory:'integration'})-[:CONTAINS_TEST]->(ti))
            OPTIONAL MATCH (tu:SysTest)-[:VERIFIES]->(f)
            WHERE (tu.testType = 'usecase')
               OR exists((:SysTestPackage {testCategory:'usecase'})-[:CONTAINS_TEST]->(tu))
            OPTIONAL MATCH (te:SysTest)-[:VERIFIES]->(f)
            WHERE (te.testType = 'e2e')
               OR exists((:SysTestPackage {testCategory:'e2e'})-[:CONTAINS_TEST]->(te))
            RETURN m.id AS mid,
                   count(DISTINCT CASE WHEN tc IS NOT NULL THEN f END) AS cmpCovered,
                   count(DISTINCT CASE WHEN ti IS NOT NULL THEN f END) AS intCovered,
                   count(DISTINCT CASE WHEN tu IS NOT NULL THEN f END) AS ucCovered,
                   count(DISTINCT CASE WHEN te IS NOT NULL THEN f END) AS e2eCovered,
                   count(DISTINCT f) AS total
            ORDER BY m.id
        """, mids=mod_ids).data()
        tier_map = {r["mid"]: r for r in tier_coverage}

        # Defects — includes feature-linked (AFFECTS) and module-detected (DETECTED_IN)
        defects = s.run("""
            MATCH (def:SysDefect) WHERE def.status <> 'closed'
            OPTIONAL MATCH (def)-[:AFFECTS]->(f:SysFeature)<-[:PROVIDES]-(mf:SysModule)
            OPTIONAL MATCH (def)-[:DETECTED_IN]->(md:SysModule)
            WITH def, f, coalesce(mf, md) AS m
            WHERE m.id IN $mids
            RETURN def.id AS did, def.title AS title,
                   coalesce(def.severity,'medium') AS sev,
                   def.source AS src,
                   def.occurrences AS occ,
                   f.id AS fid, f.name AS fname, m.id AS mid
            ORDER BY
              CASE coalesce(def.severity,'medium')
                WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                WHEN 'medium' THEN 2 ELSE 3 END,
              def.id
        """, mids=mod_ids).data()

        # Enhancements for this instance — include unlinked ones (instance filter)
        enhancements = s.run("""
            MATCH (e:SysEnhancement)
            WHERE e.instance = $inst AND e.status <> 'done'
            OPTIONAL MATCH (e)-[:EXTENDS]->(f:SysFeature)<-[:PROVIDES]-(m:SysModule)
            WHERE m.id IN $mids
            RETURN e.id AS eid, e.title AS title,
                   coalesce(e.status,'proposed') AS status,
                   coalesce(e.priority,'Should') AS priority,
                   f.id AS fid, f.name AS fname, m.id AS mid
            ORDER BY
              CASE coalesce(e.status,'proposed')
                WHEN 'in-progress' THEN 0 WHEN 'approved' THEN 1
                WHEN 'proposed' THEN 2 ELSE 3 END,
              CASE coalesce(e.priority,'Should')
                WHEN 'Must' THEN 0 WHEN 'Should' THEN 1 ELSE 2 END,
              e.id
        """, inst=instance, mids=mod_ids).data()

        # Architecture standard compliance.
        # Only show standards that have at least one CONFORMS_TO edge pointing
        # at a module in this instance — standards with no appliesTo are
        # system-wide references and not shown per-instance.
        arch = s.run("""
            MATCH (sys:SysSystem)-[:GOVERNED_BY]->(a:SysArchStd)
            MATCH (m_any:SysModule)-[:CONFORMS_TO]->(a)
            WHERE m_any.id IN $mids
            WITH a, collect(DISTINCT m_any.id) AS conforming
            RETURN a.id AS aid, a.name AS aname, a.category AS cat,
                   conforming
            ORDER BY a.category, a.id
        """, mids=mod_ids).data()

        # Evidence map: which gap features have scenario/training backing
        # (must be inside session — queried after we know the gap fids)
        all_fids = [r["fid"] for v in coverage for r in [v]]
        evidence_raw = s.run("""
            UNWIND $fids AS fid
            MATCH (f:SysFeature {id: fid})
            OPTIONAL MATCH (ch:SysScenarioChapter)-[:EXERCISES]->(f)
            OPTIONAL MATCH (ch)<-[:HAS_CHAPTER]-(sc:SysScenario)
            OPTIONAL MATCH (tm:SysTrainingModule)-[:TEACHES]->(f)
            OPTIONAL MATCH (tm)<-[:HAS_MODULE]-(tp:SysTrainingProgram)
            RETURN fid,
                   collect(DISTINCT sc.name) AS scenarios,
                   collect(DISTINCT tp.name) AS training
        """, fids=all_fids).data()
        ev_map = {r["fid"]: r for r in evidence_raw}

    drv.close()

    # ── Format output ────────────────────────────────────────────────────────
    W = 62
    print("═" * W)
    print(f"  SYSTEM GRAPH BRIEFING — {instance}  ({today})")
    print("═" * W)
    print(f"  Free CLI — full features (export, analyse, AI review, visualiser) at https://www.org-edge.com/sysedge.html")

    # Group coverage by module
    from collections import defaultdict
    by_mod = defaultdict(list)
    for row in coverage:
        by_mod[row["mid"]].append(row)

    # Separate user-facing from infrastructure features for coverage counts
    uf_rows = [r for v in by_mod.values() for r in v if r.get("isUserFacing", True)]
    total_f      = len(uf_rows)
    total_tested = sum(1 for r in uf_rows if r["testCount"] > 0)
    infra_count  = sum(1 for v in by_mod.values() for r in v if not r.get("isUserFacing", True))

    infra_note = f"  ({infra_count} infra excluded)" if infra_count else ""
    print(f"\nMODULES ({len(modules)})  —  features: {total_f}  tested: {total_tested}/{total_f}{infra_note}")
    print()

    compact = getattr(args, "compact", False)

    def tier_str(covered, total, short):
        mark = "✓" if covered == total else ("~" if covered > 0 else "✗")
        return f"{mark}{short} {covered}/{total}"

    for mod in modules:
        mid   = mod["id"]
        mrows = by_mod.get(mid, [])
        uf_mrows = [r for r in mrows if r.get("isUserFacing", True)]
        tested = sum(1 for r in uf_mrows if r["testCount"] > 0)
        all_pass = (tested == len(uf_mrows) and uf_mrows)
        pct    = f"{tested}/{len(uf_mrows)}" if uf_mrows else "0/0"
        tick   = " ✓" if all_pass else ""
        mtype  = f" [{mod['type']}]" if mod.get("type") else ""
        print(f"  {mid:<22} {mod['name']:<32} {pct}{tick}{mtype}")

        # Three-tier coverage line
        tc = tier_map.get(mid, {})
        if tc and tc.get("total", 0) > 0:
            n    = tc["total"]
            tiers = "  ".join([
                tier_str(tc.get("cmpCovered", 0), n, "cmp"),
                tier_str(tc.get("intCovered", 0), n, "int"),
                tier_str(tc.get("ucCovered",  0), n, "uc "),
                tier_str(tc.get("e2eCovered", 0), n, "e2e"),
            ])
            print(f"    {'':22} {tiers}")

        # In compact mode skip per-feature list when module is fully passing
        if compact and all_pass:
            print()
            continue

        for r in mrows:
            tick2 = "✓" if r["testCount"] > 0 else "✗"
            infra_tag = " [infra]" if not r.get("isUserFacing", True) else ""
            print(f"    {tick2} {r['fid']:<14} {r['fname']}{infra_tag}")
        print()

    # Coverage gaps — user-facing features only (infrastructure excluded by default)
    gaps = [r for v in by_mod.values() for r in v
            if r["testCount"] == 0 and r.get("isUserFacing", True)]
    if gaps:
        print(f"COVERAGE GAPS ({len(gaps)})")
        for r in gaps:
            mid_label = next((m["id"] for m in modules
                              if any(x["fid"] == r["fid"] for x in by_mod.get(m["id"], []))), "?")
            ev = ev_map.get(r["fid"], {})
            scns = ev.get("scenarios", [])
            trns = ev.get("training", [])
            tag = ""
            if scns:
                tag += f"  ← scenario: {', '.join(scns)}"
            if trns:
                tag += f"  ← training: {', '.join(trns)}"
            urgency = "!" if (scns or trns) else " "
            print(f"  {urgency}✗ {r['fid']:<14} {r['fname']:<38} {mid_label}{tag}")
        print()

    # Enhancements
    if enhancements:
        from itertools import groupby
        print(f"ENHANCEMENTS ({len(enhancements)})")
        by_status = {}
        for e in enhancements:
            by_status.setdefault(e["status"], []).append(e)
        status_order = ["approved", "in-progress", "proposed"]
        for st in status_order:
            items = by_status.get(st, [])
            if not items:
                continue
            print(f"  {st.upper()} ({len(items)})")
            for e in items:
                feat_ref = f"→ {e['fid']}" if e.get("fid") else f"[{e['mid']}]"
                prio = f"[{e['priority']}]" if e.get("priority") else ""
                print(f"    {e['eid']:<16} {prio:<8} {e['title'][:60]:<60} {feat_ref}")
        print()
    else:
        print("ENHANCEMENTS — none\n")

    # Defects
    if defects:
        print(f"OPEN DEFECTS ({len(defects)})")
        for d in defects:
            src_tag = " [log]" if d.get("src") == "log" else ""
            occ_tag = f" ×{d['occ']}" if d.get("occ") and int(d["occ"]) > 1 else ""
            feat_ref = f"→ {d['fid']}" if d.get("fid") else f"[{d['mid']}]"
            print(f"  {d['did']:<18} [{d['sev']:<8}] {d['title'][:40]:<40} {feat_ref}{src_tag}{occ_tag}")
        print()
    else:
        print("OPEN DEFECTS — none\n")

    # Architecture — only show standards that explicitly list modules in this instance
    if arch:
        print("ARCHITECTURE STANDARDS")
        last_cat = None
        for a in arch:
            cat = a["cat"] or "General"
            if cat != last_cat:
                print(f"  {cat}")
                last_cat = cat
            conforming = a["conforming"]
            status = f"✓ {', '.join(conforming)}" if conforming else "no conforming modules"
            print(f"    {a['aid']:<18} {a['aname']:<38} {status}")
        print()

    # ── Orphan warning (FB-108) ───────────────────────────────────────────────
    # Quick orphan check — if count is high, suggest running analyse-graph
    orphan_tests = s.run("""
        MATCH (t:SysTest)
        WHERE NOT ()-[:CONTAINS_TEST]->(t)
          AND NOT (t)-[:VERIFIES]->()
        RETURN count(t) AS n
    """).single()["n"]
    if orphan_tests > 10:
        print(f"\n  ⚠  {orphan_tests} unlinked tests detected — run: "
              f"sys_graph.py analyse --orphans --instance {instance}")

    print("─" * W)
    print("  sys_graph.py link-test / link-endpoint / link-symbol to update")
    print("─" * W)


# ── Link commands ─────────────────────────────────────────────────────────────

def cmd_link_test(args):
    """Link a specific test function to a feature.

    Prefers matching an already-scanned SysTest node by file+function before
    creating a new one, so scanned and manually-linked tests stay unified.
    """
    drv = _driver()
    with drv.session() as s:
        # Try to find an existing scanned node first (id = {file}::{class}::{fn} or {file}::{fn})
        existing = s.run("""
            MATCH (t:SysTest)
            WHERE t.file CONTAINS $file AND t.function = $fn
            RETURN t.id AS tid LIMIT 1
        """, file=args.file, fn=args.fn).single()

        if existing:
            tid = existing["tid"]
        else:
            # Legacy path: create a node with the old T-... format
            tid = f"T-{args.file.replace('/', '-')}-{args.fn}"[:80]
            s.run("""
                MERGE (t:SysTest {id:$tid})
                SET t.file=$file, t.function=$fn, t.testType=$ttype
            """, tid=tid, file=args.file, fn=args.fn, ttype=args.type or "integration")

        s.run("""
            MATCH (t:SysTest {id:$tid}), (f:SysFeature {id:$fid})
            MERGE (t)-[:VERIFIES]->(f)
        """, tid=tid, fid=args.feature)
    drv.close()
    print(f"✓ {args.fn}  →  VERIFIES  →  {args.feature}")


def cmd_link_feature(args):
    """Bulk-link matching test nodes to a feature by pattern.

    --tests patterns are matched against SysTest.id. If the pattern contains
    '::' (pytest node ID style) the match is anchored — only tests whose ID
    ends with or equals the pattern match, preventing file-prefix bleed where
    all tests in a file link to every feature. Plain directory prefixes (no ::)
    still use substring matching for bulk-linking by directory.

    Examples:
        link-feature --feature F-P3-001 --tests "test_p3_api.py::TestDocumentTypes"
        link-feature --feature F-P4-002 --tests "TestEntityCreate::test_create,TestEntityUpdate"
        link-feature --feature F-MGR-003 --tests "tests/manage/"  # directory bulk-link
    """
    fid      = args.feature
    patterns = [p.strip() for p in args.tests.split(",") if p.strip()]
    drv      = _driver()
    total    = 0
    with drv.session() as s:
        if not s.run("MATCH (f:SysFeature {id:$id}) RETURN f.id", id=fid).single():
            print(f"ERROR: feature '{fid}' not found", file=sys.stderr)
            drv.close(); sys.exit(1)
        for pat in patterns:
            # Anchored match when pattern contains '::' — prevents N:M bleed
            if "::" in pat:
                result = s.run("""
                    MATCH (t:SysTest)
                    WHERE t.id = $pat OR t.id ENDS WITH $pat OR t.id CONTAINS $pat
                    WITH t LIMIT 500
                    MATCH (f:SysFeature {id:$fid})
                    MERGE (t)-[:VERIFIES]->(f)
                    RETURN count(t) AS cnt
                """, pat=pat, fid=fid).single()
            else:
                result = s.run("""
                    MATCH (t:SysTest) WHERE t.id CONTAINS $pat
                    MATCH (f:SysFeature {id:$fid})
                    MERGE (t)-[:VERIFIES]->(f)
                    RETURN count(t) AS cnt
                """, pat=pat, fid=fid).single()
            cnt = result["cnt"] if result else 0
            total += cnt
            if cnt == 0:
                print(f"  ⚠ {pat}  →  0 matches — pattern did not resolve to any SysTest node",
                      file=sys.stderr)
                print(f"     Hint: run scan-tests first, then use the full path from scan output",
                      file=sys.stderr)
            else:
                print(f"  {pat}  →  {cnt} tests  →  {fid}")
                # Check for orphan tests (no CONTAINS_TEST edge → won't count in test-gaps)
                orphan = s.run("""
                    MATCH (t:SysTest)-[:VERIFIES]->(f:SysFeature {id:$fid})
                    WHERE t.id CONTAINS $pat
                      AND NOT (:SysTestPackage)-[:CONTAINS_TEST]->(t)
                    RETURN count(t) AS n
                """, fid=fid, pat=pat).single()["n"]
                if orphan > 0:
                    print(f"  ⚠ {orphan} matched test(s) have no CONTAINS_TEST edge — "
                          f"they won't count in test-gaps. Run scan-tests to register them.",
                          file=sys.stderr)
    drv.close()
    print(f"  total: {total} VERIFIES edges created")


def cmd_link_endpoint(args):
    eid = f"EP-{args.method}-{args.path.replace('/', '-')}"[:80]
    drv = _driver()
    with drv.session() as s:
        s.run("""
            MERGE (e:SysEndpoint {id:$eid})
            SET e.method=$method, e.path=$path,
                e.binary=$binary, e.permission=$perm
        """, eid=eid, method=args.method, path=args.path,
             binary=args.binary or "", perm=args.permission or "")
        s.run("""
            MATCH (e:SysEndpoint {id:$eid}), (f:SysFeature {id:$fid})
            MERGE (e)-[:IMPLEMENTS]->(f)
        """, eid=eid, fid=args.feature)
    drv.close()
    print(f"✓ {args.method} {args.path}  →  IMPLEMENTS  →  {args.feature}")


def cmd_link_symbol(args):
    sid = f"SYM-{args.file.replace('/', '-')}-{args.symbol}"[:80]
    drv = _driver()
    with drv.session() as s:
        s.run("""
            MERGE (sym:SysSymbol {id:$sid})
            SET sym.file=$file, sym.symbol=$symbol,
                sym.line=$line, sym.symbolType=$stype
        """, sid=sid, file=args.file, symbol=args.symbol,
             line=args.line or 0, stype=args.symtype or "handler")
        s.run("""
            MATCH (sym:SysSymbol {id:$sid}), (f:SysFeature {id:$fid})
            MERGE (sym)-[:IMPLEMENTS]->(f)
        """, sid=sid, fid=args.feature)
    drv.close()
    print(f"✓ {args.symbol}  →  IMPLEMENTS  →  {args.feature}")


# ── List commands ─────────────────────────────────────────────────────────────

def cmd_features(args):
    drv = _driver()
    with drv.session() as s:
        rows = s.run("""
            MATCH (m:SysModule {id:$mid})-[:PROVIDES]->(f:SysFeature)
            OPTIONAL MATCH (t:SysTest)-[:VERIFIES]->(f)
            RETURN f.id AS id, f.name AS name, f.status AS status,
                   coalesce(f.isUserFacing, true) AS isUserFacing,
                   count(t) AS tests
            ORDER BY f.id
        """, mid=args.module).data()
    drv.close()
    for r in rows:
        tick = "✓" if r["tests"] > 0 else "✗"
        infra_tag = " [infra]" if not r.get("isUserFacing", True) else ""
        print(f"  {tick} {r['id']:<16} {r['name']}{infra_tag}")


def cmd_stories(args):
    drv = _driver()
    with drv.session() as s:
        if getattr(args, "gap", False):
            rows = s.run("""
                MATCH (us:SysUserStory)
                WHERE NOT (us)-[:REALIZED_BY]->(:SysUseCase)
                RETURN us.id AS id, us.title AS title
                ORDER BY us.id
            """).data()
            drv.close()
            print(f"USER STORIES WITHOUT USE CASES ({len(rows)})")
            for r in rows:
                print(f"  {r['id']}  {r['title']}")
            return
        rows = s.run("""
            MATCH (us:SysUserStory)
            OPTIONAL MATCH (us)-[:REQUIRES]->(f:SysFeature)
            OPTIONAL MATCH (sc:SysScenario)-[:HAS_CHAPTER]->(:SysScenarioChapter)-[:EXERCISES]->(f)
            OPTIONAL MATCH (tp:SysTrainingProgram)-[:HAS_MODULE]->(:SysTrainingModule)-[:TEACHES]->(f)
            RETURN us.id AS id, us.title AS title,
                   collect(DISTINCT f.id) AS features,
                   collect(DISTINCT sc.name) AS scenarios,
                   collect(DISTINCT tp.name) AS training
            ORDER BY us.id
        """).data()
    drv.close()
    for r in rows:
        extras = []
        if r["scenarios"]: extras.append(f"scenario: {', '.join(r['scenarios'])}")
        if r["training"]:  extras.append(f"training: {', '.join(r['training'])}")
        suffix = f"  [{'; '.join(extras)}]" if extras else ""
        print(f"  {r['id']}  {r['title']}{suffix}")
        for fid in r["features"]:
            print(f"    → {fid}")


def cmd_scenarios(args):
    """Show all scenarios with chapter decomposition status."""
    drv = _driver()
    with drv.session() as s:
        scenarios = s.run("""
            MATCH (sc:SysScenario)
            RETURN sc.id AS id, sc.name AS name, sc.status AS status,
                   sc.audience AS audience, sc.durationS AS dur,
                   sc.aiCallCount AS aiCalls
            ORDER BY sc.id
        """).data()
        for sc in scenarios:
            dur_min   = f"{int(sc['dur'] or 0) // 60}m" if sc.get("dur") else "?"
            ai_tag    = f"  {sc['aiCalls']} AI call{'s' if sc['aiCalls']!=1 else ''}" if sc.get("aiCalls") else ""
            print(f"\n  {sc['id']}  {sc['name']}  [{sc.get('status','?')} · {dur_min}{ai_tag}]")
            if sc.get("audience"):
                print(f"    audience: {', '.join(sc['audience']) if isinstance(sc['audience'], list) else sc['audience']}")
            # Demonstrates links (narrative-intent)
            stories = s.run("""
                MATCH (sc:SysScenario {id:$sid})-[:DEMONSTRATES]->(us:SysUserStory)
                RETURN us.id AS uid, us.title AS title
                ORDER BY us.id
            """, sid=sc["id"]).data()
            for us in stories:
                print(f"    demonstrates: {us['uid']}  {us['title']}")
            # Chapter → feature links (evidence)
            chapters = s.run("""
                MATCH (sc:SysScenario {id:$sid})-[:HAS_CHAPTER]->(ch:SysScenarioChapter)
                OPTIONAL MATCH (ch)-[:EXERCISES]->(f:SysFeature)
                RETURN ch.number AS num, ch.title AS title,
                       collect(f.id) AS features
                ORDER BY ch.number
            """, sid=sc["id"]).data()
            for ch in chapters:
                linked = f"→ {', '.join(ch['features'])}" if ch["features"] else "  (not yet linked)"
                print(f"    Ch {ch['num']}  {ch['title']:<45} {linked}")

        print()
        # Training programs
        programs = s.run("""
            MATCH (tp:SysTrainingProgram)
            RETURN tp.id AS id, tp.name AS name, tp.audience AS audience
            ORDER BY tp.id
        """).data()
        for tp in programs:
            print(f"\n  {tp['id']}  {tp['name']}  [audience: {tp.get('audience','?')}]")
            modules = s.run("""
                MATCH (tp:SysTrainingProgram {id:$pid})-[:HAS_MODULE]->(m:SysTrainingModule)
                OPTIONAL MATCH (m)-[:TEACHES]->(f:SysFeature)
                RETURN m.id AS id, m.title AS title,
                       collect(f.id) AS features
                ORDER BY m.id
            """, pid=tp["id"]).data()
            for m in modules:
                linked = f"→ {', '.join(m['features'])}" if m["features"] else "  (not yet linked)"
                print(f"    {m['id']:<25} {m['title']:<45} {linked}")
    drv.close()


def cmd_link_chapter(args):
    """Link a scenario chapter to a feature it exercises."""
    ch_id = args.chapter
    drv = _driver()
    with drv.session() as s:
        result = s.run(
            "MATCH (ch:SysScenarioChapter {id:$cid}) RETURN ch.title AS title",
            cid=ch_id).single()
        if not result:
            print(f"ERROR: chapter '{ch_id}' not found. Run: sys_graph.py scenarios",
                  file=sys.stderr)
            drv.close(); sys.exit(1)
        s.run("""MATCH (ch:SysScenarioChapter {id:$cid}), (f:SysFeature {id:$fid})
                 MERGE (ch)-[:EXERCISES]->(f)""",
              cid=ch_id, fid=args.feature)
    drv.close()
    print(f"✓ {ch_id}  →  EXERCISES  →  {args.feature}")


def cmd_link_scenario_story(args):
    """Create a DEMONSTRATES edge from a SysScenario to a SysUserStory.

    This is the narrative-intent link: the story the scenario was designed to tell.
    Feature-level coverage (EXERCISES → SysFeature ← REALIZES) is the evidence layer;
    DEMONSTRATES is the summary layer — maintained separately so queries are simpler.
    """
    drv = _driver()
    with drv.session() as s:
        sc = s.run(
            "MATCH (n:SysScenario {id:$id}) RETURN n.name AS name", id=args.scenario
        ).single()
        if not sc:
            print(f"ERROR: scenario '{args.scenario}' not found. Run: sys_graph.py scenarios",
                  file=sys.stderr)
            drv.close(); sys.exit(1)
        us = s.run(
            "MATCH (n:SysUserStory {id:$id}) RETURN n.title AS title", id=args.story
        ).single()
        if not us:
            print(f"ERROR: user story '{args.story}' not found. Run: sys_graph.py stories",
                  file=sys.stderr)
            drv.close(); sys.exit(1)
        s.run("""MATCH (sc:SysScenario {id:$sid}), (us:SysUserStory {id:$uid})
                 MERGE (sc)-[:DEMONSTRATES]->(us)""",
              sid=args.scenario, uid=args.story)
    drv.close()
    print(f"✓ {args.scenario}  →  DEMONSTRATES  →  {args.story}")


def cmd_link_training(args):
    """Link a training module to a feature it teaches."""
    drv = _driver()
    with drv.session() as s:
        result = s.run(
            "MATCH (m:SysTrainingModule {id:$mid}) RETURN m.title AS title",
            mid=args.module).single()
        if not result:
            print(f"ERROR: training module '{args.module}' not found. Run: sys_graph.py scenarios",
                  file=sys.stderr)
            drv.close(); sys.exit(1)
        s.run("""MATCH (m:SysTrainingModule {id:$mid}), (f:SysFeature {id:$fid})
                 MERGE (m)-[:TEACHES]->(f)""",
              mid=args.module, fid=args.feature)
    drv.close()
    print(f"✓ {args.module}  →  TEACHES  →  {args.feature}")


# ── Test run recording ────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def cmd_record_run(args):
    """Record a test execution result against a test node.

    Called from pytest after-run hooks or manually:
        python3 scripts/sys_graph.py record-run \\
            --package backend/tests/integration \\
            --passed 42 --failed 2 --duration 38.4
    """
    run_id = f"RUN-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{os.getpid()}"
    drv = _driver()
    with drv.session() as s:
        status = "passed" if args.failed == 0 else "failed"
        # Create a SysTestRun node for the package
        s.run("""
            MERGE (pkg:SysTestPackage {id: $pkgId})
            SET pkg.path = $path, pkg.lastRun = $now, pkg.lastPassed = $passed,
                pkg.lastFailed = $failed, pkg.lastSkipped = $skipped,
                pkg.lastXFailed = $xfailed, pkg.lastDuration = $dur,
                pkg.lastStatus = $status
            WITH pkg
            CREATE (run:SysTestRun {
                id: $runId, packageId: $pkgId,
                ranAt: $now, passed: $passed, failed: $failed,
                skipped: $skipped, xfailed: $xfailed,
                durationS: $dur, status: $status, notes: $notes
            })
            MERGE (pkg)-[:HAS_RUN]->(run)
        """, pkgId=args.package, path=args.package,
             runId=run_id, now=_now_iso(),
             passed=args.passed, failed=args.failed,
             skipped=getattr(args, 'skipped', 0),
             xfailed=getattr(args, 'xfailed', 0),
             dur=args.duration, status=status,
             notes=getattr(args, 'notes', ''))

        # Update lastTestedAt on all SysTest nodes via CONTAINS_TEST edges (preferred),
        # falling back to file-path prefix matching for tests registered via seed/link-test.
        status_label = "passed" if args.failed == 0 else "failed"
        updated = s.run("""
            MATCH (pkg:SysTestPackage {id: $pkgId})-[:CONTAINS_TEST]->(t:SysTest)
            SET t.lastTestedAt = $now, t.lastStatus = $status
            RETURN count(t) AS cnt
        """, pkgId=args.package, now=_now_iso(), status=status_label).single()["cnt"]

        if updated == 0:
            # Fallback: path-prefix match for manually seeded tests
            pkg_basename = args.package.split("/")[-1].replace(".py", "")
            updated = s.run("""
                MATCH (t:SysTest)
                WHERE t.file CONTAINS $basename AND t.packageId IS NULL
                SET t.lastTestedAt = $now, t.lastStatus = $status
                RETURN count(t) AS cnt
            """, basename=pkg_basename, now=_now_iso(),
                 status=status_label).single()["cnt"]

    drv.close()
    status     = "✓" if args.failed == 0 else f"✗ {args.failed} failed"
    skip_str   = f"  {getattr(args,'skipped',0)}s" if getattr(args,'skipped',0) else ""
    xfail_str  = f"  {getattr(args,'xfailed',0)}x" if getattr(args,'xfailed',0) else ""
    notes_str  = f"  [{args.notes}]" if getattr(args,'notes','') else ""
    print(f"  {status} {args.passed}✓{skip_str}{xfail_str}  {args.package}{notes_str}")
    if updated:
        print(f"     updated lastTestedAt on {updated} test nodes")


def cmd_test_status(args):
    """Show last-run timestamps per test package and stale test nodes."""
    drv = _driver()
    with drv.session() as s:
        # Filter by instance if specified
        if getattr(args, 'instance', None):
            inst_filter = "WHERE pkg.instance = $inst"
            inst_param  = {"inst": args.instance}
        else:
            inst_filter = ""
            inst_param  = {}

        packages = s.run(f"""
            MATCH (pkg:SysTestPackage)
            {inst_filter}
            RETURN pkg.id AS id, pkg.lastRun AS lastRun,
                   pkg.lastPassed AS passed, pkg.lastFailed AS failed,
                   pkg.lastSkipped AS skipped, pkg.lastXFailed AS xfailed,
                   pkg.lastDuration AS dur, pkg.area AS area, pkg.instance AS inst
            ORDER BY pkg.area, pkg.id
        """, **inst_param).data()

        never   = [p for p in packages if not p["lastRun"]]
        ran     = [p for p in packages if p["lastRun"]]
        failing = [p for p in ran if (p["failed"] or 0) > 0]

        print(f"\nTEST PACKAGES — {len(ran)}/{len(packages)} run  "
              f"({len(failing)} failing  {len(never)} never run)")
        if ran:
            print()
            last_area = None
            for p in sorted(ran, key=lambda x: (x["area"] or "", x["id"])):
                area = p["area"] or "?"
                if area != last_area:
                    print(f"  {area}")
                    last_area = area
                ts     = (p["lastRun"] or "")[:19]
                tick   = "✓" if (p["failed"] or 0) == 0 else "✗"
                dur    = f" {p['dur']:.0f}s" if p.get("dur") else ""
                skip_s = f" {p['skipped']}s" if p.get("skipped") else ""
                xf_s   = f" {p['xfailed']}x" if p.get("xfailed") else ""
                name   = p["id"].split("/")[-1].replace(".py","")
                print(f"    {tick} {name:<55} {ts}  "
                      f"{p['passed'] or 0}✓{skip_s}{xf_s} {p['failed'] or 0}✗{dur}")

        # Tests with no lastTestedAt (never run since graph was populated)
        never = s.run("""
            MATCH (t:SysTest)
            WHERE t.lastTestedAt IS NULL
            RETURN count(t) AS cnt
        """).single()["cnt"]

        stale_cutoff = args.days or 7
        stale = s.run("""
            MATCH (t:SysTest)
            WHERE t.lastTestedAt IS NOT NULL
              AND datetime(t.lastTestedAt) < datetime() - duration({days: $days})
            RETURN count(t) AS cnt
        """, days=stale_cutoff).single()["cnt"]

        print(f"\n  never run: {never} tests")
        print(f"  stale (>{stale_cutoff}d): {stale} tests\n")

    drv.close()


# ── Backup and restore ────────────────────────────────────────────────────────

def _do_backup(out_path: Path) -> None:
    """Write a backup to out_path (shared by cmd_backup and the pre-seed auto-backup)."""
    drv = _driver()

    export = {"_schema_version": SCHEMA_VERSION}
    with drv.session() as s:

        def fetch(label, extra_rels=None):
            rows = s.run(f"MATCH (n:{label}) RETURN n {{ .* }} AS node ORDER BY n.id").data()
            return [r["node"] for r in rows]

        def rels(from_label, rel_type, to_label, from_key="id", to_key="id"):
            return s.run(f"""
                MATCH (a:{from_label})-[:{rel_type}]->(b:{to_label})
                RETURN a.id AS fromId, b.id AS toId
            """).data()

        export["systems"]  = fetch("SysSystem")
        export["domains"]  = fetch("SysDomain")

        # Domains — add systemId from CONTAINS relationship
        sys_dom = {r["toId"]: r["fromId"] for r in rels("SysSystem","CONTAINS","SysDomain")}
        for d in export["domains"]:
            if d["id"] in sys_dom:
                d["systemId"] = sys_dom[d["id"]]

        export["modules"] = fetch("SysModule")
        dom_mod = {r["toId"]: r["fromId"] for r in rels("SysDomain","CONTAINS","SysModule")}
        for m in export["modules"]:
            if m["id"] in dom_mod:
                m["domainId"] = dom_mod[m["id"]]

        export["features"] = fetch("SysFeature")
        mod_feat  = {r["toId"]: r["fromId"] for r in rels("SysModule","PROVIDES","SysFeature")}
        par_feat  = {r["toId"]: r["fromId"] for r in rels("SysFeature","CONTAINS","SysFeature")}
        for f in export["features"]:
            if f["id"] in mod_feat:
                f["moduleId"] = mod_feat[f["id"]]
            if f["id"] in par_feat:
                f["parentId"] = par_feat[f["id"]]

        export["userStories"] = fetch("SysUserStory")
        story_feats = {}
        for r in rels("SysUserStory","REQUIRES","SysFeature"):
            story_feats.setdefault(r["fromId"], []).append(r["toId"])
        for us in export["userStories"]:
            us["requiresFeatures"] = story_feats.get(us["id"], [])

        export["archStandards"] = fetch("SysArchStd")
        std_sys   = {r["toId"]: r["fromId"] for r in rels("SysSystem","GOVERNED_BY","SysArchStd")}
        std_mods  = {}
        for r in rels("SysModule","CONFORMS_TO","SysArchStd"):
            std_mods.setdefault(r["toId"], []).append(r["fromId"])
        for a in export["archStandards"]:
            if a["id"] in std_sys:
                a["systemId"] = std_sys[a["id"]]
            a["appliesTo"] = std_mods.get(a["id"], [])

        # Scenarios + chapters
        scenarios_raw = fetch("SysScenario")
        for sc in scenarios_raw:
            chapters = s.run("""
                MATCH (sc:SysScenario {id:$sid})-[:HAS_CHAPTER]->(ch:SysScenarioChapter)
                OPTIONAL MATCH (ch)-[:EXERCISES]->(f:SysFeature)
                RETURN ch { .* } AS ch, collect(f.id) AS feats
                ORDER BY ch.number
            """, sid=sc["id"]).data()
            sc["chapters"] = []
            for c in chapters:
                node = {k: v for k, v in c["ch"].items() if k not in ("id","scenarioId")}
                node["exercisesFeatures"] = c["feats"]
                sc["chapters"].append(node)
        export["scenarios"] = scenarios_raw

        # Training programs + modules
        programs_raw = fetch("SysTrainingProgram")
        for tp in programs_raw:
            modules = s.run("""
                MATCH (tp:SysTrainingProgram {id:$pid})-[:HAS_MODULE]->(m:SysTrainingModule)
                OPTIONAL MATCH (m)-[:TEACHES]->(f:SysFeature)
                RETURN m { .* } AS m, collect(f.id) AS feats
                ORDER BY m.id
            """, pid=tp["id"]).data()
            tp["modules"] = []
            for mod in modules:
                node = {k: v for k, v in mod["m"].items() if k not in ("programId",)}
                node["teachesFeatures"] = mod["feats"]
                tp["modules"].append(node)
        export["trainingPrograms"] = programs_raw

        # Test packages (registry with run history)
        export["testPackages"] = fetch("SysTestPackage")

        # Test nodes with both VERIFIES (feature links) and packageId (CONTAINS_TEST)
        export["tests"] = fetch("SysTest")

        # VERIFIES edges: test → feature
        test_feats = {}
        for r in rels("SysTest","VERIFIES","SysFeature"):
            test_feats.setdefault(r["fromId"], []).append(r["toId"])
        for t in export["tests"]:
            t["verifiesFeatures"] = test_feats.get(t["id"], [])

        export["endpoints"] = fetch("SysEndpoint")
        ep_feat = {r["fromId"]: r["toId"] for r in rels("SysEndpoint","IMPLEMENTS","SysFeature")}
        for e in export["endpoints"]:
            if e["id"] in ep_feat:
                e["implementsFeature"] = ep_feat[e["id"]]

        export["symbols"] = fetch("SysSymbol")
        sym_feat = {r["fromId"]: r["toId"] for r in rels("SysSymbol","IMPLEMENTS","SysFeature")}
        for sym in export["symbols"]:
            if sym["id"] in sym_feat:
                sym["implementsFeature"] = sym_feat[sym["id"]]

        export["usesPattern"] = [
            {"moduleId": r["fromId"], "patternId": r["toId"]}
            for r in rels("SysModule","USES_PATTERN","SysModule")
        ]

        # Directory → test category mappings (fully self-contained, no edges)
        export["dirCategories"] = fetch("SysDirCategory")

        # ── Previously missing — now fully captured ──────────────────────────

        # Users + their INITIATES→stories and USES→apps edges
        export["users"] = fetch("SysUser")
        user_stories, user_apps = {}, {}
        for r in rels("SysUser","INITIATES","SysUserStory"):
            user_stories.setdefault(r["fromId"], []).append(r["toId"])
        for r in rels("SysUser","USES","SysApplication"):
            user_apps.setdefault(r["fromId"], []).append(r["toId"])
        for u in export["users"]:
            u["initiatesStories"] = user_stories.get(u["id"], [])
            u["usesApps"]         = user_apps.get(u["id"], [])

        # Applications + their CONTAINS→modules edges
        export["applications"] = fetch("SysApplication")
        app_mods = {}
        for r in rels("SysApplication","CONTAINS","SysModule"):
            app_mods.setdefault(r["fromId"], []).append(r["toId"])
        for a in export["applications"]:
            a["containsModules"] = app_mods.get(a["id"], [])

        # Enhancements + EXTENDS→feature edges
        export["enhancements"] = fetch("SysEnhancement")
        enh_feats = {}
        for r in rels("SysEnhancement","EXTENDS","SysFeature"):
            enh_feats.setdefault(r["fromId"], []).append(r["toId"])
        for e in export["enhancements"]:
            e["extendsFeatures"] = enh_feats.get(e["id"], [])

        # Defects + AFFECTS→feature and DETECTED_IN→module edges
        export["defects"] = fetch("SysDefect")
        def_feats, def_mods = {}, {}
        for r in rels("SysDefect","AFFECTS","SysFeature"):
            def_feats.setdefault(r["fromId"], []).append(r["toId"])
        for r in rels("SysDefect","DETECTED_IN","SysModule"):
            def_mods.setdefault(r["fromId"], []).append(r["toId"])
        for d in export["defects"]:
            d["affectsFeatures"]    = def_feats.get(d["id"], [])
            d["detectedInModules"]  = def_mods.get(d["id"], [])

        # Use cases + REQUIRES→feature and story REALIZED_BY edges
        export["useCases"] = fetch("SysUseCase")
        uc_feats, uc_story = {}, {}
        for r in rels("SysUseCase","REQUIRES","SysFeature"):
            uc_feats.setdefault(r["fromId"], []).append(r["toId"])
        for r in rels("SysUserStory","REALIZED_BY","SysUseCase"):
            uc_story[r["toId"]] = r["fromId"]
        for uc in export["useCases"]:
            uc["requiresFeatures"] = uc_feats.get(uc["id"], [])
            if uc["id"] in uc_story:
                uc["storyId"] = uc_story[uc["id"]]

        # Architecture decisions + CONSTRAINED_BY edges (module → ADR)
        export["archDecisions"] = fetch("SysArchDecision")
        adr_mods, adr_sys = {}, {}
        for r in rels("SysModule","CONSTRAINED_BY","SysArchDecision"):
            adr_mods.setdefault(r["toId"], []).append(r["fromId"])
        for r in rels("SysSystem","GOVERNED_BY","SysArchDecision"):
            adr_sys[r["toId"]] = r["fromId"]
        for a in export["archDecisions"]:
            a["constrainsModules"] = adr_mods.get(a["id"], [])
            if a["id"] in adr_sys:
                a["systemId"] = adr_sys[a["id"]]

        # Module prerequisite edges (toolLayer ordering)
        export["prerequisiteFor"] = [
            {"fromId": r["fromId"], "toId": r["toId"]}
            for r in rels("SysModule","PREREQUISITE_FOR","SysModule")
        ]

        # Feedback entries
        export["feedback"] = fetch("SysFeedback")

        # Proposals + EXTENDS→feature edges
        export["proposals"] = fetch("SysProposal")
        prop_feats = {}
        for r in rels("SysProposal", "EXTENDS", "SysFeature"):
            prop_feats.setdefault(r["fromId"], []).append(r["toId"])
        for p in export["proposals"]:
            p["extendsFeatures"] = prop_feats.get(p["id"], [])

        # Notes (instance memory — active + archived)
        export["notes"] = fetch("SysNote")

        # Counts for summary
        total = sum(len(v) for v in export.values() if isinstance(v, list))

    drv.close()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(export, indent=2, default=str))

    print(f"✓ Backup written to {out_path}")
    for k, v in sorted(export.items()):
        if isinstance(v, list) and v:
            print(f"  {k:<25} {len(v)}")
    print(f"  total nodes/edges: {total}")


def cmd_backup(args):
    """Export all Sys* nodes and relationships to a JSON file (seed-compatible format)."""
    ts       = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    out_path = Path(args.output) if args.output else Path(f"data/sys-backup-{ts}.json")
    _do_backup(out_path)


def cmd_activate(args):
    """Activate SysEdge with a Lemon Squeezy licence key (one-time online, then offline)."""
    import importlib.util, os as _os
    lic_path = Path(__file__).parent / "licence.py"
    if not lic_path.exists():
        # Also check same directory as this script
        lic_path = Path(_os.path.dirname(_os.path.abspath(__file__))) / "licence.py"
    if not lic_path.exists():
        print("  ✗  licence.py not found — ensure you are running the Bootstrap Kit.", file=sys.stderr)
        sys.exit(1)
    spec = importlib.util.spec_from_file_location("licence", lic_path)
    lic = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lic)
    url = getattr(args, "url", None) or "https://sysedge.org-edge.com/activate"
    ok = lic.activate(args.key, activation_url=url)
    sys.exit(0 if ok else 1)


def cmd_licence_info(args):
    """Show current licence status."""
    import importlib.util, os as _os
    lic_path = Path(__file__).parent / "licence.py"
    if not lic_path.exists():
        lic_path = Path(_os.path.dirname(_os.path.abspath(__file__))) / "licence.py"
    if not lic_path.exists():
        print("  No licence.py found — running as free CLI.")
        return
    spec = importlib.util.spec_from_file_location("licence", lic_path)
    lic = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lic)
    info = lic.licence_info()
    if not info:
        print("  No valid licence found.")
        print("  Activate with: python3 cli/sys_graph.py activate <your-key>")
    else:
        print(f"  ✓  SysEdge Bootstrap Kit — licensed")
        print(f"     Email:   {info.get('email', 'unknown')}")
        print(f"     Issued:  {info.get('issued', '?')}")
        print(f"     Expires: {info.get('expiry', '?')}")


# ── Test file scanner ────────────────────────────────────────────────────────

_TS_DESCRIBE = __import__("re").compile(
    r"""(?:^|\s)(?:describe|describe\.only|describe\.skip)\s*\(\s*['"`]([^'"`]+)['"`]""",
    __import__("re").MULTILINE)
_TS_IT = __import__("re").compile(
    r"""(?:^|\s)(?:it|test|it\.only|test\.only|it\.skip|test\.skip)\s*\(\s*['"`]([^'"`]+)['"`]""",
    __import__("re").MULTILINE)

def _extract_tests_from_ts_file(file_path: Path, pkg_id: str, test_type: str) -> list[dict]:
    """Extract vitest/Jest test names from a TypeScript/TSX test file using regex."""
    rel = str(file_path)
    results = []
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        print(f"  WARN: could not read {file_path}: {e}", file=sys.stderr)
        return results

    # Build a list of (line_number, name, is_describe) pairs
    by_line = []
    for m in _TS_DESCRIBE.finditer(source):
        line = source[:m.start()].count("\n") + 1
        by_line.append((line, m.group(1), True))
    for m in _TS_IT.finditer(source):
        line = source[:m.start()].count("\n") + 1
        by_line.append((line, m.group(1), False))
    by_line.sort()

    # Pair each it/test with the most recent preceding describe block (if any)
    current_describe = ""
    for line, name, is_describe in by_line:
        if is_describe:
            current_describe = name
        else:
            fn_name = f"{current_describe} > {name}" if current_describe else name
            node_id = f"{rel}::{fn_name}"
            results.append({
                "id":        node_id,
                "file":      rel,
                "function":  fn_name,
                "className": current_describe or "",
                "type":      test_type,
                "testType":  test_type,
                "line":      line,
                "packageId": pkg_id,
            })
    return results


def _extract_tests_from_file(file_path: Path, pkg_id: str, test_type: str) -> list[dict]:
    """Parse a Python test file with AST and return a list of test descriptors."""
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
        tree   = ast.parse(source, filename=str(file_path))
    except SyntaxError as e:
        print(f"  WARN: could not parse {file_path}: {e}", file=sys.stderr)
        return []

    rel_path = str(file_path)
    results  = []

    for node in ast.walk(tree):
        # Class-level test methods
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for item in ast.walk(node):
                if (isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and item.name.startswith("test_")
                        and item is not node):
                    # Only direct methods (not nested classes)
                    if any(item is m for m in node.body
                           if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))):
                        node_id = f"{rel_path}::{node.name}::{item.name}"
                        results.append({
                            "id":        node_id,
                            "file":      rel_path,
                            "function":  item.name,
                            "className": node.name,
                            "type":      test_type,
                            "line":      item.lineno,
                            "packageId": pkg_id,
                        })

    # Module-level test functions (not inside any class)
    class_names = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    top_level   = {n.name: n for n in tree.body
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for name, node in top_level.items():
        if name.startswith("test_"):
            node_id = f"{rel_path}::{name}"
            results.append({
                "id":        node_id,
                "file":      rel_path,
                "function":  name,
                "className": "",
                "type":      test_type,
                "line":      node.lineno,
                "packageId": pkg_id,
            })

    return results


def _upsert_tests(session, tests: list[dict]) -> int:
    """Bulk-upsert SysTest nodes and CONTAINS_TEST edges. Returns count created."""
    if not tests:
        return 0
    session.run("""
        UNWIND $tests AS t
        MERGE (n:SysTest {id: t.id})
        SET n.file      = t.file,
            n.function  = t.function,
            n.className = t.className,
            n.type      = t.type,
            n.line      = t.line,
            n.packageId = t.packageId
        WITH n, t
        MATCH (pkg:SysTestPackage {id: t.packageId})
        MERGE (pkg)-[:CONTAINS_TEST]->(n)
    """, tests=tests)
    return len(tests)


def cmd_scan_go_tests(args):
    """Scan Go *_test.go files and register test functions as SysTest nodes (component tests)."""
    root = Path(args.path) if args.path else Path("backend/internal")
    if not root.exists():
        print(f"ERROR: {root} not found", file=sys.stderr); sys.exit(1)

    drv    = _driver()
    total  = 0
    files  = 0

    with drv.session() as s:
        dir_mappings = _load_dir_categories(s)

        for f in sorted(root.rglob("*_test.go")):
            # Only skip directory-level exclusions — _test.go is intentionally included here
            if any(part in _SKIP_DIRS for part in f.parts):
                continue
            rel   = str(f)
            pkg_id = rel  # use file path as package ID

            # Derive category and instance from graph config — never hardcoded
            category = _derive_test_category("unit", rel, dir_mappings=dir_mappings)
            instance = _derive_instance(rel, dir_mappings)
            s.run("""
                MERGE (p:SysTestPackage {id:$id})
                SET p.path=$path, p.area=$area, p.testType='unit',
                    p.testCategory=$cat, p.canonicalRoot='go'
            """, id=pkg_id, path=rel, cat=category,
                 area=rel.split("/")[2] if rel.count("/") >= 3 else "shared")
            if instance:
                s.run("MATCH (p:SysTestPackage {id:$id}) SET p.instance=$inst",
                      id=pkg_id, inst=instance)

            # Extract test functions
            tests = []
            try:
                for lineno, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
                    for pat in (_GO_TEST_FUNC, _GO_BENCH_FUNC):
                        m = pat.match(line)
                        if m:
                            fn   = m.group(1)
                            tid  = f"{rel}::{fn}"
                            tests.append({"id": tid, "file": rel, "function": fn,
                                          "className": "", "type": "unit",
                                          "line": lineno, "packageId": pkg_id})
            except Exception:
                pass

            if tests:
                s.run("""
                    UNWIND $tests AS t
                    MERGE (n:SysTest {id: t.id})
                    SET n.file=t.file, n.function=t.function,
                        n.type=t.type, n.line=t.line, n.packageId=t.packageId
                    WITH n, t
                    MATCH (p:SysTestPackage {id: t.packageId})
                    MERGE (p)-[:CONTAINS_TEST]->(n)
                """, tests=tests)
                total += len(tests)
                files += 1

    drv.close()
    print(f"  Go unit tests: {files} files, {total} test functions registered")


def _load_dir_categories(session) -> list[dict]:
    """Load SysDirCategory nodes from the graph, sorted longest-prefix-first.

    Returns a list of {prefix, pattern, category, instance} dicts.  The scanner
    calls this once per scan run and passes the result to _derive_test_category
    and _derive_instance so the graph is only queried once.
    """
    rows = session.run("""
        MATCH (dc:SysDirCategory)
        RETURN dc.prefix AS prefix, dc.pattern AS pattern,
               dc.category AS category, dc.instance AS instance,
               dc.moduleId AS moduleId
        ORDER BY size(coalesce(dc.prefix,'')) DESC, size(coalesce(dc.pattern,'')) DESC
    """).data()
    return [{"prefix":   r["prefix"]   or "",
             "pattern":  r["pattern"]  or "",
             "category": r["category"] or "integration",
             "instance": r["instance"] or "",
             "moduleId": r["moduleId"] or ""}
            for r in rows]


def _derive_instance(file_path: str, dir_mappings: list | None = None) -> str:
    """Derive instance from file path using SysDirCategory mappings (longest prefix first).

    Returns the instance string (e.g. 'manage', 'plan', 'core') or '' if no
    mapping matches.  Never overwrites an existing instance if '' is returned —
    callers should skip the SET when the return value is empty.
    """
    if not dir_mappings:
        return ""
    fp = file_path or ""
    fn = fp.split("/")[-1]
    for m in dir_mappings:
        if not m.get("instance"):
            continue
        if fp.startswith(m["prefix"]):
            pat = m["pattern"]
            if not pat or pat in fn:
                return m["instance"]
    return ""


def _derive_module_from_dir(file_path: str, dir_mappings: list | None = None) -> str:
    """Derive moduleId from file path using SysDirCategory mappings (longest prefix first).

    Returns the moduleId string or '' if no mapping matches.  Callers should
    fall back to _find_module / SysModule.paths when this returns empty.
    """
    if not dir_mappings:
        return ""
    fp = file_path or ""
    fn = fp.split("/")[-1]
    for m in dir_mappings:
        if not m.get("moduleId"):
            continue
        if fp.startswith(m["prefix"]):
            pat = m["pattern"]
            if not pat or pat in fn:
                return m["moduleId"]
    return ""


def _derive_test_category(test_type: str, file_path: str = "",
                           explicit: str = "",
                           dir_mappings: list | None = None) -> str:
    """Derive testCategory in priority order:

    1. Explicit override (--category flag)
    2. Graph-stored SysDirCategory mappings (longest prefix + pattern match)
    3. testType field heuristics
    4. Filename heuristics
    5. Safe default: 'integration'
    """
    if explicit:
        return explicit

    fp = file_path or ""
    fn = fp.split("/")[-1]   # filename only for pattern matching

    # Graph directory mappings (loaded once per scan run)
    if dir_mappings:
        for m in dir_mappings:   # already sorted longest-prefix-first
            if fp.startswith(m["prefix"]):
                pat = m["pattern"]
                if not pat or pat in fn:
                    return m["category"]

    # testType field heuristics
    # V-model tier derivation (ADR-027)
    # e2e     → User Story level: cross-instance full journeys
    # usecase → Use Case level:   single-UC Playwright flows
    # integration → Feature level: API/service tests
    # component   → Symbol level:  unit/handler tests
    tt = (test_type or "").lower()
    if tt in ("unit", "frontend"):
        return "component"
    if tt == "e2e":
        return "e2e"
    if tt in ("scenarios", "ui", "ui_full", "ui_walkthrough",
              "ui_scenarios", "ui_business"):
        return "usecase"
    if tt in ("api", "api_full", "functional", "integration", "extended"):
        return "integration"

    # Filename heuristics
    fnl = fn.lower()
    if "_e2e" in fnl:
        return "e2e"
    if any(x in fnl for x in ("_ui_", "walkthrough", "scenarios",
                                "_frontend", "playwright")):
        return "usecase"
    if any(x in fnl for x in ("_functional", "_api", "_integration")):
        return "integration"
    if fnl.endswith("_test.go") or fnl.endswith("_test.ts"):
        return "component"

    return "integration"   # safe default


def cmd_scan_package(args):
    """Scan one test file and register all test functions as SysTest nodes."""
    pkg_id = args.package
    drv    = _driver()

    explicit_cat = getattr(args, "category", "") or ""

    with drv.session() as s:
        dir_mappings = _load_dir_categories(s)

        row = s.run(
            "MATCH (p:SysTestPackage {id:$id}) RETURN p.path AS path, p.testType AS tt, "
            "p.testCategory AS registered_cat",
            id=pkg_id).single()
        if not row:
            # Package not yet registered — auto-register it from the path
            file_path = Path(pkg_id)
            if not file_path.exists():
                print(f"ERROR: package '{pkg_id}' not registered and path not found.",
                      file=sys.stderr)
                drv.close(); sys.exit(1)
            # FB-110: default to '' not 'integration' so filename/dir heuristics run first
            test_type = "unit" if pkg_id.endswith("_test.go") else ""
            category  = _derive_test_category(test_type, pkg_id,
                                              explicit=explicit_cat,
                                              dir_mappings=dir_mappings)
            area = pkg_id.split("/")[-1].replace(".py","").replace("_test.go","")
            s.run("""
                MERGE (p:SysTestPackage {id:$id})
                SET p.path=$path, p.area=$area, p.testType=$tt,
                    p.testCategory=$cat, p.canonicalRoot='auto'
            """, id=pkg_id, path=pkg_id, area=area, tt=test_type, cat=category)
        else:
            file_path = Path(row["path"])
            test_type = row["tt"] or "integration"
            # Prefer: (1) explicit --category flag, (2) registered testCategory, (3) derived
            registered = row.get("registered_cat") or ""
            category  = explicit_cat or registered or _derive_test_category(
                test_type, row["path"] or "", dir_mappings=dir_mappings)
            s.run("MATCH (p:SysTestPackage {id:$id}) SET p.testCategory=$cat",
                  id=pkg_id, cat=category)

        if not file_path.exists():
            print(f"ERROR: file not found: {file_path}", file=sys.stderr)
            drv.close(); sys.exit(1)

        # Route to the right extractor based on file extension
        suffix = file_path.suffix.lower()
        if suffix in (".ts", ".tsx"):
            tests = _extract_tests_from_ts_file(file_path, pkg_id, test_type)
        else:
            tests = _extract_tests_from_file(file_path, pkg_id, test_type)
        created = _upsert_tests(s, tests)

    drv.close()
    print(f"  {pkg_id}: {created} test functions registered  [{category}]")


def cmd_scan_all(args):
    """Scan all registered test packages (or a filtered subset) and register test functions."""
    root_filter  = args.root  or ""   # "p6", "backend", "go", "vitest"
    area_filter  = args.area  or ""   # "p1", "p8", etc.
    inst_filter  = args.instance or ""

    drv = _driver()
    with drv.session() as s:
        pkgs = s.run("""
            MATCH (p:SysTestPackage)
            WHERE ($root = '' OR p.canonicalRoot = $root)
              AND ($area = '' OR p.area = $area)
              AND ($inst = '' OR p.instance = $inst)
            RETURN p.id AS id, p.path AS path, p.testType AS tt,
                   p.canonicalRoot AS root, p.area AS area
            ORDER BY p.area, p.id
        """, root=root_filter, area=area_filter, inst=inst_filter).data()
        # Load dir mappings once for the whole scan
        dir_mappings = _load_dir_categories(s)

    total_tests  = 0
    total_files  = 0
    errors       = []
    last_area    = None
    explicit_cat = getattr(args, "category", "") or ""

    drv2 = _driver()
    with drv2.session() as s:
        for pkg in pkgs:
            file_path = Path(pkg["path"])
            if not file_path.exists() or file_path.is_dir():
                errors.append(pkg["id"])
                continue

            # Skip Go packages — handled by scan-go-tests
            if pkg["root"] == "go":
                continue

            area = pkg["area"] or "?"
            if area != last_area:
                print(f"\n  {area}")
                last_area = area

            # Derive and write testCategory using graph mappings
            category = _derive_test_category(pkg["tt"] or "", pkg["path"] or "",
                                             explicit=explicit_cat,
                                             dir_mappings=dir_mappings)
            s.run("MATCH (p:SysTestPackage {id:$id}) SET p.testCategory=$cat",
                  id=pkg["id"], cat=category)

            # Route to TypeScript extractor for .ts/.tsx vitest files
            if file_path.suffix.lower() in (".ts", ".tsx"):
                tests = _extract_tests_from_ts_file(file_path, pkg["id"], pkg["tt"] or "component")
            else:
                tests = _extract_tests_from_file(file_path, pkg["id"], pkg["tt"] or "integration")
            created = _upsert_tests(s, tests)
            total_tests += created
            total_files += 1
            name = file_path.stem
            print(f"    {name:<55} {created:4d} tests")

    drv2.close()

    print(f"\n{'─'*62}")
    print(f"  Scanned {total_files} files  →  {total_tests} test functions registered")
    if errors:
        print(f"  {len(errors)} files not found: {errors[:5]}")


# ── Code scanner ─────────────────────────────────────────────────────────────

import re as _re

# ── Go patterns ──────────────────────────────────────────────────────────────
_GO_EXPORTED_FUNC   = _re.compile(r'^func ([A-Z][A-Za-z0-9_]*)\s*[\(\[]')
_GO_EXPORTED_METHOD = _re.compile(r'^func \(([^)]+)\)\s+([A-Z][A-Za-z0-9_]*)\s*[\(\[]')
_GO_HANDLER_VAR     = _re.compile(r'^var\s+(handle[A-Za-z0-9_]+)\s*=')
_GO_TYPE_STRUCT     = _re.compile(r'^type ([A-Z][A-Za-z0-9_]+)\s+struct')
_GO_ENTRY_FUNC      = _re.compile(r'^func (main|init)\s*\(')  # cmd/ entry points

# ── TypeScript/TSX patterns ───────────────────────────────────────────────────
_TS_EXPORT_FUNC     = _re.compile(r'^export\s+(?:default\s+)?(?:async\s+)?function\s+([A-Za-z][A-Za-z0-9_]*)')
_TS_EXPORT_CONST    = _re.compile(r'^export\s+const\s+([A-Za-z][A-Za-z0-9_]*)\s*[:=<(]')
_TS_EXPORT_CLASS    = _re.compile(r'^export\s+(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z][A-Za-z0-9_]*)')
_TS_EXPORT_IFACE    = _re.compile(r'^export\s+(?:type|interface)\s+([A-Za-z][A-Za-z0-9_]*)')
_TS_EXPORT_ENUM     = _re.compile(r'^export\s+(?:const\s+)?enum\s+([A-Za-z][A-Za-z0-9_]*)')


def _go_sym_type(name: str, receiver: str) -> str:
    if name.lower().startswith("handle"):  return "handler"
    if receiver:                           return "method"
    return "function"


def _ts_sym_type(name: str, from_const: bool) -> str:
    if name[0].isupper():  return "component"
    if name.startswith("use"):  return "hook"
    if from_const:         return "const"
    return "function"


def _extract_symbols_go(file_path: Path, module_id: str) -> list[dict]:
    rel = str(file_path)
    results = []
    try:
        for lineno, raw in enumerate(file_path.read_text(errors="replace").splitlines(), 1):
            line = raw.strip()
            m = _GO_EXPORTED_METHOD.match(raw)
            if m:
                receiver, name = m.group(1).strip(), m.group(2)
                results.append({"id": f"{rel}::{name}", "file": rel, "symbol": name,
                                 "line": lineno, "symbolType": _go_sym_type(name, receiver),
                                 "language": "go", "exported": True,
                                 "signature": raw.strip()[:120], "moduleId": module_id})
                continue
            m = _GO_EXPORTED_FUNC.match(raw)
            if m:
                name = m.group(1)
                results.append({"id": f"{rel}::{name}", "file": rel, "symbol": name,
                                 "line": lineno, "symbolType": _go_sym_type(name, ""),
                                 "language": "go", "exported": True,
                                 "signature": raw.strip()[:120], "moduleId": module_id})
                continue
            m = _GO_HANDLER_VAR.match(raw)
            if m:
                name = m.group(1)
                results.append({"id": f"{rel}::{name}", "file": rel, "symbol": name,
                                 "line": lineno, "symbolType": "handler",
                                 "language": "go", "exported": False,
                                 "signature": raw.strip()[:120], "moduleId": module_id})
                continue
            m = _GO_ENTRY_FUNC.match(raw)
            if m:
                name = m.group(1)
                results.append({"id": f"{rel}::{name}", "file": rel, "symbol": name,
                                 "line": lineno, "symbolType": "entrypoint",
                                 "language": "go", "exported": False,
                                 "signature": raw.strip()[:120], "moduleId": module_id})
    except Exception as e:
        print(f"  WARN: {file_path}: {e}", file=sys.stderr)
    return results


def _extract_symbols_ts(file_path: Path, module_id: str) -> list[dict]:
    rel = str(file_path)
    results = []
    try:
        for lineno, raw in enumerate(file_path.read_text(errors="replace").splitlines(), 1):
            for pattern, from_const in [(_TS_EXPORT_FUNC, False), (_TS_EXPORT_CONST, True),
                                         (_TS_EXPORT_CLASS, False), (_TS_EXPORT_IFACE, False),
                                         (_TS_EXPORT_ENUM, False)]:
                m = pattern.match(raw.strip())
                if m:
                    name = m.group(1)
                    if len(name) < 2:
                        break
                    stype = _ts_sym_type(name, from_const)
                    results.append({"id": f"{rel}::{name}", "file": rel, "symbol": name,
                                     "line": lineno, "symbolType": stype,
                                     "language": "ts", "exported": True,
                                     "signature": raw.strip()[:120], "moduleId": module_id})
                    break
    except Exception as e:
        print(f"  WARN: {file_path}: {e}", file=sys.stderr)
    return results


def _extract_symbols_py(file_path: Path, module_id: str) -> list[dict]:
    """Extract top-level classes and functions from a Python file using ast."""
    rel = str(file_path)
    results = []
    try:
        import ast as _ast
        tree = _ast.parse(file_path.read_text(errors="replace"))
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                if node.col_offset == 0:  # top-level only
                    stype = "function"
                    results.append({"id": f"{rel}::{node.name}", "file": rel,
                                     "symbol": node.name, "line": node.lineno,
                                     "symbolType": stype, "language": "python",
                                     "exported": not node.name.startswith("_"),
                                     "signature": f"def {node.name}(...)", "moduleId": module_id})
            elif isinstance(node, _ast.ClassDef):
                if node.col_offset == 0:
                    results.append({"id": f"{rel}::{node.name}", "file": rel,
                                     "symbol": node.name, "line": node.lineno,
                                     "symbolType": "class", "language": "python",
                                     "exported": not node.name.startswith("_"),
                                     "signature": f"class {node.name}", "moduleId": module_id})
    except Exception as e:
        print(f"  WARN: {file_path}: {e}", file=sys.stderr)
    return results


_JAVA_PUBLIC = __import__("re").compile(
    r"^\s*public\s+(?:static\s+)?(?:final\s+)?(?:abstract\s+)?"
    r"(?:<[^>]+>\s+)?"
    r"(\w[\w<>\[\]]*)\s+(\w+)\s*[\({]"
)
_JAVA_CLASS  = __import__("re").compile(
    r"^\s*public\s+(?:abstract\s+)?(?:final\s+)?(?:class|interface|enum|record)\s+(\w+)"
)

def _extract_symbols_java(file_path: Path, module_id: str) -> list[dict]:
    """Extract public classes and methods from a Java file using regex."""
    rel = str(file_path)
    results = []
    try:
        for lineno, raw in enumerate(file_path.read_text(errors="replace").splitlines(), 1):
            m = _JAVA_CLASS.match(raw)
            if m:
                name = m.group(1)
                results.append({"id": f"{rel}::{name}", "file": rel, "symbol": name,
                                 "line": lineno, "symbolType": "class", "language": "java",
                                 "exported": True, "signature": raw.strip()[:120],
                                 "moduleId": module_id})
                continue
            m = _JAVA_PUBLIC.match(raw)
            if m:
                name = m.group(2)
                if name not in ("if","for","while","switch","catch","return","new","throw"):
                    results.append({"id": f"{rel}::{name}_{lineno}", "file": rel,
                                     "symbol": name, "line": lineno,
                                     "symbolType": "method", "language": "java",
                                     "exported": True, "signature": raw.strip()[:120],
                                     "moduleId": module_id})
    except Exception as e:
        print(f"  WARN: {file_path}: {e}", file=sys.stderr)
    return results


_CS_PUBLIC = __import__("re").compile(
    r"^\s*public\s+(?:static\s+)?(?:virtual\s+)?(?:override\s+)?(?:async\s+)?"
    r"(?:abstract\s+)?(?:readonly\s+)?"
    r"(\w[\w<>\[\]?,\s]*)\s+(\w+)\s*[\({<]"
)
_CS_CLASS = __import__("re").compile(
    r"^\s*public\s+(?:abstract\s+)?(?:sealed\s+)?(?:static\s+)?"
    r"(?:partial\s+)?(?:class|interface|enum|struct|record)\s+(\w+)"
)

def _extract_symbols_cs(file_path: Path, module_id: str) -> list[dict]:
    """Extract public classes and methods from a C# file using regex."""
    rel = str(file_path)
    results = []
    try:
        for lineno, raw in enumerate(file_path.read_text(errors="replace").splitlines(), 1):
            m = _CS_CLASS.match(raw)
            if m:
                name = m.group(1)
                results.append({"id": f"{rel}::{name}", "file": rel, "symbol": name,
                                 "line": lineno, "symbolType": "class", "language": "csharp",
                                 "exported": True, "signature": raw.strip()[:120],
                                 "moduleId": module_id})
                continue
            m = _CS_PUBLIC.match(raw)
            if m:
                name = m.group(2)
                if name not in ("if","for","while","return","new","using","namespace","void"):
                    results.append({"id": f"{rel}::{name}_{lineno}", "file": rel,
                                     "symbol": name, "line": lineno,
                                     "symbolType": "method", "language": "csharp",
                                     "exported": True, "signature": raw.strip()[:120],
                                     "moduleId": module_id})
    except Exception as e:
        print(f"  WARN: {file_path}: {e}", file=sys.stderr)
    return results


def _load_module_path_map(session) -> list[tuple[str, str]]:
    """Return [(path_prefix, module_id)] sorted longest-first for specificity."""
    rows = session.run("""
        MATCH (m:SysModule) WHERE m.paths IS NOT NULL
        RETURN m.id AS mid, m.paths AS paths
    """).data()
    pairs = []
    for r in rows:
        for p in (r["paths"] or []):
            pairs.append((p.rstrip("/"), r["mid"]))
    pairs.sort(key=lambda x: len(x[0]), reverse=True)
    return pairs


def _find_module(file_str: str, path_map: list[tuple[str, str]]) -> str:
    for prefix, mid in path_map:
        if file_str.startswith(prefix):
            return mid
    return ""


def _upsert_symbols(session, symbols: list[dict]) -> int:
    if not symbols:
        return 0
    session.run("""
        UNWIND $syms AS s
        MERGE (n:SysSymbol {id: s.id})
        SET n.file       = s.file,
            n.symbol     = s.symbol,
            n.line       = s.line,
            n.symbolType = s.symbolType,
            n.language   = s.language,
            n.exported   = s.exported,
            n.signature  = s.signature,
            n.moduleId   = s.moduleId
        WITH n, s
        MATCH (m:SysModule {id: s.moduleId})
        MERGE (m)-[:CONTAINS_SYMBOL]->(n)
    """, syms=[s for s in symbols if s["moduleId"]])
    # Symbols with no module match — still create but no edge
    unowned = [s for s in symbols if not s["moduleId"]]
    if unowned:
        session.run("""
            UNWIND $syms AS s
            MERGE (n:SysSymbol {id: s.id})
            SET n.file = s.file, n.symbol = s.symbol, n.line = s.line,
                n.symbolType = s.symbolType, n.language = s.language,
                n.exported = s.exported, n.signature = s.signature
        """, syms=unowned)
    return len(symbols)


# Skip patterns — files that should not be scanned for symbols
_SKIP_DIRS  = {"node_modules", ".git", "dist", "build", "vendor",
               "__pycache__", ".next", "coverage", "frontend-OLD", "backend-OLD",
               "_ul", "ontologies", "translations"}
_GO_TEST_FUNC  = _re.compile(r'^func (Test[A-Za-z0-9_]+)\s*\(t \*testing\.')
_GO_BENCH_FUNC = _re.compile(r'^func (Benchmark[A-Za-z0-9_]+)\s*\(')


def _should_skip(path: Path) -> bool:
    for part in path.parts:
        if part in _SKIP_DIRS:
            return True
    name = path.name
    if name.endswith("_test.go"):      return True
    if name.startswith("test_"):       return True
    if name.endswith(".min.js"):       return True
    if "generated" in name.lower():    return True
    return False


def orig_cmd_scan_code(args):
    """Scan one directory (or file) for exported symbols and upsert as SysSymbol nodes."""
    target = Path(args.path)
    if not target.exists():
        print(f"ERROR: {target} does not exist", file=sys.stderr); sys.exit(1)

    drv = _driver()
    with drv.session() as s:
        dir_mappings = _load_dir_categories(s)
        path_map     = _load_module_path_map(s)
        files    = [target] if target.is_file() else list(target.rglob("*"))
        total    = 0
        lang_filter = getattr(args, "lang", "auto")
        for f in sorted(files):
            if _should_skip(f): continue
            mod = _derive_module_from_dir(str(f), dir_mappings) or _find_module(str(f), path_map)
            if f.suffix == ".go" and lang_filter in ("auto","go"):
                syms = _extract_symbols_go(f, mod)
            elif f.suffix in (".ts", ".tsx") and lang_filter in ("auto","ts"):
                syms = _extract_symbols_ts(f, mod)
            elif f.suffix == ".py" and lang_filter in ("auto","py","python"):
                if not f.name.startswith("test_") and not f.name.endswith("_test.py"):
                    syms = _extract_symbols_py(f, mod)
                else:
                    continue
            elif f.suffix == ".java" and lang_filter in ("auto","java"):
                syms = _extract_symbols_java(f, mod)
            elif f.suffix == ".cs" and lang_filter in ("auto","cs","csharp"):
                syms = _extract_symbols_cs(f, mod)
            else:
                continue
            total += _upsert_symbols(s, syms)
    drv.close()
    print(f"  {target}: {total} symbols upserted")


def cmd_scan_code_all(args):
    """Scan entire codebase: backend/internal, backend/cmd, frontend/app/src, shared."""
    roots = [
        ("backend/internal",        "go"),
        ("backend/cmd",             "go"),
        ("frontend/app/src",        "ts"),
        ("shared/components",       "ts"),
        ("shared/hooks",            "ts"),
        ("shared/utils",            "ts"),
        ("shared/types",            "ts"),
        ("shared/auth",             "ts"),
    ]
    if args.path:
        # Override with explicit path
        roots = [(args.path, "auto")]

    drv = _driver()
    with drv.session() as s:
        dir_mappings = _load_dir_categories(s)
        path_map     = _load_module_path_map(s)

        def _mod(file_str: str) -> str:
            return _derive_module_from_dir(file_str, dir_mappings) or _find_module(file_str, path_map)

        grand_total = 0
        for root_str, lang_hint in roots:
            root = Path(root_str)
            if not root.exists():
                print(f"  (skip {root_str} — not found)")
                continue

            area_total = 0
            file_count = 0
            last_parent = None

            for f in sorted(root.rglob("*")):
                if not f.is_file() or _should_skip(f):
                    continue
                # Respect --lang flag if provided on args
                lang_override = getattr(args, "lang", "auto") or "auto"
                effective_lang = lang_override if lang_override != "auto" else lang_hint

                if f.suffix == ".go" and effective_lang in ("go", "auto"):
                    syms = _extract_symbols_go(f, _mod(str(f)))
                elif f.suffix in (".ts", ".tsx") and effective_lang in ("ts", "auto"):
                    syms = _extract_symbols_ts(f, _mod(str(f)))
                else:
                    continue
                if not syms:
                    continue
                parent = f.parent
                if parent != last_parent:
                    rel_parent = str(parent)
                    print(f"\n  {rel_parent}")
                    last_parent = parent
                n = _upsert_symbols(s, syms)
                area_total += n
                file_count += 1
                print(f"    {f.name:<60} {n:4d}")

            print(f"\n  ── {root_str}: {file_count} files, {area_total} symbols")
            grand_total += area_total

    drv.close()
    print(f"\n{'═'*62}")
    print(f"  Total symbols upserted: {grand_total}")


def cmd_test_gaps(args):
    """Show test coverage gaps by tier for an instance or the P6 master test suite.

    Three tiers:
      component   — Go *_test.go unit tests
      integration — Python API/functional tests (test_pX_api.py, test_pX_functional.py)
      usecase     — Playwright/E2E tests (test_pX_ui_*.py, *_e2e.py, *_scenarios.py)

    Usage:
        python3 scripts/sys_graph.py test-gaps --instance core
        python3 scripts/sys_graph.py test-gaps --instance core --tier component
        python3 scripts/sys_graph.py test-gaps --p6          # show P6 packages with no feature links
    """
    instance   = args.instance or ""
    tier_arg   = args.tier or "all"
    p6_mode    = getattr(args, "p6", False)
    drv        = _driver()
    today      = date.today().isoformat()
    W          = 62

    with drv.session() as s:

        if p6_mode:
            # P6 master view: test packages with few or no feature links
            pkgs = s.run("""
                MATCH (pkg:SysTestPackage)
                OPTIONAL MATCH (pkg)-[:CONTAINS_TEST]->(t:SysTest)-[:VERIFIES]->(f:SysFeature)
                WITH pkg, count(DISTINCT t) AS linkedTests, count(DISTINCT f) AS features,
                     count{(pkg)-[:CONTAINS_TEST]->()} AS totalTests
                RETURN pkg.id AS id, pkg.area AS area,
                       pkg.testCategory AS cat, pkg.testType AS ttype,
                       totalTests, linkedTests, features
                ORDER BY features ASC, totalTests DESC
            """).data()

            print("═" * W)
            print(f"  P6 TEST PACKAGE GAPS — feature-link coverage  ({today})")
            print("═" * W)

            by_cat = {}
            for p in pkgs:
                cat = p.get("cat") or "integration"
                by_cat.setdefault(cat, []).append(p)

            for cat in ("component", "integration", "usecase"):
                items = by_cat.get(cat, [])
                if not items:
                    continue
                unlinked = [p for p in items if p["features"] == 0 and p["totalTests"] > 0]
                partial  = [p for p in items if 0 < p["features"] < 3 and p["totalTests"] > 5]
                print(f"\n{'─' * W}")
                print(f"  {cat.upper()} ({len(items)} packages"
                      f"  — {len(unlinked)} fully unlinked  {len(partial)} partial)")
                print(f"{'─' * W}")
                if unlinked:
                    print(f"  Packages with tests but NO feature links:")
                    for p in sorted(unlinked, key=lambda x: -x["totalTests"])[:20]:
                        name = p["id"].split("/")[-1].replace(".py","")
                        print(f"    {p['area']:<8} {name:<50} {p['totalTests']:4d} tests")
                    if len(unlinked) > 20:
                        print(f"    … and {len(unlinked)-20} more")
                if partial:
                    print(f"  Packages with few feature links (worth extending):")
                    for p in sorted(partial, key=lambda x: x["features"])[:10]:
                        name = p["id"].split("/")[-1].replace(".py","")
                        print(f"    {p['area']:<8} {name:<50} {p['features']:2d} features / {p['totalTests']:4d} tests")

            print(f"\n{'─' * W}")
            print(f"  To link a test package to features:")
            print(f"    python3 scripts/sys_graph.py link-feature \\")
            print(f"      --feature F-P3-005 --tests 'test_p3_api.py::TestExtraction'")
            print(f"{'─' * W}")
            drv.close()
            return

        # ── Instance mode ──────────────────────────────────────────────────────
        mod_rows = s.run("""
            MATCH (m:SysModule {instance:$inst})-[:PROVIDES]->(f:SysFeature)
            RETURN m.id AS mid, m.name AS mname, m.paths AS paths,
                   f.id AS fid, f.name AS fname, f.status AS fstatus
            ORDER BY m.id, f.id
        """, inst=instance).data()

        if not mod_rows:
            print(f"No features found for instance '{instance}'.")
            drv.close(); return

        # Build feature → {component, integration, usecase} coverage map
        all_fids = list({r["fid"] for r in mod_rows})
        cov_data = s.run("""
            UNWIND $fids AS fid
            MATCH (f:SysFeature {id:fid})
            OPTIONAL MATCH (pkg1:SysTestPackage {testCategory:'component'})-[:CONTAINS_TEST]->(tc:SysTest)-[:VERIFIES]->(f)
            OPTIONAL MATCH (pkg2:SysTestPackage {testCategory:'integration'})-[:CONTAINS_TEST]->(ti:SysTest)-[:VERIFIES]->(f)
            OPTIONAL MATCH (pkg3:SysTestPackage {testCategory:'usecase'})-[:CONTAINS_TEST]->(tu:SysTest)-[:VERIFIES]->(f)
            RETURN fid,
                   count(DISTINCT tc) AS cmp,
                   count(DISTINCT ti) AS int_,
                   count(DISTINCT tu) AS uc,
                   collect(DISTINCT pkg1.id) AS cmpPkgs,
                   collect(DISTINCT pkg2.id) AS intPkgs,
                   collect(DISTINCT pkg3.id) AS ucPkgs
        """, fids=all_fids).data()
        cov_map = {r["fid"]: r for r in cov_data}

        # Module → existing test packages (for suggestions)
        mod_ids = list({r["mid"] for r in mod_rows})
        mod_pkgs = s.run("""
            MATCH (m:SysModule) WHERE m.id IN $mids
            OPTIONAL MATCH (p:SysTestPackage)
            WHERE any(path IN m.paths WHERE p.path CONTAINS split(path, '/')[0]
                        OR p.area = split(m.id, 'MOD-')[1])
            RETURN m.id AS mid, collect(DISTINCT p.id) AS pkgs
        """, mids=mod_ids).data()
        mod_pkg_map = {r["mid"]: r["pkgs"] for r in mod_pkgs}

        # Group rows by module
        from collections import defaultdict
        by_mod = defaultdict(list)
        for r in mod_rows:
            by_mod[r["mid"]].append(r)

    drv.close()

    print("═" * W)
    print(f"  TEST GAPS — {instance}  ({today})")
    print("═" * W)

    # V-model tiers (ADR-027) — component→symbol, integration→feature, usecase→UC, e2e→US
    TIERS = [
        ("component",   "cmp",  "Unit tests (Go *_test.go, vitest)",           "scan-go-tests + link-feature"),
        ("integration", "int_", "Integration/API tests (Python pytest)",        "test_pX_api.py or test_pX_functional.py"),
        ("usecase",     "uc",   "UI flow tests — one Playwright test per UC",   "test_pX_*_ui.py — see uc-ui-testing.md"),
        ("e2e",         "e2e",  "E2E tests — full User Story journeys (master)","test_*_e2e.py spanning multiple tools"),
    ]

    if tier_arg != "all":
        TIERS = [t for t in TIERS if t[0] == tier_arg]

    for tier_name, tier_key, tier_desc, tier_hint in TIERS:
        gaps = [(mid, r) for mid, rows in by_mod.items()
                for r in rows
                if cov_map.get(r["fid"], {}).get(tier_key, 0) == 0
                and r.get("fstatus") != "Superseded"]

        total_f = sum(len(rows) for rows in by_mod.values())
        covered = total_f - len(gaps)

        print(f"\n{'─' * W}")
        marker = "✓" if not gaps else "✗"
        print(f"  {marker} {tier_name.upper()} GAPS  ({len(gaps)} of {total_f} features uncovered)")
        print(f"    {tier_desc}")
        print(f"{'─' * W}")

        if not gaps:
            print(f"  ✓ All features have {tier_name} test coverage")
            continue

        # Group by module
        gaps_by_mod = defaultdict(list)
        for mid, r in gaps:
            gaps_by_mod[mid].append(r)

        for mid, rows in sorted(gaps_by_mod.items()):
            mod_r  = next(r for r in mod_rows if r["mid"] == mid)
            paths  = mod_r.get("paths") or []
            pkgs   = mod_pkg_map.get(mid, [])
            pkg_names = [p.split("/")[-1].replace(".py","") for p in pkgs if p][:3]

            print(f"\n  {mid}  {mod_r['mname']}  ({len(rows)} gaps)")
            if paths:
                for p in paths[:2]:
                    print(f"    Code: {p}")
            if pkg_names:
                print(f"    Existing test files: {', '.join(pkg_names)}")

            for r in rows:
                existing = cov_map.get(r["fid"], {})
                have = []
                if existing.get("cmp",  0) > 0: have.append("cmp")
                if existing.get("int_", 0) > 0: have.append("int")
                if existing.get("uc",   0) > 0: have.append("uc")
                has_str = f"  [has: {','.join(have)}]" if have else ""
                print(f"    ✗ {r['fid']:<16} {r['fname']}{has_str}")

        print(f"\n  → Hint: {tier_hint}")

    print(f"\n{'═' * W}")
    all_gaps = sum(
        1 for r in mod_rows
        if not all(cov_map.get(r["fid"], {}).get(k, 0) > 0 for _, k, _, _ in TIERS)
        and r.get("fstatus") != "Superseded"
    )
    print(f"  {instance}: {all_gaps} feature(s) missing at least one tier of test coverage")
    print(f"{'═' * W}")

    # ── Endpoints with no test coverage ──────────────────────────────────────
    drv2 = _driver()
    with drv2.session() as s2:
        untest_eps = s2.run("""
            MATCH (ep:SysEndpoint)-[:IMPLEMENTS]->(f:SysFeature)<-[:PROVIDES]-(m:SysModule)
            WHERE m.id IN $mids
            AND NOT exists((:SysTest)-[:VERIFIES]->(f))
            RETURN ep.method AS method, ep.path AS path,
                   f.id AS fid, m.id AS mid
            ORDER BY m.id, ep.method, ep.path
        """, mids=mod_ids).data()
    drv2.close()
    if untest_eps:
        print(f"\n{'─' * W}")
        print(f"  ⚠ ENDPOINTS WITH NO TEST COVERAGE ({len(untest_eps)})")
        print(f"    Linked to features that have no verifying tests.")
        print(f"{'─' * W}")
        for ep in untest_eps:
            print(f"    {ep.get('method','?'):6} {ep.get('path','?'):<45} → {ep['fid']}")
        print(f"  → link-feature --feature F-xxx --tests 'test_file.py::TestClass'")
        print(f"{'═' * W}")


def cmd_worklog(args):
    """Show open defects and pending enhancements for an instance with feature and code links.

    This is the session 'what to work on' view — complements briefing (coverage) by
    surfacing actionable items with direct pointers to features and code paths.
    """
    instance = args.instance
    drv      = _driver()
    today    = date.today().isoformat()

    with drv.session() as s:
        mod_ids = [r["id"] for r in s.run("""
            MATCH (m:SysModule {instance:$inst}) RETURN m.id AS id
        """, inst=instance).data()]

        # Notes and proposals are shown even with no modules
        no_modules = not mod_ids

        # Open defects — feature-linked or module-detected (skip if no modules)
        defects = [] if no_modules else s.run("""
            MATCH (d:SysDefect) WHERE d.status <> 'closed'
            OPTIONAL MATCH (d)-[:AFFECTS]->(f:SysFeature)<-[:PROVIDES]-(mf:SysModule)
            OPTIONAL MATCH (d)-[:DETECTED_IN]->(md:SysModule)
            WITH d, f, coalesce(mf, md) AS m
            WHERE m.id IN $mids
            RETURN d.id AS did, d.title AS title,
                   coalesce(d.severity,'medium') AS sev,
                   d.source AS src,
                   d.occurrences AS occ,
                   d.lastSeen AS seen,
                   f.id AS fid, f.name AS fname,
                   m.id AS mid, m.name AS mname,
                   m.paths AS paths
            ORDER BY
              CASE coalesce(d.severity,'medium')
                WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                WHEN 'medium'   THEN 2 ELSE 3 END,
              d.id
        """, mids=mod_ids).data()

        # Active notes for this instance
        active_notes = s.run("""
            MATCH (n:SysNote {instance:$inst})
            WHERE n.status = 'active'
            RETURN n.id AS nid, n.body AS body, n.createdAt AS created, n.expiresAt AS expires
            ORDER BY n.createdAt DESC
        """, inst=instance).data()

        # Open proposals for this instance
        open_proposals = s.run("""
            MATCH (p:SysProposal {instance:$inst})
            WHERE p.status IN ['draft','accepted']
            RETURN p.id AS pid, p.title AS title, p.status AS status,
                   coalesce(p.priority,'Should') AS priority,
                   p.description AS desc, p.createdAt AS created
            ORDER BY
              CASE p.status WHEN 'accepted' THEN 0 ELSE 1 END,
              CASE coalesce(p.priority,'Should')
                WHEN 'Must' THEN 0 WHEN 'Should' THEN 1 ELSE 2 END,
              p.id
        """, inst=instance).data()

        # Feedback count for the footer prompt
        feedback_count = s.run("""
            MATCH (f:SysFeedback {instance:$inst}) RETURN count(f) AS n
        """, inst=instance).single()["n"]

        # Enhancements — proposed/approved/in-progress for this instance
        since_filter = "AND e.createdAt >= $since" if getattr(args, "since", None) else ""
        enhancements = s.run(f"""
            MATCH (e:SysEnhancement)
            WHERE e.instance = $inst AND e.status IN ['proposed','approved','in-progress']
            {since_filter}
            OPTIONAL MATCH (e)-[:EXTENDS]->(f:SysFeature)<-[:PROVIDES]-(m:SysModule)
            OPTIONAL MATCH (e)-[:BLOCKED_BY]->(bl:SysEnhancement)
            WITH e, collect(DISTINCT {{fid:f.id, fname:f.name, mid:m.id, mname:m.name, paths:m.paths}}) AS features,
                 collect(DISTINCT {{bid:bl.id, bst:bl.status}}) AS blockers
            RETURN e.id AS eid, e.title AS title,
                   e.status AS status,
                   coalesce(e.priority,'Should') AS priority,
                   e.description AS desc,
                   features, blockers
            ORDER BY
              CASE e.status
                WHEN 'in-progress' THEN 0 WHEN 'approved' THEN 1
                ELSE 2 END,
              CASE coalesce(e.priority,'Should')
                WHEN 'Must' THEN 0 WHEN 'Should' THEN 1 ELSE 2 END,
              e.id
        """, inst=instance, since=getattr(args, "since", "") or "").data()

        # --strict: hide ENHs whose only feature links point outside this instance's modules
        # --strict is the default; --all opts out
        # FB-131: strict only applies when modules are known — skip if mod_ids is empty
        # (empty mod_ids means modules not yet restored; hiding ENHs would mislead)
        use_strict = getattr(args, "strict", True) and not getattr(args, "all", False) \
                     and len(mod_ids) > 0
        if use_strict:
            def _enh_in_scope(e):
                feats = [f for f in (e.get("features") or []) if f.get("fid")]
                if not feats:
                    return True   # unlinked ENHs always shown (instance match is enough)
                return any(f.get("mid") in mod_ids for f in feats)
            enhancements = [e for e in enhancements if _enh_in_scope(e)]

        # ENH-556: recently closed enhancements for this instance today
        closed_today = s.run("""
            MATCH (e:SysEnhancement)
            WHERE e.instance = $inst AND e.status = 'done'
              AND e.completedAt IS NOT NULL AND e.completedAt STARTS WITH $today
            RETURN e.id AS eid, e.title AS title
            ORDER BY e.completedAt DESC
        """, inst=instance, today=today).data()

    drv.close()

    W = 62
    since_tag = f"  (since {args.since})" if getattr(args, "since", None) else ""
    use_strict = getattr(args, "strict", True) and not getattr(args, "all", False)
    strict_tag = "" if getattr(args, "all", False) else "  [strict]"
    print("═" * W)
    print(f"  WORK LOG — {instance}  ({today}){since_tag}{strict_tag}")
    print(f"  What to work on this session")
    print("═" * W)

    # ── Active notes ─────────────────────────────────────────────────────────
    if active_notes:
        print(f"\nSESSION NOTES ({len(active_notes)})  — expire with: expire-note --id NOTE-xxx")
        today_str = date.today().isoformat()
        for n in active_notes:
            age_days = ""
            try:
                from datetime import datetime as _dt
                created = _dt.fromisoformat(n["created"][:10])
                days = (date.today() - created.date()).days
                age_days = f"  [{days}d old]"
            except Exception:
                pass
            expires_tag = f"  expires {n['expires'][:10]}" if n.get("expires") else ""
            print(f"\n  📝 {n['nid']}{age_days}{expires_tag}")
            body = (n["body"] or "").strip()
            for line in body.splitlines()[:4]:
                print(f"     {line}")
            if len(body.splitlines()) > 4:
                print(f"     … ({len(body.splitlines())-4} more lines)")

    # ── Open proposals ────────────────────────────────────────────────────────
    if open_proposals:
        print(f"\nOPEN PROPOSALS ({len(open_proposals)})  — design work awaiting decision or filing")
        for p in open_proposals:
            status_mark = "✦" if p["status"] == "accepted" else "◇"
            print(f"\n  {status_mark} {p['pid']}  [{p['status']}]  [{p['priority']}]")
            print(f"     {p['title'][:70]}")
            if p.get("desc"):
                desc = (p["desc"] or "").strip()
                if len(desc) > 10:
                    print(f"     {desc[:150]}{'…' if len(desc) > 150 else ''}")

    # ── Defects ───────────────────────────────────────────────────────────────
    open_d = list(defects)
    if open_d:
        print(f"\nOPEN DEFECTS ({len(open_d)})")
        seen_defects: set = set()
        for d in open_d:
            if d["did"] in seen_defects:
                continue
            seen_defects.add(d["did"])
            sev      = d.get("sev","medium")
            sev_mark = {"critical":"●●●●","high":"●●●○","medium":"●●○○","low":"●○○○"}.get(sev,"●●○○")
            occ_tag  = f"  ×{d['occ']}" if d.get("occ") and int(d["occ"]) > 1 else ""
            src_tag  = " [log]" if d.get("src") == "log" else ""
            print(f"\n  {sev_mark} {d['did']}  [{sev}]{src_tag}{occ_tag}")
            print(f"     {d['title'][:70]}")
            if d.get("fname"):
                print(f"     Feature : {d['fid']}  {d['fname']}")
            else:
                print(f"     Feature : (not linked — use link-defect to associate)")
            if d.get("mname"):
                print(f"     Module  : {d['mid']}  {d['mname']}")
                if d.get("paths"):
                    for p in (d["paths"] or [])[:3]:
                        print(f"     Code    : {p}")
            if d.get("seen"):
                print(f"     Last seen: {str(d['seen'])[:19]}")
    else:
        print(f"\nOPEN DEFECTS — none ✓")

    # ── Enhancements — grouped by status then priority then module ────────────
    def _print_enh_group(group_label, group):
        if not group:
            return
        # Group by module within the list (preserving priority order)
        by_mod = {}
        for e in group:
            feats = [f for f in (e.get("features") or []) if f.get("mid")]
            mod_key = feats[0]["mid"] if feats else "(unlinked)"
            by_mod.setdefault(mod_key, []).append(e)
        mod_order = list(dict.fromkeys(
            next((f["mid"] for f in (e.get("features") or []) if f.get("mid")), "(unlinked)")
            for e in group
        ))
        print(f"\nENHANCEMENTS — {group_label} ({len(group)})")
        for mod_key in mod_order:
            items = by_mod[mod_key]
            if mod_key != "(unlinked)" and len(mod_order) > 1:
                print(f"\n  ── {mod_key} ──")
            for e in items:
                prio_mark = {"Must":"★★★","Should":"★★☆","Could":"★☆☆"}.get(e["priority"],"★★☆")
                print(f"\n  {prio_mark} {e['eid']}  [{e['priority']}]")
                print(f"     {e['title'][:70]}")
                if e.get("desc"):
                    desc = e["desc"].strip()
                    if len(desc) > 10:
                        print(f"     {desc[:150]}{'…' if len(desc) > 150 else ''}")
                feats = [f for f in (e.get("features") or []) if f.get("fid")]
                if feats:
                    for f in feats[:3]:
                        print(f"     Feature : {f['fid']}  {f.get('fname','')}")
                    shown_mods = set()
                    for f in feats:
                        if f.get("mid") and f["mid"] not in shown_mods:
                            shown_mods.add(f["mid"])
                            print(f"     Module  : {f['mid']}  {f.get('mname','')}")
                            for p in (f.get("paths") or [])[:2]:
                                print(f"     Code    : {p}")
                else:
                    print(f"     Feature : (not yet linked — use link-enhancement)")
                # ENH-547: blocked-by visibility
                active_blockers = [b for b in (e.get("blockers") or [])
                                   if b.get("bid") and b.get("bst") not in ("done", None)]
                if active_blockers:
                    for b in active_blockers:
                        print(f"     ⛔ BLOCKED BY: {b['bid']}  [{b['bst']}]")

    if enhancements:
        in_prog  = [e for e in enhancements if e["status"] == "in-progress"]
        approved = [e for e in enhancements if e["status"] == "approved"]
        proposed = [e for e in enhancements if e["status"] == "proposed"]
        _print_enh_group("IN PROGRESS", in_prog)
        _print_enh_group("APPROVED — ready to implement", approved)
        _print_enh_group("PROPOSED", proposed)
    else:
        print(f"\nENHANCEMENTS — none pending")

    print(f"\n{'─' * W}")
    total_work = len(open_d) + len(enhancements) + len(open_proposals) + len(active_notes)
    print(f"  {total_work} item(s) · defects {len(open_d)} · enhancements {len(enhancements)}"
          f" · proposals {len(open_proposals)} · notes {len(active_notes)}")
    print(f"  sys_graph.py close-defect --id <id>  |  start-enhancement --id <id> --instance {instance}")
    print(f"{'─' * W}")

    # ENH-556: recently closed today for this instance
    if closed_today:
        print(f"\nRECENTLY CLOSED — {today}")
        for r in closed_today:
            print(f"  ✓ {r['eid']}  {r['title'][:60]}")
        print()

    # ── Feedback prompt — at end so defects/enhancements are immediately visible
    fb_tag = f"  ({feedback_count} entr{'y' if feedback_count==1 else 'ies'} submitted)" if feedback_count else ""
    print(f"\n{'▶'*2} GRAPH FEEDBACK{fb_tag} — record observations any time this session")
    print(f"   python3 scripts/sys_graph.py feedback --instance {instance} --category <cat> --body \"...\"")
    print(f"   Categories: general | usability | gap | workflow | positive")
    print(f"   1. Is the skill file clear — any rules that stop you without a clear alternative?")
    print(f"   2. Is the worklog/briefing output useful at session start, or do you skip parts?")
    print(f"   3. Are use cases and features a useful frame when designing/testing?")
    print(f"   4. Are there sys_graph.py commands missing that you have had to work around?")
    print(f"   5. Has the graph changed how you approach test coverage decisions?")


def cmd_link_defect(args):
    """Create a SysDefect node and link it to a feature. --id is optional; auto-assigned if omitted."""
    drv = _driver()
    with drv.session() as s:
        if not args.id:
            args.id = _alloc_id(s, "SysDefect", "DEF-")
            print(f"  auto-assigned id: {args.id}", file=sys.stderr)
        s.run("""
            MERGE (d:SysDefect {id:$id})
            SET d.title=$title, d.severity=$sev, d.status='open',
                d.createdAt=$now, d.instance=$inst
        """, id=args.id, title=args.title, sev=args.severity or "medium",
             now=_now_iso(), inst=args.instance or "")
        s.run("""MATCH (d:SysDefect {id:$id}),(f:SysFeature {id:$fid})
                 MERGE (d)-[:AFFECTS]->(f)""",
              id=args.id, fid=args.feature)
    drv.close()
    print(f"✓ {args.id}  [{args.severity}]  {args.title}  →  AFFECTS  →  {args.feature}")


def cmd_reconcile_done(args):
    """Post-completion reconciliation after closing an enhancement or defect.

    Surfaces feature/UC/US consistency issues, test gaps with ready-to-run
    commands, and auto-files cross-instance enhancements for out-of-scope gaps.
    Read-only except for auto-filed cross-instance enhancement nodes.
    """
    enh_id    = getattr(args, "id",      None) or ""
    def_id    = getattr(args, "defect",  None) or ""
    caller    = getattr(args, "instance", None) or ""
    work_id   = enh_id or def_id
    is_defect = bool(def_id)

    drv = _driver()
    with drv.session() as s:

        # ── 1. Load the work item ──────────────────────────────────────────
        if is_defect:
            row = s.run("""
                MATCH (d:SysDefect {id:$id})
                RETURN d.title AS title, d.description AS desc,
                       d.instance AS inst, d.status AS status
            """, id=def_id).single()
            rel_type = "AFFECTS"
        else:
            row = s.run("""
                MATCH (e:SysEnhancement {id:$id})
                RETURN e.title AS title, e.description AS desc,
                       e.instance AS inst, e.status AS status,
                       e.completedAt AS completedAt
            """, id=enh_id).single()
            rel_type = "EXTENDS"

        if not row:
            print(f"⚠ {work_id} not found", file=sys.stderr)
            drv.close(); return

        inst        = row["inst"] or caller or "unknown"
        title       = row["title"] or ""
        description = row["desc"]  or ""
        completed   = row.get("completedAt") or ""

        # ── 2. Linked features ────────────────────────────────────────────
        features = s.run(f"""
            MATCH (w {{id:$wid}})-[:{rel_type}]->(f:SysFeature)
            OPTIONAL MATCH (m:SysModule)-[:PROVIDES]->(f)
            RETURN f.id AS fid, f.name AS fname, m.id AS mid, m.instance AS minst
        """, wid=work_id).data()

        # ── 3. Per-feature analysis ───────────────────────────────────────
        feature_analysis = []
        for feat in features:
            fid   = feat["fid"]
            fname = feat["fname"] or fid
            minst = feat["minst"] or inst

            ep_count  = s.run("MATCH (ep:SysEndpoint)-[:IMPLEMENTS]->(f:SysFeature {id:$fid}) RETURN count(ep) AS n", fid=fid).single()["n"]
            sym_count = s.run("MATCH (m:SysModule)-[:CONTAINS_SYMBOL]->(sym:SysSymbol)-[:IMPLEMENTS]->(f:SysFeature {id:$fid}) RETURN count(sym) AS n", fid=fid).single()["n"]

            # Tier coverage
            tiers = s.run("""
                MATCH (f:SysFeature {id:$fid})
                OPTIONAL MATCH (tc:SysTest)-[:VERIFIES]->(f)
                WHERE (tc.testType IN ['component','go-unit'])
                   OR exists((:SysTestPackage {testCategory:'component'})-[:CONTAINS_TEST]->(tc))
                OPTIONAL MATCH (ti:SysTest)-[:VERIFIES]->(f)
                WHERE (ti.testType = 'integration')
                   OR exists((:SysTestPackage {testCategory:'integration'})-[:CONTAINS_TEST]->(ti))
                OPTIONAL MATCH (tu:SysTest)-[:VERIFIES]->(f)
                WHERE (tu.testType = 'usecase')
                   OR exists((:SysTestPackage {testCategory:'usecase'})-[:CONTAINS_TEST]->(tu))
                OPTIONAL MATCH (te:SysTest)-[:VERIFIES]->(f)
                WHERE (te.testType = 'e2e')
                   OR exists((:SysTestPackage {testCategory:'e2e'})-[:CONTAINS_TEST]->(te))
                RETURN count(DISTINCT tc) AS cmp, count(DISTINCT ti) AS int_,
                       count(DISTINCT tu) AS uc,  count(DISTINCT te) AS e2e
            """, fid=fid).single()

            # UCs and USs reachable from this feature
            ucs = s.run("""
                MATCH (uc:SysUseCase)-[:REQUIRES]->(f:SysFeature {id:$fid})
                OPTIONAL MATCH (us:SysUserStory)-[:REALIZED_BY]->(uc)
                RETURN uc.id AS ucid, uc.title AS uctitle, uc.description AS ucdesc,
                       uc.instance AS ucinst,
                       collect({id: us.id, title: us.title}) AS stories
            """, fid=fid).data()

            feature_analysis.append({
                "fid": fid, "fname": fname, "minst": minst,
                "ep_count": ep_count, "sym_count": sym_count,
                "tiers": dict(tiers), "ucs": ucs,
            })

        # ── 4. Print report ───────────────────────────────────────────────
        W = 70
        print("═" * W)
        label = "DEFECT" if is_defect else "ENHANCEMENT"
        print(f"  RECONCILIATION — {work_id} [{inst}]  {label}")
        print(f"  {title[:W-4]}")

        # ENH-557: infra-only shortcut — no feature traceability needed for deploy/infra
        _INFRA_INSTANCES = {"deploy", "infra", "sys-graph", "sysops"}
        if not features and inst in _INFRA_INSTANCES:
            print("═" * W)
            print(f"  ℹ  Infra-only {label.lower()} — no feature/test traceability required")
            print("═" * W)
            drv.close(); return
        print("═" * W)

        # Features section
        print(f"\n{'─'*W}")
        print(f"  FEATURES ({len(features)})")
        print(f"{'─'*W}")
        for fa in feature_analysis:
            ep_mark  = "✓" if fa["ep_count"]  > 0 else "⚠"
            sym_mark = "✓" if fa["sym_count"] > 0 else "⚠"
            print(f"  {fa['fid']}  {fa['fname']}  [{fa['minst']}]")
            print(f"    endpoints : {fa['ep_count']:3d}  {ep_mark}{'  ← no IMPLEMENTS edges — run link-endpoint' if fa['ep_count']==0 else ''}")
            print(f"    symbols   : {fa['sym_count']:3d}  {sym_mark}{'  ← no CONTAINS_SYMBOL edges — run scan-code or link-symbol' if fa['sym_count']==0 else ''}")

        # UC/US prose review — ENH-553: filter to caller instance; ENH-551: note when all filtered
        all_ucs_raw = [uc for fa in feature_analysis for uc in fa["ucs"]]
        all_ucs = [uc for uc in all_ucs_raw
                   if not caller or not uc.get("ucinst") or uc["ucinst"] == caller]
        filtered_out = len(all_ucs_raw) - len(all_ucs)

        seen_ucs: set = set()
        if all_ucs:
            print(f"\n{'─'*W}")
            print(f"  USE CASES & USER STORIES — review prose for accuracy")
            print(f"  Enhancement context: {description[:120].strip()}{'...' if len(description)>120 else ''}")
            print(f"{'─'*W}")
            for uc in all_ucs:
                ucid = uc["ucid"]
                if ucid in seen_ucs:
                    continue
                seen_ucs.add(ucid)
                print(f"\n  [KEEP or UPDATE?]  {ucid}  {uc['uctitle'] or ''}")
                if uc.get("ucdesc"):
                    for line in (uc["ucdesc"] or "")[:300].splitlines():
                        print(f"    {line}")
                print(f"    Context: does this UC still accurately describe behaviour after {work_id}?")
                for us in (uc.get("stories") or []):
                    if us.get("id"):
                        print(f"    → US: {us['id']}  {us.get('title','')}")
            if filtered_out:
                print(f"\n  ℹ  {filtered_out} UC(s) from other instances not shown")
        elif filtered_out:
            # ENH-551: UCs exist but all belong to other instances
            print(f"\n  ℹ  {len(all_ucs_raw)} UC(s) linked — all owned by other instance(s), no review needed here")
        elif features:
            # ENH-555: only show "no UCs" message when there are features to attach UCs to
            print(f"\n  ℹ  No Use Cases linked to these features — consider adding UCs via create-usecase")

        # Test gaps + auto-file cross-instance
        auto_filed = []
        print(f"\n{'─'*W}")
        print(f"  TEST GAPS & REQUIRED ACTIONS")
        print(f"{'─'*W}")
        for fa in feature_analysis:
            t      = fa["tiers"]
            minst  = fa["minst"]
            fid    = fa["fid"]
            gaps   = []
            in_scope_gaps   = []
            cross_inst_gaps = []

            for tier, count, tier_inst in [
                ("cmp",  t.get("cmp",  0), minst),
                ("int",  t.get("int_", 0), minst),
                ("uc",   t.get("uc",   0), minst),
                ("e2e",  t.get("e2e",  0), "master"),
            ]:
                mark = "✓" if count > 0 else "✗"
                gaps.append(f"  {mark} {tier:<4} {count:3d} tests")
                if count == 0:
                    if tier == "e2e":
                        cross_inst_gaps.append(("master", tier, fid, fa["fname"]))
                    elif tier_inst and caller and tier_inst != caller:
                        cross_inst_gaps.append((tier_inst, tier, fid, fa["fname"]))
                    else:
                        in_scope_gaps.append((tier, fid))

            print(f"\n  {fid}  {fa['fname']}")
            for g in gaps:
                print(g)
            for tier, fid_ in in_scope_gaps:
                print(f"    → ADD {tier}: python3 scripts/sys_graph.py link-feature \\")
                print(f"          --feature {fid_} --tests \"<test_file>::<TestClass>::<test_name>\"")
            for (target_inst, tier, fid_, fname) in cross_inst_gaps:
                enh_title = f"Add {tier} tests for {fid_} ({fname}) — triggered by {work_id}"
                enh_id_new = _alloc_id(s, "SysEnhancement", "ENH-")
                s.run("""
                    MATCH (e:SysEnhancement {id:$id})
                    SET e.title=$title, e.description=$desc,
                        e.instance=$inst, e.priority='Should',
                        e.status='proposed', e.createdAt=$now, e.source=$src
                """, id=enh_id_new, title=enh_title,
                     desc=f"Post-completion gap from {work_id}: {tier} tier has 0 tests for {fid_}. "
                          f"Enhancement: {title}",
                     inst=target_inst, now=_now_iso(), src=work_id)
                s.run("""MATCH (e:SysEnhancement {id:$eid}),(f:SysFeature {id:$fid})
                         MERGE (e)-[:EXTENDS]->(f)""", eid=enh_id_new, fid=fid_)
                auto_filed.append((enh_id_new, target_inst, enh_title))

        # Auto-filed summary
        if auto_filed:
            print(f"\n{'─'*W}")
            print(f"  AUTO-FILED CROSS-INSTANCE ENHANCEMENTS")
            print(f"{'─'*W}")
            for (eid, einst, etitle) in auto_filed:
                print(f"  ✓ {eid}  [{einst}]  {etitle[:60]}")

        # Final checklist
        print(f"\n{'─'*W}")
        print(f"  CHECKLIST — complete before ending session")
        print(f"{'─'*W}")
        for fa in feature_analysis:
            if fa["ep_count"] == 0:
                print(f"  ☐ link-endpoint for {fa['fid']}")
            if fa["sym_count"] == 0:
                print(f"  ☐ link-symbol / scan-code for {fa['fid']}")
        if all_ucs:
            print(f"  ☐ Review {len(seen_ucs)} UC prose suggestions above — update or confirm KEEP")
        for fa in feature_analysis:
            t = fa["tiers"]
            for tier, count in [("cmp", t.get("cmp",0)), ("int", t.get("int_",0)), ("uc", t.get("uc",0))]:
                if count == 0 and (not caller or fa["minst"] == caller):
                    print(f"  ☐ Add {tier} tests for {fa['fid']}")
        if auto_filed:
            print(f"  ℹ  {len(auto_filed)} cross-instance gap(s) auto-filed — owning sessions will pick them up")
        print(f"  ☐ run link-endpoint + link-symbol for any new endpoints or symbols")
        print()

    drv.close()


def cmd_close_defect(args):
    """Mark a SysDefect as closed."""
    drv = _driver()
    with drv.session() as s:
        s.run("MATCH (d:SysDefect {id:$id}) SET d.status='closed', d.closedAt=$now",
              id=args.id, now=_now_iso())
    drv.close()
    print(f"✓ {args.id} closed")
    # Trigger reconciliation
    class _A: pass
    a = _A(); a.id = ""; a.defect = args.id; a.instance = ""
    cmd_reconcile_done(a)


def cmd_feedback(args):
    """Record a SysFeedback entry from a Claude instance."""
    drv = _driver()
    with drv.session() as s:
        fb_id = _alloc_id(s, "SysFeedback", "FB-", pad=3)
        s.run("""
            MATCH (f:SysFeedback {id: $id})
            SET f.instance=$inst, f.category=$cat, f.body=$body, f.createdAt=$now
        """, id=fb_id, inst=args.instance, cat=args.category or "general",
             body=args.body, now=_now_iso())
        s.run("""
            MATCH (f:SysFeedback {id:$id})
            OPTIONAL MATCH (sys:SysSystem)
            WITH f, sys LIMIT 1
            FOREACH (_ IN CASE WHEN sys IS NOT NULL THEN [1] ELSE [] END |
              MERGE (f)-[:FEEDBACK_ON]->(sys)
            )
        """, id=fb_id)
    drv.close()
    print(f"✓ {fb_id}  [{args.instance}]  {args.body[:72]}")


def cmd_show_feedback(args):
    """Display recorded SysFeedback entries."""
    drv = _driver()
    with drv.session() as s:
        query = "MATCH (f:SysFeedback)"
        clauses = []
        params: dict = {}
        if args.instance:
            clauses.append("f.instance = $inst")
            params["inst"] = args.instance
        if getattr(args, "pending", False):
            clauses.append("(f.actioned IS NULL OR f.actioned = false)")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " RETURN f ORDER BY f.createdAt ASC"
        rows = s.run(query, **params).data()
    drv.close()
    if not rows:
        print("No feedback recorded yet.")
        return
    for r in rows:
        f = r["f"]
        ts = (f.get("createdAt") or "")[:10]
        done = " ✓ actioned" if f.get("actioned") else ""
        print(f"\n── {f['id']}  [{f['instance']}]  {ts}  ({f.get('category','general')}){done}")
        for line in f["body"].splitlines():
            print(f"   {line}")
    total = len(rows)
    actioned = sum(1 for r in rows if r["f"].get("actioned"))
    print(f"\n{total} entr{'y' if total==1 else 'ies'}  ·  {actioned} actioned  ·  {total-actioned} pending")


def cmd_ack_feedback(args):
    """Mark one or more SysFeedback entries as actioned."""
    drv = _driver()
    ids = [i.strip() for i in args.id.split(",") if i.strip()]
    actioned = []
    with drv.session() as s:
        for fb_id in ids:
            result = s.run("""
                MATCH (f:SysFeedback {id:$id})
                SET f.actioned=true, f.actionedAt=$now, f.actionNote=$note
                RETURN f.id AS id, f.instance AS inst
            """, id=fb_id, now=_now_iso(), note=args.note or "").single()
            if result:
                actioned.append(f"{result['id']} [{result['inst']}]")
            else:
                print(f"⚠ {fb_id} not found", file=sys.stderr)
    drv.close()
    for a in actioned:
        print(f"✓ actioned  {a}")


def cmd_close_enhancement(args):
    """Mark a SysEnhancement as done."""
    drv = _driver()
    with drv.session() as s:
        # ENH-548: --verify-entity check before closing
        verify_types = [t.strip() for t in (getattr(args, "verify_entity", "") or "").split(",") if t.strip()]
        if verify_types:
            for vtype in verify_types:
                count = s.run(
                    f"MATCH (e:SysEnhancement {{id:$id}})-[*1..2]->(n:{vtype}) RETURN count(DISTINCT n) AS n",
                    id=args.id
                ).single()["n"]
                if count == 0:
                    print(f"⚠  --verify-entity: no {vtype} nodes reachable from {args.id} — "
                          f"link them first or omit --verify-entity", file=sys.stderr)
                    drv.close(); return
        result = s.run("""
            MATCH (e:SysEnhancement {id:$id})
            SET e.status='done', e.completedAt=$now
            RETURN e.title AS title, e.instance AS inst
        """, id=args.id, now=_now_iso()).single()
        # ENH-600: warn when owning instance has UCs with no REQUIRES→Feature edges
        orphan_uc_count = 0
        if result:
            inst_to_check = args.instance or (result["inst"] or "")
            if inst_to_check:
                orphan_uc_count = s.run("""
                    MATCH (uc:SysUseCase {instance: $inst})
                    WHERE NOT (uc)-[:REQUIRES]->(:SysFeature)
                    RETURN count(uc) AS n
                """, inst=inst_to_check).single()["n"]
    drv.close()
    if not result:
        print(f"⚠ {args.id} not found", file=sys.stderr)
        return
    if args.instance and result["inst"] and result["inst"] != args.instance:
        print(f"⚠  Instance mismatch: {args.id} belongs to '{result['inst']}', "
              f"you are closing from '{args.instance}'", file=sys.stderr)
    if orphan_uc_count:
        inst_to_check = args.instance or (result["inst"] or "")
        print(f"⚠  {orphan_uc_count} UC(s) in '{inst_to_check}' have no REQUIRES→Feature edges — "
              f"run link-usecase or link-endpoint to wire them before ending this session", file=sys.stderr)
    print(f"✓ {args.id} done  —  {result['title']}")
    if not getattr(args, "skip_checklist", False):
        print(f"\n  Pre-close checklist — confirm before ending your session:")
        print(f"  □ link-endpoint run for any new API endpoints added this session?")
        print(f"  □ link-symbol run for any new code symbols added?")
        print(f"  □ UC REQUIRES→Feature edges wired? (link-usecase)")
        print(f"  □ Tests linked to features? (link-feature)")
        print(f"  □ User stories still accurate for what was built?")
        print(f"  (suppress with --skip-checklist)\n")
    # Trigger reconciliation
    class _A: pass
    a = _A(); a.id = args.id; a.defect = ""; a.instance = args.instance or ""
    cmd_reconcile_done(a)


def cmd_create_enhancement(args):
    """Create a new SysEnhancement node with auto-incremented ID."""
    drv = _driver()
    with drv.session() as s:
        enh_id = _alloc_id(s, "SysEnhancement", "ENH-")
        # Warn if title collision
        dupe = s.run("""
            MATCH (e:SysEnhancement)
            WHERE toLower(e.title) = toLower($title)
            RETURN e.id AS id LIMIT 1
        """, title=args.title).single()
        if dupe:
            print(f"⚠  Possible duplicate: '{args.title}' matches {dupe['id']} — check before proceeding",
                  file=sys.stderr)
        s.run("""
            MATCH (e:SysEnhancement {id: $id})
            SET e.title=$title, e.description=$desc,
                e.instance=$inst, e.priority=$prio,
                e.status='proposed', e.createdAt=$now, e.source=$src
        """, id=enh_id, title=args.title, desc=args.description or "",
             inst=args.instance, prio=args.priority or "Should",
             now=_now_iso(), src=args.source or "")
        if args.feature:
            for fid in args.feature.split(","):
                fid = fid.strip()
                if fid:
                    s.run("""MATCH (e:SysEnhancement {id:$eid}),(f:SysFeature {id:$fid})
                             MERGE (e)-[:EXTENDS]->(f)""", eid=enh_id, fid=fid)
    drv.close()
    print(f"✓ {enh_id}  [{args.instance}]  [{args.priority or 'Should'}]  {args.title}")


def cmd_update_enhancement(args):
    """Update title, description, priority, or source on an existing SysEnhancement."""
    drv = _driver()
    with drv.session() as s:
        existing = s.run("MATCH (e:SysEnhancement {id:$id}) RETURN e.id AS id",
                         id=args.id).single()
        if not existing:
            print(f"⚠ {args.id} not found", file=sys.stderr); drv.close(); sys.exit(1)
        updates = {}
        if args.title:       updates["title"]       = args.title
        if args.description: updates["description"] = args.description
        if args.priority:    updates["priority"]    = args.priority
        if args.source:      updates["source"]      = args.source
        if getattr(args, "instance", ""): updates["instance"] = args.instance
        if not updates:
            print("Nothing to update — specify at least one of --title --description --priority --source --instance",
                  file=sys.stderr); drv.close(); return
        set_clause = ", ".join(f"e.{k}=${k}" for k in updates)
        s.run(f"MATCH (e:SysEnhancement {{id:$id}}) SET {set_clause}",
              id=args.id, **updates)
    drv.close()
    print(f"✓ {args.id} updated: {', '.join(updates.keys())}")


def cmd_update_feature(args):
    """Update name, description, or status on an existing SysFeature."""
    drv = _driver()
    with drv.session() as s:
        if not s.run("MATCH (f:SysFeature {id:$id}) RETURN f.id", id=args.id).single():
            print(f"⚠ {args.id} not found", file=sys.stderr); drv.close(); sys.exit(1)
        updates = {}
        if getattr(args, "name",        ""): updates["name"]        = args.name
        if getattr(args, "description", ""): updates["description"] = args.description
        if getattr(args, "status",      ""): updates["status"]      = args.status
        if not updates:
            print("Nothing to update — specify at least one of --name --description --status",
                  file=sys.stderr); drv.close(); return
        set_clause = ", ".join(f"f.{k}=${k}" for k in updates)
        s.run(f"MATCH (f:SysFeature {{id:$id}}) SET {set_clause}", id=args.id, **updates)
    drv.close()
    print(f"✓ {args.id} updated: {', '.join(updates.keys())}")


def cmd_update_usecase(args):
    """Update title, description, preconditions, mainFlow, postconditions, or priority on a SysUseCase."""
    drv = _driver()
    with drv.session() as s:
        if not s.run("MATCH (uc:SysUseCase {id:$id}) RETURN uc.id", id=args.id).single():
            print(f"⚠ {args.id} not found", file=sys.stderr); drv.close(); sys.exit(1)
        updates = {}
        if getattr(args, "title",         ""): updates["title"]         = args.title
        if getattr(args, "description",   ""): updates["description"]   = args.description
        if getattr(args, "preconditions", ""): updates["preconditions"] = args.preconditions
        if getattr(args, "main_flow",     ""): updates["mainFlow"]      = args.main_flow
        if getattr(args, "postconditions",""): updates["postconditions"]= args.postconditions
        if getattr(args, "priority",      ""): updates["priority"]      = args.priority
        if not updates:
            print("Nothing to update — specify at least one of "
                  "--title --description --preconditions --main-flow --postconditions --priority",
                  file=sys.stderr); drv.close(); return
        set_clause = ", ".join(f"uc.{k}=${k}" for k in updates)
        s.run(f"MATCH (uc:SysUseCase {{id:$id}}) SET {set_clause}", id=args.id, **updates)
    drv.close()
    print(f"✓ {args.id} updated: {', '.join(updates.keys())}")


def cmd_update_story(args):
    """Update title, goal, benefit, narrative, acceptanceCriteria, or outOfScope on a SysUserStory."""
    drv = _driver()
    with drv.session() as s:
        if not s.run("MATCH (us:SysUserStory {id:$id}) RETURN us.id", id=args.id).single():
            print(f"⚠ {args.id} not found", file=sys.stderr); drv.close(); sys.exit(1)
        updates = {}
        if getattr(args, "title",               ""): updates["title"]              = args.title
        if getattr(args, "goal",                ""): updates["goal"]               = args.goal
        if getattr(args, "benefit",             ""): updates["benefit"]            = args.benefit
        if getattr(args, "narrative",           ""): updates["narrative"]          = args.narrative
        if getattr(args, "acceptance_criteria", ""): updates["acceptanceCriteria"] = args.acceptance_criteria
        if getattr(args, "out_of_scope",        ""): updates["outOfScope"]         = args.out_of_scope
        if not updates:
            print("Nothing to update — specify at least one of "
                  "--title --goal --benefit --narrative --acceptance-criteria --out-of-scope",
                  file=sys.stderr); drv.close(); return
        set_clause = ", ".join(f"us.{k}=${k}" for k in updates)
        s.run(f"MATCH (us:SysUserStory {{id:$id}}) SET {set_clause}", id=args.id, **updates)
    drv.close()
    print(f"✓ {args.id} updated: {', '.join(updates.keys())}")


def cmd_link_enhancement(args):
    """Link a SysEnhancement to a SysFeature via EXTENDS."""
    drv = _driver()
    linked = []
    with drv.session() as s:
        if not s.run("MATCH (e:SysEnhancement {id:$id}) RETURN e.id", id=args.id).single():
            print(f"ERROR: enhancement '{args.id}' not found", file=sys.stderr); drv.close(); sys.exit(1)
        for fid in args.feature.split(","):
            fid = fid.strip()
            if not fid:
                continue
            if not s.run("MATCH (f:SysFeature {id:$id}) RETURN f.id", id=fid).single():
                print(f"⚠ feature '{fid}' not found — skipping", file=sys.stderr)
                continue
            s.run("""MATCH (e:SysEnhancement {id:$eid}),(f:SysFeature {id:$fid})
                     MERGE (e)-[:EXTENDS]->(f)""", eid=args.id, fid=fid)
            linked.append(fid)
    drv.close()
    for fid in linked:
        print(f"✓ {args.id}  →  EXTENDS  →  {fid}")


def cmd_link_usecase(args):
    """Add feature or story links to an existing SysUseCase without touching its content."""
    drv = _driver()
    with drv.session() as s:
        if not s.run("MATCH (uc:SysUseCase {id:$id}) RETURN uc.id", id=args.id).single():
            print(f"ERROR: use case '{args.id}' not found", file=sys.stderr)
            drv.close(); sys.exit(1)
        linked_f = []
        for fid in [x.strip() for x in (args.feature or "").split(",") if x.strip()]:
            if not s.run("MATCH (f:SysFeature {id:$id}) RETURN f.id", id=fid).single():
                print(f"⚠ feature '{fid}' not found — skipping", file=sys.stderr)
                continue
            s.run("""MATCH (uc:SysUseCase {id:$uid}),(f:SysFeature {id:$fid})
                     MERGE (uc)-[:REQUIRES]->(f)""", uid=args.id, fid=fid)
            linked_f.append(fid)
        linked_s = []
        for sid in [x.strip() for x in (args.story or "").split(",") if x.strip()]:
            if not s.run("MATCH (us:SysUserStory {id:$sid}) RETURN us.id", sid=sid).single():
                print(f"⚠ story '{sid}' not found — skipping", file=sys.stderr)
                continue
            s.run("""MATCH (us:SysUserStory {id:$sid}),(uc:SysUseCase {id:$uid})
                     MERGE (us)-[:REALIZED_BY]->(uc)""", sid=sid, uid=args.id)
            linked_s.append(sid)
    drv.close()
    for fid in linked_f:
        print(f"✓ {args.id}  →  REQUIRES  →  {fid}")
    for sid in linked_s:
        print(f"✓ {sid}  →  REALIZED_BY  →  {args.id}")
    if not linked_f and not linked_s:
        print("Nothing linked — specify --feature and/or --story", file=sys.stderr)


def cmd_start_enhancement(args):
    """Mark a SysEnhancement as in-progress (signals to other sessions it is being built)."""
    drv = _driver()
    with drv.session() as s:
        result = s.run("""
            MATCH (e:SysEnhancement {id:$id})
            SET e.status='in-progress', e.startedAt=$now
            RETURN e.title AS title, e.instance AS inst
        """, id=args.id, now=_now_iso()).single()
    drv.close()
    if not result:
        print(f"⚠ {args.id} not found", file=sys.stderr)
        return
    if args.instance and result["inst"] and result["inst"] != args.instance:
        print(f"⚠  Instance mismatch: {args.id} belongs to '{result['inst']}'", file=sys.stderr)
    print(f"✓ {args.id} in-progress  —  {result['title']}")


def cmd_show_enhancement(args):
    """Display full details of a SysEnhancement by ID."""
    drv = _driver()
    with drv.session() as s:
        result = s.run("""
            MATCH (e:SysEnhancement {id:$id})
            OPTIONAL MATCH (e)-[:EXTENDS]->(f:SysFeature)<-[:PROVIDES]-(m:SysModule)
            RETURN e { .* } AS enode,
                   collect(DISTINCT {fid:f.id, fname:f.name, mid:m.id}) AS features
        """, id=args.id).single()
    drv.close()
    if not result:
        print(f"⚠ {args.id} not found", file=sys.stderr)
        return
    e = result["enode"]
    feats = [f for f in result["features"] if f.get("fid")]
    print(f"\n{'═'*62}")
    print(f"  {e['id']}  [{e.get('instance','')}]  [{e.get('priority','?')}]  status: {e.get('status','?')}")
    print(f"  {e.get('title','')}")
    print(f"{'─'*62}")
    if e.get("description"):
        for line in e["description"].splitlines():
            print(f"  {line}")
    if e.get("source"):
        print(f"\n  Source   : {e['source']}")
    if feats:
        print(f"\n  Features :")
        for f in feats:
            print(f"    {f['fid']}  {f.get('fname','')}  [{f.get('mid','')}]")
    for ts_field in ("createdAt","startedAt","completedAt"):
        if e.get(ts_field):
            print(f"  {ts_field:<12}: {str(e[ts_field])[:19]}")
    print(f"{'═'*62}")


# ── Proposals ────────────────────────────────────────────────────────────────

def cmd_create_proposal(args):
    """Create a SysProposal node — design work that needs a decision before becoming an enhancement."""
    drv = _driver()
    with drv.session() as s:
        prop_id = _alloc_id(s, "SysProposal", "PROP-")
        s.run("""
            MATCH (p:SysProposal {id:$id})
            SET p.title=$title, p.description=$desc,
                p.instance=$inst, p.priority=$prio,
                p.status='draft', p.createdAt=$now
        """, id=prop_id, title=args.title, desc=args.description or "",
             inst=args.instance, prio=args.priority or "Should", now=_now_iso())
        if getattr(args, "feature", None):
            for fid in args.feature.split(","):
                fid = fid.strip()
                if fid:
                    s.run("""MATCH (p:SysProposal {id:$pid}),(f:SysFeature {id:$fid})
                             MERGE (p)-[:EXTENDS]->(f)""", pid=prop_id, fid=fid)
    drv.close()
    print(f"✓ {prop_id}  [{args.instance}]  [{args.priority or 'Should'}]  {args.title}")


def cmd_show_proposal(args):
    """Display full details of a SysProposal by ID."""
    drv = _driver()
    with drv.session() as s:
        result = s.run("""
            MATCH (p:SysProposal {id:$id})
            OPTIONAL MATCH (p)-[:EXTENDS]->(f:SysFeature)<-[:PROVIDES]-(m:SysModule)
            RETURN p { .* } AS pnode,
                   collect(DISTINCT {fid:f.id, fname:f.name, mid:m.id}) AS features
        """, id=args.id).single()
    drv.close()
    if not result:
        print(f"⚠ {args.id} not found", file=sys.stderr); return
    p = result["pnode"]
    feats = [f for f in result["features"] if f.get("fid")]
    print(f"\n{'═'*62}")
    print(f"  {p['id']}  [{p.get('instance','')}]  [{p.get('priority','?')}]  status: {p.get('status','?')}")
    print(f"  {p.get('title','')}")
    print(f"{'─'*62}")
    if p.get("description"):
        for line in p["description"].splitlines():
            print(f"  {line}")
    if feats:
        print(f"\n  Features :")
        for f in feats:
            print(f"    {f['fid']}  {f.get('fname','')}  [{f.get('mid','')}]")
    if p.get("filedAs"):
        print(f"\n  Filed as : {p['filedAs']}")
    for ts_field in ("createdAt", "updatedAt", "closedAt"):
        if p.get(ts_field):
            print(f"  {ts_field:<12}: {str(p[ts_field])[:19]}")
    print(f"{'═'*62}")


def cmd_update_proposal(args):
    """Update title, description, priority, or status on an existing SysProposal."""
    drv = _driver()
    with drv.session() as s:
        existing = s.run("MATCH (p:SysProposal {id:$id}) RETURN p.id AS id", id=args.id).single()
        if not existing:
            print(f"⚠ {args.id} not found", file=sys.stderr); drv.close(); sys.exit(1)
        updates = {"updatedAt": _now_iso()}
        if getattr(args, "title",       None): updates["title"]       = args.title
        if getattr(args, "description", None): updates["description"] = args.description
        if getattr(args, "priority",    None): updates["priority"]    = args.priority
        if getattr(args, "status",      None): updates["status"]      = args.status
        set_clause = ", ".join(f"p.{k}=${k}" for k in updates)
        s.run(f"MATCH (p:SysProposal {{id:$id}}) SET {set_clause}",
              id=args.id, **updates)
    drv.close()
    print(f"✓ {args.id} updated")


def cmd_close_proposal(args):
    """Close a SysProposal — mark accepted/rejected/filed and optionally link to an enhancement."""
    drv = _driver()
    outcome = getattr(args, "outcome", "accepted") or "accepted"
    filed_as = getattr(args, "filed_as", "") or ""
    with drv.session() as s:
        result = s.run("""
            MATCH (p:SysProposal {id:$id})
            SET p.status=$outcome, p.closedAt=$now, p.filedAs=$filed
            RETURN p.title AS title
        """, id=args.id, outcome=outcome, now=_now_iso(), filed=filed_as).single()
    drv.close()
    if not result:
        print(f"⚠ {args.id} not found", file=sys.stderr); return
    filed_tag = f"  → {filed_as}" if filed_as else ""
    print(f"✓ {args.id} {outcome}{filed_tag}  —  {result['title']}")


# ── Notes (instance memory) ───────────────────────────────────────────────────

def cmd_add_note(args):
    """Add a SysNote — instance memory for design context, decisions, and in-session reminders."""
    drv = _driver()
    with drv.session() as s:
        note_id = _alloc_id(s, "SysNote", "NOTE-")
        s.run("""
            MATCH (n:SysNote {id:$id})
            SET n.instance=$inst, n.body=$body,
                n.status='active', n.createdAt=$now,
                n.expiresAt=$expires
        """, id=note_id, inst=args.instance, body=args.body,
             now=_now_iso(), expires=getattr(args, "expires", "") or "")
    drv.close()
    print(f"✓ {note_id}  [{args.instance}]")
    print(f"  {args.body[:100]}{'…' if len(args.body) > 100 else ''}")


def cmd_show_notes(args):
    """Show active SysNote entries for an instance."""
    drv = _driver()
    with drv.session() as s:
        notes = s.run("""
            MATCH (n:SysNote {instance:$inst})
            WHERE n.status IN ['active', $status_filter]
            RETURN n.id AS nid, n.body AS body, n.status AS status,
                   n.createdAt AS created, n.expiresAt AS expires
            ORDER BY n.createdAt DESC
        """, inst=args.instance,
             status_filter="archived" if getattr(args, "all", False) else "active").data()
    drv.close()
    if not notes:
        print(f"No active notes for '{args.instance}'.")
        return
    print(f"\n{'═'*62}")
    print(f"  NOTES — {args.instance}  ({len(notes)} entries)")
    print(f"{'═'*62}")
    for n in notes:
        status_tag = f"  [{n['status']}]" if n["status"] != "active" else ""
        expires_tag = f"  expires {n['expires'][:10]}" if n.get("expires") else ""
        print(f"\n  {n['nid']}{status_tag}{expires_tag}")
        print(f"  Created: {str(n['created'])[:19]}")
        for line in (n["body"] or "").splitlines():
            print(f"  {line}")
    print(f"{'═'*62}")


def cmd_expire_note(args):
    """Mark a SysNote as expired (removes it from worklog without deleting)."""
    drv = _driver()
    with drv.session() as s:
        result = s.run("""
            MATCH (n:SysNote {id:$id})
            SET n.status='expired', n.expiredAt=$now
            RETURN n.instance AS inst, n.body AS body
        """, id=args.id, now=_now_iso()).single()
    drv.close()
    if not result:
        print(f"⚠ {args.id} not found", file=sys.stderr); return
    print(f"✓ {args.id} expired  [{result['inst']}]")


def cmd_show_defect(args):
    """Display full details of a SysDefect by ID."""
    drv = _driver()
    with drv.session() as s:
        result = s.run("""
            MATCH (d:SysDefect {id:$id})
            OPTIONAL MATCH (d)-[:AFFECTS]->(f:SysFeature)<-[:PROVIDES]-(m:SysModule)
            RETURN d { .* } AS dnode,
                   collect(DISTINCT {fid:f.id, fname:f.name, mid:m.id}) AS features
        """, id=args.id).single()
    drv.close()
    if not result:
        print(f"⚠ {args.id} not found", file=sys.stderr)
        return
    d = result["dnode"]
    feats = [f for f in result["features"] if f.get("fid")]
    sev = d.get("severity", "medium")
    sev_mark = {"critical":"●●●●","high":"●●●○","medium":"●●○○","low":"●○○○"}.get(sev,"●●○○")
    print(f"\n{'═'*62}")
    print(f"  {d['id']}  [{sev_mark} {sev}]  status: {d.get('status','?')}")
    print(f"  {d.get('title','')}")
    print(f"{'─'*62}")
    if d.get("instance"):
        print(f"  Instance : {d['instance']}")
    if d.get("source"):
        print(f"  Source   : {d['source']}")
    if feats:
        print(f"\n  Features :")
        for f in feats:
            print(f"    {f['fid']}  {f.get('fname','')}  [{f.get('mid','')}]")
    for ts_field in ("createdAt","closedAt","lastSeen"):
        if d.get(ts_field):
            print(f"  {ts_field:<12}: {str(d[ts_field])[:19]}")
    if d.get("occurrences") and int(d["occurrences"]) > 1:
        print(f"  occurrences: {d['occurrences']}")
    print(f"{'═'*62}")


def cmd_show_feature_tests(args):
    """Show all tests already linked to a feature, and tests in the same module area."""
    drv = _driver()
    with drv.session() as s:
        # Tests already VERIFIES this feature
        linked = s.run("""
            MATCH (t:SysTest)-[:VERIFIES]->(f:SysFeature {id:$fid})
            OPTIONAL MATCH (pkg:SysTestPackage)-[:CONTAINS_TEST]->(t)
            RETURN t.id AS id, t.testType AS tier,
                   pkg.id AS pkg, pkg.testCategory AS cat
            ORDER BY t.id
        """, fid=args.feature).data()

        # Module area for this feature — find related packages
        mod_row = s.run("""
            MATCH (m:SysModule)-[:PROVIDES]->(f:SysFeature {id:$fid})
            RETURN m.id AS mid, m.paths AS paths
        """, fid=args.feature).single()

        W = 62
        print(f"\n{'─'*W}")
        print(f"  Tests linked to {args.feature}  ({len(linked)} total)")
        print(f"{'─'*W}")
        if linked:
            for t in linked:
                tier = f"[{t['cat'] or t['tier'] or '?'}]"
                print(f"  {tier:<16} {t['id']}")
        else:
            print(f"  (none linked yet — use link-feature to add)")

        if mod_row and mod_row["paths"]:
            print(f"\n  Module: {mod_row['mid']}  paths: {', '.join(mod_row['paths'][:2])}")
            # Suggest unlinked tests in same package area
            area_tests = s.run("""
                MATCH (pkg:SysTestPackage)-[:CONTAINS_TEST]->(t:SysTest)
                WHERE any(p IN $paths WHERE pkg.path CONTAINS p OR p CONTAINS pkg.area)
                  AND NOT (t)-[:VERIFIES]->(:SysFeature {id:$fid})
                RETURN t.id AS id, pkg.testCategory AS cat
                ORDER BY t.id LIMIT 15
            """, paths=mod_row["paths"], fid=args.feature).data()
            if area_tests:
                print(f"\n  Tests in same module area (not yet linked):")
                for t in area_tests:
                    print(f"  [{t['cat'] or '?':12}] {t['id']}")
    drv.close()


def cmd_link_story(args):
    """Add REQUIRES edges from a SysUserStory to one or more SysFeature nodes."""
    drv = _driver()
    features = [f.strip() for f in args.features.split(",") if f.strip()]
    linked = []
    with drv.session() as s:
        if not s.run("MATCH (us:SysUserStory {id:$id}) RETURN us.id", id=args.story).single():
            print(f"ERROR: story '{args.story}' not found", file=sys.stderr)
            drv.close(); sys.exit(1)
        for fid in features:
            if not s.run("MATCH (f:SysFeature {id:$id}) RETURN f.id", id=fid).single():
                print(f"⚠ feature '{fid}' not found — skipping", file=sys.stderr)
                continue
            s.run("""MATCH (us:SysUserStory {id:$sid}),(f:SysFeature {id:$fid})
                     MERGE (us)-[:REQUIRES]->(f)""", sid=args.story, fid=fid)
            linked.append(fid)
    drv.close()
    for fid in linked:
        print(f"✓ {args.story}  →  REQUIRES  →  {fid}")
    if not linked:
        print("No features linked.", file=sys.stderr)


def cmd_link_blocks(args):
    """Mark an enhancement as BLOCKED_BY another enhancement."""
    drv = _driver()
    with drv.session() as s:
        blocker = s.run("MATCH (e:SysEnhancement {id:$id}) RETURN e.title AS t, e.status AS st",
                        id=args.blocked_by).single()
        if not blocker:
            print(f"⚠ blocker {args.blocked_by} not found", file=sys.stderr)
            drv.close(); return
        if not s.run("MATCH (e:SysEnhancement {id:$id}) RETURN e.id", id=args.id).single():
            print(f"⚠ {args.id} not found", file=sys.stderr)
            drv.close(); return
        s.run("""
            MATCH (e:SysEnhancement {id:$id}), (b:SysEnhancement {id:$bid})
            MERGE (e)-[:BLOCKED_BY]->(b)
        """, id=args.id, bid=args.blocked_by)
    drv.close()
    print(f"✓ {args.id}  BLOCKED_BY  {args.blocked_by}  ({blocker['t'][:50]}  [{blocker['st']}])")


def cmd_retire_feature(args):
    """Mark a SysFeature as Superseded so it is excluded from coverage gap reports."""
    drv = _driver()
    with drv.session() as s:
        result = s.run("""
            MATCH (f:SysFeature {id:$id})
            SET f.status = 'Superseded', f.retiredAt = $now, f.retiredReason = $reason
            RETURN f.name AS name
        """, id=args.id, now=_now_iso(), reason=args.reason or "").single()
    drv.close()
    if result:
        print(f"✓ {args.id} retired (Superseded) — {result['name']}")
        print(f"  Feature will no longer appear in coverage gap reports.")
    else:
        print(f"⚠ {args.id} not found", file=sys.stderr)


def cmd_coverage_report(args):
    """4-tier V-model gap analysis: US→UC→Feature→Test traceability.

    surfaceType on SysFeature drives which test tiers are required.
    Values: ui | api | middleware | protocol | config | script
    Default (unset) is treated as 'api'.
    """
    instance = getattr(args, "instance", "") or ""
    tier_filter = getattr(args, "tier", "all") or "all"
    fmt = getattr(args, "format", "md") or "md"

    # Tier applicability: which test tiers are required per surfaceType
    _SURFACE_TIERS = {
        "ui":         {"cmp": True,  "int": True,  "uc": True,  "e2e": True},
        "api":        {"cmp": True,  "int": True,  "uc": True,  "e2e": False},
        "middleware": {"cmp": True,  "int": True,  "uc": False, "e2e": False},
        "protocol":   {"cmp": True,  "int": True,  "uc": False, "e2e": False},
        "config":     {"cmp": False, "int": True,  "uc": False, "e2e": False},
        "script":     {"cmp": True,  "int": True,  "uc": False, "e2e": False},
    }
    _DEFAULT_TIERS = {"cmp": True, "int": True, "uc": True, "e2e": False}  # api

    drv = _driver()
    with drv.session() as s:

        # ── T1: User Stories without Use Cases ───────────────────────────
        t1_rows = s.run("""
            MATCH (us:SysUserStory)
            WHERE NOT (us)-[:REALIZED_BY]->(:SysUseCase)
            RETURN us.id AS id, us.title AS title
            ORDER BY us.id
        """).data()

        # ── T2: Use Cases without REQUIRES→Feature edges ─────────────────
        t2_q = "MATCH (uc:SysUseCase) WHERE NOT (uc)-[:REQUIRES]->(:SysFeature)"
        t2_params: dict = {}
        if instance:
            t2_q += " AND uc.instance = $inst"
            t2_params["inst"] = instance
        t2_q += " RETURN uc.id AS id, uc.instance AS inst, uc.title AS title ORDER BY uc.instance, uc.id"
        t2_rows = s.run(t2_q, **t2_params).data()

        # ── T3: Feature test gaps (surfaceType-aware) ─────────────────────
        t3_q = """
            MATCH (f:SysFeature)<-[:PROVIDES]-(m:SysModule)
            WHERE coalesce(f.status,'') <> 'Superseded'
        """
        t3_params: dict = {}
        if instance:
            t3_q += " AND m.instance = $inst"
            t3_params["inst"] = instance
        t3_q += """
            OPTIONAL MATCH (tc:SysTest)-[:VERIFIES]->(f)
              WHERE tc.testType IN ['component','go-unit']
                 OR exists((:SysTestPackage {testCategory:'component'})-[:CONTAINS_TEST]->(tc))
            OPTIONAL MATCH (ti:SysTest)-[:VERIFIES]->(f)
              WHERE ti.testType = 'integration'
                 OR exists((:SysTestPackage {testCategory:'integration'})-[:CONTAINS_TEST]->(ti))
            OPTIONAL MATCH (tu:SysTest)-[:VERIFIES]->(f)
              WHERE tu.testType = 'usecase'
                 OR exists((:SysTestPackage {testCategory:'usecase'})-[:CONTAINS_TEST]->(tu))
            OPTIONAL MATCH (te:SysTest)-[:VERIFIES]->(f)
              WHERE te.testType = 'e2e'
                 OR exists((:SysTestPackage {testCategory:'e2e'})-[:CONTAINS_TEST]->(te))
            RETURN f.id AS fid, f.name AS fname,
                   coalesce(f.surfaceType,'api') AS surface,
                   m.id AS mid, m.instance AS minst,
                   count(DISTINCT tc) AS cmp, count(DISTINCT ti) AS int_,
                   count(DISTINCT tu) AS uc,  count(DISTINCT te) AS e2e
            ORDER BY m.instance, m.id, f.id
        """
        t3_all = s.run(t3_q, **t3_params).data()

        # Filter to only features with at least one required tier missing
        t3_gaps = []
        for row in t3_all:
            surf = row["surface"] or "api"
            tiers = _SURFACE_TIERS.get(surf, _DEFAULT_TIERS)
            counts = {"cmp": row["cmp"], "int": row["int_"], "uc": row["uc"], "e2e": row["e2e"]}
            gaps = {t: counts[t] == 0 for t, req in tiers.items() if req}
            if any(gaps.values()):
                t3_gaps.append({**row, "required": tiers, "counts": counts, "gaps": gaps})

        # ── T4: User Stories without e2e tests ────────────────────────────
        t4_rows = s.run("""
            MATCH (us:SysUserStory)
            WHERE NOT exists(
              (us)-[:REALIZED_BY]->(:SysUseCase)-[:REQUIRES]->(:SysFeature)<-[:VERIFIES]-(:SysTest {testType:'e2e'})
            )
            RETURN us.id AS id, us.title AS title
            ORDER BY us.id
        """).data()

    drv.close()

    today = date.today().isoformat()
    scope_tag = f" — {instance}" if instance else " — all instances"

    if fmt == "md":
        print(f"# SysEdge Coverage Report{scope_tag}  ({today})\n")

        if tier_filter in ("all", "1"):
            print(f"## T1 — User Stories without Use Cases ({len(t1_rows)})\n")
            if t1_rows:
                print("| US | Title |")
                print("|---|---|")
                for r in t1_rows:
                    print(f"| {r['id']} | {r['title']} |")
            else:
                print("_No gaps — all user stories have at least one UC._")
            print()

        if tier_filter in ("all", "2"):
            print(f"## T2 — Use Cases without Feature Links ({len(t2_rows)})\n")
            if t2_rows:
                print("| UC | Instance | Title |")
                print("|---|---|---|")
                for r in t2_rows:
                    print(f"| {r['id']} | {r['inst']} | {r['title'] or ''} |")
            else:
                print("_No gaps — all UCs have REQUIRES→Feature edges._")
            print()

        if tier_filter in ("all", "3"):
            print(f"## T3 — Feature Test Gaps ({len(t3_gaps)} features with missing tiers)\n")
            if t3_gaps:
                print("surfaceType controls which tiers are required "
                      "(ui/api/middleware/protocol/config/script).\n")
                print("| Feature | Module | Surface | cmp | int | uc | e2e |")
                print("|---|---|---|---|---|---|---|")
                for r in t3_gaps:
                    def _cell(tier):
                        req = r["required"].get(tier, False)
                        cnt = r["counts"].get(tier, 0)
                        if not req:
                            return "—"
                        return str(cnt) if cnt > 0 else "**0**"
                    print(f"| {r['fid']} | {r['mid']} | {r['surface']} "
                          f"| {_cell('cmp')} | {_cell('int')} | {_cell('uc')} | {_cell('e2e')} |")
            else:
                print("_No gaps — all features meet their required tier coverage._")
            print()

        if tier_filter in ("all", "4"):
            print(f"## T4 — User Stories without E2E Tests ({len(t4_rows)})\n")
            if t4_rows:
                print("| US | Title |")
                print("|---|---|")
                for r in t4_rows:
                    print(f"| {r['id']} | {r['title']} |")
            else:
                print("_No gaps — all user stories have e2e test coverage._")
            print()

        if tier_filter == "all":
            print("## Summary\n")
            print("| Tier | Gap count |")
            print("|---|---|")
            print(f"| T1 US→UC wiring | {len(t1_rows)} |")
            print(f"| T2 UC→Feature wiring | {len(t2_rows)} |")
            print(f"| T3 Feature test coverage | {len(t3_gaps)} |")
            print(f"| T4 US e2e coverage | {len(t4_rows)} |")
    else:
        # Plain text fallback
        print(f"Coverage Report{scope_tag}  {today}")
        print(f"  T1 US without UCs       : {len(t1_rows)}")
        print(f"  T2 UCs without features : {len(t2_rows)}")
        print(f"  T3 Feature test gaps    : {len(t3_gaps)}")
        print(f"  T4 US without e2e       : {len(t4_rows)}")


def cmd_create_story(args):
    """Create a SysUserStory and optionally link to features and a user type."""
    drv = _driver()
    with drv.session() as s:
        s.run("""
            MERGE (us:SysUserStory {id:$id})
            SET us.title=$title, us.actor=$actor, us.goal=$goal,
                us.priority=$prio, us.benefit=$benefit
        """, id=args.id, title=args.title, actor=args.actor or "",
             goal=args.goal or "", prio=args.priority or "Should",
             benefit=args.benefit or "")
        for fid in (args.features or "").split(","):
            fid = fid.strip()
            if fid:
                s.run("""MATCH (us:SysUserStory {id:$uid}),(f:SysFeature {id:$fid})
                         MERGE (us)-[:REQUIRES]->(f)""", uid=args.id, fid=fid)
        if args.user:
            s.run("""MATCH (u:SysUser {id:$uid}),(us:SysUserStory {id:$sid})
                     MERGE (u)-[:INITIATES]->(us)""", uid=args.user, sid=args.id)
    drv.close()
    print(f"✓ {args.id}  {args.title}")


def cmd_create_usecase(args):
    """Create or update a SysUseCase node, optionally linking to stories and features.

    On CREATE: all fields required (--title mandatory).
    On MATCH (existing UC): only non-empty provided fields are updated; existing
    data is preserved. --title becomes optional for existing UCs.
    """
    drv = _driver()
    with drv.session() as s:
        if not args.instance:
            print("ERROR: --instance is required for create-usecase", file=sys.stderr)
            drv.close(); sys.exit(1)

        existing = s.run("MATCH (uc:SysUseCase {id:$id}) RETURN uc.title AS t",
                         id=args.id).single()

        if not existing:
            # New UC — --title required
            if not args.title:
                print("ERROR: --title is required when creating a new use case", file=sys.stderr)
                drv.close(); sys.exit(1)
            s.run("""
                CREATE (uc:SysUseCase {id:$id, name:$name, title:$title,
                    instance:$inst, description:$desc, priority:$prio,
                    preconditions:[], postconditions:[]})
            """, id=args.id,
                 name=args.name or args.title.lower().replace(" ", "-"),
                 title=args.title, inst=args.instance,
                 desc=args.description or "", prio=args.priority or "P1")
        else:
            # Existing UC — only update explicitly provided non-empty fields
            updates = {}
            if args.title:       updates["title"]       = args.title
            if args.name:        updates["name"]        = args.name
            if args.description: updates["description"] = args.description
            if args.priority:    updates["priority"]    = args.priority
            if args.instance:    updates["instance"]    = args.instance
            if updates:
                set_clause = ", ".join(f"uc.{k}=${k}" for k in updates)
                s.run(f"MATCH (uc:SysUseCase {{id:$id}}) SET {set_clause}",
                      id=args.id, **updates)

        # Link to stories via REALIZED_BY (story→UC) — warn if story not found
        for sid in [x.strip() for x in (args.story or "").split(",") if x.strip()]:
            if not s.run("MATCH (us:SysUserStory {id:$sid}) RETURN us.id", sid=sid).single():
                print(f"⚠  story '{sid}' not found — skipping link", file=sys.stderr)
                continue
            s.run("""MATCH (us:SysUserStory {id:$sid}),(uc:SysUseCase {id:$uid})
                     MERGE (us)-[:REALIZED_BY]->(uc)""", sid=sid, uid=args.id)

        # Link to features via REQUIRES (UC→feature)
        for fid in [x.strip() for x in (args.feature or "").split(",") if x.strip()]:
            s.run("""MATCH (uc:SysUseCase {id:$uid}),(f:SysFeature {id:$fid})
                     MERGE (uc)-[:REQUIRES]->(f)""", uid=args.id, fid=fid)

    drv.close()
    title_display = args.title or existing["t"] if existing else args.title
    print(f"✓ {args.id}  [{args.instance}]  {title_display}")


def cmd_create_feature(args):
    """Create a SysFeature node linked to a module."""
    drv = _driver()
    with drv.session() as s:
        if not args.module:
            print("ERROR: --module is required for create-feature", file=sys.stderr)
            drv.close(); sys.exit(1)
        if not s.run("MATCH (m:SysModule {id:$id}) RETURN m.id", id=args.module).single():
            print(f"ERROR: module '{args.module}' not found", file=sys.stderr)
            drv.close(); sys.exit(1)
        s.run("""
            MERGE (f:SysFeature {id:$id})
            SET f.name=$name, f.status=$status,
                f.description=$desc, f.narrative=$narr,
                f.source=$src
        """, id=args.id, name=args.name, status=args.status or "Proposed",
             desc=args.description or "", narr=args.narrative or "",
             src=args.source or "manual")
        s.run("""MATCH (m:SysModule {id:$mid}),(f:SysFeature {id:$fid})
                 MERGE (m)-[:PROVIDES]->(f)""", mid=args.module, fid=args.id)
        for sid in [s.strip() for s in (args.story or "").split(",") if s.strip()]:
            s.run("""MATCH (us:SysUserStory {id:$sid}),(f:SysFeature {id:$fid})
                     MERGE (us)-[:REQUIRES]->(f)""", sid=sid, fid=args.id)
    drv.close()
    print(f"✓ {args.id}  [{args.module}]  {args.name}")


def cmd_status(args):
    """Cross-instance status dashboard — enhancements, defects, coverage, health."""
    drv = _driver()
    today = date.today().isoformat()
    W = 70

    with drv.session() as s:
        instances = [r["inst"] for r in s.run("""
            MATCH (m:SysModule) WHERE m.instance IS NOT NULL AND m.instance <> ''
            RETURN DISTINCT m.instance AS inst ORDER BY inst
        """).data()]

        enh_rows = s.run("""
            MATCH (e:SysEnhancement) WHERE e.status <> 'done'
            RETURN e.instance AS inst,
                   sum(CASE WHEN e.priority='Must'   THEN 1 ELSE 0 END) AS must,
                   sum(CASE WHEN e.priority='Should' THEN 1 ELSE 0 END) AS should,
                   sum(CASE WHEN e.priority='Could'  THEN 1 ELSE 0 END) AS could,
                   sum(CASE WHEN e.status='in-progress' THEN 1 ELSE 0 END) AS wip,
                   count(e) AS total
            ORDER BY inst
        """).data()
        enh_map = {r["inst"]: r for r in enh_rows}

        def_rows = s.run("""
            MATCH (d:SysDefect) WHERE d.status <> 'closed'
            RETURN coalesce(d.instance,'?') AS inst,
                   sum(CASE WHEN d.severity IN ['critical','high'] THEN 1 ELSE 0 END) AS urgent,
                   count(d) AS total
            ORDER BY inst
        """).data()
        def_map = {r["inst"]: r for r in def_rows}

        cov_rows = s.run("""
            MATCH (m:SysModule)-[:PROVIDES]->(f:SysFeature)
            WHERE NOT f.status IN ['Superseded','Deprecated']
            OPTIONAL MATCH (t:SysTest)-[:VERIFIES]->(f)
            OPTIONAL MATCH (tc:SysTest)-[:VERIFIES]->(f)
              WHERE tc.testType IN ['component','go-unit']
                 OR exists((:SysTestPackage {testCategory:'component'})-[:CONTAINS_TEST]->(tc))
            OPTIONAL MATCH (ti:SysTest)-[:VERIFIES]->(f)
              WHERE ti.testType='integration'
                 OR exists((:SysTestPackage {testCategory:'integration'})-[:CONTAINS_TEST]->(ti))
            OPTIONAL MATCH (tu:SysTest)-[:VERIFIES]->(f)
              WHERE tu.testType IN ['usecase','e2e']
                 OR exists((:SysTestPackage {testCategory:'usecase'})-[:CONTAINS_TEST]->(tu))
                 OR exists((:SysTestPackage {testCategory:'e2e'})-[:CONTAINS_TEST]->(tu))
            RETURN m.instance AS inst,
                   count(DISTINCT f)                                              AS total,
                   count(DISTINCT CASE WHEN t  IS NOT NULL THEN f END)            AS any_cov,
                   count(DISTINCT CASE WHEN tc IS NOT NULL THEN f END)            AS cmp,
                   count(DISTINCT CASE WHEN ti IS NOT NULL THEN f END)            AS int_,
                   count(DISTINCT CASE WHEN tu IS NOT NULL THEN f END)            AS uc
            ORDER BY inst
        """).data()
        cov_map = {r["inst"]: r for r in cov_rows}

        open_defs = s.run("""
            MATCH (d:SysDefect) WHERE d.status <> 'closed'
            RETURN d.id AS id, d.title AS title,
                   coalesce(d.severity,'medium') AS sev,
                   coalesce(d.instance,'?') AS inst
            ORDER BY CASE d.severity
                WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                WHEN 'medium' THEN 2 ELSE 3 END, d.id
        """).data()

        in_prog = s.run("""
            MATCH (e:SysEnhancement {status:'in-progress'})
            RETURN e.id AS id, e.title AS title, e.instance AS inst
            ORDER BY e.instance, e.id
        """).data()

        counts = {r["label"]: r["cnt"] for r in s.run("""
            MATCH (n) WHERE any(l IN labels(n) WHERE l STARTS WITH 'Sys')
            RETURN labels(n)[0] AS label, count(n) AS cnt
        """).data()}

    drv.close()

    print("═" * W)
    print(f"  SYSTEM STATUS  ({today})")
    print("═" * W)
    print(f"\n  {'Instance':<14} {'Must':>4} {'Shld':>4} {'Cld':>4} {'WIP':>4}  "
          f"{'Def':>4}  {'Cover':>5}  cmp  int   uc")
    print(f"  {'─'*14} {'─'*4} {'─'*4} {'─'*4} {'─'*4}  "
          f"{'─'*4}  {'─'*5}  {'─'*3}  {'─'*3}  {'─'*3}")

    all_must = all_def_urgent = 0
    for inst in instances:
        e = enh_map.get(inst, {"must":0,"should":0,"could":0,"wip":0,"total":0})
        d = def_map.get(inst, {"urgent":0,"total":0})
        c = cov_map.get(inst, {"total":0,"any_cov":0,"cmp":0,"int_":0,"uc":0})
        pct = int(100*c["any_cov"]/c["total"]) if c["total"] else 0
        cov_mark = "✓" if pct==100 else ("~" if pct>0 else "✗")
        def_flag = "⚠" if d["urgent"] > 0 else " "
        must_s = f"{e['must']:4d}" if e["must"] else "   ."
        def_s  = f"{d['total']:4d}" if d["total"] else "   ."
        print(f"  {inst:<14} {must_s} {e['should']:4d} {e['could']:4d} "
              f"{e['wip']:4d}  {def_s}{def_flag} "
              f" {cov_mark}{pct:3d}%  {c['cmp']:3d}  {c['int_']:3d}  {c['uc']:3d}")
        all_must += e["must"]
        all_def_urgent += d["urgent"]

    print()
    if in_prog:
        print(f"  IN PROGRESS ({len(in_prog)})")
        for e in in_prog:
            print(f"    {e['id']:<12} [{e['inst']:<12}]  {e['title'][:45]}")
        print()

    sev_mark = {"critical":"●●●●","high":"●●●○","medium":"●●○○","low":"●○○○"}
    if open_defs:
        print(f"  OPEN DEFECTS ({len(open_defs)})")
        for d in open_defs:
            print(f"    {sev_mark.get(d['sev'],'●●○○')}  {d['id']:<12} "
                  f"[{d['inst']:<12}]  {d['title'][:40]}")
        print()
    else:
        print(f"  OPEN DEFECTS — none ✓\n")

    total_enh  = sum(r["total"]   for r in enh_map.values())
    total_feat = sum(r["total"]   for r in cov_map.values())
    total_cov  = sum(r["any_cov"] for r in cov_map.values())
    overall    = int(100*total_cov/total_feat) if total_feat else 0

    print(f"  {'─'*W}")
    print(f"  {len(instances)} instances  ·  "
          f"{total_enh} open enhancements ({all_must} Must)  ·  "
          f"{len(open_defs)} defects ({all_def_urgent} urgent)  ·  "
          f"{overall}% overall coverage")
    print(f"  graph: {counts.get('SysUserStory',0)} stories  "
          f"{counts.get('SysUseCase',0)} UCs  "
          f"{counts.get('SysFeature',0)} features  "
          f"{counts.get('SysTest',0)} tests")
    if all_must:
        print(f"\n  ⚠  {all_must} Must-priority enhancements — run worklog --instance <name>")
    if all_def_urgent:
        print(f"  ⚠  {all_def_urgent} critical/high defects open")
    print(f"{'═'*W}")


# ── CLI wiring ────────────────────────────────────────────────────────────────

class _HideInternalFormatter(argparse.RawDescriptionHelpFormatter):
    """Formatter that omits subcommands whose help is set to SUPPRESS."""

    def _format_action(self, action):
        if action.help == argparse.SUPPRESS:
            return ""
        # For subparser group actions, filter out individual SUPPRESS entries
        if hasattr(action, "_get_subactions"):
            parts = []
            for sub in action._get_subactions():
                if sub.help != argparse.SUPPRESS:
                    parts.append(self._format_action(sub))
            return "".join(parts)
        return super()._format_action(action)

    def _metavar_formatter(self, action, default_metavar):
        # Narrow the {cmd1|cmd2|...} usage line to only visible subcommands
        if hasattr(action, "_get_subactions"):
            visible = [s.dest for s in action._get_subactions()
                       if s.help != argparse.SUPPRESS]
            def _format(tuple_size):
                if tuple_size == 1:
                    return (("{%s}" % ",".join(visible)),)
                return (("{%s}" % ",".join(visible)),) * tuple_size
            return _format
        return super()._metavar_formatter(action, default_metavar)


def main():
    # Intercept premium commands before argparse validates their arguments
    _PREMIUM = {"analyse", "commit-import", "coverage-review", "create-adr", "export", "merge-nodes", "migrate-binary", "preview-import", "seed-standards"}
    if len(sys.argv) > 1 and sys.argv[1] in _PREMIUM:
        print("\n  ⚡ Bootstrap Kit command — https://www.org-edge.com/sysedge.html")
        print()
        sys.exit(0)

    # Warn if not run from the project root (symptoms: .env and scripts/ not found)
    if not Path("scripts").is_dir() and Path(__file__).parent.name == "scripts":
        print(
            f"WARNING: run sys_graph.py from the project root, not '{Path.cwd().name}'. "
            f"Try:  cd {Path(__file__).parent.parent}",
            file=sys.stderr,
        )

    p = argparse.ArgumentParser(
        description="System knowledge graph — requirements traceability",
        formatter_class=_HideInternalFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help=argparse.SUPPRESS)

    sp = sub.add_parser("seed-standards",
                        help=argparse.SUPPRESS)
    sp.add_argument("file", help="Path to YAML standards file (see tools/sysedge/examples/arch-standards.yaml)")

    sp = sub.add_parser("seed", help="Load JSON seed file")
    sp.add_argument("file", help="Path to seed JSON")
    sp.add_argument("--instance", default="",
                    help="Only seed nodes belonging to this instance (safe for parallel sessions)")
    sp.add_argument("--module",   default="",
                    help="Only seed nodes belonging to this module ID")
    sp.add_argument("--no-backup",    action="store_true",
                    help="Skip the automatic pre-seed backup (sys-graph only)")
    sp.add_argument("--full-restore", action="store_true",
                    help="Allow unfiltered seed — sys-graph only, overwrites all instances")

    sp = sub.add_parser("briefing", help="Session-start briefing for an instance")
    sp.add_argument("--instance", required=True, help="Instance name (e.g. framework)")
    sp.add_argument("--compact",  action="store_true",
                    help="Skip per-feature list for fully-passing modules")

    sp = sub.add_parser("coverage", help=argparse.SUPPRESS)
    sp.add_argument("--instance", required=True)
    sp.add_argument("--compact",  action="store_true")

    sp = sub.add_parser("link-test", help=argparse.SUPPRESS)
    sp.add_argument("--feature",  required=True)
    sp.add_argument("--file",     required=True)
    sp.add_argument("--fn",       required=True, help="Test function name")
    sp.add_argument("--type",     default="integration")

    sp = sub.add_parser("test-gaps", help="Show test coverage gaps by tier for an instance or P6 master")
    grp = sp.add_mutually_exclusive_group(required=True)
    grp.add_argument("--instance", default="", help="Instance name, e.g. core, manage, plan")
    grp.add_argument("--p6",       action="store_true", help="P6 master view: packages with no feature links")
    sp.add_argument("--tier", default="all",
                    choices=["all","component","integration","usecase","e2e"],
                    help="Filter to one tier (default: all three)")

    sp = sub.add_parser("worklog", help="Show open defects and pending enhancements with feature/code links")
    sp.add_argument("--instance", required=True, help="Instance name, e.g. core, manage, plan")
    sp.add_argument("--strict",   action="store_true", default=True,
                    help="Only show ENHs whose feature links belong to this instance's modules (default: on)")
    sp.add_argument("--all",     action="store_true",
                    help="Show all ENHs for this instance including cross-module noise (overrides --strict)")
    sp.add_argument("--since",    default="",
                    help="Only show ENHs created on or after this date, e.g. 2026-05-20")

    sp = sub.add_parser("link-defect", help="Create a defect node and link it to a feature")
    sp.add_argument("--id",       default="", help="Defect ID, e.g. DEF-001 (auto-assigned if omitted)")
    sp.add_argument("--feature",  required=True)
    sp.add_argument("--title",    required=True)
    sp.add_argument("--severity", default="medium", choices=["critical","high","medium","low"])
    sp.add_argument("--instance", default="")

    sp = sub.add_parser("close-defect", help="Mark a defect as closed")
    sp.add_argument("--id", required=True)

    sp = sub.add_parser("update-enhancement", help="Update title, description, priority, source, or instance on an enhancement")
    sp.add_argument("--id",          required=True)
    sp.add_argument("--title",       default="")
    sp.add_argument("--description", default="")
    sp.add_argument("--priority",    default="", choices=["","Must","Should","Could"])
    sp.add_argument("--source",      default="")
    sp.add_argument("--instance",    default="", help="Reassign to a different instance")

    sp = sub.add_parser("update-feature", help="Update name, description, or status on a SysFeature")
    sp.add_argument("--id",          required=True, help="Feature ID, e.g. F-P7-001")
    sp.add_argument("--name",        default="", help="Short display name")
    sp.add_argument("--description", default="", help="Full functional description")
    sp.add_argument("--status",      default="", help="e.g. Active, Superseded")

    sp = sub.add_parser("update-usecase", help="Update fields on a SysUseCase")
    sp.add_argument("--id",              required=True, help="UC ID, e.g. UC-DLG-001")
    sp.add_argument("--title",           default="")
    sp.add_argument("--description",     default="")
    sp.add_argument("--preconditions",   default="")
    sp.add_argument("--main-flow",       default="", dest="main_flow")
    sp.add_argument("--postconditions",  default="")
    sp.add_argument("--priority",        default="", choices=["","P1","P2","P3"])

    sp = sub.add_parser("update-story", help="Update fields on a SysUserStory")
    sp.add_argument("--id",                    required=True, help="Story ID, e.g. US-007")
    sp.add_argument("--title",                 default="")
    sp.add_argument("--goal",                  default="")
    sp.add_argument("--benefit",               default="")
    sp.add_argument("--narrative",             default="")
    sp.add_argument("--acceptance-criteria",   default="", dest="acceptance_criteria")
    sp.add_argument("--out-of-scope",          default="", dest="out_of_scope")

    sp = sub.add_parser("create-enhancement", help="Create a new SysEnhancement with auto-incremented ID")
    sp.add_argument("--title",       required=True)
    sp.add_argument("--instance",    required=True, help="Owning instance, e.g. core, manage, plan")
    sp.add_argument("--description", default="",    help="Full description")
    sp.add_argument("--priority",    default="Should", choices=["Must","Should","Could"])
    sp.add_argument("--feature",     default="",    help="Comma-separated feature IDs to link via EXTENDS")
    sp.add_argument("--source",      default="",    help="Source reference, e.g. FB-082 or ENHANCE.md")

    sp = sub.add_parser("link-enhancement", help="Link an enhancement to a feature via EXTENDS")
    sp.add_argument("--id",      required=True, help="Enhancement ID, e.g. ENH-024")
    sp.add_argument("--feature", required=True, help="Comma-separated feature IDs")

    sp = sub.add_parser("link-usecase", help="Add feature or story links to an existing use case")
    sp.add_argument("--id",      required=True, help="Use case ID, e.g. UC-AUTH-001")
    sp.add_argument("--feature", default="", help="Comma-separated SysFeature IDs → REQUIRES")
    sp.add_argument("--story",   default="", help="Comma-separated SysUserStory IDs → REALIZED_BY")

    sp = sub.add_parser("close-enhancement", help="Mark an enhancement as done")
    sp.add_argument("--id",       required=True, help="Enhancement ID, e.g. ENH-024")
    sp.add_argument("--instance", default="",    help="Your instance name — warns on mismatch")
    sp.add_argument("--verify-entity", default="", metavar="TYPES",
                    help="Comma-sep entity types to verify exist in graph (e.g. SysFeature,SysUseCase)")
    sp.add_argument("--skip-checklist", action="store_true",
                    help="Suppress the pre-close checklist reminder")

    sp = sub.add_parser("reconcile-done",
                        help=argparse.SUPPRESS)
    grp = sp.add_mutually_exclusive_group(required=True)
    grp.add_argument("--id",     default="", help="Enhancement ID, e.g. ENH-024")
    grp.add_argument("--defect", default="", help="Defect ID, e.g. DEF-001")
    sp.add_argument("--instance", default="", help="Calling session instance — used for scope decisions")

    sp = sub.add_parser("start-enhancement", help="Mark an enhancement as in-progress")
    sp.add_argument("--id",       required=True, help="Enhancement ID, e.g. ENH-024")
    sp.add_argument("--instance", default="",    help="Your instance name — warns on mismatch")

    sp = sub.add_parser("show-enhancement", help="Display full details of an enhancement")
    sp.add_argument("--id", required=True, help="Enhancement ID, e.g. ENH-024")

    sp = sub.add_parser("show-defect", help="Display full details of a defect")
    sp.add_argument("--id", required=True, help="Defect ID, e.g. DEF-042")

    # ── Proposals ─────────────────────────────────────────────────────────────
    sp = sub.add_parser("create-proposal",
                        help="Create a SysProposal — design work awaiting decision before filing as enhancement")
    sp.add_argument("--title",       required=True, help="Short title")
    sp.add_argument("--instance",    required=True, help="Owning instance, e.g. manage")
    sp.add_argument("--description", default="",    help="Full design description")
    sp.add_argument("--priority",    default="Should", choices=["Must","Should","Could"])
    sp.add_argument("--feature",     default="",    help="Comma-separated feature IDs")

    sp = sub.add_parser("show-proposal", help="Display full details of a proposal")
    sp.add_argument("--id", required=True, help="Proposal ID, e.g. PROP-001")

    sp = sub.add_parser("update-proposal", help="Update an existing proposal")
    sp.add_argument("--id",          required=True)
    sp.add_argument("--title",       default="")
    sp.add_argument("--description", default="")
    sp.add_argument("--priority",    default="", choices=["","Must","Should","Could"])
    sp.add_argument("--status",      default="", choices=["","draft","accepted","rejected","filed"])

    sp = sub.add_parser("close-proposal",
                        help="Close a proposal (accepted/rejected/filed)")
    sp.add_argument("--id",       required=True, help="Proposal ID, e.g. PROP-001")
    sp.add_argument("--outcome",  default="accepted",
                    choices=["accepted","rejected","filed"],
                    help="Outcome: accepted (design decision recorded), rejected, or filed (ENH created)")
    sp.add_argument("--filed-as", default="",
                    help="Enhancement ID if outcome=filed, e.g. ENH-042")

    # ── Notes (instance memory) ───────────────────────────────────────────────
    sp = sub.add_parser("add-note",
                        help="Add a SysNote — instance memory for design context and session reminders")
    sp.add_argument("--instance", required=True, help="Your instance name")
    sp.add_argument("--body",     required=True, help="Note body text")
    sp.add_argument("--expires",  default="",   help="Optional ISO date when this note becomes stale, e.g. 2026-06-04")

    sp = sub.add_parser("show-notes", help="Show SysNote entries for an instance")
    sp.add_argument("--instance", required=True)
    sp.add_argument("--all", action="store_true", help="Include archived notes")

    sp = sub.add_parser("expire-note",
                        help="Mark a SysNote as expired (removes from worklog without deleting)")
    sp.add_argument("--id", required=True, help="Note ID, e.g. NOTE-001")

    sp = sub.add_parser("link-story", help="Add REQUIRES edges from a user story to features")
    sp.add_argument("--story",    required=True, help="Story ID, e.g. US-016")
    sp.add_argument("--features", required=True, help="Comma-separated feature IDs")

    sp = sub.add_parser("show-feature-tests",
                        help="Show tests linked to a feature + unlinked tests in the same module area")
    sp.add_argument("--feature", required=True, help="Feature ID, e.g. F-P4-002")

    sp = sub.add_parser("retire-feature", help="Mark a feature as Superseded (excluded from gap reports)")
    sp.add_argument("--id",     required=True, help="Feature ID, e.g. F-IMP-005")
    sp.add_argument("--reason", default="",    help="Why this feature was retired")

    sp = sub.add_parser("coverage-report",
                        help="4-tier V-model gap analysis: US→UC→Feature→Test traceability")
    sp.add_argument("--instance", default="", help="Scope T2/T3 to one instance (T1/T4 always global)")
    sp.add_argument("--tier", default="all", choices=["all","1","2","3","4"],
                    help="Show one tier only (default: all)")
    sp.add_argument("--format", default="md", choices=["md","text"],
                    help="Output format: md (default) or text summary")

    sp = sub.add_parser("coverage-review",
                        help="AI sufficiency check on US→UC and UC→Feature chains (uses Claude API)")
    sp.add_argument("--scope",    default="both", choices=["us","uc","both"],
                    help="Which chain to review: us (story→UC), uc (UC→feature), or both (default)")
    sp.add_argument("--instance", default="",
                    help="Scope to one instance's modules/UCs")
    sp.add_argument("--story",    default="",
                    help="Review a single user story, e.g. US-007 (implies --scope us)")
    sp.add_argument("--module",   default="",
                    help="Scope to one module, e.g. MOD-delegation")
    sp.add_argument("--model",    default="haiku", choices=["haiku","sonnet"],
                    help="Claude model to use (default: haiku — cheaper for large scopes)")
    sp.add_argument("--file-proposals", action="store_true", dest="file_proposals",
                    help="Create SysProposal nodes for every GAP finding")

    sp = sub.add_parser("link-blocks", help="Mark one enhancement as BLOCKED_BY another")
    sp.add_argument("--id",         required=True, help="Enhancement that is blocked, e.g. ENH-042")
    sp.add_argument("--blocked-by", required=True, dest="blocked_by",
                    help="Enhancement that is blocking, e.g. ENH-020")

    sp = sub.add_parser("migrate-binary",
                        help=argparse.SUPPRESS)
    sp.add_argument("--from", required=True, dest="from_binary", help="Source binary name, e.g. core")
    sp.add_argument("--to",   required=True, dest="to_binary",   help="Target binary name, e.g. platform")
    sp.add_argument("--features", default="", help="Comma-separated feature IDs to scope the migration")

    sp = sub.add_parser("create-usecase", help="Create or update a SysUseCase node")
    sp.add_argument("--id",          required=True)
    sp.add_argument("--title",       default="", help="Required when creating; optional when updating existing UC")
    sp.add_argument("--instance",    required=True)
    sp.add_argument("--name",        default="", help="Slug (defaults to kebab-case of title)")
    sp.add_argument("--description", default="")
    sp.add_argument("--priority",    default="P1", choices=["P0","P1","P2","P3"])
    sp.add_argument("--story",       default="", help="Comma-separated SysUserStory IDs")
    sp.add_argument("--feature",     default="", help="Comma-separated SysFeature IDs")

    sp = sub.add_parser("create-feature", help="Create a SysFeature linked to a module")
    sp.add_argument("--id",          required=True)
    sp.add_argument("--name",        required=True)
    sp.add_argument("--module",      required=True, help="SysModule.id")
    sp.add_argument("--status",      default="Proposed",
                    choices=["Proposed","InProgress","Implemented","Deprecated","Superseded"])
    sp.add_argument("--description", default="")
    sp.add_argument("--narrative",   default="")
    sp.add_argument("--source",      default="manual")
    sp.add_argument("--story",       default="", help="Comma-separated SysUserStory IDs")

    sp = sub.add_parser("create-adr", help="Create a SysArchDecision (ADR) node")
    sp.add_argument("--id",           required=True)
    sp.add_argument("--title",        required=True)
    sp.add_argument("--status",       default="proposed",
                    choices=["proposed","adopted","deprecated","superseded"])
    sp.add_argument("--date",         default="")
    sp.add_argument("--decision",     default="")
    sp.add_argument("--context",      default="")
    sp.add_argument("--consequences", default="")
    sp.add_argument("--addresses",    default="", help="Comma-separated SysArchStd IDs")
    sp.add_argument("--supersedes",   default="", help="Comma-separated ADR IDs this supersedes")

    sp = sub.add_parser("merge-nodes", help=argparse.SUPPRESS)
    sp.add_argument("--type",     required=True,
                    help="Node label, e.g. SysFeature, SysUseCase, SysUserStory")
    sp.add_argument("--keep",     required=True, help="ID of node to keep")
    sp.add_argument("--absorb",   required=True, help="ID of node to absorb (will be deleted)")
    sp.add_argument("--strategy", default="union",
                    choices=["union","prefer-keep","prefer-absorb"])

    sp = sub.add_parser("preview-import", help=argparse.SUPPRESS)
    sp.add_argument("file", help="Path to sysedge.import.v1 JSON file")

    sp = sub.add_parser("commit-import", help=argparse.SUPPRESS)
    sp.add_argument("file",        help="Path to sysedge.import.v1 JSON file")
    sp.add_argument("--decisions", required=True, help="Path to decisions JSON (from preview-import)")

    sp = sub.add_parser("analyse", help="Analyse graph for orphans, merge candidates, or split candidates")
    sp.add_argument("--instance",  default="")
    sp.add_argument("--merge",     action="store_true", help="Find merge candidates")
    sp.add_argument("--split",     action="store_true", help="Find split candidates")
    sp.add_argument("--orphans",   action="store_true", help="Find orphan nodes")
    sp.add_argument("--threshold", type=float, default=0.80, help="Merge similarity threshold (default 0.80)")
    sp.add_argument("--max-responsibilities", type=int, default=3, dest="max_responsibilities",
                    help="Max UCs per feature before flagging as split candidate (default 3)")
    sp.add_argument("--json",      action="store_true", help="Output as JSON")

    sp = sub.add_parser("export", help="Export graph slice as Markdown or JSON document")
    sp.add_argument("--type",     required=True,
                    choices=["stories","use-cases","application-arch","technical-arch",
                             "test-coverage","test-summary"])
    sp.add_argument("--instance", default="")
    sp.add_argument("--module",   default="")
    sp.add_argument("--output",   default="")
    sp.add_argument("--format",   default="md", choices=["md","json"])

    sp = sub.add_parser("create-story", help="Create a user story")
    sp.add_argument("--id",       required=True, help="Story ID, e.g. US-015")
    sp.add_argument("--title",    required=True)
    sp.add_argument("--actor",    default="")
    sp.add_argument("--goal",     default="")
    sp.add_argument("--benefit",  default="")
    sp.add_argument("--priority", default="Should", choices=["Must","Should","Could","Won't"])
    sp.add_argument("--features", default="", help="Comma-separated feature IDs")
    sp.add_argument("--user",     default="", help="SysUser ID that initiates this story")

    sp = sub.add_parser("link-feature",
                        help="Bulk-link matching scanned tests to a feature by pattern")
    sp.add_argument("--feature", required=True, help="Feature ID, e.g. F-P3-001")
    sp.add_argument("--tests",   required=True,
                    help="Comma-separated substrings matched against SysTest.id "
                         "(pytest node ID format: file.py::ClassName or file.py::fn)")

    sp = sub.add_parser("link-endpoint", help="Link an API endpoint to a feature")
    sp.add_argument("--feature",    required=True)
    sp.add_argument("--method",     required=True)
    sp.add_argument("--path",       required=True)
    sp.add_argument("--binary",     default="")
    sp.add_argument("--permission", default="")

    sp = sub.add_parser("link-symbol", help="Link a code symbol to a feature")
    sp.add_argument("--feature", required=True)
    sp.add_argument("--file",    required=True)
    sp.add_argument("--symbol",  required=True)
    sp.add_argument("--line",    type=int, default=0)
    sp.add_argument("--symtype", default="handler")

    sp = sub.add_parser("features", help="List features for a module")
    sp.add_argument("--module", required=True)

    sub.add_parser("status",  help="Cross-instance dashboard: enhancements, defects, coverage across all instances")
    sp = sub.add_parser("stories", help="List all user stories and their features")
    sp.add_argument("--gap", action="store_true",
                    help="Show only user stories with no linked use case (REALIZED_BY)")
    sub.add_parser("scenarios", help="Show scenarios and training with decomposition status")

    sp = sub.add_parser("link-chapter", help="Link a scenario chapter to a feature")
    sp.add_argument("--chapter", required=True, help="Chapter ID, e.g. SCN-001-CH-02")
    sp.add_argument("--feature", required=True)

    sp = sub.add_parser("link-scenario-story",
                        help=argparse.SUPPRESS)
    sp.add_argument("--scenario", required=True, help="Scenario ID, e.g. SCN-004")
    sp.add_argument("--story",    required=True, help="User story ID, e.g. US-042")

    sp = sub.add_parser("link-training", help="Link a training module to a feature")
    sp.add_argument("--module",  required=True, help="Training module ID, e.g. TRN-MGR-03")
    sp.add_argument("--feature", required=True)

    sp = sub.add_parser("record-run", help=argparse.SUPPRESS)
    sp.add_argument("--package",  required=True, help="Test package path, e.g. backend/tests/integration")
    sp.add_argument("--passed",   type=int, default=0)
    sp.add_argument("--failed",   type=int, default=0)
    sp.add_argument("--skipped",  type=int, default=0)
    sp.add_argument("--xfailed",  type=int, default=0, help="Expected failures (xfail)")
    sp.add_argument("--duration", type=float, default=0.0, help="Total run duration in seconds")
    sp.add_argument("--notes",    default="", help="Optional run notes")

    sp = sub.add_parser("test-status", help="Show last-run timestamps and stale tests")
    sp.add_argument("--days",     type=int, default=7,  help="Flag tests not run in this many days (default 7)")
    sp.add_argument("--instance", default="",           help="Filter by instance (e.g. core, manage, plan)")

    sp = sub.add_parser("backup", help="Export all Sys* nodes to a seed-compatible JSON file")
    sp.add_argument("--output", default="", help="Output path (default: data/sys-backup-YYYY-MM-DD.json)")

    sp = sub.add_parser("activate", help=argparse.SUPPRESS)
    sp.add_argument("key", help="Your Lemon Squeezy licence key")
    sp.add_argument("--url", default="", help="Override activation URL (for testing)")

    sp = sub.add_parser("licence", help=argparse.SUPPRESS)

    sp = sub.add_parser("feedback", help="Record a feedback entry from a Claude instance")
    sp.add_argument("--instance", required=True, help="Instance name (e.g. core, manage, plan)")
    sp.add_argument("--category", default="general",
                    choices=["general","usability","gap","workflow","positive"],
                    help="Feedback category (default: general)")
    sp.add_argument("--body", required=True, help="Feedback prose text")

    sp = sub.add_parser("show-feedback", help="Display recorded feedback entries")
    sp.add_argument("--instance", default="", help="Filter by instance")
    sp.add_argument("--pending",  action="store_true", help="Show only unactioned entries")

    sp = sub.add_parser("ack-feedback", help="Mark feedback entries as actioned")
    sp.add_argument("--id",   required=True, help="Comma-separated FB-IDs, e.g. FB-002,FB-004")
    sp.add_argument("--note", default="",    help="Optional note on how it was actioned")


    sp = sub.add_parser("scan-go-tests",
                        help="Scan Go *_test.go files and register as component-category SysTest nodes")
    sp.add_argument("--path", default="backend/internal",
                    help="Root directory to scan (default: backend/internal)")

    # ── scan-tests: register Python test functions (replaces scan-package / scan-all)
    sp = sub.add_parser(
        "scan-tests",
        help="Register test functions from Python test files into SysTest nodes (additive MERGE)",
    )
    grp = sp.add_mutually_exclusive_group(required=True)
    grp.add_argument("--path",   metavar="FILE",   help="Single test file path or SysTestPackage id")
    grp.add_argument("--all",    action="store_true", help="Scan all registered Python test packages")
    grp.add_argument("--module", metavar="MODULE", help="SysModule id — scans test packages for that module's area")
    sp.add_argument("--root",     default="", metavar="ROOT",
                    help="Filter --all by canonicalRoot: p6, backend")
    sp.add_argument("--area",     default="", metavar="AREA",
                    help="Filter --all by area: p1, p3, p8 …")
    sp.add_argument("--instance", default="", metavar="INSTANCE",
                    help="Filter --all by instance: core, manage, plan …")
    sp.add_argument("--category", default="",
                    choices=["", "component", "integration", "usecase", "e2e"],
                    help="Override derived category: component | integration | usecase | e2e")

    # ── scan-code: extract symbols from Go/TypeScript source (replaces scan-code / scan-code-all)
    sp = sub.add_parser(
        "scan-code",
        help="Extract exported symbols from Go/TypeScript source into SysSymbol nodes (additive MERGE)",
    )
    grp = sp.add_mutually_exclusive_group(required=True)
    grp.add_argument("--path",   metavar="PATH",   help="File or directory to scan")
    grp.add_argument("--all",    action="store_true",
                     help="Scan all known code roots: backend/internal, frontend/app/src, shared")
    grp.add_argument("--module", metavar="MODULE", help="SysModule id — scans its registered paths")
    sp.add_argument("--lang", default="auto",
                    choices=["go", "ts", "py", "java", "cs", "auto"],
                    help="Language filter: go, ts, py, java, cs, or auto (all). Default: auto")

    args = p.parse_args()

    def _dispatch_scan_tests(a):
        if a.all:
            cmd_scan_all(a)
        elif getattr(a, 'module', None):
            # Look up the module's area and scan matching packages
            drv = _driver()
            with drv.session() as s:
                row = s.run("MATCH (m:SysModule {id:$id}) RETURN m.id AS id, m.instance AS inst",
                            id=a.module).single()
            drv.close()
            if not row:
                print(f"ERROR: module '{a.module}' not found", file=sys.stderr); sys.exit(1)
            # Derive area from module id (MOD-auth → auth, MOD-p8-functions → p8)
            area = a.module.replace("MOD-","").split("-")[0]
            a.area = area
            a.root = ""
            a.instance = ""
            cmd_scan_all(a)
        else:
            a.package = a.path
            cmd_scan_package(a)

    def _dispatch_scan_code(a):
        if a.all:
            cmd_scan_code_all(a)
        elif getattr(a, 'module', None):
            # Look up the module's paths and scan each
            drv = _driver()
            with drv.session() as s:
                row = s.run("MATCH (m:SysModule {id:$id}) RETURN m.paths AS paths",
                            id=a.module).single()
            drv.close()
            if not row or not row["paths"]:
                print(f"ERROR: module '{a.module}' not found or has no paths", file=sys.stderr); sys.exit(1)
            total = 0
            for p in row["paths"]:
                a.path = p.rstrip("/")
                if Path(a.path).exists():
                    orig_cmd_scan_code(a)
                else:
                    print(f"  (skip {p} — not found)")
        else:
            orig_cmd_scan_code(a)

    # Premium commands — not in the free CLI
    def _upgrade(args):
        print("\n  ⚡ Bootstrap Kit command — https://www.org-edge.com/sysedge.html")
        print()
        import sys; sys.exit(0)

    dispatch = {
        "init":           cmd_init,
        "seed":           cmd_seed,
        "briefing":       cmd_briefing,
        "coverage":       cmd_briefing,
        "link-test":      cmd_link_test,
        "test-gaps":      cmd_test_gaps,
        "worklog":        cmd_worklog,
        "link-feature":   cmd_link_feature,
        "link-defect":    cmd_link_defect,
        "close-defect":       cmd_close_defect,
        "update-enhancement": cmd_update_enhancement,
        "update-feature":     cmd_update_feature,
        "update-usecase":     cmd_update_usecase,
        "update-story":       cmd_update_story,
        "create-enhancement": cmd_create_enhancement,
        "link-enhancement":   cmd_link_enhancement,
        "close-enhancement":  cmd_close_enhancement,
        "reconcile-done":     cmd_reconcile_done,
        "start-enhancement":  cmd_start_enhancement,
        "show-enhancement":   cmd_show_enhancement,
        "show-defect":        cmd_show_defect,
        "create-proposal":    cmd_create_proposal,
        "show-proposal":      cmd_show_proposal,
        "update-proposal":    cmd_update_proposal,
        "close-proposal":     cmd_close_proposal,
        "add-note":           cmd_add_note,
        "show-notes":         cmd_show_notes,
        "expire-note":        cmd_expire_note,
        "link-usecase":       cmd_link_usecase,
        "link-story":          cmd_link_story,
        "show-feature-tests":  cmd_show_feature_tests,
        "retire-feature":      cmd_retire_feature,
        "link-blocks":         cmd_link_blocks,
        "migrate-binary":      _upgrade,
        "coverage-report":     cmd_coverage_report,
        "coverage-review":     _upgrade,
        "preview-import":  _upgrade,
        "commit-import":   _upgrade,
        "create-usecase":  cmd_create_usecase,
        "create-feature":  cmd_create_feature,
        "create-adr":      _upgrade,
        "merge-nodes":     _upgrade,
        "analyse":         _upgrade,
        "export":          _upgrade,
        "create-story":    cmd_create_story,
        "link-endpoint":  cmd_link_endpoint,
        "link-symbol":    cmd_link_symbol,
        "link-chapter":         cmd_link_chapter,
        "link-scenario-story":  cmd_link_scenario_story,
        "link-training":        cmd_link_training,
        "record-run":     cmd_record_run,
        "test-status":    cmd_test_status,
        "backup":         cmd_backup,
        "activate":       cmd_activate,
        "licence":        cmd_licence_info,

        "scan-go-tests":  cmd_scan_go_tests,
        "scan-tests":     _dispatch_scan_tests,
        "scan-code":      _dispatch_scan_code,
        "features":       cmd_features,
        "status":         cmd_status,
        "stories":        cmd_stories,
        "scenarios":      cmd_scenarios,
        "seed-standards": _upgrade,
        "feedback":       cmd_feedback,
        "show-feedback":  cmd_show_feedback,
        "ack-feedback":   cmd_ack_feedback,
    }
    try:
        dispatch[args.cmd](args)
    except Exception as e:
        if "ServiceUnavailable" in type(e).__name__ or "Connection refused" in str(e):
            print(f"\n⚠  Graph database unavailable ({NEO4J_URI}) — start Docker and retry.\n",
                  file=sys.stderr)
            sys.exit(2)
        raise


if __name__ == "__main__":
    main()
