// Color scales. Sequential = one hue (blue) light->dark for burden magnitude;
// diverging = blue (burden falls) <-> gray <-> red (burden rises) for change.
// Fuel colors are the categorical slots 1-5 in fixed order.

const BLUE = ['#cde2fb', '#9ec5f4', '#6da7ec', '#3987e5', '#256abf', '#184f95', '#0d366b']
const BLUE_DARK = ['#1a3050', '#184f95', '#1c5cab', '#2a78d6', '#5598e7', '#86b6ef', '#cde2fb']
const DIV_LIGHT = ['#184f95', '#3987e5', '#9ec5f4', '#f0efec', '#f2b8b2', '#e66767', '#b02e2e']
const DIV_DARK = ['#86b6ef', '#3987e5', '#1c4a80', '#383835', '#7a2c2a', '#d04f4f', '#f2a09b']

export const FUEL_COLORS: Record<string, { light: string; dark: string; label: string }> = {
  elec: { light: '#2a78d6', dark: '#3987e5', label: 'Electricity' },
  gas: { light: '#eb6834', dark: '#d95926', label: 'Natural gas' },
  oil: { light: '#1baf7a', dark: '#199e70', label: 'Heating oil' },
  propane: { light: '#eda100', dark: '#c98500', label: 'Propane' },
  other: { light: '#e87ba4', dark: '#d55181', label: 'Wood & other' },
}

export function isDark(): boolean {
  return typeof window !== 'undefined' && window.matchMedia?.('(prefers-color-scheme: dark)').matches
}

export function fuelColor(key: string, dark = isDark()): string {
  const c = FUEL_COLORS[key]
  return dark ? c.dark : c.light
}

export interface Scale {
  color: (v: number) => string
  steps: string[]
  domain: [number, number]
}

/** Quantized sequential scale over [lo, hi]. */
export function sequential(lo: number, hi: number, dark = isDark()): Scale {
  const steps = dark ? BLUE_DARK : BLUE
  return {
    steps,
    domain: [lo, hi],
    color: (v) => {
      const t = Math.min(0.9999, Math.max(0, (v - lo) / (hi - lo || 1)))
      return steps[Math.floor(t * steps.length)]
    },
  }
}

/** Quantized diverging scale symmetric around zero over [-m, m]. */
export function diverging(m: number, dark = isDark()): Scale {
  const steps = dark ? DIV_DARK : DIV_LIGHT
  const max = m || 1
  return {
    steps,
    domain: [-max, max],
    color: (v) => {
      // Values within the middle step's band read as "no change".
      const t = Math.min(0.9999, Math.max(0, (v + max) / (2 * max)))
      return steps[Math.floor(t * steps.length)]
    },
  }
}
