// Household energy burden model for CT towns.
//
// Each town is a list of LEAD "cells": households grouped by income bracket
// (% of area median income) and primary heating fuel, with mean annual income
// and mean annual spend on electricity, utility gas and other fuels. A scenario
// turns those dollars into physical quantities at baseline (2022) prices,
// applies price and production changes, and prices the result back out.

export const FUELS = ['elec', 'gas', 'oil', 'propane'] as const
export type Fuel = (typeof FUELS)[number]
export type FuelPrices = Record<Fuel, number>

export type SpendKey = Fuel | 'other'
export type Spend = Record<SpendKey, number>

/** [amiIndex, heatFuelIndex, households, income, elecSpend, gasSpend, otherFuelSpend] */
export type Cell = [number, number, number, number, number, number, number]

export interface Town {
  name: string
  county: string
  utility: string
  utilityNote: string
  cells: Cell[]
}

export interface GenSource {
  key: string
  label: string
  share: number
  cost: number
}

export interface Assumptions {
  baseline_year: number
  electric_supply_share: { value: Record<string, number>; source: string }
  generation: { source: string; sources: GenSource[] }
  heating: {
    oil_btu_per_gal: number
    propane_btu_per_gal: number
    oil_efficiency: number
    propane_efficiency: number
    btu_per_kwh: number
    default_heat_pump_cop: number
  }
  solar: { default_cost_cents_per_kwh: number }
  burden_threshold: number
}

export interface EnergyData {
  source: string
  amiBrackets: string[]
  heatFuels: string[] // elec, gas, oil, propane, other
  counties: Record<string, string>
  prices: {
    units: Record<Fuel, string>
    baseline: FuelPrices
    current: FuelPrices
    baseline_label: Record<Fuel, string>
    current_label: Record<Fuel, string>
  }
  assumptions: Assumptions
  towns: Record<string, Town>
}

export interface Scenario {
  /** Fractional retail price change per fuel, e.g. -0.1 for a 10% cut. */
  priceChange: FuelPrices
  /** Generation mix weights by source key; renormalized to shares. */
  genMix: Record<string, number>
  /** Share (0-1) of oil- and propane-heated households switching to heat pumps. */
  heatSwitch: number
  heatPumpCop: number
  /** Share (0-1) of household electricity supplied by solar. */
  solarShare: number
  /** What solar kWh cost the household (lease/PPA/subscription), $/kWh. */
  solarCost: number
  /** Whether heat-switch and solar apply to the selected town only or to every town. */
  localScope: 'town' | 'all'
}

export interface Group {
  households: number
  income: number // mean annual household income
  spend: Spend // mean annual household spend by fuel
  total: number
  burden: number // aggregate spend / aggregate income
  highBurdenShare: number // share of households above the burden threshold
}

export interface TownResult extends Group {
  id: string
  byAmi: Group[]
  heatFuelHouseholds: number[] // indexed like EnergyData.heatFuels
  elecPrice: number
}

const HEAT = { elec: 0, gas: 1, oil: 2, propane: 3, other: 4 } as const

export function baselineScenario(a: Assumptions): Scenario {
  return {
    priceChange: { elec: 0, gas: 0, oil: 0, propane: 0 },
    genMix: Object.fromEntries(a.generation.sources.map((s) => [s.key, s.share])),
    heatSwitch: 0,
    heatPumpCop: a.heating.default_heat_pump_cop,
    solarShare: 0,
    solarCost: a.solar.default_cost_cents_per_kwh / 100,
    localScope: 'town',
  }
}

/** Shares (summing to 1) from arbitrary non-negative weights. */
export function normalizeMix(mix: Record<string, number>): Record<string, number> {
  const total = Object.values(mix).reduce((s, v) => s + Math.max(0, v), 0)
  if (total <= 0) return Object.fromEntries(Object.keys(mix).map((k) => [k, 0]))
  return Object.fromEntries(Object.entries(mix).map(([k, v]) => [k, Math.max(0, v) / total]))
}

/** Average generation cost of a mix, $/MWh. */
export function mixCost(mix: Record<string, number>, sources: GenSource[]): number {
  const shares = normalizeMix(mix)
  return sources.reduce((s, src) => s + (shares[src.key] ?? 0) * src.cost, 0)
}

/** Ratio of the scenario mix's generation cost to the baseline mix's. */
export function generationIndex(mix: Record<string, number>, a: Assumptions): number {
  const srcs = a.generation.sources
  const base = mixCost(Object.fromEntries(srcs.map((s) => [s.key, s.share])), srcs)
  return base > 0 ? mixCost(mix, srcs) / base : 1
}

/** Scenario retail electricity price for a utility, $/kWh. */
export function electricPrice(data: EnergyData, s: Scenario, utility: string): number {
  const a = data.assumptions
  const supply = a.electric_supply_share.value[utility] ?? a.electric_supply_share.value.Eversource
  const gen = generationIndex(s.genMix, a)
  return data.prices.baseline.elec * (1 + s.priceChange.elec) * (supply * gen + (1 - supply))
}

interface Part {
  weight: number // households
  income: number
  spend: Spend
}

function emptySpend(): Spend {
  return { elec: 0, gas: 0, oil: 0, propane: 0, other: 0 }
}

function cellParts(cell: Cell, data: EnergyData, s: Scenario, elecPrice: number, local: boolean): Part[] {
  const [, heat, households, income, elecSpend, gasSpend, otherSpend] = cell
  const base = data.prices.baseline
  const h = data.assumptions.heating
  const price: FuelPrices = {
    elec: elecPrice,
    gas: base.gas * (1 + s.priceChange.gas),
    oil: base.oil * (1 + s.priceChange.oil),
    propane: base.propane * (1 + s.priceChange.propane),
  }

  // Physical quantities at baseline prices.
  const kwh = elecSpend / base.elec
  const therms = gasSpend / base.gas
  let oilGal = 0
  let propaneGal = 0
  let otherFixed = 0
  if (heat === HEAT.oil) oilGal = otherSpend / base.oil
  else if (heat === HEAT.other) otherFixed = otherSpend // wood, coal, etc.: not modelled
  else propaneGal = otherSpend / base.propane // bottled-gas heat, or propane for cooking/water

  const priced = (k: number, oil: number, propane: number): Spend => {
    const solar = local ? s.solarShare : 0
    return {
      elec: k * (1 - solar) * price.elec + k * solar * s.solarCost,
      gas: therms * price.gas,
      oil: oil * price.oil,
      propane: propane * price.propane,
      other: otherFixed,
    }
  }

  const switchable = local && s.heatSwitch > 0 && (heat === HEAT.oil || heat === HEAT.propane)
  if (!switchable) return [{ weight: households, income, spend: priced(kwh, oilGal, propaneGal) }]

  const heatBtu =
    heat === HEAT.oil
      ? oilGal * h.oil_btu_per_gal * h.oil_efficiency
      : propaneGal * h.propane_btu_per_gal * h.propane_efficiency
  const addedKwh = heatBtu / (h.btu_per_kwh * s.heatPumpCop)
  const switched = priced(kwh + addedKwh, 0, heat === HEAT.propane ? 0 : propaneGal)
  return [
    { weight: households * (1 - s.heatSwitch), income, spend: priced(kwh, oilGal, propaneGal) },
    { weight: households * s.heatSwitch, income, spend: switched },
  ]
}

function summarize(parts: Part[], threshold: number): Group {
  let hh = 0
  let inc = 0
  let high = 0
  const spend = emptySpend()
  for (const p of parts) {
    if (p.weight <= 0) continue
    hh += p.weight
    inc += p.weight * p.income
    let t = 0
    for (const k of Object.keys(spend) as SpendKey[]) {
      spend[k] += p.weight * p.spend[k]
      t += p.spend[k]
    }
    if (p.income <= 0 || t / p.income > threshold) high += p.weight
  }
  const total = Object.values(spend).reduce((a, b) => a + b, 0)
  const mean = emptySpend()
  for (const k of Object.keys(spend) as SpendKey[]) mean[k] = hh ? spend[k] / hh : 0
  return {
    households: hh,
    income: hh ? inc / hh : 0,
    spend: mean,
    total: hh ? total / hh : 0,
    burden: inc > 0 ? total / inc : 0,
    highBurdenShare: hh ? high / hh : 0,
  }
}

export function computeTown(id: string, data: EnergyData, s: Scenario, local: boolean): TownResult {
  const town = data.towns[id]
  const elecPrice = electricPrice(data, s, town.utility)
  const threshold = data.assumptions.burden_threshold
  const parts: Part[] = []
  const byAmiParts: Part[][] = data.amiBrackets.map(() => [])
  const heatFuelHouseholds = data.heatFuels.map(() => 0)
  for (const cell of town.cells) {
    const ps = cellParts(cell, data, s, elecPrice, local)
    parts.push(...ps)
    byAmiParts[cell[0]].push(...ps)
    heatFuelHouseholds[cell[1]] += cell[2]
  }
  return {
    id,
    ...summarize(parts, threshold),
    byAmi: byAmiParts.map((p) => summarize(p, threshold)),
    heatFuelHouseholds,
    elecPrice,
  }
}

/** Household-weighted aggregate of several towns (county or state). */
export function aggregate(results: TownResult[]): Group {
  let hh = 0
  let inc = 0
  let high = 0
  const spend = emptySpend()
  for (const r of results) {
    hh += r.households
    inc += r.households * r.income
    high += r.households * r.highBurdenShare
    for (const k of Object.keys(spend) as SpendKey[]) spend[k] += r.households * r.spend[k]
  }
  const total = Object.values(spend).reduce((a, b) => a + b, 0)
  const mean = emptySpend()
  for (const k of Object.keys(spend) as SpendKey[]) mean[k] = hh ? spend[k] / hh : 0
  return {
    households: hh,
    income: hh ? inc / hh : 0,
    spend: mean,
    total: hh ? total / hh : 0,
    burden: inc > 0 ? total / inc : 0,
    highBurdenShare: hh ? high / hh : 0,
  }
}

export function computeAll(data: EnergyData, s: Scenario, selectedId: string | null): Record<string, TownResult> {
  const out: Record<string, TownResult> = {}
  for (const id of Object.keys(data.towns)) {
    out[id] = computeTown(id, data, s, s.localScope === 'all' || id === selectedId)
  }
  return out
}

/** Price changes that move 2022 baseline prices to the latest EIA prices. */
export function currentPriceChange(data: EnergyData): FuelPrices {
  const { baseline, current } = data.prices
  return {
    elec: current.elec / baseline.elec - 1,
    gas: current.gas / baseline.gas - 1,
    oil: current.oil / baseline.oil - 1,
    propane: current.propane / baseline.propane - 1,
  }
}

/** 1-based rank of each town by burden, highest burden = 1. */
export function burdenRanks(results: Record<string, TownResult>, ids?: string[]): Record<string, number> {
  const list = (ids ?? Object.keys(results)).slice().sort((a, b) => results[b].burden - results[a].burden)
  return Object.fromEntries(list.map((id, i) => [id, i + 1]))
}
