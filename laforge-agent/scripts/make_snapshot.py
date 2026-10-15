"""Read-only: copy the Forge result tables to local Parquet for the offline bundle.

Usage: python scripts/make_snapshot.py <output_folder>
The snapshot contains Moove rankings; decide who may receive it before sharing.
"""
import pathlib
import sys

from google.cloud import bigquery

from laforge.engine import DATASETS

out = pathlib.Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
client = bigquery.Client()
for name, cfg in DATASETS.items():
    for kind, table in (("pairs", cfg["pairs_table"]), ("singles", cfg["single_table"])):
        client.query(f"SELECT * FROM `{table}`").to_dataframe().to_parquet(out / f"{name}_{kind}.parquet")
