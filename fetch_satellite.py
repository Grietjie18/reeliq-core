import copernicusmarine
import numpy as np
import json
import os
import xarray as xr
from datetime import date, timedelta
from shapely.geometry import Point, Polygon

ALGOA_BAY_POLYGON = Polygon([
    (24.869, -34.195), (24.841, -34.145), (24.912, -34.085),
    (24.921, -34.079), (24.925, -34.052), (24.933, -34.032),
    (24.931, -34.011), (24.937, -34.005), (25.034, -33.970),
    (25.213, -33.969), (25.402, -34.034), (25.584, -34.048),
    (25.700, -34.029), (25.644, -33.955), (25.632, -33.865),
    (25.694, -33.815), (25.830, -33.727), (26.080, -33.707),
    (26.298, -33.763), (26.352, -33.760),
    (26.352, -34.300), (24.869, -34.300), (24.869, -34.195),
])

def is_ocean(lon, lat):
    return ALGOA_BAY_POLYGON.contains(Point(lon, lat))

def fetch():
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    print(f"Fetching Copernicus SST for {yesterday}...")

    copernicusmarine.subset(
        dataset_id="METOFFICE-GLO-SST-L4-NRT-OBS-SST-V2",
        variables=["analysed_sst"],
        minimum_longitude=24.840,
        maximum_longitude=26.360,
        minimum_latitude=-34.300,
        maximum_latitude=-33.700,
        start_datetime=yesterday,
        end_datetime=yesterday,
        output_filename="sst_raw.nc",
        output_directory=".",
    )

    ds = xr.open_dataset("sst_raw.nc")
    sst_data = ds['analysed_sst'].isel(time=0)
    ds.close()

    lats = sst_data.latitude.values
    lons = sst_data.longitude.values

    points = []
    for i, lat in enumerate(lats):
        for j, lon in enumerate(lons):
            val = float(sst_data.values[i, j])
            if np.isnan(val):
                continue
            temp_c = round(val - 273.15, 3)
            if is_ocean(float(lon), float(lat)):
                points.append([float(lat), float(lon), temp_c])

    output = {"date": yesterday, "points": points}
    with open("satellite_sst.json", "w") as f:
        json.dump(output, f)

    print(f"✅ Saved {len(points)} points to satellite_sst.json")
    os.remove("sst_raw.nc")

if __name__ == "__main__":
    fetch()
