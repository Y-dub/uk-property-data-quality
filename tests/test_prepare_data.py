import subprocess
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import prepare_data as pdq  # noqa: E402


def row(tid, price, date, postcode, ptype="T", paon="12", street="HIGH STREET", county="CITY OF NOTTINGHAM",
        cat="A"):
    # Same shape as the real files: quoted fields, braces round the ID, time on the date, no header.
    fields = [f"{{{tid}}}", str(price), f"{date} 00:00", postcode, ptype, "N", "F", paon, "", street, "",
              "NOTTINGHAM", "CITY OF NOTTINGHAM", county, cat, "A"]
    return ",".join(f'"{f}"' for f in fields)


ROWS = [
    row("CLEAN-1", 185000, "2025-01-15", "NG7 2RD"),
    row("CLEAN-2", 240000, "2025-02-03", "DE1 1AA", county="CITY OF DERBY"),
    row("R1-MISSING-PC", 150000, "2025-01-20", ""),
    row("R2-BAD-PC", 150000, "2025-01-21", "NG72RD"),
    row("R3-NO-PAON", 150000, "2025-01-22", "NG1 1AA", paon=""),
    row("R4-NO-STREET", 150000, "2025-01-23", "NG1 1AB", street=""),
    row("R5-LOW", 500, "2025-01-24", "NG1 1AD"),
    row("R5-HIGH", 9_000_000, "2025-01-25", "NG1 1AE"),
    row("R6-DUP-A", 210000, "2025-03-01", "NG5 1AA", paon="7"),
    row("R6-DUP-B", 210000, "2025-03-01", "NG5 1AA", paon="7"),
    row("R7-OTHER-A", 300000, "2025-03-02", "NG2 1AA", ptype="O", cat="A"),
    row("OTHER-B-OK", 300000, "2025-03-02", "NG2 1AB", ptype="O", cat="B"),
    row("OUT-OF-AREA", 999, "2025-01-01", "", county="GREATER LONDON"),
]
EXPECTED = {
    "R1": {"R1-MISSING-PC"},
    "R2": {"R2-BAD-PC"},
    "R3": {"R3-NO-PAON"},
    "R4": {"R4-NO-STREET"},
    "R5": {"R5-LOW", "R5-HIGH"},
    "R6": {"R6-DUP-A", "R6-DUP-B"},
    "R7": {"R7-OTHER-A"},
}


def sample(tmp_path) -> Path:
    path = tmp_path / "pp-sample.csv"
    path.write_text("\n".join(ROWS) + "\n")
    return path


def test_each_rule_flags_exactly_the_planted_rows(tmp_path):
    df = pdq.load([sample(tmp_path)], pdq.DEFAULT_COUNTIES)
    failures = pdq.run_checks(df)
    for rule, ids in EXPECTED.items():
        got = set(failures.loc[failures["rule_id"] == rule, "transaction_id"])
        assert got == ids, rule


def test_county_filter_and_id_cleaning(tmp_path):
    df = pdq.load([sample(tmp_path)], pdq.DEFAULT_COUNTIES)
    assert "OUT-OF-AREA" not in set(df["transaction_id"])
    assert len(df) == len(ROWS) - 1
    assert not df["transaction_id"].str.contains("[{}]").any()


def test_tables_and_issue_counts(tmp_path):
    df = pdq.load([sample(tmp_path)], pdq.DEFAULT_COUNTIES)
    tx, failures, rules = pdq.build_tables(df)
    counts = tx.set_index("transaction_id")["dq_issue_count"]
    assert counts["CLEAN-1"] == 0 and counts["OTHER-B-OK"] == 0
    assert counts["R1-MISSING-PC"] == 1
    assert set(rules["rule_id"]) == set(EXPECTED)
    assert set(failures["transaction_id"]) <= set(tx["transaction_id"])
    assert tx["property_type"].isin(pdq.PROPERTY_TYPE.values()).all()


def test_cli_end_to_end(tmp_path):
    out = tmp_path / "out"
    res = subprocess.run([sys.executable, str(ROOT / "prepare_data.py"), str(sample(tmp_path)), "--out", str(out),
                          "--tableau-out", str(out)],
                         capture_output=True, text=True, check=True)
    assert "12 sales checked" in res.stdout
    for name in ("transactions.csv", "dq_failures.csv", "dq_rules.csv"):
        assert (out / name).exists()
    for name in ("dq_monthly.csv", "dq_failure_detail.csv"):
        assert (out / name).exists()
    assert len(pd.read_csv(out / "dq_failures.csv")) == sum(len(v) for v in EXPECTED.values())


def test_tableau_monthly_table_reconciles(tmp_path):
    df = pdq.load([sample(tmp_path)], pdq.DEFAULT_COUNTIES)
    tx, failures, rules = pdq.build_tables(df)
    monthly, detail = pdq.build_tableau_tables(tx, failures, rules)
    # every month x county x type cell has all 7 rules
    assert (monthly.groupby(["month", "county", "property_type"]).size() == len(rules)).all()
    # records checked per rule = all records, failures = all failures
    for rule_id, g in monthly.groupby("rule_id"):
        assert g["records_checked"].sum() == len(tx)
        assert g["failures"].sum() == (failures["rule_id"] == rule_id).sum()
    assert len(detail) == len(failures)
    assert detail["rule_name"].notna().all()
