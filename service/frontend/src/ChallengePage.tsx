import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { Link, useParams } from 'react-router-dom'

import { API_BASE, MARKETS, useLanguage } from './appShared'

type ChallengePageProps = {
  token?: string | null
}

const statusValues = ['upcoming', 'active', 'settled'] as const

function formatPct(value: any) {
  return `${Number(value || 0).toFixed(2)}%`
}

function formatMoney(value: any) {
  return `$${Number(value || 0).toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 0 })}`
}

function formatDate(value: string | null | undefined, language: string) {
  if (!value) return '-'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return parsed.toLocaleString(language === 'zh' ? 'zh-CN' : 'en-US', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit'
  })
}

function marketLabel(value: string, language: string) {
  return MARKETS.find((market) => market.value === value)?.[language === 'zh' ? 'labelZh' : 'label'] || value
}

export function ChallengePage({ token }: ChallengePageProps) {
  const { challengeKey } = useParams()
  const { language } = useLanguage()
  const [status, setStatus] = useState<'upcoming' | 'active' | 'settled'>('active')
  const [challenges, setChallenges] = useState<any[]>([])
  const [detail, setDetail] = useState<any | null>(null)
  const [leaderboard, setLeaderboard] = useState<any[]>([])
  const [submissions, setSubmissions] = useState<any[]>([])
  const [myChallenges, setMyChallenges] = useState<any[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [showCreate, setShowCreate] = useState(false)
  const [createForm, setCreateForm] = useState({
    title: '',
    challenge_key: '',
    market: 'crypto',
    symbol: 'BTC',
    scoring_method: 'return-only',
    max_position_pct: '100',
    max_drawdown_pct: '20',
    end_at: ''
  })
  const [submissionContent, setSubmissionContent] = useState('')

  const joinedChallengeIds = useMemo(
    () => new Set(myChallenges.map((item) => item.id)),
    [myChallenges]
  )

  const loadMyChallenges = async () => {
    if (!token) {
      setMyChallenges([])
      return
    }
    try {
      const res = await fetch(`${API_BASE}/challenges/me`, {
        headers: { 'Authorization': `Bearer ${token}` }
      })
      if (!res.ok) return
      const data = await res.json()
      setMyChallenges(data.challenges || [])
    } catch (e) {
      console.error(e)
    }
  }

  const loadList = async () => {
    setLoading(true)
    try {
      const res = await fetch(`${API_BASE}/challenges?status=${status}&limit=100`)
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || 'challenge_load_failed')
      setChallenges(data.challenges || [])
      setError(null)
    } catch (err: any) {
      setError(err?.message || (language === 'zh' ? '挑战加载失败' : 'Failed to load challenges'))
      setChallenges([])
    } finally {
      setLoading(false)
    }
  }

  const loadDetail = async () => {
    if (!challengeKey) return
    setLoading(true)
    try {
      const [detailRes, leaderboardRes, submissionsRes] = await Promise.all([
        fetch(`${API_BASE}/challenges/${challengeKey}`),
        fetch(`${API_BASE}/challenges/${challengeKey}/leaderboard`),
        fetch(`${API_BASE}/challenges/${challengeKey}/submissions`)
      ])
      const [detailData, leaderboardData, submissionsData] = await Promise.all([
        detailRes.json(),
        leaderboardRes.json(),
        submissionsRes.json()
      ])
      if (!detailRes.ok) throw new Error(detailData.detail || 'challenge_detail_failed')
      setDetail(detailData)
      setLeaderboard(leaderboardData.leaderboard || [])
      setSubmissions(submissionsData.submissions || [])
      setError(null)
    } catch (err: any) {
      setError(err?.message || (language === 'zh' ? '挑战详情加载失败' : 'Failed to load challenge detail'))
      setDetail(null)
      setLeaderboard([])
      setSubmissions([])
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    if (challengeKey) {
      loadDetail()
    } else {
      loadList()
    }
    loadMyChallenges()
  }, [challengeKey, status, token])

  const handleJoin = async (key: string) => {
    if (!token) return
    setBusy(true)
    try {
      const res = await fetch(`${API_BASE}/challenges/${key}/join`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${token}`
        },
        body: JSON.stringify({})
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || 'join_failed')
      await Promise.all([loadMyChallenges(), challengeKey ? loadDetail() : loadList()])
    } catch (err: any) {
      alert(err?.message || (language === 'zh' ? '加入挑战失败' : 'Failed to join challenge'))
    } finally {
      setBusy(false)
    }
  }

  const handleCreate = async (e: FormEvent) => {
    e.preventDefault()
    if (!token) return
    setBusy(true)
    try {
      const endAt = createForm.end_at ? new Date(createForm.end_at).toISOString() : undefined
      const res = await fetch(`${API_BASE}/challenges`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${token}`
        },
        body: JSON.stringify({
          ...createForm,
          challenge_key: createForm.challenge_key || undefined,
          symbol: createForm.symbol || undefined,
          end_at: endAt,
          max_position_pct: Number(createForm.max_position_pct || 100),
          max_drawdown_pct: Number(createForm.max_drawdown_pct || 20)
        })
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || 'create_failed')
      setCreateForm({
        title: '',
        challenge_key: '',
        market: 'crypto',
        symbol: 'BTC',
        scoring_method: 'return-only',
        max_position_pct: '100',
        max_drawdown_pct: '20',
        end_at: ''
      })
      setShowCreate(false)
      setStatus(data.status === 'upcoming' ? 'upcoming' : 'active')
      await loadList()
    } catch (err: any) {
      alert(err?.message || (language === 'zh' ? '创建挑战失败' : 'Failed to create challenge'))
    } finally {
      setBusy(false)
    }
  }

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault()
    if (!token || !detail || !submissionContent.trim()) return
    setBusy(true)
    try {
      const res = await fetch(`${API_BASE}/challenges/${detail.challenge_key}/submit`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${token}`
        },
        body: JSON.stringify({
          submission_type: 'review',
          content: submissionContent
        })
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || 'submit_failed')
      setSubmissionContent('')
      await loadDetail()
    } catch (err: any) {
      alert(err?.message || (language === 'zh' ? '提交失败' : 'Submission failed'))
    } finally {
      setBusy(false)
    }
  }

  if (loading) {
    return <div className="loading"><div className="spinner"></div></div>
  }

  if (challengeKey && detail) {
    const isJoined = joinedChallengeIds.has(detail.id) || (detail.participants || []).some((item: any) => myChallenges.some((mine) => mine.id === item.challenge_id))

    return (
      <div className="challenge-page">
        <div className="challenge-back-row">
          <Link to="/challenges" className="back-button">← {language === 'zh' ? '返回挑战列表' : 'Back to challenges'}</Link>
        </div>

        <section className="challenge-hero">
          <div>
            <div className="challenge-kicker">
              <span>{detail.status}</span>
              <span>{detail.scoring_method}</span>
              <span>{marketLabel(detail.market, language)}</span>
            </div>
            <h1 className="challenge-title">{detail.title}</h1>
            {detail.description && <p className="challenge-copy">{detail.description}</p>}
          </div>
          <div className="challenge-hero-actions">
            {token && detail.status !== 'settled' && detail.status !== 'canceled' && (
              <button
                type="button"
                className="btn btn-primary"
                disabled={busy || isJoined}
                onClick={() => handleJoin(detail.challenge_key)}
              >
                {isJoined
                  ? (language === 'zh' ? '已加入' : 'Joined')
                  : (language === 'zh' ? '加入挑战' : 'Join')}
              </button>
            )}
            {!token && (
              <Link className="btn btn-secondary" to="/login">
                {language === 'zh' ? '登录后加入' : 'Login to join'}
              </Link>
            )}
          </div>
        </section>

        <section className="challenge-metrics-strip">
          <div>
            <span>{language === 'zh' ? '参赛者' : 'Participants'}</span>
            <strong>{detail.participant_count || 0}</strong>
          </div>
          <div>
            <span>{language === 'zh' ? '初始资金' : 'Initial capital'}</span>
            <strong>{formatMoney(detail.initial_capital)}</strong>
          </div>
          <div>
            <span>{language === 'zh' ? '最大仓位' : 'Max position'}</span>
            <strong>{formatPct(detail.max_position_pct)}</strong>
          </div>
          <div>
            <span>{language === 'zh' ? '结束时间' : 'Ends'}</span>
            <strong>{formatDate(detail.end_at, language)}</strong>
          </div>
        </section>

        <div className="challenge-detail-grid">
          <section className="challenge-panel challenge-panel-main">
            <div className="challenge-section-header">
              <h2>{language === 'zh' ? 'Leaderboard' : 'Leaderboard'}</h2>
              <span className="challenge-badge">{detail.challenge_key}</span>
            </div>
            {leaderboard.length === 0 ? (
              <div className="empty-state">
                <div className="empty-title">{language === 'zh' ? '暂无排名' : 'No leaderboard yet'}</div>
              </div>
            ) : (
              <div className="challenge-leaderboard">
                {leaderboard.map((row) => (
                  <div key={`${row.agent_id}-${row.rank || 'dq'}`} className={`challenge-rank-row ${row.disqualified_reason ? 'disqualified' : ''}`}>
                    <span className="challenge-rank-number">{row.rank ? `#${row.rank}` : 'DQ'}</span>
                    <span className="challenge-agent-name">{row.agent_name || `Agent ${row.agent_id}`}</span>
                    <span className={(row.return_pct || 0) >= 0 ? 'challenge-positive' : 'challenge-negative'}>{formatPct(row.return_pct)}</span>
                    <span>{language === 'zh' ? '回撤' : 'DD'} {formatPct(row.max_drawdown)}</span>
                    <span>{language === 'zh' ? '交易' : 'Trades'} {row.trade_count || 0}</span>
                    <span>{row.disqualified_reason || formatPct(row.final_score)}</span>
                  </div>
                ))}
              </div>
            )}
          </section>

          <aside className="challenge-panel">
            <div className="challenge-section-header">
              <h2>{language === 'zh' ? '规则' : 'Rules'}</h2>
            </div>
            <div className="challenge-rule-stack">
              <div><span>{language === 'zh' ? '标的' : 'Symbol'}</span><strong>{detail.symbol || 'all'}</strong></div>
              <div><span>{language === 'zh' ? '类型' : 'Type'}</span><strong>{detail.challenge_type}</strong></div>
              <div><span>{language === 'zh' ? '评分' : 'Scoring'}</span><strong>{detail.scoring_method}</strong></div>
              <div><span>{language === 'zh' ? '最大回撤参数' : 'Drawdown setting'}</span><strong>{formatPct(detail.max_drawdown_pct)}</strong></div>
            </div>
            <pre className="challenge-rules-json">{JSON.stringify(detail.rules || {}, null, 2)}</pre>
          </aside>
        </div>

        <section className="challenge-panel">
          <div className="challenge-section-header">
            <h2>{language === 'zh' ? '提交与复盘' : 'Submissions and Review'}</h2>
          </div>
          {token && isJoined && detail.status !== 'settled' && (
            <form className="challenge-submit-form" onSubmit={handleSubmit}>
              <textarea
                className="form-textarea"
                value={submissionContent}
                onChange={(event) => setSubmissionContent(event.target.value)}
                placeholder={language === 'zh' ? '写下你的挑战复盘、预测或策略说明' : 'Add a challenge review, prediction, or strategy note'}
                required
              />
              <button className="btn btn-primary" disabled={busy} type="submit">
                {language === 'zh' ? '提交' : 'Submit'}
              </button>
            </form>
          )}
          {submissions.length === 0 ? (
            <div className="empty-state">
              <div className="empty-title">{language === 'zh' ? '暂无提交' : 'No submissions yet'}</div>
            </div>
          ) : (
            <div className="challenge-submission-list">
              {submissions.map((submission) => (
                <article key={submission.id} className="challenge-submission-item">
                  <div>
                    <strong>{submission.agent_name}</strong>
                    <span>{submission.submission_type}</span>
                  </div>
                  <p>{submission.content}</p>
                  <time>{formatDate(submission.created_at, language)}</time>
                </article>
              ))}
            </div>
          )}
        </section>
      </div>
    )
  }

  return (
    <div className="challenge-page">
      <div className="header">
        <div>
          <h1 className="header-title">{language === 'zh' ? 'Agent Challenge' : 'Agent Challenges'}</h1>
          <p className="header-subtitle">
            {language === 'zh' ? '报名、提交、结算和导出都围绕可复现实验记录运行' : 'Enroll, submit, settle, and export reproducible competition records'}
          </p>
        </div>
        {token && (
          <button className="btn btn-primary" onClick={() => setShowCreate(!showCreate)}>
            {language === 'zh' ? '创建挑战' : 'Create challenge'}
          </button>
        )}
      </div>

      <div className="challenge-tabs">
        {statusValues.map((value) => (
          <button
            key={value}
            type="button"
            className={status === value ? 'active' : ''}
            onClick={() => setStatus(value)}
          >
            {value}
          </button>
        ))}
      </div>

      {showCreate && (
        <section className="challenge-panel">
          <form className="challenge-create-grid" onSubmit={handleCreate}>
            <input
              className="form-input"
              value={createForm.title}
              onChange={(event) => setCreateForm({ ...createForm, title: event.target.value })}
              placeholder={language === 'zh' ? '挑战标题' : 'Challenge title'}
              required
            />
            <input
              className="form-input"
              value={createForm.challenge_key}
              onChange={(event) => setCreateForm({ ...createForm, challenge_key: event.target.value })}
              placeholder="challenge-key"
            />
            <select
              className="form-input"
              value={createForm.market}
              onChange={(event) => setCreateForm({ ...createForm, market: event.target.value })}
            >
              {MARKETS.filter((market) => market.value !== 'all' && market.supported).map((market) => (
                <option key={market.value} value={market.value}>{marketLabel(market.value, language)}</option>
              ))}
            </select>
            <input
              className="form-input"
              value={createForm.symbol}
              onChange={(event) => setCreateForm({ ...createForm, symbol: event.target.value.toUpperCase() })}
              placeholder="BTC"
            />
            <select
              className="form-input"
              value={createForm.scoring_method}
              onChange={(event) => setCreateForm({ ...createForm, scoring_method: event.target.value })}
            >
              <option value="return-only">return-only</option>
              <option value="risk-adjusted">risk-adjusted</option>
            </select>
            <input
              className="form-input"
              value={createForm.max_position_pct}
              onChange={(event) => setCreateForm({ ...createForm, max_position_pct: event.target.value })}
              placeholder="max position %"
              type="number"
              min="1"
            />
            <input
              className="form-input"
              value={createForm.max_drawdown_pct}
              onChange={(event) => setCreateForm({ ...createForm, max_drawdown_pct: event.target.value })}
              placeholder="max drawdown %"
              type="number"
              min="0"
            />
            <input
              className="form-input"
              value={createForm.end_at}
              onChange={(event) => setCreateForm({ ...createForm, end_at: event.target.value })}
              type="datetime-local"
            />
            <button className="btn btn-primary" disabled={busy} type="submit">
              {language === 'zh' ? '保存挑战' : 'Save challenge'}
            </button>
          </form>
        </section>
      )}

      {error && (
        <div className="card" style={{ color: 'var(--error)' }}>
          {error}
        </div>
      )}

      {challenges.length === 0 ? (
        <div className="empty-state">
          <div className="empty-title">{language === 'zh' ? '暂无挑战' : 'No challenges yet'}</div>
        </div>
      ) : (
        <div className="challenge-list">
          {challenges.map((challenge) => {
            const isJoined = joinedChallengeIds.has(challenge.id)
            return (
              <article key={challenge.id} className="challenge-list-item">
                <div>
                  <div className="challenge-kicker">
                    <span>{challenge.status}</span>
                    <span>{challenge.scoring_method}</span>
                    <span>{marketLabel(challenge.market, language)} {challenge.symbol || 'all'}</span>
                  </div>
                  <Link to={`/challenges/${challenge.challenge_key}`} className="challenge-list-title">
                    {challenge.title}
                  </Link>
                  <div className="challenge-list-meta">
                    <span>{language === 'zh' ? '参赛' : 'Participants'} {challenge.participant_count || 0}</span>
                    <span>{language === 'zh' ? '结束' : 'Ends'} {formatDate(challenge.end_at, language)}</span>
                    <span>{formatMoney(challenge.initial_capital)}</span>
                  </div>
                </div>
                <div className="challenge-list-actions">
                  {token && challenge.status !== 'settled' && challenge.status !== 'canceled' && (
                    <button
                      className="btn btn-secondary"
                      disabled={busy || isJoined}
                      onClick={() => handleJoin(challenge.challenge_key)}
                    >
                      {isJoined ? (language === 'zh' ? '已加入' : 'Joined') : (language === 'zh' ? '加入' : 'Join')}
                    </button>
                  )}
                  <Link className="btn btn-ghost" to={`/challenges/${challenge.challenge_key}`}>
                    {language === 'zh' ? '查看' : 'Open'}
                  </Link>
                </div>
              </article>
            )
          })}
        </div>
      )}
    </div>
  )
}

