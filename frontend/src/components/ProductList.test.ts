import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ProductList from './ProductList.vue'

const apiMocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
}))

vi.mock('@/api/client', () => ({
  api: {
    ...apiMocks,
    patch: vi.fn(),
    delete: vi.fn(),
  },
  apiErrorMessage: (error: unknown) => error instanceof Error ? error.message : 'request failed',
}))

beforeEach(() => {
  vi.clearAllMocks()
  setActivePinia(createPinia())
  apiMocks.get.mockImplementation((path: string) => {
    if (path === '/product-categories') {
      return Promise.resolve({ items: [], page: 1, page_size: 200, total: 0 })
    }
    if (path === '/products') {
      return Promise.resolve({
        items: [{
          id: 'product-1', site_id: 'site-1', site_name: '示例站点', name: '测试商品',
          image_url: 'https://example.test/image.png', price: 12, original_price: 15,
          currency: 'CNY', availability_status: 'available', updated_at: '2026-08-04T00:00:00Z',
        }],
        page: 1,
        page_size: 20,
        total: 1,
      })
    }
    throw new Error(`unexpected GET ${path}`)
  })
})

describe('ProductList', () => {
  it('uses a text-first product row without a nonfunctional thumbnail slot', async () => {
    const wrapper = mount(ProductList, {
      global: {
        plugins: [createPinia()],
        stubs: { RouterLink: true },
      },
    })
    await flushPromises()

    expect(wrapper.get('.product-title').text()).toContain('测试商品')
    expect(wrapper.get('.product-title').text()).toContain('原价')
    expect(wrapper.find('.product-thumb').exists()).toBe(false)
    expect(wrapper.find('img').exists()).toBe(false)
  })
  it('toggles attention without clearing existing per-product thresholds', async () => {
    const original = apiMocks.get.getMockImplementation()!
    apiMocks.get.mockImplementation((path: string, ...args: unknown[]) => path.endsWith('/watch-rule')
      ? Promise.resolve({ watched: false, price_drop_amount: 12, price_drop_percent: null }) : original(path, ...args))
    apiMocks.put.mockResolvedValue({ watched: true, price_drop_amount: 12, price_drop_percent: null })
    const wrapper = mount(ProductList, { global: { plugins: [createPinia()], stubs: { RouterLink: true } } }); await flushPromises()
    await wrapper.get('button[aria-label="测试商品 重点关注"]').trigger('click'); await flushPromises()
    expect(apiMocks.put).toHaveBeenCalledWith('/products/product-1/watch-rule', { watched: true, price_drop_amount: 12, price_drop_percent: null })
    expect(wrapper.get('button[aria-label="测试商品 重点关注"]').attributes('aria-pressed')).toBe('true')
    wrapper.unmount()
  })

})
