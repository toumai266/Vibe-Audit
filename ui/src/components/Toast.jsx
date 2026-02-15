import { useState, useEffect, useCallback } from 'react';
import './Toast.css';
import { setToastHandler, clearToastHandler } from './toastBus';

const TOAST_AUTO_DISMISS_MS = 5500;

export default function ToastContainer() {
  const [toasts, setToasts] = useState([]);

  const addToast = useCallback((t) => {
    setToasts(prev => [...prev, t]);
    setTimeout(() => {
      setToasts(prev => prev.filter(x => x.id !== t.id));
    }, TOAST_AUTO_DISMISS_MS);
  }, []);

  useEffect(() => {
    setToastHandler(addToast);
    return () => { clearToastHandler(addToast); };
  }, [addToast]);

  function removeToast(id) {
    setToasts(prev => prev.filter(x => x.id !== id));
  }

  const icons = { success: '✓', error: '✕', warning: '!', info: 'i' };

  return (
    <div className="toast-container">
      {toasts.map(t => (
        <div key={t.id} className={`toast toast-${t.type}`}>
          <span className="toast-icon">{icons[t.type] || icons.info}</span>
          <span className="toast-message">{t.message}</span>
          <button className="toast-close" onClick={() => removeToast(t.id)}>×</button>
        </div>
      ))}
    </div>
  );
}
