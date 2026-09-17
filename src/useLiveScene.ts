import { useCallback, useEffect, useRef, useState } from 'react'
import { postJSON } from './api'
import type { BatteryState } from './types'

/** 二进制首字节：与 backend/app.py 的 LIVE_* 常量一致。 */
const LIVE_JSON = 1
const LIVE_MAP = 2
const LIVE_CLOUD = 3

export interface Pose {
  x: number
  y: number
  yaw: number
  z?: number
  frame_id?: string
  speed?: number | null
  stamp?: number
}

export interface CloudFrame {
  points: Float32Array
  count: number
  frameId: string
  stamp: number
  receivedAt: number
  tookMs: number | null
}

export interface BridgeStatus {
  ros?: boolean
  streaming?: boolean
  pose_hz?: number
  cloud?: boolean
  battery?: boolean
  frame_id?: string
  stamp?: number
}

export type LiveConnection = 'connecting' | 'open' | 'closed' | 'unauthorized'

export interface LiveInfo {
  simulated: boolean
  stage: number
  localized: boolean
  map: string
}

interface DecodedCloud {
  header: { name?: string; frame_id?: string; stamp?: number; count?: number; took_ms?: number | null }
  points: Float32Array
}

function decodeCloud(payload: ArrayBuffer, offset: number): DecodedCloud | null {
  const view = new DataView(payload)
  if (payload.byteLength < offset + 4) return null
  const headerSize = view.getUint32(offset, true)
  const headerStart = offset + 4
  const bodyStart = headerStart + headerSize
  if (payload.byteLength < bodyStart) return null
  let header: DecodedCloud['header'] = {}
  try {
    header = JSON.parse(new TextDecoder().decode(new Uint8Array(payload, headerStart, headerSize)))
  } catch {
    return null
  }
  // The blob is float32 xyz; slicing keeps the buffer alive without copying twice.
  const points = new Float32Array(payload.slice(bodyStart))
  return { header, points }
}

/**
 * 车端实时数据通道。一个 WebSocket 同时承载位姿/电量/航点（JSON）
 * 和地图/雷达点云（float32 二进制），订阅期间后端才唤醒 ros_bridge。
 */
export function useLiveScene(mapName: string, routeName: string) {
  const [connection, setConnection] = useState<LiveConnection>('connecting')
  const [error, setError] = useState('')
  const [pose, setPose] = useState<Pose | null>(null)
  const [map, setMap] = useState<DecodedCloud | null>(null)
  const [cloud, setCloud] = useState<CloudFrame | null>(null)
  const [waypoints, setWaypoints] = useState<Float32Array | null>(null)
  const [trace, setTrace] = useState<Float32Array | null>(null)
  const [battery, setBattery] = useState<BatteryState | null>(null)
  const [status, setStatus] = useState<BridgeStatus | null>(null)
  const [info, setInfo] = useState<LiveInfo | null>(null)
  const [stats, setStats] = useState({ cloudHz: 0, poseHz: 0, ageMs: 0, tookMs: 0 })
  const [attempt, setAttempt] = useState(0)

  const socket = useRef<WebSocket | null>(null)
  const wantedMap = useRef(mapName)
  const mounted = useRef(true)
  const cloudTimes = useRef<number[]>([])

  const send = useCallback((payload: Record<string, unknown>) => {
    const current = socket.current
    if (current && current.readyState === WebSocket.OPEN) {
      current.send(JSON.stringify(payload))
      return true
    }
    return false
  }, [])

  const requestMap = useCallback((name: string) => {
    wantedMap.current = name
    if (!name) { setMap(null); return }
    if (!send({ cmd: 'map', map: name })) setMap(null)
  }, [send])

  const setCloudEnabled = useCallback((enabled: boolean) => {
    send({ cmd: 'cloud', enabled })
    if (!enabled) setCloud(null)
  }, [send])

  useEffect(() => {
    if (mapName && mapName !== wantedMap.current) requestMap(mapName)
  }, [mapName, requestMap])

  useEffect(() => {
    if (!routeName) { setWaypoints(null); setTrace(null); return }
    let active = true
    fetch(`/api/route?file=${encodeURIComponent(routeName)}`, { cache: 'no-store' })
      .then((response) => (response.ok ? response.json() : null))
      .then((value) => {
        if (!active || !value) return
        const flat = new Float32Array(value.points.length * 2)
        value.points.forEach((point: { x: number; y: number }, index: number) => {
          flat[index * 2] = point.x
          flat[index * 2 + 1] = point.y
        })
        setWaypoints(flat)
      })
      .catch(() => undefined)
    return () => { active = false }
  }, [routeName])

  useEffect(() => {
    mounted.current = true
    let closed = false
    let timer: number | undefined
    let retry = 0

    const connect = async () => {
      setConnection('connecting')
      setError('')
      let ticket = ''
      try {
        const credentials = await postJSON<{ ticket: string }>('/api/live-ticket', {})
        ticket = credentials.ticket
      } catch (failure) {
        if (closed || !mounted.current) return
        const typed = failure as Error & { status?: number }
        if (typed.status === 401) {
          setConnection('unauthorized')
          setError('控制令牌无效，请先解锁控制')
          return
        }
        setConnection('closed')
        setError(typed.message)
        timer = window.setTimeout(connect, 3000)
        return
      }
      if (closed || !mounted.current) return
      const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      const current = new WebSocket(`${scheme}//${window.location.host}/api/live?ticket=${encodeURIComponent(ticket)}`)
      current.binaryType = 'arraybuffer'
      socket.current = current
      current.onopen = () => {
        if (!mounted.current) return
        retry = 0
        setConnection('open')
        setError('')
        if (wantedMap.current) current.send(JSON.stringify({ cmd: 'map', map: wantedMap.current }))
      }
      current.onmessage = (event) => {
        if (typeof event.data === 'string') return
        const buffer = event.data as ArrayBuffer
        const kind = new Uint8Array(buffer, 0, 1)[0]
        const body = buffer.slice(1)
        if (kind === LIVE_JSON) {
          let message: Record<string, unknown> = {}
          try { message = JSON.parse(new TextDecoder().decode(body)) } catch { return }
          const type = message.type as string
          if (type === 'pose') {
            const next = message as unknown as Pose
            setPose(next)
            setStats((old) => ({ ...old, poseHz: 0 }))
          } else if (type === 'battery') {
            const { type: _ignored, ...rest } = message
            setBattery(rest as unknown as BatteryState)
          } else if (type === 'waypoints') {
            const list = message.points as number[] | undefined
            if (list) setWaypoints(new Float32Array(list))
          } else if (type === 'trace') {
            const list = message.points as number[] | undefined
            setTrace(list ? new Float32Array(list) : null)
          } else if (type === 'bridge') {
            setStatus(message as unknown as BridgeStatus)
          } else if (type === 'bridge_error') {
            setError(String(message.message || '实时数据异常'))
          } else if (type === 'welcome') {
            setInfo({
              simulated: Boolean(message.simulated), stage: Number(message.current_stage || 0),
              localized: Boolean(message.localized), map: String(message.map || ''),
            })
            if (message.battery) setBattery(message.battery as BatteryState)
          }
          return
        }
        if (kind === LIVE_MAP || kind === LIVE_CLOUD) {
          const decoded = decodeCloud(buffer, 1)
          if (!decoded) return
          if (kind === LIVE_MAP) {
            setMap(decoded as DecodedCloud)
          } else {
            const now = performance.now()
            cloudTimes.current.push(now)
            cloudTimes.current = cloudTimes.current.filter((value) => now - value < 3000)
            setCloud({
              points: decoded.points,
              count: decoded.header.count || decoded.points.length / 3,
              frameId: decoded.header.frame_id || 'map',
              stamp: decoded.header.stamp || 0,
              receivedAt: now,
              tookMs: decoded.header.took_ms ?? null,
            })
            setStats((old) => ({
              ...old,
              cloudHz: cloudTimes.current.length / 3,
              ageMs: decoded.header.stamp ? Math.max(0, Date.now() / 1000 - decoded.header.stamp) * 1000 : 0,
              tookMs: decoded.header.took_ms ?? old.tookMs,
            }))
          }
        }
      }
      current.onclose = () => {
        if (!mounted.current || closed) return
        socket.current = null
        setConnection('closed')
        retry = Math.min(retry + 1, 6)
        timer = window.setTimeout(connect, 800 * retry)
      }
      current.onerror = () => {
        if (mounted.current) setError('实时通道连接失败')
      }
    }

    void connect()
    return () => {
      closed = true
      mounted.current = false
      if (timer !== undefined) window.clearTimeout(timer)
      socket.current?.close()
      socket.current = null
    }
  }, [attempt])

  const reconnect = useCallback(() => setAttempt((value) => value + 1), [])

  return {
    connection, error, pose, map, cloud, waypoints, trace, battery,
    status: status as BridgeStatus | null,
    info: info as LiveInfo | null,
    stats, requestMap, setCloudEnabled, reconnect,
  }
}
