from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOYMENT = ROOT / "deploy" / "runner"
DOCUMENTATION = DEPLOYMENT / "EGRESS.md"
SQUID_CONFIG = DEPLOYMENT / "squid-codex-egress.conf"
SERVICE = DEPLOYMENT / "codex-egress-proxy.service"
FIREWALL_SERVICE = DEPLOYMENT / "codex-egress-firewall.service"
FIREWALL_RULES = DEPLOYMENT / "codex-egress-firewall.s3.nft"
LOGROTATE = DEPLOYMENT / "codex-egress-logrotate"
ALLOWLIST = DEPLOYMENT / "allowed-domains.example"
BLOCKLIST = DEPLOYMENT / "blocked-destinations.example"
S3_ALLOWLIST = DEPLOYMENT / "allowed-domains.s3"
S3_BLOCKLIST = DEPLOYMENT / "blocked-destinations.s3"
RUNNER_EXAMPLE = ROOT / "config" / "runner.example.json"


class RunnerEgressDeploymentTests(unittest.TestCase):
    def test_proxy_is_loopback_only_connect_443_and_fail_closed(self) -> None:
        configuration = SQUID_CONFIG.read_text(encoding="utf-8")
        access_rules = tuple(
            line for line in configuration.splitlines() if line.startswith("http_access ")
        )

        self.assertIn("http_port 127.0.0.1:3128", configuration)
        self.assertIn("acl runner_client src 127.0.0.1/32 ::1/128", configuration)
        self.assertIn("acl connect_method method CONNECT", configuration)
        self.assertIn("acl tls_port port 443", configuration)
        self.assertIn("acl canonical_connect url_regex", configuration)
        self.assertIn("acl hostname_connect url_regex", configuration)
        self.assertIn("http_access deny !runner_client", configuration)
        self.assertIn("http_access deny !connect_method", configuration)
        self.assertIn("http_access deny !tls_port", configuration)
        self.assertIn("http_access deny !canonical_connect", configuration)
        self.assertIn("http_access deny !hostname_connect", configuration)
        self.assertEqual("http_access deny all", access_rules[-1])
        self.assertNotIn("http_port 0.0.0.0", configuration)
        self.assertNotIn("ssl_bump", configuration)
        self.assertNotIn("https_port", configuration)
        self.assertNotIn("::ffff:0:0/96", configuration)
        self.assertNotIn("via off", configuration)

    def test_private_metadata_and_site_blocks_precede_domain_allow(self) -> None:
        configuration = SQUID_CONFIG.read_text(encoding="utf-8")

        for network in (
            "10.0.0.0/8",
            "100.64.0.0/10",
            "127.0.0.0/8",
            "169.254.0.0/16",
            "172.16.0.0/12",
            "192.0.0.0/24",
            "192.168.0.0/16",
            "198.51.100.0/24",
            "203.0.113.0/24",
            "::1/128",
            "64:ff9b::/96",
            "2001:db8::/32",
            "fc00::/7",
            "fe80::/10",
        ):
            self.assertIn(f"acl blocked_destination dst {network}", configuration)
        blocked = configuration.index("http_access deny blocked_destination")
        site_blocked = configuration.index(
            "http_access deny site_blocked_destination"
        )
        allowed = configuration.index("http_access allow approved_domain")
        self.assertLess(blocked, allowed)
        self.assertLess(site_blocked, allowed)
        self.assertIn("codex-egress-blocked-destinations.conf", configuration)

    def test_audit_is_metadata_only_and_rotated_with_protected_mode(self) -> None:
        configuration = SQUID_CONFIG.read_text(encoding="utf-8")
        rotation = LOGROTATE.read_text(encoding="utf-8")
        logformat = next(
            line for line in configuration.splitlines() if line.startswith("logformat ")
        )

        for field in (
            "ts=",
            "src=",
            "result=",
            "in=",
            "out=",
            "elapsed_ms=",
            "method=",
            "dst=",
        ):
            self.assertIn(field, logformat)
        self.assertIn("%>rd:%>rP", logformat)
        self.assertIn("on-error=die", configuration)
        self.assertNotIn("%ru", logformat)
        for prohibited in ("Authorization", "Prompt", "header", "body"):
            self.assertNotIn(prohibited, logformat)
        self.assertIn("rotate 30", rotation)
        self.assertIn("maxsize 100M", rotation)
        self.assertIn("create 0640 proxy proxy", rotation)
        self.assertNotIn("|| true", rotation)

    def test_service_uses_root_coordinator_and_unprivileged_worker(self) -> None:
        service = SERVICE.read_text(encoding="utf-8")
        configuration = SQUID_CONFIG.read_text(encoding="utf-8")

        self.assertNotIn("User=", service)
        self.assertIn("Group=proxy", service)
        self.assertIn("Type=simple", service)
        self.assertNotIn("NotifyAccess", service)
        self.assertNotIn("User=codex-runner", service)
        self.assertIn("cache_effective_user proxy", configuration)
        self.assertIn("pinger_enable off", configuration)
        self.assertIn("-m codex_dispatcher.egress_policy", service)
        self.assertIn("-k parse", service)
        self.assertGreaterEqual(
            service.count("ExecReload=/usr/bin/python3 -m codex_dispatcher.egress_policy"),
            1,
        )
        for directive in (
            "NoNewPrivileges=true",
            "PrivateDevices=true",
            "ProtectHome=true",
            "ProtectSystem=strict",
            "RestrictNamespaces=true",
            "MemoryDenyWriteExecute=true",
        ):
            self.assertIn(directive, service)
        self.assertIn("CapabilityBoundingSet=CAP_SETGID CAP_SETUID", service)
        for capability in ("CAP_NET_ADMIN", "CAP_NET_RAW", "CAP_NET_BIND_SERVICE"):
            self.assertNotIn(capability, service)
        self.assertIn("AmbientCapabilities=", service)
        for standard_tree in ("/etc/squid/", "/run/squid", "/var/log/squid"):
            self.assertIn(standard_tree, service)
        self.assertNotIn("/etc/codex-egress-proxy", service)
        self.assertIn("Requires=codex-egress-firewall.service", service)

    def test_firewall_service_loads_only_the_dedicated_protected_ruleset(self) -> None:
        service = FIREWALL_SERVICE.read_text(encoding="utf-8")

        self.assertIn("ExecStart=/usr/sbin/nft -f /etc/codex-egress-firewall.nft", service)
        self.assertIn("ExecReload=/usr/sbin/nft -f /etc/codex-egress-firewall.nft", service)
        self.assertIn("ExecStop=/usr/sbin/nft delete table inet codex_egress", service)
        self.assertIn("RemainAfterExit=yes", service)
        self.assertIn("CapabilityBoundingSet=CAP_NET_ADMIN", service)
        self.assertIn("ProtectSystem=strict", service)
        self.assertNotIn("flush ruleset", service)
        self.assertNotIn("Condition", service)

    def test_s3_rules_force_runner_and_proxy_through_narrow_paths(self) -> None:
        rules = FIREWALL_RULES.read_text(encoding="utf-8")

        self.assertTrue(rules.startswith("# Host-specific production rules"))
        self.assertIn("destroy table inet codex_egress", rules)
        self.assertIn("table inet codex_egress", rules)
        self.assertEqual(1, rules.count("chain output"))
        self.assertNotIn("chain input", rules)
        self.assertNotIn("chain forward", rules)
        self.assertNotIn("flush ruleset", rules)
        runner_allow = rules.index(
            "meta skuid 1002 ip daddr 127.0.0.1 tcp dport 3128 counter accept"
        )
        runner_reject = rules.index("meta skuid 1002 counter reject")
        self.assertLess(runner_allow, runner_reject)
        self.assertIn(
            "meta skuid 13 ip daddr 127.0.0.53 udp dport 53 counter accept", rules
        )
        self.assertIn("meta skuid 13 ip daddr @blocked_ipv4 counter reject", rules)
        self.assertIn(
            "meta skuid 13 ip protocol tcp tcp dport 443 counter accept", rules
        )
        self.assertIn(
            "meta skuid 13 ip6 daddr 2000::/3 tcp dport 443 counter accept", rules
        )
        self.assertIn("meta skuid 13 counter reject", rules)
        self.assertIn(
            "ip daddr 127.0.0.1 tcp dport 3128 meta skuid != 1002 counter reject",
            rules,
        )

    def test_example_policy_cannot_accidentally_enable_wildcard_or_url(self) -> None:
        allowlist = tuple(
            line.strip()
            for line in ALLOWLIST.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
        blocklist = tuple(
            line.strip()
            for line in BLOCKLIST.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )

        self.assertEqual(("example.invalid",), allowlist)
        self.assertFalse(
            any("*" in item or "://" in item or ":" in item for item in allowlist)
        )
        self.assertTrue(blocklist)
        self.assertTrue(
            all(re.fullmatch(r"[0-9a-fA-F:.]+/[0-9]+", item) for item in blocklist)
        )

    def test_s3_policy_is_exact_audited_and_contains_no_control_plane_service(self) -> None:
        allowlist = tuple(
            line.strip()
            for line in S3_ALLOWLIST.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
        blocklist = tuple(
            line.strip()
            for line in S3_BLOCKLIST.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )

        self.assertGreaterEqual(len(allowlist), 4)
        self.assertEqual(len(allowlist), len(set(allowlist)))
        self.assertTrue(all(not item.startswith(".") for item in allowlist))
        self.assertTrue(all("*" not in item and "://" not in item for item in allowlist))
        self.assertFalse(
            any(
                marker in item
                for item in allowlist
                for marker in ("github", "slack", "docker", "npmjs", "pypi")
            )
        )
        self.assertGreaterEqual(len(blocklist), 4)
        self.assertTrue(all(item.endswith("/32") for item in blocklist))

    def test_documentation_requires_firewall_enforcement_and_safe_rollback(self) -> None:
        documentation = DOCUMENTATION.read_text(encoding="utf-8")
        runner_example = RUNNER_EXAMPLE.read_text(encoding="utf-8")

        self.assertIn("untrusted code", documentation)
        self.assertIn("can delete them", documentation)
        self.assertIn("must independently reject direct egress", documentation)
        self.assertIn("every local UID except\n`codex-runner`", documentation)
        self.assertIn("nft -c -f", documentation)
        self.assertIn("cannot start when the ruleset fails to load", documentation)
        self.assertIn("It must not record URL paths", documentation)
        self.assertIn("Do not flush firewall tables", documentation)
        self.assertIn("AppArmor profile must remain loaded in enforce mode", documentation)
        self.assertIn("Do not\ndisable the profile", documentation)
        self.assertIn("http://127.0.0.1:3128", runner_example)


if __name__ == "__main__":
    unittest.main()
