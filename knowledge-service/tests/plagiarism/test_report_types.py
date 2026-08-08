from __future__ import annotations

from datetime import datetime

from kbsvc.api.plagiarism_schemas import ReportOut
from kbsvc.plagiarism.types import CheckReport, CheckStatus


def test_report_out_preserves_matcher_snapshot():
    report = CheckReport(
        check_id="check",
        status=CheckStatus.COMPLETED,
        snapshot_at=datetime(2026, 1, 1),
        algorithm_config_hash="hash",
        query_chars=24,
        matched_chars=24,
        checked_chunks=1,
        total_chunks=1,
        matcher_version="seed-extend-v2",
        matcher_config={"profile": "zh", "min_seed_len": 12, "min_passage_len": 20},
    )

    output = ReportOut.of(report)
    assert output.matcher_version == "seed-extend-v2"
    assert output.matcher_config["min_seed_len"] == 12


def test_legacy_report_defaults_matcher_fields_to_empty_values():
    report = CheckReport(
        check_id="legacy",
        status=CheckStatus.COMPLETED,
        snapshot_at=datetime(2026, 1, 1),
        algorithm_config_hash="hash",
        query_chars=50,
        matched_chars=0,
        checked_chunks=1,
        total_chunks=1,
    )
    output = ReportOut.of(report)
    assert output.matcher_version == ""
    assert output.matcher_config == {}
