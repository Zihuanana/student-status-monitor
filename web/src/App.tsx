import { useEffect, useMemo, useRef, useState } from 'react'
// logo 走资源导入：前缀由构建器处理，换部署路径不必改代码
import logoLight from './assets/logo-00-02.svg'
import logoDark from './assets/logo-00-05.svg'
import {
  api,
  ApiError,
  type CounselorClass,
  type GroupData,
  type HealthData,
  type NotifyResult,
  type OverviewData,
  type MarkHistoryItem,
  type StudentCardData,
  type WeekData,
  type StateName,
  STATE_ORDER,
  fmtDate,
  levelOf,
  slotClass,
} from './api'

/* ─────────────────────────────────────────────────────────
   登录口令页
   ───────────────────────────────────────────────────────── */
function Gate({ onPass }: { onPass: () => void }) {
  const [pwd, setPwd] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    if (!pwd || busy) return
    setBusy(true)
    setErr('')
    try {
      await api.login(pwd)
      onPass()
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : '登录失败，请重试'
      setErr(msg === '口令不正确' ? '口令不正确，请重新输入' : msg)
      setPwd('')
      inputRef.current?.focus()
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="gate">
      <form className="gate-box" onSubmit={submit}>
        <div className="dialog-head">
          <span>登录 — 学生状态监测系统</span>
          <span className="spacer" />
        </div>
        <div className="gate-body">
          <p>本系统仅供授权教师使用，请输入访问口令。</p>
          <input
            ref={inputRef}
            className="gate-input"
            type="password"
            value={pwd}
            onChange={(e) => setPwd(e.target.value)}
            placeholder="请输入访问口令…"
            autoComplete="current-password"
          />
          {err && <div className="gate-err">⚠ {err}</div>}
          <div className="gate-row">
            <button className="xp-btn primary" type="submit" disabled={busy || !pwd}>
              {busy ? '验证中…' : '确定'}
            </button>
          </div>
        </div>
      </form>
    </div>
  )
}

/* ─────────────────────────────────────────────────────────
   学生状态卡片
   ───────────────────────────────────────────────────────── */
function StudentCard({
  stu,
  onClick,
}: {
  stu: StudentCardData
  onClick: (s: StudentCardData) => void
}) {
  const lv = stu.level || levelOf(stu.state)
  // 标记**不豁免**告警：标记后又连续无签到，照样要闪红光。
  // 只是多一个小标记提示"这个学示示例辅导员处理过"。
  const alarm = stu.state === '严重异常'
  // 标记之后那行显示标记时刻而不是「上次签到：无记录」——
  // 「无记录」配着一个活跃的绿牌子，老师看着别扭，也查不到依据。
  const lastLine = stu.manual_marked
    ? `手动标记：${stu.manual_marked_at ?? '—'}`
    : `上次签到：${stu.last_sign_in_time ?? '无记录'}`

  return (
    <button
      type="button"
      className={`card lv-${lv}${alarm ? ' alarm' : ''}${stu.manual_marked ? ' marked' : ''}`}
      onClick={() => onClick(stu)}
      title={
        `${stu.name}（${stu.student_no}）\n${stu.reason}\n` +
        (stu.manual_marked
          ? `已由${stu.manual_by || '老师'}于 ${stu.manual_marked_at ?? ''} 手动标记：${stu.manual_reason}\n`
          : '') +
        '点击查看本周课表与签到情况'
      }
    >
      <span className="c-top">
        <span className="c-name">{stu.name}</span>
        <span className="c-no">{stu.student_no}</span>
      </span>
      <span className="c-mid">
        <span className={`badge ${lv}`}>{stu.state}</span>
        {stu.consecutive_missed_days > 0 && (
          <span className="c-streak">连续{stu.consecutive_missed_days}天</span>
        )}
      </span>
      <span className={`c-last${stu.manual_marked ? ' c-mark-time' : ''}`}>{lastLine}</span>
      {/* 标记过的学生如果以前签到过，把上次签到单独补一行 —— 两个时间都有用 */}
      {stu.manual_marked && stu.last_sign_in_time && (
        <span className="c-class">上次签到：{stu.last_sign_in_time}</span>
      )}
      {stu.class_name && <span className="c-class">{stu.class_name}</span>}
    </button>
  )
}

/* ─────────────────────────────────────────────────────────
   分组（可展开 / 收起）
   ───────────────────────────────────────────────────────── */
function Group({
  group,
  open,
  onToggle,
  onPick,
}: {
  group: GroupData
  open: boolean
  onToggle: (key: string) => void
  onPick: (s: StudentCardData) => void
}) {
  const abnormal = (group.counts['异常'] ?? 0) + (group.counts['严重异常'] ?? 0)

  return (
    <section className="group">
      <header className="group-head" onClick={() => onToggle(group.key)}>
        <span className="caret">{open ? '▼' : '▶'}</span>
        <span className="gname">{group.key}</span>
        <span className="gtotal">共 {group.total} 人</span>
        <span className="spacer" style={{ flex: 1 }} />
        <span className="gcounts">
          {abnormal > 0 && (
            <span className="badge danger" title="该分组「异常」+「严重异常」合计">
              待关注 {abnormal}
            </span>
          )}
          {STATE_ORDER.filter((s) => (group.counts[s] ?? 0) > 0).map((s) => (
            <span key={s} className={`badge ${levelOf(s)}`}>
              {s} {group.counts[s]}
            </span>
          ))}
        </span>
      </header>
      {open && (
        <div className="group-body">
          {group.students.length === 0 ? (
            <div className="empty">该分组下没有符合条件的学生</div>
          ) : (
            group.students.map((s) => <StudentCard key={s.student_no} stu={s} onClick={onPick} />)
          )}
        </div>
      )}
    </section>
  )
}

/* ─────────────────────────────────────────────────────────
   周课表弹窗
   ───────────────────────────────────────────────────────── */
function WeekDialog({
  studentNo,
  onClose,
  onTop = false,
  onMarkChanged,
}: {
  studentNo: string
  onClose: () => void
  /** 从名单弹窗里打开时为 true：需要盖在名单之上，否则按钮会被蒙层挡住 */
  onTop?: boolean
  /** 标记 / 撤销成功后回调，让上层重拉总览（统计卡与卡片样式同步） */
  onMarkChanged?: () => void
}) {
  const [data, setData] = useState<WeekData | null>(null)
  const [anchor, setAnchor] = useState<string | undefined>(undefined)
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let alive = true
    setLoading(true)
    api
      .week(studentNo, anchor)
      .then((d) => {
        if (alive) {
          setData(d)
          setErr('')
        }
      })
      .catch((e) => {
        if (alive) setErr(e instanceof Error ? e.message : '加载失败')
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [studentNo, anchor])

  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [onClose])

  function shiftWeek(deltaDays: number) {
    const base = data ? new Date(`${data.week_start}T00:00:00`) : new Date()
    base.setDate(base.getDate() + deltaDays)
    const y = base.getFullYear()
    const m = String(base.getMonth() + 1).padStart(2, '0')
    const d = String(base.getDate()).padStart(2, '0')
    setAnchor(`${y}-${m}-${d}`)
  }

  return (
    <div className={`mask${onTop ? ' mask-top' : ''}`} onClick={onClose}>
      <div className="dialog" onClick={(e) => e.stopPropagation()}>
        <div className="dialog-head">
          <span>
            {data ? `${data.name} 的课表与签到情况` : '加载中…'}
          </span>
          <span className="spacer" />
          <button className="closer" onClick={onClose} title="关闭">
            ✕
          </button>
        </div>
        <div className="dialog-body">
          {err && <div className="notice err">⚠ {err}</div>}
          {data && (
            <>
              <div className="week-nav">
                <button className="xp-btn" onClick={() => shiftWeek(-7)}>
                  ◀ 上一周
                </button>
                <button className="xp-btn" onClick={() => setAnchor(undefined)}>
                  本周
                </button>
                <button className="xp-btn" onClick={() => shiftWeek(7)}>
                  下一周 ▶
                </button>
                <span style={{ color: 'var(--xp-ink-4)' }}>{data.week_label}</span>
                <span className="spacer" style={{ flex: 1 }} />
                <span className={`badge ${levelOf(data.state)}`}>{data.state}</span>
                <span style={{ color: 'var(--xp-ink-4)' }}>
                  学号 {data.student_no}
                </span>
              </div>

              <MarkBar
                studentNo={data.student_no}
                name={data.name}
                marked={data.manual_marked}
                reason={data.manual_reason}
                markedAt={data.manual_marked_at}
                by={data.manual_by}
                onChanged={(u) => {
                  setData((prev) =>
                    prev ? { ...prev, ...u, days: prev.days } : prev,
                  )
                  onMarkChanged?.()
                }}
              />

              <div className="notice info">
                判定说明：{data.reason}　·　绿色＝已签到，红色＝旷课，蓝色＝请假，灰色＝进行中／未开始
              </div>
              <div className="week-grid">
                {data.days.map((d) => (
                  <div key={d.date} className={`day${d.is_today ? ' today' : ''}`}>
                    <div className="day-head">
                      周{d.weekday}
                      <div className="d-date">{fmtDate(d.date).slice(5)}</div>
                    </div>
                    <div className="day-body">
                      {d.sessions.length === 0 ? (
                        <div className="day-empty">无课</div>
                      ) : (
                        <>
                          {d.sessions.map((s, i) => (
                            <div
                              key={`${s.schedule_guid}-${i}`}
                              className={`slot ${slotClass(s.state, s.covered !== false)}`}
                              title={
                                s.covered === false
                                  ? `${s.course_name} ${s.node_name} ${s.start}-${s.end} ${s.classroom}\n`
                                    + '无考勤源：这间教室的考勤我们看不到（录播系统未覆盖），\n'
                                    + '因此这节课不参与「未签到」判定'
                                  : `${s.course_name} ${s.node_name} ${s.start}-${s.end} ${s.classroom}\n状态：${s.state}${
                                      s.sign_in_time ? `（签到 ${s.sign_in_time}）` : ''
                                    }`
                              }
                            >
                              <span className="s-course">{s.course_name || '未命名课程'}</span>
                              <span className="s-meta">
                                {s.start}
                                {s.end ? `-${s.end}` : ''}{' '}
                                {s.covered === false ? '无考勤源' : s.state}
                              </span>
                            </div>
                          ))}
                          {(d.no_source_count ?? 0) > 0 && (
                            <div className="day-nosource">
                              {d.no_source_count} 节无考勤源，不参与判定
                            </div>
                          )}
                        </>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </>
          )}
          {loading && !data && <div className="empty">加载中…</div>}
        </div>
      </div>
    </div>
  )
}

/* ─────────────────────────────────────────────────────────
   手动标记：老师确认过的学生，暂时解除预警
   ───────────────────────────────────────────────────────── */
function MarkBar({
  studentNo,
  name,
  marked,
  reason,
  markedAt,
  by,
  onChanged,
}: {
  studentNo: string
  name: string
  marked?: boolean
  reason?: string
  markedAt?: string | null
  by?: string
  onChanged: (patch: Record<string, unknown>) => void
}) {
  const [open, setOpen] = useState(false)
  const [text, setText] = useState('')
  const [who, setWho] = useState('老师')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [history, setHistory] = useState<MarkHistoryItem[]>([])
  const [showHistory, setShowHistory] = useState(false)

  // 打开重标表单时把上次的原因填进去 —— 大多数情况下是"情况没变，再确认一次"
  function openForm() {
    setText(reason ?? '')
    setWho(by || '老师')
    setErr('')
    setOpen(true)
  }

  async function toggleHistory() {
    if (showHistory) {
      setShowHistory(false)
      return
    }
    try {
      const h = await api.markHistory(studentNo)
      setHistory(h.items)
      setShowHistory(true)
    } catch (e) {
      setErr(e instanceof Error ? e.message : '读取记录失败')
    }
  }

  async function submit() {
    if (!text.trim()) {
      setErr('请填写标记原因（会被记录下来）')
      return
    }
    setBusy(true)
    setErr('')
    try {
      const updated = await api.markStudent(studentNo, text.trim(), who.trim() || '老师')
      onChanged(updated as unknown as Record<string, unknown>)
      setOpen(false)
      setText('')
      // 重新标记后如果正开着记录，刷新一下，让新的一条立刻可见
      if (showHistory) setHistory((await api.markHistory(studentNo)).items)
    } catch (e) {
      setErr(e instanceof Error ? e.message : '标记失败')
    } finally {
      setBusy(false)
    }
  }

  async function undo() {
    setBusy(true)
    setErr('')
    try {
      const updated = await api.unmarkStudent(studentNo)
      onChanged(updated as unknown as Record<string, unknown>)
      setShowHistory(false)
    } catch (e) {
      setErr(e instanceof Error ? e.message : '撤销失败')
    } finally {
      setBusy(false)
    }
  }

  // ── 标记表单（首次标记与重新标记共用）──
  const form = (
    <>
      <input
        className="mark-input"
        placeholder={`标记「${name}」的原因，例如：已请假，辅导员确认`}
        value={text}
        maxLength={255}
        autoFocus
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') submit()
          if (e.key === 'Escape') setOpen(false)
        }}
      />
      <input
        className="mark-who"
        placeholder="标记人"
        value={who}
        maxLength={64}
        onChange={(e) => setWho(e.target.value)}
      />
      <button className="xp-btn primary" onClick={submit} disabled={busy}>
        {busy ? '提交中…' : marked ? '确认重新标记' : '确认标记'}
      </button>
      <button className="xp-btn" onClick={() => setOpen(false)} disabled={busy}>
        取消
      </button>
    </>
  )

  const historyList = showHistory && (
    <div className="mark-history">
      {history.length === 0 ? (
        <div className="mh-empty">暂无标记记录</div>
      ) : (
        history.map((h) => (
          <div key={h.id} className={`mh-row ${h.action}`}>
            <span className="mh-at">{h.at}</span>
            <span className="mh-act">{h.action === 'mark' ? '标记' : '撤销'}</span>
            <span className="mh-by">{h.by}</span>
            <span className="mh-reason">{h.reason || '（无原因）'}</span>
          </div>
        ))
      )}
    </div>
  )

  if (marked) {
    return (
      <div className="mark-block">
        <div className="mark-bar marked">
          <span className="mark-flag">✅ 已手动标记为活跃</span>
          <span className="mark-note">
            这次标记<b>视作一次签到</b>（相当于把标记之前的无签到清零）。
            它不是豁免：之后再有有课日没签到，会照常报异常。
            {markedAt && `（${by || '老师'} 于 ${markedAt} 标记：${reason}）`}
          </span>
          <span className="spacer" style={{ flex: 1 }} />
          {/* 重新标记是刚需：情况没变也要"再确认一次"，否则只能先撤销再标，
              中间那一下会短暂失去保护，而且流水里会多出一条无意义的撤销 */}
          <button className="xp-btn" onClick={openForm} disabled={busy}>
            重新标记
          </button>
          <button className="xp-btn" onClick={toggleHistory} disabled={busy}>
            {showHistory ? '收起记录' : '标记记录'}
          </button>
          <button className="xp-btn" onClick={undo} disabled={busy}>
            撤销标记
          </button>
          {err && <span className="mark-err">⚠ {err}</span>}
        </div>
        {open && <div className="mark-bar marked">{form}</div>}
        {historyList}
      </div>
    )
  }

  return (
    <div className="mark-bar">
      {!open ? (
        <>
          <span className="mark-note">
            确认这个学生的情况（请假/实习/已核实等）后，可手动标记为活跃，
            把之前的连续无签到清零。标记<b>不等于豁免</b> —— 之后仍没签到会继续报。
          </span>
          <span className="spacer" style={{ flex: 1 }} />
          <button className="xp-btn" onClick={openForm} disabled={busy}>
            手动标记为活跃
          </button>
        </>
      ) : (
        form
      )}
      {err && <span className="mark-err">⚠ {err}</span>}
    </div>
  )
}

/* ─────────────────────────────────────────────────────────
   数据健康度：老师需要一眼看出"页面上的状态是不是最新的"
   ───────────────────────────────────────────────────────── */
function HealthChip({ health }: { health: HealthData | null }) {
  if (!health) return null

  const age = health.last_success_age_minutes
  const ageText =
    age === null ? '从未成功' : age < 1 ? '刚刚' : age < 60 ? `${Math.round(age)} 分钟前` : `${(age / 60).toFixed(1)} 小时前`

  const level = health.unhealthy ? 'danger' : health.stale ? 'warn' : 'ok'
  const label = health.unhealthy ? '采集异常' : health.stale ? '数据可能过时' : '数据正常'

  return (
    <span
      className={`health-chip ${level}`}
      title={
        `最近一次采集：${health.checked_at}\n` +
        `上次成功采集：${ageText}\n` +
        `连续失败轮数：${health.consecutive_failures}\n` +
        `后台轮询：${health.polling ? '运行中' : '未运行'}\n` +
        `最近消息：${health.last_message || '（无）'}`
      }
    >
      <span className="dot" />
      {label}（{ageText}）
    </span>
  )
}

function HealthBanner({ health }: { health: HealthData | null }) {
  if (!health || !health.unhealthy) return null

  const age = health.last_success_age_minutes
  return (
    <div className="notice err">
      <b>⚠ 采集异常：页面上的状态可能已经不是最新的。</b>
      <div style={{ marginTop: 3 }}>
        已连续 <b>{health.consecutive_failures}</b> 轮未取到数据，
        距上次成功采集 <b>{age === null ? '从未成功' : `${Math.round(age)} 分钟`}</b>。
        {health.last_message ? <>最近报错：{health.last_message}</> : null}
      </div>
      <div style={{ marginTop: 3, color: 'var(--xp-ink-4)' }}>
        请检查考勤接口凭据是否过期、服务器网络是否正常；修复后会立即恢复。
      </div>
    </div>
  )
}

/* ─────────────────────────────────────────────────────────
   状态名单弹窗：点统计卡后可搜、可筛、可排序的学生表格
   ───────────────────────────────────────────────────────── */
type SortKey = 'student_no' | 'state' | 'consecutive_missed_days' | 'last_sign_in_time'

function StatusDialog({
  title,
  students,
  onClose,
  onPick,
}: {
  title: string
  students: StudentCardData[]
  onClose: () => void
  onPick: (studentNo: string) => void
}) {
  const [kw, setKw] = useState('')
  const [klass, setKlass] = useState('')
  const [sortKey, setSortKey] = useState<SortKey>('student_no')
  const [sortAsc, setSortAsc] = useState(true)

  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [onClose])

  // 选项来自当前名单本身，不会出现"选了却没人"的空筛选项
  const classes = useMemo(
    () => Array.from(new Set(students.map((s) => s.class_name).filter(Boolean))).sort(),
    [students],
  )

  const rows = useMemo(() => {
    const q = kw.trim().toLowerCase()
    let out = students.filter((s) => {
      if (klass && s.class_name !== klass) return false
      if (!q) return true
      return (
        s.name.toLowerCase().includes(q) ||
        s.student_no.toLowerCase().includes(q) ||
        s.class_name.toLowerCase().includes(q) ||
        s.major_name.toLowerCase().includes(q)
      )
    })
    const dir = sortAsc ? 1 : -1
    out = [...out].sort((a, b) => {
      if (sortKey === 'consecutive_missed_days') {
        return (a.consecutive_missed_days - b.consecutive_missed_days) * dir
      }
      const av = (a[sortKey] ?? '') as string
      const bv = (b[sortKey] ?? '') as string
      return av.localeCompare(bv, 'zh-CN') * dir
    })
    return out
  }, [students, kw, klass, sortKey, sortAsc])

  function toggleSort(key: SortKey) {
    if (sortKey === key) setSortAsc((v) => !v)
    else {
      setSortKey(key)
      setSortAsc(key === 'student_no')
    }
  }

  function resetFilters() {
    setKw('')
    setKlass('')
  }

  const hasFilter = Boolean(kw || klass)
  const arrow = (key: SortKey) => (sortKey === key ? (sortAsc ? ' ▲' : ' ▼') : '')

  return (
    <div className="mask" onClick={onClose}>
      <div className="dialog dialog-wide" onClick={(e) => e.stopPropagation()}>
        <div className="dialog-head">
          <span>
            {title}　共 {rows.length} 人
            {rows.length !== students.length && (
              <span style={{ fontWeight: 400, color: 'var(--text-muted)' }}>
                （已从 {students.length} 人中筛选）
              </span>
            )}
          </span>
          <span className="spacer" />
          <button className="closer" onClick={onClose} title="关闭">
            ✕
          </button>
        </div>

        <div className="dialog-body">
          <div className="table-toolbar">
            <input
              className="gate-input"
              style={{ width: 220, height: 34 }}
              placeholder="搜索姓名 / 学号 / 班级…"
              value={kw}
              onChange={(e) => setKw(e.target.value)}
              autoFocus
            />
            <select className="select" value={klass} onChange={(e) => setKlass(e.target.value)}>
              <option value="">全部班级</option>
              {classes.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
            {hasFilter && (
              <button className="xp-btn" onClick={resetFilters}>
                ✕ 清除筛选
              </button>
            )}
            <span className="spacer" />
            <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>
              点表头排序，点行查看课表
            </span>
          </div>

          {rows.length === 0 ? (
            <div className="empty">没有匹配的学生，试试其他关键词或清除筛选。</div>
          ) : (
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th className="sortable" onClick={() => toggleSort('student_no')}>
                      学号{arrow('student_no')}
                    </th>
                    <th>姓名</th>
                    <th>年级</th>
                    <th>专业</th>
                    <th>班级</th>
                    <th className="sortable" onClick={() => toggleSort('state')}>
                      状态{arrow('state')}
                    </th>
                    <th
                      className="sortable num-col"
                      onClick={() => toggleSort('consecutive_missed_days')}
                    >
                      连续无签到{arrow('consecutive_missed_days')}
                    </th>
                    <th className="sortable" onClick={() => toggleSort('last_sign_in_time')}>
                      上次签到{arrow('last_sign_in_time')}
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((s) => (
                    <tr
                      key={s.student_no}
                      className="clickable"
                      tabIndex={0}
                      onClick={() => onPick(s.student_no)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter' || e.key === ' ') {
                          e.preventDefault()
                          onPick(s.student_no)
                        }
                      }}
                      title={`${s.reason}\n点击查看本周课表与签到情况`}
                    >
                      <td className="mono">{s.student_no}</td>
                      <td className="strong">{s.name}</td>
                      <td>{s.grade}</td>
                      <td>{s.major_name}</td>
                      <td>{s.class_name}</td>
                      <td>
                        <span className={`badge ${levelOf(s.state)}`}>{s.state}</span>
                      </td>
                      <td className="num-col">
                        {s.consecutive_missed_days > 0 ? `${s.consecutive_missed_days} 天` : '—'}
                      </td>
                      <td className="mono">{s.last_sign_in_time ?? '无记录'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

/* ─────────────────────────────────────────────────────────
   辅导员联系人 —— 预警发给谁
   ───────────────────────────────────────────────────────── */
/**
 * 按班配置预警收件人（每班 3 个位置）。
 *
 * 为什么要按班配：辅导员是管班的。学生类预警（无记录 / 长期缺少记录 /
 * 覆盖不足）发给该生所在班示示例辅导员；数据类预警（接口失败、课表缺失）
 * 发给管理员。
 *
 * **没示示例辅导员的班，预警会回落到管理员**并在邮件里写明要补配置 ——
 * 绝不静默丢掉（那会让一批学生永远没人管，而系统看起来正常）。
 * 所以这个弹窗把「还没配」的班显眼地列在最上面。
 */
function CounselorDialog({ onClose }: { onClose: () => void }) {
  const [rows, setRows] = useState<CounselorClass[]>([])
  const [loading, setLoading] = useState(true)
  const [savingKey, setSavingKey] = useState<string | null>(null)
  const [err, setErr] = useState('')
  const [okMsg, setOkMsg] = useState('')
  const [busy, setBusy] = useState(false)
  const [onlyMissing, setOnlyMissing] = useState(true)
  /** 本地编辑缓冲：班级键 → 三个位置 + 备注（未保存的改动） */
  const [draft, setDraft] = useState<Record<string, { slot1: string; slot2: string; slot3: string; note: string }>>({})

  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [onClose])

  async function load() {
    setLoading(true)
    setErr('')
    try {
      setRows(await api.counselors())
      setDraft({})
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load()
  }, [])

  function valueOf(r: CounselorClass, slot: 'slot1' | 'slot2' | 'slot3'): string {
    return draft[r.class_key]?.[slot] ?? r[slot]
  }

  function noteOf(r: CounselorClass): string {
    return draft[r.class_key]?.note ?? r.note
  }

  function setValue(r: CounselorClass, slot: 'slot1' | 'slot2' | 'slot3', v: string) {
    setDraft((prev) => ({
      ...prev,
      [r.class_key]: {
        slot1: prev[r.class_key]?.slot1 ?? r.slot1,
        slot2: prev[r.class_key]?.slot2 ?? r.slot2,
        slot3: prev[r.class_key]?.slot3 ?? r.slot3,
        note: prev[r.class_key]?.note ?? r.note,
        [slot]: v,
      },
    }))
  }

  function setNote(r: CounselorClass, v: string) {
    setDraft((prev) => ({
      ...prev,
      [r.class_key]: {
        slot1: prev[r.class_key]?.slot1 ?? r.slot1,
        slot2: prev[r.class_key]?.slot2 ?? r.slot2,
        slot3: prev[r.class_key]?.slot3 ?? r.slot3,
        note: v,
      },
    }))
  }

  function isConfigured(r: CounselorClass): boolean {
    const d = draft[r.class_key]
    const v = d ?? r
    return Boolean(v.slot1.trim() || v.slot2.trim() || v.slot3.trim())
  }

  function isDirty(r: CounselorClass): boolean {
    const d = draft[r.class_key]
    if (!d) return false
    return (
      d.slot1 !== r.slot1 || d.slot2 !== r.slot2 || d.slot3 !== r.slot3 || d.note !== r.note
    )
  }

  async function save(r: CounselorClass) {
    const d = draft[r.class_key] ?? {
      slot1: r.slot1, slot2: r.slot2, slot3: r.slot3, note: r.note,
    }
    setSavingKey(r.class_key)
    setErr('')
    setOkMsg('')
    try {
      await api.saveCounselor({ class_name: r.class_name, ...d })
      setOkMsg(`已保存「${r.class_name}」`)
      await load()
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : '保存失败')
    } finally {
      setSavingKey(null)
    }
  }

  async function notify(dryRun: boolean) {
    setBusy(true)
    setErr('')
    setOkMsg('')
    try {
      const res: NotifyResult = await api.notifyWarnings(dryRun)
      const head = dryRun ? '【预演】' : ''
      let msg = `${head}${res.message}`
      if (res.unassigned_classes.length) {
        msg += `\n仍没示示例辅导员的班（预警已发给管理员）：${res.unassigned_classes.join('、')}`
      }
      setOkMsg(msg)
      if (!dryRun && res.ok) await load()
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : '发送失败')
    } finally {
      setBusy(false)
    }
  }

  const missing = useMemo(() => rows.filter((r) => !isConfigured(r)), [rows, draft])
  const shown = onlyMissing ? missing : rows

  return (
    <div className="mask" onClick={onClose}>
      <div className="dialog dialog-wide" onClick={(e) => e.stopPropagation()}>
        <div className="dialog-head">
          <span>
            辅导员联系人（预警收件人）
            <span style={{ fontWeight: 400, color: 'var(--text-muted)' }}>
              　共 {rows.length} 个班
              {missing.length > 0 && `，${missing.length} 个班还没配`}
            </span>
          </span>
          <span className="spacer" />
          <button className="closer" onClick={onClose} title="关闭">
            ✕
          </button>
        </div>

        <div className="dialog-body">
          <div className="notice info" style={{ marginBottom: 10 }}>
            <b>学生类预警</b>（无记录 / 长期缺少记录 / 覆盖不足）发给该生<b>所在班示示例辅导员</b>；
            <b>数据类预警</b>（接口失败、课表缺失）发给管理员。每班可填 <b>3 个</b>邮箱。
            <br />
            班级没示示例辅导员时，该班预警会<b>回落到管理员</b>（不会丢），并在邮件里提示补配置。
          </div>

          <div className="table-toolbar">
            <button
              className={`xp-btn${onlyMissing ? ' toggled' : ''}`}
              onClick={() => setOnlyMissing((v) => !v)}
            >
              {onlyMissing ? '✓ ' : ''}只看没配的（{missing.length}）
            </button>
            <button className="xp-btn" onClick={() => void load()} disabled={loading}>
              刷新
            </button>
            <span className="sep" />
            <button
              className="xp-btn"
              onClick={() => void notify(true)}
              disabled={busy}
              title="只算会发给谁、各多少条，不发信 —— 配错邮箱时先看这个"
            >
              预演分派
            </button>
            <button
              className="xp-btn"
              onClick={() => void notify(false)}
              disabled={busy}
              title="立刻按上面的配置把当前预警发出去（不等每日定时）"
            >
              {busy ? '发送中…' : '立即发送预警邮件'}
            </button>
            <span className="spacer" style={{ flex: 1 }} />
            <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>
              改动后点该行的「保存」
            </span>
          </div>

          {err && <div className="notice err">⚠ {err}</div>}
          {okMsg && (
            <div className="notice info" style={{ whiteSpace: 'pre-wrap' }}>
              {okMsg}
            </div>
          )}
          {loading && <div className="empty">加载中…</div>}
          {!loading && shown.length === 0 && (
            <div className="empty">
              {onlyMissing ? '所有班级都配示示例辅导员了 ✓' : '没有班级数据'}
            </div>
          )}

          {!loading && shown.length > 0 && (
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th style={{ minWidth: 220 }}>班级</th>
                    <th className="num-col">人数</th>
                    <th>辅导员邮箱 ①</th>
                    <th>辅导员邮箱 ②</th>
                    <th>辅导员邮箱 ③</th>
                    <th>备注</th>
                    <th style={{ width: 78 }}>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {shown.map((r) => {
                    const dirty = isDirty(r)
                    const configured = isConfigured(r)
                    return (
                      <tr key={r.class_key}>
                        <td>
                          {r.class_name}
                          {!configured && (
                            <span className="badge warn" style={{ marginLeft: 6 }}>
                              未配
                            </span>
                          )}
                        </td>
                        <td className="num-col">{r.students}</td>
                        {(['slot1', 'slot2', 'slot3'] as const).map((slot) => (
                          <td key={slot}>
                            <input
                              className="gate-input"
                              style={{ width: '100%', height: 26, minWidth: 150 }}
                              placeholder="邮箱"
                              value={valueOf(r, slot)}
                              onChange={(e) => setValue(r, slot, e.target.value)}
                            />
                          </td>
                        ))}
                        <td>
                          <input
                            className="gate-input"
                            style={{ width: '100%', height: 26, minWidth: 100 }}
                            placeholder="如：示示例辅导员"
                            value={noteOf(r)}
                            onChange={(e) => setNote(r, e.target.value)}
                          />
                        </td>
                        <td>
                          <button
                            className="xp-btn"
                            onClick={() => void save(r)}
                            disabled={savingKey === r.class_key || !dirty}
                            title={dirty ? '保存这一行' : '没有改动'}
                          >
                            {savingKey === r.class_key ? '…' : '保存'}
                          </button>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

/** 总览统计卡定义：state=null 表示「在册学生」合计 */
const STAT_CARDS: { key: string; label: string; state: StateName | null; level: string }[] = [
  { key: 'all', label: '在册学生', state: null, level: '' },
  { key: '活跃', label: '活跃', state: '活跃', level: 'ok' },
  { key: '无课', label: '无课', state: '无课', level: 'ok' },
  { key: '今日未签到', label: '今日未签到', state: '今日未签到', level: 'ok' },
  { key: '异常', label: '异常', state: '异常', level: 'warn' },
  { key: '严重异常', label: '严重异常', state: '严重异常', level: 'danger' },
]

/* ─────────────────────────────────────────────────────────
   仪表盘
   ───────────────────────────────────────────────────────── */
function Dashboard({ onLogout, version }: { onLogout: () => void; version: string }) {
  const [data, setData] = useState<OverviewData | null>(null)
  const [health, setHealth] = useState<HealthData | null>(null)
  const [rosterFor, setRosterFor] = useState<string | null>(null)
  const [openKeys, setOpenKeys] = useState<Set<string>>(new Set())
  const [onlyAbnormal, setOnlyAbnormal] = useState(false)
  const [keyword, setKeyword] = useState('')
  const [picked, setPicked] = useState<string | null>(null)
  const [counselorOpen, setCounselorOpen] = useState(false)
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(true)
  const [autoRefresh, setAutoRefresh] = useState(true)
  const [clock, setClock] = useState(() => new Date())

  async function load(show: 'all' | 'abnormal', kw: string, first = false) {
    if (first) setLoading(true)
    try {
      // 状态与体检并行拉取：体检失败不应影响主数据展示
      const [d, h] = await Promise.all([
        api.overview(show, kw),
        api.health().catch(() => null),
      ])
      setData(d)
      if (h) setHealth(h)
      setErr('')
      if (first) {
        // 默认全部展开，异常分组排在最前（后端已按年级倒序返回）
        setOpenKeys(new Set(d.groups.map((g) => g.key)))
      }
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        onLogout()
        return
      }
      setErr(e instanceof Error ? e.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }

  // 首次加载 + 筛选变化
  useEffect(() => {
    load(onlyAbnormal ? 'abnormal' : 'all', keyword, data === null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [onlyAbnormal])

  // 自动刷新（每 60s）
  useEffect(() => {
    if (!autoRefresh) return
    const t = window.setInterval(() => {
      load(onlyAbnormal ? 'abnormal' : 'all', keyword, false)
    }, 60000)
    return () => window.clearInterval(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoRefresh, onlyAbnormal, keyword])

  // 时钟
  useEffect(() => {
    const t = window.setInterval(() => setClock(new Date()), 1000)
    return () => window.clearInterval(t)
  }, [])

  const groups = data?.groups ?? []
  const counts = data?.counts ?? {}
  const totalShown = data?.totals.students ?? 0

  // 全量学生（不随搜索/筛选变化）：点统计卡看名单时用这份，
  // 否则"点了 168 人却只看到 20 个"会让人以为数据丢了。
  const allStudents = useMemo(() => groups.flatMap((g) => g.students), [groups])
  const abnormalCount = (counts['异常'] ?? 0) + (counts['严重异常'] ?? 0)

  const hasGroups = groups.length > 0

  function toggleGroup(key: string) {
    setOpenKeys((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  const expandAll = () => setOpenKeys(new Set(groups.map((g) => g.key)))
  const collapseAll = () => setOpenKeys(new Set())

  const clockText = useMemo(
    () =>
      `${clock.getFullYear()}-${String(clock.getMonth() + 1).padStart(2, '0')}-${String(
        clock.getDate(),
      ).padStart(2, '0')} ${String(clock.getHours()).padStart(2, '0')}:${String(
        clock.getMinutes(),
      ).padStart(2, '0')}:${String(clock.getSeconds()).padStart(2, '0')}`,
    [clock],
  )

  return (
    <>
      <header className="xp-header">
        <div className="xp-header-inner">
          {/* logo 用 Vite 的**资源导入**，不要写死路径。
              踩过的坑：原来写死成云端的隐藏路径
              （/3425jh3g…/logo/...），迁到内网挂 /monitor/ 后 logo 404。
              改成 `src="logo/..."` 也不行 —— 那是相对**页面 URL** 解析的，
              访问 /monitor（不带尾斜杠）时会算到站点根 /logo/ 上去。
              用 import 让构建器处理前缀，换部署路径就不用再动代码。 */}
          <picture className="brand-logo">
            <source srcSet={logoDark} media="(prefers-color-scheme: dark)" />
            <img src={logoLight} alt="中国传媒大学海南国际学院" />
          </picture>
          <span className="title">学生状态监测系统</span>
          <span className="subtitle">中国传媒大学海南国际学院</span>
          <span className="spacer" />
          <HealthChip health={health} />
          <span className="xp-clock">
            数据更新：{data?.generated_at ?? '—'}
          </span>
          <span className="xp-clock">共 {totalShown} 人</span>
        </div>
      </header>

      <div className="wrap">
        {err && <div className="notice err">⚠ {err}</div>}
        <HealthBanner health={health} />
        {onlyAbnormal && (
          <div className="notice info">
            当前只显示 <b>今日待处理</b>名单：共 {abnormalCount} 人
            （严重异常 {counts['严重异常'] ?? 0} 人，异常 {counts['异常'] ?? 0} 人）。
            这份名单与每日汇总邮件内容一致 —— 学生一旦签到就会自动移出。
            <span className="spacer" style={{ flex: 1 }} />
            <button className="xp-btn" onClick={() => setOnlyAbnormal(false)}>
              ✕ 显示全部
            </button>
          </div>
        )}
        {data?.data_mode === 'mock' && (
          <div className="notice warn">
            当前为 <b>模拟数据模式</b>（未接入真实考勤接口）。界面与判定逻辑均为真实实现，
            接入信息化处提供的接口凭据后即可切换到真实数据。
          </div>
        )}

        <div className="panel">
          <div className="panel-title">
            <span>总览</span>
            <span className="spacer" />
            <span className="legend">
              {STATE_ORDER.map((s) => (
                <span key={s} className={`badge ${levelOf(s)}`}>
                  {s} {counts[s] ?? 0}
                </span>
              ))}
            </span>
          </div>

          <div className="stats">
            {STAT_CARDS.map((card) => {
              const n = card.state === null ? totalShown : (counts[card.state] ?? 0)
              const active = rosterFor === card.key
              return (
                <button
                  key={card.key}
                  type="button"
                  className={`stat ${card.level}${n === 0 ? ' zero' : ''}${active ? ' active' : ''}`}
                  onClick={() => setRosterFor(card.key)}
                  title={`点击查看「${card.label}」的完整名单（可搜索、筛选、排序）`}
                  disabled={n === 0}
                >
                  <span className="num">{n}</span>
                  <span className="lbl">{card.label}</span>
                </button>
              )
            })}
          </div>

          <div className="toolbar">
            <button className="xp-btn" onClick={expandAll} disabled={!hasGroups}>
              全部展开
            </button>
            <button className="xp-btn" onClick={collapseAll} disabled={!hasGroups}>
              全部收起
            </button>
            <span className="sep" />
            <button
              className={`xp-btn${onlyAbnormal ? ' toggled' : ''}`}
              onClick={() => setOnlyAbnormal((v) => !v)}
              title="只显示当前仍未恢复正常的学生（与每日汇总邮件、/api/alerts/current 一致）"
            >
              {onlyAbnormal ? '✓ ' : ''}今日待处理（{abnormalCount}）
            </button>
            <span className="sep" />
            <input
              className="gate-input"
              style={{ width: 190, height: 22 }}
              placeholder="按姓名或学号搜索…"
              value={keyword}
              onChange={(e) => setKeyword(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') load(onlyAbnormal ? 'abnormal' : 'all', keyword, false)
              }}
            />
            <button className="xp-btn" onClick={() => load(onlyAbnormal ? 'abnormal' : 'all', keyword, false)}>
              搜索
            </button>
            {keyword && (
              <button
                className="xp-btn"
                onClick={() => {
                  setKeyword('')
                  load(onlyAbnormal ? 'abnormal' : 'all', '', false)
                }}
              >
                ✕ 清除
              </button>
            )}
            <span className="sep" />
            <button className="xp-btn" onClick={() => load(onlyAbnormal ? 'abnormal' : 'all', keyword, false)}>
              刷新
            </button>
            <button
              className={`xp-btn${autoRefresh ? ' toggled' : ''}`}
              onClick={() => setAutoRefresh((v) => !v)}
              title="每 60 秒自动刷新一次"
            >
              {autoRefresh ? '✓ ' : ''}自动刷新
            </button>
            <span className="spacer" style={{ flex: 1 }} />
            <button
              className="xp-btn"
              onClick={() => setCounselorOpen(true)}
              title="按班配置预警收件人（辅导员），并手动发送预警邮件"
            >
              辅导员联系人 / 发预警
            </button>
            <button className="xp-btn" onClick={onLogout}>
              退出
            </button>
          </div>
        </div>

        {loading && !data && <div className="empty">加载中…</div>}
        {!loading && !hasGroups && (
          <div className="panel">
            <div className="empty">
              {onlyAbnormal
                ? '当前没有异常学生 —— 一切正常。'
                : '没有匹配的学生，试试其他关键词。'}
            </div>
          </div>
        )}

        {groups.map((g) => (
          <Group
            key={g.key}
            group={g}
            open={openKeys.has(g.key)}
            onToggle={toggleGroup}
            onPick={(s) => setPicked(s.student_no)}
          />
        ))}

        <div className="footer">
          学生状态监测系统 v{version}　·　异常判定：连续三天以上有课日整日无签到即告警
        </div>
      </div>

      {picked && (
        <WeekDialog
          studentNo={picked}
          onClose={() => setPicked(null)}
          // 名单弹窗还开着时，课表要盖在它上面
          onTop={rosterFor !== null}
          // 标记/撤销后必须重拉总览：否则统计卡和卡片样式还是旧的，
          // 老师会以为"点了没反应"，然后再点一次
          onMarkChanged={() =>
            void load(onlyAbnormal ? 'abnormal' : 'all', keyword, false)
          }
        />
      )}
      {counselorOpen && <CounselorDialog onClose={() => setCounselorOpen(false)} />}
      {rosterFor && (
        <StatusDialog
          title={STAT_CARDS.find((c) => c.key === rosterFor)?.label ?? rosterFor}
          // 名单始终基于**全量**数据，与搜索/「今日待处理」筛选无关
          students={
            rosterFor === 'all'
              ? allStudents
              : allStudents.filter((s) => s.state === rosterFor)
          }
          onClose={() => setRosterFor(null)}
          onPick={(no) => setPicked(no)}
        />
      )}

      <div className="taskbar">
        <button className="start-btn" onClick={() => load(onlyAbnormal ? 'abnormal' : 'all', keyword, false)}>
          开 始
        </button>
        {/* 采集异常单独占一段：它比"学生异常"更紧急 ——
            数据不新鲜时，下面的学生状态本身就不可信 */}
        {health?.unhealthy && (
          <span className="tb-alert">⚠ 采集异常，数据可能过时</span>
        )}
        <span className="tb-text">
          {abnormalCount > 0
            ? `⚠ 当前有 ${abnormalCount} 名学生状态异常`
            : '✓ 当前无异常学生'}
        </span>
        <span className="spacer" />
        <span className="tb-text">
          {data?.data_mode === 'mock' ? '模拟数据' : '实时数据'}
        </span>
        <span className="tb-clock">{clockText}</span>
      </div>
    </>
  )
}

/* ─────────────────────────────────────────────────────────
   根组件
   ───────────────────────────────────────────────────────── */
export default function App() {
  const [ready, setReady] = useState(false)
  const [authed, setAuthed] = useState(false)
  const [version, setVersion] = useState('0.1.0')

  useEffect(() => {
    api
      .session()
      .then((s) => {
        setAuthed(s.authenticated)
        setVersion(s.version)
      })
      .catch(() => setAuthed(false))
      .finally(() => setReady(true))
  }, [])

  async function logout() {
    try {
      await api.logout()
    } catch {
      /* 忽略 */
    }
    setAuthed(false)
  }

  if (!ready) {
    return (
      <div className="gate">
        <div className="gate-box">
          <div className="dialog-head">
            <span>学生状态监测系统</span>
          </div>
          <div className="gate-body">
            <p>正在连接服务器…</p>
          </div>
        </div>
      </div>
    )
  }

  if (!authed) return <Gate onPass={() => setAuthed(true)} />

  return <Dashboard onLogout={logout} version={version} />
}
