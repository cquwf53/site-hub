import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import SitesPage from './SitesPage.vue'

const apiMocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
  delete: vi.fn(),
}))

vi.mock('@/api/client', () => ({
  api: apiMocks,
  apiErrorMessage: (error: unknown) => error instanceof Error ? error.message : 'request failed',
}))

const siteTypes = [
  { id: 'type-other', name: '其他', slug: 'other', color: '#6b7280', icon: 'globe', sort_order: 0, enabled: true, is_system: true, is_default: true, site_count: 1 },
  { id: 'type-tool', name: '工具', slug: 'tool', color: '#2563eb', icon: 'wrench', sort_order: 10, enabled: true, is_system: true, is_default: false, site_count: 0 },
]
const site = {
  id: 'site-1', folder_id: null, site_type_id: 'type-other', site_type: siteTypes[0], name: '示例站',
  url: 'https://example.com', status: 'active', pinned: false, tags: [], capabilities: ['bookmark'],
}

async function renderPage() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/sites', component: SitesPage },
      { path: '/sites/:siteId/overview', component: { template: '<div />' } },
    ],
  })
  await router.push('/sites')
  await router.isReady()
  const wrapper = mount(SitesPage, {
    global: { plugins: [createPinia(), router], stubs: { Teleport: true } },
  })
  await flushPromises()
  return { wrapper, router }
}

beforeEach(() => {
  vi.clearAllMocks()
  setActivePinia(createPinia())
  apiMocks.get.mockImplementation((path: string) => {
    if (path === '/sites') return Promise.resolve({ items: [{ ...site }], page: 1, page_size: 20, total: 1 })
    if (path === '/folders' || path === '/tags') return Promise.resolve([])
    if (path === '/site-types') return Promise.resolve(siteTypes.map((item) => ({ ...item })))
    throw new Error(`unexpected GET ${path}`)
  })
  apiMocks.post.mockImplementation((path: string, payload: unknown) => {
    if (path === '/sites/bulk/site-type') return Promise.resolve({ updated: 1, site_type_id: 'type-tool', site_ids: ['site-1'] })
    return Promise.resolve({ ...site, ...(payload as object) })
  })
  apiMocks.patch.mockResolvedValue({ ...site })
})

describe('SitesPage website types', () => {
  it('displays the primary type and passes the type filter to the list API', async () => {
    const { wrapper, router } = await renderPage()

    expect(wrapper.text()).toContain('其他')
    expect(wrapper.text()).toContain('◎')
    expect(wrapper.text()).not.toContain('globe')

    await wrapper.get('select[aria-label="网站类型筛选"]').setValue('type-tool')
    await flushPromises()

    expect(router.currentRoute.value.query.type).toBe('type-tool')
    expect(apiMocks.get).toHaveBeenCalledWith('/sites', expect.objectContaining({ site_type_id: 'type-tool' }))
  })

  it('creates a website with its selected primary type', async () => {
    const { wrapper } = await renderPage()
    const addButton = wrapper.findAll('button').find((button) => button.text().includes('添加网站'))
    await addButton!.trigger('click')
    await wrapper.get('#site-name').setValue('工具站')
    await wrapper.get('#site-url').setValue('https://tools.example.com')
    await wrapper.get('#site-type').setValue('type-tool')
    await wrapper.get('#site-create-form').trigger('submit')
    await flushPromises()

    expect(apiMocks.post).toHaveBeenCalledWith('/sites', {
      name: '工具站',
      url: 'https://tools.example.com',
      folder_id: null,
      site_type_id: 'type-tool',
      tag_ids: [],
      pinned: false,
      capabilities: ['bookmark'],
    })
  })

  it('uses the atomic bulk endpoint when assigning a type', async () => {
    const { wrapper } = await renderPage()
    await wrapper.get('input[aria-label="选择 示例站"]').setValue(true)
    await wrapper.get('select[aria-label="批量设置网站类型"]').setValue('type-tool')
    const assignButton = wrapper.findAll('button').find((button) => button.text() === '设置类型')
    await assignButton!.trigger('click')
    await flushPromises()

    expect(apiMocks.post).toHaveBeenCalledWith('/sites/bulk/site-type', {
      site_ids: ['site-1'],
      site_type_id: 'type-tool',
    })
  })
})
