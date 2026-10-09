"""CLI demo: base day, then a +15% heat-wave demand jump, then a cloudy day. Saves results.png and results.json."""
import json
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from pipeline import optimise
from grid import H

COL = {"Coal-1": "#4b4b4b", "Coal-2": "#8a8a8a", "Gas": "#e07b39", "Hydro": "#2f7fc1", "Renewables": "#3aa655"}


def summarise(tag, r):
    q, m, n, g = r["quantum"], r["milp"], r["naive"], r["greedy"]
    print(f"\n=== {tag} ===")
    print(f"{'method':<22}{'cost (Rs lakh)':>16}{'CO2 (t)':>10}{'curtail MWh':>13}{'worst-case unserved MWh':>25}")
    for name, key, e in [("QAOA hybrid (ours)", "quantum", q), ("Exact MILP", "milp", m), ("Greedy merit-order", "greedy", g), ("All plants ON", "naive", n)]:
        print(f"{name:<22}{e['cost']/1e5:>16.1f}{e['emissions_t']:>10.0f}{e['curtail_mwh']:>13.0f}{r['worst_unserved'][key]:>25.0f}")
    print(f"gap to exact optimum: {r['gap_pct']:.2f}%  | saving vs greedy: {r['saving_vs_greedy_pct']:.1f}%  | vs all-ON: {r['saving_vs_naive_pct']:.1f}%")
    qa = r["qaoa"]
    print(f"QAOA: sampled the QUBO optimum? rank={qa['lowest_energy_rank']} | prob. mass in best 1% of states={qa['prob_in_top1pct']:.0%} (random=1%) | time {r['t_qaoa']:.1f}s vs MILP {r['t_milp']:.2f}s")
    print("schedule (rows Coal-1, Coal-2, Gas, Hydro):"); print(q["u"])
    print("cost under uncertainty (Rs lakh): " + ", ".join(f"{k}={v['cost']/1e5:.0f} (unserved {v['unserved_mwh']:.0f} MWh)" for k, v in r["robust"].items()))


def plot(r, path):
    pb, q = r["problem"], r["quantum"]
    fig, ax = plt.subplots(2, 2, figsize=(13, 8))
    T = pb.T; x = np.arange(T)
    bottom = np.zeros(T)
    for i, g in enumerate(pb.gens):
        ax[0, 0].bar(x, q["p"][i], bottom=bottom, color=COL[g["name"]], label=g["name"]); bottom += q["p"][i]
    ax[0, 0].bar(x, q["r_used"], bottom=bottom, color=COL["Renewables"], label="Solar+Wind")
    ax[0, 0].plot(x, pb.D, "r-o", lw=2, label="Demand")
    ax[0, 0].set_xticks(x, pb.block_names); ax[0, 0].set_ylabel("MW"); ax[0, 0].set_title("Dispatch (QAOA schedule + LP)"); ax[0, 0].legend(fontsize=8, ncol=3)
    ax[0, 1].imshow(q["u"], cmap="Greens", aspect="auto", vmin=0, vmax=1.4)
    ax[0, 1].set_yticks(range(pb.G), [g["name"] for g in pb.gens]); ax[0, 1].set_xticks(x, pb.block_names); ax[0, 1].set_title("ON/OFF schedule chosen by QAOA")
    for i in range(pb.G):
        for t in range(T):
            ax[0, 1].text(t, i, "ON" if q["u"][i, t] else "off", ha="center", va="center", fontsize=9)
    names = ["QAOA hybrid", "Exact MILP", "Greedy", "All ON"]
    costs = [r[k]["cost"] / 1e5 for k in ["quantum", "milp", "greedy", "naive"]]
    ax[1, 0].bar(names, costs, color=["#6c3fc4", "#999", "#e0a030", "#cc5555"])
    for i, c in enumerate(costs):
        ax[1, 0].text(i, c, f"{c:.0f}", ha="center", va="bottom")
    ax[1, 0].set_ylabel("Daily cost (Rs lakh)"); ax[1, 0].set_title("Cost comparison"); ax[1, 0].set_ylim(min(costs) * 0.9, max(costs) * 1.05)
    ax[1, 1].plot(r["qaoa"]["counts"] and r["solver"].history, color="#6c3fc4")
    ax[1, 1].axhline(0, color="g", ls="--", lw=1); ax[1, 1].set_title("QAOA training (0 = optimum energy, 1 = random guess)")
    ax[1, 1].set_xlabel("COBYLA iteration"); ax[1, 1].set_ylabel("normalised <E>")
    fig.tight_layout(); fig.savefig(path, dpi=140); plt.close(fig)


if __name__ == "__main__":
    out = {}
    base = optimise()
    summarise("BASE DAY", base); plot(base, "results.png")
    heat = optimise(demand_scale=1.15, warm=base["qaoa"]["theta"])
    summarise("HEAT-WAVE: demand +15% (warm-started re-optimisation)", heat)
    cloud = optimise(solar_scale=0.5, warm=base["qaoa"]["theta"])
    summarise("CLOUDY DAY: solar -50% (warm-started re-optimisation)", cloud)
    for tag, r in [("base", base), ("heat", heat), ("cloudy", cloud)]:
        out[tag] = dict(schedule=r["quantum"]["u"].tolist(), cost_lakh=r["quantum"]["cost"] / 1e5,
                        milp_lakh=r["milp"]["cost"] / 1e5, gap_pct=r["gap_pct"],
                        emissions_t=r["quantum"]["emissions_t"], curtail_mwh=r["quantum"]["curtail_mwh"],
                        unserved_mwh=r["quantum"]["unserved_mwh"])
    json.dump(out, open("results.json", "w"), indent=2)
    print("\nsaved results.png, results.json")
