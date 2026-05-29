# chrome-live startup latency on Modal (Sandbox tier)

Run: re-measured 2026-05-28 PDT with the cleaned-up harness (CDP-only result,
noVNC informational). See `run-cold.log` / `run-resume.log` headers for stamps.
Earlier 2026-05-27 numbers below have been replaced by the rerun.

## Platform / environment

| Field            | Value                                                          |
| ---------------- | -------------------------------------------------------------- |
| Platform         | Modal Sandbox (modal client 1.4.3)                             |
| Tier             | Sandbox (interactive); ~3x the per-core rate of Functions      |
| Arch             | x86_64 (amd64)                                                 |
| Requested size   | cpu=2.0 (2 vCPU = 1 physical core), memory=2048 MiB (2 GiB)    |
| Observed         | nproc=2 inside the sandbox                                     |
| Isolation        | gVisor (runsc); no CAP_SYS_ADMIN, unshare(CLONE_NEWPID) denied |
| Storage / rootfs | gVisor overlay (Gofer-proxied); not raw NVMe to the guest      |
| Ports            | noVNC :80 and CDP :9222 exposed via Modal encrypted tunnels    |

## Headline finding: the stock image does NOT boot on Modal Sandboxes

The image boots via s6-overlay (ENTRYPOINT `start-init.sh` -> `/init`). s6's
`s6-overlay-suexec` aborts with `fatal: can only run as pid 1`. On Modal:

- the sandbox command runs as PID 2 under Modal's own `dumb-init` (PID 1); and
- gVisor denies `unshare(CLONE_NEWPID)` ("Operation not permitted", no CAP_SYS_ADMIN),
  so the Fly-style nested-PID-namespace workaround in `start-init.sh` also fails.

Modal also *prepends the image ENTRYPOINT* to any sandbox command, so even
`sh -c ...` hit the s6 PID-1 error until the entrypoint was cleared with
`Image.from_registry(...).entrypoint([])`.

To produce latency numbers we run an **adapted boot** (documented, Modal-specific):
clear the entrypoint and launch the same services directly, mirroring the s6 run
scripts: cont-init (hosts/adblock filter) -> Xvnc + tinyproxy -> Chrome
(`--remote-debugging-port=9221`) -> socat CDP proxy (:9222<-:9221); websockify
serves noVNC on :80. XFCE and browser-trace are omitted (neither is on the CDP or
noVNC-HTTP readiness path). These numbers therefore reflect Chrome + X + noVNC
startup under gVisor, not the stock s6 supervision tree.

CDP readiness curl must send `-H "Host: localhost"`: Chrome rejects `/json/*` when
the Host header is not localhost/IP, and the tunnel's hostname would otherwise be
forwarded. TLS SNI still routes by the URL hostname.

## Latency (>=10 runs each; wall-clock from trigger to ready signal)

Includes Modal tunnel WAN RTT from the bench host (polled at 0.2 s granularity).
Raw Modal sandbox *create* call alone was ~0.3 s; the rest is boot/restore + reconnect.

### MODE=cold (fresh sandbox per run, image pre-cached)

| Signal           | n  | p50    | p95    | min    | max    |
| ---------------- | -- | ------ | ------ | ------ | ------ |
| CDP (result)     | 10 | 4614ms | 6127ms | 3718ms | 6127ms |
| noVNC (informational) | 10 | ~5.3s | ~6.7s | ~4.5s | ~6.7s |

Rerun is tighter than the original 2026-05-27 run (which had a single ~19s
placement outlier).

### MODE=resume (memory-snapshot restore)

Sandbox memory snapshots ARE available for Sandboxes (experimental):
`_experimental_enable_snapshot=True` -> `sb._experimental_snapshot()` ->
`Sandbox._experimental_from_snapshot(snap)`. Caveats: same instance type only,
7-day expiry, no GPU. Snapshot of a running-Chrome sandbox succeeded.

| Signal           | n  | p50    | p95    | min    | max    |
| ---------------- | -- | ------ | ------ | ------ | ------ |
| CDP (result)     | 10 | 3401ms | 5142ms | 2692ms | 5142ms |
| noVNC (informational) | 10 | ~4.0s | ~5.7s | ~3.4s | ~5.7s |

Resume beats cold (CDP p50 3.4 s vs 4.6 s) but does not reach the <~1.5 s target;
restore still re-establishes tunnels and reconnects to the restored Chrome.

Acceptance (p50 <= ~1.5 s): NOT met for either mode. Local reference was ~0.85-1.37 s;
Modal is slower here mostly due to gVisor + tunnel RTT + adapted-boot sequencing.

## Pricing (Sandbox tier, confirmed from modal.com/pricing)

Rates: CPU $0.00003942 / core-s (1 core = 2 vCPU; min 0.125 core), mem
$0.00000672 / GiB-s. Per-second billing, scales to zero (no charge when not running).

At 2 vCPU (= 1 core) / 2 GiB:

```
CPU: 1 core x $0.00003942 x 3600 = $0.14191 /hr
Mem: 2 GiB x $0.00000672 x 3600 = $0.04838 /hr
Total                            ~ $0.1903 /container-hr
```

| Usage    | Gross    | Net after $30/mo credit |
| -------- | -------- | ----------------------- |
| @100 h   | $19.03   | $0 (within free credit) |
| @1000 h  | $190.3   | $160.3                  |

- Base fee: $0/mo (Starter), $30/mo free credit, no mandatory subscription.
- Scales to zero: yes (terminate -> $0).
- The brief's "~$0.092/container-hr" matches the *Functions* CPU rate
  ($0.0000131/core-s), not Sandbox; the Sandbox tier is ~2x that blended
  (CPU ~3x, memory unchanged). Use ~$0.19/hr for Sandbox planning.
- If Modal bills `cpu=2.0` as 2 physical cores rather than 2 vCPU, CPU doubles to
  ~$0.284/hr (total ~$0.332/hr). Internal evidence (nproc=2) supports the 1-core reading.

## Blockers / caveats

- Stock s6 image incompatible with Modal Sandboxes (PID-1 requirement). A Modal
  deployment would need a non-s6 supervisor or a PID-1-capable runtime. Numbers
  above use the adapted boot, not stock s6.
- `/proc/meminfo` inside the sandbox reports host memory (gVisor passthrough), not
  the 2 GiB limit; ignore it for sizing.
- Latencies include WAN tunnel RTT; a same-region client would be lower.

## Reproduce

```
# token in /Users/bin/dev/chrome-live/.env (MODAL_TOKEN_ID / MODAL_TOKEN_SECRET)
uv venv bench/.venv && uv pip install --python bench/.venv/bin/python modal
MODE=cold   COUNT=10 bash bench/modal.sh
MODE=resume COUNT=10 bash bench/modal.sh
```

Files: `bench/modal.sh` (hooks + token load), `bench/modal_bench.py` (Sandbox driver
daemon, holds the adapted boot), `bench/lib.sh` (timing + percentiles).
