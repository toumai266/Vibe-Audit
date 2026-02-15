import { useState, useEffect, useRef } from 'react';
import {
  fetchSessions,
  deleteSession as apiDeleteSession,
  fetchSettings,
  downloadDbBackup,
  uploadDbBackup,
} from './services/api';
import SessionDetail from './components/SessionDetail';
import SettingsModal from './components/SettingsModal';
import ConfirmModal from './components/ConfirmModal';
import DbBackupModal from './components/DbBackupModal';
import './App.css';
import ToastContainer from './components/Toast';
import { toast } from './components/toastBus';
import { SessionListSkeleton } from './components/Skeleton';
import { getLocale, t } from './i18n';

function formatTime(dateStr, locale, timeZone = 'local') {
  if (!dateStr) return '';
  const utcStr = dateStr.endsWith('Z') || dateStr.includes('+') ? dateStr : dateStr + 'Z';
  const date = new Date(utcStr);
  const options = {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit'
  };
  if (timeZone && timeZone !== 'local') options.timeZone = timeZone;
  return date.toLocaleString(locale, {
    ...options,
  });
}

function getStatus(session) {
  const phase = session.latest_phase;
  if (phase === 'pivot') {
    return { label: 'PIVOT', class: 'pivot' };
  }
  if (phase === 'new_phase') {
    return { label: 'PHASE', class: 'phase' };
  }
  return { label: 'INITIAL', class: 'initial' };
}

export default function App() {
  const [sessions, setSessions] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [selectedIds, setSelectedIds] = useState(new Set());
  const [selectionMode, setSelectionMode] = useState(false);
  const [loading, setLoading] = useState(true);
  const [connected, setConnected] = useState(false);
  const [interfaceLanguage, setInterfaceLanguage] = useState('en');
  const [responseLanguage, setResponseLanguage] = useState('en');
  const [timeZone, setTimeZone] = useState('local');
  const [showSettings, setShowSettings] = useState(false);
  const [showDbManager, setShowDbManager] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);
  const [pendingDeleteIds, setPendingDeleteIds] = useState([]);
  const [deletingSelected, setDeletingSelected] = useState(false);
  const [dbDownloading, setDbDownloading] = useState(false);
  const [dbUploading, setDbUploading] = useState(false);
  const dbFileInputRef = useRef(null);
  const locale = getLocale(interfaceLanguage);

  useEffect(() => {
    loadSessions();
    const interval = setInterval(loadSessions, 2000);
    return () => clearInterval(interval);
  }, []);

  useEffect(() => {
    loadInterfaceLanguage();
  }, []);

  useEffect(() => {
    document.documentElement.lang = interfaceLanguage;
  }, [interfaceLanguage]);

  useEffect(() => {
    const valid = new Set(sessions.map(s => s.id));
    setSelectedIds(prev => {
      let changed = false;
      const next = new Set();
      for (const id of prev) {
        if (valid.has(id)) next.add(id);
        else changed = true;
      }
      return changed ? next : prev;
    });
    if (selectedId && !valid.has(selectedId)) {
      setSelectedId(null);
    }
  }, [sessions, selectedId]);

  async function loadSessions() {
    try {
      const data = await fetchSessions();
      setSessions(data.sessions || []);
      setConnected(true);
    } catch {
      setConnected(false);
    } finally {
      setLoading(false);
    }
  }

  async function loadInterfaceLanguage() {
    try {
      const data = await fetchSettings();
      if (data?.interface_language) {
        setInterfaceLanguage(data.interface_language);
      }
      if (data?.response_language) {
        setResponseLanguage(data.response_language);
      }
      if (data?.time_zone) {
        setTimeZone(data.time_zone);
      }
    } catch {
      // Keep default language when settings API is unavailable.
    }
  }

  const filteredSessions = sessions.filter(session => {
    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase();
      const command = (session.cli_command || '').toLowerCase();
      const id = (session.id || '').toLowerCase();
      const goal = (session.baseline_goal || '').toLowerCase();
      return command.includes(q) || id.includes(q) || goal.includes(q);
    }
    return true;
  });

  const visibleSessionIds = filteredSessions.map(s => s.id);
  const selectedVisibleCount = visibleSessionIds.filter(id => selectedIds.has(id)).length;
  const allVisibleSelected = visibleSessionIds.length > 0 && selectedVisibleCount === visibleSessionIds.length;

  function toggleSessionChecked(sessionId) {
    setSelectedIds(prev => {
      const next = new Set(prev);
      if (next.has(sessionId)) next.delete(sessionId);
      else next.add(sessionId);
      return next;
    });
  }

  function toggleSelectAllVisible() {
    setSelectedIds(prev => {
      const next = new Set(prev);
      if (allVisibleSelected) {
        visibleSessionIds.forEach(id => next.delete(id));
      } else {
        visibleSessionIds.forEach(id => next.add(id));
      }
      return next;
    });
  }

  function toggleSelectionMode() {
    setSelectionMode(prev => {
      const next = !prev;
      if (!next) {
        setSelectedIds(new Set());
      }
      return next;
    });
  }

  function requestDeleteSelectedSessions() {
    const targetIds = visibleSessionIds.filter(id => selectedIds.has(id));
    if (targetIds.length === 0) return;
    setPendingDeleteIds(targetIds);
    setDeleteConfirmOpen(true);
  }

  async function confirmDeleteSelectedSessions() {
    if (pendingDeleteIds.length === 0) {
      setDeleteConfirmOpen(false);
      return;
    }
    setDeletingSelected(true);
    try {
      await Promise.all(pendingDeleteIds.map(id => apiDeleteSession(id)));
      setSelectedIds(prev => {
        const next = new Set(prev);
        pendingDeleteIds.forEach(id => next.delete(id));
        return next;
      });
      if (selectedId && pendingDeleteIds.includes(selectedId)) setSelectedId(null);
      toast(t(interfaceLanguage, 'app.deleteSelectedDone', { count: pendingDeleteIds.length }), 'success');
      loadSessions();
    } catch {
      toast(t(interfaceLanguage, 'app.deleteSelectedFailed'), 'error');
      loadSessions();
    } finally {
      setDeletingSelected(false);
      setDeleteConfirmOpen(false);
      setPendingDeleteIds([]);
    }
  }

  async function handleDownloadDbBackup() {
    if (dbDownloading || dbUploading) return;
    setDbDownloading(true);
    try {
      const { blob, filename } = await downloadDbBackup();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      a.click();
      URL.revokeObjectURL(url);
      toast(t(interfaceLanguage, 'app.dbDownloadDone'), 'success');
    } catch (error) {
      console.error('DB backup download failed:', error);
      toast(t(interfaceLanguage, 'app.dbDownloadFailed'), 'error');
    } finally {
      setDbDownloading(false);
    }
  }

  function handleOpenDbUpload() {
    if (dbDownloading || dbUploading) return;
    dbFileInputRef.current?.click();
  }

  async function handleDbFileSelected(e) {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (!file) return;
    setDbUploading(true);
    try {
      const result = await uploadDbBackup(file);
      toast(
        result?.restart_recommended
          ? t(interfaceLanguage, 'app.dbUploadDoneRestart')
          : t(interfaceLanguage, 'app.dbUploadDone'),
        'success'
      );
      await loadSessions();
      setShowDbManager(false);
    } catch (error) {
      console.error('DB backup upload failed:', error);
      toast(t(interfaceLanguage, 'app.dbUploadFailed'), 'error');
    } finally {
      setDbUploading(false);
    }
  }

  return (
    <div className="app">
      <header className="app-header">
        <div className="logo">
          <span className="logo-brand">Vibe Audit</span>
          <span className="logo-subtitle">/Agent Context Audit Console</span>
        </div>
        <div className="header-status">
          <button className="btn btn-xs" onClick={() => setShowDbManager(true)} disabled={dbDownloading || dbUploading}>
            {t(interfaceLanguage, 'app.dbManager')}
          </button>
          <button className="btn" onClick={() => setShowSettings(true)} style={{ fontSize: 12 }}>
            {t(interfaceLanguage, 'app.settings')}
          </button>
          <div className={`status-dot ${connected ? '' : 'idle'}`} />
          {connected ? t(interfaceLanguage, 'app.connected') : t(interfaceLanguage, 'app.disconnected')}
        </div>
      </header>

      <main className="app-main">
        <aside className="sidebar">
          <div className="sidebar-header">
            <span className="sidebar-title">{t(interfaceLanguage, 'app.sessions')}</span>
          </div>

          <div className="sidebar-search">
            <div className="sidebar-search-row">
              <input
                type="text"
                className="search-input"
                placeholder={t(interfaceLanguage, 'app.searchPlaceholder')}
                value={searchQuery}
                onChange={e => setSearchQuery(e.target.value)}
              />
              <button className="btn btn-xs sidebar-select-toggle" onClick={toggleSelectionMode}>
                {selectionMode ? t(interfaceLanguage, 'app.selectionDone') : t(interfaceLanguage, 'app.selectionMode')}
              </button>
            </div>
          </div>

          {selectionMode && (
            <div className="sidebar-selection-toolbar">
              <label className="sidebar-check-all">
                <input
                  type="checkbox"
                  checked={allVisibleSelected}
                  onChange={toggleSelectAllVisible}
                  disabled={visibleSessionIds.length === 0}
                />
                <span>{t(interfaceLanguage, 'app.selectAll')}</span>
              </label>
              <span className="sidebar-selected-count">
                {t(interfaceLanguage, 'app.selectedCount', { count: selectedVisibleCount })}
              </span>
              <button
                className="btn btn-danger btn-xs"
                onClick={requestDeleteSelectedSessions}
                disabled={selectedVisibleCount === 0}
                title={t(interfaceLanguage, 'app.deleteSelected')}
              >
                {t(interfaceLanguage, 'app.deleteSelectedSimple')}
              </button>
            </div>
          )}

          <div className="sessions-list">
            {loading ? (
              <SessionListSkeleton />
            ) : filteredSessions.length === 0 ? (
              <div className="empty-state">
                <div className="empty-state-title">{sessions.length === 0 ? t(interfaceLanguage, 'app.noSessions') : t(interfaceLanguage, 'app.noMatches')}</div>
                <div className="empty-state-desc">{sessions.length === 0 ? t(interfaceLanguage, 'app.startAgent') : t(interfaceLanguage, 'app.tryDifferentSearch')}</div>
              </div>
            ) : (
              filteredSessions.map(session => {
                const status = getStatus(session);
                return (
                  <div
                    key={session.id}
                    className={`session-card ${selectedId === session.id ? 'active' : ''}`}
                    onClick={() => {
                      if (selectionMode) {
                        toggleSessionChecked(session.id);
                        return;
                      }
                      setSelectedId(prev => (prev === session.id ? null : session.id));
                    }}
                  >
                    <div className="session-card-header">
                      {selectionMode && (
                        <label className="session-select" onClick={e => e.stopPropagation()}>
                          <input
                            type="checkbox"
                            checked={selectedIds.has(session.id)}
                            onChange={() => toggleSessionChecked(session.id)}
                          />
                        </label>
                      )}
                      <span className="session-time">{formatTime(session.start_ts, locale, timeZone)}</span>
                      <span className={`session-status ${status.class}`}>{status.label}</span>
                    </div>
                    {session.cli_command && (
                      <div className="session-command">{session.cli_command.split(':')[0]}</div>
                    )}
                    {session.baseline_goal && (
                      <div className="session-goal" title={session.baseline_goal}>
                        {session.baseline_goal.length > 60 ? session.baseline_goal.slice(0, 60) + '...' : session.baseline_goal}
                      </div>
                    )}
                    <div className="session-meta">
                      {session.flag_count > 0
                        ? t(interfaceLanguage, 'app.issues', { count: session.flag_count })
                        : t(interfaceLanguage, 'app.noIssues')}
                    </div>
                  </div>
                );
              })
            )}
          </div>
        </aside>

        <div className="content">
          {selectedId ? (
            <SessionDetail
              key={selectedId}
              sessionId={selectedId}
              interfaceLanguage={interfaceLanguage}
              responseLanguage={responseLanguage}
              timeZone={timeZone}
              onDelete={() => {
                setSelectedId(null);
                loadSessions();
              }}
            />
          ) : (
            <div className="empty-state">
              <div className="empty-state-title">{t(interfaceLanguage, 'app.selectSession')}</div>
              <div className="empty-state-desc">{t(interfaceLanguage, 'app.chooseSession')}</div>
            </div>
          )}
        </div>
      </main>

      {showSettings && (
        <SettingsModal
          onClose={() => setShowSettings(false)}
          interfaceLanguage={interfaceLanguage}
          onInterfaceLanguageChange={setInterfaceLanguage}
          onResponseLanguageChange={setResponseLanguage}
          onTimeZoneChange={setTimeZone}
        />
      )}

      {deleteConfirmOpen && (
        <ConfirmModal
          title={t(interfaceLanguage, 'app.deleteSelected')}
          message={t(interfaceLanguage, 'app.deleteSelectedConfirm', { count: pendingDeleteIds.length })}
          confirmLabel={t(interfaceLanguage, 'common.confirm')}
          cancelLabel={t(interfaceLanguage, 'common.cancel')}
          danger
          busy={deletingSelected}
          onConfirm={confirmDeleteSelectedSessions}
          onClose={() => {
            if (deletingSelected) return;
            setDeleteConfirmOpen(false);
            setPendingDeleteIds([]);
          }}
        />
      )}

      {showDbManager && (
        <DbBackupModal
          interfaceLanguage={interfaceLanguage}
          timeZone={timeZone}
          dbDownloading={dbDownloading}
          dbUploading={dbUploading}
          onDownload={handleDownloadDbBackup}
          onUpload={handleOpenDbUpload}
          onClose={() => {
            if (dbDownloading || dbUploading) return;
            setShowDbManager(false);
          }}
        />
      )}

      <input
        ref={dbFileInputRef}
        type="file"
        accept=".db,.sqlite,.sqlite3,application/x-sqlite3,application/octet-stream"
        style={{ display: 'none' }}
        onChange={handleDbFileSelected}
      />
      <ToastContainer />
    </div>
  );
}
