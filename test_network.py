import numpy as np
from network import demand_simulator, NetworkLP, NetworkMILP, save_demand_template
from grid import GENS

net = demand_simulator()
print(f"Demand simulator OK")
print(f"  Total demand per block: {net['total_demand'].round(1)}")
print(f"  Renewable per block (total): {net['renewable_mw'].sum(axis=0).round(1)}")

lp = NetworkLP()
u_on = np.ones((4, 4), dtype=int)
r = lp.solve(u_on, net)
print(f"\nNetworkLP (all-ON) OK")
print(f"  Cost      : {r['cost']/1e5:.1f} Rs lakh")
print(f"  Unserved  : {r['unserved_mwh']:.0f} MWh")
print(f"  Curtailed : {r['curtail_mwh']:.0f} MWh")
print(f"  Emissions : {r['emissions_t']:.0f} t CO2")

milp = NetworkMILP()
rm = milp.solve(net)
print(f"\nNetworkMILP OK")
print(f"  Cost      : {rm['cost']/1e5:.1f} Rs lakh")
print(f"  Unserved  : {rm['unserved_mwh']:.0f} MWh")
print(f"  Schedule  :")
gen_names = [g["name"] for g in GENS]
from grid import BLOCKS
for i, name in enumerate(gen_names):
    row = "  ".join("ON " if rm["u"][i, t] else "off" for t in range(4))
    print(f"    {name:<10} {row}")

csv_path = save_demand_template()
print(f"\nCSV template saved -> {csv_path}")
print("\nAll network tests PASSED")
