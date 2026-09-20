"""Write the DAGMan workflow for one flight: N sample jobs -> 1 BRDF fit -> N trait-map
jobs, plus one archive job (samples + coefficients to the share, then the samples are
deleted from the access point by post_archive.sh).

Stages can be selected (`--stages fit`, `--stages traits`): the fit (A + B + archive) is
a prerequisite of the trait maps, so a DAG without it needs the flight's
coeffs_<flight>.tar in chtc/runs/<flight>/ already. Running `fit` over many flights first
and `traits` later gets every flight's coefficients archived early, so a change to the
models or the trait-map format only ever costs stage C.

Runs on the access point (or here, for inspection) with only the standard library plus
config/paths.py. For each flight id it creates chtc/runs/<flight_id>/ holding the .dag
file and a logs/ folder, and prints the submit command:

    python chtc/make_dag.py NEON_2017_D11_BLUE_20170506
    python chtc/make_dag.py NEON_2017_D11_BLUE_20170506 --lines 160715 161227 161706
    cd chtc/runs/NEON_2017_D11_BLUE_20170506 && condor_submit_dag NEON_2017_D11_BLUE_20170506.dag

Many flights at once: make_batch_dag.py wraps these per-flight DAGs as SUBDAGs.

Resource requests scale with the HDF5 size recorded in the manifest, and the submit
files grow each one after a hold for exceeding it (memory after a code 34 hold, disk after
a code 21 hold, each counted in NumHoldsByReason), so a job that outgrows a request is
released with more, up to three starts:

    stage A   memory 1.5x (floor 4 GB)   disk 1.5x + 3 GB (floor 8 GB)
    stage C   memory 12 GB flat          disk 3.7x + 8 GB (floor 12 GB; the pixel-major
              (0.15x above 80 GB)         copy of a band-chunked file is about 2x the HDF5)
    stage B   memory 0.08x (floor 8 GB)  disk 0.06x + 4 GB
    archive   memory 1 GB                disk 0.06x + 2 GB

`--archive-only` writes <flight>_archive.dag with just the archive node, to back-fill
flights fitted before the node existed. `--stages` picks the stages; a DAG without every
stage is named <flight>_<stages>.dag (e.g. <flight>_fit.dag, <flight>_traits.dag) so its
DAGMan files never collide with the full run's. `--traits-only` is short for
`--stages traits`, used with `--lines` to rerun lines after a format change.


Measured over the 2026 run of the whole NEON archive: stage A peaks at 3.2-5.9 GB memory
and up to 9.3 GB disk on 5-7 GB lines.

Stage C memory does not scale with the line: the unit of work is one 96 x 1024 window,
so over 1 200 finished jobs the peak was 2.5-4.8 GB median and 9.5 GB worst for every
HDF5 size from 1 to 20 GB. An earlier "2x the HDF5" rule asked for up to 44 GB and left
jobs idle for 12 h while 259 slots sat unclaimed, so it is now flat 12 GB
(0.15x above 80 GB), with periodic_release doubling it on the rare OOM. Disk does scale:
the HDF5, its pixel-major copy for band-chunked files, the GeoTIFF and the tarball are
on disk at once (p95 3.3x the HDF5 before the copy existed).

VARS names must not start with REQUEST_: DAGMan hands VARS to condor_submit as macros,
and condor_submit reads any request_<x> macro as a resource request, so a VARS called
REQUEST_MEMORY_MB became a demand for a machine resource "MEMORY_MB" and nothing ever
matched. REQUEST_DISK works only because it coincides with the real command.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

CHTC_DIR = Path(__file__).resolve().parent
REPO = CHTC_DIR.parent
sys.path.insert(0, str(REPO))

from config.paths import CHTC_CONTAINER, CHTC_CORRECTION_DEST, CHTC_TRAIT_MAP_DEST  # noqa: E402


def mb(nbytes: int, factor: float, floor_gb: float, plus_gb: float = 0) -> int:
    """MB for the submit files' request_memory / request_disk expressions (see sample.sub)."""
    return 1024 * max(floor_gb, math.ceil(nbytes / 1e9 * factor + plus_gb))


STAGES = ("fit", "traits")


def dag_name(flight: str, stages: tuple[str, ...]) -> str:
    """<flight>.dag for a full run, <flight>_<stages>.dag for a subset."""
    return f"{flight}.dag" if tuple(stages) == STAGES else f"{flight}_{'_'.join(stages)}.dag"


def write_flight_dag(flight: str, bundle: Path, runs: Path, output_dest: str = CHTC_TRAIT_MAP_DEST,
                     container: str = CHTC_CONTAINER,
                     lines_filter: list[str] | None = None, correction_dest: str = CHTC_CORRECTION_DEST,
                     archive_only: bool = False, traits_only: bool = False,
                     stages: tuple[str, ...] = STAGES) -> tuple[Path, int]:
    """Write runs/<flight>/<flight>[_<stages>].dag and its folders; return (dag path, number of lines).

    stages is any subset of STAGES: "fit" (stage A samples -> stage B fit -> archive) and
    "traits" (stage C, one job per line). Without "fit" the flight's coeffs_<flight>.tar
    must already be in runs/<flight>/.
    traits_only is short for stages=("traits",).

    archive_only writes runs/<flight>/<flight>_archive.dag with just the archive node, for
    a flight whose samples and coefficients already sit in runs/<flight>/ (fitted before
    the archive node existed).

    A rerun of lines after a format change needs the old tarballs of those lines moved
    off the lab server first (the server refuses overwrites).
    """
    if traits_only:
        stages = ("traits",)
    stages = tuple(s for s in STAGES if s in stages)
    if not stages and not archive_only:
        raise ValueError(f"{flight}: no stages selected")
    manifest = json.loads((bundle / "flights" / f"{flight}.json").read_text())
    lines = manifest["lines"]
    if lines_filter:
        lines = [x for x in lines if any(s in x["name"] for s in lines_filter)]
    if not lines:
        raise ValueError(f"{flight}: no lines selected")

    run_dir = (runs / flight).resolve()
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)
    (run_dir / "samples").mkdir(exist_ok=True)
    # The file:// transfer plugin does not create directories, so make the flight's
    # output folder now (only possible for a local /staging destination).
    for dest in (output_dest, correction_dest):
        if dest.startswith("file://"):
            Path(dest[len("file://"):], flight).mkdir(parents=True, exist_ok=True)
    bundle = bundle.resolve()
    common = f'BUNDLE_DIR="{bundle.as_posix()}" RUN_DIR="{run_dir.as_posix()}" CONTAINER="{container}" CHTC_DIR="{CHTC_DIR.as_posix()}"'
    stems = [x["name"][:-3] for x in lines]
    sample_tars = ", ".join(f"{run_dir.as_posix()}/samples/sample_{s}.tar" for s in stems)
    total_gb = manifest["total_gb"]

    # Archive node: after the fit, ship the samples (2 % of the pixels, ~3 % of the HDF5
    # size as gzip) and the coefficients to the lab server; the POST script then deletes
    # the samples from the access point. Disk: the sample tars plus their concatenation.
    archive_lines = [
        f"JOB archive {CHTC_DIR.as_posix()}/archive.sub",
        f'VARS archive FLIGHT="{flight}" SAMPLE_TARS="{sample_tars}" '
        f'DISK_MB="{1024 * max(4, math.ceil(0.06 * total_gb) + 2)}" CORRECTION_DEST="{correction_dest}" {common}',
        f"SCRIPT POST archive {CHTC_DIR.as_posix()}/post_archive.sh {run_dir.as_posix()} $RETURN",
    ]

    if archive_only:
        out = [f"# {flight}: archive of samples + coefficients only",
               f"CONFIG {CHTC_DIR.as_posix()}/dagman.config", "", *archive_lines, "RETRY ALL_NODES 1", ""]
        dag = run_dir / f"{flight}_archive.dag"
        dag.write_text("\n".join(out))
        return dag, len(lines)

    # Stage C: one job per line.
    trait_lines = []
    for x, stem in zip(lines, stems):
        trait_lines.append(f"JOB traits_{stem} {CHTC_DIR.as_posix()}/traits_maps.sub")
        trait_lines.append(f'VARS traits_{stem} FLIGHT="{flight}" LINE="{stem}" '
                           f'DISK_MB="{mb(x["size"], 3.7, 12, 8)}" MEMORY_MB="{mb(x["size"], 0.15, 12)}" '
                           f'OUTPUT_DEST="{output_dest}" {common}')
    per_line = {"traits": trait_lines}
    per_line_nodes = [f"{stage}_{s}" for stage in stages if stage in per_line for s in stems]
    dag = run_dir / dag_name(flight, stages)

    if "fit" not in stages:
        if not (run_dir / f"coeffs_{flight}.tar").exists():
            raise ValueError(f"{flight}: no coeffs_{flight}.tar in {run_dir}; the flight was never fitted here")
        out = [f"# {flight}: {' + '.join(stages)} only, {len(lines)} of {manifest['n_lines']} flightlines",
               f"CONFIG {CHTC_DIR.as_posix()}/dagman.config", ""]
        for stage in stages:
            out += per_line[stage] + [""]
        out += ["RETRY ALL_NODES 1", ""]
        # A rescue file left by an earlier rerun with another line set would be picked up
        # by DAGMan and fail to parse: drop it when its nodes are not a subset of this
        # DAG's nodes.
        nodes = set(per_line_nodes)
        for rescue in run_dir.glob(f"{dag.name}.rescue*"):
            done = set(re.findall(r"^DONE (\S+)", rescue.read_text(), re.M))
            if not done <= nodes:
                rescue.unlink()
                print(f"removed stale {rescue.name} (nodes of a different line set)")
        dag.write_text("\n".join(out))
        return dag, len(lines)

    out = [f"# {flight}: {' + '.join(stages)}, {len(lines)} of {manifest['n_lines']} flightlines, {manifest['total_gb']} GB of HDF5",
           f"CONFIG {CHTC_DIR.as_posix()}/dagman.config", ""]
    for x, stem in zip(lines, stems):
        out.append(f"JOB sample_{stem} {CHTC_DIR.as_posix()}/sample.sub")
        out.append(f'VARS sample_{stem} FLIGHT="{flight}" LINE="{stem}" '
                   f'DISK_MB="{mb(x["size"], 1.5, 8, 3)}" MEMORY_MB="{mb(x["size"], 1.5, 4)}" {common}')
    # The fit pools 2 % of every line's pixels (float32, 367 good bands). Measured over
    # 40 flights: peak memory is 0.04 x the flight's HDF5 GB (BONA 0811,
    # 156 GB -> 6.2 GB; JERC 0907, 147 GB -> 5.1 GB), so ask for twice that, floor 8 GB.
    # A 0.30x rule (up to 78 GB) sat idle for 12 h because no partitionable slot had that
    # much free at once. periodic_release doubles memory on an OOM hold (code 34); disk
    # only holds the gzip samples.
    fit_mb = 1024 * max(8, math.ceil(0.08 * total_gb))
    fit_disk_mb = 1024 * max(8, math.ceil(0.06 * total_gb) + 4)
    out.append("")
    out.append(f"JOB fit_brdf {CHTC_DIR.as_posix()}/fit_brdf.sub")
    out.append(f'VARS fit_brdf FLIGHT="{flight}" SAMPLE_TARS="{sample_tars}" '
               f'MEMORY_MB="{fit_mb}" DISK_MB="{fit_disk_mb}" {common}')
    out.append("")
    out += archive_lines
    out.append("")
    for stage in stages:
        if stage in per_line:
            out += per_line[stage] + [""]
    out.append("PARENT " + " ".join(f"sample_{s}" for s in stems) + " CHILD fit_brdf")
    out.append("PARENT fit_brdf CHILD archive " + " ".join(per_line_nodes))
    out.append("RETRY ALL_NODES 1")
    out.append("")

    dag.write_text("\n".join(out))
    return dag, len(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("flight", help="flight id, e.g. NEON_2017_D11_BLUE_20170506")
    ap.add_argument("--lines", nargs="*", help="only these lines (substring match on the file name)")
    ap.add_argument("--bundle", type=Path, default=CHTC_DIR / "bundle")
    ap.add_argument("--runs", type=Path, default=CHTC_DIR / "runs")
    ap.add_argument("--output-dest", default=CHTC_TRAIT_MAP_DEST, help="where traits_<line>.tar go")
    ap.add_argument("--correction-dest", default=CHTC_CORRECTION_DEST, help="where samples/coeffs tars go")
    ap.add_argument("--container", default=CHTC_CONTAINER)
    ap.add_argument("--archive-only", action="store_true",
                    help="only the archive node, for a flight fitted before that node existed")
    ap.add_argument("--traits-only", action="store_true",
                    help="only the stage C nodes (rerun of lines on an already fitted flight); same as --stages traits")
    ap.add_argument("--stages", nargs="+", choices=STAGES, default=list(STAGES),
                    help="stages to include (default all); without fit the flight must already be fitted here")
    args = ap.parse_args()

    try:
        dag, n = write_flight_dag(args.flight, args.bundle, args.runs, args.output_dest,
                                  args.container, args.lines, args.correction_dest, args.archive_only,
                                  args.traits_only, tuple(args.stages))
    except ValueError as exc:
        sys.exit(str(exc))
    print(f"wrote {dag} ({n} lines)")
    print(f"submit with:  cd {dag.parent.as_posix()} && condor_submit_dag {dag.name}")


if __name__ == "__main__":
    main()
