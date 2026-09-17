# speed_limit_check - working conventions

- `~/Claude/MooveAI/` already exists on the machine this is worked on and
  is where all local checkouts/deployments of this repo live - the repo
  is checked out at `~/Claude/MooveAI/eyalamir-gpts2/`, with
  `~/Claude/MooveAI/keys.env` sitting as a sibling one level above it
  (matches `DEFAULT_KEYS_FILE` in `find_bad_speed_limit.py` and
  `keys.env.example`). Don't `mkdir` it or suggest cloning fresh - assume
  it's already there and just `cd ~/Claude/MooveAI/eyalamir-gpts2` (not
  `~/eyalamir-gpts2` or any other location), then `git pull`.
- Cloud Run deployment: `speed_limit_check/deploy.sh` (see its header and
  README.md's "Deploying to Cloud Run" section). Known values already used
  for this project's deployment: `PROJECT_ID=moove-platform-testing-data`,
  `REGION=us-central1`, `BUCKET_NAME=archimedes-control`,
  `BUCKET_LOCATION=US`, `INVOKER_EMAIL=eyal@moove.ai`.
