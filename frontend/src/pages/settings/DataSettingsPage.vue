<script setup lang="ts">
import { onMounted, ref } from 'vue'
import AsyncState from '@/components/AsyncState.vue'
import DataMaintenancePanel from '@/components/DataMaintenancePanel.vue'
import { api, apiErrorMessage } from '@/api/client'
import type { TaskRef } from '@/api/types'
import { useAppStore } from '@/stores/app'
import { useTaskStore } from '@/stores/tasks'
import { formatDate } from '@/utils/format'

interface BackupRecord {
  backup_id: string
  created_at: string
  kind: 'manual' | 'automatic' | 'pre_restore'
  label?: string | null
  database_size_bytes: number
  database_sha256: string
}

interface BackupItem {
  backup_id: string
  status: 'available' | 'invalid'
  record?: BackupRecord | null
  error?: string | null
}

interface PendingRestore {
  backup_id: string
  staged_at: string
  database_sha256: string
}

interface BackupList {
  items: BackupItem[]
  total: number
  pending?: PendingRestore | null
}

const app = useAppStore()
const tasks = useTaskStore()
const bookmarkInput = ref<HTMLInputElement | null>(null)
const backups = ref<BackupList>({ items: [], total: 0, pending: null })
const backupsLoading = ref(true)
const backupsError = ref('')
const backupLabel = ref('')
const backupBusy = ref(false)
const verifyingId = ref('')
const restoreTarget = ref<BackupRecord | null>(null)
const restoreConfirmation = ref('')

async function importBookmarks(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  const data = new FormData()
  data.append('file', file)
  try {
    const task = await api.post<TaskRef>('/imports/bookmarks', data)
    tasks.track(task, '导入浏览器书签')
    app.taskDrawerOpen = true
    app.toast('书签导入已开始', 'success')
  } catch (value) { app.toast(apiErrorMessage(value), 'error') }
  finally { input.value = '' }
}

async function loadBackups() {
  backupsLoading.value = true
  backupsError.value = ''
  try { backups.value = await api.get<BackupList>('/backups') }
  catch (value) { backupsError.value = apiErrorMessage(value) }
  finally { backupsLoading.value = false }
}

async function createBackup() {
  backupBusy.value = true
  try {
    await api.post<BackupRecord>('/backups', { label: backupLabel.value || null })
    backupLabel.value = ''
    app.toast('数据库备份已创建并校验', 'success')
    await loadBackups()
  } catch (value) { app.toast(apiErrorMessage(value), 'error') }
  finally { backupBusy.value = false }
}

async function verifyBackup(item: BackupItem) {
  verifyingId.value = item.backup_id
  try {
    await api.post(`/backups/${item.backup_id}/verify`)
    app.toast('SHA-256 与 SQLite 完整性校验通过', 'success')
  } catch (value) {
    const message = apiErrorMessage(value)
    item.status = 'invalid'
    item.error = message
    if (restoreTarget.value?.backup_id === item.backup_id) {
      restoreTarget.value = null
      restoreConfirmation.value = ''
    }
    app.toast(message, 'error')
  }
  finally { verifyingId.value = '' }
}

function prepareRestore(record: BackupRecord) {
  restoreTarget.value = record
  restoreConfirmation.value = ''
}

async function stageRestore() {
  if (!restoreTarget.value || restoreConfirmation.value !== restoreTarget.value.backup_id) return
  backupBusy.value = true
  try {
    await api.post(`/backups/${restoreTarget.value.backup_id}/restore`, {
      confirmation: restoreConfirmation.value,
    })
    app.toast('恢复已安排，将在 SiteHub 下次启动时安全执行', 'success')
    restoreTarget.value = null
    restoreConfirmation.value = ''
    await loadBackups()
  } catch (value) { app.toast(apiErrorMessage(value), 'error') }
  finally { backupBusy.value = false }
}

async function cancelRestore() {
  backupBusy.value = true
  try {
    await api.delete<void>('/backups/restore-pending')
    app.toast('待执行恢复已取消', 'success')
    await loadBackups()
  } catch (value) { app.toast(apiErrorMessage(value), 'error') }
  finally { backupBusy.value = false }
}

function formatBytes(value: number) {
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / 1024 / 1024).toFixed(1)} MB`
}

function kindLabel(kind: BackupRecord['kind']) {
  return kind === 'pre_restore' ? '恢复前回滚点' : kind === 'automatic' ? '自动备份' : '手动备份'
}

onMounted(loadBackups)
</script>

<template>
  <div class="stack">
    <DataMaintenancePanel @changed="loadBackups" />
    <section class="card">
      <div class="card-header"><div><h2>数据库备份与恢复</h2><p class="subtle-text">使用 SQLite Backup API 创建一致性快照，并校验 SHA-256 与数据库完整性</p></div></div>
      <div class="backup-create">
        <div><h3>创建当前数据库快照</h3><p>包含网站、商品、价格历史、签到配置和运行记录；浏览器 Profile 与截图不包含在数据库备份中。</p></div>
        <div class="backup-create-controls"><input v-model.trim="backupLabel" maxlength="200" placeholder="备注（可选）" aria-label="备份备注" /><button class="button" data-testid="create-backup" :disabled="backupBusy" @click="createBackup">{{ backupBusy ? '处理中…' : '立即备份' }}</button></div>
      </div>
      <div v-if="backups.pending" class="pending-restore">
        <div><strong>已安排恢复</strong><p>备份 {{ backups.pending.backup_id }} 将在 SiteHub 完全停止后的下一次启动时应用。启动前会自动备份当前数据库作为回滚点。</p></div>
        <button class="button secondary small" data-testid="cancel-restore" :disabled="backupBusy" @click="cancelRestore">取消恢复</button>
      </div>
      <AsyncState :loading="backupsLoading" :error="backupsError" :empty="!backups.items.length" empty-title="还没有数据库备份" empty-text="创建第一个快照后，可在这里校验并安排恢复。" @retry="loadBackups">
        <div class="table-wrap"><table class="data-table backup-table"><thead><tr><th>备份</th><th>类型</th><th>大小</th><th>SHA-256</th><th>状态</th><th /></tr></thead><tbody>
          <tr v-for="item in backups.items" :key="item.backup_id">
            <td><div class="backup-name"><strong>{{ item.record?.label || formatDate(item.record?.created_at) }}</strong><small>{{ item.backup_id }}</small></div></td>
            <td>{{ item.record ? kindLabel(item.record.kind) : '未知' }}</td>
            <td>{{ item.record ? formatBytes(item.record.database_size_bytes) : '—' }}</td>
            <td><code>{{ item.record?.database_sha256.slice(0, 12) || '—' }}…</code></td>
            <td><span class="backup-status" :class="item.status">{{ item.status === 'available' ? '可用' : '无效' }}</span><small v-if="item.error" class="backup-error">{{ item.error }}</small></td>
            <td class="actions"><button class="text-button" :data-testid="`verify-backup-${item.backup_id}`" :disabled="verifyingId === item.backup_id || item.status !== 'available'" @click="verifyBackup(item)">{{ verifyingId === item.backup_id ? '校验中…' : '校验' }}</button><button v-if="item.record" class="text-button danger-link" :data-testid="`restore-backup-${item.backup_id}`" :disabled="backupBusy || item.status !== 'available'" @click="prepareRestore(item.record)">恢复</button></td>
          </tr>
        </tbody></table></div>
      </AsyncState>
      <div v-if="restoreTarget" class="restore-confirmation">
        <div><h3>安排恢复“{{ restoreTarget.label || restoreTarget.backup_id }}”</h3><p>恢复不会热覆盖正在运行的数据库。请输入完整备份 ID；确认后将在下一次 SiteHub 启动、数据库打开前执行。</p></div>
        <code>{{ restoreTarget.backup_id }}</code>
        <input v-model.trim="restoreConfirmation" data-testid="restore-confirmation" autocomplete="off" placeholder="输入完整备份 ID 确认" aria-label="恢复确认" />
        <div class="restore-actions"><button class="button secondary" :disabled="backupBusy" @click="restoreTarget = null">取消</button><button class="button danger" data-testid="stage-restore" :disabled="backupBusy || restoreConfirmation !== restoreTarget.backup_id" @click="stageRestore">安排恢复</button></div>
      </div>
    </section>
    <section class="card">
      <div class="card-header"><div><h2>浏览器书签</h2><p class="subtle-text">导入 Chrome、Edge 或 Firefox 导出的 Netscape HTML 文件</p></div></div>
      <div class="data-action">
        <div><h3>保留原始目录层级</h3><p>完整 URL 相同的条目会跳过；同域名下不同路径仍会作为独立网站保留。完成后可在任务详情查看导入和重复报告。</p></div>
        <input ref="bookmarkInput" class="hidden-input" type="file" accept=".html,text/html" @change="importBookmarks" />
        <button class="button secondary" @click="bookmarkInput?.click()">选择书签文件</button>
      </div>
    </section>
    <section class="card info-card">
      <h2>旧项目迁移</h2>
      <p>VShop 与 AutoCheckin 的合并采用独立命令行迁移工具，提供只读分析、完整临时库演练和正式应用三种模式。源数据库与浏览器 Profile 永不原地修改。</p>
      <p class="subtle-text">正式迁移和恢复属于切换操作，需要在两套旧服务停止后执行，因此不会从普通网页按钮直接触发。</p>
    </section>
    <section class="card info-card"><h2>备份边界</h2><p>数据库中的 DPAPI 密钥只能由同一 Windows 用户解密。浏览器 Profile、任务截图和日志不包含在上述快照中，应继续保留原数据目录或单独复制。</p></section>
  </div>
</template>

<style scoped>
.data-action { display: flex; align-items: center; justify-content: space-between; gap: 24px; padding: 22px; }
.data-action h3, .backup-create h3, .restore-confirmation h3 { margin-bottom: 5px; }
.data-action p, .backup-create p, .pending-restore p, .restore-confirmation p, .info-card p { max-width: 760px; margin: 0; font-size: 12px; line-height: 1.7; }
.backup-create { display: flex; align-items: center; justify-content: space-between; gap: 24px; padding: 20px; border-bottom: 1px solid var(--line); }
.backup-create-controls { display: flex; min-width: min(390px, 45%); gap: 8px; }.backup-create-controls input { flex: 1; }
.pending-restore { display: flex; align-items: center; justify-content: space-between; gap: 18px; margin: 14px; padding: 13px; border: 1px solid #e8c778; border-radius: 9px; background: #fff8e8; }.pending-restore p { margin-top: 3px; color: #705411; }
.backup-table code, .restore-confirmation > code { font-size: 10px; }.backup-name { display: flex; max-width: 330px; flex-direction: column; gap: 3px; }.backup-name small { overflow: hidden; color: var(--subtle); font-family: ui-monospace, monospace; font-size: 9px; text-overflow: ellipsis; white-space: nowrap; }.backup-status { display: block; color: var(--positive); font-weight: 650; }.backup-status.invalid, .backup-error, .danger-link { color: var(--negative); }.backup-error { display: block; max-width: 240px; margin-top: 3px; font-size: 9px; }
.restore-confirmation { display: grid; grid-template-columns: minmax(0, 1fr) minmax(240px, auto); gap: 10px 18px; margin: 14px; padding: 15px; border: 1px solid #e7b9b9; border-radius: 10px; background: #fff7f7; }.restore-confirmation > div:first-child { grid-column: 1 / -1; }.restore-confirmation input { grid-column: 1 / -1; }.restore-actions { display: flex; grid-column: 1 / -1; justify-content: flex-end; gap: 8px; }
.hidden-input { position: fixed; width: 1px; height: 1px; opacity: 0; pointer-events: none; }
.info-card { padding: 20px; }
.info-card h2 { margin-bottom: 8px; }
.info-card p + p { margin-top: 8px; }
@media (max-width: 700px) { .data-action, .backup-create, .pending-restore { align-items: stretch; flex-direction: column; }.backup-create-controls { width: 100%; min-width: 0; }.restore-confirmation { grid-template-columns: 1fr; } }
</style>
