import { t } from '../i18n';
import './Modal.css';

export default function BaselineRebuildModal({
  interfaceLanguage = 'en',
  goal = '',
  reason = '',
  hint = '',
  saving = false,
  error = '',
  onReasonChange,
  onHintChange,
  onSubmit,
  onClose,
}) {
  return (
    <div className="modal-overlay" onClick={saving ? undefined : onClose}>
      <div className="modal" onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <span className="modal-title">{t(interfaceLanguage, 'context.rebuildModalTitle')}</span>
          <button className="modal-close" onClick={onClose} disabled={saving}>x</button>
        </div>

        <div className="modal-body">
          {error && <div className="modal-error">{error}</div>}

          <div className="modal-info" style={{ marginBottom: 16 }}>
            <strong>{t(interfaceLanguage, 'context.goal')}:</strong> {goal || '-'}
          </div>

          <div className="form-field">
            <label>{t(interfaceLanguage, 'context.rebuildReasonLabel')}</label>
            <textarea
              rows={4}
              value={reason}
              placeholder={t(interfaceLanguage, 'context.rebuildReasonPlaceholder')}
              onChange={e => onReasonChange(e.target.value)}
            />
          </div>

          <div className="form-field">
            <label>{t(interfaceLanguage, 'context.rebuildHintLabel')}</label>
            <input
              type="text"
              value={hint}
              placeholder={t(interfaceLanguage, 'context.rebuildHintPlaceholder')}
              onChange={e => onHintChange(e.target.value)}
            />
            <span className="form-hint">{t(interfaceLanguage, 'context.rebuildBaselineHint')}</span>
          </div>
        </div>

        <div className="modal-footer">
          <button className="btn" onClick={onClose} disabled={saving}>{t(interfaceLanguage, 'common.cancel')}</button>
          <button className="btn btn-primary" onClick={onSubmit} disabled={saving}>
            {saving ? t(interfaceLanguage, 'context.rebuildSaving') : t(interfaceLanguage, 'context.rebuildConfirm')}
          </button>
        </div>
      </div>
    </div>
  );
}
