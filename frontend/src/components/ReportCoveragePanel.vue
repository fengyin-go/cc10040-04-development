<template>
  <div class="coverage-panel" data-testid="report-coverage-panel">
    <div class="coverage-head">
      <div>
        <h3>报告取数视图</h3>
        <p class="page-desc">
          按指标集聚合各章节实际取数的样本、口径版本与缺项；空结果与无法执行严格区分，缺项不补零。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" data-testid="coverage-refresh" :disabled="loading" @click="loadCoverage">
          刷新取数视图
        </button>
        <button
          v-if="!currentDraftId"
          class="btn primary"
          type="button"
          data-testid="coverage-generate"
          :disabled="loading"
          @click="onGenerate"
        >
          生成报告稿件
        </button>
        <button
          v-else
          class="btn primary"
          type="button"
          data-testid="coverage-refetch"
          :disabled="loading"
          @click="onRefetch"
        >
          补取失败模板
        </button>
      </div>
    </div>

    <p v-if="message" class="coverage-message" :class="messageKind" data-testid="coverage-message">
      {{ message }}
    </p>

    <template v-if="view">
      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">章节总数</span>
          <strong class="stat-value">{{ view.summary.total_sections }}</strong>
        </article>
        <article class="stat-card coverage-ready">
          <span class="stat-label">齐备（可引用图表）</span>
          <strong class="stat-value">{{ view.summary.ready_sections }}</strong>
        </article>
        <article class="stat-card coverage-empty">
          <span class="stat-label">无记录（空结果缺项）</span>
          <strong class="stat-value">{{ view.summary.empty_sections }}</strong>
        </article>
        <article class="stat-card coverage-blocked">
          <span class="stat-label">无法执行</span>
          <strong class="stat-value">{{ view.summary.blocked_sections }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">会上可用图表</span>
          <strong class="stat-value">{{ view.summary.usable_charts }}</strong>
        </article>
      </div>

      <p v-if="currentDraftId" class="draft-banner" data-testid="draft-banner">
        当前稿件 #{{ currentDraftId }}（生成于 {{ view.generated_at }}，操作人：{{ view.operator }}），
        已可引用 {{ view.summary.chart_ready_sections }} / {{ view.summary.total_sections }} 章。
      </p>

      <div v-for="section in view.sections" :key="section.metric" class="coverage-section" :data-coverage="section.coverage">
        <div class="section-head" @click="toggle(section.metric)">
          <span class="coverage-badge" :class="badgeClass(section.coverage)">{{ section.coverage }}</span>
          <span v-if="section.coverage !== '齐备' && section.retriable" class="coverage-badge badge-retriable">
            可补取
          </span>
          <strong class="section-metric">{{ section.metric }}</strong>
          <span class="section-meta">
            口径版本：{{ section.caliber_version ?? '无生效口径' }} ｜
            查询模板 {{ section.query_template_count }} 个 ｜ 可用 {{ section.usable_template_count }} 个 ｜
            实际样本 {{ section.usable_row_count }} 行
          </span>
          <span class="section-toggle">{{ expanded[section.metric] ? '收起' : '展开核对' }}</span>
        </div>

        <div v-if="expanded[section.metric]" class="section-body">
          <p v-if="section.missing_items.length" class="missing-list">
            <span class="missing-title">取数缺项：</span>
            <span v-for="(item, idx) in section.missing_items" :key="idx" class="missing-item">{{ item }}</span>
          </p>
          <p v-else class="missing-list ok">本章取数齐备，样本与口径可直接用于出具分析报告。</p>

          <table class="data-table template-table">
            <thead>
              <tr>
                <th>查询模板</th>
                <th>取数结果</th>
                <th>实际口径</th>
                <th>来源</th>
                <th>样本行数</th>
                <th>执行时间 / 操作人</th>
                <th>说明</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="tpl in section.templates" :key="tpl.query_template_id" :data-outcome="tpl.outcome">
                <td>
                  {{ tpl.query_template_name }}
                  <span class="dim-text">（{{ tpl.dimensions || '无维度' }}）</span>
                </td>
                <td><span class="outcome-tag" :class="outcomeClass(tpl.outcome)">{{ tpl.outcome }}</span></td>
                <td>{{ tpl.caliber_version ?? '—' }}</td>
                <td>{{ sourceLabel(tpl.source) }}</td>
                <td>{{ tpl.row_count }}</td>
                <td>{{ tpl.executed_at ?? '未执行' }}<br /><span class="dim-text">{{ tpl.operator ?? '' }}</span></td>
                <td>{{ tpl.error || (tpl.outcome === '成功无记录' ? '结果为空，缺项不补零' : '—') }}</td>
              </tr>
              <tr v-if="!section.templates.length">
                <td colspan="7" class="empty-state">该指标未关联任何查询模板，无取数通道</td>
              </tr>
            </tbody>
          </table>

          <div v-if="section.sample_rows.length" class="sample-block">
            <h4>章节实际样本（同次查询、同一口径版本）</h4>
            <table class="data-table">
              <thead>
                <tr>
                  <th v-for="col in sampleColumns(section.sample_rows)" :key="col">{{ col }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(row, idx) in section.sample_rows" :key="idx">
                  <td v-for="col in sampleColumns(section.sample_rows)" :key="col">{{ row[col] ?? '—' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p v-else class="dim-text">无实际样本：{{ section.coverage === '无记录' ? '查询成功但结果为空，不得按零汇总' : '模板未成功取数，请按缺项提示补取' }}</p>
        </div>
      </div>
    </template>

    <p v-else-if="!loading" class="empty-state">选择左侧报告模板后，在此查看报告取数视图。</p>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref, watch } from 'vue'

import {
  fetchReportCoverage,
  generateReport,
  openReportDraft,
  refetchReport,
  type CoverageView,
} from '@/api/backend-service'

const props = defineProps<{ templateId: number | null }>()

const view = ref<CoverageView | null>(null)
const loading = ref(false)
const message = ref('')
const messageKind = ref('ok')
const currentDraftId = ref<number | null>(null)
const expanded = reactive<Record<string, boolean>>({})

function badgeClass(coverage: string): string {
  if (coverage === '齐备') return 'badge-ready'
  if (coverage === '无记录') return 'badge-empty'
  return 'badge-blocked'
}

function outcomeClass(outcome: string): string {
  if (outcome === '成功有数据') return 'outcome-ready'
  if (outcome === '成功无记录') return 'outcome-empty'
  return 'outcome-failed'
}

function sourceLabel(source: string): string {
  if (source === 'cache') return '缓存'
  if (source === 'computed') return '实时计算'
  return '未取数'
}

function sampleColumns(rows: Record<string, string | number>[]): string[] {
  const cols: string[] = []
  for (const row of rows) {
    for (const key of Object.keys(row)) {
      if (!cols.includes(key)) cols.push(key)
    }
  }
  return cols
}

function toggle(metric: string) {
  expanded[metric] = !expanded[metric]
}

function notify(text: string, kind: 'ok' | 'error' = 'ok') {
  message.value = text
  messageKind.value = kind
}

async function loadCoverage() {
  if (props.templateId === null) {
    view.value = null
    return
  }
  loading.value = true
  message.value = ''
  try {
    view.value = await fetchReportCoverage(props.templateId)
  } catch (error) {
    notify(error instanceof Error ? error.message : '取数视图读取失败', 'error')
  } finally {
    loading.value = false
  }
}

async function onGenerate() {
  if (props.templateId === null) return
  loading.value = true
  message.value = ''
  try {
    const draft = await generateReport(props.templateId)
    currentDraftId.value = draft.draft_id
    view.value = draft.coverage_view
    draft.coverage_view.sections.forEach((s) => {
      expanded[s.metric] = s.coverage !== '齐备'
    })
    const { summary } = draft.coverage_view
    notify(
      `稿件 #${draft.draft_id} 已生成：齐备 ${summary.ready_sections} 章、无记录 ${summary.empty_sections} 章、无法执行 ${summary.blocked_sections} 章；单章失败未影响其他章节取数。`,
    )
  } catch (error) {
    notify(error instanceof Error ? error.message : '报告生成失败', 'error')
  } finally {
    loading.value = false
  }
}

async function onRefetch() {
  if (props.templateId === null) return
  loading.value = true
  message.value = ''
  try {
    const result = await refetchReport(props.templateId, currentDraftId.value)
    view.value = result.coverage_view
    if (result.draft) {
      currentDraftId.value = result.draft.draft_id
      view.value = result.draft.coverage_view
    } else if (currentDraftId.value !== null) {
      // 后端已在补取时刷新同一稿件，重新打开核对最新可引用章节
      const reopened = await openReportDraft(currentDraftId.value)
      view.value = reopened.coverage_view
    }
    const { summary } = view.value
    notify(
      `补取完成：当前可引用 ${summary.ready_sections} / ${summary.total_sections} 章，仍有 ${summary.blocked_sections} 章无法执行、${summary.empty_sections} 章无记录（缺项未补零）。`,
    )
  } catch (error) {
    notify(error instanceof Error ? error.message : '补取失败', 'error')
  } finally {
    loading.value = false
  }
}

watch(
  () => props.templateId,
  (id) => {
    currentDraftId.value = null
    message.value = ''
    Object.keys(expanded).forEach((key) => delete expanded[key])
    if (id === null) {
      view.value = null
    } else {
      void loadCoverage()
    }
  },
  { immediate: true },
)
</script>
