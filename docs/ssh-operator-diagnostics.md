# Operator SSH transport diagnostics

This runbook covers the authorized workstation -> Control (`s2`) -> Runner
(`codex-runner`) route. It does not change the Runner protocol, global SSH
configuration, login credentials, or service lifecycle.

## Observed route and limits

The workstation's `s2` alias uses `assh` as its ProxyCommand. On September 20,
2026, `assh connect --dry-run --no-rewrite s2` showed a second SSH connection
through gateway `s1`. The workstation defaults included `ControlMaster auto`,
`ControlPersist 600`, and `ServerAliveInterval 60`.

Consequently, `ssh -o ControlPath=none s2 ...` disables only the outer connection's
control socket. It does not establish that the gateway connection is independent.
The dry-run command can exit nonzero while describing its route; this alone is
not a connectivity failure. Its output includes infrastructure addresses and
should not be pasted into Issues or public logs.

The bounded read-only checks in this session produced:

| Check | Observed result |
|---|---|
| Control -> Runner, five sequential short probes | All exit 0, 0.63-0.87 seconds |
| Workstation -> Control, 25-second quiet command, defaults | Exit 0, expected marker |
| Same quiet command, outer multiplexing disabled | Exit 0, expected marker; inner multiplexing warning observed |
| Workstation -> Control, 40-second quiet command, defaults | Exit 0, expected marker, 44.10 seconds total |
| Same route and quiet command, multiplexing disabled on both SSH layers | Exit 0, expected marker, 44.11 seconds total |
| Control and Runner sshd | Active; ClientAliveInterval 0, ClientAliveCountMax 3, MaxSessions 10, MaxStartups 10:30:100 |
| Available SSH error summaries, September 19 17:00 through September 20 00:00 UTC | No matching disconnect, broken-pipe, timeout, reset, or preauth error found |

These samples did not reproduce the earlier `exit 255` / `Broken pipe`. They do
not prove continuous availability or identify a root cause. The available journal
summary is not proof that every earlier session was retained or logged at the
required level. No global SSH settings were changed, services restarted, or
existing master connections terminated.

## Collect evidence on recurrence

On September 22, an uncompressed workstation-to-Control release upload failed
with `lost connection`. The remote file contained 783,360 of the expected
4,003,840 bytes. After checking the incomplete file's size and digest, a
command-scoped compressed SCP transfer completed in 9.85 seconds; its remote
SHA-256 matched the local archive. The protected client trace was retained.
A bounded Control sshd sample contained a preauthentication connection closure,
but it could not be bound to the failed transfer. A separate remote
`journalctl` query itself exceeded a ten-second subprocess timeout while the
same SSH session successfully returned database metadata. Neither observation
establishes the failed hop or root cause. Compression is a successful transfer
workaround in this sample, not a demonstrated SSH repair. Release execution used
a named systemd job and durable transaction receipt so transport loss would not
require blindly repeating a deployment.
Later, a read-only status connection emitted `muxclient: master hello exchange
failed` before successfully returning the requested unit states with exit 0.
That identifies another multiplexing-related symptom, not the cause of the
earlier incomplete transfer.

A follow-up on September 22 reproduced one failure at
`2026-09-22T07:18:53.715807+00:00`: a 4 MiB stdin upload to a read-only remote
SHA-256 calculator over the default Control connection exited 255 after 0.170
seconds, with no stderr and no expected checksum. No remote files were written.
The remaining bounded comparisons were:

| Check | Result |
|---|---|
| Same-size data through Control to Runner | Hash matched, exit 0, 13.987 seconds |
| Control upload with multiplexing disabled on both client/gateway layers | Hash matched, exit 0, 12.993 seconds |
| Two additional default Control uploads with client debug capture | Hashes matched, exit 0, 9.381 and 10.424 seconds |
| Default Control connection, 40 seconds quiet | Marker matched, exit 0, 40.345 seconds |
| Both-layer nonmultiplexed connection, 40 seconds quiet | Marker matched, exit 0, 42.695 seconds |

The client reported an existing Control master after the failed upload. Debug
traces from the successful retries confirmed reuse of that master. Bounded
Control/Runner sshd samples returned no reset, broken-pipe, key-exchange, or
session-limit error; their sparse records do not exclude a gateway or client
failure. No gateway server journal was available in this investigation. These
results establish an intermittent failure on the workstation-to-Control route,
but do not isolate multiplexing, the gateway, or the network as its cause.
Keep using durable remote job receipts and verified transfer hashes. A
command-scoped comparison that disables both layers is a diagnostic option;
there is insufficient evidence to change global SSH configuration.

1. Record UTC start/end, which hop was running, command exit status, expected
   marker presence, and whether the remote operation has its own durable receipt.
   Do not infer that a remote write failed merely because its SSH connection ended.
2. Test the hops independently with bounded read-only commands. For example:

   ```bash
   ssh -o BatchMode=yes -o ConnectTimeout=10 s2 'printf control_probe_ok'
   ssh -o BatchMode=yes -o ConnectTimeout=10 s2 \
     'ssh -o BatchMode=yes -o ConnectTimeout=10 codex-runner "printf runner_probe_ok"'
   ```

   A failure of the nested command still requires checking which SSH process
   emitted the failure. Add a bounded quiet interval only when investigating idle
   connections; a few successful short probes cannot exclude idle disconnects.
3. Compare defaults with command-scoped multiplexing disabled on **both** layers,
   using the exact locally verified gateway route. Keep infrastructure addresses
   in the protected local diagnostic session. Do not change the global config or
   close unrelated control sockets to perform the comparison.
4. Inspect the two servers' bounded sshd journals and their effective keepalive,
   startup, and session-limit settings for the same UTC interval. Check journal
   retention before treating missing events as evidence. If debug logging is
   necessary, store it locally with mode 0600 and report only selected transport
   metadata; never publish the full SSH trace.
5. For long operator jobs, use the existing identity-bound release/maintenance
   workflow with a durable remote receipt. If the transport fails, read back that
   exact receipt and unit state before deciding whether another invocation is
   permitted. Do not convert an ambiguous write into an automatic retry.

Command-scoped keepalives and durable receipts reduce uncertainty during manual
acceptance; they are not evidence that the intermittent transport cause is fixed.
