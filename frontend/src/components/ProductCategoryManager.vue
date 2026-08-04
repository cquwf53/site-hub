<script setup lang="ts">
import { onMounted, ref } from 'vue'
import AsyncState from './AsyncState.vue'
import ModalDialog from './ModalDialog.vue'
import PaginationBar from './PaginationBar.vue'
import { api, apiErrorMessage } from '@/api/client'
import type { Page, ProductCategory, Site } from '@/api/types'
import { useAppStore } from '@/stores/app'

const props = defineProps<{ siteId?: string }>()
const emit = defineEmits<{ close: []; changed: [] }>()
const app = useAppStore()
const data = ref<Page<ProductCategory>>({ items: [], page: 1, page_size: 50, total: 0 })
const sites = ref<Site[]>([])
const loading = ref(true)
const saving = ref(false)
const error = ref('')
const search = ref('')
const createSiteId = ref(props.siteId || '')
const createName = ref('')
const createSort = ref(0)
const editingId = ref('')
const editingName = ref('')
const editingSort = ref(0)
let timer = 0

async function load(page = data.value.page || 1) {
  loading.value = true
  error.value = ''
  try {
    const categoryRequest = api.get<Page<ProductCategory>>('/product-categories', {
      site_id: props.siteId, q: search.value, page, page_size: 50,
    })
    const [categoryPage, sitePage] = await Promise.all([
      categoryRequest,
      props.siteId || sites.value.length
        ? Promise.resolve(null)
        : api.get<Page<Site>>('/sites', { page: 1, page_size: 200 }),
    ])
    data.value = categoryPage
    if (sitePage) {
      sites.value = sitePage.items
      if (!createSiteId.value) createSiteId.value = sites.value[0]?.id || ''
    }
  } catch (value) { error.value = apiErrorMessage(value) }
  finally { loading.value = false }
}

function filter() {
  window.clearTimeout(timer)
  timer = window.setTimeout(() => void load(1), 250)
}

async function createCategory() {
  const siteId = props.siteId || createSiteId.value
  if (!siteId || !createName.value.trim()) return
  saving.value = true
  try {
    await api.post<ProductCategory>(`/sites/${siteId}/product-categories`, { name: createName.value, sort_order: createSort.value })
    createName.value = ''; createSort.value = 0
    app.toast('商品分类已创建', 'success')
    emit('changed')
    await load(1)
  } catch (value) { app.toast(apiErrorMessage(value), 'error') }
  finally { saving.value = false }
}

function startEdit(item: ProductCategory) {
  editingId.value = item.id
  editingName.value = item.name
  editingSort.value = item.sort_order
}

async function saveEdit() {
  if (!editingId.value || !editingName.value.trim()) return
  saving.value = true
  try {
    await api.patch<ProductCategory>(`/product-categories/${editingId.value}`, { name: editingName.value, sort_order: editingSort.value })
    editingId.value = ''
    app.toast('商品分类已保存', 'success')
    emit('changed')
    await load()
  } catch (value) { app.toast(apiErrorMessage(value), 'error') }
  finally { saving.value = false }
}

async function remove(item: ProductCategory) {
  const suffix = item.product_count ? `，其中 ${item.product_count} 个商品将变为“未分类”` : ''
  const sourceWarning = item.source_name ? '。这是抓取分类，如果数据源仍返回它，下次同步会重建' : ''
  if (!window.confirm(`确定删除分类“${item.name}”吗${suffix}${sourceWarning}？`)) return
  saving.value = true
  try {
    await api.delete<void>(`/product-categories/${item.id}`)
    app.toast('商品分类已删除', 'success')
    emit('changed')
    const lastItemOnPage = data.value.items.length === 1 && data.value.page > 1
    await load(lastItemOnPage ? data.value.page - 1 : data.value.page)
  } catch (value) { app.toast(apiErrorMessage(value), 'error') }
  finally { saving.value = false }
}

onMounted(load)
</script>

<template>
  <ModalDialog title="商品分类管理" :busy="saving" wide @close="$emit('close')">
    <div class="category-manager stack">
      <form class="category-create" @submit.prevent="createCategory">
        <select v-if="!siteId" v-model="createSiteId" required aria-label="所属网站"><option value="" disabled>选择网站</option><option v-for="site in sites" :key="site.id" :value="site.id">{{ site.name }}</option></select>
        <input v-model.trim="createName" required maxlength="200" placeholder="新分类名称" aria-label="新分类名称" />
        <input v-model.number="createSort" type="number" min="0" max="1000000" aria-label="排序" />
        <button class="button small" :disabled="saving || !createName || (!siteId && !createSiteId)">新建</button>
      </form>
      <div class="category-search"><input v-model="search" placeholder="搜索分类或网站" @input="filter" /></div>
      <AsyncState :loading="loading" :error="error" :empty="!data.items.length" empty-title="暂无商品分类" empty-text="可在上方创建第一个分类。" @retry="load()">
        <div class="category-list">
          <article v-for="item in data.items" :key="item.id" class="category-row">
            <template v-if="editingId === item.id">
              <input v-model.trim="editingName" maxlength="200" aria-label="分类名称" @keyup.enter="saveEdit" />
              <input v-model.number="editingSort" type="number" min="0" max="1000000" aria-label="排序" />
              <button class="button small" :disabled="saving || !editingName" @click="saveEdit">保存</button>
              <button class="button secondary small" :disabled="saving" @click="editingId = ''">取消</button>
            </template>
            <template v-else>
              <div><strong>{{ item.name }}</strong><small>{{ !siteId ? `${item.site_name} · ` : '' }}{{ item.source_name ? '抓取分类' : '人工分类' }}</small></div>
              <span class="category-count">{{ item.product_count }} 个商品</span>
              <span class="category-sort">排序 {{ item.sort_order }}</span>
              <button class="text-button" :disabled="saving" @click="startEdit(item)">编辑</button>
              <button class="text-button danger-link" :disabled="saving" @click="remove(item)">删除</button>
            </template>
          </article>
        </div>
        <PaginationBar :page="data.page" :page-size="data.page_size" :total="data.total" @change="load" />
      </AsyncState>
    </div>
    <template #footer><button class="button secondary" :disabled="saving" @click="$emit('close')">完成</button></template>
  </ModalDialog>
</template>

<style scoped>
.category-manager { gap: 13px; }.category-create { display: grid; grid-template-columns: minmax(0, 1.3fr) minmax(0, 1.3fr) 90px auto; gap: 8px; padding: 13px; border-radius: 9px; background: var(--surface-soft); }.category-search input { width: 100%; }.category-list { border: 1px solid var(--line); border-radius: 9px; }.category-row { display: grid; grid-template-columns: minmax(0, 1fr) 95px 75px auto auto; align-items: center; gap: 9px; padding: 11px 13px; border-bottom: 1px solid var(--line); }.category-row:last-child { border: 0; }.category-row div { display: flex; min-width: 0; flex-direction: column; gap: 2px; }.category-row strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.category-row small, .category-count, .category-sort { color: var(--subtle); font-size: 10px; }.category-row input[type='number'] { width: 80px; }.danger-link { color: var(--negative); } @media (max-width: 650px) { .category-create { grid-template-columns: 1fr 80px; }.category-create select, .category-create input:first-of-type { grid-column: 1 / -1; }.category-row { grid-template-columns: 1fr auto auto; }.category-count, .category-sort { display: none; } }
</style>
