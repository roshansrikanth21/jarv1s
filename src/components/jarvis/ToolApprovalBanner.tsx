/** Modal prompt when the backend asks for privileged-tool confirmation (e.g. shell). */
import type { CSSProperties } from "react";

export type ToolApprovalRequest = {
  id: string;
  tool: string;
  summary: string;
  args?: Record<string, unknown>;
  timeoutSec?: number;
};

type Props = {
  request: ToolApprovalRequest | null;
  onRespond: (id: string, approved: boolean) => void;
};

const overlay: CSSProperties = {
  position: "fixed",
  inset: 0,
  zIndex: 10050,
  background: "rgba(0,0,0,0.55)",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  padding: 16,
};

const card: CSSProperties = {
  width: "min(440px, 100%)",
  background: "#12151a",
  color: "#e8ecef",
  border: "1px solid oklch(0.68 0.22 38 / 0.45)",
  borderRadius: 10,
  padding: "18px 20px",
  fontFamily: "JetBrains Mono, ui-monospace, monospace",
  boxShadow: "0 12px 40px rgba(0,0,0,0.45)",
};

export function ToolApprovalBanner({ request, onRespond }: Props) {
  if (!request) return null;
  return (
    <div
      style={overlay}
      className="no-drag"
      role="alertdialog"
      aria-modal="true"
      aria-labelledby="tool-approval-title"
      aria-describedby="tool-approval-body"
    >
      <div style={card}>
        <p
          id="tool-approval-title"
          style={{ margin: 0, fontSize: 11, letterSpacing: "0.12em", color: "oklch(0.78 0.16 70)" }}
        >
          APPROVAL REQUIRED · {request.tool.toUpperCase()}
        </p>
        <pre
          id="tool-approval-body"
          style={{
            margin: "12px 0 16px",
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
            fontSize: 12,
            lineHeight: 1.45,
            maxHeight: 180,
            overflow: "auto",
            color: "#d7dde3",
          }}
        >
          {request.summary}
        </pre>
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <button
            type="button"
            aria-label="Deny tool"
            onClick={() => onRespond(request.id, false)}
            style={{
              padding: "8px 14px",
              borderRadius: 6,
              border: "1px solid #3a424c",
              background: "transparent",
              color: "#c5ccd4",
              cursor: "pointer",
              fontFamily: "inherit",
              fontSize: 12,
            }}
          >
            Deny
          </button>
          <button
            type="button"
            aria-label="Allow tool"
            onClick={() => onRespond(request.id, true)}
            style={{
              padding: "8px 14px",
              borderRadius: 6,
              border: "none",
              background: "oklch(0.68 0.22 38)",
              color: "#1a1008",
              cursor: "pointer",
              fontFamily: "inherit",
              fontSize: 12,
              fontWeight: 600,
            }}
          >
            Allow
          </button>
        </div>
        {request.timeoutSec ? (
          <p style={{ margin: "10px 0 0", fontSize: 10, opacity: 0.55 }}>
            Auto-denies in {Math.round(request.timeoutSec)}s if unanswered.
          </p>
        ) : null}
      </div>
    </div>
  );
}
