import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import ProductPriceChart from './ProductPriceChart.vue'

describe('ProductPriceChart', () => {
  it('sorts and plots finite price observations', () => {
    const wrapper = mount(ProductPriceChart, {
      props: {
        observations: [
          { id: '3', product_id: 'p', price: 8, stock: 1, availability_status: 'available', observed_at: '2026-08-03T00:00:00Z' },
          { id: '1', product_id: 'p', price: 10, stock: 1, availability_status: 'available', observed_at: '2026-08-01T00:00:00Z' },
          { id: '2', product_id: 'p', price: 9, stock: 1, availability_status: 'available', observed_at: '2026-08-02T00:00:00Z' },
        ],
      },
    })

    const chart = wrapper.get('svg')
    expect(chart.attributes('aria-label')).toContain('3')
    expect(wrapper.get('polyline').attributes('points')?.split(' ')).toHaveLength(3)
    expect(wrapper.text()).toContain('10.00')
    expect(wrapper.text()).toContain('8.00')
  })

  it('positions irregular observations using their actual time distance', () => {
    const wrapper = mount(ProductPriceChart, {
      props: {
        observations: [
          { id: '1', product_id: 'p', price: 10, stock: 1, availability_status: 'available', observed_at: '2026-08-01T00:00:00Z' },
          { id: '2', product_id: 'p', price: 9, stock: 1, availability_status: 'available', observed_at: '2026-08-02T00:00:00Z' },
          { id: '3', product_id: 'p', price: 8, stock: 1, availability_status: 'available', observed_at: '2026-08-11T00:00:00Z' },
        ],
      },
    })

    const serializedPoints = wrapper.get('polyline').attributes('points')
    expect(serializedPoints).toBeDefined()
    const xValues = serializedPoints!
      .split(' ')
      .map((point) => Number(point.split(',')[0]))
    expect(xValues[0]).toBeCloseTo(56, 1)
    expect(xValues[2]).toBeCloseTo(702, 1)
    expect((xValues[1] - xValues[0]) / (xValues[2] - xValues[0])).toBeCloseTo(0.1, 2)
  })

  it('shows an explicit empty state when no valid prices exist', () => {
    const wrapper = mount(ProductPriceChart, {
      props: {
        observations: [
          { id: '1', product_id: 'p', price: null, stock: null, availability_status: 'unknown', observed_at: '2026-08-01T00:00:00Z' },
        ],
      },
    })
    expect(wrapper.find('svg').exists()).toBe(false)
    expect(wrapper.text()).toContain('暂无有效价格记录')
  })
})
