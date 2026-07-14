"""
logging_config.py -- the two structured formatters (text/json) and the
idempotent configure_logging() entry point.
"""

import json
import logging

import logging_config


def _make_record(msg="hello", **extra):
    record = logging.LogRecord(
        name="test.logger", level=logging.INFO, pathname=__file__, lineno=1,
        msg=msg, args=(), exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


class TestJsonFormatter:
    def test_output_is_valid_json_with_expected_fields(self):
        record = _make_record("something happened", role="admin", attempt=2)
        line = logging_config.JsonFormatter().format(record)
        payload = json.loads(line)
        assert payload["message"] == "something happened"
        assert payload["level"] == "INFO"
        assert payload["role"] == "admin"
        assert payload["attempt"] == 2

    def test_no_reserved_logrecord_fields_leak_into_output(self):
        # pathname/lineno/funcName etc. are internal LogRecord bookkeeping,
        # not caller-supplied structured data -- they shouldn't appear as
        # top-level JSON keys cluttering every log line.
        record = _make_record()
        payload = json.loads(logging_config.JsonFormatter().format(record))
        assert "pathname" not in payload
        assert "lineno" not in payload
        assert "args" not in payload


class TestKeyValueFormatter:
    def test_extras_rendered_as_key_equals_value(self):
        record = _make_record("cache invalidated", role="donor", donor_id=5)
        line = logging_config.KeyValueFormatter().format(record)
        assert "cache invalidated" in line
        assert "role=donor" in line
        assert "donor_id=5" in line

    def test_no_extras_still_produces_clean_output(self):
        line = logging_config.KeyValueFormatter().format(_make_record("plain message"))
        assert "plain message" in line
        assert "|" not in line  # nothing to append when there are no extras


class TestConfigureLogging:
    def test_idempotent_does_not_stack_handlers(self):
        logging.getLogger()._ledger_ask_configured = False
        logging.getLogger().handlers = []
        logging_config.configure_logging()
        first_count = len(logging.getLogger().handlers)
        logging_config.configure_logging()
        assert len(logging.getLogger().handlers) == first_count
