#!/usr/bin/env python3
"""Dispatch a Forgejo Actions workflow on the hitkit forge and wait for its result.

    run_workflow.py OWNER/REPO WORKFLOW_FILE [--ref main] [--timeout 900] [--logs]

The workflow needs `on: workflow_dispatch`. This script finds the run that the
dispatch created, waits until it finishes, and prints
{"id", "index", "status", "html_url", "jobs": [...]} as JSON. --logs adds every
job's log; without it, the logs of failed jobs are printed. Exit codes: 0 if the
run succeeded, 1 otherwise. Same token and CA settings as forge_api.py.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from forge_api import ApiError, call  # same directory

FINAL = {"success", "failure", "cancelled", "skipped"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo", metavar="OWNER/REPO")
    ap.add_argument("workflow", metavar="WORKFLOW_FILE", help="e.g. ci.yml (the file under .forgejo/workflows)")
    ap.add_argument("--ref", default="main")
    ap.add_argument("--timeout", type=float, default=900)
    ap.add_argument("--logs", action="store_true", help="include every job's log, not only failed ones")
    args = ap.parse_args()

    runs_path = f"/repos/{args.repo}/actions/runs"
    try:
        before = {r["id"] for r in (call("GET", runs_path) or {}).get("workflow_runs", [])}
        call("POST", f"/repos/{args.repo}/actions/workflows/{args.workflow}/dispatches", {"ref": args.ref})
        deadline = time.monotonic() + args.timeout
        run = None
        while time.monotonic() < deadline:
            new = [r for r in call("GET", runs_path)["workflow_runs"] if r["id"] not in before]
            run = new[0] if new else None
            if run and run["status"] in FINAL:
                break
            time.sleep(5)
        else:
            print(f"run_workflow: not finished after {args.timeout:.0f}s "
                  f"(last status: {run['status'] if run else 'not started'})", file=sys.stderr)
            return 1
        # `id` is what the API takes; html_url ends in the per-repository index instead.
        jobs = call("GET", f"/repos/{args.repo}/actions/runs/{run['id']}/jobs") or []
        result = {"id": run["id"], "index": run.get("index_in_repo"), "status": run["status"],
                  "html_url": run["html_url"], "jobs": []}
        for job in jobs:
            entry = {"id": job["id"], "name": job["name"], "status": job["status"], "runs_on": job.get("runs_on")}
            if args.logs or job["status"] != "success":
                log = call("GET", f"/repos/{args.repo}/actions/jobs/{job['id']}/logs") or ""
                entry["log"] = log[-20000:]  # the end of a long log is where failures are
            result["jobs"].append(entry)
    except ApiError as e:
        print(f"run_workflow: HTTP {e.status}: {e.body}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
