"""Write one batch DAG that runs many flights, a bounded number at a time.

Each flight keeps its own per-flight DAG (make_dag.py) and becomes a SUBDAG EXTERNAL node
of the batch; DAGMan's CATEGORY / MAXJOBS keeps at most --max-flights of them running, and
starts the next one as soon as one finishes. No driver loop, no polling: DAGMan does the
bookkeeping, and a flight that fails leaves a rescue DAG for the batch.

    python chtc/make_batch_dag.py run1 NEON_2021_D02_SERC_20210824 NEON_2019_D19_BONA_20190812 --max-flights 4
    python chtc/make_batch_dag.py y2019 --years 2019 --max-flights 20
    python chtc/make_batch_dag.py all --all --max-flights 25
    cd chtc/runs/batches/run1 && condor_submit_dag run1.dag

`--stages` selects the stages of every flight DAG (make_dag.py): `--stages fit` over your
flights archives every flight's coefficients first, `--stages traits` over the same list
then runs the trait maps. `--flights-csv` takes the flight list from a CSV with a
flight_id column:

    python chtc/make_batch_dag.py run1 --flights-csv ~/tmp/run1.csv --stages fit --max-flights 30
    python chtc/make_batch_dag.py run1_traits --flights-csv ~/tmp/run1.csv --stages traits --max-flights 30

Skips flights whose per-flight run directory already holds a finished DAG of the same
stages (<dag>.dagman.out ending in "EXITING WITH STATUS 0") unless --rerun is given;
flights whose DAG needs coefficients that are not in chtc/runs/<flight>/ are skipped
with a message.

A stage-C-only rerun of chosen lines (format change) takes the line list as a CSV with
columns flight_id and line, for example the ledger's lines_ledger.csv filtered to
status old_format; every flight must already have its coefficients in chtc/runs/<flight>/,
and the old tarballs must have been moved off the lab server:

    python chtc/make_batch_dag.py qa_rerun1 --lines-csv ~/tmp/rerun_lines.csv --max-flights 5
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

CHTC_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(CHTC_DIR))

from make_dag import STAGES, dag_name, write_flight_dag  # noqa: E402


def flight_done(runs: Path, flight: str, stages: tuple[str, ...]) -> bool:
    """True when the flight's DAG of these stages, or its full DAG, finished with status 0."""
    for name in {dag_name(flight, stages), dag_name(flight, STAGES)}:
        out = runs / flight / f"{name}.dagman.out"
        if out.exists() and "EXITING WITH STATUS 0" in out.read_text(errors="replace")[-4000:]:
            return True
    return False


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", help="batch name, e.g. run1 (no spaces)")
    ap.add_argument("flights", nargs="*", help="flight ids; or use --all / --sites / --years / --flights-csv")
    ap.add_argument("--all", action="store_true", help="every manifest in the bundle")
    ap.add_argument("--sites", nargs="*", help="only these sites, e.g. CLBJ HARV")
    ap.add_argument("--years", nargs="*", help="only these years, e.g. 2019")
    ap.add_argument("--flights-csv", type=Path, help="CSV with a flight_id column (order kept)")
    ap.add_argument("--stages", nargs="+", choices=STAGES, default=None,
                    help="stages of every flight DAG (default: all three; with --lines-csv: traits)")
    ap.add_argument("--max-flights", type=int, default=5, help="flights running at once")
    ap.add_argument("--rerun", action="store_true", help="include flights that already finished")
    ap.add_argument("--lines-csv", type=Path,
                    help="only these lines (CSV with flight_id, line; optional status column, "
                         "rows with status other than old_format/missing are ignored)")
    ap.add_argument("--bundle", type=Path, default=CHTC_DIR / "bundle")
    ap.add_argument("--runs", type=Path, default=CHTC_DIR / "runs")
    args = ap.parse_args()

    if args.lines_csv:
        stages = tuple(args.stages or ("traits",))
        if "fit" in stages:
            sys.exit("--lines-csv runs the per-line stage (traits) only: the fit needs every line of a flight")
        wanted: dict[str, list[str]] = defaultdict(list)
        with open(args.lines_csv, newline="") as fh:
            for row in csv.DictReader(fh):
                if row.get("status", "old_format") not in ("old_format", "missing"):
                    continue
                wanted[row["flight_id"]].append(row["line"])
        if not wanted:
            sys.exit("no lines selected from the CSV")
        nodes, n_lines = [], 0
        for flight in sorted(wanted):
            try:
                dag, n = write_flight_dag(flight, args.bundle, args.runs, lines_filter=wanted[flight], stages=stages)
            except ValueError as exc:
                sys.exit(str(exc))
            nodes.append((flight, dag))
            n_lines += n
        write_batch(args, nodes, n_lines, [], jobs_per_line=len(stages))
        return

    stages = tuple(args.stages or STAGES)
    if args.all or args.sites or args.years:
        flights = sorted(p.stem for p in (args.bundle / "flights").glob("NEON_*.json"))
        if args.sites:
            flights = [f for f in flights if f.split("_")[3] in set(args.sites)]
        if args.years:
            flights = [f for f in flights if f.split("_")[1] in set(args.years)]
        flights += [f for f in args.flights if f not in flights]
    else:
        flights = list(args.flights)
    if args.flights_csv:
        with open(args.flights_csv, newline="") as fh:
            flights += [row["flight_id"] for row in csv.DictReader(fh) if row["flight_id"] not in flights]
    if not flights:
        sys.exit("no flights selected")

    skipped, unfitted, nodes, n_lines = [], [], [], 0
    for flight in flights:
        if not args.rerun and flight_done(args.runs, flight, stages):
            skipped.append(flight)
            continue
        try:
            dag, n = write_flight_dag(flight, args.bundle, args.runs, stages=stages)
        except ValueError as exc:
            if "no coeffs_" in str(exc):
                unfitted.append(flight)
                continue
            sys.exit(str(exc))
        nodes.append((flight, dag))
        n_lines += n
    if unfitted:
        print(f"skipped {len(unfitted)} flights without coefficients in {args.runs} (run --stages fit first): "
              + " ".join(unfitted))
    if not nodes:
        sys.exit(f"nothing to run: {len(skipped)} flights already finished, {len(unfitted)} not fitted")
    per_line = sum(1 for s in stages if s != "fit")
    write_batch(args, nodes, n_lines, skipped, jobs_per_line=per_line + (1 if "fit" in stages else 0))


def write_batch(args, nodes: list[tuple[str, Path]], n_lines: int, skipped: list[str], jobs_per_line: int) -> None:
    batch_dir = (args.runs / "batches" / args.name).resolve()
    batch_dir.mkdir(parents=True, exist_ok=True)
    out = [f"# batch {args.name}: {len(nodes)} flights, {n_lines} flightlines, at most {args.max_flights} flights at once",
           f"CONFIG {CHTC_DIR.as_posix()}/dagman.config", ""]
    for flight, dag in nodes:
        out.append(f"SUBDAG EXTERNAL {flight} {dag.as_posix()} DIR {dag.parent.as_posix()}")
        out.append(f"CATEGORY {flight} flights")
    out += ["", f"MAXJOBS flights {args.max_flights}", ""]
    batch = batch_dir / f"{args.name}.dag"
    batch.write_text("\n".join(out))
    print(f"wrote {batch}: {len(nodes)} flights, {n_lines} lines, about {jobs_per_line * n_lines + len(nodes)} jobs"
          + (f", {len(skipped)} already finished" if skipped else ""))
    print(f"submit with:  cd {batch_dir.as_posix()} && condor_submit_dag {batch.name}")


if __name__ == "__main__":
    main()
