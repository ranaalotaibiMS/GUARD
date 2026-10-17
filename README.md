# Trust, but Verify: Constraint-Aware Verification and Guidance for LLM-Generated Query Rewrites

LLMs often rewrite SQL queries into versions that are valid, plausible and fast. They give no guarantee that the rewrite is *semantically sound*, meaning equivalent to the original on every database that satisfies the schema's integrity constraints. The paper defines the problem of *constraint-sound LLM rewriting* and proposes **Guard**, a propose–verify–repair framework. Guard treats the LLM as an untrusted proposer and wraps it in database machinery:

| Mechanism | Idea |
|---|---|
| S1 Constraint-explicit prompting | Spell out constraints (and missing constraints) in natural language instead of DDL |
| S2 Static constraint reasoning | Chase/FD-based proofs, e.g. a lossless-join test for view reconstructions |
| S3 Constraint-aware boundary testing | Generate tiny, constraint-satisfying counterexample databases (key collisions, NULLs, empty tables) and feed them back to the LLM for repair |
| S4 Rule-grounded rewriting | Restrict the LLM to known-sound rewrite rules |
| S5 Optimizer-in-the-loop cost gate | Reject sound rewrites that are slower (EXPLAIN / measured latency) |

## Key results

- In a preliminary study, a frontier LLM noticed that a join over views could add spurious tuples only **9%** of the time when the key constraint was given in DDL, versus **79%** when the same constraint was stated in natural language.
- Benchmark: **24** (Q, Q′) pairs, **12** sound and **12** unsound, covering six families of rewrite errors: F1 lossy view reconstruction, F2 multiplicity errors, F3 NULL semantics, F4 empty-input semantics (the COUNT bug), F5 missing referential integrity, F6 aggregate misplacement. The pairs come in "twins": the same rewrite under two constraint sets, sound under one and unsound under the other.
- Checking a rewrite on one test database (V-inst, the usual practice) catches only **51.2%** of unsound rewrites. The boundary verifier (V-bound, S3) catches **100%**, with a median counterexample of **2 rows**. Ignoring constraints causes false alarms on **8 of 12** sound rewrites.
- The cost gate (S5) rejects a sound rewrite that is up to **659×** slower. A sound rewrite on the same benchmark can be up to 7,697× faster.

## Contents

- `experiments/`: about 600 lines of Python, standard library only, on SQLite
  - `cases.py`: the 24 rewrite pairs, each with its failure family, constraint set and soundness label
  - `verify.py`: the verifiers
    - **V-inst**: compare Q and Q′ on one realistic instance (200 employees, 50 departments), repeated on 20 instances
    - **V-rand**: differential testing on up to 400 random constraint-satisfying instances
    - **V-bound**: the proposed boundary-seeking generator (K = 400). Ablations: without query constants, and ignoring constraints (V-noIC)
    - **V-static**: the FD / lossless-join test
  - `cost.py`: cost-gate timing on three rewrites. R1 is a sound second-highest-salary rewrite with a large speedup. R2 is a sound decorrelation that is much slower. R3 is a faster but unsound COUNT-bug decorrelation.
  - `make_tables.py`: turns `results/*.json` into `tables/*.tex`
  - `results/`: `verify.json` and `cost.json`

## Reproduce

Requires Python 3 (standard library only; SQLite is built in). `verify.py` takes the number of seeds (default 10) and the instance budget (default 400). The paper uses 20 seeds.

```bash
cd experiments && python3 verify.py 20 400 && python3 cost.py && python3 make_tables.py
```

The rewrites match outputs reported for LLM rewriters and seen in the preliminary study. The experiment does not call an LLM live, so results are deterministic for a given seed.
