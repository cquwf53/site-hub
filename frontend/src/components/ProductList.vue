<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import AsyncState from './AsyncState.vue'
import PaginationBar from './PaginationBar.vue'
import StatusBadge from './StatusBadge.vue'
import { api, apiErrorMessage } from '@/api/client'
import type { Page, Product } from '@/api/types'
import { formatDate, formatMoney, safeExternalUrl } from '@/utils/format'
import { useAppStore } from '@/stores/app'

const props = defineProps<{ siteId?: string; compact?: boolean }>()
const app = useAppStore()
const data = ref<Page<Product>>({ items: [], page: 1, page_size: 20, total: 0 })
const loading = ref(true); const error = ref(''); const search = ref(''); const availability = ref(''); const pageNo = ref(1)
let timer = 0
async function load() {
  loading.value = true; error.value = ''
  try { data.value = await api.get<Page<Product>>('/products', { site_id: props.siteId, q: search.value, availability_status: availability.value, page: pageNo.value, page_size: props.compact ? 10 : 20 }) }
  catch (value) { error.value = apiErrorMessage(value) }
  finally { loading.value = false }
}
function filter() { window.clearTimeout(timer); timer = window.setTimeout(() => { pageNo.value = 1; void load() }, 250) }
function changePage(value: number) { pageNo.value = value; void load() }
async function openProduct(product: Product) {
  try {
    if (product.buy_endpoint_id) {
      await api.post<void>(`/endpoints/${product.buy_endpoint_id}/open`)
      return
    }
    const safe = safeExternalUrl(product.url)
    if (safe) window.open(safe, '_blank', 'noopener,noreferrer')
  } catch (value) { app.toast(apiErrorMessage(value), 'error') }
}
watch(() => props.siteId, load)
onMounted(load)
defineExpose({ refresh: load })
</script>

<template>
  <div class="toolbar product-toolbar"><label class="search-field"><input v-model="search" placeholder="搜索商品名称" @input="filter" /></label><select v-model="availability" class="availability-select" @change="filter"><option value="">全部状态</option><option value="available">在售</option><option value="sold_out">售罄</option><option value="missing">抓取缺失</option><option value="delisted">明确下架</option><option value="unknown">未知</option></select></div>
  <section class="card"><AsyncState :loading="loading" :error="error" :empty="!data.items.length" empty-title="暂无商品" empty-text="开启商品追踪并执行同步后，商品会出现在这里。" @retry="load">
    <div class="table-wrap"><table class="data-table"><thead><tr><th>商品</th><th v-if="!siteId">网站</th><th>价格</th><th>库存</th><th>更新时间</th><th /></tr></thead><tbody>
      <tr v-for="product in data.items" :key="product.id"><td><div class="cell-title"><span class="product-thumb"><img v-if="product.image_url" :src="product.image_url" alt="" loading="lazy" /><span v-else>◇</span></span><span class="product-title"><b>{{ product.name }}</b><small v-if="product.original_price && product.original_price !== product.price">原价 {{ formatMoney(product.original_price, product.currency) }}</small></span></div></td><td v-if="!siteId"><RouterLink class="text-link" :to="`/sites/${product.site_id}/products`">{{ product.site_name || '查看网站' }}</RouterLink></td><td><strong>{{ formatMoney(product.price, product.currency) }}</strong></td><td><StatusBadge :status="product.availability_status" :label="product.availability_status === 'available' ? '在售' : product.availability_status === 'sold_out' ? '售罄' : product.availability_status === 'missing' ? '抓取缺失' : product.availability_status === 'delisted' ? '明确下架' : product.availability_status" /></td><td class="muted">{{ formatDate(product.updated_at) }}</td><td class="actions"><button v-if="product.buy_endpoint_id || safeExternalUrl(product.url)" class="icon-button" aria-label="打开商品" @click="openProduct(product)">↗</button></td></tr>
    </tbody></table></div><PaginationBar :page="data.page" :page-size="data.page_size" :total="data.total" @change="changePage" />
  </AsyncState></section>
</template>
<style scoped>.product-toolbar { justify-content: flex-end; }.availability-select { width: 135px; }.product-title { display: flex; min-width: 0; flex-direction: column; gap: 3px; }.product-title b { overflow: hidden; max-width: 380px; text-overflow: ellipsis; white-space: nowrap; }.product-title small { color: var(--subtle); font-weight: 400; text-decoration: line-through; }.text-link { color: var(--accent); font-weight: 550; }</style>
