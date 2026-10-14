export const pct = (v: number, digits = 1) => `${(v * 100).toFixed(digits)}%`

export const signedPct = (v: number, digits = 1) => {
  const s = (v * 100).toFixed(digits)
  return Number(s) > 0 ? `+${s}%` : Number(s) === 0 ? `${(0).toFixed(digits)}%` : `${s.replace('-', '−')}%`
}

/** Percentage-point change, e.g. +0.4 pts. */
export const signedPts = (v: number, digits = 2) => {
  const s = (v * 100).toFixed(digits)
  return Number(s) > 0 ? `+${s} pts` : Number(s) === 0 ? `${(0).toFixed(digits)} pts` : `${s.replace('-', '−')} pts`
}

const usd = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 })
export const money = (v: number) => usd.format(v).replace('-', '−')
export const signedMoney = (v: number) => (Math.round(v) > 0 ? `+${money(v)}` : money(v))

export const count = (v: number) => Math.round(v).toLocaleString('en-US')
