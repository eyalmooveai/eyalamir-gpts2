# LaForge agent (v0)

LaForge puts Moove's Forge feature-combination results inside the customer's own LLM
through an MCP tool server. The LLM converses and explains. The tools do every
calculation, and every answer carries provenance and caveats.

**Status:** Blind mode on the existing Forge output for Quanata/MA (`frequency` target
only). Scores are in-sample correlations. Holdout validation, false-discovery control,
severity targets and the Schema-aware, Data-aware and Model-aware modes are not built yet.
See `../LaForge_Plan.md` and `../LaForge_Implementation_Plan.md` for the plan.

## Install and test
```
pip install -e ".[dev]"            # add ,bigquery or ,offline as needed
pytest
```

## Configure (environment variables)
| Variable | Meaning |
|---|---|
| `LAFORGE_CATALOG` | Path to the feature catalog JSON (a list of names, or name -> definition) |
| `LAFORGE_BQ_PROJECT` | Billing project for read-only BigQuery queries (hosted mode) |
| `LAFORGE_SNAPSHOT_DIR` | Folder of Parquet snapshots (offline mode; no network) |
| `LAFORGE_OUT_DIR` | Where recipe files are written (default `laforge_out`) |
| `LAFORGE_TRANSPORT` | `stdio` (default) or `streamable-http` |

## Run
- Claude Code: `claude mcp add laforge -- python -m laforge.server`
- Local bundle: build the `Dockerfile`, then use `mcp.example.json`.
- Hosts without MCP: `python -m laforge.export_tools` prints the tools as
  function-calling definitions; use `INSTRUCTIONS` in `laforge/server.py` as the system prompt.
- The HTTP transport has no authentication. Put it behind your own gateway.

## Offline snapshot
`python scripts/make_snapshot.py <folder>` copies the two Forge result tables to Parquet
using read-only queries. The snapshot contains Moove rankings; decide who may receive it.

## Rules for this codebase
- Code here is reviewed and deployed by people. Nothing in it creates or changes cloud
  resources; BigQuery access is `SELECT` only, with a bytes-billed cap.
