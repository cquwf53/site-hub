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
        adapter: 'test-scraper', config: { selector: '.from-site' },
      }),
      refresh: contextMocks.refresh,
    }),
  }
})

const typeBase = { color: '#6976d9', icon: 'globe', sort_order: 0, enabled: true, is_system: true, is_default: false, site_count: 0 }
const adapterManifest = {
  id: 'test-scraper', name: 'Test scraper', version: '1.0.0', capabilities: ['product_monitor'], secret_fields: [],
  site_config_schema: { type: 'object', properties: { selector: { type: 'string', title: '商品选择器' } } },
}
let getFailures: Record<string, Error> = {}

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
  getFailures = {}
  apiMocks.get.mockImplementation((path: string) => {
    if (getFailures[path]) return Promise.reject(getFailures[path])
    if (path === '/folders' || path === '/tags') return Promise.resolve([])
    if (path === '/adapters') return Promise.resolve([adapterManifest])
    if (path === '/site-types') return Promise.resolve([
      { ...typeBase, id: 'type-other', name: '其他', slug: 'other', is_default: true },
      { ...typeBase, id: 'type-tool', name: '工具', slug: 'tool' },
      { ...typeBase, id: 'type-disabled', name: '旧分类', slug: 'disabled', enabled: false, is_system: false },
    ])
    if (path === '/sites/site-1/product-settings') return Promise.resolve({
      enabled: false,
      interval_override_minutes: null,
      scraper_type: 'test-scraper',
      config: { selector: '.from-product' },
    })
    throw new Error(`unexpected GET ${path}`)
  })
  apiMocks.patch.mockResolvedValue({})
  contextMocks.refresh.mockResolvedValue(undefined)
})

describe('SiteSettingsPage website type', () => {
  it('shows a scoped error when one basic-information option source fails', async () => {
    getFailures['/folders'] = new Error('目录服务暂不可用')
    const wrapper = await renderPage()
    const basicSection = wrapper.get('.basic-settings-section')

    expect(basicSection.get('[role="alert"]').text()).toContain('目录：目录服务暂不可用')
    expect(basicSection.get('#settings-folder').attributes()).toHaveProperty('disabled')
    expect(basicSection.get('#settings-site-type').attributes()).not.toHaveProperty('disabled')
    expect(wrapper.find('.adapter-settings-section #site-adapter').exists()).toBe(true)
    expect(wrapper.find('.product-settings-section #sync-interval').exists()).toBe(true)
  })

  it('does not disguise an adapter-catalog failure as no supported capability', async () => {
    getFailures['/adapters'] = new Error('适配器目录暂不可用')
    const wrapper = await renderPage()
    const adapterSection = wrapper.get('.adapter-settings-section')

    expect(adapterSection.get('[role="alert"]').text()).toContain('适配器目录暂不可用')
    expect(adapterSection.text()).toContain('加载失败')
    expect(wrapper.find('.product-settings-section #sync-interval').exists()).toBe(true)
    expect(wrapper.find('#settings-name').exists()).toBe(true)
  })

  it('does not show default product settings when the product request fails', async () => {
    getFailures['/sites/site-1/product-settings'] = new Error('商品设置读取失败')
    const wrapper = await renderPage()
    const productSection = wrapper.get('.product-settings-section')

    expect(productSection.get('[role="alert"]').text()).toContain('商品设置读取失败')
    expect(productSection.find('#sync-interval').exists()).toBe(false)
    expect(wrapper.find('.adapter-settings-section #site-adapter').exists()).toBe(true)
  })

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

  it('loads reactive site and product adapter configs without a DataCloneError', async () => {
    const wrapper = await renderPage()

    expect((wrapper.get('#schema-selector').element as HTMLInputElement).value).toBe('.from-product')
    expect(apiMocks.get).toHaveBeenCalledWith('/sites/site-1/product-settings')
  })
})
