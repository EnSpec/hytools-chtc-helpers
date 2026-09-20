"""One table per flight under chtc/runs/: what finished, what is queued, what is stuck.

Runs on the access point with the standard library and one `condor_q` call (allowed by
chtc/CLAUDE.md: once, not in a loop):

    python3 chtc/batch_status.py                 # every flight
    python3 chtc/batch_status.py --year 2019     # substring match on the flight id
    python3 chtc/batch_status.py --problems      # only flights that need a hand

Columns: lines (of the newest DAG), samples done, fit (coeffs tar), archive (samples
shipped), traits done (from the job logs), queue (idle/running/held jobs of the
flight), DAG (running, done, failed, or none; "(fit)", "(traits)",
"(archive)" etc. when the newest DAG of the flight is not the full run), and a one-line
hint for anything held. The hints name the recovery steps in chtc/README.md, "When
things go wrong".
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

CHTC_DIR = Path(__file__).resolve().parent
RUNS = CHTC_DIR / "runs"


def condor_jobs() -> list[dict]:
    """Every job in the queue for this user, as dicts (empty list if condor_q fails)."""
    try:
        out = subprocess.run(
            ["condor_q", "-json", "-af:j", "ClusterId", "JobStatus", "Cmd", "Args", "HoldReasonCode", "HoldReason",
             "DAGManJobId", "DAGNodeName"],
            capture_output=True, text=True, timeout=60, check=True).stdout
    except Exception:
        return []
    return json.loads(out) if out.strip() else []


def flight_status(run_dir: Path, jobs: list[dict]) -> dict:
    flight = run_dir.name
    # The most recently touched DAGMan log wins: <flight>.dag (full run), <flight>_fit.dag,
    # <flight>_traits.dag (stage C rerun), <flight>_archive.dag ...; the suffix says which.
    outs = sorted(run_dir.glob(f"{flight}*.dag.dagman.out"), key=lambda p: p.stat().st_mtime)
    dags = sorted(run_dir.glob(f"{flight}*.dag"), key=lambda p: p.stat().st_mtime)
    dag = Path(str(outs[-1])[:-len(".dagman.out")]) if outs else (dags[-1] if dags else run_dir / f"{flight}.dag")
    text = dag.read_text() if dag.exists() else ""
    n_lines = len({m for m in re.findall(r"^JOB (?:sample|traits)_(\S+)", text, re.M)})
    samples = len(list((run_dir / "samples").glob("sample_*.tar")))
    archived = (run_dir / "samples" / "ARCHIVED").exists()
    coeffs = (run_dir / f"coeffs_{flight}.tar").exists()

    def jobs_ok(prefix: str) -> int:
        return sum(1 for p in (run_dir / "logs").glob(f"{prefix}_*.log")
                   if "Normal termination (return value 0)" in p.read_text(errors="replace"))
    traits_ok = jobs_ok("traits")
    dag_state = "none"
    if outs:
        out = outs[-1]
        tail = out.read_text(errors="replace")[-3000:]
        m = re.findall(r"EXITING WITH STATUS (\d+)", tail)
        if m:
            dag_state = "done" if m[-1] == "0" else f"exit {m[-1]}"
        else:
            dag_state = "running"
        kind = out.name[len(flight):].split(".")[0].lstrip("_")
        if kind:
            dag_state += f" ({kind})"
    mine = [j for j in jobs if flight in str(j.get("Args", "")) or flight in str(j.get("Cmd", ""))]
    idle = sum(1 for j in mine if j.get("JobStatus") == 1)
    running = sum(1 for j in mine if j.get("JobStatus") == 2)
    held = [j for j in mine if j.get("JobStatus") == 5]
    hint = ""
    if held:
        code = held[0].get("HoldReasonCode")
        reason = str(held[0].get("HoldReason", ""))
        if code in (34, 21):
            hint = f"{len(held)} held for memory/disk (auto-release pending); if it repeats: condor_qedit + release"
        elif "already exists" in reason:
            hint = f"{len(held)} held: upload refused (object exists) -> verify tar on G:, DONE line in rescue, resubmit"
        elif "does not exist" in reason or "No such file" in reason:
            hint = f"{len(held)} held: job produced no output -> read logs/*.err"
        else:
            hint = f"{len(held)} held (code {code}): {reason[:90]}"
    elif dag_state.startswith("exit"):
        hint = "DAG failed -> fix, then condor_submit_dag <flight>.dag (rescue is picked up; never -f)"
    return {"flight": flight, "lines": n_lines, "samples": samples, "fit": coeffs, "archive": archived,
            "traits": traits_ok, "idle": idle, "running": running, "held": len(held),
            "dag": dag_state, "hint": hint}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--year", help="substring of the flight id to keep, e.g. 2019 or CLBJ")
    ap.add_argument("--problems", action="store_true", help="only flights with a hint")
    ap.add_argument("--runs", type=Path, default=RUNS)
    args = ap.parse_args()

    jobs = condor_jobs()
    rows = []
    for run_dir in sorted(p for p in args.runs.glob("NEON_*") if p.is_dir()):
        if args.year and args.year not in run_dir.name:
            continue
        rows.append(flight_status(run_dir, jobs))
    if args.problems:
        rows = [r for r in rows if r["hint"]]
    print(f"{'flight':32s} {'lines':>5s} {'smp':>4s} {'fit':>3s} {'arc':>3s} {'trt':>4s} {'idle':>4s} {'run':>3s} {'held':>4s}  dag")
    for r in rows:
        print(f"{r['flight']:32s} {r['lines']:5d} {r['samples']:4d} {'y' if r['fit'] else '-':>3s} "
              f"{'y' if r['archive'] else '-':>3s} {r['traits']:4d} {r['idle']:4d} {r['running']:3d} {r['held']:4d}  {r['dag']}")
        if r["hint"]:
            print(f"{'':32s}   -> {r['hint']}")
    done = sum(1 for r in rows if r["dag"].startswith("done"))
    print(f"\n{len(rows)} flights, {done} done, {sum(1 for r in rows if r['dag'].startswith('running'))} running, "
          f"{sum(1 for r in rows if r['hint'])} need attention; {len(jobs)} jobs in the queue")


if __name__ == "__main__":
    main()
