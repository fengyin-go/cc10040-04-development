import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

// ── 报告取数覆盖视图 ──

export type CoverageSection = {
  metric: string
  coverage: '齐备' | '无记录' | '无法执行'
  caliber_version: string | null
  caliber_desc: string
  query_template_count: number
  usable_template_count: number
  empty_template_count: number
  blocked_template_count: number
  usable_row_count: number
  sample_rows: Record<string, string | number>[]
  templates: CoverageTemplate[]
  missing_items: string[]
  retriable: boolean
  chart_ready: boolean
}

export type CoverageTemplate = {
  query_template_id: number
  query_template_name: string
  template_status: string
  published: boolean
  dimensions: string
  filters: string
  caliber_version: string | null
  caliber_desc: string
  source: string
  outcome: '成功有数据' | '成功无记录' | '执行失败'
  row_count: number
  sample_rows: Record<string, string | number>[]
  executed_at: string | null
  operator: string | null
  error: string
  missing_items: string[]
  retriable: boolean
}

export type CoverageView = {
  template_id: number
  report_template: string
  period: string
  operator: string
  generated_at: string
  sections: CoverageSection[]
  summary: {
    total_sections: number
    ready_sections: number
    empty_sections: number
    blocked_sections: number
    chart_ready_sections: number
    usable_charts: number
  }
}

export type ReportDraft = {
  draft_id: number
  template_id: number
  report_template: string
  period: string
  generated_at: string
  operator: string
  format: string
  sections: CoverageSection[]
  coverage_view: CoverageView
  total_metrics: number
}

async function crossJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await request(path, init)
  if (!response.ok) throw new Error(`接口返回 ${response.status}，数据未更新`)
  return (await response.json()) as T
}

/** 报告取数覆盖视图：按指标集聚合各章节样本、口径与缺项 */
export function fetchReportCoverage(templateId: number): Promise<CoverageView> {
  return crossJson(`/api/cross/report-template/${templateId}/coverage`)
}

/** 生成报告稿件（章节级容错，单章失败不影响其他章节） */
export function generateReport(templateId: number, operator = '编制人'): Promise<ReportDraft> {
  return crossJson(`/api/cross/report-template/${templateId}/generate?operator=${encodeURIComponent(operator)}`)
}

/** 补取失败/缺项章节，并刷新同一稿件 */
export function refetchReport(
  templateId: number,
  draftId: number | null,
  operator = '编制人',
): Promise<{ coverage_view: CoverageView; draft: ReportDraft | null; refetch_results: unknown[] }> {
  const query = new URLSearchParams({ operator })
  if (draftId !== null) query.set('draft_id', String(draftId))
  return crossJson(`/api/cross/report-template/${templateId}/refetch?${query.toString()}`, { method: 'POST' })
}

/** 重新打开同一稿件，查看各章节最新可引用情况 */
export function openReportDraft(draftId: number): Promise<ReportDraft> {
  return crossJson(`/api/cross/report-template/drafts/${draftId}`)
}

/** 查询模板实际执行留痕（样本、口径版本、取数三态） */
export function fetchQueryRuns(
  templateId: number,
): Promise<{ template_id: number; runs: CoverageTemplate[] }> {
  return crossJson(`/api/cross/query/${templateId}/runs`)
}
