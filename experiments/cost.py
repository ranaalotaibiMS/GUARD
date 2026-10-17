"""Cost gate experiment on SQLite: sound rewrites can still regress.

R1 second-highest salary (GenRewrite Fig. 2): sound, expected large speedup.
R2 decorrelation of a selective correlated AVG query: sound, but computes
   per-department averages for the whole table -> expected regression.
R3 COUNT-bug decorrelation: faster but unsound -> must be stopped by the verifier.
"""
import json
import random
import sqlite3
import statistics
import time
from pathlib import Path

REPS = 5


def timeit(conn, sql, reps=REPS):
    ts = []
    for _ in range(reps):
        t0 = time.perf_counter()
        conn.execute(sql).fetchall()
        ts.append(1000 * (time.perf_counter() - t0))
    return statistics.median(ts)


def build(n, n_dept=1000, distinct_sal=1000, indexes=True, seed=0):
    rng = random.Random(seed)
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE dept (id INTEGER PRIMARY KEY, dname TEXT NOT NULL, budget INTEGER)")
    conn.execute("CREATE TABLE emp (id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
                 "dept_id INTEGER NOT NULL REFERENCES dept(id), salary INTEGER NOT NULL, mgr_id INTEGER)")
    conn.executemany("INSERT INTO dept VALUES (?,?,?)",
                     [(d, f"d{d}", rng.randint(1, 100)) for d in range(1, n_dept + 1)])
    conn.executemany("INSERT INTO emp VALUES (?,?,?,?,?)",
                     [(i, f"e{i}", rng.randint(1, n_dept // 2),   # half the depts are empty
                       30000 + (i % distinct_sal) * 100, None) for i in range(1, n + 1)])
    if indexes:
        conn.execute("CREATE INDEX emp_dept ON emp(dept_id)")
    conn.execute("ANALYZE")
    return conn


def plan(conn, sql):
    return [r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall()]


R1 = ("SELECT MAX(a.salary) FROM emp a, emp b WHERE a.salary < b.salary",
      "SELECT MAX(salary) FROM emp WHERE salary < (SELECT MAX(salary) FROM emp)")
R2 = ("SELECT e.name FROM emp e WHERE e.id = 4242 AND e.salary > "
      "(SELECT AVG(salary) FROM emp e2 WHERE e2.dept_id = e.dept_id)",
      "SELECT e.name FROM emp e JOIN (SELECT dept_id, AVG(salary) AS a FROM emp GROUP BY dept_id) x "
      "ON x.dept_id = e.dept_id WHERE e.id = 4242 AND e.salary > x.a")
R3 = ("SELECT d.id FROM dept d WHERE (SELECT COUNT(*) FROM emp e WHERE e.dept_id = d.id) < 2",
      "SELECT d.id FROM dept d JOIN (SELECT dept_id, COUNT(*) AS c FROM emp GROUP BY dept_id) x "
      "ON x.dept_id = d.id WHERE x.c < 2")


def main():
    out = {"second_highest": [], "decorr_selective": [], "count_bug": []}
    for n in [1000, 2000, 4000, 8000, 16000]:
        conn = build(n, indexes=False)
        same = conn.execute(R1[0]).fetchall() == conn.execute(R1[1]).fetchall()
        reps = 3 if n >= 8000 else REPS
        t0, t1 = timeit(conn, R1[0], reps), timeit(conn, R1[1], reps)
        out["second_highest"].append(dict(n=n, orig_ms=t0, rew_ms=t1, same=same))
        print(f"R1 n={n:6d}  Q={t0:9.2f} ms  Q'={t1:7.3f} ms  speedup={t0 / t1:8.1f}x same={same}", flush=True)
    out["second_highest_plan"] = {"orig": plan(conn, R1[0]), "rew": plan(conn, R1[1])}

    for n in [10_000, 100_000, 1_000_000]:
        conn = build(n)
        t0, t1 = timeit(conn, R2[0]), timeit(conn, R2[1])
        same = sorted(conn.execute(R2[0]).fetchall()) == sorted(conn.execute(R2[1]).fetchall())
        out["decorr_selective"].append(dict(n=n, orig_ms=t0, rew_ms=t1, same=same))
        print(f"R2 n={n:7d}  Q={t0:8.3f} ms  Q'={t1:8.2f} ms  slowdown={t1 / t0:8.1f}x same={same}", flush=True)
        t0, t1 = timeit(conn, R3[0]), timeit(conn, R3[1])
        a, b = conn.execute(R3[0]).fetchall(), conn.execute(R3[1]).fetchall()
        out["count_bug"].append(dict(n=n, orig_ms=t0, rew_ms=t1, orig_rows=len(a), rew_rows=len(b)))
        print(f"R3 n={n:7d}  Q={t0:8.2f} ms  Q'={t1:8.2f} ms  rows {len(a)} vs {len(b)}", flush=True)
    out["decorr_plan"] = {"orig": plan(conn, R2[0]), "rew": plan(conn, R2[1])}
    Path("results").mkdir(exist_ok=True)
    Path("results/cost.json").write_text(json.dumps(out, indent=2))
    print(json.dumps({k: v for k, v in out.items() if k.endswith("plan")}, indent=1))


if __name__ == "__main__":
    main()
