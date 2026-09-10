"""Tests for the ArangoDB URL scheme resolved in flows/_common.py.

Covers:
- ARANGO_DB_SCHEME defaults: https for a non-loopback host, http for loopback
- an explicit ARANGO_DB_SCHEME wins, and an invalid one fails at import
- arango_db_url and _arango_env carry the same scheme
"""

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_FLOWS = Path(__file__).resolve().parents[1] / "src" / "flows"
sys.path.insert(0, str(_FLOWS))


def _load_common(**env):
    """Import a fresh copy of _common with ``env`` as its ArangoDB settings.

    The module constants are resolved at import, so each case needs its own
    copy; the shared ``_common`` other tests import is left untouched.
    """
    cleared = {"ARANGO_DB_HOST": None, "ARANGO_DB_PORT": None, "ARANGO_DB_SCHEME": None}
    cleared.update(env)
    spec = importlib.util.spec_from_file_location("_common_fresh", _FLOWS / "_common.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ):
        for name, value in cleared.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        spec.loader.exec_module(module)
    return module


class ArangoDbSchemeTestCase(unittest.TestCase):
    def test_non_loopback_host_defaults_to_https(self):
        """Root credentials to a remote host are encrypted unless opted out."""
        common = _load_common(ARANGO_DB_HOST="10.0.1.5")
        self.assertFalse(common.ARANGO_DB_IS_LOCAL)
        self.assertEqual(common.ARANGO_DB_SCHEME, "https")
        self.assertEqual(common.arango_db_url(), "https://10.0.1.5:8529")
        self.assertEqual(common._arango_env("secret")["ARANGO_DB_SCHEME"], "https")

    def test_loopback_hosts_default_to_http(self):
        """The local Docker container serves plain HTTP."""
        for host in ("localhost", "127.0.0.1", "::1"):
            with self.subTest(host=host):
                self.assertEqual(_load_common(ARANGO_DB_HOST=host).ARANGO_DB_SCHEME, "http")
        self.assertEqual(_load_common().arango_db_url(), "http://localhost:8529")

    def test_explicit_scheme_overrides_default(self):
        """ARANGO_DB_SCHEME=http is the deliberate switch for a plain-HTTP remote."""
        common = _load_common(ARANGO_DB_HOST="10.0.1.5", ARANGO_DB_SCHEME="HTTP")
        self.assertEqual(common.arango_db_url(), "http://10.0.1.5:8529")
        common = _load_common(ARANGO_DB_HOST="localhost", ARANGO_DB_SCHEME="https")
        self.assertEqual(common.arango_db_url(), "https://localhost:8529")

    def test_invalid_scheme_fails_at_import(self):
        with self.assertRaisesRegex(ValueError, "ARANGO_DB_SCHEME"):
            _load_common(ARANGO_DB_HOST="10.0.1.5", ARANGO_DB_SCHEME="tcp")

    def test_url_reads_live_port_and_brackets_ipv6(self):
        """The port is read at call time; an IPv6 literal is bracketed."""
        common = _load_common(ARANGO_DB_HOST="::1")
        common.ARANGO_DB_PORT = 54321
        self.assertEqual(common.arango_db_url(), "http://[::1]:54321")
        self.assertEqual(common.arango_db_url(4000), "http://[::1]:4000")


if __name__ == "__main__":
    unittest.main()
