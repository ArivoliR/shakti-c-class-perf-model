"""Score the model on predicted-vs-measured *change*, and derive its error bar.

Absolute cycle accuracy on one core is not what the model is used for. It is
used to answer "how much would this change buy?", so it has to be scored on the
change. For each held-out design point:

    measured  = (rtl_cycles[point]   - rtl_cycles[baseline])   / rtl_cycles[baseline]
    predicted = (model_cycles[point] - model_cycles[baseline]) / model_cycles[baseline]

Both are percentages of baseline runtime, which is the unit every experiment in
the dual-issue report is quoted in, so the residuals are directly comparable to
those claims.

Three numbers come out, and they answer different questions:

  bias      mean(predicted - measured). A systematic lean; correctable.
  scatter   RMS of (predicted - measured) after removing bias. Not correctable,
            and this is what sets the resolution floor.
  slope     regression of predicted on measured. Slope < 1 means the model
            systematically understates the size of changes, which is exactly
            the failure mode that matters when pricing an optimisation.

The reported resolution floor is 2x scatter: any predicted effect smaller than
that is indistinguishable from zero and must be reported as "below model
resolution", never as a number.
"""

from __future__ import annotations

from pathlib import Path
import argparse
import json
import math
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from design_points import BY_NAME, UNBUILDABLE, UNMODELLABLE  # noqa: E402

HERE = Path(__file__).resolve().parent
RUNS = HERE / "runs"


def linear_fit(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """Least-squares y = slope*x + intercept; returns (slope, intercept, r2)."""
    n = len(xs)
    if n < 2:
        return float("nan"), float("nan"), float("nan")
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    if sxx == 0:
        return float("nan"), float("nan"), float("nan")
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    r2 = 1.0 - ss_res / ss_tot if ss_tot else float("nan")
    return slope, intercept, r2


def score(
    predictions: dict,
    small_threshold: float = 3.0,
    blind_spot_predicted_pp: float = 0.1,
    blind_spot_measured_pp: float = 1.0,
) -> dict:
    points = {p["name"]: p for p in predictions["points"] if p.get("status") == "ok"}
    baseline = points.get("baseline")
    if baseline is None:
        raise SystemExit("no baseline design point in predictions; cannot score deltas")

    rtl_base = baseline["rtl_cycles"]
    model_base = baseline["model_cycles"]

    rows = []
    for name, point in points.items():
        if name == "baseline":
            continue
        measured = (point["rtl_cycles"] - rtl_base) / rtl_base * 100.0
        predicted = (point["model_cycles"] - model_base) / model_base * 100.0
        error = predicted - measured
        rows.append(
            {
                "name": name,
                "edits": BY_NAME[name].edits if name in BY_NAME else {},
                "mechanism": BY_NAME[name].mechanism if name in BY_NAME else "",
                "tags": list(BY_NAME[name].tags) if name in BY_NAME else [],
                "rtl_cycles": point["rtl_cycles"],
                "model_cycles": point["model_cycles"],
                "measured_pct": measured,
                "predicted_pct": predicted,
                "error_pp": error,
                "relative_error": (error / abs(measured)) if abs(measured) >= 0.01 else None,
                "delta_t_accuracy": point.get("delta_t_accuracy"),
            }
        )
    # Partition before doing any statistics. A point where the model predicts
    # essentially nothing while the RTL moves substantially is not a large
    # calibration error -- it is a missing mechanism, a categorically different
    # failure. Pooling the two produces a meaningless regression (the blind
    # spots alone drive slope negative) and, worse, inflates the error bar for
    # mechanisms the model actually does represent.
    for row in rows:
        row["blind_spot"] = (
            abs(row["predicted_pct"]) < blind_spot_predicted_pp
            and abs(row["measured_pct"]) > blind_spot_measured_pp
        )
    blind = [r for r in rows if r["blind_spot"]]
    rows = [r for r in rows if not r["blind_spot"]]
    rows.sort(key=lambda r: -abs(r["measured_pct"]))
    blind.sort(key=lambda r: -abs(r["measured_pct"]))

    errors = [r["error_pp"] for r in rows]
    n = len(errors)
    bias = sum(errors) / n if n else float("nan")
    scatter = math.sqrt(sum((e - bias) ** 2 for e in errors) / n) if n else float("nan")
    rms = math.sqrt(sum(e * e for e in errors) / n) if n else float("nan")
    slope, intercept, r2 = linear_fit(
        [r["measured_pct"] for r in rows], [r["predicted_pct"] for r in rows]
    )

    # A single scatter over all points is dominated by the large-effect points,
    # which is misleading: an error bar derived from a +20% change does not
    # transfer to a +1% claim. Report the small-effect band separately, because
    # that is the regime every dual-issue experiment actually lives in.
    small = [r for r in rows if abs(r["measured_pct"]) <= small_threshold]
    small_errors = [r["error_pp"] for r in small]
    small_n = len(small_errors)
    small_bias = sum(small_errors) / small_n if small_n else float("nan")
    small_scatter = (
        math.sqrt(sum((e - small_bias) ** 2 for e in small_errors) / small_n)
        if small_n
        else float("nan")
    )

    accuracies = [r["delta_t_accuracy"] for r in rows if r["delta_t_accuracy"] is not None]
    base_acc = baseline.get("delta_t_accuracy")

    return {
        "benchmark": predictions.get("benchmark"),
        "baseline": {
            "rtl_cycles": rtl_base,
            "model_cycles": model_base,
            "absolute_error_pct": (model_base - rtl_base) / rtl_base * 100.0,
            "delta_t_accuracy": base_acc,
        },
        "rows": rows,
        "blind_spots": blind,
        "summary": {
            "points": n,
            "blind_spot_points": len(blind),
            "bias_pp": bias,
            "scatter_pp": scatter,
            "rms_error_pp": rms,
            "max_abs_error_pp": max((abs(e) for e in errors), default=float("nan")),
            "slope": slope,
            "intercept": intercept,
            "r2": r2,
            "resolution_floor_pp": 2 * scatter if n else float("nan"),
            "small_effect_threshold_pct": small_threshold,
            "small_effect_points": small_n,
            "small_effect_bias_pp": small_bias,
            "small_effect_scatter_pp": small_scatter,
            "small_effect_resolution_floor_pp": 2 * small_scatter if small_n else float("nan"),
            "min_delta_t_accuracy": min(accuracies) if accuracies else None,
            "mean_delta_t_accuracy": sum(accuracies) / len(accuracies) if accuracies else None,
        },
    }


def _fmt(value, spec="+7.3f", none="   n/a"):
    return none if value is None or (isinstance(value, float) and math.isnan(value)) else format(value, spec)


def render_markdown(result: dict) -> str:
    summary = result["summary"]
    base = result["baseline"]
    blind = result.get("blind_spots", [])
    lines = [
        f"# Held-out design-point validation ({result['benchmark']})",
        "",
        f"{summary['points'] + len(blind)} design points, each a real RTL build differing",
        "from the baseline by one BSC_DEFINES knob. The model was run with parameters",
        "derived from each variant's makefile.inc and no other change: no calibrated",
        "constant was re-fitted per point.",
        "",
    ]

    if blind:
        lines += [
            "## Blind spots: mechanisms the model does not have",
            "",
            "These are not large errors. The model predicts essentially nothing while",
            "the hardware moves substantially, which means the mechanism is absent",
            "rather than mis-tuned. They are reported separately because averaging them",
            "into an error bar would both destroy that error bar and disguise a",
            "categorical failure as a quantitative one.",
            "",
            "| point | mechanism | measured Δ | predicted Δ | Δt acc |",
            "|---|---|---:|---:|---:|",
        ]
        for row in blind:
            acc = row["delta_t_accuracy"]
            acc_text = f"{acc:.3%}" if acc is not None else "n/a"
            lines.append(
                f"| `{row['name']}` | {row['mechanism']} | {row['measured_pct']:+.3f}% "
                f"| {row['predicted_pct']:+.3f}% | {acc_text} |"
            )
        worst = min(
            (r for r in blind if r["delta_t_accuracy"] is not None),
            key=lambda r: r["delta_t_accuracy"],
            default=None,
        )
        lines += [""]
        if worst is not None:
            lines += [
                f"Note the Δt accuracy column. On `{worst['name']}` it falls to "
                f"{worst['delta_t_accuracy']:.2%}, from "
                f"{result['baseline']['delta_t_accuracy']:.2%} on the baseline. The "
                "cycle-accuracy figure is not a property of the model; it is a property",
                "of the model *on the configuration it was calibrated against*, and it",
                "does not survive contact with a core the model has no mechanism for.",
                "",
            ]

    lines += [
        "## Per-point predictions, mechanisms the model does represent",
        "",
        "| point | mechanism | measured Δ | predicted Δ | error (pp) | rel. error | Δt acc |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for row in result["rows"]:
        rel = row["relative_error"]
        rel_text = f"{rel*100:+.0f}%" if rel is not None else "n/a"
        acc = row["delta_t_accuracy"]
        acc_text = f"{acc:.3%}" if acc is not None else "n/a"
        lines.append(
            f"| `{row['name']}` | {row['mechanism']} | {row['measured_pct']:+.3f}% "
            f"| {row['predicted_pct']:+.3f}% | {row['error_pp']:+.3f} | {rel_text} | {acc_text} |"
        )

    floor = summary["resolution_floor_pp"]
    small_floor = summary["small_effect_resolution_floor_pp"]
    threshold = summary["small_effect_threshold_pct"]
    lines += [
        "",
        "## Error bar",
        "",
        f"- baseline absolute error: {base['absolute_error_pct']:+.3f}% "
        f"({base['model_cycles']:,} model vs {base['rtl_cycles']:,} RTL)",
        f"- bias: {_fmt(summary['bias_pp'], '+.3f')} pp (systematic lean, correctable)",
        f"- scatter: {_fmt(summary['scatter_pp'], '.3f')} pp (irreducible)",
        f"- RMS error: {_fmt(summary['rms_error_pp'], '.3f')} pp",
        f"- worst point: {_fmt(summary['max_abs_error_pp'], '.3f')} pp",
        f"- regression of predicted on measured: slope {_fmt(summary['slope'], '.4f')}, "
        f"intercept {_fmt(summary['intercept'], '+.4f')} pp, R² {_fmt(summary['r2'], '.4f')}",
        "",
        f"Restricted to the {summary['small_effect_points']} points with |measured| "
        f"<= {threshold:.0f}%, which is the regime the dual-issue experiments live in:",
        "",
        f"- bias: {_fmt(summary['small_effect_bias_pp'], '+.3f')} pp",
        f"- scatter: {_fmt(summary['small_effect_scatter_pp'], '.3f')} pp",
        "",
        f"**Resolution floor: {_fmt(small_floor, '.2f')} pp for small effects, "
        f"{_fmt(floor, '.2f')} pp overall.**",
        "A predicted effect smaller than the floor is not distinguishable from zero",
        "and must be reported as *below model resolution*, not as a number. Use the",
        "small-effect floor when the claim is small: an error bar derived from a +20%",
        "design change does not transfer to a +1% claim.",
        "",
    ]

    if summary["small_effect_points"] < 5:
        lines += [
            f"⚠ The small-effect floor rests on only {summary['small_effect_points']} "
            "points. Treat it as indicative, not established; add design points in that",
            "band before quoting it as a precision claim.",
            "",
        ]

    slope, r2 = summary["slope"], summary["r2"]
    if math.isnan(slope) or math.isnan(r2) or r2 < 0.5 or slope <= 0:
        lines += [
            f"The regression is not usable (slope {_fmt(slope, '.3f')}, R² "
            f"{_fmt(r2, '.3f')}): with this few points, spanning this range, it does not",
            "characterise a systematic lean. Rely on the per-point errors instead.",
            "",
        ]
    elif slope < 0.9:
        lines += [
            f"Slope {slope:.3f} < 1: the model **systematically understates** the size of",
            "changes. A predicted gain should be read as a lower bound, and scaled",
            f"estimates divided by {slope:.3f} before being compared against a target.",
            "",
        ]
    elif slope > 1.1:
        lines += [
            f"Slope {slope:.3f} > 1: the model **systematically overstates** the size of",
            "changes. Predicted gains are optimistic.",
            "",
        ]

    if UNBUILDABLE:
        lines += [
            "## Mechanisms with no ground truth",
            "",
            "The model predicts an effect for these knobs, but no core can be built",
            "with them, so the prediction can never be checked. This is the most",
            "dangerous category: the model answers confidently and nothing contradicts it.",
            "",
        ]
        for label, reason in UNBUILDABLE.items():
            lines += [f"- **{label}** — {reason}", ""]

    if UNMODELLABLE:
        lines += ["## Excluded, and why", ""]
        for label, reason in UNMODELLABLE.items():
            lines += [f"- **{label}** — {reason}", ""]

    lines += [
        "## How to read this",
        "",
        "A dual-issue experiment predicting +X% is credible only if |X| is comfortably",
        f"above {_fmt(floor, '.2f')} pp and the mechanism it perturbs is represented among",
        "the points above. Experiments touching a mechanism with no buildable design point here",
        "(notably bypass/forwarding changes and WAW stalls) carry no validated error bar at all.",
        "",
    ]
    return "\n".join(lines)


def render_latex(result: dict) -> str:
    lines = [
        "% generated by holdout/score.py -- \\input{} this into the report",
        "\\begin{tabular}{llrrrr}",
        "\\toprule",
        "Point & Mechanism & Measured & Predicted & Error (pp) & $\\Delta t$ acc. \\\\",
        "\\midrule",
    ]
    for row in result["rows"]:
        acc = row["delta_t_accuracy"]
        acc_text = f"{acc*100:.2f}\\%" if acc is not None else "--"
        mech = row["mechanism"].replace("->", "$\\rightarrow$").replace("_", "\\_")
        lines.append(
            f"\\texttt{{{row['name'].replace('_', chr(92)+'_')}}} & {mech} & "
            f"{row['measured_pct']:+.2f}\\% & {row['predicted_pct']:+.2f}\\% & "
            f"{row['error_pp']:+.2f} & {acc_text} \\\\"
        )
    summary = result["summary"]
    lines += [
        "\\midrule",
        f"\\multicolumn{{6}}{{l}}{{Bias {summary['bias_pp']:+.3f}\\,pp, "
        f"scatter {summary['scatter_pp']:.3f}\\,pp, slope {summary['slope']:.3f}, "
        f"$R^2$ {summary['r2']:.3f}, resolution floor "
        f"{summary['resolution_floor_pp']:.2f}\\,pp}} \\\\",
        "\\bottomrule",
        "\\end{tabular}",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", default="dhrystone", choices=("dhrystone", "coremark"))
    parser.add_argument("--predictions", default=None)
    parser.add_argument("--outdir", default=None)
    args = parser.parse_args()

    path = Path(args.predictions or RUNS / f"predictions-{args.benchmark}.json")
    if not path.exists():
        print(f"missing {path}; run predict.py first")
        return 1
    result = score(json.loads(path.read_text()))

    outdir = Path(args.outdir or RUNS)
    outdir.mkdir(parents=True, exist_ok=True)
    markdown = render_markdown(result)
    (outdir / f"validation-{args.benchmark}.md").write_text(markdown, encoding="utf-8")
    (outdir / f"validation-{args.benchmark}.tex").write_text(render_latex(result), encoding="utf-8")
    (outdir / f"validation-{args.benchmark}.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    print(markdown)
    print(f"wrote validation-{args.benchmark}.{{md,tex,json}} to {outdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
