# Running and deploying

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt             # web3 is optional: only for on-chain anchoring
cp .env.example .env                         # then set real secrets
export $(grep -v '^#' .env | xargs)          # or use your process manager
python scripts/create_admin_user.py          # first administrator (password prompted)
python app.py                                # http://localhost:5000  (dashboard /, console /app/)

cd frontend && npm install && npm run build  # React console served by Flask at /app/
cd blockchain && npm install && npx hardhat test

python tests/run_tests.py                    # full backend suite
cd frontend && npm run typecheck && npm test # React checks
```

Demo: `python scripts/seed_demo.py` creates a DEMO-labelled dataset through the real
API, from organizations to retail shelf and consumer verification, and prints sign-ins.

Production notes: run behind a TLS-terminating reverse proxy; use one worker process
(the SSE broker, rate limiters and token throttles are in-process); keep `.env` out of
version control; schedule `POST /api/devices/silence-check`,
`POST /api/chain/checkpoints` and `/submit` (ADMIN) from cron. There is **no** Dockerfile
or CI pipeline in this repository.
