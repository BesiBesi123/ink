Read when finishing a milestone or preparing a project to be run, shared or deployed: README, env, scripts, CI, git hygiene, deploy configs.

# Delivery Playbook - leave it runnable and deployable

## 0. Top rules
- A clean clone must work with: install -> copy .env.example to .env -> `npm run dev`; `npm test`, `npm run build` pass.
- README: what it is, features, stack, quick start, scripts, env vars table, architecture overview (or link to docs/SPEC.md), how to deploy.
- `.env.example` lists every variable with a safe dummy value and a comment; real `.env` is git-ignored; no secrets anywhere in the repo.
- Scripts in package.json: dev, build, preview/start, test, lint, typecheck, format; they work on Windows and Linux (no bash-only syntax, use cross-platform tools).
- CI (GitHub Actions): on push and PR -> install (npm ci), typecheck, lint, test, build.
- Deploys and pushes are the user's call: prepare everything, but ask before pushing or deploying (ink always asks for these).

## 1. Git
- Logical commits with clear messages (what and why); never commit build output, node_modules, .env, keys.
- Check `git status` and the secret scan before every push.

## 2. Deploy configs
- Static frontend: build command + output dir (dist); SPA fallback routing.
- Node server: start command, PORT from env, health check path, websocket support enabled on the host.
- Contracts: deploy script per network, addresses written to a JSON the frontend imports, verified on the explorer.

## Checklist
- [ ] README + .env.example current
- [ ] all scripts run; build passes
- [ ] CI workflow present
- [ ] no secrets/build output in git
