<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import PageHeader from '@/components/PageHeader.vue'
import AsyncState from '@/components/AsyncState.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { api, apiErrorMessage } from '@/api/client'
import type { InboxEvent, Job, Page, Product, Site } from '@/api/types'
import { formatDate, formatMoney } from '@/utils/format'

const loading = ref(true)
const error = ref('')
const sites = ref<Page<Site>>({ items: [], page: 1, page_size: 1, total: 0 })
const products = ref<Page<Product>>({ items: [], page: 1, page_size: 1, total: 0 })
const events = ref<Page<InboxEvent>>({ items: [], page: 1, page_size: 6, total: 0 })
const jobs = ref<Page<Job>>({ items: [], page: 1, page_size: 6, total: 0 })
const activeJobs = computed(() => jobs.value.items.filter((job) => ['queued', 'leased', 'running'].includes(job.status)))

async function load() {
  loading.value = true; error.value = ''
  try {
    ;[sites.value, products.value, events.value, jobs.value] = await Promise.all([
      api.get<Page<Site>>('/sites', { page: 1, page_size: 5 }),
      api.get<Page<Product>>('/products', { page: 1, page_size: 5 }),
      api.get<Page<InboxEvent>>('/events', { page: 1, page_size: 6, dismissed: false }),
      api.get<Page<Job>>('/jobs', { page: 1, page_size: 6 }),
    ])
  } catch (value) { error.value = apiErrorMessage(value) }
  finally { loading.value = false }
}
onMounted(load)
</script>

<template>
  <PageHeader title="早上好" description="网站、商品与自动化运行状态都在这里。">
    <template #actions><RouterLink class="button secondary" to="/sites">管理网站</RouterLink><RouterLink class="button" to="/automations/jobs">查看自动化</RouterLink></template>
  </PageHeader>
  <AsyncState :loading="loading" :error="error" @retry="load">
    <div class="grid four overview-metrics">
      <article class="card metric-card"><div class="metric-label">网站</div><div class="metric-value">{{ sites.total }}</div><div class="metric-note">统一收藏与能力配置</div></article>
      <article class="card metric-card"><div class="metric-label">追踪商品</div><div class="metric-value">{{ products.total }}</div><div class="metric-note">跨站商品目录</div></article>
      <article class="card metric-card"><div class="metric-label">待处理消息</div><div class="metric-value">{{ events.total }}</div><div class="metric-note">价格、库存与运行异常</div></article>
      <article class="card metric-card"><div class="metric-label">进行中任务</div><div class="metric-value">{{ activeJobs.length }}</div><div class="metric-note">排队与运行中的自动化</div></article>
    </div>
    <div class="grid two overview-sections">
      <section class="card">
        <div class="card-header"><div><h2>需要留意</h2><p class="subtle-text">最近未归档的消息</p></div><RouterLink class="text-button" to="/inbox">全部消息</RouterLink></div>
        <div v-if="!events.items.length" class="empty-compact">一切正常，没有待处理消息。</div>
        <div v-else class="feed-list">
          <RouterLink v-for="event in events.items" :key="event.id" :to="`/inbox?event=${event.id}`" class="feed-item">
            <span class="event-mark" :class="event.severity">{{ event.severity === 'error' ? '!' : event.severity === 'warning' ? '△' : '•' }}</span>
            <div><strong>{{ event.title || event.type }}</strong><p>{{ event.message || '查看事件详情' }}</p></div><time>{{ formatDate(event.occurred_at) }}</time>
          </RouterLink>
        </div>
      </section>
      <section class="card">
        <div class="card-header"><div><h2>自动化</h2><p class="subtle-text">最近调度与运行状态</p></div><RouterLink class="text-button" to="/automations/jobs">任务中心</RouterLink></div>
        <div v-if="!jobs.items.length" class="empty-compact">暂无自动化任务。</div>
        <div v-else class="feed-list">
          <div v-for="job in jobs.items" :key="job.id" class="feed-item job-feed">
            <div><strong>{{ job.site_name || job.kind || '自动化任务' }}</strong><p>{{ job.account_label || job.message || '后台任务' }}</p></div><StatusBadge :status="job.status" />
          </div>
        </div>
      </section>
    </div>
    <section class="card recent-products">
      <div class="card-header"><div><h2>最近更新商品</h2><p class="subtle-text">最新采集到的价格与库存</p></div><RouterLink class="text-button" to="/products">商品目录</RouterLink></div>
      <div v-if="!products.items.length" class="empty-compact">添加带商品监控能力的网站后，商品会显示在这里。</div>
      <div v-else class="compact-product-grid">
        <RouterLink v-for="product in products.items" :key="product.id" :to="`/sites/${product.site_id}/products`" class="compact-product">
          <span class="product-thumb"><img v-if="product.image_url" :src="product.image_url" alt="" /><span v-else>◇</span></span>
          <span><strong>{{ product.name }}</strong><small>{{ product.site_name || '网站商品' }}</small></span><b>{{ formatMoney(product.price, product.currency) }}</b>
        </RouterLink>
      </div>
    </section>
  </AsyncState>
</template>

<style scoped>
.overview-metrics { margin-bottom: 17px; }
.overview-sections { margin-bottom: 17px; }
.card-header p { margin-top: 3px; }
.feed-list { display: flex; flex-direction: column; }
.feed-item { display: grid; min-height: 69px; grid-template-columns: auto 1fr auto; align-items: center; gap: 12px; padding: 10px 18px; border-bottom: 1px solid var(--line); }
.feed-item:last-child { border-bottom: 0; }
.feed-item:hover { background: var(--surface-soft); }
.feed-item strong { display: block; overflow: hidden; max-width: 320px; font-size: 13px; text-overflow: ellipsis; white-space: nowrap; }
.feed-item p { overflow: hidden; max-width: 320px; margin: 3px 0 0; font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.feed-item time { color: var(--subtle); font-size: 11px; }
.event-mark { display: grid; width: 27px; height: 27px; place-items: center; border-radius: 8px; color: var(--accent); background: var(--accent-soft); font-weight: 700; }
.event-mark.error { color: var(--negative); background: var(--negative-soft); }.event-mark.warning { color: var(--warning); background: var(--warning-soft); }
.job-feed { grid-template-columns: 1fr auto; }
.compact-product-grid { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); }
.compact-product { display: flex; min-width: 0; flex-direction: column; gap: 10px; padding: 18px; border-right: 1px solid var(--line); }
.compact-product:last-child { border: 0; }.compact-product:hover { background: var(--surface-soft); }
.compact-product .product-thumb { width: 42px; height: 42px; }
.compact-product > span:nth-child(2) { display: flex; min-width: 0; flex-direction: column; gap: 3px; }
.compact-product strong, .compact-product small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.compact-product strong { font-size: 13px; }.compact-product small { color: var(--muted); }.compact-product b { font-size: 14px; }
@media (max-width: 1100px) { .compact-product-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }.compact-product { border-bottom: 1px solid var(--line); } }
@media (max-width: 620px) { .compact-product-grid { grid-template-columns: 1fr 1fr; }.feed-item time { display: none; } }
</style>
