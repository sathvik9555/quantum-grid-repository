"""
compare.py -- CLI benchmark: all methods side by side.

Usage:
    py compare.py                    # base day
    py compare.py --heat             # demand +15%
    py compare.py --cloudy           # solar -50%
    py compare.py --csv data/demand_template.csv   # real SLDC data

Outputs:
    - printed comparison table to terminal
    - network.png  (5-node network diagram with line loading)
    - data/demand_template.csv  (CSV template for real SLDC data)
"""
from __future__ import annotations

import sys
import io
import argparse
import os
import json

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

# Force UTF-8 output on Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from engine import run_all
from network import (
    NODES, LINES, LINE_CAP, GEN_NODE, GENS,
    demand_simulator, save_demand_template,
)
from grid import BLOCKS


# ---------------------------------------------------------------------------
# Terminal table
# ---------------------------------------------------------------------------
def print_table(r: dict, tag: str):
    print("\n" + "=" * 78)
    print(f"  {tag}")
    print("=" * 78)
    header = f"  {'Method':<26} {'Cost (Rs lakh)':>15} {'CO2 (t)':>9} " \
             f"{'Curtail MWh':>12} {'Unserved MWh':>13} {'Time':>8}"
    print(header)
    print("  " + "-" * 74)

    rows = [
        ("QAOA + LP (ours)",    r["lp_qaoa"],    r["t_lp"],   "qaoa"),
        ("QAOA + QP",           r["qp_qaoa"],    r["t_qp"],   "qaoa"),
        ("Network MILP",        r["milp_net"],   r["t_milp"], "milp"),
        ("MILP sched + LP",     r["lp_milp"],    r["t_lp"],   "milp"),
        ("Greedy + LP",         r["lp_greedy"],  r["t_lp"],   "greedy"),
        ("All-ON + LP",         r["lp_allon"],   r["t_lp"],   "allon"),
    ]
    for name, res, t, key in rows:
        wu = r["worst_unserved"].get(key, res.get("unserved_mwh", 0))
        print(f"  {name:<26} {res['cost']/1e5:>15.1f} {res['emissions_t']:>9.0f} "
              f"{res['curtail_mwh']:>12.0f} {wu:>13.0f} {t:>7.2f}s")

    print(f"\n  QAOA gap to Network MILP : {r['gap_pct']:+.2f}%")
    print(f"  Saving vs all-ON         : {r['saving_vs_allon_pct']:.1f}%")
    print(f"  Saving vs greedy         : {r['saving_vs_greedy_pct']:.1f}%")
    print(f"  QAOA train time          : {r['t_qaoa']:.1f}s")
    qa = r["qaoa"]
    print(f"  QUBO optimum sampled?    : rank={qa['lowest_energy_rank']}  "
          f"P(best 1%)={qa['prob_in_top1pct']:.0%}  (random=1%)")

    print("\n  ON/OFF schedule (rows=generators, cols=blocks):")
    gen_names = [g["name"] for g in GENS]
    print("  " + " " * 14 + "  ".join(f"{b:^7}" for b in BLOCKS))
    for i, name in enumerate(gen_names):
        row_str = "  ".join(" ON  " if r["u_qaoa"][i, t] else " off " for t in range(4))
        print(f"  {name:<14}{row_str}")

    print("\n  Supply vs demand per block (QAOA + LP):")
    net = r["net"]
    print(f"  {'Block':^7} {'Demand':>8} {'Thermal':>9} {'Renewable':>11} {'Total':>8} {'Status':>9}")
    for bl in r["lp_qaoa"]["blocks"]:
        t = bl["t"]
        dem = net["demand_mw"][:, t].sum()
        th  = bl["p_mw"].sum()
        ren = net["renewable_mw"][:, t].sum() - bl["curt_mw"].sum()
        status = "OK" if bl["uns_mw"].sum() < 1 else "UNSERVED"
        print(f"  {BLOCKS[t]:^7} {dem:>8.0f} {th:>9.0f} {ren:>11.0f} {th+ren:>8.0f} {status:>9}")

    print("\n  Line loading (QAOA + LP, max over blocks):")
    n_lines = len(LINES)
    max_loading = np.zeros(n_lines)
    for bl in r["lp_qaoa"]["blocks"]:
        if "line_loading_pct" in bl:
            max_loading = np.maximum(max_loading, bl["line_loading_pct"])
    for l, (frm, to, cap) in enumerate(LINES):
        bar = "#" * int(max_loading[l] / 5)
        print(f"  {frm}-{to:<10} {max_loading[l]:5.1f}%  [{bar:<20}]  cap={cap:.0f}MW")


# ---------------------------------------------------------------------------
# Network diagram (matplotlib)
# ---------------------------------------------------------------------------
NODE_POS = {
    "VSKP": (0.05, 0.65),
    "VZM":  (0.30, 0.85),
    "VJA":  (0.55, 0.60),
    "KNL":  (0.55, 0.25),
    "TPT":  (0.85, 0.40),
}

def plot_network(r: dict, path: str = "network.png"):
    fig, axes = plt.subplots(1, 2, figsize=(14, 6))
    fig.patch.set_facecolor("#0f1117")

    # ---- Left: network with line loading ----
    ax = axes[0]
    ax.set_facecolor("#0f1117")
    ax.set_xlim(-0.05, 1.05); ax.set_ylim(0.0, 1.05)
    ax.axis("off")
    ax.set_title("Network: line loading (max over blocks)", color="white", fontsize=11)

    # Compute max loading per line
    n_lines = len(LINES)
    max_loading = np.zeros(n_lines)
    for bl in r["lp_qaoa"]["blocks"]:
        if "line_loading_pct" in bl:
            max_loading = np.maximum(max_loading, bl["line_loading_pct"])

    # Draw lines
    for l, (frm, to, cap) in enumerate(LINES):
        x0, y0 = NODE_POS[frm]; x1, y1 = NODE_POS[to]
        load = max_loading[l]
        color = "#22c55e" if load < 60 else ("#f59e0b" if load < 85 else "#ef4444")
        lw = 1.5 + load / 30
        ax.plot([x0, x1], [y0, y1], color=color, lw=lw, zorder=1)
        mx, my = (x0+x1)/2, (y0+y1)/2
        ax.text(mx, my+0.02, f"{load:.0f}%", color=color, fontsize=7,
                ha="center", va="bottom", zorder=3)

    # Draw nodes
    gen_at = {}
    for i, g in enumerate(GENS):
        node = GEN_NODE[i]
        gen_at.setdefault(node, []).append(g["name"])

    for node, (nx, ny) in NODE_POS.items():
        circle = plt.Circle((nx, ny), 0.045, color="#6366f1", zorder=2)
        ax.add_patch(circle)
        ax.text(nx, ny, node, color="white", fontsize=8, ha="center", va="center",
                fontweight="bold", zorder=4)
        if node in gen_at:
            label = "\n".join(gen_at[node])
            ax.text(nx, ny - 0.08, label, color="#a5b4fc", fontsize=6.5,
                    ha="center", va="top", zorder=3)

    legend = [
        mpatches.Patch(color="#22c55e", label="< 60% loaded"),
        mpatches.Patch(color="#f59e0b", label="60-85% loaded"),
        mpatches.Patch(color="#ef4444", label="> 85% loaded"),
    ]
    ax.legend(handles=legend, loc="lower left", fontsize=7,
              facecolor="#1e2130", edgecolor="#444", labelcolor="white")

    # ---- Right: cost comparison bar ----
    ax2 = axes[1]
    ax2.set_facecolor("#0f1117")
    names  = ["QAOA+LP", "MILP", "Greedy+LP", "All-ON+LP"]
    costs  = [r["lp_qaoa"]["cost"]/1e5, r["milp_net"]["cost"]/1e5,
               r["lp_greedy"]["cost"]/1e5, r["lp_allon"]["cost"]/1e5]
    colors = ["#6366f1", "#6b7280", "#f59e0b", "#ef4444"]
    bars = ax2.bar(names, costs, color=colors, zorder=2)
    for bar, cost in zip(bars, costs):
        ax2.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 2,
                 f"{cost:.0f}", ha="center", va="bottom", color="white", fontsize=9)
    ax2.set_facecolor("#0f1117")
    ax2.set_ylabel("Daily cost (Rs lakh)", color="white")
    ax2.set_title("Cost comparison (with network flows)", color="white", fontsize=11)
    ax2.tick_params(colors="white"); ax2.yaxis.label.set_color("white")
    for spine in ax2.spines.values(): spine.set_edgecolor("#444")
    ax2.set_ylim(min(costs)*0.92, max(costs)*1.06)

    fig.tight_layout()
    fig.savefig(path, dpi=140, facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"\n  Saved network diagram -> {path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="AP Grid Optimiser -- compare all methods")
    ap.add_argument("--heat",   action="store_true", help="Heat-wave: demand +15%%")
    ap.add_argument("--cloudy", action="store_true", help="Cloudy day: solar -50%%")
    ap.add_argument("--calm",   action="store_true", help="Calm day: wind -60%%")
    ap.add_argument("--csv",    default=None, help="Path to demand CSV file")
    ap.add_argument("--shots",  type=int, default=4096)
    a = ap.parse_args()

    d_scale = 1.15 if a.heat   else 1.0
    s_scale = 0.50 if a.cloudy else 1.0
    w_scale = 0.40 if a.calm   else 1.0

    if a.csv:
        tag = f"REAL APSLDC DATA: {os.path.basename(a.csv)}"
    elif a.heat:   tag = "HEAT-WAVE: demand +15%"
    elif a.cloudy: tag = "CLOUDY DAY: solar -50%"
    elif a.calm:   tag = "CALM DAY: wind -60%"
    else:          tag = "BASE DAY"

    print(f"\nRunning scenario: {tag}")
    print("This may take 30-120 seconds (QAOA training on Aer) ...")

    r = run_all(
        demand_scale=d_scale,
        solar_scale=s_scale,
        wind_scale=w_scale,
        shots=a.shots,
        csv_path=a.csv,
    )

    print_table(r, tag)
    plot_network(r, "network.png")

    # Save demand template CSV
    save_demand_template("data/demand_template.csv")
    print("  Saved demand template -> data/demand_template.csv")
    print("  (Fill in real SLDC data and run: py compare.py --csv data/demand_template.csv)")

    # Save summary JSON
    out = {
        "scenario": tag,
        "gap_pct": round(r["gap_pct"], 2),
        "saving_vs_allon_pct": round(r["saving_vs_allon_pct"], 1),
        "lp_qaoa_cost_lakh": round(r["lp_qaoa"]["cost"]/1e5, 1),
        "milp_cost_lakh": round(r["milp_net"]["cost"]/1e5, 1),
        "lp_qaoa_unserved_mwh": round(r["lp_qaoa"]["unserved_mwh"], 1),
        "t_qaoa_s": round(r["t_qaoa"], 1),
        "t_milp_s": round(r["t_milp"], 3),
        "u_qaoa": r["u_qaoa"].tolist(),
    }
    with open("compare_results.json", "w") as f:
        json.dump(out, f, indent=2)
    print("  Saved summary -> compare_results.json")


if __name__ == "__main__":
    main()
