# Deployment inputs (runtime copy)

`deployment-inputs.local.json` is the runtime copy of `docs/config/deployment-inputs.example.json` and is gitignored.
Edit values here (addresses, digests); never put passwords, tokens or hashes in it: secrets are `env://NAME` references
resolved from `.env` files (0600) on the management VM. Keep it in sync with the example when keys are added.
