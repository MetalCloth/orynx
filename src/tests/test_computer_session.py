import json
import threading
import unittest
from unittest.mock import patch

from src.tools.computer_driver import HyprlandDriver
from src.tools.computer_session import ComputerResult, ComputerSession


class ComputerActionBatchTests(unittest.TestCase):
    def test_only_fixed_same_window_text_patterns_use_single_stream(self):
        snapshot = {"mode": "window", "pid": 123}
        self.assertTrue(ComputerSession._batchable_keyboard_sequence(snapshot, [
            {"type": "key", "key": "ctrl+l"},
            {"type": "type", "text": "https://example.com"},
            {"type": "key", "key": "enter"},
        ]))
        self.assertTrue(ComputerSession._batchable_keyboard_sequence(snapshot, [
            {"type": "type", "text": "search terms"},
            {"type": "key", "key": "enter"},
        ]))
        self.assertFalse(ComputerSession._batchable_keyboard_sequence(snapshot, [
            {"type": "key", "key": "super+w"},
            {"type": "type", "text": "text"},
            {"type": "key", "key": "enter"},
        ]))
        self.assertFalse(ComputerSession._batchable_keyboard_sequence(
            {"mode": "desktop", "pid": 123}, [
                {"type": "key", "key": "ctrl+l"},
                {"type": "type", "text": "https://example.com"},
                {"type": "key", "key": "enter"},
            ],
        ))

    def test_driver_sends_address_bar_chain_in_one_ordered_stream(self):
        driver = HyprlandDriver.__new__(HyprlandDriver)
        with patch.object(driver, "_input") as send:
            driver.execute_sequence([
                ("hotkey", {"key": "ctrl+l"}),
                ("type", {"content": "https://example.com"}),
                ("hotkey", {"key": "enter"}),
            ])

        send.assert_called_once_with([
            "key ctrl+l", "type https://example.com", "key enter",
        ])

    def test_act_uses_one_stream_and_reports_its_dispatch_mode(self):
        session = self._session()
        with (
            patch.object(session, "_dotool_keyboard_sequence") as send,
            patch.object(session, "_fresh_after", side_effect=self._fresh_observation(session)),
        ):
            result = session.act({
                "observation_id": "current",
                "actions": [
                    {"type": "key", "key": "ctrl+l"},
                    {"type": "type", "text": "https://example.com"},
                    {"type": "key", "key": "enter"},
                ],
                "expected_result": "The page opens",
            })

        send.assert_called_once()
        self.assertEqual(self._result_payload(result)["dispatch_mode"], "single_stream")

    def test_other_sequences_keep_per_step_target_checks(self):
        session = self._session()
        with (
            patch.object(session, "_dotool", return_value={"effect": "sent", "route": "dotool"}) as send,
            patch.object(session, "_check_sequence_target") as check_target,
            patch.object(session, "_fresh_after", side_effect=self._fresh_observation(session)),
        ):
            result = session.act({
                "observation_id": "current",
                "actions": [
                    {"type": "key", "key": "ctrl+w"},
                    {"type": "type", "text": "text"},
                    {"type": "key", "key": "enter"},
                ],
                "expected_result": "The UI reflects the action",
            })

        self.assertEqual(send.call_count, 3)
        self.assertEqual(check_target.call_count, 2)
        self.assertEqual(self._result_payload(result)["dispatch_mode"], "step_checked")

    @staticmethod
    def _session():
        session = ComputerSession.__new__(ComputerSession)
        session.cancel_event = threading.Event()
        session.current = {
            "id": "current", "captured_at": 0, "used": False,
            "mode": "window", "pid": 123, "window_id": 42,
            "bounds": {"x": 0, "y": 0, "width": 800, "height": 450},
            "size": (800, 450), "image_scope": "window", "elements": {},
        }
        session.dry_run = False
        session.dry_run_used = False
        session.trace = None
        session.cua = True
        return session

    @staticmethod
    def _fresh_observation(session):
        def fresh(*_args, **_kwargs):
            session.current = {"id": "fresh", "used": False}
            return ComputerResult("Fresh observation", []), None
        return fresh

    @staticmethod
    def _result_payload(result):
        return json.loads(result.text.split("\n", 1)[0])


if __name__ == "__main__":
    unittest.main()
