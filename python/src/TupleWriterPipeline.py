"""Orchestration module that runs all schema-based tuple writers.

Executes each data-source-specific tuple writer in sequence:
1. NSForest results
2. Author-to-CL mapping
3. CELLxGENE metadata
4. Open Targets
5. NCBI Gene
6. UniProt
"""

import argparse
from time import monotonic

import structlog

from LoaderUtilities import get_current_run, set_current_run
from logging_setup import configure_logging

import NSForestTupleWriter
import MappingTupleWriter
import CellxGeneTupleWriter
import OpenTargetsTupleWriter
import GeneTupleWriter
import UniProtTupleWriter

_events = structlog.get_logger(__name__)

# Writers in the order they run, each with its ``source`` value.
WRITERS = [
    ("nsforest", NSForestTupleWriter),
    ("mapping", MappingTupleWriter),
    ("cellxgene", CellxGeneTupleWriter),
    ("opentargets", OpenTargetsTupleWriter),
    ("gene", GeneTupleWriter),
    ("uniprot", UniProtTupleWriter),
]


def _ms(started: float) -> int:
    """Return the milliseconds elapsed since a ``time.monotonic()`` reading."""
    return round((monotonic() - started) * 1000)


def main():
    """Run all tuple writers.

    Logs the run as ``run_started`` and ``run_finished``, or ``run_failed``,
    and ``tuple_writers_finished`` with the tuples each writer wrote.
    """
    configure_logging("etl-pipeline")
    started = monotonic()
    try:
        tuples_out = _run_writers()
    except Exception as exc:
        _events.error(
            "run_failed",
            script="TupleWriterPipeline",
            error_type=type(exc).__name__,
            error=str(exc),
            duration_ms=_ms(started),
        )
        raise
    _events.info(
        "tuple_writers_finished", duration_ms=_ms(started), tuples_out=tuples_out
    )
    _events.info(
        "run_finished",
        script="TupleWriterPipeline",
        outcome="ok",
        duration_ms=_ms(started),
    )


def _run_writers() -> dict[str, int]:
    """Parse the command line and run each tuple writer in turn.

    Returns
    -------
    dict[str, int]
        The number of tuples each writer wrote, by ``source``
    """
    parser = argparse.ArgumentParser(description="Run all tuple writers")
    parser.add_argument(
        "--run-name",
        default=None,
        help="run name (selects data/results-<name>/; defaults to $CKN_RUN or 'full')",
    )
    args = parser.parse_args()
    set_current_run(args.run_name)
    _events.info(
        "run_started",
        script="TupleWriterPipeline",
        run_name=get_current_run().run_name,
        writers=[name for name, _ in WRITERS],
    )

    tuples_out = {}
    for name, writer in WRITERS:
        tuples_out[name] = writer.main() or 0
    return tuples_out


if __name__ == "__main__":
    main()
