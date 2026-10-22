// 与后端交互的薄封装。所有请求同源（生产由 nginx/FastAPI 同域提供），
// 因此带上 Cookie 即可，无需处理跨域。

export type StateName = '活跃' | '无课' | '今日未签到' | '异常' | '严重异常'

export interface StudentCardData {
  student_no: string
  name: string
  grade: string
  major_code: string
  major_name: string
  class_name: string
  group: string
  state: StateName
  level: 'ok' | 'warn' | 'danger'
  reason: string
  consecutive_missed_days: number
  last_class_date: string | null
  last_sign_in_time: string | null
  /** 是否示示例辅导员手动标记过（**与当前状态无关**：标记后又异常时仍为 true） */
  manual_marked?: boolean
  /** 标记时刻，精确到分钟，如 2026-09-20 12:37 */
  manual_marked_at?: string | null
  manual_reason?: string
  manual_by?: string
}

export interface GroupData {
  key: string
  grade: string
  major: string
  total: number
  counts: Record<string, number>
  students: StudentCardData[]
}

export interface OverviewData {
  generated_at: string
  data_mode: string
  totals: { students: number; groups: number; abnormal: number }
  counts: Record<string, number>
  groups: GroupData[]
}

export interface SessionCell {
  schedule_guid: string
  course_name: string
  node_name: string
  classroom: string
  start: string
  end: string
  state: string
  signed: boolean
  sign_in_time: string | null
  /** 这节课能不能看到出勤。false = 录播系统覆盖不到这间教室（教务名单里的课） */
  covered?: boolean
}

export interface DayData {
  date: string
  weekday: string
  is_today: boolean
  has_class: boolean
  state: string
  /** 当天有多少节课没有考勤源覆盖（看不到"来没来"） */
  no_source_count?: number
  sessions: SessionCell[]
}

export interface WeekData {
  student_no: string
  name: string
  week_start: string
  week_end: string
  week_label: string
  state: string
  level: string
  reason: string
  manual_marked?: boolean
  /** 标记时刻，精确到分钟，如 2026-09-20 12:37 */
  manual_marked_at?: string | null
  manual_reason?: string
  manual_by?: string
  days: DayData[]
}

/** 一条手动标记流水 */
export interface MarkHistoryItem {
  id: number
  action: 'mark' | 'unmark'
  state: string
  reason: string
  by: string
  at: string
}

export interface SessionInfo {
  authenticated: boolean
  data_mode: string
  app_base_path: string
  version: string
  now: string
}

/** 采集体检结果：判断"页面上的状态是不是最新的"。 */
export interface HealthData {
  checked_at: string
  consecutive_failures: number
  last_success_age_minutes: number | null
  stale_after_minutes: number
  stale: boolean
  unhealthy: boolean
  last_message: string
  polling: boolean
  data_mode: string
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const body = await res.json()
      if (body?.detail) detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
    } catch {
      /* 忽略解析失败 */
    }
    throw new ApiError(res.status, detail)
  }
  return (await res.json()) as T
}

export const api = {
  session: () => request<SessionInfo>('/session'),
  login: (password: string) =>
    request<SessionInfo>('/login', { method: 'POST', body: JSON.stringify({ password }) }),
  logout: () => request<SessionInfo>('/logout', { method: 'POST' }),
  overview: (show: 'all' | 'abnormal' | 'today_class' = 'all', keyword = '') =>
    request<OverviewData>(
      `/overview?show=${show}&keyword=${encodeURIComponent(keyword)}`,
    ),
  week: (studentNo: string, anchor?: string) =>
    request<WeekData>(
      `/students/${encodeURIComponent(studentNo)}/week${anchor ? `?anchor=${anchor}` : ''}`,
    ),
  health: () => request<HealthData>('/collect/health'),
  /**
   * 老师手动标记为「活跃」。
   *
   * 规则：**「标记后，视作一次没有课程的签到」** —— 相当于学生刚签到过一次
   * （那天没课），把标记日及以前的连续无签到清零；但**不赠送豁免**，
   * 从第二天起照常判定：第二天仍没签到就继续报异常。
   */
  markStudent: (studentNo: string, reason: string, by = '老师') =>
    request<StudentCardData>(`/students/${encodeURIComponent(studentNo)}/mark`, {
      method: 'POST',
      body: JSON.stringify({ state: '活跃', reason, by }),
    }),
  unmarkStudent: (studentNo: string) =>
    request<StudentCardData>(`/students/${encodeURIComponent(studentNo)}/mark`, {
      method: 'DELETE',
    }),
  /**
   * 标记流水。**可以重复标记**，每次都会留一条（重复标记会覆盖"当前标记"，
   * 光看当前标记查不到上一次是谁、为什么）。
   */
  markHistory: (studentNo: string) =>
    request<{ total: number; mark_times: number; items: MarkHistoryItem[] }>(
      `/students/${encodeURIComponent(studentNo)}/mark-history`,
    ),

  // ── 辅导员联系人（预警发给谁） ──────────────────────────
  /** 所有班 + 各自示示例辅导员收件位置（每班 3 个），给后台配置用。 */
  counselors: () => request<CounselorClass[]>('/counselors'),
  /** 保存一个班示示例辅导员邮箱。三个都空 = 等于没配（预警回落到管理员）。 */
  saveCounselor: (payload: CounselorClassInput) =>
    request<CounselorClass>('/counselors', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  deleteCounselor: (classKey: string) =>
    request<{ ok: boolean; message: string }>(
      `/counselors/${encodeURIComponent(classKey)}`,
      { method: 'DELETE' },
    ),
  /** 还没示示例辅导员的班 —— 这些班的学生预警目前只能发给管理员。 */
  missingCounselors: () =>
    request<{ count: number; classes: { class_name: string; class_key: string; students: number }[] }>(
      '/counselors/missing',
    ),
  /**
   * 立即把当前预警按收件人分发（不等到每日定时）。
   * `dryRun=true` 只回「会发给谁、各多少条」—— 配错邮箱时最怕
   * "发出去了才发现"，所以留了这个先看后发的口子。
   */
  notifyWarnings: (dryRun = false) =>
    request<NotifyResult>(
      `/notify/warnings?force=true&dry_run=${dryRun ? 'true' : 'false'}`,
      { method: 'POST' },
    ),
}

export interface CounselorClass {
  class_key: string
  class_name: string
  students: number
  slot1: string
  slot2: string
  slot3: string
  note: string
  updated_at: string | null
}

export interface CounselorClassInput {
  class_name: string
  slot1: string
  slot2: string
  slot3: string
  note: string
}

export interface NotifyResult {
  ok: boolean
  message: string
  sent: number
  recipients: string[]
  unassigned_classes: string[]
}

/** 一条手动标记流水 */
export interface MarkHistoryItem {
  id: number
  action: 'mark' | 'unmark'
  state: string
  reason: string
  by: string
  at: string
}

// ── 状态展示辅助 ────────────────────────────────────────
export const STATE_ORDER: StateName[] = ['严重异常', '异常', '今日未签到', '活跃', '无课']

export function levelOf(state: string): 'ok' | 'warn' | 'danger' {
  if (state === '严重异常') return 'danger'
  if (state === '异常') return 'warn'
  return 'ok'
}

/** 严格签到 / 旷课涂色映射（周课表用） */
export function slotClass(state: string, covered = true): string {
  // 没有考勤源的课单独一色：它既不是"已签到"也不是"旷课"，
  // 我们根本看不到这间教室。涂成红/绿都会误示示例辅导员。
  if (!covered) return 'nosource'
  switch (state) {
    case '已签到':
      return 'present'
    case '未签到':
      return 'missed'
    case '请假':
      return 'leave'
    case '进行中':
      return 'pending'
    default:
      return 'upcoming'
  }
}

export function fmtDate(iso: string | null): string {
  if (!iso) return '—'
  const [y, m, d] = iso.split('-')
  return `${y}-${m}-${d}`
}
