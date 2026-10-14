"""The ETL flow must fail, not succeed, when no stage flag is set."""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC / "flows"))
sys.path.insert(0, str(_SRC))

import pipeline  # noqa: E402


class StageFlagsTestCase(unittest.TestCase):
    def test_no_stage_flags_raises(self):
        with patch.object(pipeline, "_get_or_create_arango_password") as password:
            with self.assertRaises(ValueError) as ctx:
                pipeline.nlm_ckn_etl.fn()
        self.assertIn("No stage flags set", str(ctx.exception))
        password.assert_not_called()  # fails before touching ArangoDB or S3


if __name__ == "__main__":
    unittest.main()
