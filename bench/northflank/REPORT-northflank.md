# chrome-live startup latency on Northflank

Run: measured 2026-06-05 PDT with the shared harness (CDP-only result, noVNC
informational). See `run-cold.log` for the raw run. Numbers are real (live
deployment service on a paid Northflank team, project `remote-browser`,
us-central), measured wall-clock from the scale 0->1 trigger to CDP readiness at
the public `*.code.run` ingress.

This platform was previously listed as blocked (the old token in `bench/.env` was
free-tier and could not provision). The token in the repo-root `.env` is a
different, billed team and provisions normally.

## Environment

| Property         | Value                                                                          |
| ---------------- | ------------------------------------------------------------------------------ |
| Platform         | Northflank (northflank.com, `*.code.run` HTTP ingress)                          |
| Region / cluster | us-central (GCP, cluster `nf-us-central`, namespace `ns-k8f9gbvvtk5q`)          |
| Arch             | x86 / amd64                                                                    |
| Tier             | `nf-compute-200-4` = 2 vCPU / 4 GiB (no 2 vCPU / 2 GiB tier exists; 4 GiB is the min RAM at 2 vCPU) |
| Rootfs storage   | Not probed (Northflank exec is unavailable for this service, see below); unconfirmed |
| Image            | `ghcr.io/remotebrowser/chrome-live@sha256:08c3b928...` (base amd64; the adapted boot ignores the image ENTRYPOINT) |
| Boot             | Adapted boot (s6 bypassed), launched directly via a command override          |

## Headline finding: the stock image cannot boot, adapted boot required

Northflank always runs the container as PID 2 under its own `env-injector` PID-1
shim (mandatory runtime secret injection, no opt-out) and grants only the default
Docker capability set (`CapEff=0x00000000a80425fb`, no `CAP_SYS_ADMIN`). Verified
live with a PID probe: `MYPID=2 PID1=env-injector`.

s6-overlay refuses to run unless it is PID 1. chrome-live's `unshare --pid --fork`
workaround (used on Fly/Daytona) needs `CAP_SYS_ADMIN`, so on Northflank it fails:

    unshare: unshare failed: Operation not permitted

and both the stock `chrome-live` image and the `chrome-live-daytona` variant (which
bakes that unshare ENTRYPOINT) crash-loop. This is the same class of blocker as
Modal (there gVisor denies the unshare). Northflank exposes no field to add Linux
capabilities, run privileged, or disable the PID-1 injector (the API/CLI deployment
config has no securityContext/capabilities/privileged option).

Fix: clear the entrypoint and launch the chrome-live services directly, reusing
the Modal bench's adapted boot byte-for-byte (Xvnc + tinyproxy + websockify noVNC
+ Chrome + socat CDP proxy `:9222<-:9221`; XFCE and browser-trace omitted, neither
on the CDP/noVNC path). On Northflank this is delivered as a `customEntrypoint`
`/bin/sh` + a base64-wrapped boot script in `customCommand` (no repo/image change).

## Readiness signal: public-edge only

There is no internal probe and no literal external `/json/version` 200 on this
platform:

- Internal exec is unavailable. `northflank exec` returns `Unexpected server
  response: 500` for this service (0/8 then 0/6 across two attempts), so the
  intra-container probe the other benches use does not work here.
- Public ports are HTTP/HTTP2 only (no L4/TCP: the ports API rejects
  `protocol: TCP` with "must be one of [HTTP, HTTP/2]"). The `*.code.run` ingress
  forwards the public Host header, which Chrome's CDP rejects with HTTP 500
  "Host header is specified and is not an IP address or localhost" -- the same
  Host-header limitation seen on E2B/Modal. With HTTP-only ports there is no way
  to send `Host: localhost` while still routing, so a 200 is unobtainable.

But the readiness moment is still observable through the public edge: the
`cdp-proxy` socat only starts listening once Chrome's CDP (`:9221`) is up, so the
ingress returns 503 (no upstream) while CDP is down and flips to a Chrome HTTP
response (500 Host-reject) the instant CDP is ready. The bench measures that flip
(`webSocketDebuggerUrl` for a 200, or the Host-reject body). It is CDP-HTTP
readiness as seen at the public ingress, so it is reported as the external number;
there is no internal number to compare it against.

One client-side gotcha: the per-port `*.code.run` DNS was flaky to resolve
(NXDOMAIN / negative-caching after a delete+recreate, via a Tailscale resolver).
Both hostnames are CNAMEs to the project load balancer, so the probe pins curl to
the LB IP with `--resolve` and lets the ingress route by the real Host header.

## Results (n=10, cold only)

Northflank runs plain Kubernetes pods with no memory-snapshot resume, so the only
meaningful path is cold (scale 0 -> 1). `MODE=resume` is disabled in the script.
All times are milliseconds, trigger -> CDP ready at the public ingress.

| Signal                | p50   | p95   | min   | max   |
| --------------------- | ----- | ----- | ----- | ----- |
| CDP external (result) | 15858 | 33069 | 15022 | 33069 |
| noVNC (informational) | tracks CDP +~300ms per run (see run-cold.log) |

Per-run (ms, sorted): 15022 15224 15564 15821 15858 15953 17511 18142 28930 33069.

Observations:
- Cold CDP is ~15-18s for most runs, with two ~29-33s outliers (runs 3 and 5).
  The spikes are pod-scheduling / cross-node variance: a scale 0->1 can land on a
  different node, and the deployment rollout status (`PENDING -> IN_PROGRESS ->
  COMPLETED`) alone took ~30-50s in spot checks. The ~16s steady-state is pod
  schedule + container start + the adapted boot (Chrome launch) + ingress route
  attach.
- The image is pulled once at first deploy and stays cached across scale cycles,
  so per-run image pull is excluded (first-ever deploy with the ~2GB pull is a
  one-time setup cost, not in the timed runs).
- This is the slowest cold among the working platforms (Daytona ~2s, Modal ~4s,
  Fly ~5s, E2B ~10s typical / 24s on a bad node). Northflank's cold is in the
  E2B-bad-day range and far above the others.

## Latency decomposition

Measured 2026-06-05 with an instrumented boot (milestone markers to stderr, each
carrying an in-container `date +%s.%3N` so they are immune to log buffering and
host/container clock skew), two cold scale-cycles, read back from the timestamped
container logs. One representative cycle:

| Phase                        | What happens                                                                | Duration |
| ---------------------------- | --------------------------------------------------------------------------- | -------- |
| 1. scale 0->1 -> container   | k8s schedules a fresh pod, creates the container, env-injector init runs     | ~11s     |
| 2. adapted boot script       | cont-init (adblock hosts) + Xvnc + xrdb-ready -> Chrome launched             | ~0.9s    |
| 3. Chrome CDP + ingress      | Chrome opens `:9221`, socat exposes `:9222`, ingress attaches and serves     | ~3-4s    |
| Total                        |                                                                             | ~15-16s  |

Evidence: the container's first log line ("Starting container entrypoint") appears
~11s after the `scale:1` call; `boot_start -> chrome_launched` is ~0.9s (skew-free,
both in-container); `chrome_launched -> external CDP ready` is ~3-4s. The dominant
~70% is phase 1, before the app even starts: a full Kubernetes pod cold-start on
scale-from-zero (schedule + pod sandbox + CNI + container create + the mandatory
env-injector init). It is not image pull (cached; ~11s was stable across both
cycles) and not the app (the boot script to Chrome is under 1s).

Caveats: phases 1 and 3 cross the host clock vs the container clock (sub-second if
both NTP-synced, immaterial at this scale); phase 2 is skew-free. The ~11s of phase
1 is measured as a black box (scale call -> first container log); its internal
split (scheduler vs CNI vs container-create vs secret-injector) was not isolated.

### Why ~8x slower than Daytona

Daytona reaches CDP in ~2s internal / ~5s external; Northflank ~16s. The gap is
almost entirely phase 1, because "start" means different things on the two
platforms:

- Daytona stop->start (or archive->start) is a warm container restart on a host
  that keeps the rootfs on local NVMe: measured start infra ~1.1s, app boot
  ~0.5-0.9s.
- Northflank scale 0->1 tears the pod down completely and schedules a brand-new
  one each time: ~11s to the container's first log line.

App boot (~1s) and the Chrome-CDP startup (~4s) are comparable on both; the ~10s
difference is Northflank rebuilding a pod from scratch vs Daytona restarting a warm
container. Neither has a memory snapshot, but Daytona keeps the container + rootfs
warm across stop/start while Northflank does not.

### The ~11s is per-pod scheduling, not a scale-from-zero penalty

Scaling an already-running service 1->2 (instrumented logs, same boot) schedules
the new replica in ~11.7s, essentially identical to the ~10.5s of a 0->1
scale-from-zero:

| Phase                              | scale 0->1 | scale 1->2 |
| ---------------------------------- | ---------- | ---------- |
| scheduling (scale call -> container start) | 10.5s | 11.7s |
| adapted boot script (cont-init + Xvnc) | 1.2s   | 1.0s  |
| Chrome opens CDP (`:9221`)         | 3.8s       | 5.2s       |
| -> internal CDP up                 | ~15.5s     | ~17.9s     |

So the ~11s is the inherent cost of materialising any new pod (k8s schedule +
microVM/container create + env-injector), paid even when the service is warm. A
pre-warmed pool of replicas would not add capacity faster; only an
already-running instance (idle cost) or a memory-snapshot resume (which Northflank
lacks) hides it. This run also showed the public ingress attach is negligible:
external-ready coincided with internal CDP-up to within ~0.5s, so phase 3 above is
Chrome opening its debug port, not the ingress.

### Sandboxes (microVM) are the default and do not change this

Northflank "Sandboxes" are not a separate runtime: per the docs, on the managed
cloud "MicroVM isolation is enabled by default" and "a sandbox is a service", so
this benchmark already ran as a microVM sandbox. Confirmed by deploying the stock
image as a plain service: it still crash-loops with `s6-overlay-suexec: fatal: can
only run as pid 1` under the `env-injector` PID 1. The microVM gives a dedicated
guest kernel for isolation, but the container inside is still a restricted OCI
container (PID 2, no `CAP_SYS_ADMIN`, Kata-style), so the adapted boot is still
required and the latency is unchanged. The vendor "boots in under 1 second" refers
to the microVM substrate, not the ~16s end-to-end for this 2GB GUI image.

## Acceptance

Target: ready < ~1s, acceptance p50 <= ~1.5s. Local reference 0.85-1.37s (warm
Docker, NVMe).

FAIL. CDP p50 ~16s at the public edge, ~10x the slowest other platform's cold and
~16x the gate. There is no memory-snapshot resume to provide a fast path, so a
direct (spawn-per-request) approach inherits the full ~16s cold. A pre-warmed pool
could hide it, but Northflank deployment standbys must stay scaled to >=1 (running
compute cost), so the pool is not cheap the way Fly's suspended machines are.

## Pricing

Plan `nf-compute-200-4` (2 vCPU / 4 GiB): $0.067 / container-hr, $0 base fee,
scales to zero (no compute charge while scaled to 0).

| Item                    | Value                                   |
| ----------------------- | --------------------------------------- |
| Compute, 2 vCPU / 4 GiB | $0.067 / container-hr                   |
| Base fee                | $0                                      |
| Monthly @ 100h running  | $6.70 compute                           |
| Monthly @ 1000h running | $67 compute                             |
| Scales to zero          | Yes (compute only; scaled-to-0 = $0 compute) |

Note: there is no 2 vCPU / 2 GiB tier (the other benches' reference size); 4 GiB
is the minimum RAM at 2 vCPU, so the closest tier costs $0.067/hr. That is cheaper
per hour than Daytona/E2B ($0.133) and Modal (~$0.19), and about 2x Fly's
shared-4x ($0.033). Cost is not the problem here; the ~16s cold latency is.

## Blockers and integration notes

1. Adapted boot REQUIRED (see headline above): PID-2 + no `CAP_SYS_ADMIN` ->
   `unshare` denied -> s6 cannot be PID 1 -> stock and daytona images crash-loop.
   Worked around with a direct-launch command override (Modal recipe).
2. No usable CDP `/json/version` 200 over the public edge: HTTP-only public ports
   + Chrome's Host-header check. Readiness measured via the 503->500 ingress flip.
3. Internal exec unavailable: `northflank exec` returns HTTP 500 for this service,
   so there is no intra-container readiness number.
4. Flaky `*.code.run` DNS from some resolvers; probe pins the LB IP via `--resolve`.
5. No memory-snapshot resume (plain k8s pods); cold is the only path.

## Reproduce

Files:
- `bench/lib.sh`              shared harness (copied to `bench/northflank/lib.sh`)
- `bench/northflank/northflank.sh`  Northflank bp_* hooks + adapted boot (entry point)
- `bench/northflank/run-cold.log`   raw output

Run (needs `NORTHFLANK_API_TOKEN` in the repo-root `/Users/bin/dev/chrome-live/.env`;
in a git worktree the root `.env` is absent, so pass `ENV_FILE` explicitly):

```
ENV_FILE=/Users/bin/dev/chrome-live/.env MODE=cold COUNT=10 READY_TIMEOUT=180 \
  BENCH_TIMEOUT=1800 bash bench/northflank/northflank.sh 2>&1 | tee bench/northflank/run-cold.log
```

Hook -> Northflank mapping: bp_make_cold = scale to 0 (wait `.status.deployment ==
null`); bp_make_paused = return 1 (no resume); bp_trigger = scale to 1 (timed);
bp_ready_cdp_external = ingress CDP returns a Chrome HTTP response; bp_teardown =
delete the service (the pre-existing `remote-browser` project is left intact).
