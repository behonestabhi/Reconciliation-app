"""
Web layer. Routes only: parse the request, call a service function, render
a template or redirect. No reconciliation, comparison, or matching logic
lives here -- see src/matching/core.py (pure engine),
src/services/reconciliation_service.py (persists a run),
src/services/resolution_service.py (manual decisions), and
src/services/view_service.py (read-model queries for these templates).
"""
from flask import Blueprint, flash, redirect, render_template, request, url_for

from src.db import session_scope
from src.ingestion.registry import known_systems
from src.models import SourceFile
from src.services import view_service
from src.services.import_service import import_file
from src.services.reconciliation_service import run_reconciliation
from src.services.resolution_service import (
    ManualResolutionError,
    create_manual_accept_unmatched,
    create_manual_match,
)

bp = Blueprint("main", __name__, template_folder="templates")

VALID_STATUS_FILTERS = {
    "MATCHED", "MATCHED_WITHIN_TOLERANCE", "DIFFERS",
    "UNMATCHED_LEDGER", "UNMATCHED_STATEMENT", "MANUALLY_RESOLVED",
}
VALID_SORTS = {"status", "natural_key", "delta"}


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@bp.route("/")
def dashboard():
    with session_scope() as session:
        latest_run = view_service.get_latest_run_summary(session)
        runs = view_service.list_runs(session)
        import_counts = view_service.get_import_counts(session)
    return render_template("dashboard.html", latest_run=latest_run, runs=runs, import_counts=import_counts)


@bp.route("/runs", methods=["POST"])
def start_run():
    triggered_by = (request.form.get("triggered_by") or "manual").strip() or "manual"
    with session_scope() as session:
        run = run_reconciliation(session, triggered_by=triggered_by)
        run_id = run.id
    flash(f"Reconciliation run #{run_id} completed.", "success")
    return redirect(url_for("main.run_detail", run_id=run_id))


# ---------------------------------------------------------------------------
# File import
# ---------------------------------------------------------------------------

@bp.route("/imports")
def imports():
    with session_scope() as session:
        files = session.query(SourceFile).order_by(SourceFile.imported_at.desc()).all()
        files = [
            {
                "id": f.id,
                "filename": f.filename,
                "source_system": f.source_system.value,
                "status": f.status.value,
                "imported_at": f.imported_at,
                "row_count": f.row_count,
                "rows_created": f.rows_created,
                "rows_corrected": f.rows_corrected,
                "rows_unchanged": f.rows_unchanged,
                "rows_failed": f.rows_failed,
                "is_correction": f.is_correction,
                "error_summary": f.error_summary,
            }
            for f in files
        ]
    return render_template("imports.html", files=files, known_systems=known_systems())


@bp.route("/imports", methods=["POST"])
def do_import():
    uploaded = request.files.get("file")
    if uploaded is None or uploaded.filename == "":
        flash("choose a file first", "error")
        return redirect(url_for("main.imports"))

    source_system = request.form.get("source_system") or None  # "" -> auto-detect
    if source_system is not None and source_system not in known_systems():
        flash(f"unknown source system {source_system!r}", "error")
        return redirect(url_for("main.imports"))

    content = uploaded.read()
    if not content:
        flash("uploaded file is empty", "error")
        return redirect(url_for("main.imports"))

    with session_scope() as session:
        source_file = import_file(session, uploaded.filename, content, source_system=source_system)
        session.flush()
        summary = (
            f"{source_file.filename}: {source_file.status.value} "
            f"(created {source_file.rows_created}, corrected {source_file.rows_corrected}, "
            f"unchanged {source_file.rows_unchanged}, failed {source_file.rows_failed})"
        )
        category = "error" if source_file.status.value in ("FAILED", "DUPLICATE") else "success"

    flash(summary, category)
    return redirect(url_for("main.imports"))


# ---------------------------------------------------------------------------
# Reconciliation run results
# ---------------------------------------------------------------------------

@bp.route("/runs/<int:run_id>")
def run_detail(run_id: int):
    status_filter = request.args.get("status") or None
    if status_filter is not None and status_filter not in VALID_STATUS_FILTERS:
        status_filter = None

    sort = request.args.get("sort") or "status"
    if sort not in VALID_SORTS:
        sort = "status"

    with session_scope() as session:
        run = view_service.get_run(session, run_id)
        if run is None:
            flash(f"run #{run_id} does not exist", "error")
            return redirect(url_for("main.dashboard"))
        summary = view_service.summarize_run(run)
        results = view_service.list_results(session, run_id, status_filter=status_filter, sort=sort)

    return render_template(
        "run_detail.html",
        run=summary,
        results=results,
        status_filter=status_filter,
        sort=sort,
        status_labels=view_service.STATUS_LABELS,
    )


@bp.route("/results/<int:result_id>")
def result_detail(result_id: int):
    with session_scope() as session:
        detail = view_service.get_result_detail(session, result_id)
        if detail is None:
            flash(f"result #{result_id} does not exist", "error")
            return redirect(url_for("main.dashboard"))
    return render_template("result_detail.html", **detail)


@bp.route("/results/<int:result_id>/resolve", methods=["POST"])
def resolve_result(result_id: int):
    action = request.form.get("action")
    resolved_by = (request.form.get("resolved_by") or "").strip()
    notes = (request.form.get("notes") or "").strip() or None
    counterpart_transaction_id = request.form.get("counterpart_transaction_id")

    if not resolved_by:
        flash("enter your name before resolving", "error")
        return redirect(url_for("main.result_detail", result_id=result_id))

    with session_scope() as session:
        detail = view_service.get_result_detail(session, result_id)
        if detail is None:
            flash(f"result #{result_id} does not exist", "error")
            return redirect(url_for("main.dashboard"))
        if not detail["can_resolve"]:
            flash("this result is not eligible for manual resolution", "error")
            return redirect(url_for("main.result_detail", result_id=result_id))

        own_transaction_id = (
            detail["ledger"]["id"] if detail["status"] == "UNMATCHED_LEDGER" else detail["statement"]["id"]
        )

        error_message = None
        try:
            if action == "accept_unmatched":
                create_manual_accept_unmatched(session, own_transaction_id, resolved_by=resolved_by, notes=notes)
            elif action == "match_with":
                if not counterpart_transaction_id:
                    raise ManualResolutionError("choose a transaction to match with")
                try:
                    counterpart_id = int(counterpart_transaction_id)
                except ValueError:
                    raise ManualResolutionError("invalid transaction selected")
                if detail["status"] == "UNMATCHED_LEDGER":
                    create_manual_match(session, own_transaction_id, counterpart_id, resolved_by=resolved_by,
                                         notes=notes)
                else:
                    create_manual_match(session, counterpart_id, own_transaction_id, resolved_by=resolved_by,
                                         notes=notes)
            else:
                raise ManualResolutionError("unrecognized action")
        except ManualResolutionError as exc:
            error_message = str(exc)

    if error_message:
        flash(f"could not resolve: {error_message}", "error")
        return redirect(url_for("main.result_detail", result_id=result_id))

    with session_scope() as session:
        new_run = run_reconciliation(session, triggered_by=f"manual resolution by {resolved_by}")
        new_run_id = new_run.id

    flash("resolution saved; reconciliation re-run to reflect it.", "success")
    return redirect(url_for("main.run_detail", run_id=new_run_id, status="MANUALLY_RESOLVED"))
