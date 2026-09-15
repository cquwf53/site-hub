<script setup lang="ts">
import { onMounted, ref } from 'vue'
import AsyncState from './AsyncState.vue'
import ModalDialog from './ModalDialog.vue'
import PaginationBar from './PaginationBar.vue'
import ProductPriceChart from './ProductPriceChart.vue'
import ProductWatchEditor from './ProductWatchEditor.vue'
import StatusBadge from './StatusBadge.vue'
import { api, apiErrorMessage } from '@/api/client'
import type { Page, PriceObservation, Product, ProductCategory } from '@/api/types'
import { formatDate, formatMoney, safeExternalUrl } from '@/utils/format'
import { useAppStore } from '@/stores/app'

const props = defineProps<{ productId: string }>()
const emit = defineEmits<{ close: []; updated: [product: Product] }>()
const app = useAppStore()
const product = ref<Product | null>(null)
const categories = ref<ProductCategory[]>([])
const history = ref<Page<PriceObservation>>({ items: [], page: 1, page_size: 100, total: 0 })
const loading = ref(true)
const historyLoading = ref(false)
const saving = ref(false)
const error = ref('')
const historyError = ref('')
const categorySelection = ref('')

function selectionFor(item: Product) {
  if (!item.category_overridden) return '__automatic__'
  return item.category_id || '__unclassified__'
}

async function loadHistory(page = 1) {
  historyLoading.value = true
  historyError.value = ''
  try {
    history.value = await api.get<Page<PriceObservation>>(`/products/${props.productId}/price-history`, { page, page_size: 100 })
  } catch (value) { historyError.value = apiErrorMessage(value) }
  finally { historyLoading.value = false }
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    product.value = await api.get<Product>(`/products/${props.productId}`)
    categorySelection.value = selectionFor(product.value)
    const [categoryPage] = await Promise.all([
      api.get<Page<ProductCategory>>('/product-categories', { site_id: product.value.site_id, page_size: 200 }),
      loadHistory(),
    ])
    categories.value = categoryPage.items
  } catch (value) { error.value = apiErrorMessage(value) }
  finally { loading.value = false }
}

async function saveCategory() {
  if (!product.value) return
  saving.value = true
  try {
    const automatic = categorySelection.value === '__automatic__'
    product.value = await api.patch<Product>(`/products/${product.value.id}`, {
      category_id: automatic || categorySelection.value === '__unclassified__' ? null : categorySelection.value,
      category_mode: automatic ? 'automatic' : 'manual',
    })
    categorySelection.value = selectionFor(product.value)
    emit('updated', product.value)
    app.toast('商品分类已更新', 'success')
  } catch (value) { app.toast(apiErrorMessage(value), 'error') }
  finally { saving.value = false }
}

async function openProduct() {
  if (!product.value) return
  try {
    if (product.value.buy_endpoint_id) {
      await api.post<void>(`/endpoints/${product.value.buy_endpoint_id}/open`)
      return
    }
    const safe = safeExternalUrl(product.value.url)
    if (safe) window.open(safe, '_blank', 'noopener,noreferrer')
  } catch (value) { app.toast(apiErrorMessage(value), 'error') }
}

onMounted(load)
</script>

<template>
  <ModalDialog :title="product?.name || '商品详情'" :busy="saving" wide @close="$emit('close')">
    <AsyncState :loading="loading" :error="error" :empty="false" @retry="load">
      <div v-if="product" class="product-detail stack">
        <div class="detail-heading row-between">
          <div>
            <div class="detail-title-row"><h3>{{ product.name }}</h3><StatusBadge :status="product.availability_status || 'unknown'" /></div>
            <p>{{ product.site_name }}<span v-if="product.external_key"> · {{ product.external_key }}</span></p>
          </div>
          <button v-if="product.buy_endpoint_id || safeExternalUrl(product.url)" class="button secondary small" @click="openProduct">打开购买页 ↗</button>
        </div>

        <div class="metric-grid">
          <div><span>当前价格</span><strong>{{ formatMoney(product.price, product.currency) }}</strong></div>
          <div><span>历史最低</span><strong>{{ formatMoney(product.price_history_summary?.min_price, product.currency) }}</strong></div>
          <div><span>历史最高</span><strong>{{ formatMoney(product.price_history_summary?.max_price, product.currency) }}</strong></div>
          <div><span>观测次数</span><strong>{{ product.price_history_summary?.observation_count || 0 }}</strong></div>
        </div>

        <section class="detail-section">
          <div class="section-heading"><div><h4>价格曲线</h4><p>当前分页展示最近 {{ history.items.length }} 条，共 {{ history.total }} 条记录。</p></div></div>
          <AsyncState :loading="historyLoading" :error="historyError" :empty="false" @retry="loadHistory(history.page)">
            <ProductPriceChart :observations="history.items" :currency="product.currency" />
            <PaginationBar v-if="history.total > history.page_size" :page="history.page" :page-size="history.page_size" :total="history.total" @change="loadHistory" />
          </AsyncState>
        </section>

        <ProductWatchEditor :product-id="product.id" @updated="product.watched = $event.watched; emit('updated', product)" />

        <section class="detail-section category-editor">
          <div><h4>商品分类</h4><p>可跟随抓取分类，也可人工覆盖；人工选择不会被下次同步覆盖。</p></div>
          <select v-model="categorySelection" :disabled="saving">
            <option value="__automatic__">跟随抓取分类{{ product.scraped_category_name ? `（${product.scraped_category_name}）` : '' }}</option>
            <option value="__unclassified__">未分类（人工）</option>
            <option v-for="item in categories" :key="item.id" :value="item.id">{{ item.name }}</option>
          </select>
          <button class="button small" :disabled="saving || categorySelection === selectionFor(product)" @click="saveCategory">{{ saving ? '保存中…' : '保存分类' }}</button>
        </section>

        <section v-if="product.description || product.raw_tags?.length || product.tags?.length" class="detail-section product-copy">
          <h4>商品信息</h4>
          <p v-if="product.description">{{ product.description }}</p>
          <div v-if="product.raw_tags?.length || product.tags?.length" class="tag-list"><span v-for="tag in product.tags" :key="tag.id" class="tag">{{ tag.name }}</span><span v-for="tag in product.raw_tags" :key="tag" class="tag">{{ tag }}</span></div>
        </section>

        <div class="detail-footnote">最近观测：{{ formatDate(product.last_observed_at) }} · 最近更新：{{ formatDate(product.updated_at) }}</div>
      </div>
    </AsyncState>
  </ModalDialog>
</template>

<style scoped>
.product-detail { gap: 18px; }.detail-heading h3 { margin: 0; }.detail-heading p { margin: 5px 0 0; color: var(--subtle); font-size: 11px; }.detail-title-row { display: flex; flex-wrap: wrap; align-items: center; gap: 9px; }.metric-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 9px; }.metric-grid div { display: flex; min-width: 0; flex-direction: column; gap: 5px; padding: 12px; border-radius: 9px; background: var(--surface-soft); }.metric-grid span { color: var(--subtle); font-size: 10px; }.metric-grid strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.detail-section { display: flex; flex-direction: column; gap: 10px; }.detail-section h4 { margin: 0; }.detail-section p { margin: 3px 0 0; color: var(--muted); font-size: 12px; }.category-editor { display: grid; grid-template-columns: 1fr minmax(160px, 220px) auto; align-items: end; padding: 14px; border: 1px solid var(--line); border-radius: 10px; }.category-editor select { min-height: 34px; }.product-copy { padding: 14px; border-radius: 9px; background: var(--surface-soft); }.detail-footnote { color: var(--subtle); font-size: 10px; } @media (max-width: 650px) { .metric-grid { grid-template-columns: repeat(2, 1fr); }.category-editor { grid-template-columns: 1fr; }.detail-heading { align-items: flex-start; flex-direction: column; } }
</style>
