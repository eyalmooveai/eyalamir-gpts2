# speed_limit_check - working conventions

- All local checkouts/deployments of this repo live under `~/Claude/MooveAI/`
  (e.g. `~/Claude/MooveAI/eyalamir-gpts2/`), with `~/Claude/MooveAI/keys.env`
  sitting as a sibling one level above the repo checkout - this matches
  `DEFAULT_KEYS_FILE` in `find_bad_speed_limit.py` and `keys.env.example`.
  When giving CLI commands for this repo, `cd ~/Claude/MooveAI/eyalamir-gpts2`
  (not `~/eyalamir-gpts2` or any other location).
- Cloud Run deployment: `speed_limit_check/deploy.sh` (see its header and
  README.md's "Deploying to Cloud Run" section). Known values already used
  for this project's deployment: `PROJECT_ID=moove-platform-testing-data`,
  `REGION=us-central1`, `BUCKET_NAME=archimedes-control`,
  `BUCKET_LOCATION=US`, `INVOKER_EMAIL=eyal@moove.ai`.
