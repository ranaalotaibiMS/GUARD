"""Constraint-aware verification of query rewrites on SQLite.

Verifiers compared (see paper, Section 6):
  V-inst   : run Q and Q' on ONE realistic test instance and compare (what most
             LLM-rewrite pipelines do today).
  V-rand   : differential testing on many random, constraint-satisfying instances
             with large value domains.
  V-bound  : (proposed) constraint-aware, boundary-seeking generator: tiny domains to
             force key collisions, NULLs where allowed, empty tables, small sizes first.
  V-noIC   : the same boundary generator but IGNORING declared constraints
             (measures false alarms on rewrites that are sound only under constraints).
  V-static : chase/FD-based lossless-join test (applies to view reconstructions only).
"""
import json
import random
import re
import sqlite3
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

from cases import CASES

# ---------------------------------------------------------------------------
# Instance generation
# ---------------------------------------------------------------------------

def topo_order(schema):
    order, seen = [], set()

    def visit(t):
        if t in seen:
            return
        seen.add(t)
        for (pt, _) in schema[t]["fk"].values():
            if pt != t:
                visit(pt)
        order.append(t)

    for t in schema:
        visit(t)
    return order


def gen_instance(schema, rng, *, n, domain, null_p, respect_ic=True, values=None, indep=False):
    """Return {table: [row dicts]}.  n = base cardinality, scaled per table.
    values: optional pool of interesting non-key values (query-aware boundary values)."""
    data = {}
    for t in topo_order(schema):
        spec = schema[t]
        if indep:      # boundary mode: each table independently sized in [0, n]
            size = rng.randint(0, n)
        else:
            size = int(round(n * spec["scale"])) if n > 0 else 0
            if n > 0 and spec["scale"] < 1 and size == 0:
                size = rng.randint(0, 1)
        cols = spec["cols"]
        rows = []
        # pre-draw key / unique columns
        uniq_pools = {}
        if respect_ic:
            for c in ({spec["pk"]} | spec["unique"]) - {None}:
                pool = list(range(1, size + domain + 1))
                rng.shuffle(pool)
                uniq_pools[c] = pool[:size]
        fd_maps = {lhs: {} for (lhs, _) in spec["fd"]} if respect_ic else {}
        for i in range(size):
            row = {}
            for c in cols:
                nullable = (not respect_ic) or (c not in spec["notnull"])
                if c in uniq_pools:
                    v = uniq_pools[c][i]
                elif respect_ic and c in spec["fk"]:
                    pt, pc = spec["fk"][c]
                    parent = data.get(pt, rows if pt == t else [])
                    choices = [r[pc] for r in parent]
                    if pt == t:  # self reference: only already-generated rows + allow NULL
                        choices = [r[pc] for r in rows]
                    if not choices or (nullable and rng.random() < null_p):
                        v = None
                    else:
                        v = rng.choice(choices)
                else:
                    if nullable and rng.random() < null_p:
                        v = None
                    elif values:
                        v = rng.choice(values)
                    else:
                        v = rng.randint(1, domain)
                if c in ("name", "dname") and v is not None:
                    v = f"n{v}"
                row[c] = v
            if respect_ic:
                # FK with NOT NULL and empty parent -> row impossible
                bad = any(row[c] is None and c in spec["notnull"] for c in cols)
                if bad:
                    continue
                for (lhs, rhs) in spec["fd"]:
                    if row[lhs] is not None:
                        row[rhs] = fd_maps[lhs].setdefault(row[lhs], row[rhs])
            rows.append(row)
        data[t] = rows
    return data


def load(conn, schema, ddl, data):
    for t, spec in schema.items():
        conn.execute(f"CREATE TABLE {t} ({', '.join(spec['cols'])})")
        if data[t]:
            ph = ", ".join("?" for _ in spec["cols"])
            conn.executemany(f"INSERT INTO {t} VALUES ({ph})",
                             [tuple(r[c] for c in spec["cols"]) for r in data[t]])
    for s in ddl:
        conn.execute(s)


def norm(v):
    return round(v, 6) if isinstance(v, float) else v


def run_bag(conn, sql):
    return Counter(tuple(norm(v) for v in row) for row in conn.execute(sql).fetchall())


def differs(case, data):
    conn = sqlite3.connect(":memory:")
    try:
        load(conn, case["schema"], case["ddl"], data)
        return run_bag(conn, case["q"]) != run_bag(conn, case["r"])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Verifiers
# ---------------------------------------------------------------------------

def v_inst(case, seed):
    rng = random.Random(seed)
    data = gen_instance(case["schema"], rng, n=200, domain=1000, null_p=0.0)
    return differs(case, data), 1, sum(len(v) for v in data.values())


def query_constants(case):
    return sorted({int(c) for c in re.findall(r"[<>=]\s*(\d+)", case["q"] + " " + case["r"])})


def boundary_values(case):
    """Tiny domain {1,2,3} plus c-1, c, c+1 for every constant compared against in Q or Q'."""
    vals = {1, 2, 3}
    for c in query_constants(case):
        vals |= {c - 1, c, c + 1}
    return sorted(v for v in vals if v >= 1)


def v_search(case, seed, *, budget, boundary, respect_ic=True, const_aware=True):
    """Return (counterexample_found, instances_tried, cex_total_rows)."""
    rng = random.Random(seed)
    bvals = boundary_values(case) if const_aware else [1, 2, 3]
    for k in range(1, budget + 1):
        if boundary:
            n = min(1 + k // 20, 8)                  # small instances first, grow slowly
            k_vals = rng.randint(1, len(bvals))
            values = rng.sample(bvals, k_vals)       # few distinct values -> collisions
            domain = len(bvals)
            null_p = 0.3
        else:
            n = rng.randint(1, 50)
            domain = 1000
            null_p = 0.05
            values = None
        data = gen_instance(case["schema"], rng, n=n, domain=domain, null_p=null_p,
                            respect_ic=respect_ic, values=values, indep=boundary)
        if differs(case, data):
            return True, k, sum(len(v) for v in data.values())
    return False, budget, None


# --- static: lossless-join test via FD closure (Aho, Beeri, Ullman 1979) ----

def closure(attrs, fds):
    attrs = set(attrs)
    changed = True
    while changed:
        changed = False
        for lhs, rhs in fds:
            if set(lhs) <= attrs and not set(rhs) <= attrs:
                attrs |= set(rhs)
                changed = True
    return attrs


def v_static(case):
    """Applicable only to two-view reconstructions of R. Returns True (sound),
    False (unsound) or None (not applicable)."""
    if not case["id"].startswith("view-recon"):
        return None
    spec = case["schema"]["R"]
    fds = [((spec["pk"],), tuple(spec["cols"]))] + [((l,), (r,)) for l, r in spec["fd"]]
    r1, r2 = {"A", "B"}, {"B", "C"}
    common = r1 & r2
    lossless = closure(common, fds) >= r1 or closure(common, fds) >= r2
    join_null_safe = common <= spec["notnull"]
    return lossless and join_null_safe


# ---------------------------------------------------------------------------

def main(seeds=10, budget=400):
    out = []
    for case in CASES:
        res = {"id": case["id"], "family": case["family"], "sound": case["sound"],
               "note": case["note"]}
        t0 = time.perf_counter()
        res["inst"] = sum(v_inst(case, s)[0] for s in range(seeds)) / seeds
        t1 = time.perf_counter()
        rr = [v_search(case, s, budget=budget, boundary=False) for s in range(seeds)]
        t2 = time.perf_counter()
        bb = [v_search(case, s, budget=budget, boundary=True) for s in range(seeds)]
        t3 = time.perf_counter()
        nn = [v_search(case, s, budget=budget, boundary=True, respect_ic=False) for s in range(seeds)]
        bc = [v_search(case, s, budget=budget, boundary=True, const_aware=False) for s in range(seeds)]
        res["bound_noconst"] = sum(f for f, _, _ in bc) / seeds
        res["rand_cex_rows"] = statistics.median(c for f, _, c in rr if f) if any(f for f, _, _ in rr) else None
        res["rand"] = sum(f for f, _, _ in rr) / seeds
        res["bound"] = sum(f for f, _, _ in bb) / seeds
        res["noic"] = sum(f for f, _, _ in nn) / seeds
        res["rand_k"] = statistics.median(k for f, k, _ in rr if f) if any(f for f, _, _ in rr) else None
        res["bound_k"] = statistics.median(k for f, k, _ in bb if f) if any(f for f, _, _ in bb) else None
        res["bound_cex_rows"] = statistics.median(c for f, _, c in bb if f) if any(f for f, _, _ in bb) else None
        res["static"] = v_static(case)
        res["ms_inst"] = 1000 * (t1 - t0) / seeds
        res["ms_rand"] = 1000 * (t2 - t1) / seeds
        res["ms_bound"] = 1000 * (t3 - t2) / seeds
        out.append(res)
        print(f"{case['id']:26s} sound={case['sound']!s:5s} inst={res['inst']:.1f} "
              f"rand={res['rand']:.1f} bound={res['bound']:.1f} noIC={res['noic']:.1f} "
              f"noC={res['bound_noconst']:.1f} k_r={res['rand_k']} k_b={res['bound_k']} cexR={res['rand_cex_rows']} cexB={res['bound_cex_rows']} static={res['static']} "
              f"ms_b={res['ms_bound']:.1f}", flush=True)
    Path("results").mkdir(exist_ok=True)
    Path("results/verify.json").write_text(json.dumps(out, indent=2))


if __name__ == "__main__":
    main(*(int(a) for a in sys.argv[1:]))
