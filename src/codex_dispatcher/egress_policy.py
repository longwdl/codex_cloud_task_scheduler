"""Fail-closed validation for root-owned Squid allowlist inputs."""

from __future__ import annotations

import argparse
import ipaddress
import stat
import sys
from pathlib import Path


_MAX_POLICY_BYTES = 64 * 1024
_MAX_ALLOW_RULES = 512
_MAX_BLOCK_RULES = 128
_REQUIRED_OWNER_UID = 0


class EgressPolicyError(ValueError):
    """Raised when a proxy policy file exceeds its reviewed boundary."""


def validate_egress_policy(
    allowlist_path: Path,
    blocklist_path: Path,
) -> tuple[int, int]:
    """Validate protected Squid ACL files without returning their contents."""
    allowlist = _policy_lines(allowlist_path, "allowlist")
    blocklist = _policy_lines(blocklist_path, "blocklist")
    if not allowlist or len(allowlist) > _MAX_ALLOW_RULES:
        raise EgressPolicyError("allowlist entry count is invalid")
    if not blocklist or len(blocklist) > _MAX_BLOCK_RULES:
        raise EgressPolicyError("blocklist entry count is invalid")
    _reject_duplicates(allowlist, "allowlist")
    _reject_duplicates(blocklist, "blocklist")
    for line_number, rule in allowlist:
        _validate_domain_rule(rule, line_number)
    for line_number, rule in blocklist:
        _validate_host_network(rule, line_number)
    return len(allowlist), len(blocklist)


def _policy_lines(path: Path, policy: str) -> tuple[tuple[int, str], ...]:
    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        raise EgressPolicyError(f"{policy} path must be normalized and absolute")
    try:
        file_stat = path.lstat()
        raw = path.read_bytes()
    except OSError as exc:
        raise EgressPolicyError(f"{policy} is unavailable") from exc
    if (
        not stat.S_ISREG(file_stat.st_mode)
        or path.is_symlink()
        or file_stat.st_uid != _REQUIRED_OWNER_UID
        or file_stat.st_mode & 0o022
        or not raw
        or len(raw) > _MAX_POLICY_BYTES
        or b"\x00" in raw
    ):
        raise EgressPolicyError(f"{policy} must be a protected regular file")
    _validate_trusted_parents(path, policy)
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise EgressPolicyError(f"{policy} must contain ASCII rules") from exc
    return tuple(
        (line_number, stripped)
        for line_number, line in enumerate(text.splitlines(), start=1)
        if (stripped := line.strip()) and not stripped.startswith("#")
    )


def _reject_duplicates(rules: tuple[tuple[int, str], ...], policy: str) -> None:
    values = tuple(value for _, value in rules)
    if len(values) != len(set(values)):
        raise EgressPolicyError(f"{policy} contains duplicate entries")


def _validate_domain_rule(rule: str, line_number: int) -> None:
    suffix = rule.startswith(".")
    domain = rule[1:] if suffix else rule
    labels = domain.split(".")
    minimum_labels = 3 if suffix else 2
    if (
        len(rule) > 254
        or len(labels) < minimum_labels
        or domain.endswith(".")
        or any(
            not label
            or len(label) > 63
            or label[0] == "-"
            or label[-1] == "-"
            or any(
                not (
                    character.islower()
                    or character.isdigit()
                    or character == "-"
                )
                for character in label
            )
            for label in labels
        )
    ):
        raise EgressPolicyError(f"allowlist line {line_number} is not a safe FQDN")
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        return
    raise EgressPolicyError(f"allowlist line {line_number} must not be an IP literal")


def _validate_host_network(rule: str, line_number: int) -> None:
    try:
        network = ipaddress.ip_network(rule, strict=True)
    except ValueError as exc:
        raise EgressPolicyError(
            f"blocklist line {line_number} is not a canonical network"
        ) from exc
    if str(network) != rule or network.prefixlen != network.max_prefixlen:
        raise EgressPolicyError(
            f"blocklist line {line_number} must name one exact address"
        )


def _validate_trusted_parents(path: Path, policy: str) -> None:
    for parent in path.parents:
        try:
            parent_stat = parent.stat()
        except OSError as exc:
            raise EgressPolicyError(f"{policy} parent is unavailable") from exc
        root_sticky_directory = (
            parent_stat.st_uid == 0 and bool(parent_stat.st_mode & stat.S_ISVTX)
        )
        if (
            not stat.S_ISDIR(parent_stat.st_mode)
            or parent_stat.st_uid not in {0, _REQUIRED_OWNER_UID}
            or (parent_stat.st_mode & 0o022 and not root_sticky_directory)
        ):
            raise EgressPolicyError(f"{policy} parent directories are untrusted")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="validate protected Runner egress ACLs")
    parser.add_argument("--allowlist", required=True, type=Path)
    parser.add_argument("--blocklist", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        allow_count, block_count = validate_egress_policy(
            arguments.allowlist,
            arguments.blocklist,
        )
    except EgressPolicyError as exc:
        sys.stderr.write(f"codex-egress-policy: {exc}\n")
        return 2
    sys.stdout.write(
        f"codex-egress-policy: valid allow={allow_count} block={block_count}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
