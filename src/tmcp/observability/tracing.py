"""OpenTelemetry traces (MON-03, MON-04). Without TMCP_OTEL_ENDPOINT the server makes no traces."""

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

from tmcp.config import Settings

tracer = trace.get_tracer("tmcp")


def configure_tracing(settings: Settings, version: str) -> TracerProvider | None:
    if not settings.otel_endpoint:
        return None
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

    resource = Resource.create(
        {"service.name": settings.service_name, "service.version": version, "deployment.environment": settings.env}
    )
    # MON-04: head sampling here. The OTel collector keeps all traces with errors (tail sampling).
    provider = TracerProvider(resource=resource, sampler=ParentBased(TraceIdRatioBased(settings.trace_sample_rate)))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_endpoint, insecure=True)))
    trace.set_tracer_provider(provider)
    return provider
