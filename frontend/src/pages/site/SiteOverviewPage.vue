<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import AsyncState from '@/components/AsyncState.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { api, apiErrorMessage } from '@/api/client'
import type { InboxEvent, Job, Page, Product, SiteAccount, TaskRef } from '@/api/types'
import { useAppStore } from '@/stores/app'
import { useTaskStore } from '@/stores/tasks'
import { formatDate, formatMoney } from '@/utils/format'
import { useSiteDetail } from './siteContext'

const { site } = useSiteDetail()
const app = useAppStore()
const tasks = useTaskStore()
const loading = ref(true)
const error = ref('')
const accounts = ref<SiteAccount[]>([])
const products = ref<Page<Product>>({ items: [], page: 1, page_size: 4, total: 0 })
const events = ref<Page<InboxEvent>>({ items: [], page: 1, page_size: 5, total: 0 })
const jobs = ref<Page<Job>>({ items: [], page: 1, page_size: 3, total: 0 })
const readyAccounts = computed(() => accounts.value.filter((account) => account.auth_state === 'ready').length)

async function load() {
  if (!site.value) return
  loading.value = true; error.value = ''
  try {
    ;[accounts.value, products.value, events.value, jobs.value] = await Promise.all([
      api.get<SiteAccount[]>(`/sites/${site.value.id}/accounts`),
      api.get<Page<Product>>('/products', { site_id: site.value.id, page: 1, page_size: 4 }),
      api.get<Page<InboxEvent>>('/events', { site_id: site.value.id, page: 1, page_size: 5 }),
      api.get<Page<Job>>('/jobs', { site_id: site.value.id, page: 1, page_size: 3 }),
    ])
  } catch (value) { error.value = apiErrorMessage(value) }
  finally { loading.value = false }
}
async function sync() {
  if (!site.value) return
  try { const task = await api.post<TaskRef>(`/sites/${site.value.id}/sync`); tasks.track(task, `同步 ${site.value.name}`); app.taskDrawerOpen = true; app.toast('同步任务已创建', 'success') }
  catch (value) { app.toast(apiErrorMessage(value), 'error') }
}
onMounted(load)
</script>

<template>
  <AsyncState :loading="loading" :error="error" @retry="load">
    <div class="row-between section-lead"><div><h2>网站概览</h2><p>关键能力与最近变化</p></div><button class="button" @click="sync">立即同步</button></div>
    <div class="grid three summary-grid">
      <article class="card metric-card"><div class="metric-label">商品</div><div class="metric-value">{{ products.total }}</div><div class="metric-note">当前追踪商品</div></article>
      <article class="card metric-card"><div class="metric-label">签到账号</div><div class="metric-value">{{ readyAccounts }}<small> / {{ accounts.length }}</small></div><div class="metric-note">登录状态正常</div></article>
      <article class="card metric-card"><div class="metric-label">消息</div><div class="metric-value">{{ events.total }}</div><div class="metric-note">与此网站相关</div></article>
    </div>
    <div class="grid two overview-grid">
      <section class="card"><div class="card-header"><h2>签到账号</h2><RouterLink class="text-button" :to="`/sites/${site?.id}/accounts`">管理</RouterLink></div><div v-if="!accounts.length" class="empty-compact">尚未添加签到账号。</div><div v-else class="simple-list"><div v-for="account in accounts" :key="account.id" class="simple-row"><div><strong>{{ account.label }}</strong><p>{{ formatDate(account.last_verified_at) }}</p></div><StatusBadge :status="account.auth_state" /></div></div></section>
      <section class="card"><div class="card-header"><h2>最近任务</h2><RouterLink class="text-button" to="/automations/jobs">查看全部</RouterLink></div><div v-if="!jobs.items.length" class="empty-compact">暂无运行任务。</div><div v-else class="simple-list"><div v-for="job in jobs.items" :key="job.id" class="simple-row"><div><strong>{{ job.kind || '后台任务' }}</strong><p>{{ job.message || formatDate(job.updated_at) }}</p></div><StatusBadge :status="job.status" /></div></div></section>
    </div>
    <section class="card"><div class="card-header"><h2>最近商品</h2><RouterLink class="text-button" :to="`/sites/${site?.id}/products`">查看商品</RouterLink></div><div v-if="!products.items.length" class="empty-compact">同步后商品会出现在这里。</div><div v-else class="simple-list"><div v-for="product in products.items" :key="product.id" class="simple-row"><div><strong>{{ product.name }}</strong><p>{{ product.availability_reason || product.availability_status }}</p></div><b>{{ formatMoney(product.price, product.currency) }}</b></div></div></section>
  </AsyncState>
</template>

<style scoped>
.section-lead { margin-bottom: 14px; }.section-lead h2 { margin-bottom: 3px; }.section-lead p { margin: 0; font-size: 12px; }.summary-grid, .overview-grid { margin-bottom: 16px; }.metric-value small { color: var(--muted); font-size: 15px; }.simple-list { display: flex; flex-direction: column; }.simple-row { display: flex; min-height: 62px; align-items: center; justify-content: space-between; gap: 15px; padding: 11px 18px; border-bottom: 1px solid var(--line); }.simple-row:last-child { border: 0; }.simple-row strong { font-size: 13px; }.simple-row p { margin: 3px 0 0; font-size: 11px; }
</style>
