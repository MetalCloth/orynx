import json
import unittest

from src.agent.computer_task import ComputerTaskLedger, computer_task_schema


class ComputerTaskContractTests(unittest.TestCase):
    @staticmethod
    def _started_task():
        task = ComputerTaskLedger()
        task.apply({
            "operation": "start", "goal": "Open the target app",
            "environment": "local_desktop", "steps": ["Try launcher", "Use another route"],
            "done_checks": ["Target app is visible"],
        })
        return task

    def test_statuses_match_schema_and_blocked_checks_need_evidence(self):
        task = self._started_task()
        schema = computer_task_schema()["parameters"]["properties"]["updates"]["items"]
        self.assertEqual(schema["properties"]["status"]["enum"],
                         ["pending", "working", "verified", "blocked"])
        with self.assertRaisesRegex(ValueError, "evidence"):
            task.apply({"operation": "record", "updates": [
                {"kind": "check", "name": "Target app is visible", "status": "blocked"},
            ]})
        task.apply({"operation": "record", "updates": [
            {"kind": "check", "name": "Target app is visible", "status": "blocked",
             "evidence": "The app has not appeared."},
            {"kind": "step", "name": "Try launcher", "status": "pending"},
        ]})
        self.assertEqual(task.state["done_checks"][0]["status"], "blocked")
        self.assertEqual(task.state["steps"][0]["status"], "pending")

    def test_verified_goal_check_can_finish_after_route_changes(self):
        task = self._started_task()
        with self.assertRaisesRegex(ValueError, "Verify done check"):
            task.apply({"operation": "finish", "result": "complete"})
        task.apply({"operation": "record", "updates": [
            {"kind": "check", "name": "Target app is visible", "status": "verified",
             "evidence": "A fresh observation shows the app."},
        ]})
        result = json.loads(task.apply({"operation": "finish", "result": "complete"}))
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["remaining_steps"], ["Try launcher", "Use another route"])


if __name__ == "__main__":
    unittest.main()
