import './Modal.css';

export default function ConfirmModal({
  title,
  message,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  danger = false,
  busy = false,
  onConfirm,
  onClose,
}) {
  return (
    <div className="modal-overlay" onClick={busy ? undefined : onClose}>
      <div className="modal modal-confirm" onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <span className="modal-title">{title}</span>
          <button className="modal-close" onClick={onClose} disabled={busy}>x</button>
        </div>
        <div className="modal-body">
          <div className="modal-info">{message}</div>
        </div>
        <div className="modal-footer">
          <button className="btn" onClick={onClose} disabled={busy}>{cancelLabel}</button>
          <button className={`btn ${danger ? 'btn-danger' : 'btn-primary'}`} onClick={onConfirm} disabled={busy}>
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
