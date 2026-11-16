"""Print the tree-only computer_observe payload Oryn would send to the model."""

import argparse
import json
import sys
from pathlib import Path
from threading import Event


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.tools.computer_session import ComputerSession  # noqa: E402
from src.agent.conversation_loop import MAX_COMPUTER_TOOL_RESULT_CHARS  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", help="Name of an already-open app, such as Discord")
    parser.add_argument("--tree", action="store_true", help="Print only the tree excerpt with real line breaks")
    args = parser.parse_args()

    session = ComputerSession(Event(), lambda _: None)
    try:
        if not session.cua:
            parser.error("Oryn is configured for dotool; use ORYN_COMPUTER_DRIVER=cua.")
        windows = json.loads(session.observe({"mode": "windows"}).text)["windows"]
        named = [
            window for window in windows
            if str(window.get("app_name") or "").casefold() == args.app.casefold()
        ]
        if not named:
            named = [
                window for window in windows
                if args.app.casefold() in str(window.get("app_name") or "").casefold()
                or args.app.casefold() in str(window.get("title") or "").casefold()
            ]
        matched = [
            window for window in named
            if type(window.get("pid")) is int and type(window.get("window_id")) is int
        ]
        if not matched:
            # Oryn's windows list omits CUA IDs for hidden workspaces. Look up the
            # same PID and geometry without focusing or changing the desktop.
            for window in named:
                pid, bounds = window.get("pid"), window.get("bounds")
                if type(pid) is not int or not isinstance(bounds, dict):
                    continue
                cua_windows = session._cua_call("list_windows", {"pid": pid}).get("windows", [])
                for candidate in cua_windows:
                    if not isinstance(candidate, dict) or candidate.get("pid") != pid:
                        continue
                    cua_bounds = candidate.get("bounds")
                    if not isinstance(cua_bounds, dict):
                        continue
                    keys = ("x", "y", "width", "height")
                    if all(
                        type(bounds.get(key)) in (int, float)
                        and type(cua_bounds.get(key)) in (int, float)
                        and abs(bounds[key] - cua_bounds[key]) <= 4
                        for key in keys
                    ) and type(candidate.get("window_id")) is int:
                        matched.append({**window, "window_id": candidate["window_id"]})
        if len(matched) != 1:
            print(
                f"Expected one CUA-matched {args.app} window; found {len(matched)} "
                f"among {len(named)} named windows.", file=sys.stderr,
            )
            return 1
        window = matched[0]
        result = session.observe({
            "mode": "window", "pid": window["pid"],
            "window_id": window["window_id"], "image": False,
        })
        model_text = result.text
        if len(model_text) > MAX_COMPUTER_TOOL_RESULT_CHARS:
            marker = f"\n[Tool output truncated; original result was {len(model_text)} characters.]"
            model_text = model_text[:MAX_COMPUTER_TOOL_RESULT_CHARS - len(marker)] + marker
            if args.tree:
                print("The model's tool result was cut off; use the default JSON view.", file=sys.stderr)
                return 1
        if args.tree:
            sys.stdout.write(json.loads(result.text).get("tree_excerpt", "") + "\n")
        else:
            sys.stdout.write(model_text + "\n")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
