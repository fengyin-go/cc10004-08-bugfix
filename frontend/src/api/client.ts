/** 统一请求封装：拼后端地址、抛网络错误、给页脚留一句可读的说明。 */
const API_BASE = import.meta.env.VITE_API_BASE ?? ''

export function request(path: string, init?: RequestInit): Promise<Response> {
  const url = path.startsWith('http') ? path : `${API_BASE}${path}`
  return fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  }).catch((error: unknown) => {
    const detail = error instanceof Error ? error.message : '请求未送达'
    throw new Error(`接口请求失败：${detail}`)
  })
}

export async function fetchJson<T>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) {
    throw new Error(`接口返回 ${response.status}，数据未更新`)
  }
  return (await response.json()) as T
}

export type ActionOutcome = {
  ok: boolean
  message: string
  entry: Record<string, unknown> | null
}

/**
 * 提交业务动作。后端对「业务拒绝」返回 200 + ok=false（而非 HTTP 错误），
 * 这里统一解包并把后端的真实裁决原因抛给页面展示，避免界面吞掉拒绝信息。
 * action 同时兼容顶层与 values 两种提交位置，这里统一放到 values。
 */
export async function postAction(
  path: string,
  action: string,
  extra: Record<string, unknown> = {},
): Promise<ActionOutcome> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify({ values: { action, ...extra } }),
  })
  if (!response.ok) {
    let detail = `接口返回 ${response.status}`
    try {
      const problem = (await response.json()) as { detail?: string }
      if (problem?.detail) detail = problem.detail
    } catch {
      /* 非 JSON 错误体时保留状态码说明 */
    }
    throw new Error(detail)
  }
  const outcome = (await response.json()) as ActionOutcome
  if (!outcome.ok) {
    throw new Error(outcome.message || '动作被业务规则拒绝')
  }
  return outcome
}
