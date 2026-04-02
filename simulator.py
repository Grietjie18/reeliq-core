import asyncio, httpx, random, uuid, math
from datetime import datetime, timezone

TARGET_URL = "https://reeliq-core.onrender.com/ingest/"
API_KEY = "2026_Reeliq_dev18"
VESSEL_COUNT = 50

LAT_RANGE = (-34.180, -33.850)
LON_RANGE = (25.400, 26.200)

def algoa_bay_sst(lat, lon):
    lon_effect = (lon - 25.400) / (26.200 - 25.400) * 2.5
    lat_effect = (lat + 33.850) / (34.180 - 33.850) * -1.5
    upwelling = 0
    if lon < 25.700 and lat < -34.000:
        upwelling = -2.0 * (1 - (lon - 25.400) / 0.300)
    noise = random.uniform(-0.15, 0.15)
    return round(19.0 + lon_effect + lat_effect + upwelling + noise, 2)

def algoa_bay_salinity(lat, lon):
    base = 35.2
    lon_effect = (lon - 25.400) / (26.200 - 25.400) * 0.3
    dist_sundays = math.sqrt((lat - (-33.90))**2 + (lon - 25.55)**2)
    river_plume = max(0, 0.6 * (1 - dist_sundays / 0.25))
    upwelling_salt = 0
    if lon < 25.700 and lat < -34.000:
        upwelling_salt = 0.15 * (1 - (lon - 25.400) / 0.300)
    noise = random.uniform(-0.05, 0.05)
    return round(base + lon_effect - river_plume + upwelling_salt + noise, 2)

class ResearchVessel:
    def __init__(self, v_id):
        self.v_id = v_id
        self.lat = random.uniform(*LAT_RANGE)
        self.lon = random.uniform(*LON_RANGE)
        self.heading = random.uniform(0, 360)
        self.speed_knots = random.uniform(4, 12)
        self.base_speed = random.uniform(4, 10)
        self.fishing = False
        self.fishing_timer = 0

    def move(self):
        if self.fishing:
            self.fishing_timer -= 1
            self.speed_knots = random.uniform(1, 2)
            if self.fishing_timer <= 0:
                self.fishing = False
                self.speed_knots = self.base_speed
        else:
            if random.random() < 0.02:
                self.fishing = True
                self.fishing_timer = random.randint(10, 30)
            self.heading += random.uniform(-8, 8)
            self.heading %= 360
            self.speed_knots = self.base_speed + random.uniform(-0.5, 0.5)

        step = (self.speed_knots * 0.000277) * (5 / 3600) * 111000 / 111000
        self.lat += step * math.cos(math.radians(self.heading))
        self.lon += step * math.sin(math.radians(self.heading)) / math.cos(math.radians(self.lat))

        if self.lat > LAT_RANGE[1]: self.lat = LAT_RANGE[1]; self.heading = 180
        if self.lat < LAT_RANGE[0]: self.lat = LAT_RANGE[0]; self.heading = 0
        if self.lon < LON_RANGE[0]: self.lon = LON_RANGE[0]; self.heading = 90
        if self.lon > LON_RANGE[1]: self.lon = LON_RANGE[1]; self.heading = 270

    def get_data(self):
        self.move()
        sog = round(self.speed_knots + random.uniform(-0.1, 0.1), 2)
        stw = round(sog + random.uniform(-0.5, 0.5), 2)
        sst = algoa_bay_sst(self.lat, self.lon)
        salinity = algoa_bay_salinity(self.lat, self.lon)

        return {
            "observation_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "latitude": round(self.lat, 6),
            "longitude": round(self.lon, 6),
            "sea_surface_temperature": sst,
            "sea_surface_salinity": salinity,
            "speed_over_ground": sog,
            "speed_through_water": stw,
            "heading": round(self.heading, 1),
            "qc_flag": 0
        }

async def run_sim():
    print(f"🚀 REEL IQ Simulator: Algoa Bay SST + Salinity + Heading Active...")
    vessels = [ResearchVessel(f"RV_Algoa_{i}") for i in range(VESSEL_COUNT)]

    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            tasks = []
            for v in vessels:
                payload = v.get_data()
                url = f"{TARGET_URL}{v.v_id}?api_key={API_KEY}"
                tasks.append(client.post(url, json=payload))

            results = await asyncio.gather(*tasks, return_exceptions=True)
            successes = sum(1 for r in results if not isinstance(r, Exception) and r.status_code == 200)
            print(f"📡 Sync Cycle: {successes}/{VESSEL_COUNT} vessels reported.")
            await asyncio.sleep(5)

if __name__ == "__main__":
    try:
        asyncio.run(run_sim())
    except KeyboardInterrupt:
        print("Simulator Paused.")
