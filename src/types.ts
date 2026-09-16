export type HealthState = 'ok' | 'warn' | 'error' | 'idle' | 'running'

export interface ModuleState {
  key: string
  label: string
  state: HealthState
  detail: string
}

export interface WorkflowStep {
  id: number
  title: string
  description: string
  state: 'done' | 'current' | 'waiting' | 'blocked' | 'running'
  detail: string
  action_label: string
}

export interface ConsoleFile {
  name: string
  size: number
  modified: number
}

export interface LogEntry {
  time: string
  level: 'INFO' | 'WARN' | 'ERROR'
  module: string
  message: string
}

export interface Telemetry {
  cpu_percent: number | null
  memory_percent: number | null
  temperature_c: number | null
  can0: string
  node_count: number
  topic_count: number
}

export interface ConsoleState {
  version: string
  simulated: boolean
  connected: boolean
  container: string
  current_stage: number
  busy: string | null
  emergency: boolean
  rviz_running: boolean
  live_topics: string[]
  modules: ModuleState[]
  workflow: WorkflowStep[]
  telemetry: Telemetry
  maps: ConsoleFile[]
  routes: ConsoleFile[]
  logs: LogEntry[]
  timestamp: number
  last_error: string | null
}

export interface RoutePoint {
  x: number
  y: number
  z: number
  yaw: number
  velocity: number
}

export interface RouteData {
  name: string
  points: RoutePoint[]
  length_m: number
  min_x: number
  max_x: number
  min_y: number
  max_y: number
}
