# Connecticut Energy Burden Explorer

Interactive map of household **energy burden** (share of income spent on home energy) across Connecticut's 169 towns. Change fuel prices, the electric generation mix, heat-pump adoption and solar to see how each town's residents are affected compared with their county and the state.

- **State view**: the 8 historical counties, shaded by burden. Click one to zoom in.
- **County view**: the county's towns, plus a ranked chart of every town in the county.
- **Town view**: click a town to see its burden, cost per household, share of households above the 6% affordability line, spend by fuel, burden by income bracket and how homes are heated. Each value is shown as 2022 baseline vs your scenario, and the town is compared with its county and the state.

## Run it

```bash
cd web
npm install
npm run dev          # http://localhost:5173
npm test             # model unit tests
npm run build        # static site in web/dist
npm run build:single # one self-contained HTML file in web/dist-single
```

The processed data is committed in `web/src/data/`, so Python is only needed to rebuild it.

## Rebuild the data

```bash
python3 -m venv .venv && .venv/bin/pip install -r pipeline/requirements.txt
.venv/bin/python pipeline/fetch.py   # downloads ~200 MB into pipeline/raw/ (gitignored)
.venv/bin/python pipeline/build.py   # writes web/src/data/*.json
```

`build.py` asserts 169 towns, 8 counties, every town has LEAD data, and that town households sum to the LEAD state total.

## Data sources

| What | Source |
|---|---|
| Households, income, electricity / gas / other-fuel spend by income bracket and heating fuel | DOE [Low-Income Energy Affordability Data (LEAD) Tool](https://data.openei.org/submissions/6219), 2022 update, CT census tracts (AMI file) |
| Town and historical county boundaries | U.S. Census TIGER/Line 2020 (used for the tract → town join) and 2020 cartographic boundary files (drawn on the map). The 2020 vintage still has the 8 counties; Census switched CT to planning regions in 2022. |
| Tract geography | TIGER/Line 2022 tracts. LEAD 2022 uses planning-region tract IDs. |
| Electricity price | EIA State Electricity Profile, CT Table 8 (2022 residential average 24.61¢/kWh; latest year for the "Latest EIA prices" preset) |
| Natural gas price | EIA CT residential natural gas price, annual ($/Mcf → $/therm at 10.37 therms/Mcf) |
| Heating oil & propane | EIA weekly CT residential prices. Baseline = Oct 2022 to Mar 2023 heating-season average; preset = latest week. |
| Generation mix | ISO New England 2024 net energy for load |
| Electric utility by town | `pipeline/reference/town_utilities.csv` (UI's 17 towns, municipal utilities, Eversource elsewhere) |

## Method

All modelling is in `web/src/model/burden.ts`.

1. **Aggregation.** Each 2022 LEAD tract is assigned to the town containing its representative point. CT tracts nest within towns. For each town, LEAD rows are summed into cells of *income bracket (% of area median income) × primary heating fuel*. Each cell holds household count, mean income and mean annual spend on electricity, utility gas and other fuel.
2. **Quantities.** Spend is converted to kWh, therms and gallons at the 2022 baseline prices. LEAD's "other fuel" spend is treated as heating oil for oil-heated homes and as propane for bottled-gas, electric- and gas-heated homes. For wood-, coal- and other-heated homes it is held fixed.
3. **Prices.** The price sliders scale each fuel's retail price. Consumption is held constant (no price elasticity).
4. **Generation mix.**
   - The mix's average $/MWh cost, relative to the ISO-NE baseline mix, scales only the *supply* part of the electric bill. That part is about 44% for Eversource and UI and 50% for municipal utilities; delivery charges don't move.
   - Per-source costs in `pipeline/reference/assumptions.json` are illustrative, for comparing mixes, not market forecasts.
5. **Heat pumps.**
   - A share of oil- and propane-heated households stops buying that fuel.
   - The same delivered heat is bought as electricity instead: `kWh = gallons × Btu/gal × furnace efficiency ÷ (3412 × COP)`.
6. **Solar.** A share of each household's kWh is supplied at the "solar cost" instead of the retail rate. A cost of 0 represents owned, paid-off panels; a cost equal to the retail rate is break-even.
7. **Burden.**
   - Town burden is total spend ÷ total income, as in LEAD.
   - "Households over 6%" counts households in cells whose mean burden exceeds 6%, or whose income is zero or negative. It is coarse because it works on cell means.

Price and generation changes always apply to every town, so the map shows *relative* effects: for example, which towns gain most from cheaper heating oil. The heat-pump and solar levers apply either to the selected town only (to compare it against unchanged neighbours) or to all towns.

## Layout

```
pipeline/            Python data pipeline (fetch.py, build.py, reference/)
web/                 Vite + React + TypeScript app
  src/model/         scenario model + vitest tests
  src/components/    map (d3-geo), charts, controls
  src/data/          generated data (committed)
```
