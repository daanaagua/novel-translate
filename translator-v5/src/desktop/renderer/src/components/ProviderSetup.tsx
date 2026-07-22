import { useEffect, useMemo, useState, type JSX } from "react";

import type {
  DesktopDiscoverModelsRequest,
  DesktopModelOption,
  DesktopModelSummary,
  DesktopOnboardingProvider,
  DesktopOnboardingState,
  DesktopResult,
  DesktopTestModelRequest,
  DesktopTestModelResult,
} from "../../../contracts.js";

interface ProviderSetupProps {
  providers: readonly DesktopOnboardingProvider[];
  activeModel?: DesktopModelSummary;
  busy: boolean;
  onDiscoverModels(request: DesktopDiscoverModelsRequest): Promise<DesktopResult<readonly DesktopModelOption[]>>;
  onTestModel(request: DesktopTestModelRequest): Promise<DesktopResult<DesktopTestModelResult>>;
  onForgetCredential(providerId: string): Promise<DesktopResult<DesktopOnboardingState>>;
}

function firstModel(provider: DesktopOnboardingProvider | undefined): string {
  return provider?.fallbackModelIds[0] ?? "";
}

export function ProviderSetup({
  providers,
  activeModel,
  busy,
  onDiscoverModels,
  onTestModel,
  onForgetCredential,
}: ProviderSetupProps): JSX.Element {
  const directProviders = useMemo(
    () => providers.filter((provider) => !provider.allowCustomBaseUrl),
    [providers],
  );
  const additionalProviders = useMemo(
    () => providers.filter((provider) => provider.allowCustomBaseUrl),
    [providers],
  );
  const [showMoreServices, setShowMoreServices] = useState(false);
  const [providerId, setProviderId] = useState(() => directProviders[0]?.id ?? additionalProviders[0]?.id ?? "");
  const [apiKey, setApiKey] = useState("");
  const [modelId, setModelId] = useState("");
  const [reasoningEffort, setReasoningEffort] = useState("");
  const [customBaseUrl, setCustomBaseUrl] = useState("");
  const [discoveredModels, setDiscoveredModels] = useState<readonly DesktopModelOption[]>([]);

  const selectedProvider = providers.find((provider) => provider.id === providerId) ?? directProviders[0] ?? additionalProviders[0];

  useEffect(() => {
    if (selectedProvider === undefined) return;
    setModelId(firstModel(selectedProvider));
    setReasoningEffort(selectedProvider.efforts[0] ?? "");
    setDiscoveredModels([]);
    setApiKey("");
  }, [selectedProvider?.id]);

  const modelOptions = discoveredModels.length > 0
    ? discoveredModels
    : (selectedProvider?.fallbackModelIds ?? []).map((id) => ({ id, displayName: id }));

  function chooseProvider(nextProvider: DesktopOnboardingProvider): void {
    if (nextProvider.allowCustomBaseUrl) setShowMoreServices(true);
    setProviderId(nextProvider.id);
  }

  async function discoverModels(): Promise<void> {
    if (selectedProvider === undefined) return;
    const result = await onDiscoverModels({
      providerId: selectedProvider.id,
      ...(apiKey === "" ? {} : { apiKey }),
      ...(selectedProvider.allowCustomBaseUrl && customBaseUrl !== "" ? { customBaseUrl } : {}),
    });
    if (result.ok) {
      setDiscoveredModels(result.value);
      if (result.value[0] !== undefined) setModelId(result.value[0].id);
    }
  }

  async function testConnection(): Promise<void> {
    if (selectedProvider === undefined || modelId === "") return;
    try {
      const request: DesktopTestModelRequest = {
        providerId: selectedProvider.id,
        ...(apiKey === "" ? {} : { apiKey }),
        modelId,
        ...(reasoningEffort === "" ? {} : { reasoningEffort }),
        ...(selectedProvider.allowCustomBaseUrl && customBaseUrl !== "" ? { customBaseUrl } : {}),
      };
      await onTestModel(request);
    } finally {
      // A key may enter a one-shot request but must never stay in renderer state.
      setApiKey("");
    }
  }

  const configured = selectedProvider !== undefined && selectedProvider.credentialStatus !== "missing";
  const canTest = selectedProvider !== undefined && modelId !== "" && !busy;

  return (
    <section className="provider-setup" aria-label="模型设置">
      <div className="provider-section">
        <p className="field-label">常用服务</p>
        <div className="provider-grid">
          {directProviders.map((provider) => (
            <button
              className={`provider-choice${provider.id === selectedProvider?.id ? " is-selected" : ""}`}
              type="button"
              key={provider.id}
              onClick={() => chooseProvider(provider)}
              aria-pressed={provider.id === selectedProvider?.id}
            >
              {provider.displayName}
            </button>
          ))}
        </div>
        {additionalProviders.length === 0 ? null : (
          <button
            className="text-button"
            type="button"
            aria-expanded={showMoreServices}
            onClick={() => setShowMoreServices((shown) => !shown)}
          >
            更多服务
          </button>
        )}
        {!showMoreServices ? null : (
          <div className="provider-grid provider-grid-more">
            {additionalProviders.map((provider) => (
              <button
                className={`provider-choice${provider.id === selectedProvider?.id ? " is-selected" : ""}`}
                type="button"
                key={provider.id}
                onClick={() => chooseProvider(provider)}
                aria-pressed={provider.id === selectedProvider?.id}
              >
                {provider.displayName}
              </button>
            ))}
          </div>
        )}
      </div>

      {selectedProvider === undefined ? (
        <p className="inline-note">暂时没有可用的模型服务。</p>
      ) : (
        <div className="provider-form">
          <label className="field">
            <span>API Key</span>
            <input
              aria-label="API Key"
              autoComplete="off"
              value={apiKey}
              placeholder={selectedProvider.keyPlaceholder}
              onChange={(event) => setApiKey(event.target.value)}
              type="password"
            />
          </label>

          {selectedProvider.allowCustomBaseUrl ? (
            <label className="field">
              <span>Base URL</span>
              <input
                aria-label="Base URL"
                autoComplete="off"
                value={customBaseUrl}
                placeholder="https://api.example.com/v1"
                onChange={(event) => setCustomBaseUrl(event.target.value)}
              />
            </label>
          ) : null}

          <div className="field model-field">
            <span>模型</span>
            <div className="model-row">
              <select value={modelId} onChange={(event) => setModelId(event.target.value)} aria-label="模型">
                {modelOptions.length === 0 ? <option value="">请选择模型</option> : null}
                {modelOptions.map((model) => <option value={model.id} key={model.id}>{model.displayName}</option>)}
              </select>
              <button className="quiet-button" type="button" onClick={() => { void discoverModels(); }} disabled={busy}>
                获取模型
              </button>
            </div>
          </div>

          {selectedProvider.allowManualModel ? (
            <label className="field">
              <span>或填写模型名称</span>
              <input
                value={modelId}
                placeholder="model-id"
                onChange={(event) => setModelId(event.target.value)}
              />
            </label>
          ) : null}

          {selectedProvider.efforts.length === 0 ? null : (
            <div className="field">
              <span>推理强度</span>
              <div className="effort-list" role="group" aria-label="推理强度">
                {selectedProvider.efforts.map((effort) => (
                  <button
                    className={`effort-choice${reasoningEffort === effort ? " is-selected" : ""}`}
                    type="button"
                    key={effort}
                    aria-pressed={reasoningEffort === effort}
                    onClick={() => setReasoningEffort(effort)}
                  >
                    {effort}
                  </button>
                ))}
              </div>
            </div>
          )}

          <div className="provider-actions">
            <button className="primary-button" type="button" onClick={() => { void testConnection(); }} disabled={!canTest}>
              测试连接
            </button>
            {configured ? (
              <button className="quiet-button" type="button" onClick={() => { void onForgetCredential(selectedProvider.id); }} disabled={busy}>
                忘记此密钥
              </button>
            ) : null}
            {activeModel?.providerId === selectedProvider.id ? <span className="model-status">当前已选择 {activeModel.modelId}</span> : null}
          </div>
        </div>
      )}
    </section>
  );
}
