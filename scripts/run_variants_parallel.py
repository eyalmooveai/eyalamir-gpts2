#!/usr/bin/env python3
"""Train + archive feature-toggle variants for many states in parallel.

Per variant (variants always run one after another, because the feature_toggle config is shared):
  1. save the variant's config once        (orchestrator, empty state list, p_save_config=TRUE)
  2. run every still-unregistered state as its own BigQuery job, N at a time
     (orchestrator, one state, p_save_config=FALSE -- it verifies the saved config before training)
  3. build the national 'US' row once all 50 states are registered

Safe to re-run: registered states are skipped. Ctrl-C cancels the in-flight jobs (resumable).
Needs: pip install google-cloud-bigquery, and credentials (gcloud auth application-default login).

  pilot:    python run_variants_parallel.py --year 2026 --month 9 --states MT,NE,NV --workers 3 --variants avgspeed_only
  plan:     python run_variants_parallel.py --year 2026 --month 9 --dry-run
  full run: python run_variants_parallel.py --year 2026 --month 9 --workers 8
"""
import argparse
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from google.cloud import bigquery

US_STATES = [
    'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA',
    'HI', 'ID', 'IL', 'IN', 'IA', 'KS', 'KY', 'LA', 'ME', 'MD',
    'MA', 'MI', 'MN', 'MS', 'MO', 'MT', 'NE', 'NV', 'NH', 'NJ',
    'NM', 'NY', 'NC', 'ND', 'OH', 'OK', 'OR', 'PA', 'RI', 'SC',
    'SD', 'TN', 'TX', 'UT', 'VT', 'VA', 'WA', 'WV', 'WI', 'WY',
]
KNOWN_VARIANTS = ('avgspeed_only', 'freeflow_only', 'both')
ORCH = 'calc.model_registry_prepare_speed_limit_model_variants'
FATAL_RE = re.compile(r'quota|rate.?limit|feature_toggle config', re.I)

lock = threading.Lock()
stop = threading.Event()
inflight = {}
fatal = []
stats = {'billed_bytes': 0}


def log(msg):
    print(f'{time.strftime("%H:%M:%S")}  {msg}', flush=True)


def orch_sql(states, year, month, tag, save_config):
    arr = ('[' + ', '.join(f"'{s}'" for s in states) + ']') if states else 'CAST([] AS ARRAY<STRING>)'
    return (f"CALL {ORCH}({arr}, {year}, {month}, FALSE, ['{tag}'], "
            f"{'TRUE' if save_config else 'FALSE'});")


def run(client, sql, label):
    job = client.query(sql)
    log(f'job {job.job_id}  {label}')
    with lock:
        inflight[job.job_id] = (label, job)
    try:
        job.result()
    finally:
        with lock:
            inflight.pop(job.job_id, None)
    with lock:
        stats['billed_bytes'] += job.total_bytes_billed or 0


def registered(client, tag, year, month):
    cfg = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter('tag', 'STRING', tag),
        bigquery.ScalarQueryParameter('y', 'INT64', year),
        bigquery.ScalarQueryParameter('m', 'INT64', month),
    ])
    rows = client.query(
        'SELECT DISTINCT state FROM calc_archive.model_registry_results '
        'WHERE tag = @tag AND year_num = @y AND month_num = @m', job_config=cfg).result()
    return {r.state for r in rows}


def run_state(client, state, year, month, tag):
    if stop.is_set():
        return
    log(f'start     {tag}/{state}')
    try:
        run(client, orch_sql([state], year, month, tag, False), f'{tag}/{state}')
        log(f'job done  {tag}/{state}')
    except Exception as exc:
        msg = str(exc)
        if FATAL_RE.search(msg):
            with lock:
                fatal.append(f'{tag}/{state}: {msg}')
            stop.set()
            log(f'STOP      {tag}/{state}: {msg}')
        else:
            log(f'failed    {tag}/{state}: {msg}')


def cancel_inflight():
    with lock:
        jobs = list(inflight.values())
    for label, job in jobs:
        try:
            job.cancel()
            log(f'cancelled {label} ({job.job_id})')
        except Exception as exc:
            log(f'could not cancel {label} ({job.job_id}): {exc}')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--year', type=int, required=True)
    ap.add_argument('--month', type=int, required=True)
    ap.add_argument('--variants', default='avgspeed_only,freeflow_only', help='comma list, run in this order')
    ap.add_argument('--states', default=','.join(US_STATES), help='comma list (default: all 50)')
    ap.add_argument('--workers', type=int, default=8, help='concurrent state jobs')
    ap.add_argument('--retries', type=int, default=1, help='extra rounds for states that did not register')
    ap.add_argument('--no-us', action='store_true', help='never build the US row')
    ap.add_argument('--project', default=None, help='default: the credentials default project')
    ap.add_argument('--dry-run', action='store_true', help='print the plan; touch nothing')
    args = ap.parse_args()

    variants = [v.strip() for v in args.variants.split(',') if v.strip()]
    states = [s.strip().upper() for s in args.states.split(',') if s.strip()]
    if not 1 <= args.month <= 12:
        sys.exit('--month must be 1..12')
    if any(v not in KNOWN_VARIANTS for v in variants):
        sys.exit(f'--variants must be among {KNOWN_VARIANTS}')
    if any(not re.fullmatch(r'[A-Z]{2}', s) for s in states):
        sys.exit('--states must be 2-letter codes')
    build_us = (not args.no_us) and set(states) == set(US_STATES)

    if args.dry_run:
        for tag in variants:
            print(f'-- {tag}: save config once')
            print(orch_sql([], args.year, args.month, tag, True))
            print(f'-- {tag}: one job per unregistered state, {args.workers} at a time, e.g.')
            print(orch_sql([states[0]], args.year, args.month, tag, False))
            if build_us:
                print(f'-- {tag}: once all {len(US_STATES)} states are registered')
                print(orch_sql(['US'], args.year, args.month, tag, False))
        return 0

    client = bigquery.Client(project=args.project)
    exit_code = 0
    try:
        for tag in variants:
            if stop.is_set():
                break
            todo = [s for s in states if s not in registered(client, tag, args.year, args.month)]
            log(f'{tag}: {len(states) - len(todo)} of {len(states)} states already registered, {len(todo)} to run')
            if todo:
                run(client, orch_sql([], args.year, args.month, tag, True), f'{tag}/set-config')
                for attempt in range(args.retries + 1):
                    if not todo or stop.is_set():
                        break
                    log(f'{tag}: round {attempt + 1}: {len(todo)} state(s), {args.workers} at a time')
                    with ThreadPoolExecutor(max_workers=args.workers) as pool:
                        try:
                            list(pool.map(lambda s: run_state(client, s, args.year, args.month, tag), todo))
                        except KeyboardInterrupt:
                            stop.set()
                            cancel_inflight()
                            raise
                    done = registered(client, tag, args.year, args.month)
                    todo = [s for s in todo if s not in done]
            if stop.is_set():
                break
            if todo:
                log(f'{tag}: NOT registered after {args.retries + 1} round(s): {", ".join(todo)} '
                    f'-- see eyal_scratch.logtable; stopping before the next variant')
                exit_code = 1
                break
            if build_us and 'US' not in registered(client, tag, args.year, args.month):
                log(f'{tag}: building the US row from the archived states')
                run(client, orch_sql(['US'], args.year, args.month, tag, False), f'{tag}/US')
                if 'US' not in registered(client, tag, args.year, args.month):
                    log(f'{tag}: the US row was NOT registered -- see eyal_scratch.logtable')
                    exit_code = 1
                    break
            log(f'{tag}: complete')
    except KeyboardInterrupt:
        stop.set()
        cancel_inflight()
        log('interrupted; re-run the same command to resume')
        exit_code = 130

    if fatal:
        for f in fatal:
            log(f'STOPPED: {f}')
        log('re-run the same command to resume; registered states are skipped')
        exit_code = 1
    log(f'billed by these jobs (as reported by BigQuery): {stats["billed_bytes"] / 2**30:.2f} GB')
    return exit_code


if __name__ == '__main__':
    sys.exit(main())
