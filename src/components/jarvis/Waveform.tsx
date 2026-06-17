import { useEffect, useState } from "react";

interface WaveformProps {
  bars?: number;
  active?: boolean;
  level?: number;
}

export function Waveform({ bars = 48, active = false, level = 0 }: WaveformProps) {
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => setTick((t) => t + 1), 80);
    return () => clearInterval(id);
  }, [active]);

  const normalizedLevel = Math.min(100, (level / 25000) * 100);

  return (
    <div className="flex items-center justify-center gap-[2px] h-10">
      {Array.from({ length: bars }).map((_, i) => {
        const variance = Math.abs(Math.sin((i + tick) * 0.4)) * (active ? 40 : 10);
        const h = active
          ? Math.max(10, normalizedLevel * (0.5 + Math.random() * 0.5) + variance)
          : 8 + Math.abs(Math.sin(i * 0.3)) * 6;

        return (
          <div
            key={i}
            className="w-[2px] bg-hud transition-[height] duration-75"
            style={{ height: `${h}%`, opacity: active ? 0.9 : 0.3 }}
          />
        );
      })}
    </div>
  );
}
