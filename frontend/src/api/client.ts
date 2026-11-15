/** 统一的 API 客户端：口令、401 回门禁、错误归一化。 */

const TOKEN_KEY = 'lc-token'

let token = localStorage.getItem(TOKEN_KEY) || ''
let unauthorized: (() => void) | null = null

export function onUnauthorized(fn: () => void) {
  unauthorized = fn
}

export function getToken() {
  return token
}

export function setToken(value: string) {
  token = String(value || '').trim()
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

function buildHeaders(withJson: boolean): Record<string, string> {
  const h: Record<string, string> = {}
  if (withJson) h['Content-Type'] = 'application/json'
  if (token) h['X-Access-Token'] = token
  return h
}

/** 服务端返回 `{ok:false,error}` 时抛这个，页面统一 `catch (e) => say(e.message)`。 */
export class ApiFail extends Error {}

export async function api<T = any>(path: string, init: RequestInit = {}): Promise<T> {
  const withJson = init.body !== undefined && init.body !== null
  const res = await fetch(path, {
    ...init,
    headers: { ...buildHeaders(withJson), ...(init.headers as Record<string, string> | undefined) },
  })

  let payload: any = null
  try {
    payload = await res.json()
  } catch {
    payload = { ok: false, error: '返回不是 JSON' }
  }

  if (res.status === 401) {
    // 口令失效（配置页里改过），退回门禁重新验证
    setToken('')
    unauthorized?.()
    throw new ApiFail('需要访问口令')
  }
  if (payload && payload.ok === false && payload.error) {
    throw new ApiFail(payload.error)
  }
  return payload as T
}

export function postJson<T = any>(path: string, body: unknown): Promise<T> {
  return api<T>(path, { method: 'POST', body: JSON.stringify(body ?? {}) })
}

/** 导出走 fetch 拿 blob，才能带上口令头。 */
export async function download(path: string, filename: string) {
  const res = await fetch(path, { headers: buildHeaders(false) })
  if (!res.ok) throw new ApiFail('HTTP ' + res.status)
  const blob = await res.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  setTimeout(() => URL.revokeObjectURL(url), 2000)
}
