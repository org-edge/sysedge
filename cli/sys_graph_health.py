"""
sys_graph_health.py — Graph health check for the system knowledge graph.

Runs a suite of diagnostic queries and prints a structured report.
Exit code 0 = clean; 1 = warnings found.

Usage:
    python3 scripts/sys_graph_health.py
    python3 scripts/sys_graph_health.py --instance core
    python3 scripts/sys_graph_health.py --fix-paths     # fix known path mismatches
"""
import argparse
import os
import sys
from pathlib import Path

_root = Path(__file__).parent.parent
try:
    from dotenv import load_dotenv
    load_dotenv(_root / ".env", override=False)
except ImportError:
    pass

NEO4J_URI  = (os.environ.get("SYSGRAPH_NEO4J_URI")
              or os.environ.get("NEO4J_URI", "bolt://localhost:7687"))
NEO4J_USER = (os.environ.get("SYSGRAPH_NEO4J_USER")
              or os.environ.get("NEO4J_USER", "neo4j"))
NEO4J_PW   = (os.environ.get("SYSGRAPH_NEO4J_PASSWORD")
              or os.environ.get("NEO4J_PASSWORD", "password"))


def _driver():
    try:
        from neo4j import GraphDatabase
        import logging
        logging.getLogger("neo4j").setLevel(logging.ERROR)
    except ImportError:
        print("ERROR: pip install neo4j", file=sys.stderr)
        sys.exit(1)
    return GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PW),
                                connection_timeout=5, max_transaction_retry_time=10)


W = 66
WARN = "⚠ "
OK   = "✓ "
INFO = "  "


def section(title: str):
    print(f"\n{'─' * W}")
    print(f"  {title}")
    print(f"{'─' * W}")


def check(label: str, value, threshold=0, warn_if_above=True, fmt=str):
    """Print a check line. Warns if value > threshold (or < if warn_if_above=False)."""
    failing = (value > threshold) if warn_if_above else (value < threshold)
    tag = WARN if failing else OK
    print(f"  {tag} {label:<52} {fmt(value)}")
    return failing


def run(args):
    inst_filter = args.instance or ""
    drv = _driver()
    warnings = 0

    with drv.session() as s:
        # Probe connection before printing any output.
        s.run("RETURN 1").consume()

        # ── Summary counts ────────────────────────────────────────────────────
        section("GRAPH INVENTORY")
        counts = s.run("""
            MATCH (n) WHERE any(l IN labels(n) WHERE l STARTS WITH 'Sys')
            RETURN labels(n)[0] AS label, count(n) AS cnt
            ORDER BY label
        """).data()
        for r in counts:
            print(f"  {INFO} {r['label']:<30} {r['cnt']:6d}")

        # Architecture decisions inventory (count + constrained-modules edge count)
        adr_total = s.run("MATCH (a:SysArchDecision) RETURN count(a) AS cnt").single()["cnt"]
        adr_edges = s.run(
            "MATCH (:SysModule)-[r:CONSTRAINED_BY]->(:SysArchDecision) RETURN count(r) AS cnt"
        ).single()["cnt"]
        print(f"  {INFO} {'SysArchDecision (ADRs)':<30} {adr_total:6d}  "
              f"({adr_edges} module CONSTRAINED_BY edges)")

        # ── Coverage by instance ──────────────────────────────────────────────
        section("FEATURE COVERAGE BY INSTANCE")
        cov = s.run("""
            MATCH (m:SysModule)-[:PROVIDES]->(f:SysFeature)
            WHERE $inst = '' OR m.instance = $inst
            OPTIONAL MATCH (t:SysTest)-[:VERIFIES]->(f)
            RETURN m.instance AS inst,
                   count(DISTINCT f)  AS total,
                   count(DISTINCT CASE WHEN t IS NOT NULL THEN f END) AS covered
            ORDER BY inst
        """, inst=inst_filter).data()
        for r in cov:
            pct = int(100 * r["covered"] / r["total"]) if r["total"] else 0
            tag = OK if r["covered"] == r["total"] else (WARN if pct < 50 else INFO)
            print(f"  {tag} {r['inst']:<20} {r['covered']:3d}/{r['total']:<3d}  ({pct:3d}%)")

        # ── Orphan symbols (no module edge) ───────────────────────────────────
        section("ORPHAN SYMBOLS (no CONTAINS_SYMBOL edge)")
        total_syms = s.run("MATCH (sym:SysSymbol) RETURN count(sym) AS cnt").single()["cnt"]
        orphan_syms = s.run("""
            MATCH (sym:SysSymbol) WHERE NOT ()-[:CONTAINS_SYMBOL]->(sym)
            RETURN count(sym) AS cnt
        """).single()["cnt"]
        orphan_pct = round(100 * orphan_syms / total_syms, 1) if total_syms else 0
        # Warn only if >8% orphaned — some shared/utility code is legitimately unattributed
        if check(f"Orphan symbols ({orphan_pct}% of {total_syms})", orphan_syms, threshold=int(0.08 * total_syms)):
            warnings += 1
            # Show the top offending files
            top = s.run("""
                MATCH (sym:SysSymbol) WHERE NOT ()-[:CONTAINS_SYMBOL]->(sym)
                RETURN sym.file AS file, count(sym) AS cnt
                ORDER BY cnt DESC LIMIT 8
            """).data()
            for r in top:
                print(f"      {r['cnt']:4d}  {r['file']}")

        # ── Tests with no package (seeded manually, not scanned) ─────────────
        section("TESTS WITH NO PACKAGE LINK")
        orphan_tests = s.run("""
            MATCH (t:SysTest) WHERE NOT ()-[:CONTAINS_TEST]->(t)
            RETURN count(t) AS cnt
        """).single()["cnt"]
        if check("Tests not linked to a SysTestPackage", orphan_tests, threshold=0):
            warnings += 1
            sample = s.run("""
                MATCH (t:SysTest) WHERE NOT ()-[:CONTAINS_TEST]->(t)
                RETURN t.id AS id LIMIT 6
            """).data()
            for r in sample:
                print(f"      {r['id']}")

        # ── Empty modules (no features) ───────────────────────────────────────
        section("MODULES WITH NO FEATURES")
        empty_mods = s.run("""
            MATCH (m:SysModule)
            WHERE ($inst = '' OR m.instance = $inst)
              AND NOT (m)-[:PROVIDES]->()
              AND NOT m.type IN ['pattern']
            RETURN m.id AS id, m.instance AS inst, m.type AS type
            ORDER BY m.instance, m.id
        """, inst=inst_filter).data()
        # Distinguish populated instances (framework, core) from pending ones
        populated   = {"framework", "core"}
        unexpected  = [r for r in empty_mods if r["inst"] in populated]
        pending     = [r for r in empty_mods if r["inst"] not in populated]
        if check("Populated-instance modules with no features", len(unexpected), threshold=0):
            warnings += 1
            for r in unexpected:
                print(f"      {r['inst']:<15} {r['id']}")
        if pending:
            print(f"  {INFO} Pending instances (features not yet populated): "
                  f"{len(pending)} modules across "
                  f"{len({r['inst'] for r in pending})} instances")

        # ── Unlinked endpoints ────────────────────────────────────────────────
        section("ENDPOINTS")
        unlinked_ep = s.run("""
            MATCH (e:SysEndpoint) WHERE NOT (e)-[:IMPLEMENTS]->()
            RETURN count(e) AS cnt
        """).single()["cnt"]
        check("Endpoints without IMPLEMENTS edge", unlinked_ep, threshold=0)
        total_ep = s.run("MATCH (e:SysEndpoint) RETURN count(e) AS cnt").single()["cnt"]
        print(f"  {INFO} Total endpoints registered: {total_ep}")

        # ── Scenario chapter coverage ─────────────────────────────────────────
        section("SCENARIO CHAPTER LINKS")
        ch_stats = s.run("""
            MATCH (sc:SysScenario)-[:HAS_CHAPTER]->(ch:SysScenarioChapter)
            OPTIONAL MATCH (ch)-[:EXERCISES]->(f:SysFeature)
            RETURN sc.name AS scenario,
                   count(DISTINCT ch) AS total,
                   count(DISTINCT CASE WHEN f IS NOT NULL THEN ch END) AS linked
            ORDER BY sc.name
        """).data()
        for r in ch_stats:
            pct = int(100 * r["linked"] / r["total"]) if r["total"] else 0
            tag = OK if r["linked"] == r["total"] else (INFO if pct > 0 else WARN)
            print(f"  {tag} {r['scenario'][:45]:<45} {r['linked']:2d}/{r['total']}")

        # ── Training module links ─────────────────────────────────────────────
        section("TRAINING MODULE LINKS")
        trn_stats = s.run("""
            MATCH (tp:SysTrainingProgram)-[:HAS_MODULE]->(m:SysTrainingModule)
            OPTIONAL MATCH (m)-[:TEACHES]->(f:SysFeature)
            RETURN tp.name AS program,
                   count(DISTINCT m) AS total,
                   count(DISTINCT CASE WHEN f IS NOT NULL THEN m END) AS linked
            ORDER BY tp.name
        """).data()
        for r in trn_stats:
            pct = int(100 * r["linked"] / r["total"]) if r["total"] else 0
            tag = OK if r["linked"] == r["total"] else (INFO if pct > 0 else WARN)
            print(f"  {tag} {r['program'][:45]:<45} {r['linked']:2d}/{r['total']}")

        # ── Test coverage gaps (features with 0 tests, by instance) ──────────
        section("UNTESTED FEATURES")
        gaps = s.run("""
            MATCH (m:SysModule)-[:PROVIDES]->(f:SysFeature)
            WHERE ($inst = '' OR m.instance = $inst)
              AND NOT ()-[:VERIFIES]->(f)
            RETURN m.instance AS inst, m.id AS mod, f.id AS fid, f.name AS fname
            ORDER BY inst, mod, fid
        """, inst=inst_filter).data()
        if gaps:
            last_inst = None
            for r in gaps:
                if r["inst"] != last_inst:
                    print(f"\n  {r['inst']}")
                    last_inst = r["inst"]
                print(f"      ✗  {r['fid']:<16} {r['fname']}")
        else:
            print(f"  {OK} All features have at least one test linked")

        # ── User stories without feature links ────────────────────────────────
        section("USER STORIES")
        story_gaps = s.run("""
            MATCH (us:SysUserStory) WHERE NOT (us)-[:REQUIRES]->()
            RETURN us.id, us.title
        """).data()
        total_stories = s.run("MATCH (us:SysUserStory) RETURN count(us) AS cnt").single()["cnt"]
        print(f"  {INFO} Total user stories: {total_stories}")
        if check("Stories with no REQUIRES edge", len(story_gaps), threshold=0):
            warnings += 1
            for r in story_gaps:
                print(f"      {r['us.id']}  {r['us.title']}")

        # ══ STRUCTURAL INTEGRITY ══════════════════════════════════════════════

        section("STRUCTURAL INTEGRITY — required fields")

        # Features must belong to a module
        orphan_feats = s.run("""
            MATCH (f:SysFeature)
            WHERE NOT (:SysModule)-[:PROVIDES]->(f)
              AND NOT f.status IN ['Superseded']
            RETURN f.id AS id, f.name AS name
            ORDER BY f.id
        """).data()
        if check("Features with no module (PROVIDES edge)", len(orphan_feats), threshold=0):
            warnings += 1
            for r in orphan_feats[:8]:
                print(f"      {r['id']:<16} {r['name']}")
            if len(orphan_feats) > 8:
                print(f"      … and {len(orphan_feats)-8} more")

        # Use cases must have instance set
        uc_no_inst = s.run("""
            MATCH (uc:SysUseCase)
            WHERE uc.instance IS NULL OR uc.instance = ''
            RETURN uc.id AS id, uc.title AS title
            ORDER BY uc.id
        """).data()
        if check("Use cases with no instance set", len(uc_no_inst), threshold=0):
            warnings += 1
            for r in uc_no_inst[:8]:
                print(f"      {r['id']:<20} {(r['title'] or '')[:50]}")

        # Enhancements must have instance set
        enh_no_inst = s.run("""
            MATCH (e:SysEnhancement)
            WHERE (e.instance IS NULL OR e.instance = '')
              AND e.status <> 'done'
            RETURN e.id AS id, e.title AS title
            ORDER BY e.id
        """).data()
        if check("Open enhancements with no instance", len(enh_no_inst), threshold=0):
            warnings += 1
            for r in enh_no_inst[:6]:
                print(f"      {r['id']:<12} {(r['title'] or '')[:55]}")

        # Enhancements must have valid instance
        known_instances = {
            "p1","framework","core","manage","plan","platform",
            "training","licmcp","master","deploy","architect","sys-graph",
        }
        enh_bad_inst = s.run("""
            MATCH (e:SysEnhancement)
            WHERE e.instance IS NOT NULL AND e.instance <> ''
              AND e.status <> 'done'
            RETURN DISTINCT e.instance AS inst, count(e) AS cnt
            ORDER BY inst
        """).data()
        invalid_insts = [(r["inst"], r["cnt"]) for r in enh_bad_inst
                         if r["inst"] not in known_instances]
        if check("Enhancements with unrecognised instance", len(invalid_insts), threshold=0):
            warnings += 1
            for inst, cnt in invalid_insts:
                print(f"      '{inst}' ({cnt} enhancements) — not a known instance")

        # Modules must have instance set
        mod_no_inst = s.run("""
            MATCH (m:SysModule)
            WHERE m.instance IS NULL OR m.instance = ''
            RETURN m.id AS id, m.name AS name
            ORDER BY m.id
        """).data()
        if check("Modules with no instance set", len(mod_no_inst), threshold=0):
            warnings += 1
            for r in mod_no_inst[:6]:
                print(f"      {r['id']:<22} {r['name']}")

        # Enhancements marked done without completedAt
        enh_done_no_ts = s.run("""
            MATCH (e:SysEnhancement {status:'done'})
            WHERE e.completedAt IS NULL
            RETURN count(e) AS cnt
        """).single()["cnt"]
        if check("Done enhancements missing completedAt timestamp", enh_done_no_ts, threshold=0):
            warnings += 1

        # ══ TRACEABILITY INTEGRITY ════════════════════════════════════════════

        section("TRACEABILITY INTEGRITY — required relationships")

        # Use cases must have a REALIZED_BY edge from a user story
        uc_orphan = s.run("""
            MATCH (uc:SysUseCase)
            WHERE NOT (:SysUserStory)-[:REALIZED_BY]->(uc)
            RETURN uc.id AS id, uc.instance AS inst,
                   coalesce(uc.title, uc.name, '') AS title
            ORDER BY uc.instance, uc.id
        """).data()
        if check("Use cases with no parent user story (REALIZED_BY)", len(uc_orphan), threshold=0):
            warnings += 1
            for r in uc_orphan[:8]:
                print(f"      [{r['inst']:<12}]  {r['id']:<22}  {r['title'][:40]}")
            if len(uc_orphan) > 8:
                print(f"      … and {len(uc_orphan)-8} more")

        # Use cases should link to at least one feature
        uc_no_feat = s.run("""
            MATCH (uc:SysUseCase)
            WHERE NOT (uc)-[:REQUIRES]->(:SysFeature)
            RETURN uc.id AS id, uc.instance AS inst,
                   coalesce(uc.title, uc.name, '') AS title
            ORDER BY uc.instance, uc.id
        """).data()
        # Info-level only — UCs may be authored before features are linked
        tag = WARN if len(uc_no_feat) > 10 else INFO
        print(f"  {tag} {'Use cases with no REQUIRES feature':<52} {len(uc_no_feat)}")
        if len(uc_no_feat) > 0:
            for r in uc_no_feat[:5]:
                print(f"      [{r['inst']:<12}]  {r['id']}")
            if len(uc_no_feat) > 5:
                print(f"      … and {len(uc_no_feat)-5} more — link with link-enhancement or UC REQUIRES edges")

        # Stories with no use case at all
        us_no_uc = s.run("""
            MATCH (us:SysUserStory)
            WHERE NOT (us)-[:REALIZED_BY]->(:SysUseCase)
            RETURN count(us) AS cnt
        """).single()["cnt"]
        print(f"  {INFO} User stories with no use case: {us_no_uc} "
              f"({'✓ ok' if us_no_uc == 0 else 'use architect to derive UCs'})")

        # Superseded features still linked to open enhancements
        sup_enh = s.run("""
            MATCH (e:SysEnhancement)-[:EXTENDS]->(f:SysFeature {status:'Superseded'})
            WHERE e.status <> 'done'
            RETURN e.id AS eid, f.id AS fid
            ORDER BY e.id
        """).data()
        if check("Open enhancements linked to Superseded features", len(sup_enh), threshold=0):
            warnings += 1
            for r in sup_enh[:6]:
                print(f"      {r['eid']} → {r['fid']} (Superseded) — retire or relink")

        # ══ ARCHITECTURE STANDARDS ════════════════════════════════════════════

        section("ARCHITECTURE STANDARDS COMPLIANCE")

        total_stds = s.run("MATCH (n:SysArchStd) WHERE NOT n.id STARTS WITH 'ARCH-' RETURN count(n) AS n").single()["n"]
        addressed  = s.run("""
            MATCH (n:SysArchStd) WHERE NOT n.id STARTS WITH 'ARCH-'
              AND exists(()-[:ADDRESSES]->(n))
            RETURN count(n) AS n
        """).single()["n"]
        unaddressed = total_stds - addressed
        pct = int(100 * addressed / total_stds) if total_stds else 0
        tag = OK if unaddressed == 0 else (WARN if pct < 75 else INFO)
        print(f"  {tag} Standards addressed by ADR or pattern: "
              f"{addressed}/{total_stds} ({pct}%)")

        gaps_by_cat = s.run("""
            MATCH (n:SysArchStd)
            WHERE NOT n.id STARTS WITH 'ARCH-'
              AND NOT exists(()-[:ADDRESSES]->(n))
            RETURN n.category AS cat, n.type AS type, n.id AS id
            ORDER BY n.category, n.type, n.id
        """).data()
        if gaps_by_cat:
            cur = None
            for g in gaps_by_cat:
                if g["cat"] != cur:
                    cur = g["cat"]
                    print(f"\n    [{cur.upper()}]")
                marker = WARN if g["type"] == "standard" else INFO
                print(f"      {marker} {g['id']:<18} [{g['type']}]")
            standards_ungapped = [g for g in gaps_by_cat if g["type"] == "standard"]
            if standards_ungapped:
                warnings += 1

        # ADRs with adopted status but no date
        adr_no_date = s.run("""
            MATCH (a:SysArchDecision {status:'adopted'})
            WHERE a.date IS NULL
            RETURN a.id AS id, a.title AS title ORDER BY a.id
        """).data()
        if check("Adopted ADRs missing date field", len(adr_no_date), threshold=0):
            warnings += 1
            for r in adr_no_date[:4]:
                print(f"      {r['id']:<10} {r['title'][:55]}")

        # ══ DATA QUALITY ══════════════════════════════════════════════════════

        section("DATA QUALITY")

        # Stale in-progress enhancements (>48h without close)
        from datetime import datetime, timezone, timedelta
        stale_cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        stale_inprog = s.run("""
            MATCH (e:SysEnhancement {status:'in-progress'})
            WHERE e.startedAt < $cutoff
            RETURN e.id AS id, e.instance AS inst,
                   e.title AS title, e.startedAt AS started
            ORDER BY e.startedAt
        """, cutoff=stale_cutoff).data()
        if check("In-progress enhancements stale >48h", len(stale_inprog), threshold=0):
            warnings += 1
            for r in stale_inprog[:4]:
                print(f"      {r['id']:<12} [{r['inst']:<12}] started {str(r['started'])[:10]}")
                print(f"        {r['title'][:60]}")

        # Unactioned feedback older than 7 days
        import datetime as _dt
        old_cutoff = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=7)).isoformat()
        old_fb = s.run("""
            MATCH (f:SysFeedback)
            WHERE (f.actioned IS NULL OR f.actioned = false)
              AND f.createdAt < $cutoff
            RETURN count(f) AS cnt
        """, cutoff=old_cutoff).single()["cnt"]
        tag = WARN if old_fb > 20 else INFO
        print(f"  {tag} {'Unactioned feedback older than 7 days':<52} {old_fb}")

        # Enhancement count by instance (sanity check)
        enh_dist = s.run("""
            MATCH (e:SysEnhancement)
            WHERE e.status <> 'done'
            RETURN e.instance AS inst, count(e) AS cnt
            ORDER BY cnt DESC
        """).data()
        print(f"\n  Open enhancement distribution:")
        for r in enh_dist:
            bar = "█" * min(r["cnt"] // 2, 30)
            print(f"    {(r['inst'] or 'none'):<14} {r['cnt']:4d}  {bar}")

        # ── Notes and Proposals ───────────────────────────────────────────────
        section("NOTES & PROPOSALS")

        stale_cutoff = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=7)).isoformat()
        stale_notes = s.run("""
            MATCH (n:SysNote)
            WHERE n.status = 'active'
              AND n.createdAt < $cutoff
              AND (n.expiresAt IS NULL OR n.expiresAt = '')
            RETURN n.instance AS inst, n.id AS id, n.createdAt AS created,
                   substring(n.body, 0, 60) AS snippet
            ORDER BY n.createdAt ASC
        """, cutoff=stale_cutoff).data()
        warnings += check("Active notes older than 7 days (consider expiring)",
                          len(stale_notes))
        for r in stale_notes[:4]:
            print(f"      {r['id']:<12} [{r['inst']:<12}] {str(r['created'])[:10]}")
            print(f"        {r['snippet']}")

        orphan_proposals = s.run("""
            MATCH (p:SysProposal)
            WHERE p.status IN ['draft','accepted']
              AND (p.instance IS NULL OR p.instance = '')
            RETURN count(p) AS cnt
        """).single()["cnt"]
        warnings += check("Open proposals with no instance set", orphan_proposals)

        stale_proposals = s.run("""
            MATCH (p:SysProposal)
            WHERE p.status = 'draft' AND p.createdAt < $cutoff
            RETURN count(p) AS cnt
        """, cutoff=(_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=14)).isoformat()).single()["cnt"]
        warnings += check("Draft proposals older than 14 days (decide or close)",
                          stale_proposals)

        note_dist = s.run("""
            MATCH (n:SysNote) WHERE n.status = 'active'
            RETURN n.instance AS inst, count(n) AS cnt ORDER BY cnt DESC
        """).data()
        if note_dist:
            print(f"\n  Active note distribution:")
            for r in note_dist:
                print(f"    {(r['inst'] or 'none'):<14} {r['cnt']:4d}")

        # ── Summary ───────────────────────────────────────────────────────────
        print(f"\n{'═' * W}")
        if warnings == 0:
            print(f"  ✓  Health check passed — no warnings")
        else:
            print(f"  ⚠  {warnings} warning category(s) found — see above")
        print(f"{'═' * W}\n")

    drv.close()
    return warnings


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--instance", default="", help="Filter checks to one instance")
    args = p.parse_args()
    try:
        warnings = run(args)
    except Exception as e:
        if "ServiceUnavailable" in type(e).__name__ or "Connection refused" in str(e):
            print(f"\n⚠  Graph database unavailable ({NEO4J_URI}) — start Docker and retry.\n",
                  file=sys.stderr)
            sys.exit(2)
        raise
    sys.exit(1 if warnings else 0)


if __name__ == "__main__":
    main()
