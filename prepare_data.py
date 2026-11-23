"""Run data-quality checks on HM Land Registry Price Paid Data and write tables for Power BI.

Usage:
    python prepare_data.py pp-2024.csv pp-2025.csv
    python prepare_data.py pp-2025.csv --counties NOTTINGHAM DERBY --out powerbi_data

Input: yearly Price Paid Data CSV files (no header row) from
https://www.gov.uk/government/statistical-data-sets/price-paid-data-downloads
Licence: Open Government Licence v3.0. Contains HM Land Registry data © Crown copyright.

Output (star schema):
    transactions.csv   one row per sale (fact), with dq_issue_count
    dq_failures.csv    one row per (sale, rule) that failed
    dq_rules.csv       the rule catalogue: dimension, description, tolerance

Tableau outputs (--tableau-out, default tableau_data/):
    dq_monthly.csv         month x county x property type x rule: records checked, failures, tolerance
    dq_failure_detail.csv  one row per failed check with address and price (the correction work list)
    dq_rules.csv           as above
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

# Column order from https://www.gov.uk/guidance/about-the-price-paid-data (files have no header row).
COLUMNS = [
    "transaction_id", "price", "transfer_date", "postcode", "property_type", "old_new", "duration",
    "paon", "saon", "street", "locality", "town", "district", "county", "ppd_category", "record_status",
]

PROPERTY_TYPE = {"D": "Detached", "S": "Semi-detached", "T": "Terraced", "F": "Flat/maisonette", "O": "Other"}
OLD_NEW = {"Y": "New build", "N": "Established"}
DURATION = {"F": "Freehold", "L": "Leasehold"}
CATEGORY = {"A": "Standard", "B": "Additional"}

# Default area: Nottinghamshire, Derbyshire, Leicestershire and their cities (substring match on county).
DEFAULT_COUNTIES = ["NOTTINGHAM", "DERBY", "LEICEST"]

# UK postcode pattern (outward code, space, inward code). Simplified but strict enough for validity checks.
POSTCODE_RE = r"^[A-Z]{1,2}[0-9][A-Z0-9]? [0-9][A-Z]{2}$"

PRICE_MIN, PRICE_MAX = 10_000, 5_000_000

RULES = [
    # rule_id, name, dimension, field, description, tolerance (max acceptable failure rate, %)
    ("R1", "Postcode missing", "Completeness", "postcode", "Postcode is blank", 0.5),
    ("R2", "Postcode invalid format", "Validity", "postcode",
     "Postcode present but does not match the UK postcode pattern", 0.1),
    ("R3", "House number/name missing", "Completeness", "paon", "Primary addressable object name (PAON) is blank", 0.1),
    ("R4", "Street missing", "Completeness", "street", "Street is blank", 1.0),
    ("R5", "Price outside expected range", "Validity", "price",
     f"Price below £{PRICE_MIN:,} or above £{PRICE_MAX:,} (flag for review, not necessarily wrong)", 1.0),
    ("R6", "Suspected duplicate", "Uniqueness", "transaction_id",
     "Same address, date and price as another sale with a different transaction ID", 0.1),
    ("R7", "Property type inconsistent with category", "Consistency", "property_type",
     "Property type 'Other' recorded as a standard (Category A) sale; 'Other' belongs to Category B", 0.1),
]


def load(paths: list[Path], counties: list[str]) -> pd.DataFrame:
    frames = []
    pattern = "|".join(c.upper() for c in counties)
    for path in paths:
        for chunk in pd.read_csv(path, header=None, names=COLUMNS, dtype=str, keep_default_na=False,
                                 chunksize=250_000):
            frames.append(chunk[chunk["county"].str.upper().str.contains(pattern, regex=True)])
    df = pd.concat(frames, ignore_index=True)
    for col in COLUMNS:
        df[col] = df[col].str.strip()
    df["transaction_id"] = df["transaction_id"].str.strip("{}")
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["transfer_date"] = pd.to_datetime(df["transfer_date"], errors="coerce").dt.date
    return df


def run_checks(df: pd.DataFrame) -> pd.DataFrame:
    """Return a long table of failures: transaction_id, rule_id."""
    pc = df["postcode"].str.upper()
    blank = lambda s: s.fillna("").str.strip() == ""  # noqa: E731

    addr_key = ["postcode", "paon", "saon", "street", "transfer_date", "price"]
    dup = df.duplicated(subset=addr_key, keep=False) & ~blank(df["postcode"]) & ~blank(df["paon"])

    checks = {
        "R1": blank(df["postcode"]),
        "R2": ~blank(df["postcode"]) & ~pc.str.match(POSTCODE_RE),
        "R3": blank(df["paon"]),
        "R4": blank(df["street"]),
        "R5": df["price"].isna() | (df["price"] < PRICE_MIN) | (df["price"] > PRICE_MAX),
        "R6": dup,
        "R7": (df["property_type"] == "O") & (df["ppd_category"] == "A"),
    }
    failures = [
        pd.DataFrame({"transaction_id": df.loc[mask, "transaction_id"], "rule_id": rule})
        for rule, mask in checks.items()
    ]
    return pd.concat(failures, ignore_index=True)


def build_tables(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    failures = run_checks(df)
    counts = failures.groupby("transaction_id").size()
    tx = df.assign(
        property_type=df["property_type"].map(PROPERTY_TYPE).fillna("Unknown"),
        new_build=df["old_new"].map(OLD_NEW).fillna("Unknown"),
        tenure=df["duration"].map(DURATION).fillna("Unknown"),
        ppd_category=df["ppd_category"].map(CATEGORY).fillna("Unknown"),
        dq_issue_count=df["transaction_id"].map(counts).fillna(0).astype(int),
    )
    tx = tx[["transaction_id", "transfer_date", "price", "postcode", "paon", "saon", "street", "town",
             "district", "county", "property_type", "new_build", "tenure", "ppd_category", "dq_issue_count"]]
    rules = pd.DataFrame(RULES, columns=["rule_id", "rule_name", "dimension", "field", "description",
                                         "tolerance_pct"])
    return tx, failures, rules


def build_tableau_tables(tx: pd.DataFrame, failures: pd.DataFrame, rules: pd.DataFrame
                         ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Tableau-ready tables.

    dq_monthly: one row per month x county x property type x rule, with records checked and failures side
    by side, so failure rate = failures / records checked is correct under any filter in Tableau.
    dq_failure_detail: one row per failed check with the record's details (the correction work list).
    """
    keys = ["month", "county", "property_type"]
    tx = tx.assign(month=pd.to_datetime(tx["transfer_date"]).dt.to_period("M").dt.to_timestamp())
    base = tx.groupby(keys).agg(records_checked=("transaction_id", "size"),
                                records_with_issues=("dq_issue_count", lambda s: int((s > 0).sum())))
    base = base.reset_index().merge(rules[["rule_id"]], how="cross")
    fails = (failures.merge(tx[["transaction_id", *keys]], on="transaction_id")
             .groupby([*keys, "rule_id"]).size().rename("failures").reset_index())
    monthly = base.merge(fails, on=[*keys, "rule_id"], how="left").fillna({"failures": 0})
    monthly["failures"] = monthly["failures"].astype(int)
    monthly = monthly.merge(rules[["rule_id", "rule_name", "dimension", "tolerance_pct"]], on="rule_id")

    detail = (failures.merge(tx, on="transaction_id")
              .merge(rules[["rule_id", "rule_name", "dimension"]], on="rule_id"))
    detail = detail[["transaction_id", "rule_id", "rule_name", "dimension", "transfer_date", "month", "price",
                     "postcode", "paon", "saon", "street", "town", "district", "county", "property_type",
                     "ppd_category"]]
    return monthly, detail


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("inputs", nargs="+", type=Path, help="Price Paid yearly CSV files")
    p.add_argument("--counties", nargs="+", default=DEFAULT_COUNTIES, help="county name substrings to keep")
    p.add_argument("--out", type=Path, default=Path("powerbi_data"))
    p.add_argument("--tableau-out", type=Path, default=Path("tableau_data"))
    a = p.parse_args()

    df = load(a.inputs, a.counties)
    tx, failures, rules = build_tables(df)
    a.out.mkdir(parents=True, exist_ok=True)
    tx.to_csv(a.out / "transactions.csv", index=False)
    failures.to_csv(a.out / "dq_failures.csv", index=False)
    rules.to_csv(a.out / "dq_rules.csv", index=False)

    print(f"{len(tx):,} sales checked across {tx['county'].nunique()} counties "
          f"({tx['transfer_date'].min()} to {tx['transfer_date'].max()})")
    summary = failures["rule_id"].value_counts().reindex([r[0] for r in RULES], fill_value=0)
    for (rule_id, name, *_rest, tol), n in zip(RULES, summary, strict=True):
        rate = n / len(tx) * 100
        flag = "BREACH" if rate > tol else "ok"
        print(f"  {rule_id} {name:<42} {n:>7,} ({rate:5.2f}% vs tolerance {tol}%) {flag}")
    print(f"Wrote {a.out}/transactions.csv, dq_failures.csv, dq_rules.csv")

    monthly, detail = build_tableau_tables(tx, failures, rules)
    a.tableau_out.mkdir(parents=True, exist_ok=True)
    monthly.to_csv(a.tableau_out / "dq_monthly.csv", index=False)
    detail.to_csv(a.tableau_out / "dq_failure_detail.csv", index=False)
    rules.to_csv(a.tableau_out / "dq_rules.csv", index=False)
    print(f"Wrote {a.tableau_out}/dq_monthly.csv ({len(monthly):,} rows), dq_failure_detail.csv, dq_rules.csv")


if __name__ == "__main__":
    main()
