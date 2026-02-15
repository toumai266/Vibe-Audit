import { useState, useEffect, useCallback } from 'react';
import { fetchSettings, saveSettings, fetchModelCatalog } from '../services/api';
import { UI_LANGUAGE_OPTIONS, t } from '../i18n';
import './Modal.css';

const FALLBACK_MODELS = [
  { value: 'openai/gpt-5.2', label: 'GPT-5.2 (OpenAI)' },
  { value: 'openai/gpt-5-mini', label: 'GPT-5 mini (OpenAI)' },
  { value: 'openai/gpt-5-nano', label: 'GPT-5 nano (OpenAI)' },
  { value: 'openai/gpt-5.2-pro', label: 'GPT-5.2 pro (OpenAI)' },
  { value: 'openai/gpt-5', label: 'GPT-5 (OpenAI)' },
  { value: 'openai/gpt-4.1', label: 'GPT-4.1 (OpenAI)' },
  { value: 'gemini/gemini-3-pro-preview', label: 'Gemini 3 Pro Preview' },
  { value: 'gemini/gemini-3-flash-preview', label: 'Gemini 3 Flash Preview' },
  { value: 'gemini/gemini-2.5-pro', label: 'Gemini 2.5 Pro' },
  { value: 'gemini/gemini-2.5-flash', label: 'Gemini 2.5 Flash' },
  { value: 'gemini/gemini-2.5-flash-lite', label: 'Gemini 2.5 Flash-Lite' },
  { value: 'gemini/gemini-2.0-flash', label: 'Gemini 2.0 Flash' },
  { value: 'anthropic/claude-opus-4-6', label: 'Claude Opus 4.6' },
  { value: 'anthropic/claude-sonnet-4-5', label: 'Claude Sonnet 4.5' },
  { value: 'anthropic/claude-haiku-4-5', label: 'Claude Haiku 4.5' },
];
const FALLBACK_DEFAULT_MODEL = 'openai/gpt-5-nano';

const TIME_ZONE_OPTIONS = [
  { value: 'local', label: '__local__' },
  { value: 'UTC', label: 'UTC' },
  { value: 'Asia/Seoul', label: 'Asia/Seoul' },
  { value: 'Asia/Tokyo', label: 'Asia/Tokyo' },
  { value: 'Asia/Shanghai', label: 'Asia/Shanghai' },
  { value: 'America/Los_Angeles', label: 'America/Los_Angeles' },
  { value: 'America/New_York', label: 'America/New_York' },
  { value: 'Europe/London', label: 'Europe/London' },
  { value: 'Europe/Berlin', label: 'Europe/Berlin' },
];

export default function SettingsModal({
  onClose,
  interfaceLanguage = 'en',
  onInterfaceLanguageChange,
  onResponseLanguageChange,
  onTimeZoneChange,
}) {
  const [settings, setSettings] = useState(null);
  const [models, setModels] = useState(FALLBACK_MODELS);
  const [geminiKey, setGeminiKey] = useState('');
  const [openaiKey, setOpenaiKey] = useState('');
  const [anthropicKey, setAnthropicKey] = useState('');
  const [model, setModel] = useState(FALLBACK_DEFAULT_MODEL);
  const [customModel, setCustomModel] = useState('');
  const [responseLanguage, setResponseLanguage] = useState('en');
  const [uiLanguage, setUiLanguage] = useState(interfaceLanguage || 'en');
  const [timeZone, setTimeZone] = useState('local');
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');

  const loadSettings = useCallback(async () => {
    try {
      const [settingsData, catalogData] = await Promise.all([
        fetchSettings(),
        fetchModelCatalog().catch(() => null),
      ]);
      setSettings(settingsData);

      const modelOptions = Array.isArray(catalogData?.models) && catalogData.models.length > 0
        ? catalogData.models
        : FALLBACK_MODELS;
      const defaultModel = (typeof catalogData?.default_model === 'string' && catalogData.default_model.trim())
        ? catalogData.default_model.trim()
        : FALLBACK_DEFAULT_MODEL;

      setModels(modelOptions);

      const savedModel = settingsData.llm_model || defaultModel;
      if (modelOptions.some(m => m.value === savedModel)) {
        setModel(savedModel);
      } else {
        setModel('__custom__');
        setCustomModel(savedModel);
      }
      setResponseLanguage(settingsData.response_language || 'en');
      setUiLanguage(settingsData.interface_language || interfaceLanguage || 'en');
      setTimeZone(settingsData.time_zone || 'local');
    } catch (error) {
      console.error('Failed to load settings', error);
      setError(t(interfaceLanguage, 'settings.loadFailed'));
    }
  }, [interfaceLanguage]);

  useEffect(() => {
    loadSettings();
  }, [loadSettings]);

  async function handleSave() {
    setSaving(true);
    setError('');
    setMessage('');

    try {
      const selectedModel = model === '__custom__' ? customModel.trim() : model;
      if (!selectedModel) { setError(t(interfaceLanguage, 'settings.modelRequired')); setSaving(false); return; }
      const body = {
        llm_model: selectedModel,
        response_language: responseLanguage,
        interface_language: uiLanguage,
        time_zone: timeZone,
      };
      if (geminiKey.trim()) body.gemini_api_key = geminiKey.trim();
      if (openaiKey.trim()) body.openai_api_key = openaiKey.trim();
      if (anthropicKey.trim()) body.anthropic_api_key = anthropicKey.trim();

      const data = await saveSettings(body);
      setSettings(data.settings);
      setGeminiKey('');
      setOpenaiKey('');
      setAnthropicKey('');
      setMessage(t(interfaceLanguage, 'settings.saved'));
      if (onInterfaceLanguageChange) onInterfaceLanguageChange(uiLanguage);
      if (onResponseLanguageChange) onResponseLanguageChange(responseLanguage);
      if (onTimeZoneChange) onTimeZoneChange(timeZone);
      setTimeout(() => setMessage(''), 3000);
    } catch (error) {
      console.error('Failed to save settings', error);
      setError(t(interfaceLanguage, 'settings.connectFailed'));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <span className="modal-title">{t(interfaceLanguage, 'settings.title')}</span>
          <button className="modal-close" onClick={onClose}>x</button>
        </div>

        <div className="modal-body">
          {error && <div className="modal-error">{error}</div>}
          {message && (
            <div className="modal-info" style={{ marginBottom: 16 }}>
              {message}
            </div>
          )}

          <div className="form-field">
            <label>{t(interfaceLanguage, 'settings.llmModel')}</label>
            <select
              className="form-select"
              value={model}
              onChange={e => { setModel(e.target.value); if (e.target.value !== '__custom__') setCustomModel(''); }}
            >
              {models.map(m => (
                <option key={m.value} value={m.value}>{m.label}</option>
              ))}
              <option value="__custom__">{t(interfaceLanguage, 'settings.customModel')}</option>
            </select>
            {model === '__custom__' && (
              <input
                type="text"
                value={customModel}
                onChange={e => setCustomModel(e.target.value)}
                placeholder={t(interfaceLanguage, 'settings.customPlaceholder')}
                style={{ marginTop: 8 }}
              />
            )}
            <span className="form-hint">
              {t(interfaceLanguage, 'settings.modelHint')}
            </span>
          </div>

          <div className="form-field">
            <label>{t(interfaceLanguage, 'settings.aiResponseLanguage')}</label>
            <select
              className="form-select"
              value={responseLanguage}
              onChange={e => setResponseLanguage(e.target.value)}
            >
              {UI_LANGUAGE_OPTIONS.map(l => (
                <option key={l.value} value={l.value}>{l.label}</option>
              ))}
            </select>
            <span className="form-hint">
              {t(interfaceLanguage, 'settings.responseLanguageHint')}
            </span>
          </div>

          <div className="form-field">
            <label>{t(interfaceLanguage, 'settings.interfaceLanguage')}</label>
            <select
              className="form-select"
              value={uiLanguage}
              onChange={e => setUiLanguage(e.target.value)}
            >
              {UI_LANGUAGE_OPTIONS.map(l => (
                <option key={l.value} value={l.value}>{l.label}</option>
              ))}
            </select>
            <span className="form-hint">
              {t(interfaceLanguage, 'settings.interfaceLanguageHint')}
            </span>
          </div>

          <div className="form-field">
            <label>{t(interfaceLanguage, 'settings.timeZone')}</label>
            <select
              className="form-select"
              value={timeZone}
              onChange={e => setTimeZone(e.target.value)}
            >
              {TIME_ZONE_OPTIONS.map(tz => (
                <option key={tz.value} value={tz.value}>
                  {tz.label === '__local__' ? t(interfaceLanguage, 'settings.timeZoneLocal') : tz.label}
                </option>
              ))}
            </select>
            <span className="form-hint">
              {t(interfaceLanguage, 'settings.timeZoneHint')}
            </span>
          </div>

          <div className="form-field">
            <label>{t(interfaceLanguage, 'settings.geminiKey')}</label>
            <input
              type="password"
              value={geminiKey}
              onChange={e => setGeminiKey(e.target.value)}
              placeholder={settings?.has_gemini_key
                ? t(interfaceLanguage, 'settings.geminiPlaceholderCurrent', { value: settings.gemini_api_key })
                : t(interfaceLanguage, 'settings.geminiPlaceholder')}
            />
            <span className="form-hint">
              {settings?.has_gemini_key
                ? t(interfaceLanguage, 'settings.geminiHintCurrent')
                : t(interfaceLanguage, 'settings.geminiHint')}
            </span>
          </div>

          <div className="form-field">
            <label>{t(interfaceLanguage, 'settings.openaiKey')}</label>
            <input
              type="password"
              value={openaiKey}
              onChange={e => setOpenaiKey(e.target.value)}
              placeholder={settings?.has_openai_key
                ? t(interfaceLanguage, 'settings.openaiPlaceholderCurrent', { value: settings.openai_api_key })
                : t(interfaceLanguage, 'settings.openaiPlaceholder')}
            />
            <span className="form-hint">
              {settings?.has_openai_key
                ? t(interfaceLanguage, 'settings.openaiHintCurrent')
                : t(interfaceLanguage, 'settings.openaiHint')}
            </span>
          </div>

          <div className="form-field">
            <label>{t(interfaceLanguage, 'settings.anthropicKey')}</label>
            <input
              type="password"
              value={anthropicKey}
              onChange={e => setAnthropicKey(e.target.value)}
              placeholder={settings?.has_anthropic_key
                ? t(interfaceLanguage, 'settings.anthropicPlaceholderCurrent', { value: settings.anthropic_api_key })
                : t(interfaceLanguage, 'settings.anthropicPlaceholder')}
            />
            <span className="form-hint">
              {settings?.has_anthropic_key
                ? t(interfaceLanguage, 'settings.anthropicHintCurrent')
                : t(interfaceLanguage, 'settings.anthropicHint')}
            </span>
          </div>
        </div>

        <div className="modal-footer">
          <button className="btn" onClick={onClose}>{t(interfaceLanguage, 'common.cancel')}</button>
          <button
            className="btn btn-primary"
            onClick={handleSave}
            disabled={saving}
          >
            {saving ? t(interfaceLanguage, 'common.saving') : t(interfaceLanguage, 'common.save')}
          </button>
        </div>
      </div>
    </div>
  );
}
