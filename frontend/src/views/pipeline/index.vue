<template>
  <section class="page" data-module="pipeline">
    <header class="page-head">
      <div>
        <h2>分析流水线管理</h2>
        <p class="page-desc">按节点执行轮次记录可追溯轨迹：动作只能顺序推进，失败恢复另起新轮，归档证据按轮次隔离。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记流水线节点</button>
        <button class="btn" type="button" @click="exportRows">导出分析流水线清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <label class="filter-item">
        <span>状态</span>
        <select v-model="statusFilter">
          <option value="">全部状态</option>
          <option v-for="s in statuses" :key="s" :value="s">{{ s }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>执行轮次</th>
          <th>可执行动作</th>
          <th>裁决轨迹</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <span class="status-tag" :class="statusClass(row.status)">{{ row.status }}</span>
          </td>
          <td>
            <span v-if="roundOf(row)">第 {{ roundOf(row)?.round_no }} 轮 · {{ roundOf(row)?.state }}</span>
            <span v-else class="muted">旧记录待补建</span>
          </td>
          <td class="row-actions">
            <button
              v-for="action in allowedActions(row)"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <span v-if="!allowedActions(row).length" class="muted">无可用动作</span>
          </td>
          <td>
            <button class="link" type="button" @click="openDetail(row)">查看轮次与裁决</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 4" class="empty-state">暂无分析流水线数据，可先登记流水线节点</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条分析流水线记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <div v-if="detail" class="drawer-mask" @click.self="closeDetail">
      <aside class="drawer" role="dialog" aria-label="节点轮次裁决详情">
        <header class="drawer-head">
          <div>
            <h3>{{ detail['节点编号'] }} · 裁决详情</h3>
            <p class="page-desc">
              当前状态 {{ detail.status }}，第 {{ detail.current_round ?? '—' }} 轮；
              阻断与放行均以服务端裁决为准。
            </p>
          </div>
          <button class="btn ghost" type="button" @click="closeDetail">关闭</button>
        </header>

        <div class="drawer-body">
          <div v-for="round in detail.rounds ?? []" :key="round.round_no" class="round-card">
            <header class="round-head">
              <strong>第 {{ round.round_no }} 轮{{ round.legacy ? '（旧记录补建）' : '' }}</strong>
              <span class="status-tag" :class="statusClass(round.state)">{{ round.state }}</span>
              <span v-if="round.conclusion" class="muted">结论：{{ round.conclusion }}</span>
            </header>
            <p v-if="round.completion_proof" class="proof-line">
              完成证据：<code>{{ round.completion_proof }}</code>
              <span class="muted">（归档于 {{ round.closed_at ?? '—' }}）</span>
            </p>
            <p v-else-if="round.closed_at" class="muted">终结时间：{{ round.closed_at }}，无完成证据</p>
            <ul class="event-list">
              <li v-for="ev in round.events ?? []" :key="ev.event_id" :class="{ 'event-blocked': !ev.ok }">
                <span class="event-time">{{ ev.at }}</span>
                <span class="event-action">{{ ev.action }}</span>
                <span :class="ev.ok ? 'event-ok' : 'event-no'">{{ ev.ok ? '放行' : '阻断' }}</span>
                <span v-if="ev.completion_proof" class="muted">证据 {{ ev.completion_proof }}</span>
                <span v-if="!ev.ok && ev.reason" class="event-reason">{{ ev.reason }}</span>
              </li>
            </ul>
          </div>
        </div>
      </aside>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { fetchJson, postAction } from '@/api/client'

type TraceEvent = {
  event_id: string
  at: string
  action: string
  ok: boolean
  reason: string
  completion_proof?: string
  from_state?: string
  to_state?: string
}

type Round = {
  round_no: number
  state: string
  opened_at: string | null
  closed_at: string | null
  conclusion: string | null
  completion_proof: string | null
  legacy?: boolean
  events: TraceEvent[]
}

type Row = Record<string, string | number | boolean | null> & {
  id: number
  status: string
  current_round?: number
  rounds?: Round[]
}

const ENDPOINT = '/api/pipeline'
const columns = ["节点编号", "节点类型", "输入源", "输出目标", "处理脚本", "依赖上游", "超时秒数", "重试次数"]
const statuses = ["草稿", "待调度", "运行中", "已完成", "已失败"]
const stats = ref([
  { label: "流水线节点总数", value: 0 },
  { label: "运行中节点", value: 0 },
  { label: "已完成节点", value: 0 },
  { label: "失败待恢复", value: 0 },
])

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const statusFilter = ref('')
const filterFields = columns.slice(0, 3)
const detail = ref<Row | null>(null)

function resetFilters() {
  filters.value = {}
  statusFilter.value = ''
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '流水线节点登记入口尚未接入审批流'
}

/** 顺序协议的前端镜像：只给当前状态下唯一合法的下一动作（含失败恢复与重跑出口）。 */
function allowedActions(row: Row): string[] {
  switch (row.status) {
    case '草稿':
      return ['提交调度']
    case '待调度':
      return ['启动运行']
    case '运行中':
      return ['终止运行', '归档结果']
    case '已失败':
      return ['启动运行']
    case '已完成':
      return ['提交调度']
    default:
      return []
  }
}

function roundOf(row: Row): Round | undefined {
  return row.rounds?.[row.rounds.length - 1]
}

function statusClass(status: string): string {
  if (status === '已完成') return 'status-done'
  if (status === '已失败') return 'status-failed'
  if (status === '运行中') return 'status-running'
  return 'status-idle'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    await postAction(`${ENDPOINT}/${row.id}/actions`, action)
    await reload()
    if (detail.value && detail.value.id === row.id) {
      await openDetail(row)
    }
  } catch (error) {
    // 业务拒绝原因由后端给出，直接展示，不再吞掉
    errorMessage.value = error instanceof Error ? error.message : '分析流水线操作失败'
    await reload()
  }
}

async function openDetail(row: Row) {
  try {
    detail.value = await fetchJson<Row>(`${ENDPOINT}/${row.id}`)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '节点详情读取失败'
  }
}

function closeDetail() {
  detail.value = null
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams({
    ...(filters.value as Record<string, string>),
    ...(statusFilter.value ? { status: statusFilter.value } : {}),
  }).toString()
  try {
    const payload = await fetchJson<{ items: Row[]; total: number }>(`${ENDPOINT}?${query}`)
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    stats.value[0].value = payload.total
    stats.value[1].value = rows.value.filter((r) => r.status === '运行中').length
    stats.value[2].value = rows.value.filter((r) => r.status === '已完成').length
    stats.value[3].value = rows.value.filter((r) => r.status === '已失败').length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '分析流水线列表读取失败'
  }
}

onMounted(reload)
</script>
