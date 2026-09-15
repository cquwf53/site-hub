import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import SiteAccountsPage from './SiteAccountsPage.vue'

const apiMocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
  delete: vi.fn(),
}))
const taskMocks = vi.hoisted(() => ({ track: vi.fn() }))

vi.mock('@/api/client', () => ({
  api: apiMocks,
  apiErrorMessage: (error: unknown) => error instanceof Error ? error.message : 'request failed',
}))

vi.mock('@/stores/tasks', () => ({
  useTaskStore: () => taskMocks,
}))

vi.mock('./siteContext', async () => {
  const { ref } = await import('vue')
  return {
    useSiteDetail: () => ({
      site: ref({ id: 'site-1', name: 'Example', url: 'https://example.test' }),
    }),
  }
})

beforeEach(() => {
  vi.clearAllMocks()
  setActivePinia(createPinia())
  vi.stubGlobal('EventSource', class {
    onopen = null
    onmessage = null
    onerror = null
    addEventListener() {}
    close() {}
  })
  apiMocks.get.mockImplementation((path: string) => {
    if (path === '/sites/site-1/accounts') return Promise.resolve([])
    if (path === '/adapters') {
      return Promise.resolve([{
        id: 'classic',
        name: 'VShop Classic',
        version: '1.0.0',
        capabilities: ['product_monitor'],
        secret_fields: [],
      }])
    }
    throw new Error(`unexpected GET ${path}`)
  })
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('SiteAccountsPage', () => {
  it('does not fall back to a product adapter when no check-in adapter is available', async () => {
    const wrapper = mount(SiteAccountsPage, {
      global: { plugins: [createPinia()], stubs: { Teleport: true } },
    })
    await flushPromises()

    const add = wrapper.findAll('button').find((button) => button.text().includes('添加账号'))
    expect(add).toBeDefined()
    await add!.trigger('click')

    const select = wrapper.get('#account-adapter')
    expect((select.element as HTMLSelectElement).value).toBe('')
    expect(wrapper.text()).toContain('签到 Worker 尚未提供可用适配器')
    expect(wrapper.get('[form="account-editor"]').attributes('disabled')).toBeDefined()

    await wrapper.get('#account-editor').trigger('submit')
    await flushPromises()
    expect(apiMocks.post).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('polls Profile verification through every active job state and reloads on completion', async () => {
    const verificationCallbacks: Array<() => void> = []
    vi.spyOn(window, 'setTimeout').mockImplementation(((handler: TimerHandler, delay?: number) => {
      if (delay === 2000) verificationCallbacks.push(handler as () => void)
      return 1
    }) as typeof window.setTimeout)
    let terminal = false
    const statuses = ['queued', 'leased', 'running', 'succeeded']
    const observedStatuses: string[] = []
    apiMocks.get.mockImplementation((path: string) => {
      if (path === '/sites/site-1/accounts') return Promise.resolve([{
        id: 'account-1', site_id: 'site-1', label: 'Primary', adapter: 'nodeloc',
        enabled: false, auth_state: terminal ? 'ready' : 'unverified', configured: true,
        configured_secret_fields: [], config: {}, profile: {
          relative_path: 'account-1', state: terminal ? 'ready' : 'unverified', file_count: 12,
          total_bytes: 1024, last_verified_at: terminal ? '2026-08-03T12:00:00Z' : null, verification_error: null,
        },
      }])
      if (path === '/adapters') return Promise.resolve([{
        id: 'nodeloc', name: 'NodeLoc', version: '1.0.0',
        capabilities: ['checkin'], secret_fields: [],
      }])
      if (path === '/jobs/job-1') {
        const status = statuses.shift() || 'succeeded'
        observedStatuses.push(status)
        if (status === 'succeeded') terminal = true
        return Promise.resolve({ id: 'job-1', kind: 'login_session', status })
      }
      throw new Error(`unexpected GET ${path}`)
    })
    apiMocks.post.mockResolvedValue({
      jobId: 'job-1', kind: 'login_session', status: 'queued',
      deduplicated: false, href: '/api/v1/jobs/job-1',
    })
    const wrapper = mount(SiteAccountsPage, {
      global: { plugins: [createPinia()], stubs: { Teleport: true } },
    })
    await flushPromises()

    expect(wrapper.get('button.switch').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('请先验证 Profile 再启用')
    await wrapper.get('button.verify-profile').trigger('click')
    await flushPromises()

    expect(apiMocks.post).toHaveBeenCalledWith('/accounts/account-1/login-session/verify')
    expect(apiMocks.post).not.toHaveBeenCalledWith('/accounts/account-1/run')
    expect(observedStatuses).toEqual(['queued'])
    expect(wrapper.get('button.verify-profile').text()).toContain('验证中')

    for (const status of ['leased', 'running']) {
      verificationCallbacks.shift()?.()
      await flushPromises()
      expect(wrapper.get('button.verify-profile').text()).toContain('验证中')
      expect(observedStatuses.at(-1)).toBe(status)
    }

    verificationCallbacks.shift()?.()
    await flushPromises()
    expect(wrapper.get('button.verify-profile').text()).toContain('验证 Profile')
    expect(wrapper.get('button.switch').attributes('disabled')).toBeUndefined()
    expect(wrapper.get('.profile-summary').text()).toContain('已登录')
    expect(apiMocks.get.mock.calls.filter(([path]) => path === '/jobs/job-1')).toHaveLength(4)
    wrapper.unmount()
  })

  it('does not require Profile verification for API accounts', async () => {
    apiMocks.get.mockImplementation((path: string) => {
      if (path === '/sites/site-1/accounts') return Promise.resolve([{
        id: 'account-api', site_id: 'site-1', label: 'API account', adapter: 'api',
        enabled: true, auth_state: 'unverified', configured: true,
        configured_secret_fields: ['headers.Authorization'], config: {}, profile: {
          relative_path: 'account-api', state: 'unverified', file_count: 0,
          total_bytes: 0, last_verified_at: null, verification_error: null,
        },
      }])
      if (path === '/adapters') return Promise.resolve([{
        id: 'api', name: 'HTTP API', version: '1.0.0',
        capabilities: ['checkin'], secret_fields: ['headers.Authorization'],
      }])
      throw new Error(`unexpected GET ${path}`)
    })
    apiMocks.post.mockResolvedValue({
      jobId: 'checkin-job', kind: 'checkin', status: 'queued',
      deduplicated: false, href: '/api/v1/jobs/checkin-job',
    })
    const wrapper = mount(SiteAccountsPage, {
      global: { plugins: [createPinia()], stubs: { Teleport: true } },
    })
    await flushPromises()

    expect(wrapper.find('button.verify-profile').exists()).toBe(false)
    expect(wrapper.text()).toContain('无需 Profile')
    expect(wrapper.get('button.run-now').attributes('disabled')).toBeUndefined()
    await wrapper.get('button.run-now').trigger('click')
    await flushPromises()
    expect(apiMocks.post).toHaveBeenCalledWith('/accounts/account-api/run')
    wrapper.unmount()
  })
  it('edits reactive account config and creates from adapter defaults without losing nested values', async () => {
    apiMocks.get.mockImplementation((path: string) => Promise.resolve(path === '/adapters' ? [{
      id: 'api', name: 'API', version: '1', capabilities: ['checkin'], secret_fields: [],
      defaults: { headers: { Accept: 'application/json' } }, account_config_schema: { type: 'object' },
    }] : [{ id: 'account-1', label: 'Nested', adapter: 'api', enabled: true, auth_state: 'ready', configured_secret_fields: [], config: { headers: { Accept: 'text/html' } } }]))
    const wrapper = mount(SiteAccountsPage, { global: { plugins: [createPinia()], stubs: { Teleport: true } } })
    await flushPromises()
    const edit = wrapper.findAll('button').find(button => button.text() === '配置')
    await edit!.trigger('click'); await flushPromises()
    await wrapper.get('#account-editor').trigger('submit'); await flushPromises()
    expect(apiMocks.patch).toHaveBeenCalledWith('/accounts/account-1', expect.objectContaining({ config: { headers: { Accept: 'text/html' } } }))
    await wrapper.findAll('button').find(button => button.text().includes('添加账号'))!.trigger('click')
    await wrapper.get('#account-editor').trigger('submit'); await flushPromises()
    expect(apiMocks.post).toHaveBeenCalledWith('/sites/site-1/accounts', expect.objectContaining({ config: { headers: { Accept: 'application/json' } } }))
    wrapper.unmount()
  })

})
