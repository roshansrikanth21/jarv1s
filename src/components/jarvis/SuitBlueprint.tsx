import { useMemo } from "react";

interface SuitBlueprintProps {
  active?: boolean;
}

export function SuitBlueprint({ active = false }: SuitBlueprintProps) {
  const points = useMemo(
    () => [
      { x: 50, y: 15, label: "Neural Link", status: "OK" },
      { x: 50, y: 45, label: "Arc Core", status: "STABLE", primary: true },
      { x: 25, y: 35, label: "Left Servo", status: "NOMINAL" },
      { x: 75, y: 35, label: "Right Servo", status: "NOMINAL" },
      { x: 30, y: 85, label: "Repulsor L", status: "READY" },
      { x: 70, y: 85, label: "Repulsor R", status: "READY" },
    ],
    [],
  );

  return (
    <div className="relative w-full h-[500px] flex items-center justify-center overflow-hidden">
      <div
        className="absolute inset-0 opacity-10 pointer-events-none"
        style={{
          backgroundImage: "radial-gradient(var(--hud) 0.5px, transparent 0.5px)",
          backgroundSize: "16px 16px",
        }}
      />

      <svg className="w-full h-full max-w-md opacity-80" viewBox="0 0 100 150">
        <defs>
          <filter id="glow">
            <feGaussianBlur stdDeviation="1" result="blur" />
            <feComposite in="SourceGraphic" in2="blur" operator="over" />
          </filter>
          <linearGradient id="suitGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--hud)" stopOpacity="0.2" />
            <stop offset="100%" stopColor="var(--hud)" stopOpacity="0.05" />
          </linearGradient>
        </defs>

        <g
          filter="url(#glow)"
          className="transition-all duration-1000"
          style={{ opacity: active ? 1 : 0.3 }}
        >
          <path
            d="M 45 10 Q 50 5 55 10 L 58 20 L 42 20 Z"
            fill="url(#suitGrad)"
            stroke="var(--hud)"
            strokeWidth="0.5"
          />
          <path
            d="M 42 22 L 58 22 L 65 40 L 60 70 L 40 70 L 35 40 Z"
            fill="url(#suitGrad)"
            stroke="var(--hud)"
            strokeWidth="0.5"
          />
          <path
            d="M 34 25 L 20 50 L 25 60 L 33 40 Z"
            fill="url(#suitGrad)"
            stroke="var(--hud)"
            strokeWidth="0.5"
          />
          <path
            d="M 66 25 L 80 50 L 75 60 L 67 40 Z"
            fill="url(#suitGrad)"
            stroke="var(--hud)"
            strokeWidth="0.5"
          />
          <path
            d="M 40 72 L 42 110 L 30 140 L 40 140 L 48 110 Z"
            fill="url(#suitGrad)"
            stroke="var(--hud)"
            strokeWidth="0.5"
          />
          <path
            d="M 60 72 L 58 110 L 70 140 L 60 140 L 52 110 Z"
            fill="url(#suitGrad)"
            stroke="var(--hud)"
            strokeWidth="0.5"
          />
          <line
            x1="50"
            y1="22"
            x2="50"
            y2="70"
            stroke="var(--hud)"
            strokeWidth="0.2"
            strokeDasharray="2 2"
            opacity="0.5"
          />
          <line
            x1="40"
            y1="45"
            x2="60"
            y2="45"
            stroke="var(--hud)"
            strokeWidth="0.2"
            strokeDasharray="1 1"
            opacity="0.3"
          />
        </g>

        {active &&
          points.map((p, i) => (
            <g
              key={i}
              className="animate-in fade-in duration-700"
              style={{ animationDelay: `${i * 150}ms` }}
            >
              <circle
                cx={p.x}
                cy={p.y}
                r={p.primary ? 2.5 : 1.2}
                fill={p.primary ? "var(--hud)" : "none"}
                stroke="var(--hud)"
                strokeWidth="0.5"
                className={p.primary ? "animate-pulse" : ""}
              />
              {p.primary && (
                <circle
                  cx={p.x}
                  cy={p.y}
                  r={4}
                  fill="none"
                  stroke="var(--hud)"
                  strokeWidth="0.2"
                  className="animate-ping"
                />
              )}
              <polyline
                points={`${p.x},${p.y} ${p.x + (p.x > 50 ? 10 : -10)},${p.y - 5} ${p.x + (p.x > 50 ? 25 : -25)},${p.y - 5}`}
                fill="none"
                stroke="var(--hud)"
                strokeWidth="0.2"
                opacity="0.6"
              />
              <text
                x={p.x + (p.x > 50 ? 26 : -26)}
                y={p.y - 4}
                textAnchor={p.x > 50 ? "start" : "end"}
                className="text-[3px] fill-hud uppercase tracking-[0.2em] font-bold"
              >
                {p.label}
              </text>
              <text
                x={p.x + (p.x > 50 ? 26 : -26)}
                y={p.y - 1}
                textAnchor={p.x > 50 ? "start" : "end"}
                className="text-[2.5px] fill-muted-foreground uppercase tracking-[0.1em]"
              >
                STATUS: {p.status}
              </text>
            </g>
          ))}
      </svg>

      <div className="absolute left-4 top-1/4 bottom-1/4 w-px bg-hud/20 overflow-hidden">
        <div className="h-full w-full bg-gradient-to-b from-transparent via-hud to-transparent animate-scan opacity-40" />
      </div>
      <div className="absolute right-4 top-1/4 bottom-1/4 w-px bg-hud/20 overflow-hidden">
        <div
          className="h-full w-full bg-gradient-to-b from-transparent via-hud to-transparent animate-scan opacity-40"
          style={{ animationDelay: "3s" }}
        />
      </div>
    </div>
  );
}
