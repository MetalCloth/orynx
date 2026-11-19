import base64
import io
import json
import subprocess
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from src.tools.computer_driver import HyprlandDriver
from src.tools.computer_session import (
    ActionNotDispatched, ComputerResult, ComputerSession, StaleDesktopZoom,
)


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
        session._observations = {}
        session.cancel_event = threading.Event()
        session.current = {
            "id": "current", "captured_at": 0, "used": False,
            "mode": "window", "pid": 123, "window_id": 42,
            "bounds": {"x": 0, "y": 0, "width": 800, "height": 450},
            "size": (800, 450), "image_scope": "window", "elements": {},
        }
        session._observations["screen"] = session.current
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
            "total_element_count": 8, "returned_element_count": 8,
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
                {"element_index": 8, "element_token": "logical", "role": "button",
                 "label": "Next tab", "enabled": True,
                 "actions": ["click"] + [f"action-{number}" for number in range(16)]},
            ],
        }

    @staticmethod
    def _session(path, stamp):
        session = ComputerSession.__new__(ComputerSession)
        session._observations = {}
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
            self.assertEqual(len(controls), 8)
            self.assertEqual(controls[2]["actions"], [])
            self.assertTrue(all("can_click" not in item for item in controls.values()))
            self.assertFalse(controls[1]["mouse_position_available"])
            self.assertTrue(controls[2]["mouse_position_available"])
            self.assertTrue(controls[3]["mouse_position_available"])
            self.assertFalse(controls[4]["mouse_position_available"])
            self.assertFalse(controls[8]["mouse_position_available"])
            self.assertEqual(len(controls[8]["actions"]), 15)
            self.assertEqual(controls[8]["actions"][-1], "action-13")
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
            self.assertEqual(payload["returned_element_count"], 8)
            self.assertEqual(payload["controls_omitted_count"], 0)
            self.assertIn('password text "hunter2"', payload["tree_excerpt"])
            self.assertNotIn("hunter2", result.history_text)
            self.assertNotIn("sk-raw-secret", result.text)
            self.assertNotIn("element_token", result.text)
            self.assertEqual(set(session.current["elements"]), set(range(1, 9)))
            ComputerSession._action(session.current, {"type": "click_element", "element_index": 2})
            with patch.object(session, "_dotool", return_value={"effect": "sent"}) as dotool:
                session._click(session.current, {"type": "click_element", "element_index": 2})
            self.assertEqual(dotool.call_args.args[1]["_dotool_point"], (120, 35))
            self.assertEqual(dotool.call_args.kwargs["target"]["frame_space"], "observation_image")
            with patch.object(session, "_dotool", return_value={"effect": "sent"}) as dotool:
                session._click(session.current, {"type": "click_element", "element_index": 5})
            self.assertEqual(dotool.call_args.kwargs["target"]["value"], "[redacted]")
            self.assertEqual(dotool.call_args.kwargs["target"]["frame_space"], "screen")
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
            with patch.object(session, "_dotool") as dotool:
                with self.assertRaises(ActionNotDispatched) as refused:
                    session._click(session.current, {"type": "click_element", "element_index": 8})
            self.assertEqual(refused.exception.reason, "element_frame_unavailable")
            dotool.assert_not_called()
            with self.assertRaisesRegex(ValueError, "absent"):
                ComputerSession._action(session.current, {"type": "click_element", "element_index": 99})
            old_id = session.current["id"]
            state["elements"].extend(
                {"element_index": index, "element_token": f"extra-{index}",
                 "role": "entry", "label": f"Extra {index}", "actions": []}
                for index in range(9, 68)
            )
            with patch.object(session, "_cua_call", return_value=state):
                capped = json.loads(session._window(123, 7, False).text)
            self.assertEqual(len(capped["controls"]), 60)
            self.assertEqual(capped["controls_omitted_count"], 7)
            no_image_controls = {item["index"]: item for item in capped["controls"]}
            self.assertTrue(no_image_controls[2]["mouse_position_available"])
            self.assertFalse(no_image_controls[8]["mouse_position_available"])
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


class ComputerElementCoordinateTests(unittest.TestCase):
    @staticmethod
    def _session():
        session = ComputerSession.__new__(ComputerSession)
        session._observations = {}
        session.driver = SimpleNamespace(
            monitor_rect=(0.0, 0.0, 1920.0, 1080.0), width=1920, height=1080,
        )
        return session

    @staticmethod
    def _snapshot():
        return {
            "bounds": {"x": 735, "y": 340, "width": 500, "height": 400},
            "size": (500, 400), "image_scope": "window", "elements": {},
        }

    def test_pavucontrol_screen_frame_dispatches_to_its_screen_center(self):
        session = self._session()
        snapshot = self._snapshot()
        snapshot["elements"][10] = {
            "role": "toggle button", "label": "Test control",
            "frame": {"x": 1109, "y": 378, "w": 36, "h": 28},
            "screenshot_frame": None,
        }
        with patch.object(session, "_dotool", return_value={"effect": "sent"}) as send:
            session._click(snapshot, {"type": "click_element", "element_index": 10})
        self.assertEqual(send.call_args.args[1]["_dotool_point"], (1127, 392))
        self.assertEqual(send.call_args.kwargs["target"]["frame_space"], "screen")

        snapshot["image_scope"] = "desktop"
        snapshot["size"] = (1920, 1080)
        self.assertEqual(session._dotool_point_for_element(snapshot, snapshot["elements"][10]),
                         (1127, 392))

    def test_screenshot_frame_takes_precedence_and_scales_with_image(self):
        session = self._session()
        snapshot = self._snapshot()
        snapshot["size"] = (250, 200)
        row = {
            "frame": {"x": 0, "y": 0, "w": 20, "h": 20},
            "screenshot_frame": {"x": 187, "y": 19, "w": 18, "h": 14},
        }
        self.assertEqual(session._dotool_point_for_element(snapshot, row), (1127, 392))
        row["screenshot_frame"]["x"] = 250
        with self.assertRaisesRegex(ValueError, "outside the current window image"):
            session._dotool_point_for_element(snapshot, row)

    def test_missing_invalid_and_outside_frames_never_dispatch(self):
        session = self._session()
        snapshot = self._snapshot()
        frames = [
            None,
            {"x": 1109, "y": 378, "w": 0, "h": 28},
            {"x": float("nan"), "y": 378, "w": 36, "h": 28},
            {"x": 700, "y": 378, "w": 2, "h": 28},
            {"x": 1235, "y": 378, "w": 2, "h": 28},
            {"x": 1109, "y": 300, "w": 36, "h": 2},
            {"x": 1109, "y": 740, "w": 36, "h": 2},
        ]
        with patch.object(session, "_dotool") as send:
            for frame in frames:
                with self.subTest(frame=frame):
                    snapshot["elements"][10] = {"role": "button", "label": "Test", "frame": frame}
                    with self.assertRaises(ActionNotDispatched):
                        session._click(snapshot, {"type": "click_element", "element_index": 10})
            snapshot["elements"][10]["frame"] = {"x": 1109, "y": 378, "w": 36, "h": 28}
            snapshot["bounds"] = None
            with self.assertRaises(ActionNotDispatched):
                session._click(snapshot, {"type": "click_element", "element_index": 10})
            snapshot["bounds"] = {"x": 1800, "y": 340, "width": 500, "height": 400}
            snapshot["elements"][10]["frame"] = {"x": 2000, "y": 378, "w": 36, "h": 28}
            with self.assertRaises(ActionNotDispatched):
                session._click(snapshot, {"type": "click_element", "element_index": 10})
            send.assert_not_called()

    def test_one_pixel_and_slightly_clipped_frames_stay_inside_window(self):
        session = self._session()
        snapshot = self._snapshot()
        for frame, expected in [
            ({"x": 735, "y": 340, "w": 1, "h": 1}, (735, 340)),
            ({"x": 1234, "y": 739, "w": 1, "h": 1}, (1234, 739)),
            ({"x": 734, "y": 339, "w": 77, "h": 25}, (772, 351)),
        ]:
            with self.subTest(frame=frame):
                self.assertEqual(session._dotool_point_for_element(snapshot, {"frame": frame}), expected)

        snapshot["elements"][10] = {
            "role": "button", "label": "Test",
            "screenshot_frame": {"x": 499, "y": 399, "w": 1, "h": 1},
        }
        with patch.object(session, "_dotool", return_value={"effect": "sent"}) as send:
            session._click(snapshot, {"type": "click_element", "element_index": 10})
        self.assertEqual(send.call_args.args[1]["_dotool_point"], (1234, 739))


class ComputerPreflightTests(unittest.TestCase):
    @staticmethod
    def _image(size, color="white", image_format="PNG"):
        data = io.BytesIO()
        with Image.new("RGB", size, color) as image:
            image.save(data, format=image_format)
        return data.getvalue()

    def setUp(self):
        self.session = ComputerSession.__new__(ComputerSession)
        self.session.cua = True
        self.session.trace = None
        self.session.cancel_event = threading.Event()
        self.session.driver = SimpleNamespace(
            width=800, height=600, transform=0,
            monitor_rect=(0.0, 0.0, 800.0, 600.0),
            desktop_bounds=(0.0, 0.0, 800.0, 600.0),
            active_window_stamp=Mock(return_value=("0x1", 0, (100, 50), (400, 300))),
        )
        self.session._active_target = Mock(return_value=(123, 42))
        self.session._check_sequence_target = Mock()
        self.session._focus_for_dotool = Mock()
        self.full_image = self._image((400, 300))
        self.session._preflight_image = Mock(return_value=(self.full_image, (400, 300)))
        self.snapshot = {
            "mode": "window", "pid": 123, "window_id": 42,
            "bounds": {"x": 100, "y": 50, "width": 400, "height": 300},
            "size": (400, 300), "image_scope": "window",
            "image_data": base64.b64encode(self.full_image).decode(),
        }
        self.action = {"type": "click", "x": 100, "y": 80}

    def test_stable_target_uses_small_capture_and_checks_identity_before_input(self):
        response = SimpleNamespace(stdout=self._image((73, 49)))
        with (patch("src.tools.computer_session.subprocess.run", return_value=response) as capture,
              patch.object(HyprlandDriver, "execute") as send):
            result = self.session._dotool(self.snapshot, self.action)
        self.assertEqual(result["effect"], "sent")
        self.assertEqual(capture.call_args.args[0][1:3], ["-g", "164,106 73x49"])
        self.session._active_target.assert_called_once()
        self.session._check_sequence_target.assert_called_once()
        self.session._preflight_image.assert_not_called()
        send.assert_called_once()

    def test_changed_target_refuses_without_input(self):
        response = SimpleNamespace(stdout=self._image((73, 49), "black"))
        with (patch("src.tools.computer_session.subprocess.run", return_value=response),
              patch.object(self.session.cancel_event, "wait", return_value=False),
              patch.object(HyprlandDriver, "execute") as send):
            with self.assertRaises(ActionNotDispatched) as error:
                self.session._dotool(self.snapshot, self.action)
        self.assertEqual(error.exception.reason, "target_changed")
        send.assert_not_called()

    def test_replaced_window_with_identical_pixels_is_refused(self):
        self.session._active_target.return_value = (123, 99)
        response = SimpleNamespace(stdout=self._image((73, 49)))
        with (patch("src.tools.computer_session.subprocess.run", return_value=response),
              patch.object(HyprlandDriver, "execute") as send):
            with self.assertRaises(ActionNotDispatched) as error:
                self.session._dotool(self.snapshot, self.action)
        self.assertEqual(error.exception.reason, "target_changed")
        self.session._preflight_image.assert_not_called()
        send.assert_not_called()

    def test_uncertain_or_failed_fast_capture_falls_back_to_full_check(self):
        cases = ("rotated", "different_size", "timeout", "bad_image", "unknown_identity", "lookup_error")
        for case in cases:
            with self.subTest(case=case):
                self.setUp()
                response = SimpleNamespace(stdout=self._image((73, 49)))
                if case == "rotated":
                    self.session.driver.transform = 1
                elif case == "different_size":
                    self.snapshot["bounds"]["width"] = 401
                elif case == "timeout":
                    response = subprocess.TimeoutExpired("grim", 2)
                elif case == "bad_image":
                    response.stdout = self._image((1, 1))
                elif case == "unknown_identity":
                    self.session._active_target.return_value = None
                elif case == "lookup_error":
                    self.session._active_target.side_effect = RuntimeError("not available")
                runner = {"side_effect": response} if isinstance(response, Exception) else {"return_value": response}
                with (patch("src.tools.computer_session.subprocess.run", **runner),
                      patch.object(HyprlandDriver, "execute") as send):
                    result = self.session._dotool(self.snapshot, self.action)
                self.assertEqual(result["effect"], "sent")
                self.session._preflight_image.assert_called_once()
                send.assert_called_once()

    def test_cancellation_after_capture_never_sends_input(self):
        def cancel(*_args, **_kwargs):
            self.session.cancel_event.set()
            return SimpleNamespace(stdout=self._image((73, 49)))
        with (patch("src.tools.computer_session.subprocess.run", side_effect=cancel),
              patch.object(HyprlandDriver, "execute") as send):
            with self.assertRaises(InterruptedError):
                self.session._dotool(self.snapshot, self.action)
        self.session._preflight_image.assert_not_called()
        send.assert_not_called()

    def test_screen_edge_uses_a_clipped_region(self):
        self.action.update(x=0, y=0)
        response = SimpleNamespace(stdout=self._image((37, 25)))
        with (patch("src.tools.computer_session.subprocess.run", return_value=response) as capture,
              patch.object(HyprlandDriver, "execute") as send):
            self.session._dotool(self.snapshot, self.action)
        self.assertEqual(capture.call_args.args[0][1:3], ["-g", "100,50 37x25"])
        send.assert_called_once()


class ComputerActionRecoveryTests(unittest.TestCase):
    def test_window_lookup_preserves_screen_and_marks_list_result(self):
        session = self._session()
        session.cua = False
        screen = session._new_snapshot(mode="desktop")
        with patch.object(session, "_hyprland_clients", return_value=[]):
            result = session._windows()
        self.assertEqual(result.observation_scope, "windows")
        self.assertIs(session._require_snapshot(screen["id"]), screen)
        self.assertEqual(session._require_snapshot(json.loads(result.text)["observation_id"])["mode"], "windows")

    def test_focus_from_retained_list_after_screen_read_and_close_expires_refs(self):
        session = self._session()
        session.cua = False
        windows = session._new_snapshot(mode="windows", windows={1: {"pid": 123}})
        screen = session._new_snapshot(mode="desktop")
        fresh = ComputerResult('{"scope":"window"}', observation_scope="screen")
        with (patch.object(session, "_focus_window", return_value={"effect": "confirmed"}) as focus,
              patch.object(session, "_fresh_after", return_value=(fresh, None))):
            result = session.act({"observation_id": windows["id"],
                                  "action": [{"type": "focus_window", "window_index": 1}],
                                  "expected_result": "Existing app focused"})
        focus.assert_called_once_with(windows, {"type": "focus_window", "window_index": 1})
        self.assertEqual(result.observation_scope, "screen")
        for old in (windows, screen):
            with self.assertRaises(ValueError):
                session._require_snapshot(old["id"])
        last = session._new_snapshot(mode="desktop")
        session.close()
        with self.assertRaises(ValueError):
            session._require_snapshot(last["id"])

    def test_reads_keep_latest_screen_and_latest_windows_independently(self):
        session = self._session()
        screen = session._new_snapshot(mode="desktop")
        windows = session._new_snapshot(mode="windows")
        self.assertIs(session._require_snapshot(screen["id"]), screen)
        newer = session._new_snapshot(mode="desktop")
        self.assertIs(session._require_snapshot(windows["id"]), windows)
        self.assertIs(session._require_snapshot(newer["id"]), newer)
        with self.assertRaises(ValueError):
            session._require_snapshot(screen["id"])
        newest_list = session._new_snapshot(mode="windows")
        with self.assertRaises(ValueError):
            session._require_snapshot(windows["id"])
        self.assertIs(session._require_snapshot(newer["id"]), newer)
        self.assertIs(session._require_snapshot(newest_list["id"]), newest_list)

    def test_screen_key_after_window_lookup_checks_focus_and_invalidates_both_ids(self):
        for focus in ("before", "changed"):
            with self.subTest(focus=focus):
                session = self._session()
                screen = session._new_snapshot(mode="desktop", stamp="before")
                windows = session._new_snapshot(mode="windows")
                session.driver = SimpleNamespace(active_window_stamp=Mock(return_value=focus))
                fresh = ComputerResult('{"scope":"desktop"}')
                with (patch.object(session, "_fresh_after", return_value=(fresh, None)),
                      patch.object(HyprlandDriver, "execute") as send):
                    result = session.act({"observation_id": screen["id"],
                                          "action": [{"type": "key", "key": "leftmeta"}],
                                          "expected_result": "Launcher visible"})
                outcome = json.loads(result.text.splitlines()[0])["action_outcome"]
                if focus == "before":
                    self.assertEqual(outcome["effect"], "sent")
                    send.assert_called_once()
                else:
                    self.assertEqual(outcome["reason"], "active_window_changed")
                    send.assert_not_called()
                for old in (screen, windows):
                    with self.assertRaises(ValueError):
                        session._require_snapshot(old["id"])

    def test_native_wait_consumes_retained_screen_and_window_ids(self):
        session = self._session()
        screen = session._new_snapshot(mode="window", pid=123, window_id=42)
        windows = session._new_snapshot(mode="windows")
        with (patch.object(session, "_verify", return_value={"status": "satisfied"}) as verify,
              patch.object(session, "_fresh_after", return_value=(ComputerResult("{}"), None))):
            session.wait({"observation_id": screen["id"], "expect": {"window": {"exists": True}}})
        verify.assert_called_once()
        for old in (screen, windows):
            with self.assertRaises(ValueError):
                session._require_snapshot(old["id"])

    @staticmethod
    def _session(snapshot=None, *, dry_run=False):
        session = ComputerSession.__new__(ComputerSession)
        session._observations = {}
        session.current = snapshot
        if snapshot is not None:
            session._observations["windows" if snapshot["mode"] == "windows" else "screen"] = snapshot
        session.cancel_event = threading.Event()
        session.dry_run = dry_run
        session.dry_run_used = False
        session.trace = None
        session._recent_actions = []
        session._last_expected_result = ""
        session.goal = ""
        return session

    def test_old_turn_id_returns_fresh_observation_without_input(self):
        session = self._session()
        fresh = ComputerResult('{"observation_id":"fresh","scope":"desktop"}')
        with patch.object(session, "_desktop", return_value=fresh) as observe, \
             patch.object(session, "_dotool") as send:
            result = session.execute("computer_act", {
                "observation_id": "old", "action": [{"type": "key", "key": "esc"}],
                "expected_result": "The menu closes.",
            })
        outcome, observation = map(json.loads, result.text.splitlines())
        self.assertEqual(outcome["action_outcome"]["reason"], "stale_observation_id")
        self.assertEqual(observation["observation_id"], "fresh")
        observe.assert_called_once()
        send.assert_not_called()

    def test_old_focus_id_returns_fresh_windows_list(self):
        session = self._session()
        fresh = ComputerResult('{"observation_id":"new-list","scope":"windows"}')
        with patch.object(session, "_windows", return_value=fresh) as observe, \
             patch.object(session, "_focus_window") as focus:
            result = session.act({"observation_id": "old",
                                  "action": [{"type": "focus_window", "window_index": 1}],
                                  "expected_result": "The app is focused."})
        outcome, observation = map(json.loads, result.text.splitlines())
        self.assertEqual(outcome["action_outcome"]["reason"], "stale_observation_id")
        self.assertEqual(observation["observation_id"], "new-list")
        observe.assert_called_once()
        focus.assert_not_called()

    def test_old_wait_id_returns_fresh_observation_without_verifying(self):
        session = self._session()
        fresh = ComputerResult('{"observation_id":"fresh","scope":"desktop"}')
        with patch.object(session, "_desktop", return_value=fresh) as observe, \
             patch.object(session, "_verify") as verify:
            result = session.wait({"observation_id": "old", "expect": {"window": {"exists": True}}})
        outcome, observation = map(json.loads, result.text.splitlines())
        self.assertEqual(outcome["wait_outcome"]["status"], "not_run")
        self.assertEqual(observation["observation_id"], "fresh")
        observe.assert_called_once()
        verify.assert_not_called()

    def test_windows_list_key_returns_desktop_image_without_input(self):
        session = self._session({"id": "windows-id", "mode": "windows", "used": False})
        session.trace = Mock()
        fresh = ComputerResult('{"observation_id":"desktop-id","scope":"desktop"}',
                               [{"base64_data": "image"}])
        with patch.object(session, "_desktop", return_value=fresh) as observe, \
             patch.object(session, "_dotool") as send:
            result = session.act({"observation_id": "windows-id",
                                  "action": [{"type": "key", "key": "super"}],
                                  "expected_result": "The launcher opens."})
        outcome, observation = map(json.loads, result.text.splitlines())
        self.assertEqual(outcome["action_outcome"]["reason"], "windows_list_has_no_image")
        self.assertEqual(observation["scope"], "desktop")
        self.assertEqual(result.images, fresh.images)
        observe.assert_called_once()
        send.assert_not_called()
        event = session.trace.write.call_args
        self.assertEqual(event.args[0], "computer_action_result")
        self.assertEqual(event.kwargs["outcome"]["effect"], "refused")

    def test_one_action_object_is_accepted_as_shorthand(self):
        snapshot = {"id": "current", "mode": "desktop", "used": False}
        session = self._session(snapshot, dry_run=True)
        result = session.act({"observation_id": "current", "action": {"type": "key", "key": "esc"},
                              "expected_result": "The menu closes."})
        self.assertIn('"type": "key"', result.text)
        self.assertTrue(snapshot["used"])

    def test_changed_focus_during_zoom_returns_fresh_full_desktop(self):
        session = self._session({"id": "overview", "mode": "desktop", "used": False,
                                 "image_scope": "desktop", "size": (800, 600), "stamp": "before"})
        session.driver = SimpleNamespace(width=800, height=600)
        fresh = ComputerResult('{"observation_id":"new","scope":"desktop"}')
        with patch.object(session, "_desktop", side_effect=[StaleDesktopZoom("focus changed"), fresh]):
            result = session._desktop_zoom("overview", {"x": 0, "y": 0, "width": 100, "height": 100})
        outcome, observation = map(json.loads, result.text.splitlines())
        self.assertEqual(outcome["zoom_aborted"], "desktop_focus_changed")
        self.assertEqual(observation["observation_id"], "new")

    def test_changed_click_target_refuses_input_and_returns_fresh_state(self):
        snapshot = {"id": "current", "mode": "desktop", "used": False, "size": (800, 600)}
        session = self._session(snapshot)
        fresh = ComputerResult('{"observation_id":"after","scope":"desktop"}')
        with patch.object(session, "_click", side_effect=ActionNotDispatched(
                "dotool", "target_changed", "No click sent.")) as click, \
             patch.object(session, "_fresh_after", return_value=(fresh, None)) as observe:
            result = session.act({"observation_id": "current",
                                  "action": [{"type": "click", "x": 10, "y": 10}],
                                  "expected_result": "The button opens."})
        outcome, observation = map(json.loads, result.text.splitlines())
        self.assertEqual(outcome["action_outcome"]["effect"], "refused")
        self.assertEqual(outcome["action_outcome"]["reason"], "target_changed")
        self.assertEqual(observation["observation_id"], "after")
        click.assert_called_once()
        observe.assert_called_once()


if __name__ == "__main__":
    unittest.main()
