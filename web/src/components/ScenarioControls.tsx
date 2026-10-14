import {
  FUELS,
  currentPriceChange,
  baselineScenario,
  mixCost,
  normalizeMix,
  type EnergyData,
  type Fuel,
  type Scenario,
} from '../model/burden'
import { FUEL_COLORS } from '../colors'
import { pct, signedPct } from '../format'

interface Props {
  data: EnergyData
  scenario: Scenario
  onChange: (s: Scenario) => void
  townName: string | null
  elecPrice: number
}

const PRICE_DIGITS: Record<Fuel, number> = { elec: 3, gas: 2, oil: 2, propane: 2 }

function Slider(props: {
  label: string
  value: number
  min: number
  max: number
  step: number
  display: string
  hint?: string
  onChange: (v: number) => void
}) {
  return (
    <label className="slider">
      <span className="slider-head">
        <span>{props.label}</span>
        <span className="slider-value">{props.display}</span>
      </span>
      <input
        type="range"
        min={props.min}
        max={props.max}
        step={props.step}
        value={props.value}
        onChange={(e) => props.onChange(Number(e.target.value))}
      />
      {props.hint && <span className="slider-hint">{props.hint}</span>}
    </label>
  )
}

export function ScenarioControls({ data, scenario: s, onChange, townName, elecPrice }: Props) {
  const a = data.assumptions
  const base = data.prices.baseline
  const sources = a.generation.sources
  const shares = normalizeMix(s.genMix)
  const set = (patch: Partial<Scenario>) => onChange({ ...s, ...patch })
  const setPrice = (f: Fuel, v: number) => set({ priceChange: { ...s.priceChange, [f]: v } })
  const fresh = baselineScenario(a)

  return (
    <div className="controls">
      <section>
        <div className="section-head">
          <h3>Retail prices</h3>
          <span className="scope">All towns</span>
        </div>
        <div className="button-row">
          <button onClick={() => set({ priceChange: fresh.priceChange })}>2022 prices</button>
          <button onClick={() => set({ priceChange: currentPriceChange(data) })}>Latest EIA prices</button>
        </div>
        {FUELS.map((f) => {
          const price = base[f] * (1 + s.priceChange[f])
          return (
            <Slider
              key={f}
              label={FUEL_COLORS[f].label}
              value={Math.round(s.priceChange[f] * 100)}
              min={-60}
              max={60}
              step={1}
              display={`${signedPct(s.priceChange[f], 0)} · $${price.toFixed(PRICE_DIGITS[f])}${data.prices.units[f].slice(1)}`}
              onChange={(v) => setPrice(f, v / 100)}
            />
          )
        })}
      </section>

      <section>
        <div className="section-head">
          <h3>Electric generation mix</h3>
          <span className="scope">All towns</span>
        </div>
        <p className="note">
          Supply cost ${mixCost(s.genMix, sources).toFixed(0)}/MWh vs ${mixCost(fresh.genMix, sources).toFixed(0)}{' '}
          baseline. Only the supply part of the bill moves.
          {townName && (
            <>
              {' '}
              {townName} pays <strong>{(elecPrice * 100).toFixed(1)}¢/kWh</strong>.
            </>
          )}
        </p>
        <div className="mix-bar" aria-hidden>
          {sources.map((src) =>
            shares[src.key] > 0 ? (
              <span key={src.key} style={{ flexGrow: shares[src.key] }} className={`mix-seg mix-${src.key}`} />
            ) : null,
          )}
        </div>
        {sources.map((src) => (
          <Slider
            key={src.key}
            label={src.label}
            value={s.genMix[src.key] ?? 0}
            min={0}
            max={100}
            step={1}
            display={`${pct(shares[src.key] ?? 0, 0)} · $${src.cost}/MWh`}
            onChange={(v) => set({ genMix: { ...s.genMix, [src.key]: v } })}
          />
        ))}
        <div className="button-row">
          <button onClick={() => set({ genMix: fresh.genMix })}>ISO-NE 2024 mix</button>
          <button
            onClick={() =>
              set({ genMix: { ...fresh.genMix, gas: 30, offshore: 15, solar: 10, wind: 6 } })
            }
          >
            More renewables
          </button>
        </div>
      </section>

      <section>
        <div className="section-head">
          <h3>Local production</h3>
          <div className="segmented" role="radiogroup" aria-label="Where local changes apply">
            <button
              role="radio"
              aria-checked={s.localScope === 'town'}
              className={s.localScope === 'town' ? 'on' : ''}
              onClick={() => set({ localScope: 'town' })}
            >
              {townName ?? 'Selected town'}
            </button>
            <button
              role="radio"
              aria-checked={s.localScope === 'all'}
              className={s.localScope === 'all' ? 'on' : ''}
              onClick={() => set({ localScope: 'all' })}
            >
              All towns
            </button>
          </div>
        </div>
        {s.localScope === 'town' && !townName && <p className="note">Pick a town on the map to apply these.</p>}
        <Slider
          label="Oil & propane homes switching to heat pumps"
          value={Math.round(s.heatSwitch * 100)}
          min={0}
          max={100}
          step={5}
          display={pct(s.heatSwitch, 0)}
          onChange={(v) => set({ heatSwitch: v / 100 })}
        />
        <Slider
          label="Heat pump efficiency (seasonal COP)"
          value={s.heatPumpCop}
          min={1.5}
          max={4.5}
          step={0.1}
          display={s.heatPumpCop.toFixed(1)}
          onChange={(v) => set({ heatPumpCop: v })}
        />
        <Slider
          label="Household electricity from solar"
          value={Math.round(s.solarShare * 100)}
          min={0}
          max={100}
          step={5}
          display={pct(s.solarShare, 0)}
          onChange={(v) => set({ solarShare: v / 100 })}
        />
        <Slider
          label="Solar cost to household"
          value={Math.round(s.solarCost * 1000) / 10}
          min={0}
          max={30}
          step={0.5}
          display={`${(s.solarCost * 100).toFixed(1)}¢/kWh`}
          hint="Lease, PPA or community-solar price. 0 means owned and paid off."
          onChange={(v) => set({ solarCost: v / 100 })}
        />
      </section>

      <button className="reset" onClick={() => onChange({ ...fresh, localScope: s.localScope })}>
        Reset everything to 2022 baseline
      </button>
      <p className="note">
        Baseline incomes and bills: {data.source}. Baseline prices: EIA 2022 CT residential averages. Consumption is
        held constant when prices change.
      </p>
    </div>
  )
}
