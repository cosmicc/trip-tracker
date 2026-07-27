from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from reportlab.lib import colors
from reportlab.platypus import Paragraph as ReportLabParagraph
from reportlab.platypus import TableStyle as ReportLabTableStyle
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from trip_tracker.config import Settings
from trip_tracker.models import Base, MonthlyGasPrice, MonthlyReportExpense, Site, Trip
from trip_tracker.services import pdf as pdf_service
from trip_tracker.services.pdf import (
    PDF_IDENTITY_TO_TABLE_SPACER,
    PDF_REIMBURSEMENT_HIGHLIGHT_COLOR,
    PDF_REPORT_HORIZONTAL_MARGIN,
    PDF_REPORT_PAGE_SIZE,
    PDF_REPORT_VERTICAL_MARGIN,
    PDF_TITLE_TO_IDENTITY_SPACER,
    PDF_TITLE_TO_TABLE_SPACER,
    PDF_TRIP_TABLE_COLUMN_WIDTHS,
    _report_month_title,
    calculate_reimbursement,
    calculate_reimbursement_gallons,
    generate_monthly_pdf,
    trip_report_rows,
)


def test_calculate_reimbursement_uses_requested_formula() -> None:
    assert calculate_reimbursement_gallons(Decimal("120.50"), Decimal("25.0")) == Decimal("4.820")
    assert calculate_reimbursement(
        Decimal("120.50"),
        Decimal("4.250"),
        Decimal("25.0"),
    ) == Decimal("20.49")


def test_pdf_report_layout_uses_portrait_letter_width() -> None:
    available_width = PDF_REPORT_PAGE_SIZE[0] - (PDF_REPORT_HORIZONTAL_MARGIN * 2)

    assert PDF_REPORT_PAGE_SIZE[0] < PDF_REPORT_PAGE_SIZE[1]
    assert PDF_REPORT_HORIZONTAL_MARGIN == PDF_REPORT_VERTICAL_MARGIN
    assert sum(PDF_TRIP_TABLE_COLUMN_WIDTHS) <= available_width
    assert PDF_TITLE_TO_IDENTITY_SPACER == 0
    assert PDF_IDENTITY_TO_TABLE_SPACER == 6
    assert PDF_TITLE_TO_TABLE_SPACER <= 8


def test_pdf_report_month_title_uses_month_name_and_year() -> None:
    assert _report_month_title(2026, 6) == "June 2026"


def test_trip_report_rows_include_trip_mileage() -> None:
    origin = Site(
        name="Shop",
        latitude=Decimal("42.3314"),
        longitude=Decimal("-83.0458"),
        radius_m=150,
    )
    client = Site(
        name="Client",
        latitude=Decimal("42.3440"),
        longitude=Decimal("-83.0600"),
        radius_m=150,
    )
    started_at = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    trips = [
        Trip(
            trip_date=date(2026, 6, 11),
            origin_site=origin,
            destination_site=client,
            started_at=started_at,
            ended_at=started_at + timedelta(minutes=20),
            start_latitude=origin.latitude,
            start_longitude=origin.longitude,
            end_latitude=client.latitude,
            end_longitude=client.longitude,
            start_odometer_miles=Decimal("1000.100"),
            end_odometer_miles=Decimal("1012.600"),
            miles=Decimal("12.50"),
        ),
        Trip(
            trip_date=date(2026, 6, 11),
            origin_site=client,
            destination_site=origin,
            started_at=started_at + timedelta(hours=2),
            ended_at=started_at + timedelta(hours=2, minutes=20),
            start_latitude=client.latitude,
            start_longitude=client.longitude,
            end_latitude=origin.latitude,
            end_longitude=origin.longitude,
            start_odometer_miles=Decimal("1012.600"),
            end_odometer_miles=Decimal("1019.850"),
            miles=Decimal("7.25"),
        ),
    ]

    rows = trip_report_rows(trips)

    assert rows[0].from_location == "Shop"
    assert rows[0].to_location == "Client"
    assert rows[0].start_odometer == Decimal("1000.1")
    assert rows[0].end_odometer == Decimal("1012.6")
    assert rows[0].trip_miles == Decimal("12.5")
    assert rows[1].from_location == "Client"
    assert rows[1].to_location == "Shop"
    assert rows[1].start_odometer == Decimal("1012.6")
    assert rows[1].end_odometer == Decimal("1019.9")
    assert rows[1].trip_miles == Decimal("7.3")


def test_trip_report_rows_use_unknown_for_unresolved_sites() -> None:
    started_at = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    trip = Trip(
        trip_date=date(2026, 6, 11),
        started_at=started_at,
        ended_at=started_at + timedelta(minutes=20),
        start_latitude=Decimal("42.3314"),
        start_longitude=Decimal("-83.0458"),
        end_latitude=Decimal("42.3440"),
        end_longitude=Decimal("-83.0600"),
        miles=Decimal("12.50"),
    )

    rows = trip_report_rows([trip])

    assert rows[0].from_location == "Unknown"
    assert rows[0].to_location == "Unknown"
    assert rows[0].start_odometer is None
    assert rows[0].end_odometer is None


def test_trip_report_rows_use_trip_location_name_overrides() -> None:
    site = Site(
        name="Original Site",
        latitude=Decimal("42.3314"),
        longitude=Decimal("-83.0458"),
        radius_m=150,
    )
    started_at = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    trip = Trip(
        trip_date=date(2026, 6, 11),
        origin_site=site,
        destination_site=site,
        origin_name="Edited Start",
        destination_name="Edited End",
        started_at=started_at,
        ended_at=started_at + timedelta(minutes=20),
        start_latitude=Decimal("42.3314"),
        start_longitude=Decimal("-83.0458"),
        end_latitude=Decimal("42.3440"),
        end_longitude=Decimal("-83.0600"),
        miles=Decimal("12.50"),
    )

    rows = trip_report_rows([trip])

    assert rows[0].from_location == "Edited Start"
    assert rows[0].to_location == "Edited End"


def test_generate_monthly_pdf_escapes_location_markup() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    started_at = datetime(2026, 6, 11, 13, 0, tzinfo=UTC)
    with session_factory() as db:
        origin = Site(
            name="Shop <unclosed",
            latitude=Decimal("42.3314"),
            longitude=Decimal("-83.0458"),
            radius_m=150,
        )
        client = Site(
            name='Client <a href="https://example.invalid">link</a> & Sons',
            latitude=Decimal("42.3440"),
            longitude=Decimal("-83.0600"),
            radius_m=150,
        )
        db.add_all([origin, client])
        db.flush()
        db.add(
            Trip(
                trip_date=date(2026, 6, 11),
                origin_site=origin,
                destination_site=client,
                started_at=started_at,
                ended_at=started_at + timedelta(minutes=20),
                start_latitude=origin.latitude,
                start_longitude=origin.longitude,
                end_latitude=client.latitude,
                end_longitude=client.longitude,
                miles=Decimal("12.50"),
            )
        )
        db.add(
            MonthlyGasPrice(
                year=2026,
                month=6,
                state="MI",
                average_price_per_gallon=Decimal("3.500"),
                buffer_per_gallon=Decimal("0.50"),
                effective_rate=Decimal("4.000"),
                source="manual",
                source_detail="test",
            )
        )
        db.commit()

        report = generate_monthly_pdf(db, 2026, 6)

    assert report.filename == "mileage-2026-06.pdf"
    assert report.content.startswith(b"%PDF")
    assert report.total_miles == Decimal("12.5")


def test_generate_monthly_pdf_adds_configured_report_display_name(monkeypatch) -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    rendered_paragraphs: list[str] = []

    def record_paragraph(text, style, *args, **kwargs):
        rendered_paragraphs.append(str(text))
        return ReportLabParagraph(text, style, *args, **kwargs)

    monkeypatch.setattr(
        pdf_service,
        "get_settings",
        lambda: Settings(
            database_url="sqlite://",
            report_display_name=" Jane <Tech> & Co ",
        ),
    )
    monkeypatch.setattr(pdf_service, "Paragraph", record_paragraph)

    with session_factory() as db:
        db.add(
            MonthlyGasPrice(
                year=2026,
                month=7,
                state="MI",
                average_price_per_gallon=Decimal("3.500"),
                buffer_per_gallon=Decimal("0.50"),
                effective_rate=Decimal("4.000"),
                source="manual",
                source_detail="test",
            )
        )
        db.commit()

        report = generate_monthly_pdf(db, 2026, 7)

    assert report.filename == "mileage-2026-07.pdf"
    assert report.content.startswith(b"%PDF")
    assert "Mileage &amp; Expense Report - July 2026" in rendered_paragraphs
    assert "Mileage &amp; Expense Report - 2026-07" not in rendered_paragraphs
    assert "<b>Submitted by:</b> Jane &lt;Tech&gt; &amp; Co" in rendered_paragraphs


def test_generate_monthly_pdf_adds_extra_expenses_to_report_total(monkeypatch) -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    captured_tables: list[list[list[object]]] = []
    captured_styles: list[list[tuple]] = []
    original_table = pdf_service.Table

    def record_table(data, *args, **kwargs):
        captured_tables.append(data)
        return original_table(data, *args, **kwargs)

    def record_table_style(commands):
        captured_styles.append(list(commands))
        return ReportLabTableStyle(commands)

    monkeypatch.setattr(pdf_service, "Table", record_table)
    monkeypatch.setattr(pdf_service, "TableStyle", record_table_style)

    with session_factory() as db:
        db.add_all(
            [
                Trip(
                    trip_date=date(2026, 7, 6),
                    started_at=datetime(2026, 7, 6, 13, 0, tzinfo=UTC),
                    ended_at=datetime(2026, 7, 6, 13, 30, tzinfo=UTC),
                    start_latitude=Decimal("42.3314"),
                    start_longitude=Decimal("-83.0458"),
                    end_latitude=Decimal("42.3440"),
                    end_longitude=Decimal("-83.0600"),
                    miles=Decimal("25.0"),
                ),
                MonthlyReportExpense(
                    expense_date=date(2026, 7, 7),
                    year=2026,
                    month=7,
                    reason="Parking <garage> & tolls",
                    amount=Decimal("12.345"),
                ),
                MonthlyGasPrice(
                    year=2026,
                    month=7,
                    state="MI",
                    average_price_per_gallon=Decimal("3.500"),
                    buffer_per_gallon=Decimal("0.50"),
                    effective_rate=Decimal("4.000"),
                    source="manual",
                    source_detail="test",
                ),
            ]
        )
        db.commit()

        report = generate_monthly_pdf(db, 2026, 7)

    assert report.content.startswith(b"%PDF")
    assert report.reimbursement_total == Decimal("15.85")
    expense_row_index = None
    assert any(
        row[0] == "2026-07-07"
        and isinstance(row[1], ReportLabParagraph)
        and row[1].text == "Parking &lt;garage&gt; &amp; tolls"
        and row[5] == "$12.35"
        for table in captured_tables
        for row in table
    )
    for index, row in enumerate(captured_tables[0]):
        if row[0] == "2026-07-07":
            expense_row_index = index
            break

    assert expense_row_index is not None
    assert ("SPAN", (1, expense_row_index), (4, expense_row_index)) in captured_styles[0]
    assert not any(
        command[0] == "BACKGROUND"
        and command[1] == (0, expense_row_index)
        and command[2] == (-1, expense_row_index)
        for command in captured_styles[0]
    )
    assert ["Extra expense total", "$12.35"] in captured_tables[-1]
    assert ["Total reimbursement", "$15.85"] in captured_tables[-1]


def test_generate_monthly_pdf_highlights_total_reimbursement_value(monkeypatch) -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    captured_styles: list[list[tuple]] = []

    def record_table_style(commands):
        captured_styles.append(list(commands))
        return ReportLabTableStyle(commands)

    monkeypatch.setattr(pdf_service, "TableStyle", record_table_style)

    with session_factory() as db:
        db.add(
            MonthlyGasPrice(
                year=2026,
                month=7,
                state="MI",
                average_price_per_gallon=Decimal("3.500"),
                buffer_per_gallon=Decimal("0.50"),
                effective_rate=Decimal("4.000"),
                source="manual",
                source_detail="test",
            )
        )
        db.commit()

        report = generate_monthly_pdf(db, 2026, 7)

    assert report.content.startswith(b"%PDF")
    assert any(
        command[0] == "BACKGROUND"
        and command[1] == (1, -1)
        and command[2] == (1, -1)
        and command[3].hexval() == colors.HexColor(PDF_REIMBURSEMENT_HIGHLIGHT_COLOR).hexval()
        for commands in captured_styles
        for command in commands
    )
