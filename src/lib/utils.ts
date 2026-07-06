import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/** Fire a native OS notification (Windows Action Center toast under Electron)
 *  — used for system_alert broadcasts instead of an in-page banner. Silently
 *  a no-op if unsupported/blocked; never throws into a message handler. */
export function notifyNative(title: string, body: string) {
  try {
    if (typeof Notification === "undefined") return;
    if (Notification.permission === "granted") {
      new Notification(title, { body });
    } else if (Notification.permission !== "denied") {
      void Notification.requestPermission().then((p) => {
        if (p === "granted") new Notification(title, { body });
      });
    }
  } catch {
    /* notifications unsupported/blocked — safe to ignore */
  }
}
