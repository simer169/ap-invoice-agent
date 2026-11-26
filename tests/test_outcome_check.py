from evals.outcome_check import evaluate, expected_statuses


def test_expected_status_mapping() -> None:
    assert expected_statuses("READY") == {"READY"}
    assert expected_statuses("ANOMALY_PRICE") == {"READY"}
    assert expected_statuses("PARK_THEN_READY") == {"WAITING"}
    assert expected_statuses("STAYS_WAITING") == {"WAITING"}
    assert expected_statuses("EXCEPTION_NO_PO") == {"EXCEPTION"}
    assert expected_statuses("REJECT_DUPLICATE") == {"REJECTED", "NOT_RUN"}
    assert expected_statuses("IGNORE") is None


def test_evaluate_counts_and_mismatches() -> None:
    entries = [
        {"id": "A", "scenario": "s", "expected_outcome": "READY", "pdf_sha256": "a"},
        {"id": "B", "scenario": "s", "expected_outcome": "EXCEPTION_NO_PO", "pdf_sha256": "b"},
        {"id": "C", "scenario": "s", "expected_outcome": "REJECT_DUPLICATE", "pdf_sha256": "c"},
        {"id": "D", "scenario": "s", "expected_outcome": "IGNORE", "pdf_sha256": "d"},
    ]
    actual = {"a": ("READY", ""), "b": ("WAITING", "not received"), "c": ("NOT_RUN", "")}
    confusion, bad, n = evaluate(entries, lambda h: actual[h])
    assert n == 3
    assert confusion[("EXCEPTION_NO_PO", "WAITING")] == 1
    assert bad == [("B", "s", "EXCEPTION_NO_PO", "WAITING", "not received")]


def test_anomaly_outcome_needs_its_flag_code() -> None:
    entries = [
        {"id": "P", "scenario": "s", "expected_outcome": "ANOMALY_PRICE", "pdf_sha256": "p"},
        {"id": "Q", "scenario": "s", "expected_outcome": "ANOMALY_PRICE", "pdf_sha256": "q"},
        {"id": "A", "scenario": "s", "expected_outcome": "ANOMALY_AMOUNT", "pdf_sha256": "a"},
    ]
    actual = {
        "p": ("READY", "Anomaly PRICE_OVER_TOLERANCE (warning): x"),
        "q": ("READY", ""),
        "a": ("READY", "Anomaly AMOUNT_OUTLIER (warning): y"),
    }
    _, bad, n = evaluate(entries, lambda h: actual[h])
    assert n == 3 and [b[0] for b in bad] == ["Q"] and bad[0][3] == "NO_FLAG"
