import logging
from calendar import month_name
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from html import escape
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from trip_tracker.config import get_settings
from trip_tracker.models import MonthlyReportExpense, Trip
from trip_tracker.services.gas_prices import get_or_create_monthly_price
from trip_tracker.services.mileage import monthly_miles

logger = logging.getLogger(__name__)

PDF_REPORT_PAGE_SIZE = LETTER
PDF_REPORT_HORIZONTAL_MARGIN = 0.35 * inch
PDF_REPORT_VERTICAL_MARGIN = 0.35 * inch
PDF_TRIP_TABLE_COLUMN_WIDTHS = [65, 145, 145, 75, 75, 50]
PDF_TITLE_TO_IDENTITY_SPACER = 0
PDF_IDENTITY_TO_TABLE_SPACER = 6
PDF_TITLE_TO_TABLE_SPACER = 6
PDF_REIMBURSEMENT_HIGHLIGHT_COLOR = "#fff3b0"


@dataclass(frozen=True)
class TripReportRow:
    trip_date: date
    from_location: str
    to_location: str
    start_odometer: Decimal | None
    end_odometer: Decimal | None
    trip_miles: Decimal


@dataclass(frozen=True)
class ExpenseReportRow:
    """Manual extra expense row rendered after trip rows on the monthly PDF."""

    expense_date: date
    reason: str
    amount: Decimal


@dataclass(frozen=True)
class MonthlyPdfReport:
    filename: str
    content: bytes
    total_miles: Decimal
    reimbursement_total: Decimal


def calculate_reimbursement_gallons(total_miles: Decimal, vehicle_mpg: Decimal) -> Decimal:
    if vehicle_mpg <= 0:
        raise ValueError("vehicle_mpg must be greater than zero")
    return (total_miles / vehicle_mpg).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def calculate_reimbursement(
    total_miles: Decimal,
    monthly_gas_price: Decimal,
    vehicle_mpg: Decimal,
) -> Decimal:
    if vehicle_mpg <= 0:
        raise ValueError("vehicle_mpg must be greater than zero")
    return ((total_miles / vehicle_mpg) * monthly_gas_price).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    end = date(year + int(month == 12), 1 if month == 12 else month + 1, 1)
    return start, end


def _report_month_title(year: int, month: int) -> str:
    """Return the human-readable PDF report month label."""

    return f"{month_name[month]} {year}"


def trips_for_month(db: Session, year: int, month: int) -> list[Trip]:
    start, end = _month_bounds(year, month)
    stmt = (
        select(Trip)
        .options(joinedload(Trip.origin_site), joinedload(Trip.destination_site))
        .where(Trip.trip_date >= start)
        .where(Trip.trip_date < end)
        .order_by(Trip.trip_date.asc(), Trip.started_at.asc(), Trip.id.asc())
    )
    return list(db.scalars(stmt))


def expenses_for_month(db: Session, year: int, month: int) -> list[MonthlyReportExpense]:
    """Return manual extra expenses for one report month in PDF display order."""

    stmt = (
        select(MonthlyReportExpense)
        .where(MonthlyReportExpense.year == year)
        .where(MonthlyReportExpense.month == month)
        .order_by(
            MonthlyReportExpense.expense_date.asc(),
            MonthlyReportExpense.created_at.asc(),
            MonthlyReportExpense.id.asc(),
        )
    )
    return list(db.scalars(stmt))


def extra_expense_total(expenses: list[MonthlyReportExpense]) -> Decimal:
    """Return the two-decimal total for manual extra expenses."""

    total = sum((Decimal(expense.amount) for expense in expenses), Decimal("0.00"))
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _origin_location(trip: Trip) -> str:
    return trip.origin_display_name


def _destination_location(trip: Trip) -> str:
    return trip.destination_display_name


def _odometer_value(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(value).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def _format_odometer(value: Decimal | None) -> str:
    if value is None:
        return "-"
    return f"{value:.1f}"


def _paragraph_text(value: str) -> str:
    """Escape user-managed location text before ReportLab parses Paragraph markup."""

    return escape(value)


def trip_report_rows(trips: list[Trip]) -> list[TripReportRow]:
    rows: list[TripReportRow] = []
    for trip in trips:
        trip_miles = Decimal(trip.miles).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        rows.append(
            TripReportRow(
                trip_date=trip.trip_date,
                from_location=_origin_location(trip),
                to_location=_destination_location(trip),
                start_odometer=_odometer_value(trip.start_odometer_miles),
                end_odometer=_odometer_value(trip.end_odometer_miles),
                trip_miles=trip_miles,
            )
        )
    return rows


def expense_report_rows(expenses: list[MonthlyReportExpense]) -> list[ExpenseReportRow]:
    """Return escaped-ready manual extra expense rows for the PDF report."""

    rows: list[ExpenseReportRow] = []
    for expense in expenses:
        rows.append(
            ExpenseReportRow(
                expense_date=expense.expense_date,
                reason=expense.reason,
                amount=Decimal(expense.amount).quantize(
                    Decimal("0.01"),
                    rounding=ROUND_HALF_UP,
                ),
            )
        )
    return rows


def generate_monthly_pdf(db: Session, year: int, month: int) -> MonthlyPdfReport:
    logger.info("Generating monthly PDF report year=%s month=%s", year, month)
    settings = get_settings()
    gas_price = get_or_create_monthly_price(db, year, month)
    trips = trips_for_month(db, year, month)
    expenses = expenses_for_month(db, year, month)
    total_miles = monthly_miles(db, year, month)
    reimbursement_gallons = calculate_reimbursement_gallons(total_miles, settings.vehicle_mpg)
    mileage_reimbursement_total = calculate_reimbursement(
        total_miles,
        gas_price.average_price_per_gallon,
        settings.vehicle_mpg,
    )
    expense_total = extra_expense_total(expenses)
    reimbursement_total = mileage_reimbursement_total + expense_total
    report_rows = trip_report_rows(trips)
    expense_rows = expense_report_rows(expenses)

    styles = getSampleStyleSheet()
    table_cell = ParagraphStyle(
        "TableCell",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=7,
        leading=8.5,
    )
    report_title = ParagraphStyle(
        "ReportTitle",
        parent=styles["Title"],
        spaceBefore=0,
        spaceAfter=0,
    )
    report_identity = ParagraphStyle(
        "ReportIdentity",
        parent=styles["BodyText"],
        alignment=TA_CENTER,
        fontName="Helvetica",
        fontSize=10,
        leading=12,
    )
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=PDF_REPORT_PAGE_SIZE,
        leftMargin=PDF_REPORT_HORIZONTAL_MARGIN,
        rightMargin=PDF_REPORT_HORIZONTAL_MARGIN,
        topMargin=PDF_REPORT_VERTICAL_MARGIN,
        bottomMargin=PDF_REPORT_VERTICAL_MARGIN,
    )
    report_title_text = f"Mileage &amp; Expense Report - {_report_month_title(year, month)}"
    story = [
        Paragraph(report_title_text, report_title),
    ]
    report_display_name = settings.report_display_name.strip()
    if report_display_name:
        story.extend(
            [
                Spacer(1, PDF_TITLE_TO_IDENTITY_SPACER),
                Paragraph(
                    f"<b>Submitted by:</b> {_paragraph_text(report_display_name)}",
                    report_identity,
                ),
                Spacer(1, PDF_IDENTITY_TO_TABLE_SPACER),
            ]
        )
    else:
        story.append(Spacer(1, PDF_TITLE_TO_TABLE_SPACER))

    trip_rows = [["Date", "From", "To", "Start Odometer", "End Odometer", "Trip Mi"]]
    for row in report_rows:
        trip_rows.append(
            [
                row.trip_date.isoformat(),
                Paragraph(_paragraph_text(row.from_location), table_cell),
                Paragraph(_paragraph_text(row.to_location), table_cell),
                _format_odometer(row.start_odometer),
                _format_odometer(row.end_odometer),
                f"{row.trip_miles:.1f}",
            ]
        )

    if len(trip_rows) == 1:
        trip_rows.append(["", "No trips", "", "", "", "0.0"])

    expense_row_start = len(trip_rows)
    for row in expense_rows:
        trip_rows.append(
            [
                row.expense_date.isoformat(),
                Paragraph(_paragraph_text(row.reason), table_cell),
                "",
                "",
                "",
                f"${row.amount:.2f}",
            ]
        )

    table = Table(trip_rows, repeatRows=1, colWidths=PDF_TRIP_TABLE_COLUMN_WIDTHS)
    table_style_commands = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#101828")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d0d5dd")),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (3, 1), (-1, -1), "RIGHT"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
    ]
    for row_index in range(expense_row_start, len(trip_rows)):
        table_style_commands.extend(
            [
                ("SPAN", (1, row_index), (4, row_index)),
                ("ALIGN", (5, row_index), (5, row_index), "RIGHT"),
            ]
        )
    table.setStyle(
        TableStyle(table_style_commands)
    )
    story.append(table)
    story.append(Spacer(1, 18))

    summary_data = [
        ["Michigan Avg Monthly Gas Price", f"${gas_price.average_price_per_gallon:.3f}"],
        ["Vehicle MPG", f"{settings.vehicle_mpg:.1f}"],
        ["Total trip miles for month", f"{total_miles:.1f}"],
        ["Reimbursement gallons", f"{reimbursement_gallons:.3f}"],
        ["Mileage reimbursement", f"${mileage_reimbursement_total:.2f}"],
        ["Extra expense total", f"${expense_total:.2f}"],
        ["Total reimbursement", f"${reimbursement_total:.2f}"],
    ]
    summary = Table(summary_data, hAlign="RIGHT", colWidths=[220, 120])
    summary.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f2f4f7")),
                (
                    "BACKGROUND",
                    (1, -1),
                    (1, -1),
                    colors.HexColor(PDF_REIMBURSEMENT_HIGHLIGHT_COLOR),
                ),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#d0d5dd")),
                ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story.append(summary)
    doc.build(story)
    report = MonthlyPdfReport(
        filename=f"mileage-{year}-{month:02d}.pdf",
        content=buffer.getvalue(),
        total_miles=total_miles,
        reimbursement_total=reimbursement_total,
    )
    logger.info(
        "Generated monthly PDF report year=%s month=%s trips=%s total_miles=%s "
        "reimbursement_total=%s",
        year,
        month,
        len(trips),
        total_miles,
        reimbursement_total,
    )
    return report
