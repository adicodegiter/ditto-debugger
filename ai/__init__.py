from .report import generate_findings

__all__ = [
    "generate_findings",
    "build_findings_for_run",
    "build_report_markdown",
    "trace_check_markdown",
]


def __getattr__(name):
    if name in {"build_findings_for_run", "build_report_markdown", "trace_check_markdown"}:
        from . import findings

        return getattr(findings, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
