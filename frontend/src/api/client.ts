import type { ApiErrorBody, Bootstrap } from './types'

const API_BASE = '/api/v1'

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly field?: string
  readonly requestId?: string

  constructor(status: number, body?: Partial<ApiErrorBody>) {
    super(body?.error?.message || `请求失败（${status}）`)
    this.name = 'ApiError'
    this.status = status
    this.code = body?.error?.code || 'HTTP_ERROR'
    this.field = body?.error?.field
    this.requestId = body?.request_id
  }
}

let csrfToken = ''
let bootstrapPromise: Promise<Bootstrap> | null = null

function queryString(query?: Record<string, unknown>) {
  if (!query) return ''
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === '') continue
    if (Array.isArray(value)) value.forEach((item) => params.append(key, String(item)))
    else params.set(key, String(value))
  }
  const result = params.toString()
  return result ? `?${result}` : ''
}

async function parseError(response: Response): Promise<never> {
  let body: Partial<ApiErrorBody> | undefined
  try { body = await response.json() as ApiErrorBody } catch { /* non-JSON proxy error */ }
  throw new ApiError(response.status, body)
}

async function rawRequest<T>(path: string, init: RequestInit = {}, query?: Record<string, unknown>): Promise<T> {
  const method = (init.method || 'GET').toUpperCase()
  const unsafe = !['GET', 'HEAD', 'OPTIONS'].includes(method)
  if (unsafe && !csrfToken) await getBootstrap()

  const headers = new Headers(init.headers)
  if (init.body && !(init.body instanceof FormData)) headers.set('Content-Type', 'application/json')
  if (unsafe) headers.set('X-CSRF-Token', csrfToken)
  headers.set('Accept', 'application/json')

  const response = await fetch(`${API_BASE}${path}${queryString(query)}`, {
    ...init,
    headers,
    credentials: 'same-origin',
  })
  if (!response.ok) return parseError(response)
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export function getBootstrap(force = false): Promise<Bootstrap> {
  if (!bootstrapPromise || force) {
    bootstrapPromise = rawRequest<Bootstrap>('/bootstrap').then((value) => {
      csrfToken = value.csrf_token
      return value
    }).catch((error) => {
      bootstrapPromise = null
      throw error
    })
  }
  return bootstrapPromise
}

export const api = {
  get: <T>(path: string, query?: Record<string, unknown>, options?: Pick<RequestInit, 'signal'>) => rawRequest<T>(path, options || {}, query),
  post: <T>(path: string, body?: unknown) => rawRequest<T>(path, { method: 'POST', body: body instanceof FormData ? body : body === undefined ? undefined : JSON.stringify(body) }),
  put: <T>(path: string, body?: unknown) => rawRequest<T>(path, { method: 'PUT', body: body === undefined ? undefined : JSON.stringify(body) }),
  patch: <T>(path: string, body?: unknown) => rawRequest<T>(path, { method: 'PATCH', body: body === undefined ? undefined : JSON.stringify(body) }),
  delete: <T>(path: string, body?: unknown) => rawRequest<T>(path, { method: 'DELETE', body: body === undefined ? undefined : JSON.stringify(body) }),
}

export function apiErrorMessage(error: unknown) {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error) return error.message
  return '发生了未知错误，请稍后重试。'
}

export function taskStreamUrl() {
  return `${API_BASE}/stream`
}
