import { Expand, Map as MapIcon, MonitorUp, RotateCcw, ZoomIn, ZoomOut } from 'lucide-react'
import { memo, useEffect, useMemo, useRef, useState } from 'react'
import type { RouteData } from '../types'

interface Props {
  route: RouteData | null
  connected: boolean
  rvizRunning: boolean
  localizationReady: boolean
  onLaunchRviz: () => void
}

function RouteGraphic({ route, zoom }: { route: RouteData | null; zoom: number }) {
  const drawing = useMemo(() => {
    if (!route || route.points.length < 2) return null
    const width = Math.max(route.max_x - route.min_x, 1)
    const height = Math.max(route.max_y - route.min_y, 1)
    const padding = 44
    const vw = 960
    const vh = 560
    const scale = Math.min((vw - padding * 2) / width, (vh - padding * 2) / height) * zoom
    const cx = (route.min_x + route.max_x) / 2
    const cy = (route.min_y + route.max_y) / 2
    const path = route.points.map((point, index) => {
      const x = vw / 2 + (point.x - cx) * scale
      const y = vh / 2 - (point.y - cy) * scale
      return `${index === 0 ? 'M' : 'L'}${x.toFixed(1)} ${y.toFixed(1)}`
    }).join(' ')
    const start = route.points[0]
    const end = route.points[route.points.length - 1]
    const project = (point: typeof start) => ({
      x: vw / 2 + (point.x - cx) * scale,
      y: vh / 2 - (point.y - cy) * scale,
    })
    return { path, start: project(start), end: project(end) }
  }, [route, zoom])

  if (!drawing) {
    return (
      <div className="map-empty">
        <MapIcon aria-hidden="true" />
        <strong>请选择路径文件</strong>
        <span>路径加载后将在此显示真实 CSV 航迹</span>
      </div>
    )
  }

  return (
    <svg className="route-svg" viewBox="0 0 960 560" role="img" aria-label={`${route?.name || ''} 路径预览`}>
      <defs>
        <pattern id="minorGrid" width="24" height="24" patternUnits="userSpaceOnUse">
          <path d="M 24 0 L 0 0 0 24" fill="none" stroke="#26364a" strokeWidth="1" />
        </pattern>
        <pattern id="grid" width="120" height="120" patternUnits="userSpaceOnUse">
          <rect width="120" height="120" fill="url(#minorGrid)" />
          <path d="M 120 0 L 0 0 0 120" fill="none" stroke="#36506d" strokeWidth="1.2" />
        </pattern>
        <filter id="routeGlow" x="-20%" y="-20%" width="140%" height="140%">
          <feGaussianBlur stdDeviation="3" result="blur" />
          <feMerge><feMergeNode in="blur" /><feMergeNode in="SourceGraphic" /></feMerge>
        </filter>
      </defs>
      <rect width="960" height="560" fill="#0d1826" />
      <rect width="960" height="560" fill="url(#grid)" />
      <path d={drawing.path} fill="none" stroke="#22d3ee" strokeOpacity=".22" strokeWidth="9" />
      <path d={drawing.path} fill="none" stroke="#46e691" strokeWidth="3" strokeDasharray="10 7" filter="url(#routeGlow)" />
      <circle cx={drawing.start.x} cy={drawing.start.y} r="7" fill="#34d399" stroke="#d1fae5" strokeWidth="3" />
      <circle cx={drawing.end.x} cy={drawing.end.y} r="7" fill="#f59e0b" stroke="#fef3c7" strokeWidth="3" />
    </svg>
  )
}

export const RouteMap = memo(function RouteMap({ route, connected, rvizRunning, localizationReady, onLaunchRviz }: Props) {
  const [view, setView] = useState<'map' | 'rviz'>('map')
  const [zoom, setZoom] = useState(1)
  const autoOpenedRviz = useRef(false)

  useEffect(() => {
    if (rvizRunning && !autoOpenedRviz.current) {
      setView('rviz')
      autoOpenedRviz.current = true
    }
    if (!rvizRunning) autoOpenedRviz.current = false
  }, [rvizRunning])

  return (
    <section className="panel map-panel" aria-labelledby="map-title">
      <header className="panel-title map-titlebar">
        <h2 id="map-title">三维地图视图（RViz）</h2>
        <div className="map-tools">
          <div className="segmented" aria-label="视图切换">
            <button className={view === 'map' ? 'active' : ''} aria-pressed={view === 'map'} onClick={() => setView('map')}><MapIcon aria-hidden="true" />地图视图</button>
            <button className={view === 'rviz' ? 'active' : ''} aria-pressed={view === 'rviz'} onClick={() => setView('rviz')}>RViz</button>
          </div>
          <button className="icon-text" aria-label="放大地图" onClick={() => setZoom((value) => Math.min(value + .15, 1.8))}><ZoomIn aria-hidden="true" />放大</button>
          <button className="icon-text" aria-label="缩小地图" onClick={() => setZoom((value) => Math.max(value - .15, .55))}><ZoomOut aria-hidden="true" />缩小</button>
          <button className="icon-button" aria-label="重置地图缩放" onClick={() => setZoom(1)}><RotateCcw aria-hidden="true" /></button>
          <button className="icon-button" aria-label="浏览器全屏" onClick={() => document.documentElement.requestFullscreen?.()}><Expand aria-hidden="true" /></button>
        </div>
      </header>
      <div className="map-stage">
        {view === 'map' ? <RouteGraphic route={route} zoom={zoom} /> : (
          <div className="rviz-stage">
            <img src="/api/rviz.mjpeg" alt="车载桌面和 RViz 实时画面" />
            <div className="rviz-help">
              <MonitorUp aria-hidden="true" />
              <span>{!connected ? '车端未连接' : localizationReady ? '定位已获得实时数据，可以继续加载路径' : rvizRunning ? '请在 RViz 选择 2D Pose Estimate，在地图上拖动设置车辆位置与朝向' : '打开 RViz 后，在地图上人工设置车辆初始位置与朝向'}</span>
              <button onClick={onLaunchRviz}>{rvizRunning ? '重新打开' : '打开 RViz'}</button>
            </div>
          </div>
        )}
        <div className="map-legend">
          <span><i className="start" />起点</span>
          <span><i className="route" />录制路径</span>
          <span><i className="end" />终点</span>
        </div>
        <div className="map-readout">
          <strong>路径长度 <span>{route ? `${route.length_m.toFixed(1)} m` : '—'}</span></strong>
          <strong>航点数量 <span>{route?.points.length ?? '—'}</span></strong>
          <strong>系统连接 <span className={connected ? 'online' : ''}>{connected ? '正常' : '离线'}</span></strong>
        </div>
      </div>
    </section>
  )
})
