const DEFAULT_API_BASE = 'http://localhost:8000';
export const API_BASE = (import.meta.env.VITE_API_BASE || DEFAULT_API_BASE).replace(/\/+$/, '');

function buildUrl(path) {
  return `${API_BASE}${path.startsWith('/') ? path : `/${path}`}`;
}

export async function fetchSessions(limit = 20, offset = 0) {
  const res = await fetch(buildUrl(`/sessions?limit=${limit}&offset=${offset}`));
  if (!res.ok) throw new Error('Failed to fetch sessions');
  return res.json();
}

export async function fetchSession(sessionId) {
  const res = await fetch(buildUrl(`/sessions/${sessionId}`));
  if (!res.ok) throw new Error('Failed to fetch session');
  return res.json();
}

export async function deleteSession(sessionId) {
  const res = await fetch(buildUrl(`/sessions/${sessionId}`), { method: 'DELETE' });
  if (!res.ok) throw new Error('Failed to delete session');
  return res.json();
}

export async function sealPending(sessionId) {
  const res = await fetch(buildUrl(`/api/sessions/${sessionId}/seal`), { method: 'POST' });
  if (!res.ok) throw new Error('Failed to seal pending turns');
  return res.json();
}

export async function fetchBaseline(sessionId) {
  const res = await fetch(buildUrl(`/sessions/${sessionId}/baseline`));
  if (!res.ok) return null;
  return res.json();
}

export async function fetchSnapshots(sessionId) {
  const res = await fetch(buildUrl(`/sessions/${sessionId}/snapshots`));
  if (!res.ok) throw new Error('Failed to fetch snapshots');
  return res.json();
}

export async function fetchFlags(sessionId) {
  const res = await fetch(buildUrl(`/sessions/${sessionId}/flags`));
  if (!res.ok) throw new Error('Failed to fetch flags');
  return res.json();
}

export async function fetchReport(sessionId) {
  const res = await fetch(buildUrl(`/sessions/${sessionId}/report`));
  if (!res.ok) throw new Error('Failed to fetch report');
  return res.json();
}

export async function fetchEvents(sessionId) {
  const res = await fetch(buildUrl(`/sessions/${sessionId}/events`));
  if (!res.ok) throw new Error('Failed to fetch events');
  return res.json();
}

export async function fetchTurns(sessionId) {
  const res = await fetch(buildUrl(`/sessions/${sessionId}/turns`));
  if (!res.ok) throw new Error('Failed to fetch turns');
  return res.json();
}

export function subscribeToSession(sessionId, onEvent, handlers = {}) {
  let es = null;
  let closed = false;
  let reconnectAttempt = 0;
  let reconnectTimer = null;

  function scheduleReconnect() {
    if (closed || reconnectTimer) return;
    const delay = Math.min(1000 * (2 ** reconnectAttempt), 10000);
    reconnectAttempt += 1;
    console.log(`[SSE] reconnecting in ${delay}ms...`);
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      connect();
    }, delay);
  }

  function connect() {
    if (closed) return;
    es = new EventSource(buildUrl(`/sessions/${sessionId}/stream`));

    es.onmessage = (event) => {
      try {
        if (!event.data) return; // heartbeat
        const data = JSON.parse(event.data);
        const eventType = data.event || 'message';
        if (handlers[eventType]) {
          handlers[eventType](data.data || data);
        }
        onEvent(data);
      } catch {
        // ignore parse errors (heartbeat empty data)
      }
    };

    es.onopen = () => {
      reconnectAttempt = 0;
      console.log(`[SSE] connected to session ${sessionId}`);
    };

    es.onerror = () => {
      if (es) {
        es.close();
      }
      scheduleReconnect();
    };
  }

  connect();

  return () => {
    closed = true;
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    if (es) es.close();
  };
}

export async function updateBaseline(sessionId, data) {
  const res = await fetch(buildUrl(`/sessions/${sessionId}/baseline`), {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error('Failed to update baseline');
  return res.json();
}

export async function regenerateBaseline(sessionId, data) {
  const res = await fetch(buildUrl(`/sessions/${sessionId}/baseline/rebuild`), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  if (!res.ok) {
    let payload = null;
    try {
      payload = await res.json();
    } catch {
      payload = null;
    }
    const detail = payload?.detail;
    const message = typeof detail === 'string'
      ? detail
      : detail?.message || 'Failed to regenerate baseline';
    const err = new Error(message);
    err.status = res.status;
    if (detail && typeof detail === 'object') {
      Object.assign(err, detail);
    }
    throw err;
  }
  return res.json();
}

export async function exportSession(sessionId, format = 'json') {
  const res = await fetch(buildUrl(`/sessions/${sessionId}/export?format=${format}`));
  if (!res.ok) throw new Error('Failed to export session');
  return res.json();
}

// Phase and Baseline versioning APIs

export async function fetchBaselines(sessionId) {
  const res = await fetch(buildUrl(`/sessions/${sessionId}/baselines`));
  if (!res.ok) throw new Error('Failed to fetch baselines');
  return res.json();
}

export async function fetchBaselineVersion(sessionId, version) {
  const res = await fetch(buildUrl(`/sessions/${sessionId}/baseline/${version}`));
  if (!res.ok) throw new Error(`Failed to fetch baseline version ${version}`);
  return res.json();
}

export async function fetchPhases(sessionId) {
  const res = await fetch(buildUrl(`/sessions/${sessionId}/phases`));
  if (!res.ok) throw new Error('Failed to fetch phases');
  return res.json();
}

export async function fetchSettings() {
  const res = await fetch(buildUrl('/settings'));
  if (!res.ok) throw new Error('Failed to load settings');
  return res.json();
}

export async function saveSettings(data) {
  const res = await fetch(buildUrl('/settings'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error('Failed to save settings');
  return res.json();
}

export async function fetchModelCatalog() {
  const res = await fetch(buildUrl('/settings/models'));
  if (!res.ok) throw new Error('Failed to load model catalog');
  return res.json();
}

export async function downloadDbBackup() {
  const res = await fetch(buildUrl(`/backup/db?ts=${Date.now()}`), {
    cache: 'no-store',
    headers: { 'Cache-Control': 'no-store' },
  });
  if (!res.ok) {
    let detail = 'Failed to download DB backup';
    try {
      const data = await res.json();
      detail = data?.detail || detail;
    } catch {
      // keep default detail
    }
    throw new Error(detail);
  }
  const blob = await res.blob();
  const dispo = res.headers.get('content-disposition') || '';
  const match = dispo.match(/filename="?([^"]+)"?/i);
  const filename = match?.[1] || 'vibe-audit-backup.db';
  return { blob, filename };
}

export async function fetchDbBackupState() {
  const res = await fetch(buildUrl(`/backup/db/state?ts=${Date.now()}`), {
    cache: 'no-store',
    headers: { 'Cache-Control': 'no-store' },
  });
  if (res.status === 404) {
    // Backward compatibility for older daemons without /backup/db/state.
    return fetchDbBackupStateFallback();
  }
  if (!res.ok) {
    let detail = 'Failed to fetch DB state';
    try {
      const data = await res.json();
      detail = data?.detail || detail;
    } catch {
      // keep default detail
    }
    throw new Error(detail);
  }
  return res.json();
}

async function fetchDbBackupStateFallback() {
  let sessionCount = 0;
  try {
    const sessionsRes = await fetch(buildUrl('/sessions?limit=1&offset=0'), {
      cache: 'no-store',
      headers: { 'Cache-Control': 'no-store' },
    });
    if (sessionsRes.ok) {
      const sessionsData = await sessionsRes.json();
      sessionCount = Number(sessionsData?.total || 0);
    }
  } catch {
    // keep 0
  }

  return {
    captured_at: new Date().toISOString(),
    file: {
      name: 'vibe_audit.db',
      size_bytes: null,
      modified_at: null,
    },
    counts: {
      sessions: sessionCount,
      events: 0,
      turns: 0,
      baselines: 0,
      phases: 0,
      snapshots: 0,
      flags: 0,
    },
    orphans: {
      snapshots_without_session: 0,
      snapshots_without_event: 0,
      flags_without_session: 0,
      flags_without_snapshot: 0,
      total: 0,
    },
    limited: true,
  };
}

export async function uploadDbBackup(file) {
  const res = await fetch(buildUrl('/backup/db'), {
    method: 'POST',
    headers: {
      'Content-Type': 'application/octet-stream',
      'X-Backup-Filename': file?.name || 'backup.db',
    },
    body: file,
  });
  if (!res.ok) {
    let detail = 'Failed to upload DB backup';
    try {
      const data = await res.json();
      detail = data?.detail || detail;
    } catch {
      // keep default detail
    }
    throw new Error(detail);
  }
  return res.json();
}

export async function deleteDbBackupArchives() {
  const res = await fetch(buildUrl('/backup/db/archives'), {
    method: 'DELETE',
  });
  if (!res.ok) {
    let detail = 'Failed to delete DB backup archives';
    try {
      const data = await res.json();
      detail = data?.detail || detail;
    } catch {
      // keep default detail
    }
    throw new Error(detail);
  }
  return res.json();
}

