// Writes dist/_redirects so Netlify proxies /api/* to the backend (Fly.io).
// netlify.toml can't interpolate env vars into redirects, hence this step.
import { writeFileSync } from "node:fs";

const backend = (process.env.BACKEND_URL || "https://finance-advisor-agent-api.fly.dev").replace(/\/$/, "");
writeFileSync("dist/_redirects", `/api/*  ${backend}/api/:splat  200\n/*  /index.html  200\n`);
console.log(`_redirects: /api/* -> ${backend}/api/*`);
