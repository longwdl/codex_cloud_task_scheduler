# Audited Runner egress proxy contract

The intended stage puts every new outbound connection created by the `codex-runner` account through
one root-owned HTTP CONNECT proxy. It applies to the current direct Codex process and later to
rootless Docker traffic. Proxy environment variables are compatibility inputs only: untrusted code
can delete them, so the host firewall must independently reject direct egress from the Runner UID.

The selected implementation is the Ubuntu 24.04 security-supported Squid package. Pin the exact
candidate version recorded during the guarded installation. Squid runs as the distribution's
dedicated `proxy` account, never as `codex-runner`, and does not terminate TLS. The repository
contains a complete restricted configuration and a hardened service unit, but they remain offline
artifacts until the native Squid parser and credential-free network probes pass on the target host.

## Policy

The proxy accepts only HTTP `CONNECT` requests to TCP port 443. The root-owned domain file may be
expanded for normal development, but it remains an allowlist. Prefer exact lowercase FQDNs. A
leading dot authorizes every subdomain and therefore requires an explicit supply-chain review; a
top-level or public-suffix entry is forbidden. IP literals, `*`, URL schemes, paths, credentials,
and arbitrary ports are forbidden.

The service runs the repository's protected policy validator before Squid starts. Exact rules need
at least two DNS labels; suffix rules need at least three labels after the leading dot. This is a
deliberately conservative substitute for bundling a mutable public-suffix database. Site-specific
blocked entries must be canonical `/32` or `/128` host addresses, which prevents an accidental
catch-all route while still denying public Control Host or provider-service addresses.

Regardless of the domain allowlist, the proxy and firewall permanently reject:

- host loopback, RFC 1918, IPv4 link-local, CGNAT, multicast, reserved and benchmark networks;
- IPv6 loopback, ULA, link-local, multicast and mapped IPv4 destinations;
- every cloud metadata/service endpoint, including endpoints outside the common link-local range;
- the Runner host, Control Host, Docker/RootlessKit gateways, other containers and local services;
- Docker APIs, SSH, DNS, plain HTTP, QUIC and all non-443 destination ports.

The site-specific blocked-address file must contain the current host, Control Host, VPC DNS/service,
metadata and container-network addresses. An empty or missing file is a deployment failure. Squid
must evaluate the resolved destination-address ACL before the domain allow rule, so an approved
name that resolves to a prohibited address is still denied.

Do not initially authorize GitHub, Slack, Docker registries or package registries. The Runner gets
source as a Git bundle and has no GitHub or Slack credentials. Later additions are grouped by
purpose, reviewed as code, tested one at a time and recorded with owner, reason and expiry/review
date. Image pulls occur under a separate administrator-controlled installation identity; task
containers retain `--pull=never`.

OpenAI does not publish a complete Codex CLI egress-domain contract. The current s3 bootstrap file,
`allowed-domains.s3`, therefore contains the broad set of exact OpenAI/ChatGPT hostnames embedded in
the installed Codex 0.147.0 binary. It is version-specific evidence rather than an upstream
compatibility guarantee. Keep a denied request denied until its metadata-only audit record has been
reviewed; never add a wildcard merely to make a Turn pass.

## Audit boundary

`/var/log/squid/codex-egress-access.log` records only timestamp, source address, Squid/HTTP outcome,
request and response byte counts, elapsed time, method, destination hostname and destination port.
It must not record URL paths, request or response headers, proxy credentials, authorization tokens,
Prompt, TLS content or full Codex output. Plain HTTP is denied before use, and TLS remains end to end.

Keep 30 daily compressed rotations, mode `0640`, writable only by the proxy service and readable by
root for audit. The proxy log is operational evidence, not a replay source. Correlate it to the
globally single active Turn by timestamp and durable Turn ID in the Runner/Dispatcher stores; never
place a WorkItem ID, Turn ID or Prompt in the proxy URL or authentication fields.

## Guarded activation

Before any change, keep the Dispatcher timer disabled and the backup timer active. Require a fresh
mode-`0600` SQLite Online Backup with `integrity_check=ok`, read-only preflight `idle`, no active
local/remote Turn, no exact Runner/Codex process and an acquirable Runner lock. Preserve checksummed
copies of the effective UFW, nftables, iptables and ip6tables rules plus the existing Runner config.
Also prove that the Runner UID owns no pre-existing outbound socket; otherwise the established-flow
exception could preserve an unreviewed connection after activation.

Mask `squid.service` before installing the exact pinned `squid` package; package installation must
never expose its distribution default listener. Prove the distribution unit remains masked and no
port appeared before installing the repository configuration root-owned and non-writable. Create
non-secret domain and site-block files, validate with Squid's native parse-only command, then start
only `codex-egress-proxy.service`. It must listen only on loopback and run as the `proxy` UID. The
`codex-runner` config may receive only the canonical credential-free endpoint
`http://127.0.0.1:3128` after the proxy is healthy.

Ubuntu's package-provided AppArmor profile must remain loaded in enforce mode. The custom
configuration, policy, PID and log files deliberately stay below the distribution-standard
`/etc/squid`, `/run/squid` and `/var/log/squid` trees. Validate both the service unit and the loaded
profile before start, then prove that startup and a denied probe produce no AppArmor denial. Do not
disable the profile, switch it to complain mode, or add a broad path grant. If a release-specific
profile does not cover an exact custom filename, stop and review a narrow local include before
activation.

Add a dedicated nftables OUTPUT chain without flushing or replacing UFW-owned tables. It allows
established traffic, allows new `codex-runner` TCP connections only to the exact local proxy socket,
and rejects every other new connection from that UID. The proxy UID may resolve DNS through only
the current trusted host resolver and may make public TCP/443 connections, but its destinations are
still constrained by Squid ACLs. IPv4 and IPv6 require equivalent rules. Do not rely on UFW default
policies, DNS filtering, security groups or proxy variables as the enforcement boundary.
The loopback proxy socket must also reject new connections from every local UID except
`codex-runner`; source address `127.0.0.1` alone does not identify the caller and is not an audit
identity.

Install the reviewed host-specific rules as `/etc/codex-egress-firewall.nft`, owned by root with
mode `0600`. Its batch must begin by destroying only the dedicated `inet codex_egress` table and
recreating it, so start and reload replace that table atomically without touching any other table.
Validate it with `nft -c -f` before enabling `codex-egress-firewall.service`. The proxy unit requires
that service and cannot start when the ruleset fails to load.
The current s3 candidate is `codex-egress-firewall.s3.nft`; its numeric UIDs, resolver and public
Runner/Control Host addresses are deployment assertions, not portable defaults, and must match a
fresh read-only baseline before every installation.

Credential-free probes must prove:

1. an allowlisted public TLS endpoint succeeds only through the proxy;
2. direct public TCP/443, UDP/443 and DNS from the Runner UID fail;
3. loopback services other than the proxy, private/ULA/link-local/CGNAT destinations, metadata,
   Control Host, Docker APIs and a second test container fail;
4. an allowlisted hostname resolving to a prohibited address fails;
5. a disallowed public hostname fails and produces one metadata-only audit record;
6. proxy stop or an unreadable/malformed allowlist fails closed;
7. inbound fixed-command SSH and read-only STATUS remain usable;
8. another unprivileged local UID cannot connect to the proxy;
9. log rotation preserves ownership and the proxy continues auditing afterward.

For the later rootless container stage, determine the proxy's container-visible host endpoint with
a credential-free image. Store that canonical endpoint only in the protected Docker runtime config.
The host firewall must prove that clearing all proxy variables still cannot create direct egress.
Do not activate a Codex container merely because `HTTPS_PROXY` works.

## Rollback

Keep the Dispatcher timer disabled. Remove only the dedicated Runner OUTPUT jump/rules, restore the
previous root-owned Runner config, stop and disable `codex-egress-proxy.service`, and verify the
effective UFW/nftables/iptables rules match their saved checksums. Do not flush firewall tables,
restart SSH, delete WorkItems/sessions/Turn records, or remove audit logs. Re-run read-only STATUS
reconciliation before returning to the previous direct execution path.
