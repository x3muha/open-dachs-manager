import copy
from datetime import datetime, timezone
import http.client
import json
from pathlib import Path
import smtplib
import ssl
import tempfile
import threading
import unittest
from unittest.mock import patch

from open_dachs_manager.email_notifications import (
    TEMPERATURE_SOURCES,
    default_email_notification_settings,
    detect_notification_events,
    normalize_email_notification_settings,
    public_email_notification_settings,
    render_notification,
    resolve_smtp_target,
    send_smtp_email,
    smtp_error_policy,
)
from open_dachs_manager.web import (
    APIRequestConflictError, DachsHTTPServer, DachsStore, DachsWebApp, init_users,
)


RECORDED_AT = "2026-11-24T10:00:00+00:00"
TEST_REQUEST_ID = "12345678-1234-4234-9234-123456789abc"


def configured_settings():
    settings = default_email_notification_settings()
    settings.update({
        "enabled": True,
        "recipients": ["owner@example.test"],
        "public_base_url": "https://tools.example.test/dachs",
    })
    settings["smtp"].update({
        "host": "smtp.example.test",
        "port": 587,
        "security": "starttls",
        "username": "odm@example.test",
        "password": "secret-value",
        "from_address": "odm@example.test",
    })
    settings["categories"].update({
        "maintenance": True,
        "fault": True,
        "fault_cleared": True,
    })
    return normalize_email_notification_settings(settings, strict=False)


def live_values(*, service_raw=0, count=3, runtime=12345, temperatures=None, recorded_at=RECORDED_AT):
    rows = [
        {"block": 22, "key": "Hka_Bd.bStoerung", "raw": service_raw, "value": service_raw, "recorded_at": recorded_at},
        {"block": 22, "key": "Hka_Bd.usAnzahlStoerungenHka", "raw": count, "value": count, "recorded_at": recorded_at},
        {
            "block": 22,
            "key": "Hka_Bd.ulBetriebssekundenLetzteStoerung",
            "raw": runtime,
            "value": runtime,
            "recorded_at": recorded_at,
        },
    ]
    for source in TEMPERATURE_SOURCES:
        if temperatures and source["id"] in temperatures:
            rows.append({
                "block": source["block"],
                "key": source["key"],
                "raw": temperatures[source["id"]],
                "value": temperatures[source["id"]],
                "unit": "°C",
                "recorded_at": recorded_at,
            })
    return rows


class FakeSMTP:
    instances = []

    def __init__(self, host, port, *, timeout, context=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.context = context
        self.calls = []
        self.message = None
        type(self).instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.calls.append("close")

    def quit(self):
        self.calls.append("quit")

    def close(self):
        self.calls.append("close")

    def ehlo(self):
        self.calls.append("ehlo")

    def has_extn(self, name):
        return name == "starttls"

    def starttls(self, *, context):
        self.calls.append("starttls")
        self.context = context

    def login(self, username, password):
        self.calls.append(("login", username, password))

    def send_message(self, message):
        self.calls.append("send")
        self.message = message
        return {}


class EmailNotificationCoreTests(unittest.TestCase):
    def setUp(self):
        FakeSMTP.instances.clear()

    def test_defaults_are_disabled_and_public_payload_redacts_password(self):
        settings = default_email_notification_settings()
        self.assertFalse(settings["enabled"])
        self.assertTrue(all(not selected for selected in settings["categories"].values()))
        self.assertTrue(all(rule["threshold_c"] is None for rule in settings["temperature_rules"].values()))
        self.assertTrue(all(rule["hysteresis_c"] is None for rule in settings["temperature_rules"].values()))
        settings["smtp"]["password"] = "do-not-return"
        public = public_email_notification_settings(settings)
        self.assertNotIn("password", public["smtp"])
        self.assertTrue(public["smtp"]["password_configured"])
        self.assertNotIn("do-not-return", str(public))

    def test_settings_validate_transport_addresses_templates_and_temperature_rules(self):
        settings = configured_settings()
        self.assertTrue(settings["enabled"])
        self.assertEqual(settings["public_base_url"], "https://tools.example.test/dachs")
        self.assertEqual(settings["recipients"], ["owner@example.test"])

        for path, value in (
            (("smtp", "host"), "smtp.example.test/path"),
            (("smtp", "from_address"), "owner@example.test\nBcc:x@example.test"),
            (("public_base_url", None), "file:///tmp/odm"),
            (("recipients", None), ["not-an-address"]),
        ):
            candidate = copy.deepcopy(settings)
            if path[1] is None:
                candidate[path[0]] = value
            else:
                candidate[path[0]][path[1]] = value
            with self.subTest(path=path, value=value):
                with self.assertRaises(ValueError):
                    normalize_email_notification_settings(candidate)

        candidate = copy.deepcopy(settings)
        candidate["templates"]["fault"]["subject"] = "{unknown}"
        with self.assertRaisesRegex(ValueError, "Platzhalter"):
            normalize_email_notification_settings(candidate)

        candidate = copy.deepcopy(settings)
        candidate["categories"]["temperature"] = True
        with self.assertRaisesRegex(ValueError, "Temperaturbenachrichtigung"):
            normalize_email_notification_settings(candidate)
        candidate["temperature_rules"]["coolant"] = {
            "enabled": True,
            "direction": "high",
            "threshold_c": 90,
            "hysteresis_c": 3,
        }
        normalized = normalize_email_notification_settings(candidate)
        self.assertEqual(normalized["temperature_rules"]["coolant"]["threshold_c"], 90.0)
        missing_hysteresis = copy.deepcopy(candidate)
        missing_hysteresis["temperature_rules"]["coolant"]["hysteresis_c"] = None
        with self.assertRaisesRegex(ValueError, "benötigt eine Hysterese"):
            normalize_email_notification_settings(missing_hysteresis)
        impossible = copy.deepcopy(candidate)
        impossible["temperature_rules"]["dachs_exhaust"] = {
            "enabled": True,
            "direction": "high",
            "threshold_c": 201,
            "hysteresis_c": 2,
        }
        with self.assertRaisesRegex(ValueError, "0.1 bis 200"):
            normalize_email_notification_settings(impossible)
        for direction, threshold in (("high", -50), ("low", 126)):
            unreachable = copy.deepcopy(candidate)
            unreachable["temperature_rules"]["coolant"] = {
                "enabled": True,
                "direction": direction,
                "threshold_c": threshold,
                "hysteresis_c": 0.1,
            }
            with self.subTest(direction=direction):
                with self.assertRaisesRegex(ValueError, "unerreichbare Rücksetzgrenze"):
                    normalize_email_notification_settings(unreachable)
        for field in ("threshold_c", "hysteresis_c"):
            boolean_value = copy.deepcopy(candidate)
            boolean_value["temperature_rules"]["coolant"][field] = True
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, "ist keine Zahl"):
                    normalize_email_notification_settings(boolean_value)
        candidate = copy.deepcopy(settings)
        candidate["smtp"]["security"] = "none"
        with self.assertRaisesRegex(ValueError, "SMTP-Sicherheit"):
            normalize_email_notification_settings(candidate)
        candidate = copy.deepcopy(settings)
        candidate["public_base_url"] = "http://odm.example.test/dachs"
        with self.assertRaisesRegex(ValueError, "https://"):
            normalize_email_notification_settings(candidate)

    def test_smtp_target_blocks_private_ranges_unless_root_allowlisted(self):
        private = lambda *_args, **_kwargs: [
            (2, 1, 6, "", ("192.0.2.10", 587)),
        ]
        with self.assertRaisesRegex(ValueError, "PRIVATE_ALLOWLIST"):
            resolve_smtp_target("relay.internal", 587, resolver=private)
        self.assertEqual(
            resolve_smtp_target(
                "relay.internal", 587, resolver=private,
                private_allowlist={"relay.internal:587"},
            ),
            "192.0.2.10",
        )

    def test_smtp_error_policy_distinguishes_retry_dead_and_pause(self):
        self.assertEqual(smtp_error_policy(OSError("network")), "retry")
        self.assertEqual(
            smtp_error_policy(smtplib.SMTPResponseException(451, b"later")), "retry"
        )
        self.assertEqual(
            smtp_error_policy(smtplib.SMTPRecipientsRefused({
                "x@example.test": (550, b"unknown"),
            })),
            "dead",
        )
        self.assertEqual(
            smtp_error_policy(smtplib.SMTPAuthenticationError(535, b"bad auth")),
            "pause",
        )
        self.assertEqual(smtp_error_policy(ssl.SSLError("bad cert")), "pause")

    def test_master_switch_prevents_events_even_with_selected_categories(self):
        settings = configured_settings()
        settings["enabled"] = False
        _state, events = detect_notification_events(
            values=live_values(service_raw=22),
            maintenance={"level": "red", "title": "Wartung fällig"},
            settings=settings,
            previous_state=None,
            recorded_at=RECORDED_AT,
        )
        self.assertEqual(events, [])

    def test_category_activation_is_not_consumed_without_fresh_values(self):
        disabled = configured_settings()
        disabled["enabled"] = False
        state, events = detect_notification_events(
            values=live_values(service_raw=22),
            maintenance=None,
            settings=disabled,
            previous_state=None,
            recorded_at=RECORDED_AT,
        )
        self.assertEqual(events, [])
        enabled = configured_settings()
        state, events = detect_notification_events(
            values=[], maintenance=None, settings=enabled,
            previous_state=state, recorded_at="2026-11-24T10:00:01+00:00",
        )
        self.assertEqual(events, [])
        state, events = detect_notification_events(
            values=live_values(
                service_raw=22, recorded_at="2026-11-24T10:00:02+00:00"
            ),
            maintenance=None, settings=enabled,
            previous_state=state, recorded_at="2026-11-24T10:00:02+00:00",
        )
        self.assertEqual([event["category"] for event in events], ["fault"])

    def test_blank_password_preserves_existing_and_explicit_clear_removes_it(self):
        settings = configured_settings()
        payload = public_email_notification_settings(settings)
        payload["smtp"]["password"] = ""
        preserved = normalize_email_notification_settings(
            payload,
            existing_password="old-secret",
        )
        self.assertEqual(preserved["smtp"]["password"], "old-secret")
        payload["enabled"] = False
        payload["smtp"]["clear_password"] = True
        cleared = normalize_email_notification_settings(
            payload,
            existing_password="old-secret",
        )
        self.assertEqual(cleared["smtp"]["password"], "")
        payload = public_email_notification_settings(settings)
        payload["smtp"]["password"] = "  exact secret  "
        exact = normalize_email_notification_settings(payload)
        self.assertEqual(exact["smtp"]["password"], "  exact secret  ")

    def test_fault_activation_is_edge_triggered_and_clear_distinguishes_manual_reset(self):
        settings = configured_settings()
        maintenance = {"level": "green", "title": "Wartung im Plan"}
        state, events = detect_notification_events(
            values=live_values(service_raw=22),
            maintenance=maintenance,
            settings=settings,
            previous_state=None,
            recorded_at=RECORDED_AT,
        )
        self.assertEqual([event["category"] for event in events], ["fault"])
        self.assertEqual(events[0]["context"]["service_code"], "122")
        self.assertIn("Grenzwert", events[0]["context"]["service_text"])

        state, events = detect_notification_events(
            values=live_values(service_raw=22),
            maintenance=maintenance,
            settings=settings,
            previous_state=state,
            recorded_at="2026-11-24T10:00:01+00:00",
        )
        self.assertEqual(events, [])

        cleared_state, events = detect_notification_events(
            values=live_values(service_raw=0),
            maintenance=maintenance,
            settings=settings,
            previous_state=state,
            recorded_at="2026-11-24T10:01:00+00:00",
            manual_fault_clear=False,
        )
        self.assertEqual([event["category"] for event in events], ["fault_cleared"])
        self.assertIn("ohne einen von ODM bestätigten", events[0]["context"]["details"])

        _state, events = detect_notification_events(
            values=live_values(service_raw=0),
            maintenance=maintenance,
            settings=settings,
            previous_state=state,
            recorded_at="2026-11-24T10:01:00+00:00",
            manual_fault_clear=True,
        )
        self.assertEqual(events, [])
        self.assertEqual(cleared_state["fault"]["raw"], 0)

    def test_fault_episode_survives_counter_changes_and_recurring_code_gets_new_keys(self):
        settings = configured_settings()
        state, events = detect_notification_events(
            values=live_values(service_raw=22, count=3, runtime=12345),
            maintenance=None,
            settings=settings,
            previous_state=None,
            recorded_at=RECORDED_AT,
            service_text="122 Kuehlw.Motor>100°",
        )
        self.assertEqual(events[0]["event_key"], "fault:1")
        self.assertIn("100", events[0]["context"]["service_text"])
        state, events = detect_notification_events(
            values=live_values(service_raw=22, count=4, runtime=12346),
            maintenance=None,
            settings=settings,
            previous_state=state,
            recorded_at="2026-11-24T10:00:01+00:00",
            service_text="122 Kuehlw.Motor>100°",
        )
        self.assertEqual(events, [])
        state, events = detect_notification_events(
            values=live_values(service_raw=0, count=4, runtime=12346),
            maintenance=None,
            settings=settings,
            previous_state=state,
            recorded_at="2026-11-24T10:00:02+00:00",
        )
        self.assertEqual(events[0]["event_key"], "fault-cleared:1")
        self.assertIn("100", events[0]["context"]["service_text"])
        _state, events = detect_notification_events(
            values=live_values(service_raw=22, count=4, runtime=12346),
            maintenance=None,
            settings=settings,
            previous_state=state,
            recorded_at="2026-11-24T10:00:03+00:00",
        )
        self.assertEqual(events[0]["event_key"], "fault:2")

    def test_maintenance_transition_and_category_activation_are_deduplicated(self):
        settings = configured_settings()
        green = {"level": "green", "title": "Wartung im Plan"}
        yellow = {
            "level": "yellow",
            "title": "Wartung nähert sich",
            "remaining_hours": 150,
            "remaining_days": 20,
            "maintenance_count": 7,
        }
        state, _ = detect_notification_events(
            values=live_values(), maintenance=green, settings=settings,
            previous_state=None, recorded_at=RECORDED_AT,
        )
        state, events = detect_notification_events(
            values=live_values(), maintenance=yellow, settings=settings,
            previous_state=state, recorded_at="2026-11-24T10:02:00+00:00",
        )
        self.assertEqual([event["category"] for event in events], ["maintenance"])
        self.assertIn("150 Bh", events[0]["context"]["details"])
        state, events = detect_notification_events(
            values=live_values(), maintenance=yellow, settings=settings,
            previous_state=state, recorded_at="2026-11-24T10:03:00+00:00",
        )
        self.assertEqual(events, [])

        due = {
            "level": "red", "title": "Wartung fällig",
            "remaining_hours": 0, "remaining_days": 0, "maintenance_count": 7,
        }
        state, events = detect_notification_events(
            values=live_values(), maintenance=due, settings=settings,
            previous_state=state, recorded_at="2026-11-24T10:04:00+00:00",
        )
        self.assertEqual(events[0]["event_key"], "maintenance:due:7.0")
        overdue = {**due, "remaining_hours": -1}
        _state, events = detect_notification_events(
            values=live_values(), maintenance=overdue, settings=settings,
            previous_state=state, recorded_at="2026-11-24T10:05:00+00:00",
        )
        self.assertEqual(events[0]["event_key"], "maintenance:overdue:7.0")
        self.assertEqual(events[0]["context"]["event"], "Wartung überfällig")
        self.assertIn("Wartung überfällig", events[0]["context"]["details"])

    def test_temperature_threshold_uses_hysteresis_and_requires_configured_limit(self):
        settings = configured_settings()
        settings["categories"]["temperature"] = True
        settings["temperature_rules"]["coolant"] = {
            "enabled": True,
            "direction": "high",
            "threshold_c": 90.0,
            "hysteresis_c": 3.0,
        }
        state, events = detect_notification_events(
            values=live_values(temperatures={"coolant": 89.9}),
            maintenance={"level": "green"},
            settings=settings,
            previous_state=None,
            recorded_at=RECORDED_AT,
        )
        self.assertEqual(events, [])
        for index, value in enumerate((90.0, 90.1, 90.2), start=0):
            state, events = detect_notification_events(
                values=live_values(temperatures={"coolant": value}),
                maintenance={"level": "green"},
                settings=settings,
                previous_state=state,
                recorded_at=f"2026-11-24T10:05:0{index}+00:00",
            )
            if index < 2:
                self.assertEqual(events, [])
        self.assertEqual([event["category"] for event in events], ["temperature"])
        self.assertIn("3 frische Messungen", events[0]["context"]["details"])
        state, events = detect_notification_events(
            values=live_values(temperatures={"coolant": 88.0}),
            maintenance={"level": "green"},
            settings=settings,
            previous_state=state,
            recorded_at="2026-11-24T10:06:00+00:00",
        )
        self.assertEqual(events, [])
        self.assertTrue(state["temperature"]["coolant"]["active"])
        state, events = detect_notification_events(
            values=live_values(temperatures={"coolant": 87.0}),
            maintenance={"level": "green"},
            settings=settings,
            previous_state=state,
            recorded_at="2026-11-24T10:07:00+00:00",
        )
        self.assertFalse(state["temperature"]["coolant"]["active"])
        for index in range(3):
            state, events = detect_notification_events(
                values=live_values(temperatures={"coolant": 91.0}),
                maintenance={"level": "green"},
                settings=settings,
                previous_state=state,
                recorded_at=f"2026-11-24T10:08:0{index}+00:00",
            )
        self.assertEqual(events, [])
        self.assertTrue(state["temperature"]["coolant"]["active"])

    def test_temperature_rule_disable_resets_latch_and_reenable_requires_fresh_dwell(self):
        settings = configured_settings()
        settings["categories"]["temperature"] = True
        settings["temperature_rules"]["coolant"] = {
            "enabled": True, "direction": "high",
            "threshold_c": 90.0, "hysteresis_c": 3.0,
        }
        state = None
        for index in range(3):
            state, events = detect_notification_events(
                values=live_values(temperatures={"coolant": 91.0}),
                maintenance=None, settings=settings, previous_state=state,
                recorded_at=f"2026-11-24T09:00:0{index}+00:00",
            )
        self.assertEqual(len(events), 1)
        disabled = copy.deepcopy(settings)
        disabled["temperature_rules"]["coolant"]["enabled"] = False
        state, events = detect_notification_events(
            values=live_values(temperatures={"coolant": 91.0}),
            maintenance=None, settings=disabled, previous_state=state,
            recorded_at="2026-11-24T09:01:00+00:00",
        )
        self.assertEqual(events, [])
        self.assertFalse(state["temperature"]["coolant"]["active"])
        for index in range(3):
            state, events = detect_notification_events(
                values=live_values(temperatures={"coolant": 91.0}),
                maintenance=None, settings=settings, previous_state=state,
                recorded_at=f"2026-11-24T11:00:0{index}+00:00",
            )
            if index < 2:
                self.assertEqual(events, [])
        self.assertEqual(len(events), 1)

    def test_temperature_rule_generation_catches_off_on_between_monitor_cycles(self):
        settings = configured_settings()
        settings["categories"]["temperature"] = True
        settings["temperature_rules"]["coolant"] = {
            "enabled": True, "direction": "high",
            "threshold_c": 90.0, "hysteresis_c": 3.0,
        }
        state = None
        for index in range(3):
            state, events = detect_notification_events(
                values=live_values(temperatures={"coolant": 91.0}),
                maintenance=None, settings=settings, previous_state=state,
                recorded_at=f"2026-11-24T09:00:0{index}+00:00",
            )
        self.assertEqual(len(events), 1)
        reenabled = copy.deepcopy(settings)
        reenabled["temperature_rules_revision"] = 2
        for index in range(3):
            state, events = detect_notification_events(
                values=live_values(temperatures={"coolant": 91.0}),
                maintenance=None, settings=reenabled, previous_state=state,
                recorded_at=f"2026-11-24T11:00:0{index}+00:00",
            )
            if index < 2:
                self.assertEqual(events, [])
        self.assertEqual(len(events), 1)

    def test_editable_template_renders_and_smtp_uses_tls_without_leaking_secret(self):
        settings = configured_settings()
        settings["templates"]["fault"] = {
            "subject": "Anlage: {event}",
            "body": "{details}\nLink: {url}",
        }
        event = {
            "event_key": "fault:22:3:12345",
            "category": "fault",
            "context": {
                "event": "Fehler 122 ist aktiv",
                "details": "Kühlwasser zu warm",
                "service_code": "122",
                "service_text": "Kühlwasser zu warm",
            },
        }
        rendered = render_notification(event, settings, recorded_at=RECORDED_AT)
        self.assertEqual(rendered["subject"], "Anlage: Fehler 122 ist aktiv")
        self.assertIn("https://tools.example.test/dachs", rendered["body"])
        result = send_smtp_email(
            settings,
            subject=rendered["subject"],
            body=rendered["body"],
            message_id="<stable@example.test>",
            smtp_factory=FakeSMTP,
            resolver=lambda *_args, **_kwargs: [
                (2, 1, 6, "", ("93.184.216.34", 587)),
            ],
        )
        self.assertTrue(result["sent"])
        client = FakeSMTP.instances[-1]
        self.assertEqual((client.host, client.port), ("93.184.216.34", 587))
        self.assertEqual(client.calls[:3], ["ehlo", "starttls", "ehlo"])
        self.assertIn(("login", "odm@example.test", "secret-value"), client.calls)
        self.assertIn("send", client.calls)
        self.assertNotIn("secret-value", client.message.as_string())
        self.assertNotIn("secret-value", str(result))
        self.assertGreaterEqual(client.context.minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertEqual(client.message["Message-ID"], "<stable@example.test>")
        self.assertFalse(client.message.is_multipart())

        class RefusingSMTP(FakeSMTP):
            def send_message(self, message):
                super().send_message(message)
                return {"owner@example.test": (550, b"rejected")}

        with self.assertRaises(Exception):
            send_smtp_email(
                settings, subject="test", body="test",
                smtp_factory=RefusingSMTP,
                resolver=lambda *_args, **_kwargs: [
                    (2, 1, 6, "", ("93.184.216.34", 587)),
                ],
            )

    def test_plain_internal_relay_requires_private_root_allowlist_and_never_authenticates(self):
        settings = configured_settings()
        settings["smtp"].update({
            "host": "relay.intern",
            "port": 25,
            "security": "plain-internal",
            "username": "",
            "password": "",
        })
        settings = normalize_email_notification_settings(settings, strict=False)
        private = lambda *_args, **_kwargs: [
            (2, 1, 6, "", ("192.0.2.20", 25)),
        ]
        with self.assertRaisesRegex(ValueError, "OPEN_DACHS_SMTP_PLAIN_ALLOWLIST"):
            send_smtp_email(
                settings, subject="Test", body="Text", smtp_factory=FakeSMTP,
                resolver=private, plain_allowlist=set(),
            )
        result = send_smtp_email(
            settings, subject="Test", body="Text", smtp_factory=FakeSMTP,
            resolver=private, plain_allowlist={"relay.intern:25"},
        )
        self.assertEqual(result["security"], "plain-internal")
        calls = FakeSMTP.instances[-1].calls
        self.assertNotIn("starttls", calls)
        self.assertFalse(any(isinstance(call, tuple) and call[0] == "login" for call in calls))
        public = lambda *_args, **_kwargs: [
            (2, 1, 6, "", ("93.184.216.34", 25)),
        ]
        with self.assertRaisesRegex(ValueError, "ausschließlich.*internen"):
            send_smtp_email(
                settings, subject="Test", body="Text", smtp_factory=FakeSMTP,
                resolver=public, plain_allowlist={"relay.intern:25"},
            )
        with_credentials = copy.deepcopy(settings)
        with_credentials["smtp"]["username"] = "unsafe"
        with self.assertRaisesRegex(ValueError, "keine Zugangsdaten"):
            normalize_email_notification_settings(with_credentials, strict=False)


class EmailNotificationStoreAndAppTests(unittest.TestCase):
    def test_outbox_state_dedup_claim_retry_and_restart_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dachs-web.db"
            store = DachsStore(path)
            state = {"version": 1, "fault": {"raw": 22}}
            message = {
                "event_key": "fault:22:3:12345",
                "category": "fault",
                "recipient": "owner@example.test",
                "subject": "Fehler 122",
                "body": "Kühlwasser zu warm",
                "message_id": "<stable@example.test>",
                "context": {"service_code": "122"},
            }
            self.assertEqual(
                store.record_notification_observation(RECORDED_AT, state, [message]), 1
            )
            self.assertEqual(
                store.record_notification_observation(RECORDED_AT, state, [message]), 0
            )
            self.assertEqual(store.notification_state()["fault"]["raw"], 22)
            claimed = store.claim_notification(RECORDED_AT)
            self.assertEqual(claimed["status"], "sending")
            self.assertEqual(claimed["attempts"], 1)
            store.finish_notification(
                claimed["id"],
                sent=False,
                recorded_at=RECORDED_AT,
                error="temporary failure",
                next_attempt_at="2026-11-24T10:05:00+00:00",
            )
            self.assertIsNone(
                store.claim_notification("2026-11-24T10:04:59+00:00")
            )
            claimed = store.claim_notification("2026-11-24T10:05:00+00:00")
            self.assertEqual(claimed["attempts"], 2)
            # Reopening the store recovers an interrupted sending state.
            recovered = DachsStore(path)
            claimed = recovered.claim_notification("2026-11-24T10:05:01+00:00")
            self.assertEqual(claimed["attempts"], 3)
            recovered.finish_notification(
                claimed["id"], sent=True, recorded_at="2026-11-24T10:05:02+00:00"
            )
            summary = recovered.notification_summary()
            self.assertEqual(summary["counts"]["sent"], 1)
            self.assertEqual(summary["counts"]["pending"], 0)
            dead_message = {**message, "event_key": "fault:dead"}
            recovered.record_notification_observation(
                "2026-11-24T10:06:00+00:00", state, [dead_message]
            )
            claimed = recovered.claim_notification("2026-11-24T10:06:00+00:00")
            recovered.finish_notification(
                claimed["id"],
                sent=False,
                recorded_at="2026-11-24T10:06:01+00:00",
                error="permanent failure",
                terminal=True,
            )
            self.assertEqual(recovered.notification_summary()["counts"]["dead"], 1)
            exhausted = {**message, "event_key": "fault:exhausted"}
            recovered.record_notification_observation(
                "2026-11-24T10:07:00+00:00", state, [exhausted]
            )
            claimed = recovered.claim_notification("2026-11-24T10:07:00+00:00")
            with recovered.lock, recovered.database() as db:
                db.execute(
                    "UPDATE notification_outbox SET attempts=8,status='sending' WHERE id=?",
                    (claimed["id"],),
                )
            restarted = DachsStore(path)
            self.assertEqual(restarted.notification_summary()["counts"]["dead"], 2)
            self.assertIsNone(restarted.claim_notification("2026-11-24T10:08:00+00:00"))

            bulk = [
                {**message, "event_key": f"bulk:{index}"}
                for index in range(501)
            ]
            inserted = restarted.record_notification_observation(
                "2026-11-24T10:09:00+00:00", state, bulk
            )
            self.assertEqual(inserted, 500)
            self.assertEqual(
                restarted.notification_summary()["counts"]["pending"], 500
            )
            self.assertEqual(
                restarted.notification_audits(limit=1)[0]["action"],
                "queue-overflow",
            )

    def test_retention_removes_terminal_content_but_never_pending_mail(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DachsStore(Path(directory) / "dachs-web.db")
            base = {
                "category": "fault", "recipient": "owner@example.test",
                "subject": "subject", "body": "body",
                "message_id": "<stable@example.test>", "context": {},
            }
            store.record_notification_observation(
                "2026-01-01T00:00:00+00:00", {},
                [{**base, "event_key": "old-sent"}],
            )
            claimed = store.claim_notification("2026-01-01T00:00:00+00:00")
            store.finish_notification(
                claimed["id"], sent=True, recorded_at="2026-01-01T00:00:01+00:00"
            )
            store.record_notification_observation(
                "2026-01-01T00:00:02+00:00", {},
                [{**base, "event_key": "old-pending"}],
            )
            store.notification_audit(
                "2025-01-01T00:00:00+00:00", "system", "old", {}
            )
            removed = store.purge_notifications(
                datetime(2026, 11, 24, tzinfo=timezone.utc)
            )
            self.assertEqual(removed, {"outbox": 1, "audit": 1})
            items = store.notification_outbox()
            self.assertEqual([(item["event_key"], item["status"]) for item in items], [
                ("old-pending", "pending"),
            ])

    def test_confirmed_manual_reset_is_detected_without_storing_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DachsStore(Path(directory) / "dachs-web.db")
            store.begin_service_reset_write_intent(
                "intent-1", "2026-11-24T09:59:45+00:00", "admin",
                22, 3, 12345, {"operation": "manual-servicecode-reset-intent"},
            )
            store.mark_service_reset_clear_confirmed("intent-1", RECORDED_AT)
            self.assertTrue(store.recent_confirmed_manual_reset(
                122, "2026-11-24T09:59:30+00:00",
                "2026-11-24T10:01:00+00:00",
            ))
            store.record_service_reset_outcome(
                "intent-1", "2026-11-24T10:00:01+00:00", "admin", 0,
                {"operation": "manual-servicecode-reset", "write_confirmed": True},
                resolve=True, resolution="write-confirmed", details={"error": None},
            )
            self.assertTrue(store.recent_confirmed_manual_reset(
                122, "2026-11-24T09:59:30+00:00",
                "2026-11-24T10:01:00+00:00",
            ))
            self.assertFalse(store.recent_confirmed_manual_reset(
                123, "2026-11-24T09:59:30+00:00",
                "2026-11-24T10:01:00+00:00",
            ))
            self.assertFalse(store.recent_confirmed_manual_reset(
                122, "2026-11-24T10:00:30+00:00",
                "2026-11-24T10:01:00+00:00",
            ))
            self.assertFalse(store.recent_confirmed_manual_reset(
                122, "2026-11-24T09:59:30+00:00",
                "2026-11-24T09:59:59+00:00",
            ))

    def test_confirmed_clear_marker_suppresses_racy_fault_cleared_mail(self):
        with tempfile.TemporaryDirectory() as directory:
            app = DachsWebApp(data_dir=directory, interval=60)
            app.set_email_notification_settings(configured_settings(), "admin")
            previous = {
                "version": 1,
                "fault": {
                    "raw": 22, "episode": 1,
                    "episode_started_at": "2026-11-24T09:59:30+00:00",
                    "text": "Kühlwasser-Motortemperatur über Grenzwert",
                    "fault_count": 3, "fault_runtime_seconds": 12345,
                },
                "categories": {
                    "maintenance": True, "fault": True,
                    "fault_cleared": True, "temperature": False,
                },
            }
            app.store.record_notification_observation(
                "2026-11-24T09:59:30+00:00", previous, []
            )
            app.store.begin_service_reset_write_intent(
                "intent-race", "2026-11-24T09:59:50+00:00", "admin",
                22, 3, 12345, {"operation": "manual-servicecode-reset-intent"},
            )
            app.store.mark_service_reset_clear_confirmed(
                "intent-race", "2026-11-24T10:00:00.100000+00:00"
            )
            observed_at = "2026-11-24T10:00:00.200000+00:00"
            for row in live_values(service_raw=0, recorded_at=observed_at):
                app.live_values[(row["block"], row["key"])] = row
            app._observe_email_notifications(observed_at)
            self.assertEqual(app.store.notification_outbox(), [])
            self.assertEqual(app.store.notification_state()["fault"]["raw"], 0)

    def test_testmail_enqueue_never_replaces_detector_state(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DachsStore(Path(directory) / "dachs-web.db")
            current = {"version": 1, "fault": {"raw": 22, "episode": 4}}
            store.record_notification_observation(RECORDED_AT, current, [])
            store.enqueue_notification_messages(
                "2026-11-24T10:00:01+00:00",
                [{
                    "event_key": "test:state-safe", "category": "test",
                    "recipient": "owner@example.test", "subject": "Test",
                    "body": "Text", "message_id": "<test@example.test>",
                    "context": {"test": True},
                }],
                audit={"actor": "admin", "action": "test-queued", "details": {}},
            )
            self.assertEqual(store.notification_state(), current)

    def test_app_settings_are_fail_closed_private_and_secret_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            app = DachsWebApp(data_dir=directory, interval=60)
            public = app.email_notification_settings()
            self.assertFalse(public["enabled"])
            self.assertFalse(public["ready"])
            self.assertNotIn("password", public["smtp"])
            settings = configured_settings()
            saved = app.set_email_notification_settings(settings, "admin")
            self.assertTrue(saved["enabled"])
            self.assertTrue(saved["ready"])
            self.assertNotIn("password", saved["smtp"])
            self.assertNotIn("secret_ref", saved["smtp"])
            path = Path(directory) / "email_notification_settings.json"
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            stored = json.loads(path.read_text())
            self.assertNotIn("password", stored["smtp"])
            secret_dir = Path(directory) / "secrets"
            secret_files = list(secret_dir.glob("smtp-*.json"))
            self.assertEqual(len(secret_files), 1)
            secret_path = secret_files[0]
            self.assertEqual(stored["smtp"]["secret_ref"], secret_path.name)
            self.assertEqual(secret_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(secret_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(secret_path.read_text())["password"], "secret-value")
            audits = app.store.notification_audits(limit=1)
            self.assertNotIn("secret-value", json.dumps(audits))
            self.assertEqual(audits[0]["action"], "settings")
            restarted = DachsWebApp(data_dir=directory, interval=60)
            self.assertTrue(restarted.email_notification_settings()["enabled"])
            self.assertNotIn(
                "secret-value", json.dumps(restarted.email_notification_settings())
            )

    def test_temperature_rule_revision_changes_only_with_temperature_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            app = DachsWebApp(data_dir=directory, interval=60)
            settings = configured_settings()
            settings["categories"]["temperature"] = True
            settings["temperature_rules"]["coolant"] = {
                "enabled": True, "direction": "high",
                "threshold_c": 90.0, "hysteresis_c": 3.0,
            }
            first = app.set_email_notification_settings(settings, "admin")
            self.assertEqual(first["temperature_rules_revision"], 1)
            template_only = copy.deepcopy(settings)
            template_only["smtp"]["password"] = ""
            template_only["templates"]["fault"]["subject"] = "Geändert: {event}"
            second = app.set_email_notification_settings(template_only, "admin")
            self.assertEqual(second["temperature_rules_revision"], 1)
            disabled = copy.deepcopy(template_only)
            disabled["categories"]["temperature"] = False
            disabled["temperature_rules"]["coolant"]["enabled"] = False
            third = app.set_email_notification_settings(disabled, "admin")
            self.assertEqual(third["temperature_rules_revision"], 2)
            reenabled = copy.deepcopy(template_only)
            fourth = app.set_email_notification_settings(reenabled, "admin")
            self.assertEqual(fourth["temperature_rules_revision"], 3)

    def test_new_secret_never_binds_to_old_host_if_settings_commit_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            app = DachsWebApp(data_dir=directory, interval=60)
            original = configured_settings()
            original["smtp"]["host"] = "old.example.test"
            original["smtp"]["password"] = "old-secret"
            app.set_email_notification_settings(original, "admin")
            before = json.loads(
                (Path(directory) / "email_notification_settings.json").read_text()
            )
            changed = configured_settings()
            changed["smtp"]["host"] = "new.example.test"
            changed["smtp"]["password"] = "new-secret"
            with patch(
                "open_dachs_manager.web.write_json_atomic",
                side_effect=OSError("simulated settings commit failure"),
            ):
                with self.assertRaises(OSError):
                    app.set_email_notification_settings(changed, "admin")
            after = json.loads(
                (Path(directory) / "email_notification_settings.json").read_text()
            )
            self.assertEqual(after, before)
            self.assertEqual(len(list((Path(directory) / "secrets").glob("smtp-*.json"))), 1)
            restarted = DachsWebApp(data_dir=directory, interval=60)
            self.assertEqual(restarted.email_notification_config["smtp"]["host"], "old.example.test")
            self.assertEqual(restarted.email_notification_config["smtp"]["password"], "old-secret")

    def test_secret_symlink_is_rejected_before_settings_commit(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            app = DachsWebApp(data_dir=directory, interval=60)
            (Path(directory) / "secrets").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(PermissionError):
                app.set_email_notification_settings(configured_settings(), "admin")
            self.assertFalse((Path(directory) / "email_notification_settings.json").exists())

    def test_observation_queues_once_and_worker_sends_without_serial_access(self):
        with tempfile.TemporaryDirectory() as directory:
            app = DachsWebApp(data_dir=directory, interval=60)
            app.set_email_notification_settings(configured_settings(), "admin")
            for row in live_values(service_raw=22):
                app.live_values[(row["block"], row["key"])] = row
            app._observe_email_notifications(RECORDED_AT)
            app._observe_email_notifications("2026-11-24T10:00:01+00:00")
            self.assertEqual(app.store.notification_summary()["counts"]["pending"], 1)
            calls = []

            def sender(settings, *, subject, body, message_id):
                calls.append((subject, body, settings["smtp"]["host"], message_id))
                app.stop_event.set()
                return {"sent": True, "recipient_count": 1, "message_id": "test-id"}

            app._send_smtp_email = sender
            app._email_notification_loop()
            self.assertEqual(len(calls), 1)
            self.assertIn("Fehler 122", calls[0][0])
            self.assertEqual(app.store.notification_summary()["counts"]["sent"], 1)
            self.assertEqual(app.controller if hasattr(app, "controller") else None, None)

    def test_each_recipient_gets_a_separate_outbox_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            app = DachsWebApp(data_dir=directory, interval=60)
            settings = configured_settings()
            settings["recipients"] = ["one@example.test", "two@example.test"]
            app.set_email_notification_settings(settings, "admin")
            for row in live_values(service_raw=22):
                app.live_values[(row["block"], row["key"])] = row
            app._observe_email_notifications(RECORDED_AT)
            items = app.store.notification_outbox()
            self.assertEqual(len(items), 2)
            self.assertEqual(
                {item["recipient"] for item in items},
                {"one@example.test", "two@example.test"},
            )
            self.assertEqual(len({item["event_key"] for item in items}), 2)
            self.assertEqual(len({item["message_id"] for item in items}), 2)

    def test_removed_recipient_is_canceled_before_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            app = DachsWebApp(data_dir=directory, interval=60)
            settings = configured_settings()
            settings["recipients"] = ["one@example.test", "two@example.test"]
            app.set_email_notification_settings(settings, "admin")
            for row in live_values(service_raw=22):
                app.live_values[(row["block"], row["key"])] = row
            app._observe_email_notifications(RECORDED_AT)
            changed = configured_settings()
            changed["recipients"] = ["one@example.test"]
            app.set_email_notification_settings(changed, "admin")
            summary = app.store.notification_summary()["counts"]
            self.assertEqual(summary["pending"], 1)
            self.assertEqual(summary["canceled"], 1)
            canceled = next(
                item for item in app.store.notification_outbox()
                if item["recipient"] == "two@example.test"
            )
            self.assertEqual(canceled["status"], "canceled")

    def test_permanent_config_error_pauses_dispatch_until_revision_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            app = DachsWebApp(data_dir=directory, interval=60)
            settings = configured_settings()
            settings["recipients"] = ["one@example.test", "two@example.test"]
            app.set_email_notification_settings(settings, "admin")
            for row in live_values(service_raw=22):
                app.live_values[(row["block"], row["key"])] = row
            app._observe_email_notifications(RECORDED_AT)
            calls = []

            def auth_failure(*_args, **_kwargs):
                calls.append(1)
                raise smtplib.SMTPAuthenticationError(
                    535, b"bad auth odm@example.test secret-value"
                )

            app._send_smtp_email = auth_failure
            app._email_notification_dispatch_once()
            revision = app.email_notification_config["revision"]
            self.assertEqual(app.email_notification_runtime["paused_revision"], revision)
            self.assertEqual(len(calls), 1)
            self.assertNotIn(
                "secret-value",
                json.dumps(app.email_notification_runtime)
                + json.dumps(app.store.notification_outbox())
                + json.dumps(app.store.notification_dispatch_pause()),
            )
            self.assertNotIn(
                "odm@example.test",
                json.dumps(app.email_notification_runtime)
                + json.dumps(app.store.notification_outbox())
                + json.dumps(app.store.notification_dispatch_pause()),
            )
            self.assertEqual(
                app.email_notification_settings()["smtp"]["username"],
                "odm@example.test",
            )
            app._email_notification_dispatch_once()
            self.assertEqual(len(calls), 1)
            restarted = DachsWebApp(data_dir=directory, interval=60)
            restart_calls = []
            restarted._send_smtp_email = lambda *_args, **_kwargs: restart_calls.append(1)
            restarted._email_notification_dispatch_once()
            self.assertEqual(restart_calls, [])
            self.assertEqual(
                restarted.email_notification_runtime["paused_revision"], revision
            )
            restarted.set_email_notification_settings(settings, "admin")
            self.assertIsNone(restarted.email_notification_runtime["paused_revision"])
            self.assertIsNone(restarted.store.notification_dispatch_pause()["paused_revision"])

    def test_settings_update_waits_for_inflight_dispatch_and_remains_consistent(self):
        with tempfile.TemporaryDirectory() as directory:
            app = DachsWebApp(data_dir=directory, interval=60)
            old = configured_settings()
            old["smtp"]["host"] = "old.example.test"
            old["smtp"]["password"] = "old-secret"
            app.set_email_notification_settings(old, "admin")
            for row in live_values(service_raw=22):
                app.live_values[(row["block"], row["key"])] = row
            app._observe_email_notifications(RECORDED_AT)
            entered = threading.Event()
            release = threading.Event()
            sent_hosts = []

            def sender(settings, **_message):
                sent_hosts.append(settings["smtp"]["host"])
                entered.set()
                self.assertTrue(release.wait(2))
                return {"sent": True}

            app._send_smtp_email = sender
            dispatch = threading.Thread(target=app._email_notification_dispatch_once)
            dispatch.start()
            self.assertTrue(entered.wait(2))
            changed = configured_settings()
            changed["smtp"]["host"] = "new.example.test"
            changed["smtp"]["password"] = "new-secret"
            saved = threading.Event()
            update = threading.Thread(
                target=lambda: (app.set_email_notification_settings(changed, "admin"), saved.set())
            )
            update.start()
            self.assertFalse(saved.wait(0.05))
            release.set()
            dispatch.join(2); update.join(2)
            self.assertFalse(dispatch.is_alive()); self.assertFalse(update.is_alive())
            self.assertTrue(saved.is_set())
            self.assertEqual(sent_hosts, ["old.example.test"])
            stored = json.loads(
                (Path(directory) / "email_notification_settings.json").read_text()
            )
            self.assertEqual(stored["smtp"]["host"], "new.example.test")
            self.assertEqual(app.email_notification_config["smtp"]["host"], "new.example.test")
            secrets_on_disk = list((Path(directory) / "secrets").glob("smtp-*.json"))
            self.assertEqual(len(secrets_on_disk), 1)
            self.assertEqual(json.loads(secrets_on_disk[0].read_text())["password"], "new-secret")

    def test_parallel_settings_updates_keep_disk_runtime_and_secret_generation_aligned(self):
        with tempfile.TemporaryDirectory() as directory:
            app = DachsWebApp(data_dir=directory, interval=60)
            barrier = threading.Barrier(3)
            errors = []

            def update(host, password):
                payload = configured_settings()
                payload["smtp"]["host"] = host
                payload["smtp"]["password"] = password
                barrier.wait()
                try:
                    app.set_email_notification_settings(payload, "admin")
                except Exception as exc:
                    errors.append(exc)

            threads = [
                threading.Thread(target=update, args=("a.example.test", "secret-a")),
                threading.Thread(target=update, args=("b.example.test", "secret-b")),
            ]
            for thread in threads: thread.start()
            barrier.wait()
            for thread in threads: thread.join(2)
            self.assertEqual(errors, [])
            stored = json.loads(
                (Path(directory) / "email_notification_settings.json").read_text()
            )
            runtime = app.email_notification_config
            self.assertEqual(stored["smtp"]["host"], runtime["smtp"]["host"])
            secret_files = list((Path(directory) / "secrets").glob("smtp-*.json"))
            self.assertEqual(len(secret_files), 1)
            self.assertEqual(stored["smtp"]["secret_ref"], secret_files[0].name)
            self.assertEqual(
                json.loads(secret_files[0].read_text())["password"],
                runtime["smtp"]["password"],
            )

    def test_testmail_rate_limit_is_serialized_across_concurrent_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            app = DachsWebApp(data_dir=directory, interval=60)
            settings = configured_settings(); settings["enabled"] = False
            app.set_email_notification_settings(settings, "admin")
            barrier = threading.Barrier(3)
            outcomes = []

            def submit(request_id):
                barrier.wait()
                try:
                    outcomes.append(("ok", app.test_email_notification("admin", request_id)))
                except Exception as exc:
                    outcomes.append(("error", exc))

            threads = [
                threading.Thread(target=submit, args=(request_id,))
                for request_id in (
                    "32345678-1234-4234-9234-123456789abc",
                    "42345678-1234-4234-9234-123456789abc",
                )
            ]
            for thread in threads: thread.start()
            barrier.wait()
            for thread in threads: thread.join(2)
            self.assertEqual([kind for kind, _ in outcomes].count("ok"), 1)
            self.assertEqual([kind for kind, _ in outcomes].count("error"), 1)
            self.assertIsInstance(
                next(value for kind, value in outcomes if kind == "error"),
                APIRequestConflictError,
            )
            self.assertEqual(app.store.notification_summary()["counts"]["pending"], 1)

    def test_notification_retention_runs_even_when_serial_monitor_is_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            app = DachsWebApp(data_dir=directory, interval=60)
            app.serial_enabled = False
            calls = []
            original = app.store.purge_notifications

            def purge(now):
                calls.append(now)
                return original(now)

            app.store.purge_notifications = purge
            app._email_notification_dispatch_once = (
                lambda: (app.stop_event.set() or 0.0)
            )
            app._email_notification_loop()
            self.assertEqual(len(calls), 1)

    def test_stale_cached_values_never_create_fault_events(self):
        with tempfile.TemporaryDirectory() as directory:
            app = DachsWebApp(data_dir=directory, interval=60)
            app.set_email_notification_settings(configured_settings(), "admin")
            for row in live_values(
                service_raw=22, recorded_at="2026-11-24T09:59:59+00:00"
            ):
                app.live_values[(row["block"], row["key"])] = row
            app._observe_email_notifications(RECORDED_AT)
            self.assertEqual(app.store.notification_summary()["counts"]["pending"], 0)

    def test_testmail_dispatches_while_automatic_categories_are_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            app = DachsWebApp(data_dir=directory, interval=60)
            settings = configured_settings()
            settings["enabled"] = False
            app.set_email_notification_settings(settings, "admin")
            result = app.test_email_notification("admin", TEST_REQUEST_ID)
            self.assertEqual(result["queued"], 1)
            duplicate = app.test_email_notification("admin", TEST_REQUEST_ID)
            self.assertEqual(duplicate["queued"], 0)
            with self.assertRaises(APIRequestConflictError):
                app.test_email_notification(
                    "admin", "22345678-1234-4234-9234-123456789abc"
                )
            self.assertEqual(app.store.notification_summary()["counts"]["pending"], 1)
            calls = []

            def sender(settings, **message):
                calls.append((settings, message))
                app.stop_event.set()
                return {"sent": True}

            app._send_smtp_email = sender
            app._email_notification_loop()
            self.assertEqual(len(calls), 1)
            self.assertEqual(app.store.notification_summary()["counts"]["sent"], 1)

    def test_testmail_audit_failure_rolls_back_outbox(self):
        with tempfile.TemporaryDirectory() as directory:
            app = DachsWebApp(data_dir=directory, interval=60)
            settings = configured_settings()
            settings["enabled"] = False
            app.set_email_notification_settings(settings, "admin")
            with app.store.database() as db:
                db.execute(
                    "CREATE TRIGGER fail_notification_audit BEFORE INSERT ON "
                    "notification_audit BEGIN SELECT RAISE(ABORT, 'audit failed'); END"
                )
            with self.assertRaises(Exception):
                app.test_email_notification("admin", TEST_REQUEST_ID)
            self.assertEqual(app.store.notification_summary()["counts"]["pending"], 0)

    def test_dispatcher_loop_survives_internal_failure_and_reports_status(self):
        with tempfile.TemporaryDirectory() as directory:
            app = DachsWebApp(data_dir=directory, interval=60)
            calls = []

            def dispatch():
                calls.append(1)
                if len(calls) == 1:
                    raise RuntimeError("simulated dispatcher failure")
                app.stop_event.set()
                return 0

            app._email_notification_dispatch_once = dispatch
            waits = []
            app.stop_event.wait = lambda delay: waits.append(delay) or False
            app._email_notification_loop()
            self.assertEqual(len(calls), 2)
            self.assertIn(5.0, waits)
            self.assertIn("dispatcher failure", app.email_notification_runtime["last_send_error"])
            self.assertFalse(app.email_notification_runtime["dispatcher_alive"])

    def test_worker_failure_is_persisted_for_bounded_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            app = DachsWebApp(data_dir=directory, interval=60)
            app.set_email_notification_settings(configured_settings(), "admin")
            for row in live_values(service_raw=22):
                app.live_values[(row["block"], row["key"])] = row
            app._observe_email_notifications(RECORDED_AT)

            def sender(*_args, **_kwargs):
                app.stop_event.set()
                raise OSError("smtp unavailable")

            app._send_smtp_email = sender
            app._email_notification_loop()
            items = app.store.notification_outbox()
            self.assertEqual(items[0]["status"], "failed")
            self.assertEqual(items[0]["attempts"], 1)
            self.assertIn("smtp unavailable", items[0]["last_error"])
            self.assertIsNotNone(items[0]["next_attempt_at"])


class EmailNotificationUITests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]

    def test_admin_panel_categories_templates_security_and_base_path_calls(self):
        html = (self.ROOT / "src/open_dachs_manager/web/index.html").read_text()
        app = (self.ROOT / "src/open_dachs_manager/web/app.js").read_text()
        css = (self.ROOT / "src/open_dachs_manager/web/style.css").read_text()
        self.assertIn('data-system-tab="notifications"', html)
        self.assertIn('id="emailNotificationForm"', html)
        for category in ("maintenance", "fault", "fault_cleared", "temperature"):
            self.assertIn(f'data-email-category="{category}"', html)
        self.assertIn("Automatische Texte · bearbeitbar", html)
        self.assertIn("Keine Grenze wird erfunden", html)
        self.assertIn("STARTTLS erforderlich · TLS ≥ 1.2", html)
        self.assertIn("Internes Relay · ohne TLS und ohne Anmeldung", html)
        self.assertIn('value="plain-internal"', html)
        self.assertNotIn('<option value="none">', html)
        self.assertIn("function collectEmailNotificationSettings()", app)
        self.assertIn('api("/api/settings/email-notifications"', app)
        self.assertIn('api("/api/notifications/outbox?limit=50"', app)
        self.assertIn('api("/api/notifications/test"', app)
        self.assertIn("crypto.randomUUID()", app)
        self.assertIn('$("emailSmtpPassword").value = ""', app)
        self.assertIn(".email-notification-form", css)
        self.assertIn('static/app.js?v=71', html)
        self.assertIn('static/style.css?v=50', html)


class EmailNotificationHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        init_users(
            self.temp.name,
            admin_password="admin-password-123",
            guest_password="guest-password-123",
        )
        self.app = DachsWebApp(data_dir=self.temp.name, interval=60)
        self.admin_token = self.app.login("admin", "admin-password-123")[0]
        self.guest_token = self.app.login("gast", "guest-password-123")[0]
        self.server = DachsHTTPServer(
            ("127.0.0.1", 0), self.app, base_path="/dachs"
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, method, path, *, token=None, payload=None, guard=True):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        headers = {}
        body = None
        if token:
            headers["Cookie"] = f"open_dachs_session={token}"
        if payload is not None:
            headers["Content-Type"] = "application/json"
            if guard:
                headers["X-Open-Dachs-Request"] = "1"
            body = json.dumps(payload)
        connection.request(method, f"/dachs{path}", body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        decoded = json.loads(raw.decode()) if raw else {}
        return response.status, decoded

    def test_routes_are_admin_only_base_path_safe_and_secret_redacted(self):
        status, _ = self.request("GET", "/api/settings/email-notifications")
        self.assertEqual(status, 401)
        status, _ = self.request(
            "GET", "/api/settings/email-notifications", token=self.guest_token
        )
        self.assertEqual(status, 403)
        status, settings = self.request(
            "GET", "/api/settings/email-notifications", token=self.admin_token
        )
        self.assertEqual(status, 200)
        self.assertFalse(settings["enabled"])
        self.assertNotIn("password", settings["smtp"])
        status, _ = self.request(
            "GET", "/api/notifications/outbox", token=self.guest_token
        )
        self.assertEqual(status, 403)

    def test_local_override_cannot_change_smtp_secrets(self):
        local_server = DachsHTTPServer(
            ("127.0.0.1", 0),
            self.app,
            base_path="/dachs",
            local_access=True,
            local_role="admin",
        )
        local_thread = threading.Thread(
            target=local_server.serve_forever, daemon=True
        )
        local_thread.start()
        try:
            connection = http.client.HTTPConnection(*local_server.server_address, timeout=3)
            connection.request(
                "POST",
                "/dachs/api/settings/email-notifications",
                body=json.dumps(configured_settings()),
                headers={
                    "Content-Type": "application/json",
                    "X-Open-Dachs-Request": "1",
                },
            )
            response = connection.getresponse()
            response.read()
            connection.close()
            self.assertEqual(response.status, 403)
        finally:
            local_server.shutdown()
            local_server.server_close()
            local_thread.join(timeout=2)

    def test_settings_and_testmail_routes_validate_strictly(self):
        settings = configured_settings()
        status, _ = self.request(
            "POST",
            "/api/settings/email-notifications",
            token=self.admin_token,
            payload=settings,
            guard=False,
        )
        self.assertEqual(status, 403)
        status, saved = self.request(
            "POST",
            "/api/settings/email-notifications",
            token=self.admin_token,
            payload=settings,
        )
        self.assertEqual(status, 200)
        self.assertTrue(saved["ready"])
        self.assertNotIn("password", saved["smtp"])

        invalid = copy.deepcopy(settings)
        invalid["raw_smtp_command"] = "MAIL FROM"
        status, _ = self.request(
            "POST",
            "/api/settings/email-notifications",
            token=self.admin_token,
            payload=invalid,
        )
        self.assertEqual(status, 400)

        status, result = self.request(
            "POST", "/api/notifications/test", token=self.admin_token,
            payload={"request_id": TEST_REQUEST_ID},
        )
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertEqual(result["queued"], 1)
        self.assertNotIn("secret-value", json.dumps(result))
        self.assertEqual(self.app.store.notification_summary()["counts"]["pending"], 1)
        status, _ = self.request(
            "POST",
            "/api/notifications/test",
            token=self.admin_token,
            payload={"smtp_password": "forbidden"},
        )
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
