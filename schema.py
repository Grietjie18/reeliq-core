from pydantic import BaseModel

class Observation(BaseModel):
    timestamp: str  # The date and time of the reading
    latitude: float
    longitude: float
    vessel_speed_knots: float
    temp_c: float