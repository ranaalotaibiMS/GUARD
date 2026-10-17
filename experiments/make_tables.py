"""Turn results/*.json into LaTeX snippets included by the paper."""
import json
import statistics
from pathlib import Path

V = json.loads(Path("results/verify.json").read_text())
C = json.loads(Path("results/cost.json").read_text())
OUT = Path("../tables")
OUT.mkdir(exist_ok=True)


def pct(x):
    v = 100 * x
    return f"{v:.0f}" if v == round(v) else f"{v:.1f}"


def cell(x, sound):
    # unsound: detection rate (higher is better); sound: false-alarm rate (lower is better)
    s = pct(x)
    if (not sound and x < 1) or (sound and x > 0):
        return r"\textbf{" + s + "}"
    return s


def num(x):
    return "--" if x is None else (f"{x:.0f}" if x == int(x) else f"{x:.1f}")


# ---- per-case table --------------------------------------------------------
rows = []
for r in V:
    s = r["sound"]
    rows.append(" & ".join([
        r"\texttt{" + r["id"].replace("_", r"\_") + "}",
        "S" if s else "U",
        cell(r["inst"], s), cell(r["rand"], s), cell(r["bound_noconst"], s),
        cell(r["bound"], s), cell(r["noic"], s),
        num(r["rand_cex_rows"]), num(r["bound_cex_rows"]),
    ]) + r" \\")
(OUT / "per_case.tex").write_text("\n".join(rows) + "\n")

# ---- summary table ---------------------------------------------------------
uns = [r for r in V if not r["sound"]]
snd = [r for r in V if r["sound"]]


def summ(key, cex=None):
    det = statistics.mean(r[key] for r in uns)
    full = sum(r[key] == 1 for r in uns)
    fa = sum(r[key] > 0 for r in snd)
    cx = [r[cex] for r in uns if cex and r[cex] is not None]
    return pct(det), f"{full}/{len(uns)}", f"{fa}/{len(snd)}", (num(statistics.median(cx)) if cx else "--")


lines = []
for name, key, cex in [("V-inst (one test DB)", "inst", None),
                       ("V-rand", "rand", "rand_cex_rows"),
                       ("V-bound w/o constants", "bound_noconst", None),
                       (r"\textbf{V-bound (S3, ours)}", "bound", "bound_cex_rows"),
                       (r"V-bound ignoring $\Sigma$", "noic", None)]:
    lines.append(" & ".join([name, *summ(key, cex)]) + r" \\")
st = [r for r in V if r["static"] is not None]
st_ok = sum((r["static"] is True) == r["sound"] for r in st)
lines.append(r"V-static (S2) & \multicolumn{3}{c}{" + f"{st_ok}/{len(st)} proved/refuted; covers {len(st)}/{len(V)}" + r"} & -- \\")
(OUT / "summary.tex").write_text("\n".join(lines) + "\n")

# ---- cost tables -------------------------------------------------------------
sh = C["second_highest"]
(OUT / "r1_coords.tex").write_text(
    r"\addplot coordinates {" + " ".join(f"({d['n']},{d['orig_ms']:.3f})" for d in sh) + "};\n"
    r"\addplot coordinates {" + " ".join(f"({d['n']},{d['rew_ms']:.4f})" for d in sh) + "};\n")
def big(x, fmt):
    return format(x, fmt).replace(",", "{,}")


cl = []
for d in [sh[0], sh[2], sh[4]]:
    cl.append(f"R1 & {big(d['n'], ',')} & {big(d['orig_ms'], ',.1f')} & {d['rew_ms']:.2f} & {big(d['orig_ms'] / d['rew_ms'], ',.0f')}$\\times$ faster & sound, accept \\\\")
for d in C["decorr_selective"]:
    cl.append(f"R2 & {big(d['n'], ',')} & {d['orig_ms']:.3f} & {d['rew_ms']:.1f} & {d['rew_ms'] / d['orig_ms']:.0f}$\\times$ slower & sound, reject \\\\")
for d in C["count_bug"]:
    cl.append(f"R3 & {big(d['n'], ',')} & {d['orig_ms']:.1f} & {d['rew_ms']:.1f} & {d['orig_rows']} vs.\\ {d['rew_rows']} rows & unsound, reject \\\\")
(OUT / "cost.tex").write_text("\n".join(cl) + "\n")

# numbers quoted in text
macros = {
    "NCases": len(V), "NUnsound": len(uns), "NSound": len(snd),
    "InstDet": summ("inst")[0], "InstMissed": sum(r["inst"] == 0 for r in uns),
    "RandDet": summ("rand")[0], "BoundDet": summ("bound")[0],
    "NoICFA": sum(r["noic"] > 0 for r in snd),
    "RandCex": summ("rand", "rand_cex_rows")[3], "BoundCex": summ("bound", "bound_cex_rows")[3],
    "NoConstMissed": sum(r["bound_noconst"] == 0 for r in uns),
    "BoundMs": f"{statistics.mean(r['ms_bound'] for r in snd):.0f}",
    "RMaxSpeed": f"{sh[-1]['orig_ms'] / sh[-1]['rew_ms']:,.0f}".replace(",", "{,}"),
    "RTwoSlow": f"{C['decorr_selective'][-1]['rew_ms'] / C['decorr_selective'][-1]['orig_ms']:.0f}",
}
(OUT / "macros.tex").write_text("".join(f"\\newcommand{{\\{k}}}{{{v}}}\n" for k, v in macros.items()))
print((OUT / "summary.tex").read_text())
print((OUT / "macros.tex").read_text())
