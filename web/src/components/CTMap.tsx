import { geoMercator, geoPath } from 'd3-geo'
import type { Feature, FeatureCollection, GeoJsonProperties, Geometry } from 'geojson'
import { useMemo, useRef, useState } from 'react'
import type { Scale } from '../colors'

export type CountyProps = {
  county: string
  name: string
}
export type TownProps = {
  id: string
  name: string
  county: string
}

interface Props {
  counties: FeatureCollection<Geometry, CountyProps>
  towns: FeatureCollection<Geometry, TownProps>
  countyValues: Record<string, number>
  townValues: Record<string, number>
  scale: Scale
  format: (v: number) => string
  selectedCounty: string | null
  selectedTown: string | null
  onSelectCounty: (county: string | null) => void
  onSelectTown: (id: string) => void
}

const W = 960
const H = 560
const PAD = 24

interface Tip {
  x: number
  y: number
  title: string
  value: string
}

export function CTMap(p: Props) {
  const wrap = useRef<HTMLDivElement>(null)
  const [tip, setTip] = useState<Tip | null>(null)

  const { path, countyShapes, townShapes } = useMemo(() => {
    const projection = geoMercator().fitExtent(
      [
        [PAD, PAD],
        [W - PAD, H - PAD],
      ],
      p.counties,
    )
    const path = geoPath(projection)
    const shape = <T extends GeoJsonProperties>(f: Feature<Geometry, T>) => ({ f, d: path(f) ?? '', c: path.centroid(f) })
    return {
      path,
      countyShapes: p.counties.features.map(shape),
      townShapes: p.towns.features.map(shape),
    }
  }, [p.counties, p.towns])

  // Zoom: fit the selected county's projected bounds into the view.
  const zoom = useMemo(() => {
    const sel = p.selectedCounty && p.counties.features.find((f) => f.properties.county === p.selectedCounty)
    if (!sel) return { x: 0, y: 0, k: 1 }
    const [[x0, y0], [x1, y1]] = path.bounds(sel)
    const k = Math.min((W - 2 * PAD) / (x1 - x0), (H - 2 * PAD) / (y1 - y0))
    return { x: W / 2 - (k * (x0 + x1)) / 2, y: H / 2 - (k * (y0 + y1)) / 2, k }
  }, [p.selectedCounty, p.counties, path])

  const showTip = (e: React.MouseEvent, title: string, value: string) => {
    const r = wrap.current!.getBoundingClientRect()
    setTip({ x: e.clientX - r.left, y: e.clientY - r.top, title, value })
  }

  const activate = (fn: () => void) => ({
    onClick: fn,
    onKeyDown: (e: React.KeyboardEvent) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault()
        fn()
      }
    },
  })

  const countyTowns = townShapes.filter((t) => t.f.properties.county === p.selectedCounty)
  const label = 11 / zoom.k

  return (
    <div className="map-wrap" ref={wrap} onMouseLeave={() => setTip(null)}>
      <svg viewBox={`0 0 ${W} ${H}`} className="map" role="group" aria-label="Map of Connecticut energy burden">
        <rect width={W} height={H} className="map-bg" onClick={() => p.onSelectCounty(null)} />
        <g className="zoom" style={{ transform: `translate(${zoom.x}px, ${zoom.y}px) scale(${zoom.k})` }}>
          {countyShapes.map(({ f, d }) => {
            const id = f.properties.county
            const isSel = id === p.selectedCounty
            const muted = p.selectedCounty !== null && !isSel
            const v = p.countyValues[id]
            return (
              <path
                key={id}
                d={d}
                className={`county${muted ? ' muted' : ''}${isSel ? ' selected' : ''}`}
                fill={isSel ? 'none' : muted ? undefined : p.scale.color(v)}
                tabIndex={isSel ? -1 : 0}
                role="button"
                aria-label={`${f.properties.name} County, ${p.format(v)}`}
                onMouseMove={(e) => showTip(e, `${f.properties.name} County`, p.format(v))}
                {...activate(() => p.onSelectCounty(id))}
              />
            )
          })}
          {countyTowns.map(({ f, d }) => {
            const id = f.properties.id
            const v = p.townValues[id]
            return (
              <path
                key={id}
                d={d}
                className={`town${id === p.selectedTown ? ' selected' : ''}`}
                fill={p.scale.color(v)}
                tabIndex={0}
                role="button"
                aria-label={`${f.properties.name}, ${p.format(v)}`}
                aria-pressed={id === p.selectedTown}
                onMouseMove={(e) => showTip(e, f.properties.name, p.format(v))}
                {...activate(() => p.onSelectTown(id))}
              />
            )
          })}
          {countyShapes
            .filter(({ f }) => f.properties.county === p.selectedCounty)
            .map(({ f, d }) => (
              <path key={`o${f.properties.county}`} d={d} className="county-outline" />
            ))}
          {p.selectedCounty === null &&
            countyShapes.map(({ f, c }) => (
              <text key={`l${f.properties.county}`} x={c[0]} y={c[1]} className="map-label county-label">
                {f.properties.name}
              </text>
            ))}
          {countyTowns.map(({ f, c }) => (
            <text
              key={`l${f.properties.id}`}
              x={c[0]}
              y={c[1]}
              className={`map-label town-label${f.properties.id === p.selectedTown ? ' strong' : ''}`}
              style={{ fontSize: label }}
            >
              {f.properties.name}
            </text>
          ))}
        </g>
      </svg>
      {tip && (
        <div className="tooltip" style={{ left: tip.x + 14, top: tip.y + 14 }}>
          <div className="tooltip-title">{tip.title}</div>
          <div>{tip.value}</div>
        </div>
      )}
    </div>
  )
}
