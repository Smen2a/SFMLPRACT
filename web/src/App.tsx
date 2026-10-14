import type { FeatureCollection, Geometry } from 'geojson'
import { useMemo, useState } from 'react'
import { diverging, sequential } from './colors'
import { CTMap, type CountyProps, type TownProps } from './components/CTMap'
import { AmiChart, HeatingMix, RankChart, SpendBars, StatTiles } from './components/Charts'
import { ScenarioControls } from './components/ScenarioControls'
import countiesJson from './data/counties.geo.json'
import energyJson from './data/energy.json'
import townsJson from './data/towns.geo.json'
import { count, money, pct, signedPts } from './format'
import {
  aggregate,
  baselineScenario,
  burdenRanks,
  computeAll,
  electricPrice,
  type EnergyData,
  type Group,
  type Scenario,
  type TownResult,
} from './model/burden'

const data = energyJson as unknown as EnergyData
const counties = countiesJson as unknown as FeatureCollection<Geometry, CountyProps>
const towns = townsJson as unknown as FeatureCollection<Geometry, TownProps>
const townIds = Object.keys(data.towns)
const countyIds = Object.keys(data.counties).sort((a, b) => data.counties[a].localeCompare(data.counties[b]))

type Metric = 'scenario' | 'baseline' | 'change'

function groupByCounty(results: Record<string, TownResult>): Record<string, Group> {
  return Object.fromEntries(
    countyIds.map((c) => [c, aggregate(townIds.filter((id) => data.towns[id].county === c).map((id) => results[id]))]),
  )
}

const baseResults = computeAll(data, baselineScenario(data.assumptions), null)
const baseCounties = groupByCounty(baseResults)
const baseState = aggregate(Object.values(baseResults))
const baseRanks = burdenRanks(baseResults)
const burdens = Object.values(baseResults).map((r) => r.burden)
const SEQ_DOMAIN: [number, number] = [Math.min(...burdens), Math.max(...burdens)]

export default function App() {
  const [county, setCounty] = useState<string | null>(null)
  const [town, setTown] = useState<string | null>(null)
  const [scenario, setScenario] = useState<Scenario>(() => baselineScenario(data.assumptions))
  const [metric, setMetric] = useState<Metric>('scenario')

  const results = useMemo(() => computeAll(data, scenario, town), [scenario, town])
  const countyResults = useMemo(() => groupByCounty(results), [results])
  const state = useMemo(() => aggregate(Object.values(results)), [results])
  const ranks = useMemo(() => burdenRanks(results), [results])

  const value = (scen: Group, base: Group) =>
    metric === 'scenario' ? scen.burden : metric === 'baseline' ? base.burden : scen.burden - base.burden
  const townValues = Object.fromEntries(townIds.map((id) => [id, value(results[id], baseResults[id])]))
  const countyValues = Object.fromEntries(countyIds.map((c) => [c, value(countyResults[c], baseCounties[c])]))

  const scale = useMemo(() => {
    if (metric !== 'change') return sequential(SEQ_DOMAIN[0], SEQ_DOMAIN[1])
    const m = Math.max(0.0005, ...Object.values(townValues).map(Math.abs))
    return diverging(m)
  }, [metric, townValues])
  const fmt = (v: number) => (metric === 'change' ? `${signedPts(v)} burden` : `${pct(v, 2)} energy burden`)

  const selectCounty = (c: string | null) => {
    setCounty(c)
    if (c === null || (town && data.towns[town].county !== c)) setTown(null)
  }
  const selectTown = (id: string) => {
    setCounty(data.towns[id].county)
    setTown(id)
  }

  const t = town ? data.towns[town] : null
  const countyTownIds = county ? townIds.filter((id) => data.towns[id].county === county) : []
  const countyRanks = county ? burdenRanks(results, countyTownIds) : {}
  const baseCountyRanks = county ? burdenRanks(baseResults, countyTownIds) : {}

  return (
    <div className="app">
      <header className="top">
        <div>
          <h1>Connecticut Energy Burden Explorer</h1>
          <p className="lede">
            Share of household income spent on electricity, gas, heating oil and propane, by town. Change prices
            and how energy is produced to see who gains and who is left behind.
          </p>
        </div>
        <nav className="crumbs" aria-label="Map level">
          <button onClick={() => selectCounty(null)} className={county ? '' : 'current'}>
            Connecticut
          </button>
          {county && (
            <>
              <span aria-hidden>›</span>
              <button onClick={() => setTown(null)} className={town ? '' : 'current'}>
                {data.counties[county]} County
              </button>
            </>
          )}
          {t && (
            <>
              <span aria-hidden>›</span>
              <span className="current">{t.name}</span>
            </>
          )}
        </nav>
      </header>

      <main className="layout">
        <aside className="panel controls-panel">
          <ScenarioControls
            data={data}
            scenario={scenario}
            onChange={setScenario}
            townName={t?.name ?? null}
            elecPrice={electricPrice(data, scenario, t?.utility ?? 'Eversource')}
          />
        </aside>

        <section className="panel map-panel">
          <div className="map-toolbar">
            <div className="segmented" role="radiogroup" aria-label="Map shows">
              {(
                [
                  ['scenario', 'Scenario burden'],
                  ['baseline', '2022 burden'],
                  ['change', 'Change'],
                ] as [Metric, string][]
              ).map(([m, label]) => (
                <button key={m} role="radio" aria-checked={metric === m} className={metric === m ? 'on' : ''} onClick={() => setMetric(m)}>
                  {label}
                </button>
              ))}
            </div>
            <MapLegend metric={metric} scale={scale} />
          </div>
          <CTMap
            counties={counties}
            towns={towns}
            countyValues={countyValues}
            townValues={townValues}
            scale={scale}
            format={fmt}
            selectedCounty={county}
            selectedTown={town}
            onSelectCounty={selectCounty}
            onSelectTown={selectTown}
          />
          <p className="hint">
            {county
              ? 'Click a town for details. Click outside the county or use the breadcrumb to go back.'
              : 'Click a county to zoom in to its towns.'}
          </p>
          {county ? (
            <RankChart
              title={`Towns in ${data.counties[county]} County, by scenario energy burden`}
              items={countyTownIds.map((id) => ({
                id,
                name: data.towns[id].name,
                base: baseResults[id].burden,
                scen: results[id].burden,
              }))}
              selected={town}
              onSelect={selectTown}
              refs={[
                { label: 'County', value: countyResults[county].burden },
                { label: 'State', value: state.burden },
              ]}
            />
          ) : (
            <RankChart
              title="Counties, by scenario energy burden"
              items={countyIds.map((c) => ({
                id: c,
                name: data.counties[c],
                base: baseCounties[c].burden,
                scen: countyResults[c].burden,
              }))}
              selected={null}
              onSelect={selectCounty}
              refs={[{ label: 'State', value: state.burden }]}
            />
          )}
        </section>

        <aside className="panel detail-panel">
          {t && town ? (
            <>
              <div className="detail-head">
                <h2>{t.name}</h2>
                <p>
                  {data.counties[t.county]} County · {count(results[town].households)} households · mean income{' '}
                  {money(results[town].income)}
                </p>
                <p>
                  Electric utility: {t.utility}
                  {t.utilityNote && ` (${t.utilityNote})`}
                </p>
                <p className="rank-line">
                  Burden rank <strong>#{ranks[town]}</strong> of 169 statewide (2022: #{baseRanks[town]}) ·{' '}
                  <strong>#{countyRanks[town]}</strong> of {countyTownIds.length} in county (2022: #
                  {baseCountyRanks[town]}). #1 is the highest burden.
                </p>
              </div>
              <StatTiles base={baseResults[town]} scen={results[town]} />
              <Versus
                rows={[
                  { label: t.name, base: baseResults[town], scen: results[town], strong: true },
                  { label: `${data.counties[t.county]} County`, base: baseCounties[t.county], scen: countyResults[t.county] },
                  { label: 'Connecticut', base: baseState, scen: state },
                ]}
              />
              <SpendBars base={baseResults[town]} scen={results[town]} />
              <AmiChart brackets={data.amiBrackets} base={baseResults[town].byAmi} scen={results[town].byAmi} />
              <HeatingMix fuels={data.heatFuels} households={results[town].heatFuelHouseholds} />
            </>
          ) : county ? (
            <>
              <div className="detail-head">
                <h2>{data.counties[county]} County</h2>
                <p>
                  {countyTownIds.length} towns · {count(countyResults[county].households)} households · mean income{' '}
                  {money(countyResults[county].income)}
                </p>
              </div>
              <StatTiles base={baseCounties[county]} scen={countyResults[county]} />
              <Versus
                rows={[
                  { label: `${data.counties[county]} County`, base: baseCounties[county], scen: countyResults[county], strong: true },
                  { label: 'Connecticut', base: baseState, scen: state },
                ]}
              />
              <SpendBars base={baseCounties[county]} scen={countyResults[county]} />
              <p className="hint">Select a town on the map for income-bracket detail and town-level production levers.</p>
            </>
          ) : (
            <>
              <div className="detail-head">
                <h2>Connecticut</h2>
                <p>
                  169 towns · {count(state.households)} households · mean income {money(state.income)}
                </p>
              </div>
              <StatTiles base={baseState} scen={state} />
              <SpendBars base={baseState} scen={state} />
              <p className="hint">Select a county on the map to see its towns.</p>
            </>
          )}
        </aside>
      </main>
      <footer className="foot">
        Data: DOE LEAD Tool 2022 (tract-level household income and energy spend, aggregated to towns) · U.S. Census
        TIGER/Line 2020 boundaries (historical counties) · EIA residential prices · ISO New England 2024 resource mix.
        Generation costs are illustrative. See the README for methodology.
      </footer>
    </div>
  )
}

function Versus({ rows }: { rows: { label: string; base: Group; scen: Group; strong?: boolean }[] }) {
  return (
    <table className="versus">
      <thead>
        <tr>
          <th scope="col">Compared with</th>
          <th scope="col">2022</th>
          <th scope="col">Scenario</th>
          <th scope="col">Change</th>
          <th scope="col">$ / home</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => {
          const d = r.scen.burden - r.base.burden
          const dm = r.scen.total - r.base.total
          return (
            <tr key={r.label} className={r.strong ? 'strong' : ''}>
              <th scope="row">{r.label}</th>
              <td>{pct(r.base.burden, 2)}</td>
              <td>{pct(r.scen.burden, 2)}</td>
              <td className={Math.abs(d) < 0.00005 ? '' : d < 0 ? 'good' : 'bad'}>{signedPts(d)}</td>
              <td className={Math.abs(dm) < 0.5 ? '' : dm < 0 ? 'good' : 'bad'}>
                {dm > 0.5 ? '+' : ''}
                {money(dm)}
              </td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

function MapLegend({ metric, scale }: { metric: Metric; scale: ReturnType<typeof sequential> }) {
  const [lo, hi] = scale.domain
  const f = (v: number) => (metric === 'change' ? signedPts(v, 2) : pct(v, 1))
  return (
    <div className="map-legend" aria-label="Color legend">
      <span>{f(lo)}</span>
      <span className="ramp">
        {scale.steps.map((c) => (
          <span key={c} style={{ background: c }} />
        ))}
      </span>
      <span>{f(hi)}</span>
      <span className="legend-caption">{metric === 'change' ? 'blue = burden falls' : 'energy burden'}</span>
    </div>
  )
}
