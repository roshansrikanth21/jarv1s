export type ApiKeyStatus = { secure: boolean; groq: boolean; anthropic: boolean };

declare global {
  interface Window {
    electronAPI?: {
      minimizeWindow?: () => void;
      toggleMaximize?: () => void;
      closeWindow?: () => void;
      restartBackend?: () => Promise<void>;
      openTrading?: () => Promise<{ ok: boolean; error?: string }>;
      onMaximizeChange?: (cb: (isMax: boolean) => void) => (() => void) | void;
      getApiKeyStatus?: () => Promise<ApiKeyStatus>;
      setApiKeys?: (keys: Record<string, string>) => Promise<ApiKeyStatus>;
      openExternal?: (url: string) => void;
    };
  }
}

export {};
