const SYSTEM_ICON_GLYPHS: Record<string, string> = {
  globe: '◎',
  'shopping-bag': '▣',
  'calendar-check': '✓',
  wrench: '⌁',
  bookmark: '◆',
}

export function siteTypeIcon(icon?: string | null, fallback = '') {
  const value = icon?.trim()
  if (!value) return fallback
  return SYSTEM_ICON_GLYPHS[value] || value
}
