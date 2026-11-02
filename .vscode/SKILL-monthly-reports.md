# Skill: Monthly Reports and Reimbursement Submission

Read this guide when changing PDF downloads, report inputs, submission state, or odometer
maintenance that could affect a submitted report.

## Ownership and workflow

- `services/report_status.py` owns `MonthlyReportStatus`, PDF download fingerprints, and report
  locks. `POST /reports/{year}/{month}/submission` accepts an explicit `submitted` boolean.
- Submission requires a completed app-local month and download evidence matching current saved
  report inputs. The app records successful PDF responses, not browser file saves or employer
  delivery. Do not mark a report submitted automatically on download.
- Unsubmission clears download evidence. Report input edits invalidate the fingerprint, so a fresh
  PDF is required before submission. Keep the Work Trips selected-month status and accessible
  explanation beside the report controls. The download script refreshes the page after delivery
  starts so the submission control updates.

## Security and concurrent writes

- Both web and bearer-authenticated API PDF downloads create monthly gas prices first, then take
  the shared PostgreSQL transaction advisory lock before reading report inputs and recording
  download evidence. Responses use `Cache-Control: no-store`.
- Submission takes the same lock. `Session.before_flush` guards submitted trip, expense,
  deleted-trip, and monthly gas rows for all ORM writers, including indirect odometer changes.
  Legacy trip names that fall back to waypoint names are included in fingerprints; relevant
  waypoint renames/deletions must also respect submitted-month locks. Keep this guard imported
  by app and CLI writing paths. An affected submitted month rejects the
  whole edit transaction until explicitly unsubmitted.
- Bulk/Core writes bypass ORM guards. Do not use them for normal report edits. Full backup restore
  remains an explicitly confirmed whole-database replacement operation.
- Automatic generation and missing/overlapping odometer repair skip submitted months. A repair
  that would overlap a later submitted month is rejected before generation commits. Manual
  maintenance that would alter submitted report rows must be blocked until they are unsubmitted.
- No changes to authentication, authorization, or the approved palette are needed for this flow.

## Odometers, migrations, and backups

- Mileage edits check the predecessor and propagate forward across months, preserving positive
  non-trip gaps and saved mileage. Repair existing overlaps by moving starts forward and setting
  each end to start plus saved mileage. Never change the master checkpoint from trip repair.
- Deleting a trip leaves every other trip's odometers unchanged.
- `20261101_0022` adds `monthly_report_statuses`. Full backups include workflow state;
  pre-1.5.3 backups restore with an empty status table.

## Validation

Run `tests/test_report_status.py` plus the web, mileage, backup, and PDF suites. Validate migration
upgrade/rollback against disposable PostgreSQL. Check concurrent submission and report-input
writes with separate sessions. Browser checks must exercise PDF download, submission, locked
controls, unsubmission, fresh-download requirements, and an unfinished month on desktop/mobile.
