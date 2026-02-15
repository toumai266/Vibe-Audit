import { useState, useEffect, useMemo } from 'react';
import { regenerateBaseline, fetchBaselineVersion } from '../services/api';
import { getLocale, t } from '../i18n';
import BaselineRebuildModal from './BaselineRebuildModal';
import './ContextFlow.css';

const ALIGNMENT_ALIAS_MAP = {
  aligned: 'aligned',
  align: 'aligned',
  on_track: 'aligned',
  ontrack: 'aligned',
  drifted: 'drifting',
  drifting: 'drifting',
  drift: 'drifting',
  off_track: 'off_track',
  offtrack: 'off_track',
  unknown: 'unknown',
  pending: 'unknown',
  // Localized values observed in historical snapshot data
  '일치': 'aligned',
  '정렬됨': 'aligned',
  '정렬': 'aligned',
  '표류': 'drifting',
  '이탈': 'off_track',
  '벗어남': 'off_track',
  '탈선': 'off_track',
};

export default function ContextFlow({
  baseline,
  latestSnapshot,
  snapshots,
  events,
  phases = [],
  baselines = [],
  activeBaselineVersion = null,
  sessionId,
  interfaceLanguage = 'en',
  responseLanguage = 'en',
  timeZone = 'local',
  onBaselineUpdated
}) {
  const userMessages = events.filter(e => e.type === 'user_message');
  const locale = getLocale(interfaceLanguage);
  const [rebuildOpen, setRebuildOpen] = useState(false);
  const [rebuildReason, setRebuildReason] = useState('');
  const [rebuildHint, setRebuildHint] = useState('');
  const [rebuildError, setRebuildError] = useState('');
  const [saving, setSaving] = useState(false);
  const [alignmentReasonModalOpen, setAlignmentReasonModalOpen] = useState(false);

  // Phase & baseline versioning state
  const activeVersion = activeBaselineVersion
    || baselines.find(b => b?.is_active)?.version
    || baseline?.version
    || 1;
  const [selectedVersion, setSelectedVersion] = useState(activeVersion);
  const [displayBaseline, setDisplayBaseline] = useState(baseline);
  const [selectedPhaseId, setSelectedPhaseId] = useState(null); // null = latest/all
  const [phaseNavigatorQuery, setPhaseNavigatorQuery] = useState('');
  const [phaseNavigatorOpen, setPhaseNavigatorOpen] = useState(false);
  const [collapsedPhaseOverrides, setCollapsedPhaseOverrides] = useState({}); // phase_id -> collapsed boolean
  const [viewingSnapshotIdx, setViewingSnapshotIdx] = useState(null); // null = latest, number = index

  function normalizeAlignment(alignment) {
    if (!alignment) return 'unknown';
    const raw = String(alignment).trim().toLowerCase();
    const key = raw.replace(/-/g, '_').replace(/\s+/g, '_');
    const mapped = ALIGNMENT_ALIAS_MAP[key];
    if (mapped) return mapped;
    if (raw.includes('aligned') || raw.includes('align') || raw.includes('on track') || raw.includes('일치') || raw.includes('정렬')) return 'aligned';
    if (raw.includes('drifting') || raw.includes('drift') || raw.includes('표류')) return 'drifting';
    if (raw.includes('off track') || raw.includes('off_track') || raw.includes('offtrack') || raw.includes('이탈') || raw.includes('벗어') || raw.includes('탈선')) return 'off_track';
    return 'unknown';
  }

  useEffect(() => {
    setSelectedVersion(prev => {
      if (prev == null) return activeVersion;
      if (baselines.length === 0) return activeVersion;
      const exists = baselines.some(b => b?.version === prev);
      return exists ? prev : activeVersion;
    });
  }, [activeVersion, baselines]);

  // Update displayBaseline when baseline prop changes or version changes
  useEffect(() => {
    if (!selectedVersion || !sessionId || baselines.length <= 1) {
      setDisplayBaseline(baseline);
      return;
    }

    if (baseline?.version === selectedVersion) {
      setDisplayBaseline(baseline);
      return;
    }

    let cancelled = false;
    fetchBaselineVersion(sessionId, selectedVersion)
      .then(data => { if (!cancelled) setDisplayBaseline(data); })
      .catch(() => { if (!cancelled) setDisplayBaseline(baseline); });
    return () => { cancelled = true; };
  }, [selectedVersion, baseline, sessionId, baselines.length]);

  function openRebuildForm() {
    if (!canRebuildBaseline) return;
    setRebuildError('');
    setRebuildReason('');
    setRebuildHint('');
    setRebuildOpen(true);
  }

  function closeRebuildForm() {
    if (saving) return;
    setRebuildError('');
    setRebuildOpen(false);
  }

  async function submitRebuild() {
    if (!displayBaseline || displayBaseline.is_active === false) return;
    const reason = rebuildReason.trim();
    if (!reason) {
      setRebuildError(t(interfaceLanguage, 'context.rebuildReasonRequired'));
      return;
    }

    setRebuildError('');
    setSaving(true);
    try {
      await regenerateBaseline(displayBaseline.session_id, {
        reason,
        hint: rebuildHint.trim() || null,
        expected_active_version: activeVersion || null,
      });
      setRebuildOpen(false);
      setRebuildReason('');
      setRebuildHint('');
      setRebuildError('');
      if (onBaselineUpdated) onBaselineUpdated();
    } catch (e) {
      if (e?.status === 409) {
        setRebuildError(t(interfaceLanguage, 'context.rebuildConflict'));
      } else {
        setRebuildError(t(interfaceLanguage, 'context.rebuildFailed'));
      }
      console.error('Failed to rebuild baseline:', e);
    } finally {
      setSaving(false);
    }
  }

  function getAlignmentColor(alignment) {
    switch (normalizeAlignment(alignment)) {
      case 'aligned': return 'var(--success)';
      case 'drifting': return 'var(--warning)';
      case 'off_track': return 'var(--danger)';
      default: return 'var(--text-dim)';
    }
  }

  function getAlignmentLabel(alignment) {
    switch (normalizeAlignment(alignment)) {
      case 'aligned': return 'ALIGNED';
      case 'drifting': return 'DRIFTING';
      case 'off_track': return 'OFF TRACK';
      default: return 'PENDING';
    }
  }

  function getAlignmentHint(alignment) {
    switch (normalizeAlignment(alignment)) {
      case 'aligned': return t(interfaceLanguage, 'context.align.onTrack');
      case 'drifting': return t(interfaceLanguage, 'context.align.drifting');
      case 'off_track': return t(interfaceLanguage, 'context.align.intervention');
      default: return '';
    }
  }

  function getBaselineStatusClass(alignment) {
    switch (normalizeAlignment(alignment)) {
      case 'aligned': return 'cf-status-aligned';
      case 'drifting': return 'cf-status-drifting';
      case 'off_track': return 'cf-status-off-track';
      default: return 'cf-status-neutral';
    }
  }

  function getBaselineStatusIcon(alignment) {
    switch (normalizeAlignment(alignment)) {
      case 'aligned': return '✓';
      case 'drifting': return '⚠';
      case 'off_track': return '✗';
      default: return '…';
    }
  }

  function getDriftPercent(score) {
    return Math.round((score || 0) * 100);
  }

  function getAlignPercent(score) {
    return 100 - Math.round((score || 0) * 100);
  }

  function getAlignmentReasons(snapshot, alignmentValue, driftValue) {
    const details = snapshot?.evidence?.alignment_details;
    const reasons = [];
    const pushItems = (items, key) => {
      items.slice(0, 2).forEach((item) => {
        reasons.push(t(responseLanguage, key, { value: item }));
      });
    };

    if (details && typeof details === 'object') {
      pushItems(toStringList(details.mismatches), 'context.align.reasonMismatch');
      pushItems(toStringList(details.missing_must_have), 'context.align.reasonMissingMustHave');
      pushItems(toStringList(details.constraint_violations), 'context.align.reasonConstraint');
      pushItems(toStringList(details.new_scope), 'context.align.reasonNewScope');
    }

    if (reasons.length > 0) return reasons.slice(0, 3);

    if (alignmentValue === 'aligned') {
      if ((driftValue || 0) <= 0.1) return [t(responseLanguage, 'context.align.reasonNoIssue')];
      return [t(responseLanguage, 'context.align.reasonMinorDrift')];
    }
    if (alignmentValue === 'drifting') return [t(responseLanguage, 'context.align.reasonDriftingDefault')];
    if (alignmentValue === 'off_track') return [t(responseLanguage, 'context.align.reasonOffTrackDefault')];
    return [];
  }

  function formatTime(ts) {
    if (!ts) return '';
    const utcTs = ts.endsWith('Z') || ts.includes('+') ? ts : ts + 'Z';
    const options = {
      hour: '2-digit',
      minute: '2-digit'
    };
    if (timeZone && timeZone !== 'local') options.timeZone = timeZone;
    return new Date(utcTs).toLocaleTimeString(locale, options);
  }

  function getPhaseClassBadge(classification) {
    const classMap = {
      'initial': { label: t(interfaceLanguage, 'context.phase.initial'), class: 'phase-initial' },
      'continuation': { label: t(interfaceLanguage, 'context.phase.continuation'), class: 'phase-continuation' },
      'new_phase': { label: t(interfaceLanguage, 'context.phase.new'), class: 'phase-new' },
      'pivot': { label: t(interfaceLanguage, 'context.phase.pivot'), class: 'phase-pivot' }
    };
    return classMap[classification] || { label: classification, class: '' };
  }

  function getTaskText(task) {
    if (!task) return '';
    return (task.text || task.name || '').trim();
  }

  function toStringList(value) {
    if (!Array.isArray(value)) return [];
    return value
      .map(v => (typeof v === 'string' ? v.trim() : ''))
      .filter(Boolean);
  }

  function buildEvidenceWorkItems(evidence) {
    if (!evidence || typeof evidence !== 'object') return [];
    const items = [];
    const filesCreated = toStringList(evidence.files_created).slice(0, 4);
    const filesModified = toStringList(evidence.files_modified).slice(0, 4);
    const filesRead = toStringList(evidence.files_read).slice(0, 4);
    const commands = toStringList(evidence.commands_run).slice(0, 4);
    const packages = toStringList(evidence.packages_installed).slice(0, 4);
    const errors = toStringList(evidence.errors).slice(0, 2);
    const toolCount = Number(evidence.tool_count || 0);

    if (toolCount > 0) items.push(t(interfaceLanguage, 'context.evidence.toolCalls', { count: toolCount }));
    filesCreated.forEach(path => items.push(t(interfaceLanguage, 'context.evidence.fileCreated', { value: path })));
    filesModified.forEach(path => items.push(t(interfaceLanguage, 'context.evidence.fileModified', { value: path })));
    filesRead.forEach(path => items.push(t(interfaceLanguage, 'context.evidence.fileRead', { value: path })));
    commands.forEach(command => items.push(t(interfaceLanguage, 'context.evidence.commandRun', { value: command })));
    packages.forEach(pkg => items.push(t(interfaceLanguage, 'context.evidence.packageInstalled', { value: pkg })));
    errors.forEach(err => items.push(t(interfaceLanguage, 'context.evidence.errorDetected', { value: err.slice(0, 120) })));

    return items;
  }

  function buildPhaseNavigatorText(phase) {
    if (!phase) return '';
    const summary = (phase.summary || phase.user_message || '').trim();
    const summaryPreview = summary.length > 50 ? `${summary.slice(0, 50)}...` : summary;
    return summaryPreview ? `#${phase.phase_num} - ${summaryPreview}` : `#${phase.phase_num}`;
  }

  function getBaselineAccuracyText(confidence, ambiguityNotes) {
    if (ambiguityNotes) return ambiguityNotes;
    if (confidence >= 0.85) return t(responseLanguage, 'context.accuracy.high');
    if (confidence >= 0.7) return t(responseLanguage, 'context.accuracy.mediumHigh');
    if (confidence >= 0.5) return t(responseLanguage, 'context.accuracy.medium');
    return t(responseLanguage, 'context.accuracy.low');
  }

  function getBaselineStatusText(snapshot, baselineGoal, normalizedAlignment) {
    const goal = (baselineGoal || '').trim();
    const mismatch = toStringList(snapshot?.evidence?.alignment_details?.mismatches)[0] || '';

    if (normalizedAlignment === 'aligned') {
      return goal
        ? t(responseLanguage, 'context.baselineStatus.alignedWithGoal', { goal })
        : t(responseLanguage, 'context.baselineStatus.aligned');
    }

    if (normalizedAlignment === 'drifting' || normalizedAlignment === 'off_track') {
      if (mismatch) {
        return t(responseLanguage, 'context.baselineStatus.driftingReason', { reason: mismatch });
      }
      return goal
        ? t(responseLanguage, 'context.baselineStatus.driftingWithGoal', { goal })
        : t(responseLanguage, 'context.baselineStatus.drifting');
    }

    return (snapshot?.baseline_status || '').trim();
  }

  // Snapshot navigation: viewingSnapshotIdx selects which snapshot to display
  const displaySnapshot = viewingSnapshotIdx !== null && viewingSnapshotIdx < snapshots.length
    ? snapshots[viewingSnapshotIdx]
    : latestSnapshot;
  const currentSnapshotIdx = viewingSnapshotIdx !== null ? viewingSnapshotIdx : snapshots.length - 1;

  // Group snapshots by phase for accordion timeline
  const phaseGroups = useMemo(() => {
    if (phases.length === 0) return [{ phase: null, snapshots: snapshots }];
    const groups = [];
    const phaseMap = new Map(phases.map(p => [p.id, p]));
    let currentGroup = null;
    for (const snap of snapshots) {
      const phaseId = snap.phase_id || null;
      if (!currentGroup || currentGroup.phaseId !== phaseId) {
        currentGroup = { phaseId, phase: phaseMap.get(phaseId) || null, snapshots: [] };
        groups.push(currentGroup);
      }
      currentGroup.snapshots.push(snap);
    }
    return groups;
  }, [phases, snapshots]);

  function isPhaseCollapsed(phaseId, isLastGroup) {
    if (!phaseId) return false;
    if (Object.prototype.hasOwnProperty.call(collapsedPhaseOverrides, phaseId)) {
      return collapsedPhaseOverrides[phaseId];
    }
    return !isLastGroup;
  }

  // When a phase is clicked, switch baseline + jump to that phase's latest snapshot
  function applyPhaseSelection(phase) {
    setSelectedPhaseId(phase.id);
    setSelectedVersion(phase.baseline_version);
    const lastIdx = snapshots.findLastIndex(s => s.phase_id === phase.id);
    setViewingSnapshotIdx(lastIdx >= 0 ? (lastIdx === snapshots.length - 1 ? null : lastIdx) : null);
    setPhaseNavigatorQuery(buildPhaseNavigatorText(phase));
    setPhaseNavigatorOpen(false);
  }

  function selectPhase(phase) {
    if (selectedPhaseId === phase.id) {
      // Deselect → back to latest
      setSelectedPhaseId(null);
      setSelectedVersion(activeVersion);
      setViewingSnapshotIdx(null);
      setPhaseNavigatorQuery('');
      setPhaseNavigatorOpen(false);
    } else {
      applyPhaseSelection(phase);
    }
  }

  function handlePhaseNavigatorSubmit() {
    const query = phaseNavigatorQuery.trim().toLowerCase();
    if (!query) {
      if (phaseNavigatorOptions.length > 0) applyPhaseSelection(phaseNavigatorOptions[0]);
      return;
    }

    const numMatch = query.match(/#?\s*(\d+)/);
    let matched = null;
    if (numMatch) {
      const phaseNum = Number(numMatch[1]);
      if (Number.isFinite(phaseNum)) {
        matched = visiblePhases.find(p => p.phase_num === phaseNum) || null;
      }
    }
    if (!matched) {
      matched = visiblePhases.find(p => buildPhaseNavigatorText(p).toLowerCase().includes(query)) || null;
    }
    if (!matched && phaseNavigatorOptions.length > 0) {
      matched = phaseNavigatorOptions[0];
    }
    if (matched) {
      applyPhaseSelection(matched);
    }
  }

  function togglePhaseCollapse(phaseId, isLastGroup) {
    if (!phaseId) return;
    setCollapsedPhaseOverrides(prev => {
      const current = Object.prototype.hasOwnProperty.call(prev, phaseId)
        ? prev[phaseId]
        : !isLastGroup;
      return {
        ...prev,
        [phaseId]: !current,
      };
    });
  }

  function collapseAllEvolutionPhases() {
    const next = {};
    phaseGroups.forEach((group) => {
      if (group.phaseId) next[group.phaseId] = true;
    });
    setCollapsedPhaseOverrides(next);
  }

  function expandAllEvolutionPhases() {
    const next = {};
    phaseGroups.forEach((group) => {
      if (group.phaseId) next[group.phaseId] = false;
    });
    setCollapsedPhaseOverrides(next);
  }

  function navigateSnapshot(delta) {
    const newIdx = currentSnapshotIdx + delta;
    if (newIdx >= 0 && newIdx < snapshots.length) {
      setViewingSnapshotIdx(newIdx === snapshots.length - 1 ? null : newIdx);
      // Update phase selection to match the snapshot's phase
      const snap = snapshots[newIdx];
      if (snap.phase_id) {
        const phase = phases.find(p => p.id === snap.phase_id);
        if (phase) {
          setSelectedPhaseId(phase.id);
          setSelectedVersion(phase.baseline_version);
        }
      }
    }
  }

  const alignment = normalizeAlignment(displaySnapshot?.alignment || 'unknown');
  const driftScore = displaySnapshot?.drift_score || 0;
  const baselineStatusText = getBaselineStatusText(
    displaySnapshot,
    displayBaseline?.goal || baseline?.goal || '',
    alignment
  );
  const alignmentReasons = getAlignmentReasons(displaySnapshot, alignment, driftScore);
  const canShowAlignmentReasons = alignment !== 'unknown' && alignmentReasons.length > 0;
  const prevSnapshotForDisplay = currentSnapshotIdx > 0 ? snapshots[currentSnapshotIdx - 1] : null;
  const currentTaskItems = (displaySnapshot?.current_tasks || [])
    .map(getTaskText)
    .filter(Boolean);
  const prevTaskSet = new Set((prevSnapshotForDisplay?.current_tasks || []).map(getTaskText).filter(Boolean));
  const newTaskItems = currentTaskItems.filter(t => !prevTaskSet.has(t));
  const evidenceWorkItems = buildEvidenceWorkItems(displaySnapshot?.evidence);
  const fallbackTaskItems = newTaskItems.length > 0 ? newTaskItems : currentTaskItems;
  const recentWorkItems = evidenceWorkItems.length > 0 ? evidenceWorkItems : fallbackTaskItems;
  const usesEvidenceWork = evidenceWorkItems.length > 0;
  const hasStableTurnNotice = !usesEvidenceWork && newTaskItems.length === 0 && Boolean(prevSnapshotForDisplay);
  const progressNotice = usesEvidenceWork
    ? t(responseLanguage, 'context.progress.fromLogs')
    : hasStableTurnNotice
      ? t(responseLanguage, 'context.progress.noNewItems')
      : t(responseLanguage, 'context.progress.fromItems');

  const displaySnapTime = displaySnapshot?.ts
    ? new Date(displaySnapshot.ts.endsWith('Z') || displaySnapshot.ts.includes('+') ? displaySnapshot.ts : `${displaySnapshot.ts}Z`)
    : null;
  const prevSnapTime = prevSnapshotForDisplay?.ts
    ? new Date(prevSnapshotForDisplay.ts.endsWith('Z') || prevSnapshotForDisplay.ts.includes('+') ? prevSnapshotForDisplay.ts : `${prevSnapshotForDisplay.ts}Z`)
    : new Date(0);
  const userMsgsForDisplay = displaySnapTime
    ? userMessages.filter(e => {
        const et = new Date(e.ts.endsWith('Z') || e.ts.includes('+') ? e.ts : `${e.ts}Z`);
        return et > prevSnapTime && et <= displaySnapTime;
      })
    : [];
  const latestUserIntent = userMsgsForDisplay.length > 0
    ? (userMsgsForDisplay[userMsgsForDisplay.length - 1]?.payload?.content || '').trim()
    : '';
  // Show only phases that already have at least one analyzed snapshot.
  // This prevents phase panel jumps before assistant-turn analysis completes.
  const analyzedPhases = phases.filter((p) => {
    if (typeof p?.snapshot_count !== 'number') return true; // backward-compatible payloads
    return p.snapshot_count > 0;
  });
  const phaseCards = analyzedPhases.filter(p => p.classification !== 'continuation');
  const visiblePhases = phaseCards.length > 0 ? phaseCards : analyzedPhases;
  const phaseNavigatorQueryNorm = phaseNavigatorQuery.trim().toLowerCase();
  const phaseNavigatorOptions = (() => {
    if (visiblePhases.length === 0) return [];
    if (!phaseNavigatorQueryNorm) return visiblePhases.slice(0, 12);
    const queryNum = phaseNavigatorQueryNorm.replace(/[^0-9]/g, '');
    return visiblePhases
      .filter((phase) => {
        if (queryNum && String(phase.phase_num).includes(queryNum)) return true;
        return buildPhaseNavigatorText(phase).toLowerCase().includes(phaseNavigatorQueryNorm);
      })
      .slice(0, 12);
  })();
  const baselineConfidencePct = displayBaseline?.confidence != null
    ? Math.round(displayBaseline.confidence * 100)
    : null;
  const baselineAccuracyText = baselineConfidencePct != null
    ? getBaselineAccuracyText(displayBaseline.confidence || 0, displayBaseline?.ambiguity_notes)
    : null;
  const baselineAccuracyLabel = t(responseLanguage, 'context.baselineAccuracy');
  const baselineConfidenceTooltip = baselineConfidencePct != null
    ? [
        `${baselineAccuracyLabel}: ${baselineConfidencePct}%`,
        baselineAccuracyText
      ].filter(Boolean).join('\n')
    : null;
  const canRebuildBaseline = Boolean(
    displayBaseline
    && displayBaseline.is_active !== false
    && displayBaseline.version === activeVersion
  );

  function openAlignmentReasonPreview() {
    if (!canShowAlignmentReasons) return;
    setAlignmentReasonModalOpen(true);
  }

  function closeAlignmentReasonPreview() {
    setAlignmentReasonModalOpen(false);
  }

  useEffect(() => {
    if (canShowAlignmentReasons) return;
    setAlignmentReasonModalOpen(false);
  }, [canShowAlignmentReasons, currentSnapshotIdx]);

  return (
    <div className="context-flow">
      {/* Phase Indicator Panel - show if phases exist */}
      {visiblePhases.length > 0 && (
        <div className="cf-phases">
          <div className="cf-phases-header">
            <div className="cf-phases-title-wrap">
              <span className="cf-phases-label">{t(interfaceLanguage, 'context.phaseShifts')}</span>
              <span className="cf-phases-hint">{t(interfaceLanguage, 'context.phaseHint')}</span>
            </div>
            <div className="cf-phases-actions">
              <span className="cf-phases-count">{visiblePhases.length} phase{visiblePhases.length > 1 ? 's' : ''}</span>
              <div className="cf-phase-nav">
                <input
                  className="cf-phase-nav-input"
                  type="search"
                  value={phaseNavigatorQuery}
                  onChange={e => setPhaseNavigatorQuery(e.target.value)}
                  onFocus={() => setPhaseNavigatorOpen(true)}
                  onBlur={() => {
                    setTimeout(() => setPhaseNavigatorOpen(false), 100);
                  }}
                  onKeyDown={e => {
                    if (e.key === 'Enter') {
                      e.preventDefault();
                      handlePhaseNavigatorSubmit();
                    }
                  }}
                  placeholder={t(interfaceLanguage, 'context.phaseNavigatorPlaceholder')}
                />
                <button className="cf-phase-nav-go" onClick={handlePhaseNavigatorSubmit}>
                  {t(interfaceLanguage, 'context.phaseNavigatorGo')}
                </button>
                {phaseNavigatorOpen && (
                  <div className="cf-phase-nav-menu">
                    {phaseNavigatorOptions.length > 0 ? phaseNavigatorOptions.map((phase) => (
                      <button
                        key={`nav-opt-${phase.id}`}
                        type="button"
                        className={`cf-phase-nav-option ${selectedPhaseId === phase.id ? 'active' : ''}`}
                        onMouseDown={e => e.preventDefault()}
                        onClick={() => applyPhaseSelection(phase)}
                      >
                        <span className="cf-phase-nav-option-num">#{phase.phase_num}</span>
                        <span className="cf-phase-nav-option-text">
                          {phase.summary || phase.user_message || buildPhaseNavigatorText(phase)}
                        </span>
                      </button>
                    )) : (
                      <div className="cf-phase-nav-empty">{t(interfaceLanguage, 'context.phaseNavigatorNoResults')}</div>
                    )}
                  </div>
                )}
              </div>
            </div>
          </div>
          <div className="cf-phases-list">
            {visiblePhases.map((phase) => {
              const badge = getPhaseClassBadge(phase.classification);
              return (
                <div key={phase.id} className={`cf-phase-item ${badge.class} ${selectedPhaseId === phase.id ? 'selected' : ''}`} onClick={() => selectPhase(phase)} style={{cursor: 'pointer'}} title={[phase.summary, phase.user_message].filter(Boolean).join('\n\n')}>
                  <div className="cf-phase-num">#{phase.phase_num}</div>
                  <div className="cf-phase-info">
                    <span className={`cf-phase-badge ${badge.class}`}>{badge.label}</span>
                    {phase.summary && <span className="cf-phase-summary">{phase.summary}</span>}
                  </div>
                  <div className="cf-phase-meta">
                    <span className="cf-phase-time">{formatTime(phase.started_at)}</span>
                    <span className="cf-phase-baseline">v{phase.baseline_version}</span>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* 3-Column Comparison */}
      <div className="cf-comparison">
        {/* Left: Baseline Card */}
        <div className="cf-card cf-baseline">
          <div className="cf-card-header">
            <span className="cf-card-label">{t(interfaceLanguage, 'context.baseline')}</span>
            <div className="cf-baseline-header-actions">
              {baselines.length > 1 && (
                <div className="cf-snapshot-nav">
                  <button className="cf-nav-btn" onClick={() => setSelectedVersion(Math.max(1, (selectedVersion || 1) - 1))} disabled={(selectedVersion || 1) <= 1}>&lsaquo;</button>
                  <span className="cf-nav-counter">v{selectedVersion || 1}/{baselines.length}</span>
                  <button className="cf-nav-btn" onClick={() => setSelectedVersion(Math.min(baselines.length, (selectedVersion || 1) + 1))} disabled={(selectedVersion || 1) >= baselines.length}>&rsaquo;</button>
                </div>
              )}
              {displayBaseline?.is_active === false && <span className="cf-archived-tag">{t(interfaceLanguage, 'context.archived')}</span>}
              {canRebuildBaseline && (
                <button className="cf-edit-btn" onClick={openRebuildForm} title={t(interfaceLanguage, 'context.rebuildBaseline')}>
                  {t(interfaceLanguage, 'context.rebuildBaseline')}
                </button>
              )}
            </div>
          </div>
          <div className="cf-card-body cf-baseline-body">
            <div className="cf-baseline-main">
            {displayBaseline ? (
              <>
                <div className="cf-field">
                  <div className="cf-field-label">{t(interfaceLanguage, 'context.goal')}</div>
                  <div className="cf-field-value">{displayBaseline.goal}</div>
                </div>
                {displayBaseline.must_have?.length > 0 && (
                  <div className="cf-field">
                    <div className="cf-field-label">{t(interfaceLanguage, 'context.mustHave')}</div>
                    <ul className="cf-list cf-list-success">
                      {displayBaseline.must_have.map((item, i) => (
                        <li key={i}>{item}</li>
                      ))}
                    </ul>
                  </div>
                )}
                {displayBaseline.must_not?.length > 0 && (
                  <div className="cf-field">
                    <div className="cf-field-label">{t(interfaceLanguage, 'context.mustNot')}</div>
                    <ul className="cf-list cf-list-danger">
                      {displayBaseline.must_not.map((item, i) => (
                        <li key={i}>{item}</li>
                      ))}
                    </ul>
                  </div>
                )}
                {baselineConfidencePct != null && (
                  <div
                    className="cf-field cf-baseline-accuracy-field"
                    title={baselineConfidenceTooltip}
                    aria-label={baselineConfidenceTooltip}
                  >
                    <div className="cf-field-label">{baselineAccuracyLabel}</div>
                    <div className="cf-baseline-accuracy-row">
                      <span className={`cf-confidence-badge ${displayBaseline.confidence < 0.7 ? 'cf-confidence-low' : 'cf-confidence-ok'}`}>
                        {baselineConfidencePct}%
                      </span>
                      <span className="cf-baseline-accuracy-text">{baselineAccuracyText}</span>
                    </div>
                  </div>
                )}
              </>
            ) : (
              <div className="cf-placeholder">
                <div className="cf-placeholder-icon">...</div>
                <div className="cf-placeholder-text">{t(interfaceLanguage, 'context.baselinePending')}</div>
                <div className="cf-placeholder-hint">{t(interfaceLanguage, 'context.waitingFirstUser')}</div>
              </div>
            )}
            </div>
          </div>
        </div>

        {/* Center: Alignment Indicator */}
        <div className="cf-alignment">
          <div
            className="cf-alignment-score-wrap"
            onMouseEnter={openAlignmentReasonPreview}
            onMouseLeave={closeAlignmentReasonPreview}
          >
            <div
              className={`cf-alignment-ring ${alignment} ${canShowAlignmentReasons ? 'has-reasons' : ''}`}
              style={{ borderColor: getAlignmentColor(alignment) }}
            >
              <div
                className={`cf-alignment-score ${canShowAlignmentReasons ? 'interactive' : ''}`}
                style={{ color: getAlignmentColor(alignment) }}
                aria-label={canShowAlignmentReasons ? t(interfaceLanguage, 'context.align.why') : undefined}
                title={canShowAlignmentReasons ? t(interfaceLanguage, 'context.align.why') : undefined}
              >
                {alignment !== 'unknown' ? `${getAlignPercent(driftScore)}%` : '?'}
              </div>
            </div>
            {canShowAlignmentReasons && alignmentReasonModalOpen && (
              <div className="cf-alignment-reason-modal" role="dialog" aria-label={t(interfaceLanguage, 'context.align.why')} aria-modal="false">
                <div className="cf-alignment-reason-modal-header">
                  <div className="cf-alignment-reason-modal-title">{t(interfaceLanguage, 'context.align.why')}</div>
                </div>
                <ul className="cf-alignment-reason-list">
                  {alignmentReasons.map((reason, idx) => (
                    <li key={`${reason}-${idx}`}>{reason}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
          <div className="cf-alignment-label" style={{ color: getAlignmentColor(alignment) }}>
            {getAlignmentLabel(alignment)}
          </div>
          {alignment !== 'unknown' && (
            <div className="cf-alignment-hint">
              {getAlignmentHint(alignment)}
            </div>
          )}
          {alignment !== 'unknown' && driftScore > 0 && (
            <div className="cf-alignment-drift-label">
              Drift: {getDriftPercent(driftScore)}%
            </div>
          )}
        </div>

        {/* Right: Current State Card */}
        <div className="cf-card cf-current">
          <div className="cf-card-header">
            <span className="cf-card-label">{t(interfaceLanguage, 'context.currentState')}</span>
            {snapshots.length > 1 && (
              <div className="cf-snapshot-nav">
                <button className="cf-nav-btn" onClick={() => navigateSnapshot(-1)} disabled={currentSnapshotIdx <= 0}>&lsaquo;</button>
                <span className="cf-nav-counter">{currentSnapshotIdx + 1}/{snapshots.length}</span>
                <button className="cf-nav-btn" onClick={() => navigateSnapshot(1)} disabled={currentSnapshotIdx >= snapshots.length - 1}>&rsaquo;</button>
              </div>
            )}
            {viewingSnapshotIdx !== null && (
              <button className="cf-edit-btn" onClick={() => { setViewingSnapshotIdx(null); setSelectedPhaseId(null); setSelectedVersion(activeVersion); }}>
                Latest
              </button>
            )}
          </div>
          <div className="cf-card-body">
            {displaySnapshot ? (
              <>
                <div className="cf-field">
                  <div className="cf-field-label">{t(interfaceLanguage, 'context.currentGoal')}</div>
                  <div className="cf-field-value">{displaySnapshot.current_goal}</div>
                </div>
                {latestUserIntent && (
                  <div className="cf-field">
                    <div className="cf-field-label">{t(interfaceLanguage, 'context.latestUserIntent')}</div>
                    <div className="cf-field-value">{latestUserIntent}</div>
                  </div>
                )}
                {(recentWorkItems.length > 0 || prevSnapshotForDisplay) && (
                  <div className="cf-field">
                    <div className="cf-field-label">{t(interfaceLanguage, 'context.recentWork')}</div>
                    {recentWorkItems.length > 0 ? (
                      <ul className="cf-list cf-list-work">
                        {recentWorkItems.map((item, i) => (
                          <li key={`${item}-${i}`}>{item}</li>
                        ))}
                      </ul>
                    ) : (
                      <div className="cf-field-value-muted">{t(interfaceLanguage, 'context.noWorkItems')}</div>
                    )}
                  </div>
                )}
                <div className="cf-field">
                  <div className="cf-field-label">{t(interfaceLanguage, 'context.progressNote')}</div>
                  <div className={`cf-field-value cf-progress-note ${hasStableTurnNotice ? 'cf-progress-note-stable' : ''}`}>
                    {progressNotice}
                  </div>
                </div>
                {baselineStatusText && (
                  <div className="cf-field cf-baseline-status-field">
                    <div className="cf-field-label">{t(interfaceLanguage, 'context.baselineStatus')}</div>
                    <div className={`cf-baseline-status-inline ${getBaselineStatusClass(displaySnapshot.alignment)}`}>
                      <span className="cf-status-inline-icon">
                        {getBaselineStatusIcon(displaySnapshot.alignment)}
                      </span>
                      <span className="cf-status-inline-text">{baselineStatusText}</span>
                    </div>
                  </div>
                )}
                {displaySnapshot.suggested_prompt && (
                  <div className="cf-field cf-suggested-prompt">
                    <div className="cf-field-label">{t(interfaceLanguage, 'context.suggestedCorrectionPrompt')}</div>
                    <div className="cf-prompt-box" onClick={() => {navigator.clipboard.writeText(displaySnapshot.suggested_prompt); }}>
                      <code>{displaySnapshot.suggested_prompt}</code>
                      <span className="cf-copy-hint">{t(interfaceLanguage, 'context.clickToCopy')}</span>
                    </div>
                  </div>
                )}
              </>
            ) : (
              <div className="cf-placeholder">
                <div className="cf-placeholder-icon">...</div>
                <div className="cf-placeholder-text">{t(interfaceLanguage, 'context.analysisPending')}</div>
                <div className="cf-placeholder-hint">{t(interfaceLanguage, 'context.waitingAgent')}</div>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Evolution Timeline - accordion grouped by phase */}
      {snapshots.length > 1 && (
        <details className="cf-evolution-block" open>
          <summary className="cf-evolution-summary">
            <span className="cf-evolution-label">{t(interfaceLanguage, 'context.evolution')}</span>
            <span className="cf-evolution-count">{t(interfaceLanguage, 'timeline.entries', { count: snapshots.length })}</span>
            {phaseGroups.length > 1 && (
              <span className="cf-evolution-actions">
                <button
                  type="button"
                  className="cf-evo-control-btn"
                  onClick={(e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    collapseAllEvolutionPhases();
                  }}
                >
                  {t(interfaceLanguage, 'common.collapseAll')}
                </button>
                <button
                  type="button"
                  className="cf-evo-control-btn"
                  onClick={(e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    expandAllEvolutionPhases();
                  }}
                >
                  {t(interfaceLanguage, 'common.expandAll')}
                </button>
              </span>
            )}
          </summary>
          <div className="cf-evolution">
            <div className="cf-evolution-list">
              {phaseGroups.map((group, groupIdx) => {
                const isLastGroup = groupIdx === phaseGroups.length - 1;
                const isCollapsed = isPhaseCollapsed(group.phaseId, isLastGroup);
                const badge = group.phase ? getPhaseClassBadge(group.phase.classification) : null;

                return (
                  <div key={group.phaseId || `no-phase-${groupIdx}`} className="cf-evo-phase-group">
                    {/* Phase accordion header (skip for single-phase sessions) */}
                    {group.phase && phaseGroups.length > 1 && (
                      <div
                        className={`cf-evo-phase-header ${badge.class} ${isCollapsed ? 'collapsed' : ''}`}
                        onClick={() => togglePhaseCollapse(group.phaseId, isLastGroup)}
                      >
                        <span className="cf-evo-phase-toggle">{isCollapsed ? '\u25b6' : '\u25bc'}</span>
                        <span className="cf-evo-phase-badge">{badge.label}</span>
                        <span className="cf-evo-phase-num">Phase #{group.phase.phase_num}</span>
                        {group.phase.summary && (
                          <span className="cf-evo-phase-summary">{group.phase.summary.length > 50 ? group.phase.summary.slice(0, 50) + '...' : group.phase.summary}</span>
                        )}
                        <span className={`cf-evo-phase-snap-count ${isCollapsed ? 'is-collapsed' : ''}`}>
                          {isCollapsed
                            ? t(interfaceLanguage, 'context.snapshotsHidden', { count: group.snapshots.length })
                            : group.snapshots.length}
                        </span>
                      </div>
                    )}
                    {/* Snapshot rows (hidden when collapsed) */}
                    {!isCollapsed && group.snapshots.map((snap, idx) => {
                      const globalIdx = snapshots.indexOf(snap);
                      const prevSnap = globalIdx > 0 ? snapshots[globalIdx - 1] : null;
                      const currentPhase = snap.phase_id ? phases.find(p => p.id === snap.phase_id) : null;
                      const prevPhase = prevSnap?.phase_id ? phases.find(p => p.id === prevSnap.phase_id) : null;
                      const phaseTransition = !!(prevSnap && currentPhase && prevPhase && currentPhase.id !== prevPhase.id);
                      const goalChanged = phaseTransition && (currentPhase.classification === 'new_phase' || currentPhase.classification === 'pivot');
                      const currentAlign = normalizeAlignment(snap.alignment);
                      const prevAlign = prevSnap ? normalizeAlignment(prevSnap.alignment) : null;
                      const alignChanged = prevSnap && prevAlign !== currentAlign;

                      const snapTime = new Date(snap.ts.endsWith('Z') || snap.ts.includes('+') ? snap.ts : snap.ts + 'Z');
                      const prevSnapTs = prevSnap
                        ? new Date(prevSnap.ts.endsWith('Z') || prevSnap.ts.includes('+') ? prevSnap.ts : prevSnap.ts + 'Z')
                        : new Date(0);
                      const userMsgsBetween = userMessages.filter(e => {
                        const et = new Date(e.ts.endsWith('Z') || e.ts.includes('+') ? e.ts : e.ts + 'Z');
                        return et > prevSnapTs && et <= snapTime;
                      });
                      const isViewing = globalIdx === currentSnapshotIdx;

                      return (
                        <div
                          key={snap.id}
                          className={`cf-evo-row ${isViewing ? 'viewing' : ''}`}
                          onClick={() => setViewingSnapshotIdx(globalIdx === snapshots.length - 1 ? null : globalIdx)}
                          style={{ cursor: 'pointer' }}
                        >
                          <div className="cf-evo-rail">
                            <div className={`cf-evo-dot ${currentAlign || 'unknown'}`} />
                            {(idx < group.snapshots.length - 1 || !isLastGroup) && <div className="cf-evo-line" />}
                          </div>
                          <div className="cf-evo-content">
                            <div className="cf-evo-meta">
                              <span className="cf-evo-time">{formatTime(snap.ts)}</span>
                              <span className={`cf-evo-status ${currentAlign || 'unknown'}`}>
                                {getAlignmentLabel(currentAlign)}
                              </span>
                              <span className="cf-evo-drift">{getAlignPercent(snap.drift_score)}%</span>
                            </div>
                            <div className="cf-evo-goal">{snap.current_goal}</div>
                            <div className="cf-evo-changes">
                              {userMsgsBetween.length > 0 && (
                                <span className="cf-evo-tag cf-evo-tag-user">{t(interfaceLanguage, 'context.userInput')}</span>
                              )}
                              {goalChanged && (
                                <span className="cf-evo-tag cf-evo-tag-goal">{t(interfaceLanguage, 'context.goalChanged')}</span>
                              )}
                              {alignChanged && (
                                <span className={`cf-evo-tag cf-evo-tag-align ${currentAlign}`}>
                                  {getAlignmentLabel(prevAlign)} &rarr; {getAlignmentLabel(currentAlign)}
                                </span>
                              )}
                              {!goalChanged && !alignChanged && !userMsgsBetween.length && idx > 0 && (
                                <span className="cf-evo-tag cf-evo-tag-none">{t(interfaceLanguage, 'context.noChange')}</span>
                              )}
                            </div>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                );
              })}
            </div>
          </div>
        </details>
      )}

      {rebuildOpen && (
        <BaselineRebuildModal
          interfaceLanguage={interfaceLanguage}
          goal={displayBaseline?.goal || ''}
          reason={rebuildReason}
          hint={rebuildHint}
          saving={saving}
          error={rebuildError}
          onReasonChange={setRebuildReason}
          onHintChange={setRebuildHint}
          onSubmit={submitRebuild}
          onClose={closeRebuildForm}
        />
      )}
    </div>
  );
}
