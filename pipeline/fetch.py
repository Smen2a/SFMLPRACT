"""Download the raw public datasets used by build.py into pipeline/raw/.

Everything here is public and needs no API key. Files that already exist are
skipped, so re-running is cheap.
"""
from pathlib import Path
import zipfile

import requests

RAW = Path(__file__).parent / "raw"

SOURCES = {
    # Census TIGER/Line. 2020 vintage still uses CT's 8 historical counties.
    "tl_2020_us_county.zip": "https://www2.census.gov/geo/tiger/TIGER2020/COUNTY/tl_2020_us_county.zip",
    "tl_2020_09_cousub.zip": "https://www2.census.gov/geo/tiger/TIGER2020/COUSUB/tl_2020_09_cousub.zip",
    # Cartographic boundary versions, clipped to the shoreline, for drawing the map.
    "cb_2020_us_county_500k.zip": "https://www2.census.gov/geo/tiger/GENZ2020/shp/cb_2020_us_county_500k.zip",
    "cb_2020_09_cousub_500k.zip": "https://www2.census.gov/geo/tiger/GENZ2020/shp/cb_2020_09_cousub_500k.zip",
    # LEAD 2022 tract IDs use the 2022 planning-region county codes, so tracts come from 2022.
    "tl_2022_09_tract.zip": "https://www2.census.gov/geo/tiger/TIGER2022/TRACT/tl_2022_09_tract.zip",
    # DOE Low-Income Energy Affordability Data (LEAD) Tool, 2022 update, Connecticut.
    "CT-2022-LEAD-data.zip": "https://data.openei.org/files/6219/CT-2022-LEAD-data.zip",
    # EIA price series.
    "eia/sep_ct.xlsx": "https://www.eia.gov/electricity/state/xls/SEP%20Tables%20for%20CT.xlsx",
    "eia/N3010CT3a.xls": "https://www.eia.gov/dnav/ng/hist_xls/N3010CT3a.xls",
    "eia/heating_oil_weekly.xls": "https://www.eia.gov/dnav/pet/xls/PET_PRI_WFR_A_EPD2F_PRS_DPGAL_W.xls",
    "eia/propane_weekly.xls": "https://www.eia.gov/dnav/pet/xls/PET_PRI_WFR_A_EPLLPA_PRS_DPGAL_W.xls",
}

LEAD_MEMBER = "CT AMI Census Tracts 2022.csv"


def download(name: str, url: str) -> Path:
    dest = RAW / name
    if dest.exists() and dest.stat().st_size > 0:
        print(f"have  {name}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"fetch {name}")
    with requests.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    return dest


def main() -> None:
    for name, url in SOURCES.items():
        download(name, url)
    lead_csv = RAW / "lead" / LEAD_MEMBER
    if not lead_csv.exists():
        with zipfile.ZipFile(RAW / "CT-2022-LEAD-data.zip") as z:
            z.extract(LEAD_MEMBER, RAW / "lead")


if __name__ == "__main__":
    main()
