# hytools-chtc-helpers

This repository contains the CHTC scripts we use to run
[HyTools](https://github.com/EnSpec/hytools) topographic and FlexBRDF correction and PLSR
trait mapping over NEON AOP flightlines. It includes the HTCondor wrappers and submit
files, DAG generation, resource settings, NEON downloads, output packaging, and some
utilities for checking what finished and what did not. Outputs go to the EnSpec share.

It reflects the workflow currently used on CHTC and is not maintained as a general
package, so some paths and settings are specific to our setup. Feel free to copy and adapt
it.

## Workflow

Each flight (one site, one day) is processed as one DAGMan workflow:

```text
sample_<line>  x N   stage A: download, topo coefficients, BRDF samples      ~5 min
       └──────────► fit_brdf   stage B: one BRDF fit per flight from all its lines
                        ├────► archive        samples + coefficients to the share
                        └────► traits_<line>  x N   stage C: trait maps        20-40 min
```

`make_dag.py` writes the DAG for a single flight and `make_batch_dag.py` wraps many
flights into one batch with a limit on how many run at the same time. The `--stages`
option lets you run only the fit (stages A, B and archive) or only the trait maps, which
is useful if you want the coefficients for every flight archived first and want to run or
rerun the trait maps later.

The raw HDF5 files never touch `/home` or `/staging`. Each job downloads its flightline
from NEON into the job's scratch directory and deletes it at the end, so only the
coefficients, the sample tarballs and the output tarballs are transferred back.

## What you need

* A CHTC account on one of the Townsend lab access points, and a NEON API token.
* A folder you can write to on the EnSpec share. The share is a Pelican origin, which is
  how the jobs write to it directly.
* PLSR trait models in HyTools model JSON format, one file per trait.

## Quick start

```powershell
cp config/local_paths.example.py config/local_paths.py   # NetID, roots, share folder
cp config/credentials.example.py config/credentials.py   # NEON API token

uv run python src/10_neon_aop__flightline_inventory/1_build_inventory.py --sites CLBJ
uv run python chtc/make_bundle.py
bash chtc/deploy.sh

# on the access point
python3 chtc/make_dag.py NEON_2019_D11_CLBJ_20190419
cd chtc/runs/NEON_2019_D11_CLBJ_20190419 && condor_submit_dag NEON_2019_D11_CLBJ_20190419.dag
```

The full setup, including the container build and the SSH configuration, is in
[docs/getting_started.md](docs/getting_started.md).

## Before running many flights

I would read these first:

* [docs/traps.md](docs/traps.md) covers the problems we ran into at scale: the change in
  chunking of NEON files from 2022 on, the share refusing to overwrite files, hold codes
  that do and do not release themselves, `condor_submit_dag -f`, and the delay after
  holding a DAGMan job.
* [docs/resources.md](docs/resources.md) lists the memory and disk requests per stage
  and where they come from. Requesting too much is a common reason for jobs sitting idle.
* [docs/outputs.md](docs/outputs.md) describes the output files and formats.

## Layout

| Path | Contents |
| :--- | :--- |
| `config/` | all paths and remote locations; `local_paths.py` is the file to edit |
| `src/00_common/` | NEON API and download code, HDF5 helpers, correction parameters, ATCOR class table |
| `src/10_.../` | flightline inventory from the NEON API |
| `src/20_.../` | stage A (sampling) and stage B (BRDF fit) |
| `src/30_.../` | stage C: trait maps, QA raster, quicklook |
| `chtc/` | container definition, wrappers, submit files, DAG generators, status and verification scripts |
| `docs/` | setup, known problems, resource settings, output formats |

The folders under the data root mirror `src/` (for example
`DATA_ROOT/30_neon_aop__trait_maps/`), and the same folder names are used on the share.

## Scope

This assumes the CHTC pool, the `HasCHTCStaging` requirement and the EnSpec share as the
output location. It could be pointed somewhere else, but we have not tried.

## License

GPL-3.0, same as HyTools.
