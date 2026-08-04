<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import PageHeader from '@/components/PageHeader.vue'
import AsyncState from '@/components/AsyncState.vue'
import ModalDialog from '@/components/ModalDialog.vue'
import PaginationBar from '@/components/PaginationBar.vue'
import FolderTree, { type FolderNode } from '@/components/FolderTree.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { api, apiErrorMessage } from '@/api/client'
import type { Folder, Page, Site, SiteType, Tag, TaskRef } from '@/api/types'
import { useAppStore } from '@/stores/app'
import { useTaskStore } from '@/stores/tasks'
import { formatDate, hostname } from '@/utils/format'
import { siteTypeIcon } from '@/utils/siteType'

const route = useRoute()
const router = useRouter()
const app = useAppStore()
const tasks = useTaskStore()
const loading = ref(true)
const error = ref('')
const page = ref<Page<Site>>({ items: [], page: 1, page_size: 20, total: 0 })
const folders = ref<Folder[]>([])
const tags = ref<Tag[]>([])
const siteTypes = ref<SiteType[]>([])
const createOpen = ref(false)
const folderOpen = ref(false)
const saving = ref(false)
const importInput = ref<HTMLInputElement | null>(null)
const selectedIds = ref<Set<string>>(new Set())
const bulkFolderId = ref('')
const bulkTagId = ref('')
const bulkSiteTypeId = ref('')
const searchDraft = ref(String(route.query.q || ''))
let searchTimer = 0
const siteForm = ref({ name: '', url: '', folder_id: null as string | null, site_type_id: null as string | null, tag_ids: [] as string[], pinned: false, capabilities: ['bookmark'] as string[] })
const folderForm = ref({ name: '', parent_id: null as string | null })

const selectedFolder = computed(() => route.query.folder ? String(route.query.folder) : null)
const view = computed(() => route.query.view === 'cards' ? 'cards' : 'table')
const assignableSiteTypes = computed(() => siteTypes.value.filter((siteType) => siteType.enabled))
const folderNodes = computed<FolderNode[]>(() => {
  const map = new Map<string, FolderNode>()
  folders.value.forEach((folder) => map.set(folder.id, { ...folder, children: [], siteCount: 0 }))
  page.value.items.forEach((site) => { const node = site.folder_id ? map.get(site.folder_id) : undefined; if (node) node.siteCount = (node.siteCount || 0) + 1 })
  const roots: FolderNode[] = []
  map.forEach((node) => { const parent = node.parent_id ? map.get(node.parent_id) : undefined; if (parent) parent.children.push(node); else roots.push(node) })
  const sort = (nodes: FolderNode[]) => nodes.sort((a, b) => a.sort_order - b.sort_order).forEach((node) => sort(node.children))
  sort(roots)
  return roots
})
const unfiledCount = computed(() => page.value.items.filter((site) => !site.folder_id).length)

async function load() {
  loading.value = true; error.value = ''
  try {
    const folder = selectedFolder.value
    ;[page.value, folders.value, tags.value, siteTypes.value] = await Promise.all([
      api.get<Page<Site>>('/sites', { page: route.query.page || 1, page_size: 20, q: route.query.q, folder_id: folder === 'unfiled' ? 'null' : folder, tag_id: route.query.tag, site_type_id: route.query.type }),
      api.get<Folder[]>('/folders'),
      api.get<Tag[]>('/tags'),
      api.get<SiteType[]>('/site-types'),
    ])
    const visible = new Set(page.value.items.map((site) => site.id))
    selectedIds.value = new Set([...selectedIds.value].filter((id) => visible.has(id)))
  } catch (value) { error.value = apiErrorMessage(value) }
  finally { loading.value = false }
}

function setQuery(values: Record<string, string | undefined>) {
  void router.replace({ query: { ...route.query, ...values } })
}
function search() { window.clearTimeout(searchTimer); searchTimer = window.setTimeout(() => setQuery({ q: searchDraft.value || undefined, page: undefined }), 280) }
function selectFolder(id: string | null) { setQuery({ folder: id || undefined, page: undefined }) }
function setCapability(value: string) {
  const set = new Set(siteForm.value.capabilities)
  if (set.has(value)) set.delete(value); else set.add(value)
  if (!set.size) set.add('bookmark')
  siteForm.value.capabilities = [...set]
}

async function createSite() {
  if (!siteForm.value.name.trim() || !siteForm.value.url.trim()) return
  saving.value = true
  try {
    const site = await api.post<Site>('/sites', siteForm.value)
    app.toast('网站已添加', 'success'); createOpen.value = false
    siteForm.value = { name: '', url: '', folder_id: null, site_type_id: null, tag_ids: [], pinned: false, capabilities: ['bookmark'] }
    await load(); void router.push(`/sites/${site.id}/overview`)
  } catch (value) { app.toast(apiErrorMessage(value), 'error') }
  finally { saving.value = false }
}

async function createFolder() {
  if (!folderForm.value.name.trim()) return
  saving.value = true
  try { await api.post<Folder>('/folders', folderForm.value); app.toast('目录已创建', 'success'); folderOpen.value = false; folderForm.value = { name: '', parent_id: null }; await load() }
  catch (value) { app.toast(apiErrorMessage(value), 'error') }
  finally { saving.value = false }
}

async function moveSite(siteId: string, folderId: string | null) {
  try { await api.patch<Site>(`/sites/${siteId}`, { folder_id: folderId }); app.toast('网站已移动', 'success'); await load() }
  catch (value) { app.toast(apiErrorMessage(value), 'error') }
}

async function togglePin(site: Site) {
  const before = site.pinned; site.pinned = !before
  try { await api.patch<Site>(`/sites/${site.id}`, { pinned: site.pinned }) }
  catch (value) { site.pinned = before; app.toast(apiErrorMessage(value), 'error') }
}

function toggleSelected(id: string) {
  const next = new Set(selectedIds.value)
  if (next.has(id)) next.delete(id); else next.add(id)
  selectedIds.value = next
}

function toggleAll() {
  selectedIds.value = selectedIds.value.size === page.value.items.length
    ? new Set()
    : new Set(page.value.items.map((site) => site.id))
}

async function bulkMove() {
  if (!selectedIds.value.size) return
  const folderId = bulkFolderId.value === 'unfiled' ? null : bulkFolderId.value || null
  saving.value = true
  try {
    await Promise.all([...selectedIds.value].map((id) => api.patch<Site>(`/sites/${id}`, { folder_id: folderId })))
    app.toast(`已移动 ${selectedIds.value.size} 个网站`, 'success'); selectedIds.value = new Set(); await load()
  } catch (value) { app.toast(apiErrorMessage(value), 'error') }
  finally { saving.value = false }
}

async function bulkAddTag() {
  if (!selectedIds.value.size || !bulkTagId.value) return
  saving.value = true
  try {
    const selected = page.value.items.filter((site) => selectedIds.value.has(site.id))
    await Promise.all(selected.map((site) => api.patch<Site>(`/sites/${site.id}`, {
      tag_ids: [...new Set([...(site.tags || []).map((tag) => tag.id), bulkTagId.value])],
    })))
    app.toast(`已为 ${selected.length} 个网站添加标签`, 'success'); bulkTagId.value = ''; await load()
  } catch (value) { app.toast(apiErrorMessage(value), 'error') }
  finally { saving.value = false }
}

async function bulkSetSiteType() {
  if (!selectedIds.value.size || !bulkSiteTypeId.value) return
  saving.value = true
  try {
    await api.post<{ updated: number; site_type_id: string; site_ids: string[] }>('/sites/bulk/site-type', {
      site_ids: [...selectedIds.value],
      site_type_id: bulkSiteTypeId.value,
    })
    app.toast(`已设置 ${selectedIds.value.size} 个网站的类型`, 'success')
    bulkSiteTypeId.value = ''
    selectedIds.value = new Set()
    await load()
  } catch (value) { app.toast(apiErrorMessage(value), 'error') }
  finally { saving.value = false }
}

function siteTypeStyle(siteType?: SiteType | null) {
  const color = siteType?.color || '#6e7078'
  return { color, borderColor: `${color}55`, background: `${color}12` }
}

function dragSite(event: DragEvent, siteId: string) { event.dataTransfer?.setData('text/site-id', siteId); if (event.dataTransfer) event.dataTransfer.effectAllowed = 'move' }

async function importBookmarks(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  const data = new FormData(); data.append('file', file)
  try { const task = await api.post<TaskRef>('/imports/bookmarks', data); tasks.track(task, '导入浏览器书签'); app.taskDrawerOpen = true; app.toast('书签导入已开始', 'success') }
  catch (value) { app.toast(apiErrorMessage(value), 'error') }
  finally { input.value = '' }
}

watch(() => route.query, load, { deep: true })
onMounted(load)
</script>

<template>
  <PageHeader title="网站" description="用目录和标签整理网站，并为它们开启商品追踪或自动签到。">
    <template #actions>
      <input ref="importInput" class="visually-hidden" type="file" accept=".html,text/html" @change="importBookmarks" />
      <button class="button secondary" @click="importInput?.click()">导入书签</button>
      <button class="button" @click="createOpen = true">＋ 添加网站</button>
    </template>
  </PageHeader>
  <div class="sites-layout">
    <aside class="card explorer-panel">
      <div class="explorer-header"><strong>目录</strong><button class="icon-button mini" title="新建目录" @click="folderOpen = true">＋</button></div>
      <FolderTree :nodes="folderNodes" :selected="selectedFolder" :all-count="page.total" :unfiled-count="unfiledCount" @select="selectFolder" @drop-site="moveSite" />
      <div class="explorer-heading">标签</div>
      <button class="tag-filter" :class="{ active: !route.query.tag }" @click="setQuery({ tag: undefined })">全部标签</button>
      <button v-for="tag in tags" :key="tag.id" class="tag-filter" :class="{ active: route.query.tag === tag.id }" @click="setQuery({ tag: tag.id, page: undefined })"><i :style="{ background: tag.color || '#9b9da4' }" />{{ tag.name }}</button>
    </aside>
    <section class="sites-content">
      <div class="toolbar">
        <label class="search-field"><input v-model="searchDraft" placeholder="搜索名称、网址或备注" @input="search" /></label>
        <select :value="route.query.type || ''" class="compact-select" aria-label="网站类型筛选" @change="setQuery({ type: ($event.target as HTMLSelectElement).value || undefined, page: undefined })"><option value="">全部类型</option><option v-for="siteType in siteTypes" :key="siteType.id" :value="siteType.id">{{ siteTypeIcon(siteType.icon) }} {{ siteType.name }}</option></select>
        <select :value="route.query.tag || ''" class="compact-select" aria-label="标签筛选" @change="setQuery({ tag: ($event.target as HTMLSelectElement).value || undefined, page: undefined })"><option value="">全部标签</option><option v-for="tag in tags" :key="tag.id" :value="tag.id">{{ tag.name }}</option></select>
        <div class="view-toggle"><button :class="{ active: view === 'table' }" aria-label="表格视图" @click="setQuery({ view: 'table' })">☷</button><button :class="{ active: view === 'cards' }" aria-label="卡片视图" @click="setQuery({ view: 'cards' })">▦</button></div>
      </div>
      <div v-if="selectedIds.size" class="bulk-toolbar card">
        <strong>已选择 {{ selectedIds.size }} 项</strong>
        <select v-model="bulkFolderId" aria-label="批量移动目标目录"><option value="">选择目录</option><option value="unfiled">未归档</option><option v-for="folder in folders" :key="folder.id" :value="folder.id">{{ folder.name }}</option></select>
        <button class="button secondary small" :disabled="saving || !bulkFolderId" @click="bulkMove">批量移动</button>
        <select v-model="bulkTagId" aria-label="批量添加标签"><option value="">选择标签</option><option v-for="tag in tags" :key="tag.id" :value="tag.id">{{ tag.name }}</option></select>
        <button class="button secondary small" :disabled="saving || !bulkTagId" @click="bulkAddTag">添加标签</button>
        <select v-model="bulkSiteTypeId" aria-label="批量设置网站类型"><option value="">选择类型</option><option v-for="siteType in assignableSiteTypes" :key="siteType.id" :value="siteType.id">{{ siteType.name }}</option></select>
        <button class="button secondary small" :disabled="saving || !bulkSiteTypeId" @click="bulkSetSiteType">设置类型</button>
        <button class="text-button" @click="selectedIds = new Set()">取消选择</button>
      </div>
      <div class="card">
        <AsyncState :loading="loading" :error="error" :empty="!page.items.length" empty-title="还没有网站" empty-text="添加第一个网站，或导入浏览器导出的 HTML 书签。" @retry="load">
          <template #empty-action><button class="button" @click="createOpen = true">添加网站</button></template>
          <div v-if="view === 'table'" class="table-wrap">
            <table class="data-table"><thead><tr><th class="select-cell"><input type="checkbox" :checked="page.items.length > 0 && selectedIds.size === page.items.length" aria-label="选择本页全部网站" @change="toggleAll" /></th><th>网站</th><th>类型</th><th>能力</th><th>标签</th><th>状态</th><th>最近访问</th><th /></tr></thead>
              <tbody><tr v-for="site in page.items" :key="site.id" draggable="true" @dragstart="dragSite($event, site.id)">
                <td class="select-cell"><input type="checkbox" :checked="selectedIds.has(site.id)" :aria-label="`选择 ${site.name}`" @click.stop @change="toggleSelected(site.id)" /></td>
                <td><RouterLink class="cell-title" :to="`/sites/${site.id}/overview`"><span class="site-icon">{{ site.name.slice(0, 1).toUpperCase() }}</span><span><b>{{ site.name }}</b><small>{{ hostname(site.url) }}</small></span></RouterLink></td>
                <td><span v-if="site.site_type" class="site-type-badge" :style="siteTypeStyle(site.site_type)"><span v-if="site.site_type.icon">{{ siteTypeIcon(site.site_type.icon) }}</span>{{ site.site_type.name }}</span><span v-else class="subtle-text">未分类</span></td>
                <td><div class="tag-list"><span v-for="capability in site.capabilities || ['bookmark']" :key="capability" class="tag">{{ capability === 'checkin' ? '签到' : capability === 'product_monitor' ? '商品' : '书签' }}</span></div></td>
                <td><div class="tag-list"><span v-for="tag in site.tags" :key="tag.id" class="tag">{{ tag.name }}</span><span v-if="!site.tags?.length" class="subtle-text">—</span></div></td>
                <td><StatusBadge :status="site.status" /></td><td class="muted">{{ formatDate(site.last_visited_at) }}</td>
                <td class="actions"><button class="pin-button" :class="{ pinned: site.pinned }" :aria-label="site.pinned ? '取消置顶' : '置顶'" @click="togglePin(site)">★</button></td>
              </tr></tbody>
            </table>
          </div>
          <div v-else class="site-card-grid">
            <div v-for="site in page.items" :key="site.id" class="site-card-wrap"><label class="card-selector"><input type="checkbox" :checked="selectedIds.has(site.id)" @change="toggleSelected(site.id)" /> 选择</label><RouterLink class="site-card" :to="`/sites/${site.id}/overview`" draggable="true" @dragstart="dragSite($event, site.id)">
              <div class="row-between"><span class="site-icon">{{ site.name.slice(0, 1).toUpperCase() }}</span><StatusBadge :status="site.status" /></div><div><h3>{{ site.name }}</h3><p>{{ hostname(site.url) }}</p></div><div class="card-meta"><span v-if="site.site_type" class="site-type-badge" :style="siteTypeStyle(site.site_type)"><span v-if="site.site_type.icon">{{ siteTypeIcon(site.site_type.icon) }}</span>{{ site.site_type.name }}</span><div class="tag-list"><span v-for="capability in site.capabilities || ['bookmark']" :key="capability" class="tag">{{ capability }}</span></div></div>
            </RouterLink></div>
          </div>
          <PaginationBar :page="page.page" :page-size="page.page_size" :total="page.total" @change="setQuery({ page: String($event) })" />
        </AsyncState>
      </div>
    </section>
  </div>

  <ModalDialog v-if="createOpen" title="添加网站" :busy="saving" @close="createOpen = false">
    <form id="site-create-form" class="form-grid" @submit.prevent="createSite">
      <div class="field span-2"><label for="site-name">名称 *</label><input id="site-name" v-model.trim="siteForm.name" required placeholder="例如：少数派" autofocus /></div>
      <div class="field span-2"><label for="site-url">网站地址 *</label><input id="site-url" v-model.trim="siteForm.url" required type="url" placeholder="https://example.com" /></div>
      <div class="field"><label for="site-folder">目录</label><select id="site-folder" v-model="siteForm.folder_id"><option :value="null">未归档</option><option v-for="folder in folders" :key="folder.id" :value="folder.id">{{ folder.name }}</option></select></div>
      <div class="field"><label for="site-type">主类型</label><select id="site-type" v-model="siteForm.site_type_id"><option :value="null">使用默认类型</option><option v-for="siteType in assignableSiteTypes" :key="siteType.id" :value="siteType.id">{{ siteTypeIcon(siteType.icon) }} {{ siteType.name }}</option></select></div>
      <div class="field span-2"><label>能力</label><div class="capability-picker"><button v-for="cap in [['bookmark','书签'],['product_monitor','商品'],['checkin','签到']]" :key="cap[0]" type="button" :class="{ active: siteForm.capabilities.includes(cap[0]!) }" @click="setCapability(cap[0]!)">{{ cap[1] }}</button></div></div>
      <label class="check-row span-2"><input v-model="siteForm.pinned" type="checkbox" /> 添加后置顶</label>
    </form>
    <template #footer><button class="button secondary" :disabled="saving" @click="createOpen = false">取消</button><button form="site-create-form" class="button" :disabled="saving">{{ saving ? '正在添加…' : '添加网站' }}</button></template>
  </ModalDialog>
  <ModalDialog v-if="folderOpen" title="新建目录" :busy="saving" @close="folderOpen = false">
    <form id="folder-create-form" class="form-grid" @submit.prevent="createFolder">
      <div class="field span-2"><label for="folder-name">目录名称 *</label><input id="folder-name" v-model.trim="folderForm.name" required autofocus /></div>
      <div class="field span-2"><label for="folder-parent">上级目录</label><select id="folder-parent" v-model="folderForm.parent_id"><option :value="null">无（顶层）</option><option v-for="folder in folders" :key="folder.id" :value="folder.id">{{ folder.name }}</option></select></div>
    </form>
    <template #footer><button class="button secondary" @click="folderOpen = false">取消</button><button form="folder-create-form" class="button" :disabled="saving">创建</button></template>
  </ModalDialog>
</template>

<style scoped>
.sites-layout { display: flex; align-items: flex-start; gap: 17px; }.explorer-panel { position: sticky; top: 75px; width: 210px; flex: 0 0 auto; padding: 9px; }.explorer-header { display: flex; align-items: center; justify-content: space-between; padding: 5px 6px 10px; font-size: 12px; }.icon-button.mini { width: 27px; height: 27px; font-size: 15px; }.explorer-heading { margin: 14px 7px 5px; color: var(--subtle); font-size: 10px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }.tag-filter { display: flex; width: 100%; min-height: 32px; align-items: center; gap: 8px; padding: 0 9px; border: 0; border-radius: 7px; background: transparent; color: var(--muted); cursor: pointer; font-size: 12px; text-align: left; }.tag-filter:hover { background: var(--surface-soft); }.tag-filter.active { color: var(--accent); background: var(--accent-soft); }.tag-filter i { width: 7px; height: 7px; border-radius: 50%; }.sites-content { min-width: 0; flex: 1; }.compact-select { width: 145px; }.view-toggle { display: flex; overflow: hidden; border: 1px solid var(--line); border-radius: 8px; }.view-toggle button { width: 34px; height: 34px; border: 0; border-right: 1px solid var(--line); background: #fff; cursor: pointer; }.view-toggle button:last-child { border: 0; }.view-toggle button.active { color: var(--accent); background: var(--accent-soft); }.bulk-toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 9px; margin-bottom: 10px; padding: 9px 12px; }.bulk-toolbar strong { margin-right: auto; font-size: 12px; }.bulk-toolbar select { width: 142px; min-height: 32px; }.select-cell { width: 34px; text-align: center; }.select-cell input, .card-selector input { width: 15px; min-height: 15px; }.cell-title > span:last-child { display: flex; min-width: 0; flex-direction: column; gap: 3px; }.cell-title small { color: var(--muted); font-weight: 400; }.pin-button { border: 0; color: #c5c4c0; background: transparent; cursor: pointer; font-size: 17px; }.pin-button.pinned { color: #e7a839; }.site-type-badge { display: inline-flex; min-height: 24px; align-items: center; gap: 5px; padding: 0 8px; border: 1px solid; border-radius: 7px; font-size: 11px; font-weight: 600; white-space: nowrap; }.site-card-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; padding: 14px; }.site-card-wrap { position: relative; }.card-selector { position: absolute; z-index: 1; top: 9px; right: 9px; display: flex; align-items: center; gap: 4px; padding: 4px 6px; border-radius: 6px; background: rgba(255,255,255,.92); color: var(--muted); font-size: 10px; }.site-card { display: flex; min-height: 155px; flex-direction: column; justify-content: space-between; padding: 16px; border: 1px solid var(--line); border-radius: 10px; }.site-card:hover { border-color: #c8cbef; box-shadow: var(--shadow); }.site-card h3 { margin: 0; }.site-card p { margin: 4px 0 0; font-size: 12px; }.card-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 7px; }.capability-picker { display: flex; overflow: hidden; border: 1px solid var(--line-strong); border-radius: 8px; }.capability-picker button { min-height: 36px; flex: 1; border: 0; border-right: 1px solid var(--line); background: #fff; cursor: pointer; }.capability-picker button:last-child { border: 0; }.capability-picker button.active { color: var(--accent); background: var(--accent-soft); font-weight: 600; }.check-row { display: flex; align-items: center; gap: 8px; font-size: 13px; }.check-row input { width: 16px; min-height: 16px; }.visually-hidden { position: fixed; width: 1px; height: 1px; opacity: 0; pointer-events: none; }
@media (max-width: 980px) { .site-card-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 760px) { .sites-layout { flex-direction: column; }.explorer-panel { position: static; width: 100%; }.site-card-grid { grid-template-columns: 1fr; } }
</style>
