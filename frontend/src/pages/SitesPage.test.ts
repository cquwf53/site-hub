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
const folders = [
  { id: '00000000-0000-0000-0000-000000000001', parent_id: null, name: 'Sites', sort_order: -1 },
  { id: 'folder-guides', parent_id: null, name: '资料', sort_order: 0 },
  { id: 'folder-tools', parent_id: null, name: '工具箱', sort_order: 1 },
]
let statsFailure: Error | null = null
type DirectoryStats = { total: number; unfiled: number; by_folder: Record<string, number> }
const statsQueue: Array<Promise<DirectoryStats>> = []

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((onResolve, onReject) => {
    resolve = onResolve
    reject = onReject
  })
  return { promise, resolve, reject }
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
  statsFailure = null
  statsQueue.length = 0
  apiMocks.get.mockImplementation((path: string) => {
    if (path === '/sites') return Promise.resolve({ items: [{ ...site }], page: 1, page_size: 20, total: 1 })
    if (path === '/sites/stats') {
      if (statsQueue.length) return statsQueue.shift()!
      return statsFailure
        ? Promise.reject(statsFailure)
        : Promise.resolve({ total: 77, unfiled: 5, by_folder: { 'folder-guides': 42, 'folder-tools': 30 } })
    }
    if (path === '/folders') return Promise.resolve(folders.map((item) => ({ ...item })))
    if (path === '/tags') return Promise.resolve([])
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
  it('uses unfiltered server statistics for directory counts instead of the current page', async () => {
    const { wrapper } = await renderPage()
    const rows = wrapper.findAll('.explorer-panel .folder-row')
    const row = (label: string) => rows.find((item) => item.text().includes(label))

    expect(row('全部网站')?.text()).toContain('77')
    expect(row('未归档')?.text()).toContain('5')
    expect(row('资料')?.text()).toContain('42')
    expect(row('工具箱')?.text()).toContain('30')
    expect(row('Sites')).toBeUndefined()
    expect(wrapper.find('option[value="00000000-0000-0000-0000-000000000001"]').exists()).toBe(false)
    expect(apiMocks.get).toHaveBeenCalledWith('/sites/stats')
  })

  it('keeps the website list usable and identifies a directory-statistics failure', async () => {
    statsFailure = new Error('统计接口暂不可用')
    const { wrapper } = await renderPage()

    expect(wrapper.get('.explorer-stats-state[role="alert"]').text()).toContain('目录计数加载失败：统计接口暂不可用')
    expect(wrapper.text()).toContain('示例站')
    expect(wrapper.find('.error-state').exists()).toBe(false)
    expect(wrapper.findAll('.folder-row em')).toHaveLength(0)
  })

  it('ignores an older directory-statistics failure after a newer request succeeds', async () => {
    const { wrapper, router } = await renderPage()
    const older = deferred<DirectoryStats>()
    const newer = deferred<DirectoryStats>()
    statsQueue.push(older.promise, newer.promise)

    await router.replace({ query: { q: 'older' } })
    await flushPromises()
    await router.replace({ query: { q: 'newer' } })
    await flushPromises()

    newer.resolve({ total: 88, unfiled: 6, by_folder: { 'folder-guides': 50 } })
    await flushPromises()
    expect(wrapper.findAll('.folder-row')[0]?.text()).toContain('88')

    older.reject(new Error('stale request failed'))
    await flushPromises()
    expect(wrapper.findAll('.folder-row')[0]?.text()).toContain('88')
    expect(wrapper.find('.explorer-stats-state[role="alert"]').exists()).toBe(false)
  })

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
  it('reports partial bulk failures and retries only failed websites', async () => {
    const original = apiMocks.get.getMockImplementation()!
    apiMocks.get.mockImplementation((path: string, ...args: unknown[]) => path === '/sites'
      ? Promise.resolve({ items: [{ ...site }, { ...site, id: 'site-2', name: '失败站点' }], page: 1, page_size: 20, total: 2 })
      : original(path, ...args))
    apiMocks.patch.mockImplementation((path: string) => path.endsWith('site-2') ? Promise.reject(new Error('目标目录不可用')) : Promise.resolve(site))
    const { wrapper } = await renderPage()
    await wrapper.get('input[aria-label="选择本页全部网站"]').setValue(true)
    await wrapper.get('select[aria-label="批量移动目标目录"]').setValue('unfiled')
    await wrapper.findAll('button').find(button => button.text() === '批量移动')!.trigger('click'); await flushPromises()
    const report = wrapper.get('[aria-label="批量操作结果"]')
    expect(report.text()).toContain('成功 1 项，失败 1 项')
    expect(report.text()).toContain('失败站点：目标目录不可用')
    apiMocks.patch.mockResolvedValue(site)
    await report.findAll('button').find(button => button.text() === '仅重试失败项')!.trigger('click'); await flushPromises()
    expect(apiMocks.patch.mock.calls.filter(([path]) => path === '/sites/site-1')).toHaveLength(1)
    expect(apiMocks.patch.mock.calls.filter(([path]) => path === '/sites/site-2')).toHaveLength(2)
    wrapper.unmount()
  })

})
