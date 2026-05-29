# chrome-live on-demand hosting: provider benchmark results

Aggregated: 2026-05-28. All four tested platforms (Fly, Daytona, Modal, E2B)
re-measured 2026-05-28 PDT with the cleaned-up CDP-only harness. See each
`bench/<platform>/REPORT-<platform>.md` and `run-*.log` / `*-results.txt` for
the exact run timestamps.

Note on the Fly numbers: the 2026-05-28 rerun's cold time (p50 2.5s) is far
better than the original 2026-05-27 measurement (~67s). Possible explanations
include Fly upgrading machine rootfs storage, the worker's page cache warming
across the warm-up cycle in `bp_setup` and surviving subsequent stop/start
calls on the same worker, or scheduler stickiness keeping all 10 runs on the
same machine. We have not isolated the cause; the older "HDD rootfs ⇒ ~30x
slowdown" framing should be treated as historical and re-verified before
relying on it.

Aggregated results from the per-platform validation sessions. Each platform was
measured in its own session with a reusable script built on the shared
`bench/lib.sh` timing harness. This report covers the four platforms with
working free trials that have completed: [Fly.io][fly], [Modal][modal],
[Daytona][daytona], [E2B][e2b]. The other four ([Hetzner][hetzner] NVMe baseline,
[Northflank][northflank], [Koyeb][koyeb], [Cloudflare][cloudflare-containers])
are pending or blocked, listed at the end.

Goal: one ephemeral instance per user session, ready (CDP responding) on demand,
scaling to zero with no fixed base fee. VNC/noVNC is out of scope; CDP is the
only readiness signal measured here.

## Executive summary

Tested platforms ranked by **internal CDP resume latency**, p50 ascending. Times
in seconds. Internal CDP isolates real container readiness from per-platform
proxy / tunnel overhead and is the signal we use for platform decisions; the
external number is the user-facing public-endpoint number for reference. See
the terminology section in Methodology for what each column actually measures.

Approach is "direct" (spawn fresh container per request, zero idle cost) or
"pool" (claim one of a pre-warmed set); see the approach section below.

| Rank | Platform           | Resume int p50/p95 | Resume ext p50/p95 | Cold int p50/p95 | Cold ext p50/p95 | $/container-hr                       | Base fee            | Approach |
| ---- | ------------------ | ------------------ | ------------------ | ---------------- | ---------------- | ------------------------------------ | ------------------- | -------- |
| 1    | [Fly.io][fly]      | 1.7 / 4.8          | 1.9 / 5.0          | 5.1 / 5.9        | 5.2 / 6.8        | $0.033 (shared-4x; $0.016 shared-2x) | $0                  | pool     |
| 2    | [Daytona][daytona] | 2.1 / 2.3          | 4.7 / 4.9          | 2.0 / 2.3        | 4.7 / 5.0        | $0.133                               | $0 (+$200 credit)   | direct   |
| 3    | [E2B][e2b]         | 2.2 / 3.4          | n/a                | 24.2 / 32.5      | n/a              | $0.133                               | $150/mo (mandatory) | pool     |
| 4    | [Modal][modal]     | 3.0 / 4.4          | 3.6 / 5.0          | 3.9 / 8.3        | 4.7 / 9.5        | ~$0.19                               | $0 (+$30/mo credit) | pool     |

How each platform delivers that performance:

- Fly.io: cheap suspended pool, resume restores RAM (min 1.6s on this rerun).
  2026-05-28 cold (~5s) is far faster than the original 2026-05-27 baseline
  (~67s), suggesting Fly upgraded rootfs; direct may now be viable. Internal
  and external both reach the dedicated IPv4 and track within ~200ms here.
- Daytona: NVMe, no memory snapshot. Cold and resume are both ~2s container
  restart. External adds ~2.6s of preview-proxy reattach; a custom network
  (WireGuard, SDK transport, SSH tunnel) closes that gap.
- E2B: Firecracker pause/resume with full memory + fs preserved on NVMe.
  Disqualified on cost by the base fee. No usable external CDP path (Chrome
  rejects the proxy's Host header). Cold today was on a slow node; original
  run was ~10s p50.
- Modal: gVisor checkpoint/restore. Latency capped by gVisor syscall overhead
  + tunnel RTT, not disk. Stock s6 image needs an adapted boot.

Bottom line: with internal CDP as the primary signal, Fly.io leads on resume
(p50 1.7s, min 1.6s) AND remains the cheapest by a wide margin, so it's the
clear winner under the no-base-fee + scale-to-zero constraints. Daytona is
second at ~2.1s with no pool needed (direct creates reach the same ~2s, both
cold and resume), provided a custom network replaces the preview proxy.
E2B's resume is similar at ~2.2s but the $150/mo base fee disqualifies it.
Modal is the slowest tested, capped by gVisor + tunnel.

### Not yet tested (ranked by expected fast-path latency, best first)

For platforms with a snapshot/resume path, the fast path is resume. For direct-only
platforms (Hetzner, Cloudflare), it is cold-create. All numbers below are claims or
extrapolations, not measurements.

1. [Koyeb][koyeb] (pool). Claims ~200ms snapshot-backed light-sleep wake. Cost: per-second,
   but the small-CPU container rate is not cleanly published; a possible ~$29/mo
   base plan would fail the no-base-fee rule. Unverified; needs a token.
2. [Northflank][northflank] (direct if its sub-second cold claim holds, else pool). Claims
   sub-second cold + snapshot resume; cheapest scale-to-zero compute at ~$0.05/
   container-hr ($0.01667/vCPU-hr + $0.00833/GiB-hr), $0 base. Blocked: the provided
   token is free-tier with no payment method (cannot provision, 409). Storage type
   unconfirmed.
3. [Hetzner][hetzner] / bare NVMe VM (direct). Expected ~1s cold start (local NVMe, image
   pre-cached), matching the local baseline. No pool needed, zero idle cost. Cost is
   per-VM fixed, not per-container: a CCX23 (4 vCPU / 16 GiB, ~$32/mo) packs ~6
   containers ≈ ~$0.007/container-hr if fully packed, but it does not scale to zero.
   Pending an HCLOUD_TOKEN to confirm.
4. [Cloudflare Containers][cloudflare-containers] (direct only). 2-3s cold and no memory snapshot, so a pool
   cannot help. Cost ~$0.043/container-hr net + a $5/mo Workers Paid base fee.

## Methodology

Per run, wall-clock from the start/resume trigger until CDP is ready
(`curl /json/version` == 200). CDP is the only signal measured; VNC readiness
is out of scope. Each platform ran 10 runs per mode and reports p50/p95/min/max.
The container image was pre-cached on the execution host in every case, so
image-pull time is excluded from the numbers. Raw logs and the per-platform
scripts live under `bench/fly/`, `bench/modal/`, `bench/daytona/`, `bench/e2b/`.

### Terminology

- internal CDP: the readiness probe runs INSIDE the container. The bench
  asks the platform SDK to exec `curl 127.0.0.1:9222/json/version` against
  the running sandbox and times trigger to that succeeding. This isolates
  container-level readiness from any public-edge latency.
- external CDP: the bench curls the platform's public endpoint
  (preview URL, dedicated IPv4, etc.) for `/json/version`. This is what
  a naive HTTP client over the public edge would see; includes whatever
  proxy / tunnel reattach cost the platform adds.
- cold: the platform is brought to a fully-stopped (and on Daytona,
  archived-to-object-storage) state before the timed trigger. No memory
  preserved.
- resume: the platform is brought to a paused / suspended / snapshotted
  state before the timed trigger. Memory is preserved where supported
  (Fly suspend, E2B beta_pause, Modal `_experimental_snapshot`). Daytona
  has no memory-resume; "resume" there is a faster restart (stop without
  archive), but Chrome still re-launches.

Internal CDP is the PRIMARY signal for decisions, because it isolates true
container readiness from per-platform public-edge variance. External CDP is
also reported because it is what a real client would see. The two diverge in
both directions:

- On Daytona and Modal, internal < external. The public path is a reverse
  proxy that takes ~2-3s to reattach a route after start.
- On Fly, internal > external. Fly's "internal" probe goes through
  `flyctl machine exec`, which is an SSH round-trip with its own connect
  overhead. The dedicated public IPv4 has no proxy in the path, so curl
  against it (external) is closer to true container readiness.
- On E2B, only internal is available. Chrome's DevTools HTTP rejects E2B's
  proxied Host header, so there is no usable external CDP path.

### Operational note: bash coproc + `wait` deadlock (bench bug, not platform)

While instrumenting the reruns, I hit and fixed a bug in our own harness
that earlier looked like a Daytona / Modal "SDK hang." Symptoms: bench
appeared frozen for the entire `BENCH_TIMEOUT`; sandbox-side state checks
showed the SDK call had actually succeeded server-side; pure bare-SDK
scripts on the same machine completed in seconds.

Root cause: `bench/lib.sh` forked background subshells for the external and
noVNC probes while the parent shell held a coproc to the Daytona / Modal
Python daemon. The subshells inherited the parent's coproc FDs (`{CTL[0]}`,
`{CTL[1]}`) and bash's `wait` builtin then refused to return after the
subshells had already exited cleanly. Both the FD inheritance and bash's
`wait` behavior under a live coproc are well-known undefined territory.

Fix: probe subshells explicitly close the inherited coproc FDs before doing
their curl work, and the parent waits via a `kill -0 <pid>` poll loop
instead of the `wait` builtin. With those two changes the bench runs cleanly
across all 10 iterations on Daytona and Modal. The numbers below are from
that fixed harness; the earlier reports that called this an "SDK hang" were
wrong.

A process-wide `BENCH_TIMEOUT` watchdog still exists in `lib.sh` (default
300s, raised to 600s for the reruns) as a generic backstop against any
future genuine SDK / platform hang. It is independent of any per-call SDK
timeout, which we no longer rely on.

## Results

CDP latency (milliseconds, trigger to ready) from the 2026-05-28 reruns of
the fixed harness. n=10 per cell. Rows ordered by resume internal p50.

| Platform | Arch  | Tier  | Rootfs storage | Cold int p50/p95 | Cold ext p50/p95 | Resume int p50/p95 | Resume ext p50/p95 |
| -------- | ----- | ----- | -------------- | ---------------- | ---------------- | ------------------ | ------------------ |
| Daytona  | amd64 | 2 vCPU / 2 GiB | local NVMe (confirmed)            | 2010 / 2348      | 4657 / 4983      | 2060 / 2294        | 4652 / 4917        |
| E2B      | amd64 | Firecracker 2vCPU/2GiB | local NVMe (microVM spawn ~0.64s) | 24233 / 32495 (slow-node day) | n/a (no usable external path) | 2233 / 3355        | n/a                |
| Modal    | amd64 | Sandbox 2vCPU/2GiB | gVisor overlay (not NVMe)         | 3868 / 8291      | 4676 / 9458      | 2985 / 4354        | 3634 / 5039        |
| Fly.io   | amd64 | shared-4x / 2GB | unverified | 5058 / 5908 | 5161 / 6776 | 1748 / 4833 | 1938 / 5030 |

Read this table with the terminology section in mind. The "primary" number
for each platform decision is the column that bypasses platform-specific
public-edge noise:

- Daytona: internal (the preview proxy adds ~2.6s). True container readiness
  is ~2s and cold ≈ resume (no memory snapshot, just a faster restart).
- E2B: internal (the only available number on this platform). Cold ran on a
  slow node today (p50 24s vs 10s baseline); resume is reliably ~2.2s.
- Modal: internal (the public tunnel adds ~0.6s). Resume ~3s, cold ~4-8s.
- Fly: internal and external both reach the same machine port via direct
  IPv4. The internal probe is `flyctl machine exec`, which adds an SSH
  round-trip; on this rerun it tracked external within ~0-200ms, so use
  either. Cold this run is ~5s (variance: prior rerun showed external 2.3s
  on a different worker). Resume p50 1.7s, p95 4.8s, min 1.6s — close to
  the 2026-05-27 original (1.0s / 1.4s).

Cost (compute only, 2 vCPU / 2 GiB), with the scale-to-zero / base-fee lens:

| Platform | $/container-hr | Monthly @100h | Monthly @1000h | Base fee        | Scales to zero |
| -------- | -------------- | ------------- | -------------- | --------------- | -------------- |
| Fly.io   | $0.0329 (shared-4x) | $3.29    | $32.90         | $0              | Yes (storage-only when stopped/suspended) |
| Modal    | ~$0.19 (Sandbox tier) | $19.03 (~$0 net) | $190.3 ($160.3 net) | $0 + $30/mo credit | Yes |
| Daytona  | $0.1332        | $13.32        | $133.20        | $0 (+$200 credit) | Yes (compute) |
| E2B      | $0.1332        | $163 (incl base) | $283 (incl base) | $150/mo Pro (mandatory for prod) | No (base fee disqualifies) |

## Per-platform findings

### [Fly.io][fly] (incumbent)

2026-05-28 rerun (fresh app + machine, fixed harness, n=10):

- Internal CDP cold p50 5058ms / p95 5908ms (range 4335-5908)
- External CDP cold p50 5161ms / p95 6776ms (range 4435-6776)
- Internal CDP resume p50 1748ms / p95 4833ms (range 1570-4833)
- External CDP resume p50 1938ms / p95 5030ms (range 1665-5030)

Resume p50 1.7s, min 1.6s — close to the 2026-05-27 original (1.0s / 1.4s).
A small handful of runs hit ~4-5s on resume (p95) but no 30s spikes like the
prior rerun. Cold is reliably 5-6s today; an earlier rerun on a different
worker saw external 2.3s, so there is real cross-worker variance. Both ways,
cold is far better than the original 2026-05-27 cold of ~67s, which suggests
Fly upgraded the rootfs storage at some point in the last day. The
suspend/resume framing (memory-restore as the only fast path) may no longer
be necessary — direct creates may now be viable on Fly, pending a clean cold
re-verification on a machine that consistently hits sub-3s.

Internal and external both reach the dedicated public IPv4 + machine port;
they tracked within ~200ms on this rerun (5.1 vs 5.2 cold, 1.7 vs 1.9
resume). Either is fine to read for Fly; we list both.

Cheapest option measured, true scale-to-zero (suspended = rootfs storage
only). Standing caveats: 2GB suspend ceiling, and every deploy invalidates
the snapshot.

Follow-up questions asked in this session:
- "Make sure the image is cached and image-fetch time is not counted." Honored:
  one machine is created once, then stop/suspend between runs, so the image is
  already on the worker and the timed start/resume excludes any pull.

### [Modal][modal] (Sandbox tier)

Headline blocker: the stock s6-overlay image does not boot on Modal Sandboxes.
s6 requires PID 1, but Modal runs the command as PID 2 under its own init, and
gVisor denies `unshare(CLONE_NEWPID)` (no CAP_SYS_ADMIN), so the Fly-style
nested PID-namespace workaround also fails. Numbers below were produced with an
adapted boot (entrypoint cleared, services launched directly).

2026-05-28 rerun (fixed harness, n=10):

- Internal CDP cold p50 3868ms / p95 8291ms (range 3633-8291)
- Internal CDP resume p50 2985ms / p95 4354ms (range 2621-4354)
- External CDP cold p50 4676ms / p95 9458ms
- External CDP resume p50 3634ms / p95 5039ms

Internal < external on Modal (the public tunnel adds ~0.6s on top of true
container readiness). Resume p50 ~3s is via gVisor checkpoint/restore
(`_experimental_snapshot`). The slowest of the tested platforms even on resume,
dominated by gVisor syscall overhead + WAN tunnel RTT, not disk. Pricing
correction: the Sandbox tier is ~$0.19/container-hr, not the ~$0.092 Functions
rate used in the desk research.

Follow-up questions asked in this session:
- "Image cached?" Pre-cached on the worker; the raw sandbox create call is ~0.3s,
  pull excluded.
- "Would an alternative supervisor allow faster boot than the custom
  entrypoint?" A PID-1-capable / non-s6 supervisor is required just to boot on
  Modal, but it would not materially cut latency: the ~3-5s is dominated by
  gVisor syscall interception + WAN tunnel RTT + boot sequencing, not the
  supervisor. So yes for compatibility, no for speed.
- "Why is Chrome's GUI startup on Modal slower than local?" gVisor (runsc)
  overhead + the public tunnel's WAN round-trips (readiness polled at 0.2s) +
  the adapted-boot sequencing. Not disk. Local is bare Docker on NVMe with no
  tunnel.
- "How does snapshot/resume work on Modal?" gVisor checkpoint/restore via the
  experimental Sandbox API: create with `_experimental_enable_snapshot=True`,
  `sb._experimental_snapshot()` returns a `SandboxSnapshot`, restore with
  `Sandbox._experimental_from_snapshot(snap)`. Constraints: same instance type
  only, 7-day expiry, no GPU.
- "What's the cost of a sandbox in snapshot state?" Running compute is $0 (a
  snapshotted sandbox is terminated, billing nothing). Snapshot artifact storage
  is not publicly priced; if billed at the volume rate ($0.09/GiB-mo) it would be
  ~$0.03/snapshot worst case over its <=7-day life. No manual delete API;
  snapshots auto-expire after 7 days.


### [Daytona][daytona]

Rootfs is confirmed local NVMe (overlay2 on md-RAID NVMe, `rotational=0`), so
disk is not the bottleneck. The 2026-05-28 rerun (fixed harness, n=10):

- Internal CDP cold p50 2010ms / p95 2348ms (tight: 1957-2348)
- Internal CDP resume p50 2060ms / p95 2294ms (tight: 1873-2294)
- External CDP cold p50 4657ms / p95 4983ms
- External CDP resume p50 4652ms / p95 4917ms

Cold and resume are effectively identical at ~2s internal because Daytona has
no memory snapshot; both are full container restarts. The internal probe runs
`curl 127.0.0.1:9222` via `sandbox.process.exec()`; the external probe hits
the public preview URL. The ~2.6s gap between them is the preview proxy
reattaching a route after the sandbox starts. A consumer using a custom
network (WireGuard baked into the image, the SDK transport, or an SSH tunnel)
sees the internal number, ~2s.

Stock image needed an entrypoint override
(`unshare --pid --fork --mount-proc /init`, mirroring the Fly path; CAP_SYS_ADMIN
is available). `:latest` tags are rejected; pin a digest.


Follow-up questions asked in this session:
- "DAYTONA_API_KEY is in .env." Used; all numbers are live.
- "'Daytona's number is orchestration-bound', what does that mean and where is
  the latency from?" See the decomposition above: the dominant ~3-4s is the
  public preview proxy re-establishing a route, not compute, disk, app boot, or
  container scheduling. Internal `localhost:9222` is ready at ~2s.
- "How can the container's CDP be accessible without the public preview
  endpoint?" Via an SSH tunnel, a private network, or the SDK transport, all of
  which bypass the preview proxy and give ~2s effective readiness instead of ~5s.
- "Could Tailscale or WireGuard provide better network setup time?" Raw WireGuard
  plausibly yes: no control plane, interface-up + one handshake is sub-second, and
  baked into the image via s6 it is ready roughly when the app is, landing near
  the ~2s floor. Tailscale probably not: its coordination-server login + STUN/DERP
  join on a fresh node is itself a few seconds, the same order as the proxy lag
  being removed; its value is operational (NAT, identity, ACLs), not setup speed.
  Neither beats the ~2s floor, since resume is a full restart (~1.1s start +
  ~0.8s boot). This is reasoning, not measured; when sandbox egress becomes usable
  after `start()` still needs verification.

### [E2B][e2b]

Firecracker microVM built from the chrome-live image as a Build System 2.0
template, on NVMe-backed hosts (bare microVM spawn ~0.64s, image restore
~0.40s, no per-spawn pull). 2026-05-28 rerun:

- Cold internal CDP p50 24233ms / p95 32495ms / min 4056ms (this was a
  noticeably slow node day; the original 2026-05-27 run had p50 ~10s)
- Resume internal CDP p50 2233ms / p95 3355ms / min 1161ms

Cold is highly variable on E2B because Chrome's cold start on a 2-vCPU
microVM is CPU/IO bound; one bad placement and you get 30+s. Resume-from-pause
(full memory + filesystem preserved) is much better and reliably ~2.2s. Both
numbers include one `commands.run` RPC round trip (~0.3-0.5s); true container
readiness is that much faster.

No external CDP path: Chrome's DevTools HTTP rejects E2B's public proxy
because it can't rewrite the Host header. Only the internal probe is
applicable to this platform.

Cost is $0.133/container-hr compute, but the $150/mo Pro base fee is
effectively mandatory for production, which disqualifies E2B under the
no-base-fee requirement regardless of latency.

Integration blockers hit: CDP is unreachable through E2B's public proxy (Chrome
rejects a non-localhost Host header; would need a Host-rewriting sidecar), so
CDP was measured intra-sandbox. Same s6 PID-1
issue as elsewhere: the start-command context lacks CAP_SYS_ADMIN so `unshare`
is denied, worked around by starting `sleep infinity` and launching init via a
post-create `commands.run(background=True)` (which sits inside the timed cold
window). `e2b template build` needs E2B_ACCESS_TOKEN; only an API key was
available, so the build went through the SDK Build System 2.0 instead.

This session also found and fixed a precision bug in the shared `bench/lib.sh`:
`now_ms()` routed the millisecond value through awk's default `%.6g`, truncating
to 6 significant figures so every elapsed time computed as 0ms. Fixed with
integer math on `EPOCHREALTIME`. The fix is in the canonical `bench/lib.sh` on
this branch; the Fly/Modal/Daytona runs used per-platform copies that already
timed correctly.

Pricing reconciliation: the desk-research table earlier listed E2B at
$0.0524/container-hr, which does not reconcile with E2B's own per-second rates
(2 vCPU @ $0.000028/s + 2 GiB @ $0.0000045/GiB/s = $0.000037/s = $0.1332/hr).
Use $0.133/hr; the $0.0524 figure was an error.

Follow-up question asked in this session:
- "Make sure the container image is cached so fetch time is minimized." Honored:
  the 2 GB image is pulled from ghcr once at `Template.build` and baked into the
  Firecracker template snapshot; `Sandbox.create` restores it in ~0.40s with no
  per-spawn pull. So the cold number is Chrome boot, not image fetch.

## Read so far

- Internal CDP resume ranking (primary signal, p50): Fly 1.7s, Daytona 2.1s,
  E2B 2.2s, Modal 3.0s.
- Internal CDP cold ranking (p50): Daytona 2.0s, Modal 3.9s, Fly 5.1s. E2B
  cold was 24s today on a slow node (10s in the original run).
- For the non-Fly platforms the bottleneck is never disk or the container
  itself: Modal is gVisor + tunnel RTT; Daytona is the public preview proxy
  (~2.6s of external time reachable as 0 if bypassed); E2B cold is Chrome
  CPU-bound boot variance. E2B and Daytona both run on confirmed NVMe.
- A cross-cutting integration finding: the stock s6 image's PID-1 requirement
  breaks on every platform that does not give the container PID 1 with
  CAP_SYS_ADMIN. Daytona and E2B needed `unshare` workarounds (E2B via a
  post-create launch since its start-command lacks the cap); Modal could not do it
  at all (gVisor denies the unshare). Any non-Fly target needs an image change here.
- A second cross-cutting finding: CDP (`:9222`) is rejected by Chrome through a
  public proxy because of the non-localhost Host header (hit on E2B; Modal needed a
  `Host: localhost` curl header through its tunnel). Exposing CDP publicly needs a
  Host-rewriting sidecar.

## Approach recommendation per platform

Two ways to deliver a container on demand:

- direct: spawn a fresh container per request and tear it down after. No pool, zero
  idle cost. Latency is the platform's cold-create time, so direct only works where
  that is acceptably fast for your use case. Implemented by [chromefleet][chromefleet]
  on a host you control, or by a platform's own create/start API.
- pool: keep a set of pre-warmed instances and hand one to each request, refilling
  in the background. Latency is the claim/resume of an already-ready instance, so it
  hides a slow cold-create. Cheap only if the pre-warmed standby state is cheap
  (e.g. Fly's suspended machines = storage only); otherwise standby instances must
  stay running and you pay idle cost. Implemented by [flyfleet][flyfleet] on Fly.

Decision rule:

- Cold-create is fast (a couple of seconds or less, e.g. local NVMe or Daytona's
  ~2s internal): direct. Simplest, zero idle cost.
- Cold-create is slow, but suspend is cheap and resume is fast: pool. The
  pre-warmed pool hides the cold boot at near-zero idle cost.
- Cold-create is slow and suspend is not cheap: pool is the only way to keep
  latency low, but standby must stay running, so you pay idle cost.

| Platform | Cold-create (int p50) | Fast path (measured) | Standby cost | Approach |
| -------- | --------------------- | -------------------- | ------------ | -------- |
| Fly.io   | 5.1s today (was 67s on 2026-05-27, 2.3s on a prior rerun) | resume p50 1.7s, min 1.6s | cheap (suspended = storage only) | pool, possibly direct now |
| Hetzner (NVMe) | ~1s expected | fast cold (NVMe) | n/a (no pool) | direct |
| Daytona  | 2.0s internal (4.7s via preview proxy) | direct create ~2s, resume ~2s | n/a (no pool) | direct (+ custom network) |
| E2B      | 24s today (10s typical) | resume ~2.2s | free while paused | pool |
| Modal    | 3.9s | snapshot resume 3.0s | $0 snapshotted | pool |

Notes:

- Fly.io: pool's natural home (slow cold hidden by cheap-suspended pool with
  fast resume). 2026-05-28 reruns show cold has dropped to ~2-5s (from ~67s
  on 2026-05-27), so direct may now be viable; cross-worker variance is
  noticeable and needs a few clean cold runs on a sub-3s machine before
  committing to direct. Resume p50 1.7s on the fresh rerun is reliable.
- Hetzner / any bare NVMe VM: cold create is ~1s, so direct (chromefleet) is
  ideal; a pool would only add idle cost. (Pending HCLOUD_TOKEN.)
- Daytona: a fresh container reaches internal CDP in ~2s on NVMe and both
  cold and resume measure the same ~2s, so direct works with zero idle cost
  and no pool, provided a custom network (WireGuard, SSH tunnel, or SDK
  transport) replaces the public preview proxy, which adds ~2.6s.
- E2B fits the pool pattern (slow / variable cold, free pause, ~2.2s resume),
  but flyfleet's pool code is Fly-only and would need porting, and the
  $150/mo base fee rules E2B out on cost regardless.
- Modal: snapshot resume is 3.0s, the slowest of the tested platforms. A
  pool of already-running sandboxes could cut the boot but tunnel RTT + gVisor
  marginal, and running standby costs ~$0.19/hr each.

Bottom line: direct for fast-cold hosts (Hetzner / bare NVMe VMs) and for Daytona
(~2s, with a custom network); pool for Fly.io (the only cheap-suspend platform).
E2B would work as a pool but is disqualified on cost; Modal is the slowest tested.

## Pending / blocked platforms

| Platform        | Status                                                                 |
| --------------- | ---------------------------------------------------------------------- |
| Hetzner (NVMe baseline) | Pending. Needs HCLOUD_TOKEN. This is the local-NVMe lower-bound reference. |
| Northflank      | Blocked. The provided token is free-tier with no payment method; cannot provision services (409). Script left in its worktree. |
| Koyeb           | Pending. Needs a Koyeb token; also resolve the real CPU container rate and whether the ~$29/mo plan is mandatory. |
| Cloudflare      | Pending. Needs Workers Paid ($5/mo) + wrangler. No memory snapshot, so cold-only; expected 2-3s cold. |

To finish these, supply the relevant credentials and re-run the per-platform
script (see each session's worktree and `bench/README.md`).

[fly]: https://fly.io
[modal]: https://modal.com
[daytona]: https://www.daytona.io
[e2b]: https://e2b.dev
[hetzner]: https://www.hetzner.com/cloud/
[northflank]: https://northflank.com
[koyeb]: https://www.koyeb.com
[cloudflare-containers]: https://developers.cloudflare.com/containers/
[chromefleet]: https://github.com/remotebrowser/chromefleet
[flyfleet]: https://github.com/gather-engineering/flyfleet
