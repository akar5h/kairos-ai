import { NodeSDK } from '@opentelemetry/sdk-node';
import { OTLPTraceExporter } from '@opentelemetry/exporter-trace-otlp-http';
import { W3CTraceContextPropagator, CompositePropagator } from '@opentelemetry/core';
import { HttpInstrumentation } from '@opentelemetry/instrumentation-http';
import { ExpressInstrumentation } from '@opentelemetry/instrumentation-express';
import { PgInstrumentation } from '@opentelemetry/instrumentation-pg';

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
