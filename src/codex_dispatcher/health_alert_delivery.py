"""Durable, idempotent Slack projection for lifecycle-health episodes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256

from codex_dispatcher.health_alerts import HealthAlertDelivery
from codex_dispatcher.lifecycle_health import LifecycleAlert
from codex_dispatcher.slack_reporting import (
    SlackDeliveryState,
    SlackOutboundPublisher,
    build_slack_outbound_message,
    validate_slack_channel_id,
)
from codex_dispatcher.state_store import StateStore


@dataclass(frozen=True, slots=True)
class HealthAlertProjection:
    action: str
    fingerprint: str | None = None
    delivery_key: str | None = None
    permalink: str | None = None

    def to_mapping(self) -> dict[str, object]:
        result: dict[str, object] = {"action": self.action}
        if self.fingerprint is not None:
            result["fingerprint"] = self.fingerprint
        if self.delivery_key is not None:
            result["delivery_key"] = self.delivery_key
        if self.permalink is not None:
            result["permalink"] = self.permalink
        return result


class HealthAlertDeliveryCoordinator:
    """Publish one alert per stable episode and one threaded recovery receipt."""

    def __init__(
        self,
        *,
        store: StateStore,
        publisher: SlackOutboundPublisher,
        channel_id: str,
    ) -> None:
        if not isinstance(store, StateStore):
            raise TypeError("store must be a StateStore")
        self._store = store
        self._publisher = publisher
        self._channel_id = validate_slack_channel_id(channel_id)

    def reconcile(
        self,
        alerts: tuple[LifecycleAlert, ...],
        *,
        checked_at: str,
        alerts_truncated: bool,
    ) -> HealthAlertProjection:
        if not isinstance(alerts, tuple) or any(
            not isinstance(alert, LifecycleAlert) for alert in alerts
        ):
            raise TypeError("alerts must be a tuple of LifecycleAlert values")
        if not isinstance(checked_at, str) or not checked_at:
            raise ValueError("checked_at is required")

        active = self._store.get_active_health_alert()
        if active is not None:
            active_delivery = self._required_delivery(active.delivery_key)
            if active_delivery.channel_id != self._channel_id:
                raise RuntimeError("active health alert channel differs from configuration")
            active_delivery = self._deliver(active_delivery)
        else:
            active_delivery = None

        if alerts:
            fingerprint = health_alert_fingerprint(alerts)
            if active is not None and active.fingerprint == fingerprint:
                assert active_delivery is not None
                return HealthAlertProjection(
                    "unchanged",
                    fingerprint,
                    active_delivery.delivery_key,
                    active_delivery.permalink,
                )
            previous_key = None if active is None else active.delivery_key
            delivery_key = _new_alert_delivery_key(
                checked_at=checked_at,
                fingerprint=fingerprint,
                previous_delivery_key=previous_key,
            )
            report = build_slack_outbound_message(
                deduplication_key=delivery_key,
                channel_id=self._channel_id,
                text=_render_alert(
                    alerts,
                    checked_at=checked_at,
                    alerts_truncated=alerts_truncated,
                    updated=active is not None,
                ),
            )
            delivery = self._store.prepare_health_alert_delivery(
                report,
                fingerprint=fingerprint,
                expected_active_delivery_key=previous_key,
                created_at=checked_at,
            )
            delivery = self._deliver(delivery)
            return HealthAlertProjection(
                "alert_updated" if active is not None else "alert_opened",
                fingerprint,
                delivery.delivery_key,
                delivery.permalink,
            )

        if active is None:
            return HealthAlertProjection("healthy")
        assert active_delivery is not None
        recovery_key = f"{active.delivery_key}:recovery"
        delivery = self._store.get_health_alert_delivery(recovery_key)
        if delivery is None:
            if active_delivery.message_ts is None:
                raise RuntimeError("active health alert has no delivery receipt")
            report = build_slack_outbound_message(
                deduplication_key=recovery_key,
                channel_id=self._channel_id,
                thread_ts=active_delivery.message_ts,
                text=_render_recovery(checked_at=checked_at),
            )
            delivery = self._store.prepare_health_recovery_delivery(
                report,
                fingerprint=active.fingerprint,
                active_alert_delivery_key=active.delivery_key,
                created_at=checked_at,
            )
        delivery = self._deliver(
            delivery,
            clear_active_alert_key=active.delivery_key,
        )
        return HealthAlertProjection(
            "recovered",
            active.fingerprint,
            delivery.delivery_key,
            delivery.permalink,
        )

    def _required_delivery(self, delivery_key: str) -> HealthAlertDelivery:
        delivery = self._store.get_health_alert_delivery(delivery_key)
        if delivery is None:
            raise RuntimeError("active health alert delivery is missing")
        return delivery

    def _deliver(
        self,
        delivery: HealthAlertDelivery,
        *,
        clear_active_alert_key: str | None = None,
    ) -> HealthAlertDelivery:
        if delivery.state is SlackDeliveryState.PREPARED:
            receipt = self._publisher.publish(delivery.to_outbound_message())
            return self._store.complete_health_alert_delivery(
                delivery.delivery_key,
                receipt,
                clear_active_alert_key=clear_active_alert_key,
            )
        if clear_active_alert_key is not None:
            assert delivery.message_ts is not None
            assert delivery.permalink is not None
            from codex_dispatcher.slack_reporting import SlackDeliveryReceipt

            return self._store.complete_health_alert_delivery(
                delivery.delivery_key,
                SlackDeliveryReceipt(
                    deduplication_key=delivery.delivery_key,
                    channel_id=delivery.channel_id,
                    message_ts=delivery.message_ts,
                    thread_ts=delivery.thread_ts or delivery.message_ts,
                    permalink=delivery.permalink,
                ),
                clear_active_alert_key=clear_active_alert_key,
            )
        return delivery


def health_alert_fingerprint(alerts: tuple[LifecycleAlert, ...]) -> str:
    """Hash stable alert identities, deliberately excluding changing ages."""
    identities = sorted(
        (
            alert.code,
            alert.work_item_id or "",
            alert.repository or "",
            alert.issue_number or 0,
            alert.unit or "",
        )
        for alert in alerts
    )
    payload = json.dumps(
        identities,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def _new_alert_delivery_key(
    *, checked_at: str, fingerprint: str, previous_delivery_key: str | None
) -> str:
    identity = "\x00".join((checked_at, fingerprint, previous_delivery_key or ""))
    return f"slack-health:{sha256(identity.encode('utf-8')).hexdigest()}:alert"


def _render_alert(
    alerts: tuple[LifecycleAlert, ...],
    *,
    checked_at: str,
    alerts_truncated: bool,
    updated: bool,
) -> str:
    lines = [
        "Codex dispatcher health alert updated" if updated else "Codex dispatcher health alert",
        "",
        f"- Checked at: {checked_at}",
        f"- Reported alerts: {len(alerts)}",
    ]
    if alerts_truncated:
        lines.append("- Additional alerts were truncated by the bounded health check.")
    lines.append("")
    for alert in alerts:
        identity = alert.code
        if alert.repository is not None and alert.issue_number is not None:
            identity += f" {alert.repository}#{alert.issue_number}"
        elif alert.unit is not None:
            identity += f" {alert.unit}"
        elif alert.work_item_id is not None:
            identity += f" {alert.work_item_id}"
        if alert.age_seconds is not None:
            identity += f" age_seconds={alert.age_seconds}"
        lines.append(f"- {identity}")
    return "\n".join(lines)


def _render_recovery(*, checked_at: str) -> str:
    return "\n".join(
        (
            "Codex dispatcher health recovered",
            "",
            f"- Checked at: {checked_at}",
            "- The previously reported alert set is now clear.",
        )
    )
