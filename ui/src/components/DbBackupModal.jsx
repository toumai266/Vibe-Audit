import { useCallback, useEffect, useState } from 'react';
import { deleteDbBackupArchives, fetchDbBackupState } from '../services/api';
import ConfirmModal from './ConfirmModal';
import { toast } from './toastBus';
import { getLocale, t } from '../i18n';
import './Modal.css';
import './DbBackupModal.css';

function formatTime(dateStr, locale, timeZone = 'local') {
  if (!dateStr) return '-';
  const normalized = dateStr.endsWith('Z') || dateStr.includes('+') ? dateStr : `${dateStr}Z`;
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return '-';
  const options = {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  };
  if (timeZone && timeZone !== 'local') options.timeZone = timeZone;
  return date.toLocaleString(locale, options);
}

export default function DbBackupModal({
  interfaceLanguage,
  timeZone = 'local',
  dbDownloading,
  dbUploading,
  onDownload,
  onUpload,
  onClose,
}) {
  const [state, setState] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);
  const [deletingArchives, setDeletingArchives] = useState(false);
  const locale = getLocale(interfaceLanguage);
  const busy = dbDownloading || dbUploading || deletingArchives;

  const loadState = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const data = await fetchDbBackupState();
      setState(data);
    } catch (e) {
      setError(e?.message || t(interfaceLanguage, 'app.dbStateFailed'));
      setState(null);
    } finally {
      setLoading(false);
    }
  }, [interfaceLanguage]);

  useEffect(() => {
    loadState();
  }, [loadState]);

  async function handleConfirmDeleteArchives() {
    if (deletingArchives) return;
    setDeletingArchives(true);
    try {
      const result = await deleteDbBackupArchives();
      const deletedCount = Number(result?.deleted_count || 0);
      if (deletedCount > 0) {
        toast(t(interfaceLanguage, 'app.dbDeleteBackupsDone', { count: deletedCount }), 'success');
      } else {
        toast(t(interfaceLanguage, 'app.dbDeleteBackupsNone'), 'info');
      }
      await loadState();
      setDeleteConfirmOpen(false);
    } catch (e) {
      toast(e?.message || t(interfaceLanguage, 'app.dbDeleteBackupsFailed'), 'error');
    } finally {
      setDeletingArchives(false);
    }
  }

  const archiveCount = Number(state?.backup_archives?.count || 0);

  return (
    <>
      <div className="modal-overlay" onClick={busy ? undefined : onClose}>
        <div className="modal modal-db-backup" onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <h3 className="modal-title">{t(interfaceLanguage, 'app.dbManager')}</h3>
          <button className="modal-close" onClick={onClose} disabled={busy}>×</button>
        </div>

        <div className="modal-body">
          {error && <div className="modal-error">{error}</div>}

          {loading ? (
            <div className="db-state-loading">{t(interfaceLanguage, 'app.dbStateLoading')}</div>
          ) : state ? (
            <div className="db-state-wrap">
              <div className="db-state-meta">
                <div className="db-state-meta-row">
                  <span>{t(interfaceLanguage, 'app.dbStateCapturedAt')}</span>
                  <strong>{formatTime(state.captured_at, locale, timeZone)}</strong>
                </div>
                <div className="db-state-meta-row">
                  <span>{t(interfaceLanguage, 'app.dbStateFile')}</span>
                  <strong>{state.file?.name || '-'}</strong>
                </div>
                <div className="db-state-meta-row">
                  <span>{t(interfaceLanguage, 'app.dbBackupArchives')}</span>
                  <strong>{archiveCount}</strong>
                </div>
              </div>

              <div className="db-state-counts">
                <div>{t(interfaceLanguage, 'app.dbStateSessions', { count: state.counts?.sessions ?? 0 })}</div>
                <div>{t(interfaceLanguage, 'app.dbStateEvents', { count: state.counts?.events ?? 0 })}</div>
                <div>{t(interfaceLanguage, 'app.dbStateTurns', { count: state.counts?.turns ?? 0 })}</div>
                <div>{t(interfaceLanguage, 'app.dbStateSnapshots', { count: state.counts?.snapshots ?? 0 })}</div>
              </div>

              {(state.orphans?.total ?? 0) > 0 && (
                <div className="db-state-warning">
                  {t(interfaceLanguage, 'app.dbStateOrphans', { count: state.orphans.total })}
                </div>
              )}

              {state.limited && (
                <div className="db-state-warning">
                  {t(interfaceLanguage, 'app.dbStateLimited')}
                </div>
              )}

              <div className="db-state-overwrite">
                {t(interfaceLanguage, 'app.dbOverwriteNotice')}
              </div>
            </div>
          ) : null}
        </div>

        <div className="modal-footer">
          <button className="btn" onClick={loadState} disabled={busy || loading}>
            {t(interfaceLanguage, 'app.dbStateRefresh')}
          </button>
          <button
            className="btn btn-danger btn-xs"
            onClick={() => setDeleteConfirmOpen(true)}
            disabled={busy || loading || !!error || archiveCount === 0}
          >
            {deletingArchives ? t(interfaceLanguage, 'app.dbDeletingBackups') : t(interfaceLanguage, 'app.dbDeleteBackups')}
          </button>
          <button className="btn btn-xs" onClick={onDownload} disabled={busy || loading || !!error}>
            {dbDownloading ? t(interfaceLanguage, 'app.dbDownloading') : t(interfaceLanguage, 'app.dbDownload')}
          </button>
          <button className="btn btn-xs" onClick={onUpload} disabled={busy || loading}>
            {dbUploading ? t(interfaceLanguage, 'app.dbUploading') : t(interfaceLanguage, 'app.dbUpload')}
          </button>
        </div>
        </div>
      </div>

      {deleteConfirmOpen && (
        <ConfirmModal
          title={t(interfaceLanguage, 'app.dbDeleteBackups')}
          message={t(interfaceLanguage, 'app.dbDeleteBackupsConfirm', { count: archiveCount })}
          confirmLabel={t(interfaceLanguage, 'common.confirm')}
          cancelLabel={t(interfaceLanguage, 'common.cancel')}
          danger
          busy={deletingArchives}
          onConfirm={handleConfirmDeleteArchives}
          onClose={() => {
            if (deletingArchives) return;
            setDeleteConfirmOpen(false);
          }}
        />
      )}
    </>
  );
}
