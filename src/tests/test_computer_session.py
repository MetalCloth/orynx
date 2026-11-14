import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from src.tools.computer_driver import HyprlandDriver
from src.tools.computer_session import ActionNotDispatched, ComputerResult, ComputerSession


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
                "action": [
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
                "action": [
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


class ComputerAccessibilityObservationTests(unittest.TestCase):
    @staticmethod
    def _state():
        return {
            "window_title": "Settings", "window_bounds": {"x": 0, "y": 0, "width": 800, "height": 450},
            "screenshot_width": 800, "screenshot_height": 450,
            "tree_markdown": '- document "Settings"\n  - password text "hunter2"',
            "total_element_count": 7, "returned_element_count": 7,
            "filtered_element_count": 0, "elements_complete": False,
            "degraded": True, "degraded_reason": "AT-SPI read was partial",
            "truncated": True, "truncation_reason": "max_elements",
            "elements": [
                {"element_index": 1, "element_token": "parent", "role": "document",
                 "label": "Settings pane", "depth": 0},
                {"element_index": 2, "role": "entry",
                 "label": "Search", "value": "display", "enabled": True, "actions": [],
                 "depth": 1, "parent_index": 1,
                 "frame": {"x": 20, "y": 20, "w": 200, "h": 30},
                 "screenshot_frame": {"x": 20, "y": 20, "w": 200, "h": 30}},
                {"element_index": 3, "element_token": "disabled", "role": "check box",
                 "label": "Dark mode", "selected": True, "enabled": False,
                 "actions": ["click"], "frame": {"x": 20, "y": 70, "w": 20, "h": 20}},
                {"element_index": 4, "element_token": "bad-frame", "role": "button",
                 "label": "Save", "enabled": True, "actions": ["click"],
                 "frame": {"x": 900, "y": 20, "w": 20, "h": 20},
                 "screenshot_frame": {"x": 900, "y": 20, "w": 20, "h": 20}},
                {"element_index": 5, "element_token": "password", "role": "AXSecureTextField",
                 "label": "hunter2", "value": "hunter2", "enabled": True,
                 "frame": {"x": 20, "y": 100, "w": 200, "h": 30}},
                {"element_index": 6, "element_token": "api-key", "role": "entry",
                 "label": "API key", "value": "sk-raw-secret", "enabled": True,
                 "frame": {"x": 20, "y": 140, "w": 200, "h": 30}},
                {"element_index": 7, "element_token": "toggle", "role": "toggle button",
                 "label": "Notifications", "selected": False, "enabled": True,
                 "frame": {"x": 20, "y": 180, "w": 200, "h": 30}},
            ],
        }

    @staticmethod
    def _session(path, stamp):
        session = ComputerSession.__new__(ComputerSession)
        session.cancel_event = threading.Event()
        session.cua = True
        session.trace = None
        session.current = None
        session.driver = SimpleNamespace(
            socket=Path(path) / "driver.sock", _counter=0,
            width=800, height=450, monitor_rect=(0.0, 0.0, 800.0, 450.0),
            active_window_stamp=lambda: stamp,
        )
        return session

    def test_window_lists_controls_without_click_gate_and_refuses_unmappable_clicks(self):
        state = self._state()
        stamp = ("window", 0, (0, 0), (800, 450))
        with TemporaryDirectory() as path:
            session = self._session(path, stamp)

            def cua_read(_name, args, **_kwargs):
                if args["include_screenshot"]:
                    with Image.new("RGB", (800, 450)) as image:
                        image.save(args["screenshot_out_file"])
                return state

            with patch.object(session, "_cua_call", side_effect=cua_read):
                result = session._window(123, 7, True)

            payload = json.loads(result.text)
            controls = {item["index"]: item for item in payload["controls"]}
            self.assertEqual(len(controls), 7)
            self.assertEqual(controls[2]["actions"], [])
            self.assertTrue(all("can_click" not in item for item in controls.values()))
            self.assertEqual(controls[2]["parent_label"], "Settings pane")
            self.assertEqual(controls[2]["depth"], 1)
            self.assertEqual(controls[2]["image_frame"], {"x": 20, "y": 20, "w": 200, "h": 30})
            self.assertTrue(controls[3]["selected"])
            self.assertFalse(controls[3]["enabled"])
            self.assertNotIn("image_frame", controls[4])
            self.assertEqual(controls[5]["label"], "[sensitive input]")
            self.assertEqual(controls[5]["value"], "[redacted]")
            self.assertEqual(controls[6]["value"], "[redacted]")
            self.assertFalse(controls[7]["selected"])
            self.assertTrue(payload["degraded"])
            self.assertEqual(payload["truncation_reason"], "max_elements")
            self.assertEqual(payload["returned_element_count"], 7)
            self.assertEqual(payload["controls_omitted_count"], 0)
            self.assertIn("omitted", payload["tree_excerpt"])
            self.assertNotIn("hunter2", result.text)
            self.assertNotIn("sk-raw-secret", result.text)
            self.assertNotIn("element_token", result.text)
            self.assertEqual(set(session.current["elements"]), set(range(1, 8)))
            ComputerSession._action(session.current, {"type": "click_element", "element_index": 2})
            with patch.object(session, "_dotool", return_value={"effect": "sent"}) as dotool:
                session._click(session.current, {"type": "click_element", "element_index": 2})
            self.assertEqual(dotool.call_args.args[1]["_dotool_point"], (120, 35))
            self.assertEqual(dotool.call_args.kwargs["target"]["frame_space"], "observation_image")
            with patch.object(session, "_dotool", return_value={"effect": "sent"}) as dotool:
                session._click(session.current, {"type": "click_element", "element_index": 5})
            self.assertEqual(dotool.call_args.kwargs["target"]["value"], "[redacted]")
            self.assertEqual(dotool.call_args.kwargs["target"]["frame_space"], "window")
            ComputerSession._action(session.current, {"type": "click_element", "element_index": 3})
            with patch.object(session, "_dotool", return_value={"effect": "sent"}) as dotool:
                session._click(session.current, {"type": "click_element", "element_index": 3})
            dotool.assert_called_once()
            ComputerSession._action(session.current, {"type": "click_element", "element_index": 4})
            with patch.object(session, "_dotool") as dotool:
                with self.assertRaises(ActionNotDispatched) as refused:
                    session._click(session.current, {"type": "click_element", "element_index": 4})
            self.assertEqual(refused.exception.reason, "element_frame_unavailable")
            dotool.assert_not_called()
            with self.assertRaisesRegex(ValueError, "absent"):
                ComputerSession._action(session.current, {"type": "click_element", "element_index": 99})
            old_id = session.current["id"]
            state["elements"].extend(
                {"element_index": index, "element_token": f"extra-{index}",
                 "role": "entry", "label": f"Extra {index}", "actions": []}
                for index in range(8, 68)
            )
            with patch.object(session, "_cua_call", return_value=state):
                capped = json.loads(session._window(123, 7, False).text)
            self.assertEqual(len(capped["controls"]), 60)
            self.assertEqual(capped["controls_omitted_count"], 7)
            with self.assertRaisesRegex(ValueError, "stale"):
                session._require_snapshot(old_id)

    def test_desktop_summary_keeps_state_and_masks_sensitive_values(self):
        state = self._state()
        stamp = ("window", 0, (0, 0), (800, 450))
        with TemporaryDirectory() as path:
            session = self._session(path, stamp)
            with (patch.object(session, "_active_target", return_value=(123, 7)),
                  patch.object(session, "_cua_call", return_value=state)):
                summary = session._active_accessibility(stamp)
        self.assertFalse(summary["actionable"])
        self.assertTrue(summary["degraded"])
        self.assertEqual(summary["truncation_reason"], "max_elements")
        self.assertEqual(summary["controls_omitted_count"], 0)
        controls = {item["label"]: item for item in summary["controls"]}
        self.assertTrue(controls["Dark mode"]["selected"])
        self.assertEqual(controls["[sensitive input]"]["value"], "[redacted]")
        self.assertEqual(controls["API key"]["value"], "[redacted]")
        self.assertNotIn("hunter2", json.dumps(summary))


if __name__ == "__main__":
    unittest.main()
