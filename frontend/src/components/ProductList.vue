<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue'
import AsyncState from './AsyncState.vue'
import PaginationBar from './PaginationBar.vue'
import ProductCategoryManager from './ProductCategoryManager.vue'
import ProductDetailsDialog from './ProductDetailsDialog.vue'
import StatusBadge from './StatusBadge.vue'
import { api, apiErrorMessage } from '@/api/client'
import type { Page, Product, ProductCategory } from '@/api/types'
import { formatDate, formatMoney, safeExternalUrl } from '@/utils/format'
import { useAppStore } from '@/stores/app'

const props = defineProps<{ siteId?: string; compact?: boolean }>()
const app = useAppStore()
const data = ref<Page<Product>>({ items: [], page: 1, page_size: 20, total: 0 })
const categories = ref<ProductCategory[]>([])
const loading = ref(true)
const error = ref('')
const search = ref('')
const availability = ref('')
const categoryId = ref('')
const pageNo = ref(1)
const detailsId = ref('')
const managerOpen = ref(false)
let timer = 0

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await api.get<Page<Product>>('/products', {
      site_id: props.siteId,
      q: search.value,
      availability_status: availability.value,
      category_id: categoryId.value,
      page: pageNo.value,
      page_size: props.compact ? 10 : 20,
    })
  } catch (value) { error.value = apiErrorMessage(value) }
  finally { loading.value = false }
}

async function loadCategories() {
  try {
    const page = await api.get<Page<ProductCategory>>('/product-categories', { site_id: props.siteId, page: 1, page_size: 200 })
    categories.value = page.items
    if (categoryId.value && !categories.value.some((item) => item.id === categoryId.value)) categoryId.value = ''
  } catch (value) { app.toast(apiErrorMessage(value), 'error') }
}

function filter() {
  window.clearTimeout(timer)
  timer = window.setTimeout(() => { pageNo.value = 1; void load() }, 250)
}

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

function applyProductUpdate(product: Product) {
  const index = data.value.items.findIndex((item) => item.id === product.id)
  if (index >= 0) data.value.items[index] = product
}

async function categoriesChanged() {
  await loadCategories()
  await load()
}

watch(() => props.siteId, () => {
  categoryId.value = ''
  pageNo.value = 1
  void Promise.all([loadCategories(), load()])
})
onMounted(() => void Promise.all([loadCategories(), load()]))
onUnmounted(() => window.clearTimeout(timer))
</script>

<template>
  <div class="toolbar product-toolbar">
    <label class="search-field"><input v-model="search" placeholder="搜索商品名称" @input="filter" /></label>
    <select v-model="categoryId" class="category-select" aria-label="商品分类" @change="filter"><option value="">全部分类</option><option v-for="category in categories" :key="category.id" :value="category.id">{{ siteId ? category.name : `${category.site_name} / ${category.name}` }}</option></select>
    <select v-model="availability" class="availability-select" aria-label="库存状态" @change="filter"><option value="">全部状态</option><option value="available">在售</option><option value="sold_out">售罄</option><option value="missing">抓取缺失</option><option value="delisted">明确下架</option><option value="unknown">未知</option></select>
    <button class="button secondary small" @click="managerOpen = true">分类管理</button>
  </div>
  <section class="card">
    <AsyncState :loading="loading" :error="error" :empty="!data.items.length" empty-title="暂无商品" empty-text="开启商品追踪并执行同步后，商品会出现在这里。" @retry="load">
      <div class="table-wrap"><table class="data-table"><thead><tr><th>商品</th><th v-if="!siteId">网站</th><th>分类</th><th>价格</th><th>库存</th><th>更新时间</th><th /></tr></thead><tbody>
        <tr v-for="product in data.items" :key="product.id">
          <td><div class="product-title"><button class="product-name-button" @click="detailsId = product.id">{{ product.name }}</button><small v-if="product.original_price && product.original_price !== product.price">原价 {{ formatMoney(product.original_price, product.currency) }}</small></div></td>
          <td v-if="!siteId"><RouterLink class="text-link" :to="`/sites/${product.site_id}/products`">{{ product.site_name || '查看网站' }}</RouterLink></td>
          <td><span v-if="product.category_name" class="category-pill">{{ product.category_name }}</span><span v-else class="muted">未分类</span></td>
          <td><strong>{{ formatMoney(product.price, product.currency) }}</strong></td>
          <td><StatusBadge :status="product.availability_status" :label="product.availability_status === 'available' ? '在售' : product.availability_status === 'sold_out' ? '售罄' : product.availability_status === 'missing' ? '抓取缺失' : product.availability_status === 'delisted' ? '明确下架' : product.availability_status" /></td>
          <td class="muted">{{ formatDate(product.updated_at) }}</td>
          <td class="actions"><button class="text-button" @click="detailsId = product.id">详情</button><button v-if="product.buy_endpoint_id || safeExternalUrl(product.url)" class="icon-button" aria-label="打开商品" @click="openProduct(product)">↗</button></td>
        </tr>
      </tbody></table></div>
      <PaginationBar :page="data.page" :page-size="data.page_size" :total="data.total" @change="changePage" />
    </AsyncState>
  </section>
  <ProductDetailsDialog v-if="detailsId" :product-id="detailsId" @close="detailsId = ''" @updated="applyProductUpdate" />
  <ProductCategoryManager v-if="managerOpen" :site-id="siteId" @close="managerOpen = false" @changed="categoriesChanged" />
</template>

<style scoped>
.product-toolbar { justify-content: flex-end; }.availability-select { width: 135px; }.category-select { width: min(230px, 30vw); }.product-title { display: flex; min-width: 0; flex-direction: column; gap: 3px; }.product-title small { color: var(--subtle); font-weight: 400; text-decoration: line-through; }.product-name-button { overflow: hidden; max-width: 360px; padding: 0; border: 0; color: var(--text); background: transparent; cursor: pointer; font: inherit; font-weight: 650; text-align: left; text-overflow: ellipsis; white-space: nowrap; }.product-name-button:hover { color: var(--accent); }.text-link { color: var(--accent); font-weight: 550; }.category-pill { display: inline-flex; max-width: 150px; overflow: hidden; padding: 3px 7px; border-radius: 999px; color: var(--accent); background: var(--accent-soft); font-size: 10px; text-overflow: ellipsis; white-space: nowrap; } @media (max-width: 900px) { .product-toolbar { align-items: stretch; }.search-field { flex: 1 0 100%; }.category-select { width: auto; flex: 1; } }
</style>
