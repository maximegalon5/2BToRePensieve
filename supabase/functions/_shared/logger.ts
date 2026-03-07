// Structured logger for Open Brain Edge Functions
// Outputs JSON logs with request correlation, step tracking, and timing.

export interface LogEntry {
  timestamp: string;
  level: "info" | "warn" | "error";
  requestId: string;
  function: string;
  step: string;
  durationMs?: number;
  message?: string;
  data?: Record<string, unknown>;
}

export class Logger {
  readonly requestId: string;
  readonly functionName: string;
  private timers: Map<string, number> = new Map();
  private stepLog: Array<{ step: string; durationMs: number; status: string }> = [];
  private requestStart: number;

  constructor(functionName: string, requestId?: string) {
    this.functionName = functionName;
    this.requestId = requestId || crypto.randomUUID().slice(0, 8);
    this.requestStart = performance.now();
  }

  private emit(level: LogEntry["level"], step: string, message?: string, data?: Record<string, unknown>) {
    const entry: LogEntry = {
      timestamp: new Date().toISOString(),
      level,
      requestId: this.requestId,
      function: this.functionName,
      step,
      message,
      data,
    };

    // Include duration if timer was running for this step
    const started = this.timers.get(step);
    if (started) {
      entry.durationMs = Math.round(performance.now() - started);
      this.timers.delete(step);
    }

    // Route to appropriate console method
    const json = JSON.stringify(entry);
    if (level === "error") console.error(json);
    else if (level === "warn") console.warn(json);
    else console.log(json);
  }

  startStep(step: string, message?: string, data?: Record<string, unknown>) {
    this.timers.set(step, performance.now());
    if (message || data) {
      this.emit("info", step, message, data);
    }
  }

  endStep(step: string, message?: string, data?: Record<string, unknown>) {
    const started = this.timers.get(step);
    const durationMs = started ? Math.round(performance.now() - started) : 0;
    this.stepLog.push({ step, durationMs, status: "ok" });
    this.emit("info", step, message, data);
  }

  failStep(step: string, error: unknown, data?: Record<string, unknown>) {
    const started = this.timers.get(step);
    const durationMs = started ? Math.round(performance.now() - started) : 0;
    this.stepLog.push({ step, durationMs, status: "failed" });
    const msg = error instanceof Error ? error.message : String(error);
    this.emit("error", step, msg, data);
  }

  info(step: string, message?: string, data?: Record<string, unknown>) {
    this.emit("info", step, message, data);
  }

  warn(step: string, message?: string, data?: Record<string, unknown>) {
    this.emit("warn", step, message, data);
  }

  error(step: string, error: unknown, data?: Record<string, unknown>) {
    const msg = error instanceof Error ? error.message : String(error);
    this.emit("error", step, msg, data);
  }

  summary(data?: Record<string, unknown>) {
    const totalMs = Math.round(performance.now() - this.requestStart);
    this.emit("info", "summary", `Completed in ${totalMs}ms`, {
      totalMs,
      steps: this.stepLog,
      ...data,
    });
  }
}
