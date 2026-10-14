import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import {
  aggregate,
  baselineScenario,
  computeAll,
  computeTown,
  currentPriceChange,
  electricPrice,
  generationIndex,
  normalizeMix,
  type EnergyData,
  type Scenario,
} from './burden'

const data: EnergyData = JSON.parse(
  readFileSync(new URL('../data/energy.json', import.meta.url), 'utf8'),
)
const ids = Object.keys(data.towns)
const byName = (n: string) => ids.find((id) => data.towns[id].name === n)!
const base = baselineScenario(data.assumptions)
const withChange = (patch: Partial<Scenario>): Scenario => ({ ...base, ...patch })

describe('baseline', () => {
  it('reproduces LEAD spend and income at 0% change', () => {
    const id = byName('Hartford')
    const r = computeTown(id, data, base, true)
    let hh = 0
    let spend = 0
    let inc = 0
    for (const [, , n, income, e, g, o] of data.towns[id].cells) {
      hh += n
      inc += n * income
      spend += n * (e + g + o)
    }
    expect(r.households).toBeCloseTo(hh, 6)
    expect(r.total * r.households).toBeCloseTo(spend, 3)
    expect(r.burden).toBeCloseTo(spend / inc, 10)
  })

  it('covers all 169 towns with plausible burdens', () => {
    const all = computeAll(data, base, null)
    expect(Object.keys(all)).toHaveLength(169)
    for (const r of Object.values(all)) {
      expect(r.burden).toBeGreaterThan(0.01)
      expect(r.burden).toBeLessThan(0.1)
    }
    const state = aggregate(Object.values(all))
    expect(state.households).toBeGreaterThan(1.4e6)
  })
})

describe('price changes', () => {
  it('a 10% oil increase only raises oil spend', () => {
    const id = byName('Hartford')
    const b = computeTown(id, data, base, true)
    const s = computeTown(id, data, withChange({ priceChange: { ...base.priceChange, oil: 0.1 } }), true)
    expect(s.spend.oil).toBeCloseTo(b.spend.oil * 1.1, 6)
    expect(s.spend.elec).toBeCloseTo(b.spend.elec, 6)
    expect(s.spend.gas).toBeCloseTo(b.spend.gas, 6)
    expect(s.spend.propane).toBeCloseTo(b.spend.propane, 6)
  })

  it('an oil price cut helps oil-heavy towns more than gas-heavy ones', () => {
    const cut = withChange({ priceChange: { ...base.priceChange, oil: -0.2 } })
    const drop = (n: string) => {
      const id = byName(n)
      return computeTown(id, data, base, false).burden - computeTown(id, data, cut, false).burden
    }
    const share = (n: string) => {
      const r = computeTown(byName(n), data, base, false)
      return r.spend.oil / r.total
    }
    const [hi, lo] = share('Hartford') < share('Salem') ? ['Salem', 'Hartford'] : ['Hartford', 'Salem']
    expect(drop(hi)).toBeGreaterThan(drop(lo))
  })

  it('current-price preset moves baseline prices to current prices', () => {
    const pc = currentPriceChange(data)
    expect(data.prices.baseline.oil * (1 + pc.oil)).toBeCloseTo(data.prices.current.oil, 8)
  })
})

describe('generation mix', () => {
  it('renormalizes weights to shares', () => {
    const n = normalizeMix({ a: 2, b: 6 })
    expect(n.a).toBeCloseTo(0.25)
    expect(n.b).toBeCloseTo(0.75)
    expect(normalizeMix({ a: 0, b: 0 })).toEqual({ a: 0, b: 0 })
  })

  it('baseline mix has index 1, scaled weights too', () => {
    expect(generationIndex(base.genMix, data.assumptions)).toBeCloseTo(1, 10)
    const doubled = Object.fromEntries(Object.entries(base.genMix).map(([k, v]) => [k, v * 2]))
    expect(generationIndex(doubled, data.assumptions)).toBeCloseTo(1, 10)
  })

  it('a cheaper mix only lowers the supply part of the electric price', () => {
    const mix = { ...base.genMix, gas: 0, nuclear: 74 } // nuclear is cheaper than gas
    const idx = generationIndex(mix, data.assumptions)
    expect(idx).toBeLessThan(1)
    const supply = data.assumptions.electric_supply_share.value.Eversource
    const p = electricPrice(data, withChange({ genMix: mix }), 'Eversource')
    expect(p).toBeCloseTo(data.prices.baseline.elec * (supply * idx + 1 - supply), 10)
  })
})

describe('production levers', () => {
  it('heat pump switch at COP 3 lowers spend for oil-heated homes at baseline prices', () => {
    const id = byName('Salem')
    const b = computeTown(id, data, base, true)
    const s = computeTown(id, data, withChange({ heatSwitch: 1, heatPumpCop: 3 }), true)
    expect(s.spend.oil).toBe(0)
    expect(s.spend.elec).toBeGreaterThan(b.spend.elec)
    expect(s.total).toBeLessThan(b.total)
  })

  it('local levers apply only to the selected town unless scope is all', () => {
    const sel = byName('Salem')
    const other = byName('Hartford')
    const s = withChange({ solarShare: 0.5 })
    const scoped = computeAll(data, s, sel)
    const baseAll = computeAll(data, base, sel)
    expect(scoped[sel].spend.elec).toBeLessThan(baseAll[sel].spend.elec)
    expect(scoped[other].spend.elec).toBeCloseTo(baseAll[other].spend.elec, 8)
    const all = computeAll(data, { ...s, localScope: 'all' }, sel)
    expect(all[other].spend.elec).toBeLessThan(baseAll[other].spend.elec)
  })

  it('solar priced at retail is neutral', () => {
    const id = byName('Salem')
    const b = computeTown(id, data, base, true)
    const s = computeTown(id, data, withChange({ solarShare: 0.6, solarCost: data.prices.baseline.elec }), true)
    expect(s.total).toBeCloseTo(b.total, 6)
  })
})
