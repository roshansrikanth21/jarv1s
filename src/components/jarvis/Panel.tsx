import { motion } from "framer-motion";
import { ReactNode } from "react";

interface PanelProps {
  title: string;
  status?: string;
  children: ReactNode;
  className?: string;
  dense?: boolean;
  active?: boolean;
  right?: ReactNode;
}

export function Panel({
  title,
  status,
  children,
  className = "",
  dense = false,
  active = false,
  right,
}: PanelProps) {
  return (
    <motion.div
      className={`j-panel scanlines ${active ? "active" : ""} ${className}`}
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3 }}
    >
      <div className="j-panel-header">
        <div className="flex items-center gap-2 text-muted-foreground">
          <span className="text-hud opacity-70">›</span>
          <span className="tracking-widest">{title}</span>
        </div>
        <div className="flex items-center gap-2">
          {right}
          {status && (
            <div className="flex items-center gap-1.5 text-muted-foreground">
              {active && <span className="w-1 h-1 rounded-full bg-hud animate-pulse-soft" />}
              <span className="tracking-widest text-[8px]">{status}</span>
            </div>
          )}
        </div>
      </div>
      <div className={dense ? "p-2" : "p-3"}>{children}</div>
    </motion.div>
  );
}
