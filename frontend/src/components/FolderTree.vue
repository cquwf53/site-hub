<script setup lang="ts">
import type { Folder } from '@/api/types'
export interface FolderNode extends Folder { children: FolderNode[]; siteCount?: number }
defineProps<{ nodes: FolderNode[]; selected: string | null; allCount?: number; unfiledCount?: number }>()
const emit = defineEmits<{ select: [id: string | null]; dropSite: [siteId: string, folderId: string | null] }>()

function dragOver(event: DragEvent) { if (event.dataTransfer?.types.includes('text/site-id')) event.preventDefault() }
function drop(event: DragEvent, folderId: string | null) {
  event.preventDefault()
  const id = event.dataTransfer?.getData('text/site-id')
  if (id) emit('dropSite', id, folderId)
}
</script>

<template>
  <div class="folder-tree">
    <button class="folder-row" :class="{ active: selected === null }" @click="$emit('select', null)" @dragover="dragOver" @drop="drop($event, null)"><span>▦</span><b>全部网站</b><em>{{ allCount }}</em></button>
    <button class="folder-row" :class="{ active: selected === 'unfiled' }" @click="$emit('select', 'unfiled')" @dragover="dragOver" @drop="drop($event, null)"><span>□</span><b>未归档</b><em>{{ unfiledCount }}</em></button>
    <template v-for="node in nodes" :key="node.id">
      <button class="folder-row" :class="{ active: selected === node.id }" @click="$emit('select', node.id)" @dragover="dragOver" @drop="drop($event, node.id)"><span>▸</span><b>{{ node.name }}</b><em>{{ node.siteCount }}</em></button>
      <div v-if="node.children.length" class="folder-children">
        <FolderTree :nodes="node.children" :selected="selected" @select="$emit('select', $event)" @drop-site="(siteId, folderId) => $emit('dropSite', siteId, folderId)" />
      </div>
    </template>
  </div>
</template>

<style scoped>
.folder-tree { display: flex; flex-direction: column; gap: 2px; }
.folder-row { display: grid; width: 100%; min-height: 35px; grid-template-columns: 18px 1fr auto; align-items: center; gap: 7px; padding: 0 9px; border: 0; border-radius: 7px; background: transparent; color: var(--muted); cursor: pointer; text-align: left; }
.folder-row:hover { background: var(--surface-soft); }
.folder-row.active { color: var(--accent); background: var(--accent-soft); }
.folder-row b { overflow: hidden; font-size: 12px; font-weight: 550; text-overflow: ellipsis; white-space: nowrap; }
.folder-row em { color: var(--subtle); font-size: 11px; font-style: normal; }
.folder-children { margin-left: 16px; padding-left: 4px; border-left: 1px solid var(--line); }
</style>
