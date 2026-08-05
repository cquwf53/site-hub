import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import SiteSettingsPage from './SiteSettingsPage.vue'

const apiMocks = vi.hoisted(() => ({
  get: vi.fn(),
  put: vi.fn(),
  patch: vi.fn(),
  delete: vi.fn(),
}))
const contextMocks = vi.hoisted(() => ({ refresh: vi.fn() }))

vi.mock('@/api/client', () => ({
  api: apiMocks,
  apiErrorMessage: (error: unknown) => error instanceof Error ? error.message : 'request failed',
}))

vi.mock('./siteContext', async () => {
  const { ref } = await import('vue')
  return {
    useSiteDetail: () => ({
      site: ref({
        id: 'site-1', name: 'Example', url: 'https://example.test', folder_id: null, site_type_id: 'type-other',
        status: 'active', pinned: false, overview_hidden: true, tags: [], capabilities: ['bookmark'],
      }),
      refresh: contextMocks.refresh,
    }),
  }
})

const typeBase = { color: '#6976d9', icon: 'globe', sort_order: 0, enabled: true, is_system: true, is_default: false, site_count: 0 }

async function renderPage() {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: { template: '<div />' } }] })
  await router.push('/')
  await router.isReady()
  const wrapper = mount(SiteSettingsPage, {
    global: { plugins: [createPinia(), router], stubs: { Teleport: true } },
  })
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  vi.clearAllMocks()
  setActivePinia(createPinia())
  apiMocks.get.mockImplementation((path: string) => {
    if (path === '/folders' || path === '/tags' || path === '/adapters') return Promise.resolve([])
    if (path === '/site-types') return Promise.resolve([
      { ...typeBase, id: 'type-other', name: '其他', slug: 'other', is_default: true },
      { ...typeBase, id: 'type-tool', name: '工具', slug: 'tool' },
      { ...typeBase, id: 'type-disabled', name: '旧分类', slug: 'disabled', enabled: false, is_system: false },
    ])
    if (path === '/sites/site-1/product-settings') return Promise.resolve({ enabled: false, interval_override_minutes: null })
    throw new Error(`unexpected GET ${path}`)
  })
  apiMocks.patch.mockResolvedValue({})
  contextMocks.refresh.mockResolvedValue(undefined)
})

describe('SiteSettingsPage website type', () => {
  it('edits the primary type while excluding unrelated disabled types', async () => {
    const wrapper = await renderPage()
    const select = wrapper.get('#settings-site-type')

    expect((select.element as HTMLSelectElement).value).toBe('type-other')
    expect(select.text()).toContain('工具')
    expect(select.text()).not.toContain('旧分类')

    await select.setValue('type-tool')
    await wrapper.get('form.card-body').trigger('submit')
    await flushPromises()

    expect(apiMocks.patch).toHaveBeenCalledWith('/sites/site-1', expect.objectContaining({
      site_type_id: 'type-tool',
    }))
    expect(contextMocks.refresh).toHaveBeenCalled()
  })

  it('can restore a hidden website to overview quick access', async () => {
    const wrapper = await renderPage()
    const visibilitySwitch = wrapper.get('button[aria-label="在总览快捷访问中显示"]')

    expect(visibilitySwitch.attributes('aria-pressed')).toBe('false')
    await visibilitySwitch.trigger('click')
    expect(visibilitySwitch.attributes('aria-pressed')).toBe('true')
    await wrapper.get('form.card-body').trigger('submit')
    await flushPromises()

    expect(apiMocks.patch).toHaveBeenCalledWith('/sites/site-1', expect.objectContaining({
      overview_hidden: false,
    }))
  })
})
