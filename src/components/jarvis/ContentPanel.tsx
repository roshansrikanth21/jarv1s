import { X } from "lucide-react";

export type ContentPanelData = {
  title: string;
  body: string;
  ts?: string;
};

type Props = {
  data: ContentPanelData | null;
  onDismiss: () => void;
};

export function ContentPanel({ data, onDismiss }: Props) {
  if (!data) return null;

  return (
    <section className="pr-content-panel no-drag" aria-label={data.title}>
      <header className="pr-content-head">
        <div>
          <span className="pr-lab">results</span>
          <h2 className="pr-content-title">{data.title}</h2>
        </div>
        <button type="button" className="pr-content-close" onClick={onDismiss} title="Dismiss panel">
          <X size={14} />
        </button>
      </header>
      {data.ts && <span className="pr-content-ts">{data.ts}</span>}
      <div className="pr-content-body">{data.body}</div>
    </section>
  );
}
