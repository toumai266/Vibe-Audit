function asText(value) {
  if (value == null) return '';
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function normalizeForDedupe(text) {
  return String(text || '').replace(/\s+/g, ' ').trim();
}

export function buildConversationEntries(events = [], turnRows = []) {
  const turns = [];
  const finalAssistantTexts = new Set(
    turnRows
      .filter(t => t.role === 'assistant')
      .map(t => normalizeForDedupe(t.content))
      .filter(Boolean)
  );

  for (const t of turnRows) {
    if (t.role !== 'user' && t.role !== 'assistant') continue;
    turns.push({
      id: `turn-${t.id}`,
      role: t.role,
      kind: t.role === 'assistant' ? 'final' : undefined,
      content: asText(t.content),
      ts: t.ts
    });
  }

  for (const event of events) {
    const payload = event.payload || {};
    if (event.type === 'assistant_response' || event.type === 'agent_response' || event.type === 'assistant_update') {
      const content = asText(payload.content || payload.response || payload.text || '');
      const normalized = normalizeForDedupe(content);
      if (!normalized || finalAssistantTexts.has(normalized)) {
        continue;
      }
      turns.push({
        id: `event-${event.id}`,
        role: 'assistant',
        kind: 'progress',
        content,
        thought: payload.thought || null,
        ts: event.ts
      });
      continue;
    }

    if (event.type !== 'tool_use' && event.type !== 'tool_result') continue;

    const tool = payload.tool || payload.tool_name || payload.name || 'unknown';
    const toolPayload = event.type === 'tool_use'
      ? (payload.inputs ?? payload.input ?? payload)
      : (payload.outputs ?? payload.output ?? payload.result ?? payload);

    turns.push({
      id: `event-${event.id}`,
      role: 'tool',
      type: event.type,
      tool,
      content: asText(toolPayload),
      ts: event.ts
    });
  }

  turns.sort((a, b) => new Date(a.ts).getTime() - new Date(b.ts).getTime());
  return turns;
}

export function getConversationEntryCount(events = [], turnRows = []) {
  return buildConversationEntries(events, turnRows).length;
}
