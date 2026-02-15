import { useState, useEffect, useCallback, useMemo } from 'react';
import { fetchTurns as fetchSessionTurns } from '../services/api';
import './ConversationTimeline.css';
import { TimelineSkeleton } from './Skeleton';
import { getLocale, t } from '../i18n';
import { buildConversationEntries } from './conversationTimelineUtils';

export default function ConversationTimeline({
  sessionId,
  interfaceLanguage = 'en',
  timeZone = 'local',
  events: incomingEvents = [],
  turnRows: incomingTurnRows = [],
  phases = [],
  phaseControlAction = null,
}) {
  const [events, setEvents] = useState(incomingEvents);
  const [turnRows, setTurnRows] = useState(incomingTurnRows);
  const [collapsedPhaseOverrides, setCollapsedPhaseOverrides] = useState({});
  const [loading, setLoading] = useState(!(incomingEvents.length || incomingTurnRows.length));
  const locale = getLocale(interfaceLanguage);
  const initialTurnCount = incomingTurnRows?.length || 0;

  const isSameTurns = useCallback((prevRows, nextRows) => {
    if (prevRows.length !== nextRows.length) return false;
    for (let i = 0; i < prevRows.length; i += 1) {
      const prev = prevRows[i];
      const next = nextRows[i];
      if (!next) return false;
      if (prev.id !== next.id) return false;
      if (prev.role !== next.role) return false;
      if (prev.ts !== next.ts) return false;
      if (prev.content !== next.content) return false;
    }
    return true;
  }, []);

  const loadTurns = useCallback(async () => {
    try {
      const turnsData = await fetchSessionTurns(sessionId);
      const nextTurns = turnsData.turns || [];
      setTurnRows(prevRows => (isSameTurns(prevRows, nextTurns) ? prevRows : nextTurns));
    } catch (error) {
      console.error('Failed to load turns:', error);
    } finally {
      setLoading(false);
    }
  }, [sessionId, isSameTurns]);

  useEffect(() => {
    setEvents(incomingEvents || []);
    setTurnRows(incomingTurnRows || []);
    if ((incomingEvents?.length || 0) > 0 || (incomingTurnRows?.length || 0) > 0) {
      setLoading(false);
    }
  }, [incomingEvents, incomingTurnRows]);

  useEffect(() => {
    const hasInitialTurns = initialTurnCount > 0;
    if (!hasInitialTurns) {
      loadTurns();
    }
    const interval = setInterval(loadTurns, 2000);
    return () => clearInterval(interval);
  }, [sessionId, initialTurnCount, loadTurns]);

  useEffect(() => {
    setCollapsedPhaseOverrides({});
  }, [sessionId]);

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

  function getRoleLabel(turn) {
    switch (turn.role) {
      case 'user': return 'USER';
      case 'assistant': return 'AGENT';
      case 'tool': return `${turn.tool}`;
      default: return turn.type;
    }
  }

  function getPhaseClassBadge(classification) {
    const map = {
      initial: { label: t(interfaceLanguage, 'context.phase.initial'), class: 'phase-initial' },
      continuation: { label: t(interfaceLanguage, 'context.phase.continuation'), class: 'phase-continuation' },
      new_phase: { label: t(interfaceLanguage, 'context.phase.new'), class: 'phase-new' },
      pivot: { label: t(interfaceLanguage, 'context.phase.pivot'), class: 'phase-pivot' },
    };
    return map[classification] || { label: (classification || 'UNKNOWN').toUpperCase(), class: '' };
  }

  function toUtcMs(ts) {
    if (!ts || typeof ts !== 'string') return Number.NaN;
    const utcTs = ts.endsWith('Z') || ts.includes('+') ? ts : `${ts}Z`;
    return new Date(utcTs).getTime();
  }

  function groupTurns(turns) {
    const groups = [];
    let currentToolGroup = null;

    for (const turn of turns) {
      if (turn.role === 'tool') {
        if (!currentToolGroup) {
          currentToolGroup = { type: 'tool-group', tools: [] };
        }
        currentToolGroup.tools.push(turn);
      } else {
        if (currentToolGroup) {
          groups.push(currentToolGroup);
          currentToolGroup = null;
        }
        groups.push({ type: 'turn', turn });
      }
    }
    if (currentToolGroup) {
      groups.push(currentToolGroup);
    }

    return groups;
  }

  const turns = buildConversationEntries(events, turnRows);
  const phaseWindows = useMemo(() => {
    const ordered = [...(phases || [])].sort((a, b) => {
      if (a.phase_num !== b.phase_num) return a.phase_num - b.phase_num;
      return toUtcMs(a.started_at) - toUtcMs(b.started_at);
    });
    return ordered.map((phase, idx) => ({
      ...phase,
      startMs: toUtcMs(phase.started_at),
      endMs: idx < ordered.length - 1 ? toUtcMs(ordered[idx + 1].started_at) : Number.POSITIVE_INFINITY,
      key: phase.id || `phase-${phase.phase_num}`,
    }));
  }, [phases]);

  const phaseGroups = useMemo(() => {
    if (turns.length === 0) return [];
    if (phaseWindows.length <= 1) {
      return [{ key: phaseWindows[0]?.key || 'all', phase: phaseWindows[0] || null, turns }];
    }

    const grouped = new Map();
    const unphased = [];

    for (const turn of turns) {
      const turnMs = toUtcMs(turn.ts);
      let matched = null;
      if (Number.isFinite(turnMs)) {
        for (const window of phaseWindows) {
          if (!Number.isFinite(window.startMs)) continue;
          if (turnMs >= window.startMs && turnMs < window.endMs) {
            matched = window;
            break;
          }
        }
      }

      if (!matched) {
        unphased.push(turn);
        continue;
      }

      if (!grouped.has(matched.key)) {
        grouped.set(matched.key, { key: matched.key, phase: matched, turns: [] });
      }
      grouped.get(matched.key).turns.push(turn);
    }

    const ordered = phaseWindows
      .map(window => grouped.get(window.key))
      .filter(Boolean);

    if (unphased.length > 0) {
      ordered.unshift({ key: 'unphased', phase: null, turns: unphased });
    }

    return ordered;
  }, [turns, phaseWindows]);

  const latestPhaseGroupKey = useMemo(() => {
    for (let i = phaseGroups.length - 1; i >= 0; i -= 1) {
      if (phaseGroups[i]?.phase) return phaseGroups[i].key;
    }
    return null;
  }, [phaseGroups]);

  function isPhaseCollapsed(groupKey, hasPhase) {
    if (Object.prototype.hasOwnProperty.call(collapsedPhaseOverrides, groupKey)) {
      return !!collapsedPhaseOverrides[groupKey];
    }
    if (!hasPhase) return false;
    return groupKey !== latestPhaseGroupKey;
  }

  function togglePhaseCollapse(groupKey, hasPhase) {
    setCollapsedPhaseOverrides((prev) => {
      const current = Object.prototype.hasOwnProperty.call(prev, groupKey)
        ? !!prev[groupKey]
        : (hasPhase ? groupKey !== latestPhaseGroupKey : false);
      return {
        ...prev,
        [groupKey]: !current,
      };
    });
  }

  function setAllPhaseGroupsCollapsed(collapsed) {
    const next = {};
    phaseGroups.forEach((group) => {
      next[group.key] = collapsed;
    });
    setCollapsedPhaseOverrides(next);
  }

  const rawEventCount = events.length;
  const entryCount = turns.length;
  const hasPhaseAccordion = phaseWindows.length > 1 && phaseGroups.length > 0;

  useEffect(() => {
    if (!phaseControlAction || !hasPhaseAccordion) return;
    if (phaseControlAction.action === 'collapse') {
      setAllPhaseGroupsCollapsed(true);
    } else if (phaseControlAction.action === 'expand') {
      setAllPhaseGroupsCollapsed(false);
    }
  }, [phaseControlAction, hasPhaseAccordion]);

  function renderTurnGroups(groups, keyPrefix = '') {
    return groups.map((group, gIdx) => {
      if (group.type === 'tool-group') {
        return (
          <details key={`${keyPrefix}tg-${gIdx}`} className="tool-group-details">
            <summary className="tool-group-summary">
              <span className="tool-group-icon">{'\u{1F527}'}</span>
              <span className="tool-group-label">
                {t(interfaceLanguage, 'timeline.toolCalls', {
                  count: group.tools.length,
                  suffix: group.tools.length > 1 ? 's' : '',
                })}
              </span>
              <span className="tool-group-names">
                {[...new Set(group.tools.map(t => t.tool))].join(', ')}
              </span>
            </summary>
            <div className="tool-group-content">
              {group.tools.map(turn => (
                <div key={turn.id} className="turn-item tool">
                  <div className="turn-header">
                    <span className="turn-role">
                      {'\u{1F527}'} {getRoleLabel(turn)}
                    </span>
                    <span className="turn-time">{formatTime(turn.ts)}</span>
                  </div>
                  <div className="turn-content">{turn.content}</div>
                </div>
              ))}
            </div>
          </details>
        );
      }

      const turn = group.turn;
      return (
        <div key={turn.id} className={`turn-item ${turn.role}`}>
          <div className="turn-body">
            <div className="turn-header">
              <span className="turn-role">
                {turn.role === 'user' ? '\u{1F464}' : '\u{1F916}'} {getRoleLabel(turn)}
              </span>
              {turn.role === 'assistant' && turn.kind === 'progress' && (
                <span className="turn-stage-badge">{t(interfaceLanguage, 'timeline.working')}</span>
              )}
              <span className="turn-time">{formatTime(turn.ts)}</span>
            </div>
            {turn.thought && (
              <details className="turn-thought">
                <summary>{'\u{1F9E0}'} {t(interfaceLanguage, 'timeline.agentThought')}</summary>
                <div className="thought-content">{turn.thought}</div>
              </details>
            )}
            <div className="turn-content">{turn.content}</div>
          </div>
        </div>
      );
    });
  }

  if (loading) {
    return <TimelineSkeleton />;
  }

  return (
    <div className="conversation-timeline conversation-timeline--inline">
      <div className="turns-list">
        {turns.length === 0 ? (
            <div className="empty-turns">
            <div className="empty-icon">...</div>
            <div className="empty-text">{t(interfaceLanguage, 'timeline.noEvents')}</div>
            <div className="empty-hint">{t(interfaceLanguage, 'timeline.noEventsHint')}</div>
          </div>
        ) : (
          hasPhaseAccordion ? (
            phaseGroups.map((phaseGroup) => {
              const groupedTurns = groupTurns(phaseGroup.turns);
              const hasPhase = !!phaseGroup.phase;
              const phaseBadge = hasPhase ? getPhaseClassBadge(phaseGroup.phase.classification) : null;
              const collapsed = isPhaseCollapsed(phaseGroup.key, hasPhase);
              return (
                <div key={phaseGroup.key} className={`timeline-phase-group ${phaseBadge?.class || ''}`}>
                  <button
                    type="button"
                    className={`timeline-phase-summary ${phaseBadge?.class || ''} ${collapsed ? 'collapsed' : ''}`}
                    onClick={() => togglePhaseCollapse(phaseGroup.key, hasPhase)}
                  >
                    <span className="timeline-phase-toggle">{collapsed ? '\u25b6' : '\u25bc'}</span>
                    {hasPhase ? (
                      <>
                        <span className={`timeline-phase-badge ${phaseBadge.class}`}>{phaseBadge.label}</span>
                        <span className="timeline-phase-title">{`${t(interfaceLanguage, 'timeline.phase')} #${phaseGroup.phase.phase_num}`}</span>
                        {phaseGroup.phase.summary && (
                          <span className="timeline-phase-text">
                            {phaseGroup.phase.summary.length > 72
                              ? `${phaseGroup.phase.summary.slice(0, 72)}...`
                              : phaseGroup.phase.summary}
                          </span>
                        )}
                      </>
                    ) : (
                      <>
                        <span className="timeline-phase-badge phase-initial">{t(interfaceLanguage, 'context.phase.initial')}</span>
                        <span className="timeline-phase-title">{t(interfaceLanguage, 'timeline.firstPrompt')}</span>
                      </>
                    )}
                    <span className="timeline-phase-count">
                      {t(interfaceLanguage, 'timeline.entries', { count: phaseGroup.turns.length })}
                    </span>
                  </button>
                  {!collapsed && (
                    <div className="timeline-phase-content">
                      {renderTurnGroups(groupedTurns, `${phaseGroup.key}-`)}
                    </div>
                  )}
                </div>
              );
            })
          ) : (
            renderTurnGroups(groupTurns(turns))
          )
        )}
      </div>

      <div className="timeline-stats">
        <span>{t(interfaceLanguage, 'timeline.entries', { count: entryCount })}</span>
        <span title={t(interfaceLanguage, 'timeline.rawEventsHint')}>{t(interfaceLanguage, 'timeline.rawEvents', { count: rawEventCount })}</span>
        <span>{t(interfaceLanguage, 'timeline.user', { count: turns.filter(t => t.role === 'user').length })}</span>
        <span>{t(interfaceLanguage, 'timeline.agent', { count: turns.filter(t => t.role === 'assistant').length })}</span>
        <span>{t(interfaceLanguage, 'timeline.tools', { count: turns.filter(t => t.role === 'tool').length })}</span>
      </div>
    </div>
  );
}
