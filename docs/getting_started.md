# Getting started

This walks through the setup from scratch to trait maps for one flight. The first time
takes an afternoon, most of it waiting for the container build and for the jobs to run.

## 1. Access

You need:

* A CHTC account and access to a Townsend lab access point
  (`townsend-ap4000.chtc.wisc.edu`). CHTC gives you `/staging/<first letter of NetID>/<NetID>`,
  which is where the container image goes.
* A NEON API token from <https://data.neonscience.org/myaccount>. Downloads are
  rate-limited without one.
* A folder on the EnSpec share you can write to, and the same folder mounted on your own
  machine so you can look at the results.

### SSH

CHTC asks for Duo on every login, so I use SSH connection sharing. Windows OpenSSH does
not support it but WSL does. In WSL's `~/.ssh/config`:

```
Host chtc
    HostName townsend-ap4000.chtc.wisc.edu
    User <NetID>
    ControlMaster auto
    ControlPath ~/.ssh/cm-%r@%h:%p
    ControlPersist 8h
```

Log in once and leave that window open:

```powershell
wsl -d Ubuntu -- ssh chtc
```

Later calls of the form `wsl -d Ubuntu -- ssh chtc <command>` reuse the connection
without another Duo prompt.

Two things to watch: a command containing `$`, quotes or a loop tends to lose them on the
way through, so put anything non-trivial in a script and `scp` it over; and the first
line of output is sometimes dropped, so print something unimportant first.

## 2. Configuration

```powershell
cp config/local_paths.example.py config/local_paths.py
cp config/credentials.example.py config/credentials.py
uv run python -c "from config.paths import validate_paths; print(validate_paths())"
```

In `local_paths.py` set your NetID, a `DATA_ROOT` on your machine, and your share folder
in both forms (the Pelican sub-path the jobs write to, and the mounted path you read
from). Put the NEON token in `credentials.py`. Both files are gitignored.

## 3. Trait models

Put the models as HyTools model JSON, one file per trait, in
`DATA_ROOT/00_reference/trait_models/`, or set `TRAIT_MODEL_DIR` to wherever they are.
Every `*.json` in that folder is applied to every line. Each model file carries its own
wavelengths, so models with different band sets can be mixed; `1_map_traits_line.py`
groups them and resamples once per group.

## 4. Container

This takes about ten minutes and only needs redoing when the HyTools pin changes. It has
to be done in an interactive build job: running `apptainer build` from a batch job fails
in stage 2 with `no such file or directory` on the CHTC build nodes. The build sandbox
also has no writable `/tmp` for apt, which is why `hytools.def` starts from the full
`python:3.11` image and installs HyTools from a GitHub zip instead of `git+https`.

```bash
cd ~/hytools-chtc/chtc
condor_submit -i build_container.sub
apptainer build hytools_<date>.sif hytools.def
apptainer exec hytools_<date>.sif python -c "import hytools, rasterio; print('container ok')"
mv hytools_<date>.sif /staging/<letter>/<netid>/
exit
```

Set `CHTC_CONTAINER` in `local_paths.py` to that file name. Use a new name for every
rebuild, because the transfer mechanism caches by path and can hand out the old image.

## 5. Check that you can write to the share

Do this before submitting anything large:

```bash
cd ~/hytools-chtc/chtc
condor_submit pelican_test.sub DEST=$(python3 -c "import sys; sys.path.insert(0,'..'); from config.paths import UWDF_PROJECT_DIR; print(UWDF_PROJECT_DIR)")/chtc_tests
```

A small file should show up in that folder on the mounted share within a minute. If it
does not, sort that out first, since every job writes its output this way.

## 6. Inventory, bundle, deploy

On your own machine:

```powershell
uv run python src/10_neon_aop__flightline_inventory/1_build_inventory.py --sites CLBJ
uv run python chtc/make_bundle.py
wsl -d Ubuntu -- bash -c "cd /mnt/<repo path> && bash chtc/deploy.sh"
```

The inventory script queries the NEON API for the flightlines and groups them into
flights (one site, one day), writing one manifest per flight. `make_bundle.py` packs
`config/paths.py`, `src/`, the models, the manifests and the token into `chtc/bundle/`.
`deploy.sh` copies `chtc/`, `src/`, `config/` and the bundle to the access point.

Run these again after changing code or models. Note that changing `src/` while a batch is
running affects the jobs that have not started yet.

## 7. One flight

```bash
cd ~/hytools-chtc
python3 chtc/make_dag.py NEON_2019_D11_CLBJ_20190419
cd chtc/runs/NEON_2019_D11_CLBJ_20190419
condor_submit_dag NEON_2019_D11_CLBJ_20190419.dag     # do not use -f, see traps.md
```

To check on it:

```bash
python3 chtc/batch_status.py              # one line per flight
python3 chtc/batch_status.py --problems   # only flights that need attention
```

The trait-map tarballs end up in `<share folder>/30_neon_aop__trait_maps/<flight>/`, one
per flightline, each with a small JSON sidecar next to it so you can check versions and
timings without opening the tar.

I would start with one small flight and read [traps.md](traps.md) before scaling up.

## 8. Many flights

```bash
python3 chtc/make_batch_dag.py run1 --years 2019 --sites CLBJ HARV --max-flights 15
cd chtc/runs/batches/run1 && condor_submit_dag run1.dag
```

`--max-flights` limits how many flights run at once; DAGMan starts the next one when one
finishes. Flights that have already finished are skipped unless you pass `--rerun`.

If you want the coefficients archived for every flight before running the trait maps
(so that a later change to the models only means rerunning stage C), split the stages:

```bash
python3 chtc/make_batch_dag.py run1 --flights-csv ~/tmp/run1.csv --stages fit --max-flights 30
python3 chtc/make_batch_dag.py run1_traits --flights-csv ~/tmp/run1.csv --stages traits --max-flights 20
```

## 9. Checking what landed

On your machine, against the mounted share:

```powershell
uv run python chtc/ledger.py            # per-flight table; writes ledger/*.csv on the share
uv run python chtc/ledger.py --stale    # only flights with missing or old-format lines
```

I treat the ledger as the record of what exists, rather than the DAG logs, because the
logs on the access point get overwritten when something is rerun.
