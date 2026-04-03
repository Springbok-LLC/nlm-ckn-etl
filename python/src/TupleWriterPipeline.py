"""Orchestration module that runs all schema-based tuple writers.

Executes each data-source-specific tuple writer in sequence:
1. NSForest results
2. Author-to-CL mapping
3. CELLxGENE metadata
4. Open Targets
5. NCBI Gene
6. UniProt
7. HuBMAP
"""

import NSForestTupleWriter
import MappingTupleWriter
import CellxGeneTupleWriter
import OpenTargetsTupleWriter
import GeneTupleWriter
import UniProtTupleWriter
import HuBMAPTupleWriter


def main():
    """Run all tuple writers."""
    print("=" * 70)
    print("Running NSForest tuple writer")
    print("=" * 70)
    NSForestTupleWriter.main()

    print()
    print("=" * 70)
    print("Running Mapping tuple writer")
    print("=" * 70)
    MappingTupleWriter.main()

    print()
    print("=" * 70)
    print("Running CELLxGENE tuple writer")
    print("=" * 70)
    CellxGeneTupleWriter.main()

    print()
    print("=" * 70)
    print("Running Open Targets tuple writer")
    print("=" * 70)
    OpenTargetsTupleWriter.main()

    print()
    print("=" * 70)
    print("Running Gene tuple writer")
    print("=" * 70)
    GeneTupleWriter.main()

    print()
    print("=" * 70)
    print("Running UniProt tuple writer")
    print("=" * 70)
    UniProtTupleWriter.main()

    print()
    print("=" * 70)
    print("Running HuBMAP tuple writer")
    print("=" * 70)
    HuBMAPTupleWriter.main()

    print()
    print("=" * 70)
    print("All tuple writers complete")
    print("=" * 70)


if __name__ == "__main__":
    main()
