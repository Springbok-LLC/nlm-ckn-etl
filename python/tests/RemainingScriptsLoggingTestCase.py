"""E_Utilities, UniProtIdMapper and ProductionDataValidator log events."""

import io
import json
import logging
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
import structlog
from structlog.testing import capture_logs

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import E_Utilities  # noqa: E402
import ProductionDataValidator as validator  # noqa: E402
import UniProtIdMapper  # noqa: E402
import logging_setup  # noqa: E402


def _one(logs, name):
    (event,) = [e for e in logs if e["event"] == name]
    return event


def _response(status_code=200, text="", json_data=None, url="https://x"):
    response = MagicMock(status_code=status_code, text=text, url=url)
    response.json.return_value = json_data
    return response


class EUtilitiesLoggingTestCase(unittest.TestCase):
    def _call(self, fn, response, *args):
        with (
            patch.object(E_Utilities.requests, "get", return_value=response),
            patch.object(E_Utilities, "sleep"),
            capture_logs() as logs,
        ):
            result = fn(*args)
        return result, logs

    def test_pubmed_failure_is_a_warning(self):
        result, logs = self._call(
            E_Utilities.get_data_for_pmid, _response(500), "123"
        )
        self.assertEqual(result, {})
        event = _one(logs, "pubmed_fetch_failed")
        self.assertEqual(
            (event["pmid"], event["reason"], event["status_code"], event["log_level"]),
            ("123", "http_error", 500, "warning"),
        )

    def test_gene_search_with_no_hit(self):
        response = _response(json_data={"esearchresult": {"idlist": []}})
        result, logs = self._call(E_Utilities.find_gene_id_for_gene_name, response, "NOPE")
        self.assertIsNone(result)
        self.assertEqual(_one(logs, "gene_search_empty")["name"], "NOPE")

    def test_gene_search_hit_logs_nothing(self):
        response = _response(json_data={"esearchresult": {"idlist": ["7157"]}})
        result, logs = self._call(E_Utilities.find_gene_id_for_gene_name, response, "TP53")
        self.assertEqual((result, logs), ("7157", []))

    def test_gene_search_failure(self):
        _, logs = self._call(E_Utilities.find_gene_id_for_gene_name, _response(429), "TP53")
        event = _one(logs, "gene_search_failed")
        self.assertEqual((event["name"], event["status_code"]), ("TP53", 429))

    def test_gene_fetch_failure_names_the_gene(self):
        result, logs = self._call(E_Utilities.fetch_xml_for_gene_id, _response(503), "7157")
        self.assertIsNone(result)
        event = _one(logs, "gene_fetch_failed")
        self.assertEqual((event["gene_id"], event["status_code"]), ("7157", 503))

    def test_successful_fetch_logs_nothing(self):
        result, logs = self._call(
            E_Utilities.fetch_xml_for_gene_id, _response(text="<xml/>"), "7157"
        )
        self.assertEqual((result, logs), ("<xml/>", []))


class UniProtLoggingTestCase(unittest.TestCase):
    def test_http_error_is_logged_and_reraised_even_for_a_non_json_body(self):
        response = requests.Response()
        response.status_code = 503
        response.url = "https://rest.uniprot.org/idmapping/run"
        response._content = b"<html>Service Unavailable</html>"
        with capture_logs() as logs:
            with self.assertRaises(requests.HTTPError):
                UniProtIdMapper.check_response(response)
        event = _one(logs, "uniprot_http_error")
        self.assertEqual(
            (event["status_code"], event["url"], event["log_level"]),
            (503, "https://rest.uniprot.org/idmapping/run", "error"),
        )
        self.assertIn("Service Unavailable", event["body"])

    def test_a_long_body_is_cut(self):
        response = requests.Response()
        response.status_code = 500
        response._content = b"x" * 5000
        with capture_logs() as logs:
            with self.assertRaises(requests.HTTPError):
                UniProtIdMapper.check_response(response)
        self.assertEqual(len(_one(logs, "uniprot_http_error")["body"]), 500)

    def test_a_good_response_logs_nothing(self):
        response = requests.Response()
        response.status_code = 200
        with capture_logs() as logs:
            UniProtIdMapper.check_response(response)
        self.assertEqual(logs, [])

    def test_polling_and_progress_are_debug(self):
        pending = MagicMock()
        pending.json.return_value = {"jobStatus": "RUNNING"}
        done = MagicMock()
        done.json.return_value = {"results": [1], "failedIds": []}
        with (
            patch.object(UniProtIdMapper.session, "get", side_effect=[pending, done]),
            patch.object(UniProtIdMapper, "check_response"),
            patch.object(UniProtIdMapper.time, "sleep"),
            capture_logs() as logs,
        ):
            self.assertTrue(UniProtIdMapper.check_id_mapping_results_ready("job1"))
            UniProtIdMapper.print_progress_batches(1, 500, 750)
        poll = _one(logs, "idmapping_job_pending")
        self.assertEqual(
            (poll["job_id"], poll["job_status"], poll["wait_s"], poll["log_level"]),
            ("job1", "RUNNING", UniProtIdMapper.POLLING_INTERVAL, "debug"),
        )
        progress = _one(logs, "idmapping_batch_fetched")
        self.assertEqual((progress["n_fetched"], progress["total"]), (750, 750))


class ValidatorLoggingTestCase(unittest.TestCase):
    def _run(self, argv, files=()):
        with tempfile.TemporaryDirectory() as d:
            for name in files:
                (Path(d) / name).write_text("x")
            out = io.StringIO()
            with (
                patch.object(validator, "configure_logging") as configure,
                patch.dict(os.environ, {}, clear=False),
                redirect_stdout(out),
                capture_logs() as logs,
            ):
                os.environ.pop("LOG_FORMAT", None)
                code = validator.main(["--data-dir", d, *argv])
                fmt = os.environ.get("LOG_FORMAT")
        return code, logs, out.getvalue(), configure, fmt

    def test_findings_are_logged_by_severity_then_a_summary(self):
        code, logs, out, _, _ = self._run([])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        findings = [e for e in logs if e["event"] == "validation_finding"]
        self.assertEqual(
            [(e["check"], e["log_level"]) for e in findings],
            [("no-results", "error"), ("manifest-absent", "warning")],
        )
        self.assertIn("detail", findings[0])
        done = _one(logs, "validation_finished")
        self.assertEqual(
            (done["n_errors"], done["n_warnings"], done["exit_code"]), (1, 1, 1)
        )
        self.assertIsInstance(done["duration_ms"], int)

    def test_logs_go_to_stderr_in_readable_form_by_default(self):
        _, _, _, configure, fmt = self._run([])
        self.assertIs(configure.call_args.kwargs["stream"], sys.stderr)
        self.assertEqual(configure.call_args.args, ("etl-pipeline",))
        self.assertEqual(fmt, "console")

    def test_json_output_stays_on_stdout_alone(self):
        code, logs, out, _, _ = self._run(["--json"])
        self.assertEqual(code, 1)
        report = json.loads(out)
        self.assertEqual((report["errors"], report["warnings"]), (1, 1))
        self.assertEqual(_one(logs, "validation_finished")["exit_code"], 1)


class ConsoleFormatTestCase(unittest.TestCase):
    def test_console_lines_show_the_event_name(self):
        stream = io.StringIO()
        with patch.dict(os.environ, {"LOG_FORMAT": "console"}):
            logging_setup.configure_logging("etl-pipeline", stream=stream)
        try:
            structlog.get_logger("m").info("run_started", script="x")
        finally:
            logging.getLogger().handlers = []
            structlog.reset_defaults()
        # The name is the line's headline, not a trailing ``message=`` field.
        self.assertIn("] run_started", stream.getvalue())
        self.assertNotIn("message=", stream.getvalue())
        self.assertIn("script=x", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
