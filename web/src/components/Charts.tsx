import { useState } from 'react'
import { FUEL_COLORS, fuelColor } from '../colors'
import { count, money, pct, signedMoney, signedPts } from '../format'
import type { Group, SpendKey } from '../model/burden'

const SPEND_KEYS: SpendKey[] = ['elec', 'gas', 'oil', 'propane', 'other']

function Delta({ v, fmt, goodWhenNegative = true }: { v: number; fmt: (v: number) => string; goodWhenNegative?: boolean }) {
  const shown = fmt(v)
  const zero = /^[−+]?\$?0(\.0+)?( pts|%)?$/.test(shown.replace('+', ''))
  if (zero) return <span className="delta neutral">no change</span>
  const good = goodWhenNegative ? v < 0 : v > 0
  return (
    <span className={`delta ${good ? 'good' : 'bad'}`}>
      <span aria-hidden>{v < 0 ? '▼' : '▲'}</span> {shown}
    </span>
  )
}

export function StatTiles({ base, scen }: { base: Group; scen: Group }) {
  const tiles = [
    {
      label: 'Energy burden',
      value: pct(scen.burden, 2),
      sub: `of income · 2022: ${pct(base.burden, 2)}`,
      delta: <Delta v={scen.burden - base.burden} fmt={(v) => signedPts(v)} />,
    },
    {
      label: 'Energy cost per household',
      value: money(scen.total),
      sub: `per year · 2022: ${money(base.total)}`,
      delta: <Delta v={scen.total - base.total} fmt={signedMoney} />,
    },
    {
      label: 'Households over 6% burden',
      value: pct(scen.highBurdenShare, 0),
      sub: `≈ ${count(scen.highBurdenShare * scen.households)} homes · 2022: ${pct(base.highBurdenShare, 0)}`,
      delta: <Delta v={scen.highBurdenShare - base.highBurdenShare} fmt={(v) => signedPts(v, 1)} />,
    },
  ]
  return (
    <div className="tiles">
      {tiles.map((t) => (
        <div className="tile" key={t.label}>
          <div className="tile-label">{t.label}</div>
          <div className="tile-value">{t.value}</div>
          <div className="tile-delta">{t.delta}</div>
          <div className="tile-sub">{t.sub}</div>
        </div>
      ))}
    </div>
  )
}

function Legend({ keys, values }: { keys: SpendKey[]; values?: Record<string, string> }) {
  return (
    <div className="legend">
      {keys.map((k) => (
        <span key={k} className="legend-item">
          <span className="swatch" style={{ background: fuelColor(k) }} />
          {FUEL_COLORS[k].label}
          {values && <span className="legend-value">{values[k]}</span>}
        </span>
      ))}
    </div>
  )
}

/** Two stacked bars (2022 vs scenario) of annual spend per household by fuel. */
export function SpendBars({ base, scen }: { base: Group; scen: Group }) {
  const [hover, setHover] = useState<string | null>(null)
  const max = Math.max(base.total, scen.total) || 1
  const keys = SPEND_KEYS.filter((k) => base.spend[k] > 1 || scen.spend[k] > 1)
  const rows = [
    { label: '2022', g: base },
    { label: 'Scenario', g: scen },
  ]
  return (
    <figure className="chart">
      <figcaption>Annual energy spend per household, by fuel</figcaption>
      {rows.map(({ label, g }) => (
        <div className="hbar-row" key={label}>
          <span className="hbar-label">{label}</span>
          <div className="hbar-track">
            <div className="hbar-stack" style={{ width: `${(g.total / max) * 100}%` }}>
              {keys.map((k) =>
                g.spend[k] > 0.5 ? (
                  <span
                    key={k}
                    className="hbar-seg"
                    style={{ flexGrow: g.spend[k], background: fuelColor(k) }}
                    onMouseEnter={() => setHover(`${label} · ${FUEL_COLORS[k].label}: ${money(g.spend[k])}`)}
                    onMouseLeave={() => setHover(null)}
                  />
                ) : null,
              )}
            </div>
            <span className="hbar-value">{money(g.total)}</span>
          </div>
        </div>
      ))}
      <div className="chart-hover" aria-live="polite">
        {hover ?? ' '}
      </div>
      <Legend keys={keys} values={Object.fromEntries(keys.map((k) => [k, money(scen.spend[k])]))} />
    </figure>
  )
}

/** Burden by income bracket: scenario bars with a 2022 tick, 6% threshold line. */
export function AmiChart({ brackets, base, scen }: { brackets: string[]; base: Group[]; scen: Group[] }) {
  const [hover, setHover] = useState<number | null>(null)
  const capped = (v: number) => Math.min(v, 0.3)
  const max = Math.max(0.08, ...scen.map((g) => capped(g.burden)), ...base.map((g) => capped(g.burden)))
  const H = 120
  const y = (v: number) => H - (capped(v) / max) * H
  return (
    <figure className="chart">
      <figcaption>Energy burden by household income (% of area median income)</figcaption>
      <div className="vbars" style={{ height: H + 50 }}>
        <div className="threshold" style={{ top: y(0.06) + 20 }}>
          <span>6% affordability line</span>
        </div>
        {brackets.map((b, i) => {
          const s = scen[i]
          const bb = base[i]
          return (
            <div
              className={`vbar-col${hover === i ? ' hover' : ''}`}
              key={b}
              onMouseEnter={() => setHover(i)}
              onMouseLeave={() => setHover(null)}
            >
              <div className="vbar-area" style={{ height: H }}>
                <span className="vbar-value" style={{ top: y(s.burden) - 16 }}>
                  {s.households > 0 ? pct(s.burden, 1) : '–'}
                </span>
                <span className="vbar" style={{ top: y(s.burden), height: H - y(s.burden) }} />
                <span className="vbar-base" style={{ top: y(bb.burden) }} title="2022" />
              </div>
              <span className="vbar-label">{b}</span>
            </div>
          )
        })}
      </div>
      <div className="chart-hover" aria-live="polite">
        {hover !== null
          ? `${brackets[hover]} AMI · ${count(scen[hover].households)} households · income ${money(scen[hover].income)} · burden ${pct(scen[hover].burden, 1)} (2022 ${pct(base[hover].burden, 1)})`
          : 'Bars: scenario. Tick: 2022. Burdens over 30% are drawn at the top of the chart.'}
      </div>
    </figure>
  )
}

const HEAT_LABELS: Record<string, string> = {
  elec: 'Electric',
  gas: 'Natural gas',
  oil: 'Heating oil',
  propane: 'Propane',
  other: 'Wood & other',
}

export function HeatingMix({ fuels, households }: { fuels: string[]; households: number[] }) {
  const total = households.reduce((a, b) => a + b, 0) || 1
  return (
    <figure className="chart">
      <figcaption>How homes are heated</figcaption>
      <div className="hbar-track">
        <div className="hbar-stack" style={{ width: '100%' }}>
          {fuels.map((f, i) =>
            households[i] > 0 ? (
              <span
                key={f}
                className="hbar-seg"
                style={{ flexGrow: households[i], background: fuelColor(f) }}
                title={`${HEAT_LABELS[f]}: ${pct(households[i] / total, 0)}`}
              />
            ) : null,
          )}
        </div>
      </div>
      <div className="legend">
        {fuels.map((f, i) => (
          <span key={f} className="legend-item">
            <span className="swatch" style={{ background: fuelColor(f) }} />
            {HEAT_LABELS[f]}
            <span className="legend-value">{pct(households[i] / total, 0)}</span>
          </span>
        ))}
      </div>
    </figure>
  )
}

export interface RankItem {
  id: string
  name: string
  base: number
  scen: number
}

/** Ranked horizontal bars of scenario burden with 2022 ticks and reference lines. */
export function RankChart(props: {
  title: string
  items: RankItem[]
  selected: string | null
  onSelect: (id: string) => void
  refs: { label: string; value: number }[]
}) {
  const items = props.items.slice().sort((a, b) => b.scen - a.scen)
  const max = Math.max(...items.map((i) => Math.max(i.scen, i.base)), ...props.refs.map((r) => r.value)) * 1.08
  const x = (v: number) => `${(v / max) * 100}%`
  return (
    <figure className="chart rank">
      <figcaption>{props.title}</figcaption>
      <div className="rank-body">
        <div className="rank-refs" aria-hidden>
          {props.refs.map((r) => (
            <div key={r.label} className="rank-ref" style={{ left: x(r.value) }}>
              <span>
                {r.label} {pct(r.value, 1)}
              </span>
            </div>
          ))}
        </div>
        {items.map((it) => {
          const d = it.scen - it.base
          return (
            <button
              key={it.id}
              className={`rank-row${it.id === props.selected ? ' selected' : ''}`}
              onClick={() => props.onSelect(it.id)}
              title={`${it.name}: ${pct(it.scen, 2)} (2022 ${pct(it.base, 2)})`}
            >
              <span className="rank-name">{it.name}</span>
              <span className="rank-track">
                <span className="rank-bar" style={{ width: x(it.scen) }} />
                <span className="rank-base" style={{ left: x(it.base) }} />
              </span>
              <span className="rank-value">
                {pct(it.scen, 2)}
                {Math.abs(d) >= 0.00005 && <span className={d < 0 ? 'good' : 'bad'}> {signedPts(d)}</span>}
              </span>
            </button>
          )
        })}
      </div>
    </figure>
  )
}
