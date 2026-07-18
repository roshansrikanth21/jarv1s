import { Component, type ReactNode } from "react";

type Props = {
  children: ReactNode;
  /** Shown when a child tree throws (e.g. WebGL / Three.js orb). */
  fallback?: ReactNode;
};

type State = { failed: boolean };

/**
 * Deck-local boundary so a crashed orb/widget can't blank the whole preset.
 * Router-level errorComponent still covers route load failures; this covers
 * runtime throws inside an otherwise healthy deck.
 */
export class DeckErrorBoundary extends Component<Props, State> {
  state: State = { failed: false };

  static getDerivedStateFromError(): State {
    return { failed: true };
  }

  render() {
    if (this.state.failed) {
      return (
        this.props.fallback ?? <div className="pr-orb-canvas pr-orb-canvas--loading" aria-hidden />
      );
    }
    return this.props.children;
  }
}
