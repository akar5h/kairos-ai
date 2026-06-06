import { NodeSDK } from '@opentelemetry/sdk-node';
import { OTLPTraceExporter } from '@opentelemetry/exporter-trace-otlp-http';
import { W3CTraceContextPropagator, CompositePropagator } from '@opentelemetry/core';
import { HttpInstrumentation } from '@opentelemetry/instrumentation-http';
import { ExpressInstrumentation } from '@opentelemetry/instrumentation-express';
import { PgInstrumentation } from '@opentelemetry/instrumentation-pg';
import * as api from '@opentelemetry/api';
import child_process from 'child_process';

const OTLP_ENDPOINT = process.env.OTEL_EXPORTER_OTLP_ENDPOINT ?? 'http://localhost:4318';

try {
  const sdk = new NodeSDK({
    traceExporter: new OTLPTraceExporter({
      url: `${OTLP_ENDPOINT}/v1/traces`,
    }),
    propagator: new CompositePropagator({
      propagators: [new W3CTraceContextPropagator()],
    }),
    instrumentations: [
      new HttpInstrumentation(),
      new ExpressInstrumentation(),
      new PgInstrumentation(),
    ],
  });

  sdk.start();

  process.on('SIGTERM', () => {
    sdk.shutdown().finally(() => process.exit(0));
  });
} catch (err) {
  process.stderr.write(`[otel-preload] OTel bootstrap failed — server will still start: ${err}\n`);
}

// Phase 2: propagate W3C trace context into child_process.execFile subprocesses
const _originalExecFile = child_process.execFile;
child_process.execFile = function patchedExecFile(file, ...rest) {
  const span = api.trace.getActiveSpan();
  if (span && span.isRecording()) {
    // rest = [?args[], ?options{}, ?callback()]
    // Find existing options object (non-array, non-function object)
    let optsIdx = rest.findIndex(a => a !== null && typeof a === 'object' && !Array.isArray(a));
    if (optsIdx === -1) {
      // Insert an options object before the callback (or at end if no callback)
      const cbIdx = rest.findIndex(a => typeof a === 'function');
      optsIdx = cbIdx !== -1 ? cbIdx : rest.length;
      rest.splice(optsIdx, 0, {});
    }

    const opts = rest[optsIdx];
    if (!opts.env) opts.env = { ...process.env };

    if (!opts.env.TRACEPARENT) {
      const ctx = span.spanContext();
      const flags = ctx.traceFlags.toString(16).padStart(2, '0');
      opts.env.TRACEPARENT = `00-${ctx.traceId}-${ctx.spanId}-${flags}`;
      const tracestate = ctx.traceState?.serialize() ?? '';
      if (tracestate) opts.env.TRACESTATE = tracestate;
    }
  }

  return _originalExecFile(file, ...rest);
};
