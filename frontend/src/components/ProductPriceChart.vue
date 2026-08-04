<script setup lang="ts">
import { computed } from 'vue'
import type { PriceObservation } from '@/api/types'
import { formatDate, formatMoney } from '@/utils/format'

const props = withDefaults(defineProps<{
  observations: PriceObservation[]
  currency?: string
}>(), { currency: 'CNY' })

const width = 720
const height = 230
const inset = { left: 56, right: 18, top: 18, bottom: 34 }
type PlottableObservation = PriceObservation & { price: number; observedAtMs: number }

const values = computed<PlottableObservation[]>(() => props.observations
  .flatMap((item) => {
    const observedAtMs = new Date(item.observed_at).getTime()
    if (typeof item.price !== 'number' || !Number.isFinite(item.price) || !Number.isFinite(observedAtMs)) return []
    return [{ ...item, price: item.price, observedAtMs }]
  })
  .sort((left, right) => left.observedAtMs - right.observedAtMs))
const minimum = computed(() => values.value.length ? Math.min(...values.value.map((item) => item.price)) : 0)
const maximum = computed(() => values.value.length ? Math.max(...values.value.map((item) => item.price)) : 0)
const points = computed(() => {
  const items = values.value
  if (!items.length) return ''
  const drawableWidth = width - inset.left - inset.right
  const drawableHeight = height - inset.top - inset.bottom
  const span = maximum.value - minimum.value
  const firstObservedAt = items[0].observedAtMs
  const observedAtSpan = items[items.length - 1].observedAtMs - firstObservedAt
  return items.map((item) => {
    const x = inset.left + (observedAtSpan === 0
      ? drawableWidth / 2
      : (item.observedAtMs - firstObservedAt) / observedAtSpan * drawableWidth)
    const y = inset.top + (span === 0 ? drawableHeight / 2 : (maximum.value - item.price) / span * drawableHeight)
    return `${x.toFixed(1)},${y.toFixed(1)}`
  }).join(' ')
})
</script>

<template>
  <div v-if="values.length" class="price-chart">
    <svg :viewBox="`0 0 ${width} ${height}`" role="img" :aria-label="`价格历史，共 ${values.length} 个有效价格点`">
      <line :x1="inset.left" :x2="width - inset.right" :y1="inset.top" :y2="inset.top" class="grid-line" />
      <line :x1="inset.left" :x2="width - inset.right" :y1="height - inset.bottom" :y2="height - inset.bottom" class="grid-line" />
      <text x="4" :y="inset.top + 4" class="axis-label">{{ formatMoney(maximum, currency) }}</text>
      <text x="4" :y="height - inset.bottom + 4" class="axis-label">{{ formatMoney(minimum, currency) }}</text>
      <polyline :points="points" class="price-line" />
      <circle v-if="values.length === 1" :cx="width / 2" :cy="height / 2 - 8" r="4" class="price-dot" />
      <text :x="inset.left" :y="height - 9" class="date-label">{{ formatDate(values[0]?.observed_at, false) }}</text>
      <text :x="width - inset.right" :y="height - 9" text-anchor="end" class="date-label">{{ formatDate(values.at(-1)?.observed_at, false) }}</text>
    </svg>
  </div>
  <div v-else class="chart-empty">暂无有效价格记录。完成商品同步后会在这里生成曲线。</div>
</template>

<style scoped>
.price-chart { overflow: hidden; border: 1px solid var(--line); border-radius: 10px; background: linear-gradient(180deg, #fafaff, #fff); }
.price-chart svg { display: block; width: 100%; min-height: 190px; }
.grid-line { stroke: #e8e8ef; stroke-width: 1; }
.price-line { fill: none; stroke: var(--accent); stroke-linecap: round; stroke-linejoin: round; stroke-width: 3; vector-effect: non-scaling-stroke; }
.price-dot { fill: var(--accent); }
.axis-label, .date-label { fill: var(--subtle); font-family: 'Segoe UI', sans-serif; font-size: 10px; }
.chart-empty { display: grid; min-height: 190px; place-items: center; padding: 20px; border: 1px dashed var(--line-strong); border-radius: 10px; color: var(--subtle); font-size: 12px; text-align: center; }
</style>
