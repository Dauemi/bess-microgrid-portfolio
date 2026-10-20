"""
Saul hybrid microgrid (French Guiana) - independent check of HOMER Pro.

1. Rebuilds HOMER's Load Following dispatch hour by hour from the exported
   time series (load + PV output) and compares the result with HOMER.
2. Rebuilds the 25-year cash flow, including the battery replacement that
   HOMER's cash-flow chart does not show, and recomputes NPC and LCOE.
3. Saves charts and summary tables to ./output

Run:  python saul_analysis.py
"""
import math
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

HERE = Path(__file__).parent
HOMER_HOURLY = HERE / "data" / "homer_hourly_optimal.csv"
HOMER_OPTIM = HERE / "data" / "homer_optimisation_results.csv"
OUT = HERE / "output"

# --- Optimal system chosen in HOMER ---
PV_KW = 326.049
PV_DERATE = 0.80
BATT_KWH = 890.0
SOC_MIN = 0.20
SOC_INIT = 1.00
RTE = 0.90                 # battery round-trip efficiency
CONV_KW = 113.90
ETA_CONV = 0.95            # inverter and rectifier
GEN_KW = 160.0
GEN_MIN_LOAD = 0.25        # fraction of rated power
FUEL_INTERCEPT = 4.67      # L/h whenever the genset runs
FUEL_SLOPE = 0.236         # L per kWh produced
RESERVE_LOAD = 0.10        # spare capacity: 10% of the load
RESERVE_PV = 0.80          # spare capacity: 80% of PV output (passing clouds)

# --- Economics (same inputs as HOMER) ---
DISCOUNT = 0.08
INFLATION = 0.02
YEARS = 25
FUEL_PRICE = 1.60          # $/L
BATT_LIFE_YEARS = 15
BATT_THROUGHPUT = 3000     # kWh per kWh of capacity
GEN_LIFE_HOURS = 15000

# --- Chart style (validated categorical palette, fixed order) ---
PV_C, BATT_C, GEN_C, CONV_C = "#2a78d6", "#eb6834", "#1baf7a", "#eda100"
INK, INK_2, MUTED, GRID, AXIS, SURFACE = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7", "#fcfcfb"
HIGHLIGHT_OFF = "#c3c2b7"

plt.rcParams.update({
    "font.family": ["Segoe UI", "DejaVu Sans"],
    "font.size": 10,
    "axes.edgecolor": AXIS,
    "axes.labelcolor": INK_2,
    "axes.titlecolor": INK,
    "axes.titlesize": 12,
    "axes.titleweight": "bold",
    "axes.titlelocation": "left",
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": True,
    "axes.axisbelow": True,
    "grid.color": GRID,
    "grid.linewidth": 0.6,
    "xtick.color": MUTED,
    "ytick.color": MUTED,
    "legend.frameon": False,
    "legend.labelcolor": INK_2,
    "figure.facecolor": SURFACE,
    "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE,
    "savefig.dpi": 160,
})


def load_homer(path):
    df = pd.read_csv(path, skiprows=[0, 2], encoding="latin-1")
    df.columns = [c.replace("Generic flat plate PV ", "PV ")
                   .replace("Generic 1kWh Li-Ion ", "Batt ")
                   .replace("Autosize Genset ", "Gen ") for c in df.columns]
    df["Time"] = pd.to_datetime(df["Time"], format="%m/%d/%Y %I:%M:%S %p")
    return df


def simulate(load, pv):
    """HOMER-style Load Following dispatch, one-hour time steps."""
    eta_b = math.sqrt(RTE)          # same loss on charge and on discharge
    e_min, e_max = SOC_MIN * BATT_KWH, BATT_KWH
    gen_min = GEN_MIN_LOAD * GEN_KW
    e = SOC_INIT * BATT_KWH
    rows = []
    for L, P in zip(load, pv):
        dis_max = max(0.0, e - e_min) * eta_b       # DC kW the battery can deliver this hour
        chg_max = max(0.0, e_max - e) / eta_b       # DC kW it can absorb this hour

        # Can PV + battery carry the load plus reserves without the genset?
        firm_dc = (1 - RESERVE_PV) * P + dis_max
        gen_needed = min(CONV_KW, firm_dc * ETA_CONV) < (1 + RESERVE_LOAD) * L

        gen = 0.0
        if gen_needed:
            # Load following: cover only what PV + battery cannot,
            # but never below the genset's minimum load
            dc_to_ac_max = min(CONV_KW, (P + dis_max) * ETA_CONV)
            gen = min(GEN_KW, max(gen_min, L - dc_to_ac_max))

        gen_to_load = min(gen, L)
        rest = L - gen_to_load
        pv_to_load = min(P * ETA_CONV, rest, CONV_KW)                      # AC kW
        rest -= pv_to_load
        batt_out = min(rest / ETA_CONV, dis_max, (CONV_KW - pv_to_load) / ETA_CONV)  # DC kW
        rest -= batt_out * ETA_CONV
        unmet = max(0.0, rest)

        # Surplus PV, then surplus genset output (via rectifier), charges the battery
        pv_surplus = P - pv_to_load / ETA_CONV
        chg_pv = min(pv_surplus, chg_max)
        gen_surplus = gen - gen_to_load
        chg_gen = min(gen_surplus * ETA_CONV, chg_max - chg_pv, CONV_KW * ETA_CONV)
        excess = (pv_surplus - chg_pv) + (gen_surplus - chg_gen / ETA_CONV)

        e += (chg_pv + chg_gen) * eta_b - batt_out / eta_b
        fuel = FUEL_INTERCEPT + FUEL_SLOPE * gen if gen > 0 else 0.0
        rows.append((gen, gen_to_load, pv_to_load, batt_out * ETA_CONV, chg_pv + chg_gen,
                     batt_out, 100 * e / BATT_KWH, excess, unmet, fuel))

    return pd.DataFrame(rows, columns=[
        "gen_kw", "gen_to_load_kw", "pv_to_load_kw", "batt_to_load_kw", "batt_charge_kw",
        "batt_discharge_kw", "soc_pct", "excess_kw", "unmet_kw", "fuel_l"])


def annual_summary(pv, load, gen, fuel, batt_discharge, excess, unmet):
    served = load.sum() - unmet.sum()
    return {
        "PV production (kWh)": pv.sum(),
        "Load served (kWh)": served,
        "Diesel production (kWh)": gen.sum(),
        "Diesel hours": int((gen > 0).sum()),
        "Fuel (L)": fuel.sum(),
        "Battery throughput (kWh)": batt_discharge.sum() / math.sqrt(RTE),
        "Excess electricity (kWh)": excess.sum(),
        "Unmet load (kWh)": unmet.sum(),
        "Renewable fraction (%)": 100 * (1 - gen.sum() / served),
    }


def real_rate():
    return (DISCOUNT - INFLATION) / (1 + INFLATION)


def components(annual, replace_battery=True):
    hours = annual["Diesel hours"]
    batt_life = min(BATT_LIFE_YEARS, BATT_THROUGHPUT * BATT_KWH / annual["Battery throughput (kWh)"])
    return [
        dict(name="PV", capital=1800 * PV_KW, replacement=1500 * PV_KW,
             om=25 * PV_KW, fuel=0.0, life=25.0, replace=True),
        dict(name="Battery", capital=550 * BATT_KWH, replacement=400 * BATT_KWH,
             om=10 * BATT_KWH, fuel=0.0, life=batt_life, replace=replace_battery),
        dict(name="Converter", capital=400 * CONV_KW, replacement=350 * CONV_KW,
             om=10 * CONV_KW, fuel=0.0, life=15.0, replace=True),
        dict(name="Genset", capital=600 * GEN_KW, replacement=500 * GEN_KW,
             om=0.05 * GEN_KW * hours, fuel=FUEL_PRICE * annual["Fuel (L)"],
             life=GEN_LIFE_HOURS / hours, replace=True),
    ]


def cash_flow(comps):
    """Undiscounted yearly cash flow per component (negative = cost), years 0..25."""
    cf = pd.DataFrame(0.0, index=range(YEARS + 1), columns=[c["name"] for c in comps])
    for c in comps:
        n = c["name"]
        cf.loc[0, n] -= c["capital"]
        cf.loc[1:, n] -= c["om"] + c["fuel"]
        last_install = 0.0
        if c["replace"]:
            k = 1
            while k * c["life"] < YEARS:
                cf.loc[math.ceil(k * c["life"]), n] -= c["replacement"]
                last_install = k * c["life"]
                k += 1
        remaining = max(0.0, c["life"] - (YEARS - last_install))
        cf.loc[YEARS, n] += c["replacement"] * remaining / c["life"]   # salvage value
    return cf


def npc_and_lcoe(cf, served_kwh):
    i = real_rate()
    discount = np.array([(1 + i) ** -y for y in cf.index])
    npc = -(cf.sum(axis=1).values * discount).sum()
    crf = i * (1 + i) ** YEARS / ((1 + i) ** YEARS - 1)
    return npc, npc * crf / served_kwh


def best_per_architecture(path):
    opt = pd.read_csv(path, skiprows=[0])
    has = lambda col: pd.to_numeric(opt[col], errors="coerce").fillna(0) > 0
    pv, gen, batt = has("Architecture/PV (kW)"), has("Architecture/Gen (kW)"), has("Architecture/1kWh LI (#)")
    names = {(True, True, True): "PV + battery + diesel", (True, True, False): "PV + battery",
             (False, True, True): "Battery + diesel", (True, False, True): "PV + diesel",
             (False, False, True): "Diesel only"}
    opt["System"] = [names.get(k, "Other") for k in zip(pv, batt, gen)]
    best = opt.loc[opt.groupby("System")["Cost/NPC ($)"].idxmin()]
    return best.sort_values("Cost/NPC ($)")[["System", "Cost/NPC ($)", "Cost/LCOE ($/kWh)",
                                              "System/Total Fuel (L/yr)", "System/Load Ren Frac (%)"]]


# ---------------- charts ----------------

def chart_dispatch_week(homer, sim, start):
    sl = slice(start, start + 7 * 24)
    t = homer["Time"].iloc[sl]
    s = sim.iloc[sl]
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 6.2), sharex=True,
                                   gridspec_kw={"height_ratios": [2, 1], "hspace": 0.12})
    ax1.stackplot(t, s["pv_to_load_kw"], s["batt_to_load_kw"], s["gen_to_load_kw"],
                  colors=[PV_C, BATT_C, GEN_C], labels=["PV to load", "Battery to load", "Diesel to load"],
                  edgecolor=SURFACE, linewidth=0.4)
    ax1.plot(t, homer["AC Primary Load"].iloc[sl], color=INK, linewidth=1.4, label="Village load")
    ax1.set_ylabel("Power (kW)")
    ax1.set_ylim(0, None)
    ax1.set_title(f"Python dispatch, one week of the rainy season ({t.iloc[0]:%d %b} - {t.iloc[-1]:%d %b})")
    ax1.legend(loc="upper left", ncol=4, fontsize=9)

    ax2.plot(t, s["soc_pct"], color=BATT_C, linewidth=4, alpha=0.55, label="Python")
    ax2.plot(t, homer["Batt State of Charge"].iloc[sl], color=INK, linewidth=1.2, linestyle="--", label="HOMER")
    ax2.axhline(100 * SOC_MIN, color=MUTED, linewidth=1, linestyle=":")
    ax2.text(t.iloc[0], 100 * SOC_MIN - 11, "minimum SoC 20%", color=MUTED, ha="left", fontsize=8)
    ax2.set_ylim(0, 105)
    ax2.set_ylabel("Battery SoC (%)")
    ax2.set_title("Battery state of charge: HOMER vs Python (the lines overlap)", fontsize=10)
    ax2.legend(loc="upper right", ncol=2, fontsize=9)
    fig.savefig(OUT / "dispatch_week.png", bbox_inches="tight")
    plt.close(fig)


def chart_monthly_diesel(homer, sim):
    m = homer["Time"].dt.month
    h = homer["Gen Power Output"].groupby(m).sum() / 1000
    p = sim["gen_kw"].groupby(m).sum() / 1000
    x = np.arange(1, 13)
    fig, ax = plt.subplots(figsize=(10, 4))
    w = 0.38
    ax.bar(x - w / 2 - 0.01, h.values, w, color=INK_2, label="HOMER", edgecolor=SURFACE, linewidth=1)
    ax.bar(x + w / 2 + 0.01, p.values, w, color=GEN_C, label="Python", edgecolor=SURFACE, linewidth=1)
    ax.set_xticks(x, ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])
    ax.set_ylabel("Diesel energy (MWh)")
    ax.set_title("Monthly diesel production: HOMER vs Python")
    ax.legend(loc="upper right")
    ax.grid(axis="x", visible=False)
    fig.savefig(OUT / "monthly_diesel.png", bbox_inches="tight")
    plt.close(fig)


def chart_cash_flow(cf):
    years = cf.index[1:]
    fig, ax = plt.subplots(figsize=(10, 4.3))
    neg_bottom = np.zeros(len(years))
    pos_bottom = np.zeros(len(years))
    colors = {"PV": PV_C, "Battery": BATT_C, "Converter": CONV_C, "Genset": GEN_C}
    for name in ["Genset", "PV", "Battery", "Converter"]:
        vals = cf.loc[years, name].values / 1000
        neg = np.minimum(vals, 0)
        pos = np.maximum(vals, 0)
        ax.bar(years, neg, bottom=neg_bottom, color=colors[name], label=name,
               edgecolor=SURFACE, linewidth=1, width=0.75)
        ax.bar(years, pos, bottom=pos_bottom, color=colors[name], edgecolor=SURFACE, linewidth=1, width=0.75)
        neg_bottom += neg
        pos_bottom += pos
    batt_rep = cf.loc[years, "Battery"].idxmin()
    ax.annotate(f"Battery replacement\nyear {batt_rep}: ${400 * BATT_KWH / 1000:,.0f}k",
                xy=(batt_rep - 0.4, -250), xytext=(batt_rep - 7, -330),
                color=INK_2, fontsize=9, arrowprops=dict(arrowstyle="-", color=MUTED))
    ax.annotate("Salvage value\n(equipment life left)", xy=(YEARS, pos_bottom[-1]), xytext=(YEARS - 5.5, 60),
                color=INK_2, fontsize=9, arrowprops=dict(arrowstyle="-", color=MUTED))
    ax.axhline(0, color=AXIS, linewidth=1)
    capex = -cf.loc[0].sum() / 1000
    ax.set_title(f"Yearly cash flow, years 1-25 (initial investment in year 0: ${capex:,.0f}k)")
    ax.set_ylabel("Cash flow (k$, undiscounted)")
    ax.set_xlabel("Project year")
    ax.legend(loc="lower left", ncol=4)
    ax.grid(axis="x", visible=False)
    fig.savefig(OUT / "cash_flow.png", bbox_inches="tight")
    plt.close(fig)


def chart_architectures(best):
    b = best.iloc[::-1]
    fig, ax = plt.subplots(figsize=(10, 3.8))
    npc = b["Cost/NPC ($)"].values / 1e6
    colors = [PV_C if s == "PV + battery + diesel" else HIGHLIGHT_OFF for s in b["System"]]
    ax.barh(b["System"], npc, color=colors, height=0.6)
    for y, (v, fuel) in enumerate(zip(npc, b["System/Total Fuel (L/yr)"].values)):
        ax.text(v + 0.06, y, f"${v:.2f}M  ·  {fuel:,.0f} L diesel/yr", va="center", color=INK_2, fontsize=9)
    ax.set_xlim(0, npc.max() * 1.45)
    ax.set_xlabel("Net present cost over 25 years (M$)")
    ax.set_title("Cheapest system of each type (HOMER optimisation)")
    ax.grid(axis="y", visible=False)
    ax.tick_params(axis="y", colors=INK_2)
    fig.savefig(OUT / "architectures.png", bbox_inches="tight")
    plt.close(fig)


def main():
    OUT.mkdir(exist_ok=True)
    homer = load_homer(HOMER_HOURLY)

    # 1. PV check: HOMER's output = rated kW x derating x incident irradiance (kW/m2)
    pv_formula = PV_KW * PV_DERATE * homer["PV Incident Solar"]
    print(f"PV formula check: max hourly difference {np.abs(pv_formula - homer['PV Power Output']).max():.2f} kW")

    # 2. Dispatch
    sim = simulate(homer["AC Primary Load"].values, homer["PV Power Output"].values)
    sim.insert(0, "time", homer["Time"])
    sim.to_csv(OUT / "python_dispatch.csv", index=False)

    h_sum = annual_summary(homer["PV Power Output"], homer["AC Primary Load"], homer["Gen Power Output"],
                           homer["Gen Fuel"], homer["Batt Discharge Power"],
                           homer["Excess Electrical Production"], homer["Unmet Electrical Load"])
    p_sum = annual_summary(homer["PV Power Output"], homer["AC Primary Load"], sim["gen_kw"], sim["fuel_l"],
                           sim["batt_discharge_kw"], sim["excess_kw"], sim["unmet_kw"])
    comp = pd.DataFrame({"HOMER": h_sum, "Python": p_sum})
    comp["Difference (%)"] = 100 * (comp["Python"] - comp["HOMER"]) / comp["HOMER"].replace(0, np.nan)
    soc_mae = np.abs(sim["soc_pct"] - homer["Batt State of Charge"]).mean()
    gen_agree = 100 * ((sim["gen_kw"] > 0) == (homer["Gen Power Output"] > 0)).mean()

    # 3. Economics
    homer_npc = pd.read_csv(HOMER_OPTIM, skiprows=[0]).iloc[0]["Cost/NPC ($)"]
    served = h_sum["Load served (kWh)"]
    npc_no_rep, _ = npc_and_lcoe(cash_flow(components(h_sum, replace_battery=False)), served)
    cf = cash_flow(components(h_sum, replace_battery=True))
    npc_rep, lcoe_rep = npc_and_lcoe(cf, served)
    cf_py = cash_flow(components(p_sum, replace_battery=True))
    npc_py, lcoe_py = npc_and_lcoe(cf_py, p_sum["Load served (kWh)"])
    cf.to_csv(OUT / "cash_flow.csv")

    econ = pd.DataFrame({
        "NPC ($)": [homer_npc, npc_no_rep, npc_rep, npc_py],
    }, index=["HOMER (reported)", "Rebuilt, battery NOT replaced", "Rebuilt, battery replaced at year 15",
              "Python dispatch, battery replaced"])
    econ["Difference vs HOMER (%)"] = 100 * (econ["NPC ($)"] - homer_npc) / homer_npc

    best = best_per_architecture(HOMER_OPTIM)

    pd.set_option("display.float_format", lambda v: f"{v:,.1f}")
    print("\n=== Annual energy: HOMER vs Python ===")
    print(comp.to_string())
    print(f"\nHours where both agree on genset ON/OFF: {gen_agree:.1f}%")
    print(f"Mean absolute SoC difference: {soc_mae:.1f} percentage points")
    print("\n=== Economics (25 years, real discount rate "
          f"{100 * real_rate():.2f}%) ===")
    print(econ.to_string())
    print(f"LCOE (HOMER energy, battery replaced): ${lcoe_rep:.4f}/kWh   Python dispatch: ${lcoe_py:.4f}/kWh")
    print("\n=== Cheapest system of each type ===")
    print(best.to_string(index=False, formatters={"Cost/LCOE ($/kWh)": "{:.3f}".format}))

    comp.to_csv(OUT / "energy_comparison.csv")
    econ.to_csv(OUT / "economics_comparison.csv")
    best.to_csv(OUT / "architectures.csv", index=False)

    # 4. Charts: the rainy-season week (Jan-May) with the most diesel hours
    rainy = homer["Time"].dt.month <= 5
    weekly = (homer["Gen Power Output"] > 0).where(rainy, 0).rolling(7 * 24).sum()
    start = int(weekly.idxmax()) - 7 * 24 + 1
    chart_dispatch_week(homer, sim, start)
    chart_monthly_diesel(homer, sim)
    chart_cash_flow(cf)
    chart_architectures(best)
    print(f"\nCharts and tables saved to {OUT}")


if __name__ == "__main__":
    main()
