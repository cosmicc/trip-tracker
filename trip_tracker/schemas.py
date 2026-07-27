from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class WaypointRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str = Field(min_length=1, max_length=160)
    owntracks_region_id: str | None
    latitude: Decimal
    longitude: Decimal
    radius_m: int = Field(default=150, ge=1)


class LocationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user: str | None
    device: str | None
    captured_at: datetime
    latitude: Decimal
    longitude: Decimal
    accuracy_m: int | None


class TripUpdate(BaseModel):
    trip_date: date | None = None
    origin_name: str = Field(min_length=1, max_length=160)
    destination_name: str = Field(min_length=1, max_length=160)
    miles: Decimal | None = Field(default=None, ge=0)


class TripRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    trip_date: date
    started_at: datetime
    ended_at: datetime
    origin_name: str | None
    destination_name: str | None
    miles: Decimal
    start_odometer_miles: Decimal | None
    end_odometer_miles: Decimal | None
    start_odometer_source: str | None
    end_odometer_source: str | None
    mileage_source: str
    source: str
    notes: str


class MonthlyGasPriceCreate(BaseModel):
    year: int = Field(ge=2000)
    month: int = Field(ge=1, le=12)
    average_price_per_gallon: Decimal = Field(ge=0)
    buffer_per_gallon: Decimal = Field(default=Decimal("0.50"), ge=0)
    state: str = "MI"
    source_detail: str = "manual"
