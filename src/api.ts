const TOKEN_KEY = 'bigcar-control-token'

export function getToken() {
  return sessionStorage.getItem(TOKEN_KEY) || ''
}

export function setToken(token: string) {
  if (token) sessionStorage.setItem(TOKEN_KEY, token)
  else sessionStorage.removeItem(TOKEN_KEY)
}

async function decode<T>(response: Response): Promise<T> {
  const body = await response.json().catch(() => ({ error: `HTTP ${response.status}` }))
  if (!response.ok) {
    const error = new Error(body.error || body.message || `HTTP ${response.status}`)
    ;(error as Error & { status?: number }).status = response.status
    throw error
  }
  return body as T
}

export async function getJSON<T>(path: string): Promise<T> {
  return decode<T>(await fetch(path, { cache: 'no-store' }))
}

export async function postJSON<T>(path: string, body: unknown, authenticated = true): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (authenticated) headers['X-Control-Token'] = getToken()
  return decode<T>(await fetch(path, { method: 'POST', headers, body: JSON.stringify(body) }))
}
