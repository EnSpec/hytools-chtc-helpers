"""NEON data API client for the DP1.30006.001 flightline reflectance product.

Three jobs: list what NEON has (product availability), list the files of one site-month
with their signed download URLs, and download one file with retries and a size check.
The signed URLs (Google Cloud Storage) expire after 7 days and need no token, so a CHTC
job re-queries the API at run time and downloads straight from GCS.

Flightline naming changed in 2022. Up to 2021 a file is named by its UTC start time,
NEON_D11_BLUE_DP1_20170506_160715_reflectance.h5; from 2022 by its planned line id,
NEON_D01_HARV_DP1_L032-1_20240904_directional_reflectance.h5. Both parse to the same
fields; the ``tag`` is the time or the line id. A *flight* is one site on one day and is
the FlexBRDF group; its id is NEON_<year>_<domain>_<site>_<yyyymmdd>, the same string
the legacy script used.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import requests

PRODUCT = "DP1.30006.001"
API_ROOT = "https://data.neonscience.org/api/v0"

_LINE_RE_2013 = re.compile(
    r"^NEON_(?P<domain>D\d{2})_(?P<site>[A-Z]{4})_DP1_(?P<date>\d{8})_(?P<tag>\d{6})_reflectance\.h5$"
)
_LINE_RE_2022 = re.compile(
    r"^NEON_(?P<domain>D\d{2})_(?P<site>[A-Z]{4})_DP1_(?P<tag>L\d{3}-\d+)_(?P<date>\d{8})_directional_reflectance\.h5$"
)


@dataclass
class Flightline:
    name: str
    size: int
    domain: str
    site: str
    date: str  # yyyymmdd
    tag: str  # hhmmss UTC start time (to 2021) or planned line id such as L032-1 (2022 on)
    month: str  # yyyy-mm, the NEON API site-month that lists this file
    release: str
    crc32c: str | None = None
    #: the site code the API lists this file under, when it differs from the one in the
    #: file name: a flight over several co-located sites is named after the flight box
    #: (CHEQ = STEI + TREE, BRDF 2020 in D10) and only the host site exists in the API.
    api_site: str = ""

    @property
    def listing_site(self) -> str:
        return self.api_site or self.site

    @property
    def flight_id(self) -> str:
        return f"NEON_{self.date[:4]}_{self.domain}_{self.site}_{self.date}"

    @property
    def stem(self) -> str:
        return self.name[:-3]


def parse_flightline(name: str) -> dict | None:
    m = _LINE_RE_2013.match(name) or _LINE_RE_2022.match(name)
    return m.groupdict() if m else None


def _get(url: str, token: str | None, params: dict | None = None, retries: int = 5) -> dict:
    headers = {"X-API-Token": token} if token else {}
    delay = 5
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=120)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"{r.status_code} from {url}")
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as exc:
            if attempt == retries - 1:
                raise
            print(f"  API call failed ({exc}); retrying in {delay}s")
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("unreachable")


def product_availability(token: str | None) -> list[dict]:
    """Every (site, month, release) that has DP1.30006.001 data, provisional included."""
    data = _get(f"{API_ROOT}/products/{PRODUCT}", token)["data"]
    rows = []
    for site in data["siteCodes"]:
        release_of = {}
        for rel in site.get("availableReleases", []):
            for month in rel["availableMonths"]:
                release_of[month] = rel["release"]
        for month in site["availableMonths"]:
            rows.append(
                {"site": site["siteCode"], "month": month, "release": release_of.get(month, "")}
            )
    return rows


def site_month_files(site: str, month: str, token: str | None) -> list[dict]:
    """All files NEON lists for one site-month, signed URLs included."""
    data = _get(
        f"{API_ROOT}/data/{PRODUCT}/{site}/{month}", token, params={"include-provisional": "true"}
    )["data"]
    for f in data["files"]:
        f["release"] = data.get("release", "")
        f["month"] = month
        f["api_site"] = site
    return data["files"]


def flightlines_of(files: list[dict]) -> list[Flightline]:
    """Keep only the reflectance HDF5 files and parse their names."""
    lines = []
    for f in files:
        parsed = parse_flightline(f["name"])
        if parsed is None:
            continue
        lines.append(
            Flightline(
                name=f["name"],
                size=int(f["size"]),
                month=f["month"],
                release=f["release"],
                crc32c=f.get("crc32c"),
                api_site=f.get("api_site", "") if f.get("api_site", "") != parsed["site"] else "",
                **parsed,
            )
        )
    return sorted(lines, key=lambda x: x.name)


def signed_url(site: str, month: str, name: str, token: str | None) -> str:
    """Fresh signed URL for one file; the ones stored in an inventory go stale in 7 days."""
    for f in site_month_files(site, month, token):
        if f["name"] == name:
            return f["url"]
    raise FileNotFoundError(f"{name} not listed for {site} {month}")


def download(url: str, dest: Path, expected_size: int | None = None, retries: int = 3) -> Path:
    """Stream one file to dest. Skips the download if dest already has the expected size."""
    dest = Path(dest)
    if expected_size and dest.exists() and dest.stat().st_size == expected_size:
        print(f"  already present: {dest.name}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(retries):
        try:
            t0 = time.time()
            with requests.get(url, stream=True, timeout=300) as r:
                r.raise_for_status()
                with open(tmp, "wb") as fh:
                    for chunk in r.iter_content(chunk_size=16 << 20):
                        fh.write(chunk)
            size = tmp.stat().st_size
            if expected_size and size != expected_size:
                raise IOError(f"size mismatch: got {size}, expected {expected_size}")
            tmp.replace(dest)
            print(f"  downloaded {dest.name}: {size / 1e9:.2f} GB in {time.time() - t0:.0f}s")
            return dest
        except (requests.RequestException, IOError) as exc:
            print(f"  download failed ({exc}); attempt {attempt + 1}/{retries}")
            tmp.unlink(missing_ok=True)
            time.sleep(30 * (attempt + 1))
    raise IOError(f"could not download {dest.name}")


def fetch_flightline(line: Flightline | dict, dest_dir: Path, token: str | None) -> Path:
    """Download one flightline into dest_dir, re-signing the URL first."""
    if isinstance(line, dict):
        line = Flightline(**line)
    dest = Path(dest_dir) / line.name
    if dest.exists() and dest.stat().st_size == line.size:
        print(f"  already present: {dest.name}")
        return dest
    url = signed_url(line.listing_site, line.month, line.name, token)
    return download(url, dest, line.size)


# --- flight manifests -------------------------------------------------------------------


def write_flight_manifest(flight_id: str, lines: list[Flightline], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "flight_id": flight_id,
        "site": lines[0].site,
        "domain": lines[0].domain,
        "date": lines[0].date,
        "month": lines[0].month,
        "release": lines[0].release,
        "n_lines": len(lines),
        "total_gb": round(sum(x.size for x in lines) / 1e9, 2),
        "lines": [asdict(x) for x in lines],
    }
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")


def read_flight_manifest(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def manifest_line(manifest: dict, stem_or_name: str) -> Flightline:
    """Find one line in a manifest by file name or stem."""
    for row in manifest["lines"]:
        if row["name"] in (stem_or_name, stem_or_name + ".h5"):
            return Flightline(**row)
    raise KeyError(f"{stem_or_name} not in flight {manifest['flight_id']}")
