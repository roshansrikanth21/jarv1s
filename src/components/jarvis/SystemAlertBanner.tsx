type Props = {
  text: string;
  severity?: string;
  onDismiss: () => void;
};

export function SystemAlertBanner({ text, severity = "warn", onDismiss }: Props) {
  if (!text) return null;
  return (
    <div className={`pr-sys-alert pr-sys-alert--${severity} no-drag`} role="alert">
      <span className="pr-lab">system</span>
      <span className="pr-sys-alert-text">{text}</span>
      <button type="button" className="pr-sys-alert-x" onClick={onDismiss} aria-label="Dismiss alert">
        ×
      </button>
    </div>
  );
}
