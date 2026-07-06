import { useEffect, useState, type CSSProperties, type ReactNode } from "react";
import { Maximize2, Minimize2, Minus, Settings, Square, X } from "lucide-react";
import "./window-controls.css";

export type WindowControlsVariant = "minimal" | "prime";

type WindowControlsProps = {
  variant?: WindowControlsVariant;
  /** Accent color for minimal variant (focus cyan, terminal green, etc.) */
  accent?: string;
  className?: string;
  /** Prime only — e.g. settings gear before min/max/close */
  before?: ReactNode;
  onSettings?: () => void;
};

export function WindowControls({
  variant = "minimal",
  accent,
  className = "",
  before,
  onSettings,
}: WindowControlsProps) {
  const [maximized, setMaximized] = useState(false);

  useEffect(() => {
    const unsub = window.electronAPI?.onMaximizeChange?.((isMax) => setMaximized(isMax));
    return () => {
      if (typeof unsub === "function") unsub();
    };
  }, []);

  const api = window.electronAPI;
  const minimize = () => api?.minimizeWindow?.();
  const toggleMax = () => api?.toggleMaximize?.();
  const close = () => api?.closeWindow?.();

  if (variant === "prime") {
    return (
      <div className={`pr-winctl no-drag ${className}`.trim()}>
        {onSettings && (
          <button type="button" className="pr-iconbtn" title="Settings" onClick={onSettings}>
            <Settings size={13} />
          </button>
        )}
        {before}
        <button type="button" className="pr-iconbtn" title="Minimize" onClick={minimize}>
          <Minus size={13} />
        </button>
        <button type="button" className="pr-iconbtn" title={maximized ? "Restore" : "Maximize"} onClick={toggleMax}>
          {maximized ? <Minimize2 size={11} /> : <Square size={11} />}
        </button>
        <button type="button" className="pr-iconbtn pr-iconbtn--close" title="Close" onClick={close}>
          <X size={13} />
        </button>
      </div>
    );
  }

  const style = accent ? ({ "--winctl-accent": accent } as CSSProperties) : undefined;

  return (
    <div className={`winctl winctl--minimal no-drag ${className}`.trim()} style={style}>
      <button type="button" className="winctl-btn" title="Minimize" onClick={minimize}>
        <Minus size={12} />
      </button>
      <button type="button" className="winctl-btn" title={maximized ? "Restore" : "Maximize"} onClick={toggleMax}>
        {maximized ? <Minimize2 size={11} /> : <Maximize2 size={11} />}
      </button>
      <button type="button" className="winctl-btn winctl-btn--close" title="Close" onClick={close}>
        <X size={12} />
      </button>
    </div>
  );
}
