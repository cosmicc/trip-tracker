from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

UNKNOWN_LOCATION_NAME = "Unknown"
AUTOMATIC_TRIP_PROCESSING_CHECKPOINT = "automatic_trip_processing"


def normalize_location_name(value: str | None) -> str:
    cleaned = (value or "").strip()
    return cleaned or UNKNOWN_LOCATION_NAME


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class OwnTracksLocation(Base):
    __tablename__ = "owntracks_locations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user: Mapped[str | None] = mapped_column(String(120), nullable=True)
    device: Mapped[str | None] = mapped_column(String(120), nullable=True)
    topic: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tracker_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    latitude: Mapped[Decimal] = mapped_column(Numeric(10, 7))
    longitude: Mapped[Decimal] = mapped_column(Numeric(10, 7))
    odometer_miles: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 1),
        nullable=True,
        comment="Rolling OwnTracks-derived odometer value after this location row is processed.",
    )
    odometer_source: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
        comment="Source label for the rolling odometer value stored on this location row.",
    )
    accuracy_m: Mapped[int | None] = mapped_column(Integer, nullable=True)
    battery_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_payload: Mapped[dict] = mapped_column(JSON)


class Site(Base):
    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), unique=True)
    owntracks_region_id: Mapped[str | None] = mapped_column(String(80), unique=True, nullable=True)
    latitude: Mapped[Decimal] = mapped_column(Numeric(10, 7))
    longitude: Mapped[Decimal] = mapped_column(Numeric(10, 7))
    radius_m: Mapped[int] = mapped_column(Integer, default=150)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_visited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    origin_trips: Mapped[list["Trip"]] = relationship(
        back_populates="origin_site", foreign_keys="Trip.origin_site_id"
    )
    destination_trips: Mapped[list["Trip"]] = relationship(
        back_populates="destination_site", foreign_keys="Trip.destination_site_id"
    )


class Trip(Base):
    __tablename__ = "trips"
    __table_args__ = (
        Index(
            "uq_trips_auto_generation_signature",
            "origin_site_id",
            "destination_site_id",
            "started_at",
            "ended_at",
            unique=True,
            postgresql_where=text("source = 'auto'"),
            sqlite_where=text("source = 'auto'"),
        ),
        Index(
            "uq_trips_auto_recorded_values",
            "trip_date",
            "origin_site_id",
            "destination_site_id",
            "miles",
            "start_odometer_miles",
            "end_odometer_miles",
            unique=True,
            postgresql_where=text(
                "source = 'auto' AND start_odometer_miles IS NOT NULL "
                "AND end_odometer_miles IS NOT NULL"
            ),
            sqlite_where=text(
                "source = 'auto' AND start_odometer_miles IS NOT NULL "
                "AND end_odometer_miles IS NOT NULL"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trip_date: Mapped[date] = mapped_column(Date, index=True)
    origin_site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id"), nullable=True)
    destination_site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id"), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    start_latitude: Mapped[Decimal] = mapped_column(Numeric(10, 7))
    start_longitude: Mapped[Decimal] = mapped_column(Numeric(10, 7))
    end_latitude: Mapped[Decimal] = mapped_column(Numeric(10, 7))
    end_longitude: Mapped[Decimal] = mapped_column(Numeric(10, 7))
    origin_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    destination_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    miles: Mapped[Decimal] = mapped_column(Numeric(9, 1))
    start_odometer_miles: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 1), nullable=True
    )
    end_odometer_miles: Mapped[Decimal | None] = mapped_column(Numeric(12, 1), nullable=True)
    start_odometer_source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    end_odometer_source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    mileage_source: Mapped[str] = mapped_column(String(40), default="waypoint_distance")
    source: Mapped[str] = mapped_column(String(40), default="auto")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    origin_site: Mapped[Site | None] = relationship(
        back_populates="origin_trips", foreign_keys=[origin_site_id]
    )
    destination_site: Mapped[Site | None] = relationship(
        back_populates="destination_trips", foreign_keys=[destination_site_id]
    )

    @property
    def origin_display_name(self) -> str:
        if self.origin_name and self.origin_name.strip():
            return self.origin_name.strip()
        if self.origin_site is not None:
            return self.origin_site.name
        return UNKNOWN_LOCATION_NAME

    @property
    def destination_display_name(self) -> str:
        if self.destination_name and self.destination_name.strip():
            return self.destination_name.strip()
        if self.destination_site is not None:
            return self.destination_site.name
        return UNKNOWN_LOCATION_NAME


class DeletedTrip(Base):
    """Trip deletion tombstone that blocks automatic recreation from source events."""

    __tablename__ = "deleted_trips"
    __table_args__ = (
        UniqueConstraint(
            "origin_site_id",
            "destination_site_id",
            "started_at",
            "ended_at",
            name="uq_deleted_trip_generation_signature",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    deleted_trip_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Original trips.id value for operator audit after the visible row is deleted.",
    )
    trip_date: Mapped[date] = mapped_column(
        Date,
        index=True,
        comment="Local trip date for the deleted trip.",
    )
    origin_site_id: Mapped[int | None] = mapped_column(
        ForeignKey("sites.id"),
        nullable=True,
        comment="Origin waypoint id used in the automatic generation signature.",
    )
    destination_site_id: Mapped[int | None] = mapped_column(
        ForeignKey("sites.id"),
        nullable=True,
        comment="Destination waypoint id used in the automatic generation signature.",
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        index=True,
        comment="Trip start timestamp used in the automatic generation signature.",
    )
    ended_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        index=True,
        comment="Trip end timestamp used in the automatic generation signature.",
    )
    origin_name: Mapped[str | None] = mapped_column(
        String(160),
        nullable=True,
        comment="Displayed origin name at deletion time.",
    )
    destination_name: Mapped[str | None] = mapped_column(
        String(160),
        nullable=True,
        comment="Displayed destination name at deletion time.",
    )
    miles: Mapped[Decimal | None] = mapped_column(
        Numeric(9, 1),
        nullable=True,
        comment="Displayed trip miles at deletion time.",
    )
    source: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
        comment="Trip source at deletion time.",
    )
    mileage_source: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
        comment="Mileage source at deletion time.",
    )
    reason: Mapped[str] = mapped_column(
        String(80),
        default="user_deleted",
        comment="Reason this automatic trip signature should stay suppressed.",
    )
    deleted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        comment="Timestamp the visible trip row was deleted.",
    )
    notes: Mapped[str] = mapped_column(
        Text,
        default="",
        comment="Trip notes copied at deletion time.",
    )


class TripProcessingCheckpoint(Base):
    __tablename__ = "trip_processing_checkpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    last_owntracks_location_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    odometer_anchor_miles: Mapped[Decimal | None] = mapped_column(Numeric(12, 1), nullable=True)
    odometer_anchor_recorded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class GasPriceSnapshot(Base):
    __tablename__ = "gas_price_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    observed_on: Mapped[date] = mapped_column(Date)
    state: Mapped[str] = mapped_column(String(2))
    grade: Mapped[str] = mapped_column(String(40), default="regular")
    price_per_gallon: Mapped[Decimal] = mapped_column(Numeric(6, 3))
    source: Mapped[str] = mapped_column(String(80))
    source_detail: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class MonthlyGasPrice(Base):
    __tablename__ = "monthly_gas_prices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    year: Mapped[int] = mapped_column(Integer)
    month: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(2), default="MI")
    average_price_per_gallon: Mapped[Decimal] = mapped_column(Numeric(6, 3))
    buffer_per_gallon: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("0.50"))
    effective_rate: Mapped[Decimal] = mapped_column(Numeric(6, 3))
    source: Mapped[str] = mapped_column(String(80))
    source_detail: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class MonthlyReportExpense(Base):
    """Manual non-mileage expense line included in one monthly PDF report."""

    __tablename__ = "monthly_report_expenses"
    __table_args__ = (
        Index("ix_monthly_report_expenses_year_month", "year", "month"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    expense_date: Mapped[date] = mapped_column(
        Date,
        index=True,
        comment="Local expense date used to select the monthly PDF report.",
    )
    year: Mapped[int] = mapped_column(Integer)
    month: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(
        String(160),
        comment="Operator-entered expense reason shown on the monthly PDF report.",
    )
    amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        comment="Expense amount added to the monthly reimbursement total.",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class OwnTracksMonthlySummary(Base):
    """Stable monthly OwnTracks-derived totals retained after raw location cleanup."""

    __tablename__ = "owntracks_monthly_summaries"
    __table_args__ = (
        UniqueConstraint("year", "month", name="uq_owntracks_monthly_summary_month"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    year: Mapped[int] = mapped_column(Integer)
    month: Mapped[int] = mapped_column(Integer)
    total_miles: Mapped[Decimal] = mapped_column(Numeric(12, 1), default=Decimal("0.0"))
    event_count: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(80), default="owntracks_retention_rollup")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class CloudflareIPBlock(Base):
    """App-managed Cloudflare zone IP Access Rule block."""

    __tablename__ = "cloudflare_ip_blocks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ip_address: Mapped[str] = mapped_column(String(45), unique=True, index=True)
    cloudflare_rule_id: Mapped[str] = mapped_column(String(80), unique=True)
    source: Mapped[str] = mapped_column(
        String(40),
        default="manual",
        comment="How this app-managed Cloudflare IP block was created.",
    )
    reason: Mapped[str] = mapped_column(String(160), default="")
    failure_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class HiddenLoginFailure(Base):
    """Failed-login audit entry hidden from the Diagnostics list without deleting its audit row."""

    __tablename__ = "hidden_login_failures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entry_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    client_ip: Mapped[str] = mapped_column(String(45), default="")
    occurred_at_utc: Mapped[str] = mapped_column(String(40), default="")
    hidden_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class WebLoginAudit(Base):
    """Database-backed structured audit record for a web login attempt."""

    __tablename__ = "web_login_audits"
    __table_args__ = (
        Index("ix_web_login_audits_event_occurred_at", "event", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entry_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    event: Mapped[str] = mapped_column(String(40), index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class PasskeyCredential(Base):
    """WebAuthn passkey credential for the single configured web-login user."""

    __tablename__ = "passkey_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    credential_id: Mapped[str] = mapped_column(String(2048), unique=True, index=True)
    user_handle: Mapped[str] = mapped_column(String(128), index=True)
    username: Mapped[str] = mapped_column(String(256))
    public_key: Mapped[str] = mapped_column(Text)
    sign_count: Mapped[int] = mapped_column(Integer, default=0)
    transports: Mapped[list[str]] = mapped_column(JSON, default=list)
    aaguid: Mapped[str] = mapped_column(String(80), default="")
    credential_type: Mapped[str] = mapped_column(String(40), default="public-key")
    device_type: Mapped[str] = mapped_column(String(40), default="")
    backed_up: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
