"""Benchmark of candidate query rewrites (original Q, rewrite Q') with ground-truth
soundness labels under declared integrity constraints.

Each case mirrors a failure mode (or a constraint-dependent success) that LLM-based
rewriters are known to produce: spurious join tuples, dropped DISTINCT, NOT IN / NULL
semantics, the COUNT bug, outer->inner join conversion, join elimination, etc.

Schemas are given as specs; the actual SQLite tables are created WITHOUT constraints so
that the data generator (not the engine) decides which constraints hold.
"""

# ---------------------------------------------------------------------------
# Schema specs
# ---------------------------------------------------------------------------

def dept_spec(unique_name=False):
    return {
        "cols": ["id", "dname", "budget"],
        "pk": "id",
        "notnull": {"id", "dname"},
        "unique": {"dname"} if unique_name else set(),
        "fk": {},
        "fd": [],
        "scale": 0.25,
    }


def emp_spec(dept_notnull=True, fk=True, mgr=True):
    return {
        "cols": ["id", "name", "dept_id", "salary", "mgr_id"],
        "pk": "id",
        "notnull": {"id", "name", "salary"} | ({"dept_id"} if dept_notnull else set()),
        "unique": set(),
        "fk": ({"dept_id": ("dept", "id")} if fk else {}) | ({"mgr_id": ("emp", "id")} if mgr else {}),
        "fd": [],
        "scale": 1.0,
    }


def R_spec(fd_b_c=False, b_notnull=True):
    return {
        "cols": ["A", "B", "C"],
        "pk": "A",
        "notnull": {"A", "C"} | ({"B"} if b_notnull else set()),
        "unique": set(),
        "fk": {},
        "fd": [("B", "C")] if fd_b_c else [],
        "scale": 1.0,
    }


HR = lambda **kw: {"dept": dept_spec(kw.get("unique_name", False)),
                   "emp": emp_spec(kw.get("dept_notnull", True), kw.get("fk", True))}

VIEWS_R = ["CREATE VIEW r1 AS SELECT A, B FROM R",
           "CREATE VIEW r2 AS SELECT DISTINCT B, C FROM R"]

# ---------------------------------------------------------------------------
# Cases: id, family, schema, extra DDL, Q, Q', sound?, note
# ---------------------------------------------------------------------------

CASES = [
    # --- view-based reconstruction (from our preliminary LLM study) ---------
    dict(id="view-recon/key-only", family="View reconstruction",
         schema={"R": R_spec(fd_b_c=False)}, ddl=VIEWS_R,
         q="SELECT A, B, C FROM R",
         r="SELECT r1.A, r1.B, r2.C FROM r1 JOIN r2 ON r1.B = r2.B",
         sound=False, note="key A only; join on non-key B is lossy (spurious tuples)"),
    dict(id="view-recon/fd", family="View reconstruction",
         schema={"R": R_spec(fd_b_c=True)}, ddl=VIEWS_R,
         q="SELECT A, B, C FROM R",
         r="SELECT r1.A, r1.B, r2.C FROM r1 JOIN r2 ON r1.B = r2.B",
         sound=True, note="FD B->C and B NOT NULL make the decomposition lossless"),
    dict(id="view-recon/fd-nullable", family="View reconstruction",
         schema={"R": R_spec(fd_b_c=True, b_notnull=False)}, ddl=VIEWS_R,
         q="SELECT A, B, C FROM R",
         r="SELECT r1.A, r1.B, r2.C FROM r1 JOIN r2 ON r1.B = r2.B",
         sound=False, note="FD holds but NULL B rows are dropped by the join"),

    # --- DISTINCT elimination ---------------------------------------------
    dict(id="distinct/key", family="DISTINCT elimination",
         schema=HR(), ddl=[],
         q="SELECT DISTINCT id, name FROM emp",
         r="SELECT id, name FROM emp",
         sound=True, note="projection contains the key"),
    dict(id="distinct/non-key", family="DISTINCT elimination",
         schema=HR(), ddl=[],
         q="SELECT DISTINCT dept_id, salary FROM emp",
         r="SELECT dept_id, salary FROM emp",
         sound=False, note="projection has no key; duplicates appear"),

    # --- IN-subquery to join ----------------------------------------------
    dict(id="in2join/key", family="Subquery unnesting",
         schema=HR(), ddl=[],
         q="SELECT e.name FROM emp e WHERE e.dept_id IN (SELECT id FROM dept WHERE budget > 5)",
         r="SELECT e.name FROM emp e JOIN dept d ON e.dept_id = d.id WHERE d.budget > 5",
         sound=True, note="subquery column is a key; join cannot duplicate"),
    dict(id="in2join/non-key", family="Subquery unnesting",
         schema=HR(), ddl=[],
         q="SELECT d.dname FROM dept d WHERE d.id IN (SELECT dept_id FROM emp WHERE salary > 5)",
         r="SELECT d.dname FROM dept d JOIN emp e ON d.id = e.dept_id WHERE e.salary > 5",
         sound=False, note="many employees per dept duplicate the dept"),

    # --- NOT IN vs NOT EXISTS ---------------------------------------------
    dict(id="notin/nullable", family="NULL semantics",
         schema=HR(), ddl=[],
         q="SELECT e.name FROM emp e WHERE e.id NOT IN (SELECT mgr_id FROM emp)",
         r="SELECT e.name FROM emp e WHERE NOT EXISTS (SELECT 1 FROM emp m WHERE m.mgr_id = e.id)",
         sound=False, note="a single NULL mgr_id empties NOT IN"),
    dict(id="notin/not-null", family="NULL semantics",
         schema=HR(), ddl=[],
         q="SELECT d.dname FROM dept d WHERE d.id NOT IN (SELECT dept_id FROM emp)",
         r="SELECT d.dname FROM dept d WHERE NOT EXISTS (SELECT 1 FROM emp e WHERE e.dept_id = d.id)",
         sound=True, note="dept_id NOT NULL makes the two equivalent"),

    # --- outer join simplification ----------------------------------------
    dict(id="outer2inner/fk-notnull", family="Join simplification",
         schema=HR(), ddl=[],
         q="SELECT e.name, d.dname FROM emp e LEFT JOIN dept d ON e.dept_id = d.id",
         r="SELECT e.name, d.dname FROM emp e JOIN dept d ON e.dept_id = d.id",
         sound=True, note="FK + NOT NULL: every emp has a matching dept"),
    dict(id="outer2inner/nullable", family="Join simplification",
         schema=HR(dept_notnull=False), ddl=[],
         q="SELECT e.name, d.dname FROM emp e LEFT JOIN dept d ON e.dept_id = d.id",
         r="SELECT e.name, d.dname FROM emp e JOIN dept d ON e.dept_id = d.id",
         sound=False, note="employees with NULL dept_id are lost"),
    dict(id="join-elim/fk", family="Join simplification",
         schema=HR(), ddl=[],
         q="SELECT e.name FROM emp e JOIN dept d ON e.dept_id = d.id",
         r="SELECT e.name FROM emp e",
         sound=True, note="FK + NOT NULL + key on dept.id"),
    dict(id="join-elim/no-fk", family="Join simplification",
         schema=HR(fk=False), ddl=[],
         q="SELECT e.name FROM emp e JOIN dept d ON e.dept_id = d.id",
         r="SELECT e.name FROM emp e",
         sound=False, note="no FK: dangling dept_id values"),

    # --- aggregation -------------------------------------------------------
    dict(id="second-highest", family="Aggregation",
         schema=HR(), ddl=[],
         q="SELECT MAX(a.salary) FROM emp a, emp b WHERE a.salary < b.salary",
         r="SELECT MAX(salary) FROM emp WHERE salary < (SELECT MAX(salary) FROM emp)",
         sound=True, note="GenRewrite Fig. 2 example"),
    dict(id="max2limit", family="Aggregation",
         schema=HR(), ddl=[],
         q="SELECT MAX(salary) FROM emp",
         r="SELECT salary FROM emp ORDER BY salary DESC LIMIT 1",
         sound=False, note="empty table: one NULL row vs. zero rows"),
    dict(id="agg-push/unique-name", family="Aggregation",
         schema=HR(unique_name=True), ddl=[],
         q="SELECT d.dname, SUM(e.salary) FROM emp e JOIN dept d ON e.dept_id = d.id GROUP BY d.dname",
         r="SELECT d.dname, x.s FROM dept d JOIN (SELECT dept_id, SUM(salary) AS s FROM emp GROUP BY dept_id) x ON x.dept_id = d.id",
         sound=True, note="dname UNIQUE: grouping by dname = grouping by id"),
    dict(id="agg-push/non-unique", family="Aggregation",
         schema=HR(unique_name=False), ddl=[],
         q="SELECT d.dname, SUM(e.salary) FROM emp e JOIN dept d ON e.dept_id = d.id GROUP BY d.dname",
         r="SELECT d.dname, x.s FROM dept d JOIN (SELECT dept_id, SUM(salary) AS s FROM emp GROUP BY dept_id) x ON x.dept_id = d.id",
         sound=False, note="two depts sharing a name are merged in Q only"),
    dict(id="having2where/group-col", family="Aggregation",
         schema=HR(), ddl=[],
         q="SELECT dept_id, COUNT(*) FROM emp GROUP BY dept_id HAVING dept_id > 2",
         r="SELECT dept_id, COUNT(*) FROM emp WHERE dept_id > 2 GROUP BY dept_id",
         sound=True, note="predicate on grouping column"),
    dict(id="having2where/aggregate", family="Aggregation",
         schema=HR(), ddl=[],
         q="SELECT dept_id FROM emp GROUP BY dept_id HAVING MIN(salary) > 3",
         r="SELECT DISTINCT dept_id FROM emp WHERE salary > 3",
         sound=False, note="MIN over the group is not a per-row filter"),

    # --- disjunction / set operations -------------------------------------
    dict(id="or2union/key", family="Set operations",
         schema=HR(), ddl=[],
         q="SELECT id, name FROM emp WHERE salary > 4 OR dept_id = 1",
         r="SELECT id, name FROM emp WHERE salary > 4 UNION SELECT id, name FROM emp WHERE dept_id = 1",
         sound=True, note="key in output makes UNION dedup exact"),
    dict(id="or2unionall", family="Set operations",
         schema=HR(), ddl=[],
         q="SELECT id, name FROM emp WHERE salary > 4 OR dept_id = 1",
         r="SELECT id, name FROM emp WHERE salary > 4 UNION ALL SELECT id, name FROM emp WHERE dept_id = 1",
         sound=False, note="rows satisfying both disjuncts are duplicated"),

    # --- correlated subqueries --------------------------------------------
    dict(id="decorr/count-bug", family="Subquery unnesting",
         schema=HR(), ddl=[],
         q="SELECT d.id FROM dept d WHERE (SELECT COUNT(*) FROM emp e WHERE e.dept_id = d.id) < 2",
         r="SELECT d.id FROM dept d JOIN (SELECT dept_id, COUNT(*) AS c FROM emp GROUP BY dept_id) x ON x.dept_id = d.id WHERE x.c < 2",
         sound=False, note="classic COUNT bug: empty departments vanish"),
    dict(id="decorr/avg", family="Subquery unnesting",
         schema=HR(), ddl=[],
         q="SELECT e.name FROM emp e WHERE e.salary > (SELECT AVG(salary) FROM emp e2 WHERE e2.dept_id = e.dept_id)",
         r="SELECT e.name FROM emp e JOIN (SELECT dept_id, AVG(salary) AS a FROM emp GROUP BY dept_id) x ON x.dept_id = e.dept_id WHERE e.salary > x.a",
         sound=True, note="AVG over empty group is NULL, comparison fails either way"),
    dict(id="exists-count", family="Subquery unnesting",
         schema=HR(), ddl=[],
         q="SELECT d.dname FROM dept d WHERE (SELECT COUNT(*) FROM emp e WHERE e.dept_id = d.id) > 0",
         r="SELECT d.dname FROM dept d WHERE EXISTS (SELECT 1 FROM emp e WHERE e.dept_id = d.id)",
         sound=True, note="COUNT(*) > 0 iff EXISTS"),
]
