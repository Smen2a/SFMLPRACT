"""Build the web app's data files from the raw downloads (run fetch.py first).

Outputs (in web/src/data/):
  counties.geo.json  8 historical CT counties
  towns.geo.json    169 towns (county subdivisions), tagged with their county
  energy.json       per-town household cells from DOE LEAD + prices + model assumptions
"""
from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import pandas as pd

HERE = Path(__file__).parent
RAW = HERE / "raw"
REF = HERE / "reference"
OUT = HERE.parent / "web" / "src" / "data"

SIMPLIFY_DEG = 0.0006
THERMS_PER_MCF = 10.37  # EIA average heat content of delivered natural gas

AMI_ORDER = ["0-30%", "30-60%", "60-80%", "80-100%", "100-150%", "150%+"]
FUEL_GROUP = {
    "ELECTRICITY": "elec",
    "UTILITY GAS": "gas",
    "FUEL OIL": "oil",
    "BOTTLED GAS": "propane",
}
FUEL_ORDER = ["elec", "gas", "oil", "propane", "other"]


def load_geography() -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    counties = gpd.read_file(f"zip://{RAW / 'tl_2020_us_county.zip'}", where="STATEFP = '09'")
    counties = counties[["COUNTYFP", "NAME", "geometry"]].to_crs(4326)
    counties = counties.rename(columns={"COUNTYFP": "county", "NAME": "name"})

    towns = gpd.read_file(f"zip://{RAW / 'tl_2020_09_cousub.zip'}")
    towns = towns[towns["COUSUBFP"] != "00000"]  # water-only "not defined" areas
    towns = towns[["GEOID", "NAME", "COUNTYFP", "geometry"]].to_crs(4326)
    towns = towns.rename(columns={"GEOID": "id", "NAME": "name", "COUNTYFP": "county"})
    return counties, towns


def load_display_shapes() -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Cartographic boundary versions (clipped to the shoreline) for drawing.

    The full TIGER shapes extend into Long Island Sound, so they are used only
    for the tract-to-town join.
    """
    counties = gpd.read_file(f"zip://{RAW / 'cb_2020_us_county_500k.zip'}", where="STATEFP = '09'")
    counties = counties[["COUNTYFP", "NAME", "geometry"]].rename(columns={"COUNTYFP": "county", "NAME": "name"})
    towns = gpd.read_file(f"zip://{RAW / 'cb_2020_09_cousub_500k.zip'}")
    towns = towns[towns["COUSUBFP"] != "00000"]
    towns = towns[["GEOID", "NAME", "COUNTYFP", "geometry"]].rename(
        columns={"GEOID": "id", "NAME": "name", "COUNTYFP": "county"}
    )
    return counties.to_crs(4326), towns.to_crs(4326)


def tract_to_town(towns: gpd.GeoDataFrame) -> pd.Series:
    tracts = gpd.read_file(f"zip://{RAW / 'tl_2022_09_tract.zip'}")[["GEOID", "ALAND", "geometry"]]
    tracts = tracts[tracts["ALAND"] > 0].to_crs(4326)
    pts = tracts.copy()
    pts["geometry"] = tracts.representative_point()
    joined = gpd.sjoin(pts, towns[["id", "geometry"]], how="left", predicate="within")
    missing = joined["id"].isna().sum()
    if missing:
        raise SystemExit(f"{missing} land tracts did not fall inside a town")
    return joined.set_index("GEOID")["id"]


def load_lead(tract_town: pd.Series) -> pd.DataFrame:
    df = pd.read_csv(RAW / "lead" / "CT AMI Census Tracts 2022.csv", dtype={"FIP": str})
    df["tract"] = df["FIP"].str.zfill(11)
    df["town"] = df["tract"].map(tract_town)
    unmatched = df.loc[df["town"].isna(), "UNITS"].sum()
    if unmatched > 0.001 * df["UNITS"].sum():
        raise SystemExit(f"{unmatched:.0f} LEAD households in tracts with no town")
    df = df.dropna(subset=["town"])

    fuel = df["TEN-HFL"].str.replace(r"^(OWNER|RENTER) ", "", regex=True)
    df["fuel_group"] = fuel.map(FUEL_GROUP).fillna("other")
    df["ami"] = df["AMI150"]

    sums = {
        "units": "UNITS",
        "inc_sum": "HINCP*UNITS", "inc_n": "HINCP UNITS",
        "elec_sum": "ELEP*UNITS", "elec_n": "ELEP UNITS",
        "gas_sum": "GASP*UNITS", "gas_n": "GASP UNITS",
        "other_sum": "FULP*UNITS", "other_n": "FULP UNITS",
    }
    g = df.groupby(["town", "ami", "fuel_group"]).agg(**{k: (v, "sum") for k, v in sums.items()}).reset_index()
    g = g[g["units"] > 0.5]
    # Means use LEAD's per-variable denominators, as the LEAD tool does.
    for k in ["inc", "elec", "gas", "other"]:
        g[k] = (g[f"{k}_sum"] / g[f"{k}_n"].where(g[f"{k}_n"] > 0)).fillna(0.0)
    return g[["town", "ami", "fuel_group", "units", "inc", "elec", "gas", "other"]]


def heating_season_mean(path: Path, start_year: int) -> float:
    x = pd.read_excel(path, sheet_name="Data 1", header=2)
    col = next(c for c in x.columns if "Connecticut" in c)
    s = x[["Date", col]].dropna()
    s["Date"] = pd.to_datetime(s["Date"])
    m = s[(s["Date"] >= f"{start_year}-10-01") & (s["Date"] < f"{start_year + 1}-04-01")]
    return float(m[col].mean())


def latest_weekly(path: Path) -> tuple[float, str]:
    x = pd.read_excel(path, sheet_name="Data 1", header=2)
    col = next(c for c in x.columns if "Connecticut" in c)
    s = x[["Date", col]].dropna()
    row = s.iloc[-1]
    return float(row[col]), pd.to_datetime(row["Date"]).date().isoformat()


def load_prices() -> dict:
    sep = pd.read_excel(RAW / "eia" / "sep_ct.xlsx", sheet_name="8. Sales", header=None)
    years = [str(v).replace("Year\n", "") for v in sep.iloc[2, 1:]]
    label = sep[0].astype(str).str.strip()
    price_hdr = label[label.str.startswith("Average price")].index[0]
    res_row = sep.loc[price_hdr:].loc[label.loc[price_hdr:] == "Residential"].iloc[0]
    elec_by_year = {y: float(v) for y, v in zip(years, res_row.iloc[1:]) if y.isdigit() and pd.notna(v)}
    latest_elec_year = max(elec_by_year)

    gas = pd.read_excel(RAW / "eia" / "N3010CT3a.xls", sheet_name="Data 1", header=2).dropna()
    gas_col = gas.columns[1]
    gas["year"] = pd.to_datetime(gas["Date"]).dt.year.astype(str)
    gas_by_year = dict(zip(gas["year"], gas[gas_col].astype(float)))
    latest_gas_year = max(gas_by_year)

    oil_latest, oil_date = latest_weekly(RAW / "eia" / "heating_oil_weekly.xls")
    pro_latest, pro_date = latest_weekly(RAW / "eia" / "propane_weekly.xls")

    baseline = {
        "elec": elec_by_year["2022"] / 100,
        "gas": gas_by_year["2022"] / THERMS_PER_MCF,
        "oil": heating_season_mean(RAW / "eia" / "heating_oil_weekly.xls", 2022),
        "propane": heating_season_mean(RAW / "eia" / "propane_weekly.xls", 2022),
    }
    current = {
        "elec": elec_by_year[latest_elec_year] / 100,
        "gas": gas_by_year[latest_gas_year] / THERMS_PER_MCF,
        "oil": oil_latest,
        "propane": pro_latest,
    }
    return {
        "units": {"elec": "$/kWh", "gas": "$/therm", "oil": "$/gal", "propane": "$/gal"},
        "baseline": {k: round(v, 4) for k, v in baseline.items()},
        "current": {k: round(v, 4) for k, v in current.items()},
        "current_label": {
            "elec": f"EIA {latest_elec_year} annual average",
            "gas": f"EIA {latest_gas_year} annual average",
            "oil": f"EIA weekly, {oil_date}",
            "propane": f"EIA weekly, {pro_date}",
        },
        "baseline_label": {
            "elec": "EIA 2022 annual average residential price",
            "gas": "EIA 2022 annual average residential price",
            "oil": "EIA weekly residential price, Oct 2022 - Mar 2023 average",
            "propane": "EIA weekly residential price, Oct 2022 - Mar 2023 average",
        },
    }


def write_geojson(gdf: gpd.GeoDataFrame, path: Path) -> None:
    gdf = gdf.copy()
    gdf["geometry"] = gdf.geometry.simplify(SIMPLIFY_DEG, preserve_topology=True)
    path.write_text(gdf.to_json(drop_id=True, to_wgs84=True))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    counties, towns = load_geography()
    assert len(counties) == 8, len(counties)
    assert len(towns) == 169, len(towns)

    util = pd.read_csv(REF / "town_utilities.csv").fillna("")
    util_map = dict(zip(util["town"], util["electric_utility"]))
    note_map = dict(zip(util["town"], util["note"]))
    unknown = set(util_map) - set(towns["name"])
    assert not unknown, f"unknown towns in town_utilities.csv: {unknown}"
    towns["utility"] = towns["name"].map(util_map).fillna("Eversource")

    cells = load_lead(tract_to_town(towns))
    missing = set(towns["id"]) - set(cells["town"])
    assert not missing, f"towns without LEAD data: {missing}"

    lead_total = pd.read_csv(RAW / "lead" / "CT AMI Census Tracts 2022.csv", usecols=["UNITS"])["UNITS"].sum()
    kept = cells["units"].sum()
    assert abs(kept - lead_total) / lead_total < 0.01, (kept, lead_total)

    county_names = dict(zip(counties["county"], counties["name"]))
    out_towns = {}
    for t in towns.sort_values("name").itertuples():
        tc = cells[cells["town"] == t.id]
        rows = []
        for c in tc.itertuples():
            rows.append([
                AMI_ORDER.index(c.ami), FUEL_ORDER.index(c.fuel_group),
                round(c.units, 1), round(c.inc), round(c.elec), round(c.gas), round(c.other),
            ])
        rows.sort()
        out_towns[t.id] = {
            "name": t.name,
            "county": t.county,
            "utility": t.utility,
            "utilityNote": note_map.get(t.name, ""),
            "cells": rows,
        }

    assumptions = json.loads((REF / "assumptions.json").read_text())
    assumptions.pop("_comment", None)
    energy = {
        "source": "DOE Low-Income Energy Affordability Data (LEAD) Tool 2022, census tracts aggregated to towns",
        "cellColumns": ["ami", "heatFuel", "households", "income", "elecSpend", "gasSpend", "otherFuelSpend"],
        "amiBrackets": AMI_ORDER,
        "heatFuels": FUEL_ORDER,
        "counties": county_names,
        "prices": load_prices(),
        "assumptions": assumptions,
        "towns": out_towns,
    }
    (OUT / "energy.json").write_text(json.dumps(energy, separators=(",", ":")))

    cb_counties, cb_towns = load_display_shapes()
    assert len(cb_counties) == 8 and set(cb_towns["id"]) == set(towns["id"]), "display shapes do not match"
    write_geojson(cb_counties, OUT / "counties.geo.json")
    write_geojson(cb_towns, OUT / "towns.geo.json")

    # Sanity report
    hh = cells.groupby("town")["units"].sum()
    spend = (cells["elec"] + cells["gas"] + cells["other"]) * cells["units"]
    inc = cells["inc"] * cells["units"]
    burden = spend.groupby(cells["town"]).sum() / inc.groupby(cells["town"]).sum()
    names = towns.set_index("id")["name"]
    print(f"towns {len(out_towns)}  counties {len(county_names)}  households {hh.sum():,.0f} (LEAD {lead_total:,.0f})")
    print(f"state burden {spend.sum() / inc.sum():.2%}  town range {burden.min():.2%} ({names[burden.idxmin()]}) - {burden.max():.2%} ({names[burden.idxmax()]})")
    print("prices", json.dumps(energy["prices"]["baseline"]), json.dumps(energy["prices"]["current"]))
    for f in OUT.iterdir():
        print(f"{f.name:18s} {f.stat().st_size / 1024:8.0f} KB")


if __name__ == "__main__":
    main()
