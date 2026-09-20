"""Your machine and your CHTC account. Copy to config/local_paths.py and edit.

config/local_paths.py is gitignored; this example is checked in. Nothing here may be
imported outside config/paths.py.
"""

# --------------------------------------------------------------------------------------
# Required
# --------------------------------------------------------------------------------------

#: Where data lives on the machine you drive CHTC from. Code stays in the repository;
#: the inventory, the trait models and anything you cache go under this root.
DATA_ROOT = r"F:\data\neon-aop"

#: Your CHTC NetID. Personal staging is /staging/<first letter>/<NetID>.
CHTC_USER = "yournetid"

#: Your folder on the EnSpec share, below the namespace /fwe/townsend/Enspec. Jobs write
#: here through Pelican, so it must be a path you may write to.
ENSPEC_PROJECT_SUBDIR = "projects/yourname/neon-traits"

#: The same folder as this machine sees it (the mounted EnSpec share). ledger.py and
#: compare_rerun.py read the results here; the jobs never use this path.
ENSPEC_PROJECT_MOUNT = r"G:\projects\yourname\neon-traits"


# --------------------------------------------------------------------------------------
# Optional — the defaults in config/paths.py are usually right
# --------------------------------------------------------------------------------------

#: The Apptainer image on staging, as HTCondor addresses it. Build it with
#: chtc/build_container.sh and put the resulting name here; give every rebuild a new
#: name so running jobs cannot pick up a half-written image.
CHTC_CONTAINER = "file:///staging/y/yournetid/hytools_2026-01-01.sif"

#: Folder name for this repository on the access point (deploy.sh creates ~/<name>).
CHTC_PROJECT_NAME = "hytools-chtc"

#: The PLSR trait models in HyTools JSON format. Defaults to
#: DATA_ROOT/00_reference/trait_models.
# TRAIT_MODEL_DIR = r"F:\data\neon-aop\00_reference\trait_models"

#: Only if CHTC moves the access point or the federation is renamed.
# CHTC_ACCESS_POINT = "townsend-ap4000.chtc.wisc.edu"
# UWDF_FEDERATION = "chtc.wisc.edu"
