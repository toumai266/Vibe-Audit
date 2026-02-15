import { useState, useEffect, useRef, useCallback } from 'react';
import {
  fetchSession,
  fetchBaseline,
  fetchSnapshots,
  fetchEvents,
  fetchFlags,
  fetchTurns,
  fetchPhases,
  fetchBaselines,
  subscribeToSession,
  exportSession,
  deleteSession
} from '../services/api';
import ConversationTimeline from './ConversationTimeline';
import { getConversationEntryCount } from './conversationTimelineUtils';
import ContextFlow from './ContextFlow';
import ConfirmModal from './ConfirmModal';
import { SessionDetailSkeleton } from './Skeleton';
import { t } from '../i18n';

export default function SessionDetail({ sessionId, interfaceLanguage = 'en', responseLanguage = 'en', timeZone = 'local', onDelete }) {
  const [session, setSession] = useState(null);
  const [baseline, setBaseline] = useState(null);
  const [snapshots, setSnapshots] = useState([]);
  const [events, setEvents] = useState([]);
  const [turnRows, setTurnRows] = useState([]);
  const [flags, setFlags] = useState([]);
  const [phases, setPhases] = useState([]);
  const [baselines, setBaselines] = useState([]);
  const [activeBaselineVersion, setActiveBaselineVersion] = useState(null);
  const [loading, setLoading] = useState(true);
  const [showLoadingSkeleton, setShowLoadingSkeleton] = useState(false);
  const [processing, setProcessing] = useState(false);
  const [processingSeconds, setProcessingSeconds] = useState(0);
  const processingStartedAtRef = useRef(null);
  const [exporting, setExporting] = useState(null);
  const [deleting, setDeleting] = useState(false);
  const [confirmModal, setConfirmModal] = useState(null);
  const [phaseNotice, setPhaseNotice] = useState(null);
  const [eventLogPhaseControl, setEventLogPhaseControl] = useState(null);
  const phaseNoticeTimer = useRef(null);
  const skeletonTimerRef = useRef(null);

  const clearSkeletonTimer = useCallback(() => {
    if (skeletonTimerRef.current) {
      clearTimeout(skeletonTimerRef.current);
      skeletonTimerRef.current = null;
    }
  }, []);

  const fetchAll = useCallback(async () => {
    const [sessionData, baselineData, snapshotsData, eventsData, flagsData, turnsData, phasesData, baselinesData] = await Promise.all([
      fetchSession(sessionId),
      fetchBaseline(sessionId),
      fetchSnapshots(sessionId),
      fetchEvents(sessionId),
      fetchFlags(sessionId),
      fetchTurns(sessionId),
      fetchPhases(sessionId).catch(() => ({ phases: [] })),
      fetchBaselines(sessionId).catch(() => ({ baselines: [], active_version: null })),
    ]);
    setSession(sessionData);
    setBaseline(baselineData);
    setSnapshots(snapshotsData.snapshots || []);
    setEvents(eventsData.events || []);
    setFlags(flagsData.flags || []);
    setTurnRows(turnsData.turns || []);
    setPhases(phasesData?.phases || []);
    setBaselines(baselinesData?.baselines || []);
    setActiveBaselineVersion(baselinesData?.active_version || null);
  }, [sessionId]);

  const loadData = useCallback(async () => {
    try {
      setLoading(true);
      setShowLoadingSkeleton(false);
      clearSkeletonTimer();
      skeletonTimerRef.current = setTimeout(() => {
        setShowLoadingSkeleton(true);
        skeletonTimerRef.current = null;
      }, 140);
      await fetchAll();
    } catch (error) {
      console.error(error);
    } finally {
      clearSkeletonTimer();
      setShowLoadingSkeleton(false);
      setLoading(false);
    }
  }, [fetchAll, clearSkeletonTimer]);

  const silentReload = useCallback(async () => {
    try {
      await fetchAll();
    } catch (error) {
      console.error(error);
    }
  }, [fetchAll]);

  // Auto-dismiss phase toast after 5 seconds
  useEffect(() => {
    if (phaseNotice) {
      if (phaseNoticeTimer.current) clearTimeout(phaseNoticeTimer.current);
      phaseNoticeTimer.current = setTimeout(() => setPhaseNotice(null), 5000);
    }
    return () => { if (phaseNoticeTimer.current) clearTimeout(phaseNoticeTimer.current); };
  }, [phaseNotice]);

  // Show live elapsed time while analysis is running.
  useEffect(() => {
    if (!processing) {
      setProcessingSeconds(0);
      return;
    }
    const tick = () => {
      if (!processingStartedAtRef.current) return;
      const elapsed = (Date.now() - processingStartedAtRef.current) / 1000;
      setProcessingSeconds(Math.max(0, elapsed));
    };
    tick();
    const interval = setInterval(tick, 100);
    return () => clearInterval(interval);
  }, [processing]);

  useEffect(() => {
    loadData();

    const unsubscribe = subscribeToSession(sessionId, () => {}, {
      alignment: () => { silentReload(); },
      flag: () => { silentReload(); },
      processing_start: () => {
        processingStartedAtRef.current = Date.now();
        setProcessing(true);
      },
      processing_complete: () => {
        setTimeout(() => {
          silentReload().then(() => {
            setProcessing(false);
            processingStartedAtRef.current = null;
          });
        }, 300);
      },
      processing_error: () => {
        setProcessing(false);
        processingStartedAtRef.current = null;
      },
      phase: (data) => {
        const cls = data?.classification || '';
        const summary = data?.summary || '';
        if (cls === 'pivot') {
          setPhaseNotice({ type: 'pivot', message: t(interfaceLanguage, 'session.pivotDetected', { summary }), ts: Date.now() });
        } else if (cls === 'new_phase') {
          setPhaseNotice({ type: 'new_phase', message: t(interfaceLanguage, 'session.newPhase', { summary }), ts: Date.now() });
        }
        silentReload();
      },
      baseline_version: () => {
        silentReload();
      },
      context_updated: () => {
        silentReload();
      },
    });

    return unsubscribe;
  }, [sessionId, interfaceLanguage, loadData, silentReload]);

  useEffect(() => {
    return () => {
      clearSkeletonTimer();
    };
  }, [clearSkeletonTimer]);

  function requestDelete() {
    setConfirmModal({ kind: 'delete' });
  }

  async function handleDelete() {
    setDeleting(true);
    let success = false;
    try {
      await deleteSession(sessionId);
      success = true;
    } catch (error) {
      console.error('Failed to delete session', error);
    } finally {
      setDeleting(false);
      setConfirmModal(null);
    }
    return success;
  }

  function requestExport(format) {
    setConfirmModal({ kind: 'export', format });
  }

  async function handleExport(format) {
    setExporting(format);
    try {
      const data = await exportSession(sessionId, format);
      let blob, filename;
      if (format === 'markdown') {
        blob = new Blob([data.content], { type: 'text/markdown' });
        filename = `session-${sessionId.slice(0, 8)}.md`;
      } else {
        blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
        filename = `session-${sessionId.slice(0, 8)}.json`;
      }
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      console.error('Export failed:', err);
    } finally {
      setExporting(null);
      setConfirmModal(null);
    }
  }

  function closeConfirmModal() {
    if (deleting || exporting !== null) return;
    setConfirmModal(null);
  }

  async function confirmModalAction() {
    if (!confirmModal) return;
    if (confirmModal.kind === 'delete') {
      const deleted = await handleDelete();
      if (deleted && onDelete) onDelete();
      return;
    }
    if (confirmModal.kind === 'export') {
      await handleExport(confirmModal.format);
    }
  }

  function getSeverityColor(severity) {
    switch (severity) {
      case 'high': return 'var(--danger)';
      case 'med': return 'var(--warning)';
      case 'low': return 'var(--accent)';
      default: return 'var(--text-dim)';
    }
  }

  if (loading && showLoadingSkeleton) {
    return <SessionDetailSkeleton />;
  }

  if (loading && !session) {
    return null;
  }

  const actionsLocked = processing || exporting !== null || deleting;
  const accuracyLanguage = responseLanguage || interfaceLanguage;
  const accuracyNotice = t(accuracyLanguage, 'session.accuracyNotice');

  const latestSnapshot = snapshots.length > 0 ? snapshots[snapshots.length - 1] : null;
  const eventLogEntryCount = getConversationEntryCount(events, turnRows);

  // Group flags by severity
  const flagsBySeverity = { high: [], med: [], low: [] };
  for (const flag of flags) {
    const sev = flag.severity || 'low';
    if (flagsBySeverity[sev]) {
      flagsBySeverity[sev].push(flag);
    } else {
      flagsBySeverity.low.push(flag);
    }
  }

  return (
    <div className="session-detail-enter">
      {/* Header */}
      <div className="content-header">
        <div className="content-title-group">
          <span className="content-title">
            {session?.cli_command?.split(':')[0] || `Session ${sessionId}`}
          </span>
          <div className="accuracy-note-wrap accuracy-note-wrap-inline">
            <span className="accuracy-note-chip" tabIndex={0} aria-label={accuracyNotice} title={accuracyNotice}>
              <span className="accuracy-note-icon">ⓘ</span>
              <span className="accuracy-note-label">{t(accuracyLanguage, 'session.accuracyInfo')}</span>
            </span>
            <div className="accuracy-note-tooltip" role="tooltip">
              {accuracyNotice}
            </div>
          </div>
        </div>
        <div className="content-actions">
          <button className="btn btn-export" onClick={() => requestExport('json')} disabled={actionsLocked}>
            {exporting === 'json' ? '...' : '\u2193 JSON'}
          </button>
          <button className="btn btn-danger" onClick={requestDelete} disabled={actionsLocked}>{t(interfaceLanguage, 'session.delete')}</button>
        </div>
      </div>

      {/* Phase transition toast notification */}
      {phaseNotice && (
        <div className={`phase-toast phase-toast--${phaseNotice.type}`}>
          <span className="phase-toast-icon">{phaseNotice.type === 'pivot' ? '🔄' : '➡️'}</span>
          <span className="phase-toast-text">{phaseNotice.message}</span>
          <button className="phase-toast-close" onClick={() => setPhaseNotice(null)}>✕</button>
        </div>
      )}
      {/* Processing overlay - covers content area only, not sidebar */}
      {processing && (
        <div className="processing-overlay">
          <div className="processing-indicator">
            <div className="processing-spinner" />
            <span className="processing-title">{t(interfaceLanguage, 'session.processing')}</span>
            <span className="processing-elapsed">{processingSeconds.toFixed(1)}s</span>
            <div className="processing-pulse">
              <span />
              <span />
              <span />
            </div>
          </div>
        </div>
      )}

      {/* Main Content: single scroll */}
      <div className="session-detail-body">
        {/* Section 1: Context Flow (Main Focus) */}
        <div className="session-section">
          <ContextFlow
            baseline={baseline}
            latestSnapshot={latestSnapshot}
            snapshots={snapshots}
            events={events}
            phases={phases}
            baselines={baselines}
            activeBaselineVersion={activeBaselineVersion}
            sessionId={sessionId}
            interfaceLanguage={interfaceLanguage}
            responseLanguage={responseLanguage}
            timeZone={timeZone}
            onBaselineUpdated={loadData}
          />
        </div>

        {/* Section 2: Flags (collapsible) */}
        {flags.length > 0 && (
          <details className="session-details-block" open>
            <summary className="session-details-summary">
              <span className="session-details-title">
                {t(interfaceLanguage, 'session.flags')}
                <span className="session-details-badge" style={{ background: 'rgba(239, 68, 68, 0.15)', color: 'var(--danger)' }}>
                  {flags.length}
                </span>
              </span>
            </summary>
            <div className="session-details-content">
              {['high', 'med', 'low'].map(sev => {
                const group = flagsBySeverity[sev];
                if (group.length === 0) return null;
                return (
                  <div key={sev} className="flags-severity-group">
                    <div className="flags-severity-header" style={{ color: getSeverityColor(sev) }}>
                      {sev === 'high' ? 'HIGH' : sev === 'med' ? 'MEDIUM' : 'LOW'} ({group.length})
                    </div>
                    {group.map(flag => (
                      <div key={flag.id} className="flag-card" style={{ borderLeftColor: getSeverityColor(flag.severity) }}>
                        <div className="flag-card-header">
                          <span className="flag-title">{flag.title}</span>
                          <span className="flag-category">{flag.category}</span>
                        </div>
                        <div className="flag-rationale">{flag.rationale}</div>
                      </div>
                    ))}
                  </div>
                );
              })}
            </div>
          </details>
        )}

        {/* Section 3: Event Log (collapsible, no nested scroll) */}
        <details className="session-details-block">
          <summary className="session-details-summary">
            <span className="session-details-title">
              {t(interfaceLanguage, 'session.eventLog')}
              <span className="session-details-count">
                {t(interfaceLanguage, 'timeline.entries', { count: eventLogEntryCount })}
              </span>
              <span className="session-details-actions">
                <button
                  type="button"
                  className="session-details-action-btn"
                  onClick={(e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    setEventLogPhaseControl({ action: 'collapse', nonce: Date.now() });
                  }}
                >
                  {t(interfaceLanguage, 'common.collapseAll')}
                </button>
                <button
                  type="button"
                  className="session-details-action-btn"
                  onClick={(e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    setEventLogPhaseControl({ action: 'expand', nonce: Date.now() });
                  }}
                >
                  {t(interfaceLanguage, 'common.expandAll')}
                </button>
              </span>
            </span>
          </summary>
          <div className="session-details-content">
            <ConversationTimeline
              sessionId={sessionId}
              interfaceLanguage={interfaceLanguage}
              timeZone={timeZone}
              events={events}
              turnRows={turnRows}
              phases={phases}
              phaseControlAction={eventLogPhaseControl}
            />
          </div>
        </details>
      </div>

      {confirmModal && (
        <ConfirmModal
          title={confirmModal.kind === 'delete'
            ? t(interfaceLanguage, 'session.delete')
            : t(interfaceLanguage, 'session.exportConfirmTitle')}
          message={confirmModal.kind === 'delete'
            ? t(interfaceLanguage, 'session.deleteConfirm')
            : t(interfaceLanguage, 'session.exportConfirmMessage', { format: String(confirmModal.format || '').toUpperCase() })}
          confirmLabel={confirmModal.kind === 'delete'
            ? t(interfaceLanguage, 'common.confirm')
            : t(interfaceLanguage, 'common.download')}
          cancelLabel={t(interfaceLanguage, 'common.cancel')}
          danger={confirmModal.kind === 'delete'}
          busy={deleting || exporting !== null}
          onConfirm={confirmModalAction}
          onClose={closeConfirmModal}
        />
      )}
    </div>
  );
}
