import { beforeEach, describe, expect, it, vi } from 'vitest'

describe('API client', () => {
  beforeEach(() => vi.resetModules())

  it('bootstraps CSRF before a mutating request', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrf_token: 'token-123', api_version: 'v1', features: {}, worker: {} }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ id: 'site-1' }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    vi.stubGlobal('fetch', fetchMock)
    const { api } = await import('./client')

    await api.post('/sites', { name: 'Example' })

    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/bootstrap')
    const request = fetchMock.mock.calls[1]?.[1] as RequestInit
    expect(new Headers(request.headers).get('X-CSRF-Token')).toBe('token-123')
    expect(new Headers(request.headers).get('Content-Type')).toBe('application/json')
  })

  it('surfaces the structured API error message', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: { code: 'INVALID', message: '网址不合法', field: 'origin' }, request_id: 'req-1' }), { status: 422, headers: { 'Content-Type': 'application/json' } })))
    const { api, ApiError } = await import('./client')
    await expect(api.get('/sites')).rejects.toMatchObject({ name: 'ApiError', message: '网址不合法', status: 422, code: 'INVALID', field: 'origin', requestId: 'req-1' } satisfies Partial<InstanceType<typeof ApiError>>)
  })
})
