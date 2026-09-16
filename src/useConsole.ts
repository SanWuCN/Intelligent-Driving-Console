import { useCallback, useEffect, useRef, useState } from 'react'
import { getJSON } from './api'
import type { ConsoleState } from './types'

export function useConsole() {
  const [state, setState] = useState<ConsoleState | null>(null)
  const [connectionError, setConnectionError] = useState(false)
  const mounted = useRef(true)

  const refresh = useCallback(async () => {
    try {
      const next = await getJSON<ConsoleState>('/api/state')
      if (mounted.current) {
        setState(next)
        setConnectionError(false)
      }
    } catch {
      if (mounted.current) setConnectionError(true)
    }
  }, [])

  useEffect(() => {
    mounted.current = true
    let timer: number | undefined
    let cancelled = false
    const poll = async () => {
      await refresh()
      if (!cancelled) timer = window.setTimeout(poll, 1800)
    }
    void poll()
    return () => {
      cancelled = true
      mounted.current = false
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [refresh])

  return { state, connectionError, refresh }
}
