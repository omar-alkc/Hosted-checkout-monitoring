from __future__ import annotations

from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from sqlalchemy import select, text
from sqlalchemy.orm import Session, selectinload

from app.constants import (
    INVESTIGATOR_BULK_STATUS_KEYS,
    SCENARIO_LABELS,
    STATUS_KEYS,
    STATUS_LABELS,
    allowed_targets,
    status_helper_text,
    status_quick_actions,
    status_select_groups,
)
from app.database import get_db
from app.deps.auth import require_supervisor, require_supervisor_or_investigator
from app.models import Detection, ImportBatch, ImportBatchStatus, User
from app.services.policy_service import investigator_effective_targets
from app.template_ctx import operator_display_name, template_ctx as _ctx
from app.templating import templates
from app.services.detections_export import build_detections_export_workbook
from app.services.detections_service import (
    add_note,
    bulk_change_status,
    change_status,
    count_detections,
    delete_test_detections,
    delete_note,
    force_set_status,
    get_note,
    list_assignee_options,
    list_detections_with_previous_count,
    ordered_detection_metric_items,
    prior_detections_for_wallet_tokens,
    transactions_for_detection,
    update_note,
    wallet_tokens_for_prior_lookup,
)
from app.services.import_service import parse_upload_to_batch, search_transactions_for_batch
from app.services.note_permissions import can_modify_note
from app.services.external_enrichment_retry import retry_wallet_and_risk_enrichment
from app.services.scenario_run import metrics_row, run_scenarios_for_batch, run_scenarios_for_rolling, run_single_scenario_for_batch
from app.services.thresholds_service import (
    SCENARIO_CODES,
    get_or_create_scenario_config,
    get_threshold_fields_for_scenario,
    monitored_bank_for_scenario,
    scenario_label_map,
    scenario_enabled_normalized,
    set_scenario_enabled,
    set_scenario_label,
    update_scenario_config,
    update_scenario_partial,
)

router = APIRouter()


def _safe_int(s: str | None, default: int) -> int:
    try:
        return int(str(s).strip())
    except Exception:
        return default


def _is_htmx(request: Request) -> bool:
    return bool(request.headers.get("hx-request"))


async def _read_upload_capped(upload: UploadFile, max_bytes: int) -> bytes:
    """Read upload in chunks; fail before buffering more than max_bytes."""
    chunks: list[bytes] = []
    total = 0
    chunk_size = 1024 * 1024
    while True:
        chunk = await upload.read(chunk_size)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            mib = max(1, max_bytes // (1024 * 1024))
            raise ValueError(
                f"File exceeds maximum upload size ({mib} MiB). "
                "Increase MAX_UPLOAD_BYTES in the environment if needed."
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    from app.deps.auth import get_optional_user

    u = get_optional_user(request, db)
    if u is None:
        return RedirectResponse(url="/login", status_code=302)
    if u.role == "admin":
        return RedirectResponse(url="/admin/users", status_code=302)
    return RedirectResponse(url="/detections", status_code=302)


@router.get("/health", response_model=None)
def health() -> JSONResponse:
    """Process liveness check — responds without a database connection."""
    return JSONResponse(content={"ok": True})


@router.get("/detections", response_class=HTMLResponse)
def detections_list(
    request: Request,
    user: User = Depends(require_supervisor_or_investigator),
    db: Session = Depends(get_db),
    status: str | None = None,
    queue: str | None = Query(None),
    assigned: str | None = Query(None),
    scenario_id: str | None = None,
    risk: str | None = Query(None),
    batch_id: str | None = Query(None),
    scope: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    detection_id: str | None = Query(None),
    msisdn: str | None = Query(None),
    page: int | None = Query(1),
    per_page: int | None = Query(20),
    notice: str | None = Query(None),
    bulk_n: int | None = Query(None),
    bulk_applied: int | None = Query(None),
    bulk_skipped: int | None = Query(None),
    bulk_error: str | None = Query(None),
):
    status = (status or "").strip() or None
    queue_in = (queue or "").strip().lower() or None
    if queue_in and queue_in not in ("open", "closed", "initial", "test"):
        queue_in = None
    assigned_param = (assigned or "").strip() or None
    assigned_in = assigned_param
    if assigned_in and assigned_in.lower() == "me":
        assigned_in = operator_display_name(request, user)
    scenario_id = (scenario_id or "").strip() or None
    risk_raw = (risk or "").strip().lower()
    risk_q = risk_raw if risk_raw in ("high", "low") else None
    batch_raw = (batch_id or "").strip()
    batch_id_int = int(batch_raw) if batch_raw.isdigit() else None
    scope_in = (scope or "").strip().lower() or None
    df_in = (date_from or "").strip()
    dt_in = (date_to or "").strip()
    ms_in = (msisdn or "").strip()
    det_raw = (detection_id or "").strip()
    det_id_int = int(det_raw) if det_raw.isdigit() else None
    allowed_pp = (20, 50, 100, 200)
    pp = int(per_page or 20)
    if pp not in allowed_pp:
        pp = 20
    p = int(page or 1)
    if p < 1:
        p = 1
    export_q: dict[str, str] = {}
    if status:
        export_q["status"] = status
    elif queue_in:
        export_q["queue"] = queue_in
    if assigned_in:
        export_q["assigned"] = assigned_in
    if scenario_id:
        export_q["scenario_id"] = scenario_id
    if risk_q:
        export_q["risk"] = risk_q
    if batch_raw:
        export_q["batch_id"] = batch_raw
    if scope_in:
        export_q["scope"] = scope_in
    if df_in:
        export_q["date_from"] = df_in
    if dt_in:
        export_q["date_to"] = dt_in
    if det_raw:
        export_q["detection_id"] = det_raw
    if ms_in:
        export_q["msisdn"] = ms_in
    export_q["per_page"] = str(pp)
    export_query = urlencode(export_q)
    labels = scenario_label_map(db)

    total = count_detections(
        db,
        status=status,
        queue=queue_in,
        scenario_id=scenario_id,
        batch_id=batch_id_int,
        scope=scope_in,
        date_from=df_in or None,
        date_to=dt_in or None,
        assigned=assigned_in,
        detection_id=det_id_int,
        msisdn=ms_in or None,
        risk=risk_q,
    )
    pages = max(1, (total + pp - 1) // pp)
    if p > pages:
        p = pages
    offset = (p - 1) * pp
    det_pairs = list_detections_with_previous_count(
        db,
        status=status,
        queue=queue_in,
        scenario_id=scenario_id,
        batch_id=batch_id_int,
        scope=scope_in,
        date_from=df_in or None,
        date_to=dt_in or None,
        assigned=assigned_in,
        detection_id=det_id_int,
        msisdn=ms_in or None,
        risk=risk_q,
        limit=pp,
        offset=offset,
    )
    previous_counts = {d.id: n for d, n in det_pairs}
    dets = [d for d, _n in det_pairs]

    base_q = dict(export_q)
    base_q.pop("page", None)
    base_q["per_page"] = str(pp)
    def _page_url(n: int) -> str:
        return "/detections?" + urlencode({**base_q, "page": str(n)})

    return templates.TemplateResponse(
        request,
        "detections_list.html",
        _ctx(
            request,
            current_user=user,
            scenario_labels=labels,
            show_bulk=user.role in ("supervisor", "investigator"),
            show_bulk_delete_test=user.role == "supervisor",
            show_export=user.role == "supervisor",
            show_import_links=user.role == "supervisor",
            detections=dets,
            previous_counts=previous_counts,
            status_filter=status,
            queue_filter=queue_in,
            assigned_filter=assigned_in,
            assigned_filter_param=assigned_param,
            assignee_options=list_assignee_options(db),
            scenario_filter=scenario_id,
            risk_filter=risk_q,
            batch_filter=batch_raw,
            scope_filter=scope_in,
            date_from_input=df_in,
            date_to_input=dt_in,
            detection_id_input=det_raw,
            msisdn_input=ms_in,
            flash_notice=(notice or "").strip() or None,
            export_query=export_query,
            status_keys=STATUS_KEYS,
            bulk_status_keys=(
                list(INVESTIGATOR_BULK_STATUS_KEYS)
                if user.role == "investigator"
                else list(STATUS_KEYS)
            ),
            bulk_n=bulk_n,
            bulk_applied=bulk_applied,
            bulk_skipped=bulk_skipped,
            bulk_error=(bulk_error or "").strip() or None,
            page=p,
            per_page=pp,
            per_page_options=allowed_pp,
            total=total,
            pages=pages,
            page_url=_page_url,
        ),
    )


@router.get("/detections/export")
def detections_export_xlsx(
    user: User = Depends(require_supervisor),
    db: Session = Depends(get_db),
    status: str | None = None,
    queue: str | None = Query(None),
    assigned: str | None = Query(None),
    scenario_id: str | None = None,
    risk: str | None = Query(None),
    batch_id: str | None = Query(None),
    scope: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    detection_id: str | None = Query(None),
    msisdn: str | None = Query(None),
):
    status = (status or "").strip() or None
    queue_in = (queue or "").strip().lower() or None
    if queue_in and queue_in not in ("open", "closed", "initial", "test"):
        queue_in = None
    assigned_in = (assigned or "").strip() or None
    if assigned_in and assigned_in.lower() == "me":
        assigned_in = operator_display_name(request, user)
    scenario_id = (scenario_id or "").strip() or None
    risk_raw = (risk or "").strip().lower()
    risk_q = risk_raw if risk_raw in ("high", "low") else None
    batch_raw = (batch_id or "").strip()
    batch_id_int = int(batch_raw) if batch_raw.isdigit() else None
    scope_in = (scope or "").strip().lower() or None
    df_in = (date_from or "").strip()
    dt_in = (date_to or "").strip()
    ms_in = (msisdn or "").strip()
    det_raw = (detection_id or "").strip()
    det_id_int = int(det_raw) if det_raw.isdigit() else None
    raw, fname = build_detections_export_workbook(
        db,
        status=status,
        queue=queue_in,
        scenario_id=scenario_id,
        batch_id=batch_id_int,
        scope=scope_in,
        date_from=df_in or None,
        date_to=dt_in or None,
        assigned=assigned_in,
        detection_id=det_id_int,
        msisdn=ms_in or None,
        risk=risk_q,
    )
    return StreamingResponse(
        iter([raw]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.post("/detections/bulk-status", response_class=HTMLResponse)
def detections_bulk_status(
    request: Request,
    user: User = Depends(require_supervisor_or_investigator),
    db: Session = Depends(get_db),
    ids: list[int] = Form(default=[]),
    to_status: str = Form(...),
):
    id_list = list(ids) if ids else []
    if not id_list:
        return RedirectResponse(
            url="/detections?notice=bulk_status&bulk_applied=0&bulk_skipped=0&bulk_error="
            + quote("Select at least one detection."),
            status_code=303,
        )
    applied, skipped = bulk_change_status(
        db,
        detection_ids=id_list,
        to_status=to_status.strip(),
        actor_name=operator_display_name(request, user),
        supervisor=(user.role == "supervisor"),
    )
    q = f"notice=bulk_status&bulk_applied={applied}&bulk_skipped={skipped}"
    return RedirectResponse(url=f"/detections?{q}", status_code=303)


@router.post("/detections/bulk-delete-test", response_class=HTMLResponse)
def detections_bulk_delete_test(
    user: User = Depends(require_supervisor),
    db: Session = Depends(get_db),
    ids: list[int] = Form(default=[]),
):
    deleted, skipped = delete_test_detections(db, detection_ids=ids)
    return RedirectResponse(
        url=f"/detections?notice=test_deleted&bulk_applied={deleted}&bulk_skipped={skipped}",
        status_code=303,
    )


@router.get("/detections/{detection_id}", response_class=HTMLResponse)
def detection_detail(
    request: Request,
    detection_id: int,
    user: User = Depends(require_supervisor_or_investigator),
    db: Session = Depends(get_db),
    notice: str | None = Query(None),
):
    stmt = (
        select(Detection)
        .where(Detection.id == detection_id)
        .options(selectinload(Detection.notes), selectinload(Detection.status_history))
    )
    det = db.scalars(stmt).first()
    if det is None:
        raise HTTPException(status_code=404)
    targets = allowed_targets(det.status)
    if user.role == "investigator":
        targets = investigator_effective_targets(db, from_status=det.status, workflow_targets=targets)
    if user.role == "supervisor":
        targets = set(STATUS_KEYS)
    targets_sorted = sorted(targets)
    tx_rows = transactions_for_detection(db, det)
    wallet_toks = wallet_tokens_for_prior_lookup(det)
    prior_dets = prior_detections_for_wallet_tokens(db, detection_id=det.id, wallet_tokens=wallet_toks)
    snapshot = metrics_row(det)
    labels = scenario_label_map(db)
    actor = operator_display_name(request, user)
    note_can_edit = {n.id: can_modify_note(user, n, actor_name=actor) for n in det.notes}
    return templates.TemplateResponse(
        request,
        "detection_detail.html",
        _ctx(
            request,
            current_user=user,
            scenario_labels=labels,
            detection=det,
            metrics=snapshot,
            metrics_ordered=ordered_detection_metric_items(snapshot, scenario_id=det.scenario_id),
            allowed_statuses=targets_sorted,
            status_select_groups=status_select_groups(targets),
            status_quick_actions=status_quick_actions(targets, from_status=det.status),
            status_helper=status_helper_text(det.status, targets),
            tx_rows=tx_rows,
            flash_notice=(notice or "").strip() or None,
            prior_wallet_tokens=wallet_toks,
            prior_detections=prior_dets,
            note_can_edit=note_can_edit,
        ),
    )


@router.get("/imports/{batch_id}/transactions", response_class=HTMLResponse)
def transactions_popup(
    request: Request,
    batch_id: int,
    user: User = Depends(require_supervisor_or_investigator),
    db: Session = Depends(get_db),
    msisdn: str | None = Query(None),
    card_id: str | None = Query(None),
    account_holder: str | None = Query(None),
    bank: str | None = Query(None),
    amount_min: str | None = Query(None),
    amount_max: str | None = Query(None),
    date_from: str | None = Query(None),
    time_from: str | None = Query(None),
    date_to: str | None = Query(None),
    time_to: str | None = Query(None),
    approved: str | None = Query(None),
    exclude_indices: str | None = Query(None),
    page: int | None = Query(1),
    per_page: int | None = Query(20),
):
    pp = int(per_page or 20)
    if pp not in (20, 50, 100, 200):
        pp = 20
    p = int(page or 1)
    if p < 1:
        p = 1
    offset = (p - 1) * pp
    df = (date_from or "").strip()
    tf = (time_from or "").strip()
    dt = (date_to or "").strip()
    tt = (time_to or "").strip()
    dt_from_iso = None
    dt_to_iso = None
    if df:
        dt_from_iso = df + "T" + (tf if tf else "00:00:00")
    if dt:
        dt_to_iso = dt + "T" + (tt if tt else "23:59:59")
    rows, total = search_transactions_for_batch(
        db,
        batch_id=batch_id,
        msisdn=(msisdn or "").strip() or None,
        card_id=(card_id or "").strip() or None,
        account_holder=(account_holder or "").strip() or None,
        bank=(bank or "").strip() or None,
        amount_min=(amount_min or "").strip() or None,
        amount_max=(amount_max or "").strip() or None,
        dt_from=dt_from_iso,
        dt_to=dt_to_iso,
        approved=(approved or "").strip() or None,
        exclude_row_indices=[
            int(x)
            for x in (exclude_indices or "").split(",")
            if x.strip().isdigit() and int(x.strip()) > 0
        ]
        or None,
        limit=pp,
        offset=offset,
    )
    pages = max(1, (total + pp - 1) // pp)
    if p > pages:
        p = pages
    return templates.TemplateResponse(
        request,
        "partials/transactions_popup.html",
        _ctx(
            request,
            current_user=user,
            batch_id=batch_id,
            rows=rows,
            total=total,
            page=p,
            pages=pages,
            per_page=pp,
            per_page_options=(20, 50, 100, 200),
            msisdn_input=(msisdn or "").strip(),
            card_id_input=(card_id or "").strip(),
            account_holder_input=(account_holder or "").strip(),
            bank_input=(bank or "").strip(),
            amount_min_input=(amount_min or "").strip(),
            amount_max_input=(amount_max or "").strip(),
            date_from_input=df,
            time_from_input=tf,
            date_to_input=dt,
            time_to_input=tt,
            approved_input=(approved or "").strip(),
            exclude_indices_input=(exclude_indices or "").strip(),
        ),
    )


@router.get("/transactions", response_class=HTMLResponse)
def transactions_explorer(
    request: Request,
    user: User = Depends(require_supervisor),
    db: Session = Depends(get_db),
    batch_id: str | None = Query(None),
    unique_id: str | None = Query(None),
    msisdn: str | None = Query(None),
    card_id: str | None = Query(None),
    account_holder: str | None = Query(None),
    bank: str | None = Query(None),
    amount_min: str | None = Query(None),
    amount_max: str | None = Query(None),
    date_from: str | None = Query(None),
    time_from: str | None = Query(None),
    date_to: str | None = Query(None),
    time_to: str | None = Query(None),
    approved: str | None = Query(None),
    page: int | None = Query(1),
    per_page: int | None = Query(50),
):
    batch_raw = (batch_id or "").strip()
    batch_id_int = int(batch_raw) if batch_raw.isdigit() else None
    allowed_pp = (20, 50, 100, 200)
    pp = int(per_page or 50)
    if pp not in allowed_pp:
        pp = 50
    p = int(page or 1)
    if p < 1:
        p = 1

    rows: list = []
    total = 0
    pages = 1
    df = (date_from or "").strip()
    tf = (time_from or "").strip()
    dt = (date_to or "").strip()
    tt = (time_to or "").strip()
    dt_from_iso = None
    dt_to_iso = None
    if df:
        dt_from_iso = df + "T" + (tf if tf else "00:00:00")
    if dt:
        dt_to_iso = dt + "T" + (tt if tt else "23:59:59")
    offset = (p - 1) * pp
    rows, total = search_transactions_for_batch(
        db,
        batch_id=batch_id_int,
        unique_id=(unique_id or "").strip() or None,
        msisdn=(msisdn or "").strip() or None,
        card_id=(card_id or "").strip() or None,
        account_holder=(account_holder or "").strip() or None,
        bank=(bank or "").strip() or None,
        amount_min=(amount_min or "").strip() or None,
        amount_max=(amount_max or "").strip() or None,
        dt_from=dt_from_iso,
        dt_to=dt_to_iso,
        approved=(approved or "").strip() or None,
        limit=pp,
        offset=offset,
    )
    pages = max(1, (total + pp - 1) // pp)
    if p > pages:
        p = pages

    def _page_url(n: int) -> str:
        from urllib.parse import urlencode

        q = {
            "batch_id": batch_raw,
            "unique_id": (unique_id or "").strip(),
            "msisdn": (msisdn or "").strip(),
            "card_id": (card_id or "").strip(),
            "account_holder": (account_holder or "").strip(),
            "bank": (bank or "").strip(),
            "amount_min": (amount_min or "").strip(),
            "amount_max": (amount_max or "").strip(),
            "date_from": df,
            "time_from": tf,
            "date_to": dt,
            "time_to": tt,
            "approved": (approved or "").strip(),
            "per_page": str(pp),
            "page": str(n),
        }
        q = {k: v for k, v in q.items() if str(v).strip() != ""}
        return "/transactions?" + urlencode(q)

    return templates.TemplateResponse(
        request,
        "transactions_explorer.html",
        _ctx(
            request,
            current_user=user,
            batch_id_input=batch_raw,
            batch_id_int=batch_id_int,
            unique_id_input=(unique_id or "").strip(),
            msisdn_input=(msisdn or "").strip(),
            card_id_input=(card_id or "").strip(),
            account_holder_input=(account_holder or "").strip(),
            bank_input=(bank or "").strip(),
            amount_min_input=(amount_min or "").strip(),
            amount_max_input=(amount_max or "").strip(),
            date_from_input=df,
            time_from_input=tf,
            date_to_input=dt,
            time_to_input=tt,
            approved_input=(approved or "").strip().lower(),
            per_page=pp,
            per_page_options=allowed_pp,
            rows=rows,
            total=total,
            page=p,
            pages=pages,
            page_url=_page_url,
        ),
    )


@router.post("/detections/{detection_id}/status", response_class=HTMLResponse)
def detection_status(
    request: Request,
    detection_id: int,
    user: User = Depends(require_supervisor_or_investigator),
    db: Session = Depends(get_db),
    to_status: str = Form(...),
):
    det = db.get(Detection, detection_id)
    if det is None:
        raise HTTPException(status_code=404)
    wf = allowed_targets(det.status)
    override: set[str] | None = None
    if user.role == "investigator":
        override = investigator_effective_targets(db, from_status=det.status, workflow_targets=wf)
    try:
        if user.role == "supervisor":
            force_set_status(
                db,
                detection_id=detection_id,
                to_status=to_status.strip(),
                actor_name=operator_display_name(request, user),
            )
        else:
            change_status(
                db,
                detection_id=detection_id,
                to_status=to_status.strip(),
                actor_name=operator_display_name(request, user),
                allowed_targets_override=override,
            )
    except ValueError as e:
        return RedirectResponse(url=f"/detections/{detection_id}?error={quote(str(e))}", status_code=303)
    return RedirectResponse(
        url=f"/detections/{detection_id}?notice=status_saved", status_code=303
    )


@router.post("/detections/{detection_id}/notes", response_class=HTMLResponse)
def detection_notes(
    request: Request,
    detection_id: int,
    user: User = Depends(require_supervisor_or_investigator),
    db: Session = Depends(get_db),
    body: str = Form(...),
):
    author = operator_display_name(request, user)
    try:
        note = add_note(db, detection_id=detection_id, body=body, author_name=author)
    except ValueError as e:
        msg = str(e)
        if _is_htmx(request):
            return templates.TemplateResponse(
                request, "partials/htmx_note_error.html", _ctx(request, current_user=user, message=msg)
            )
        return RedirectResponse(url=f"/detections/{detection_id}?error={quote(msg)}", status_code=303)
    if note is None:
        msg = "Detection not found."
        if _is_htmx(request):
            return templates.TemplateResponse(
                request, "partials/htmx_note_error.html", _ctx(request, current_user=user, message=msg)
            )
        raise HTTPException(status_code=404)
    if _is_htmx(request):
        return templates.TemplateResponse(
            request,
            "partials/note_row.html",
            {
                "request": request,
                "note": note,
                "detection_id": detection_id,
                "can_edit": can_modify_note(user, note, actor_name=author),
            },
        )
    return RedirectResponse(url=f"/detections/{detection_id}", status_code=303)


def _note_row_ctx(request: Request, user: User, note, detection_id: int) -> dict:
    actor = operator_display_name(request, user)
    return {
        "request": request,
        "note": note,
        "detection_id": detection_id,
        "can_edit": can_modify_note(user, note, actor_name=actor),
    }


@router.get("/detections/{detection_id}/notes/{note_id}", response_class=HTMLResponse)
def note_row_partial(
    request: Request,
    detection_id: int,
    note_id: int,
    user: User = Depends(require_supervisor_or_investigator),
    db: Session = Depends(get_db),
):
    note = get_note(db, detection_id=detection_id, note_id=note_id)
    if note is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request,
        "partials/note_row.html",
        _note_row_ctx(request, user, note, detection_id),
    )


@router.get("/detections/{detection_id}/notes/{note_id}/edit", response_class=HTMLResponse)
def note_edit_form(
    request: Request,
    detection_id: int,
    note_id: int,
    user: User = Depends(require_supervisor_or_investigator),
    db: Session = Depends(get_db),
):
    note = get_note(db, detection_id=detection_id, note_id=note_id)
    if note is None:
        raise HTTPException(status_code=404)
    if not can_modify_note(user, note, actor_name=operator_display_name(request, user)):
        raise HTTPException(status_code=403, detail="Not allowed to edit this note.")
    return templates.TemplateResponse(
        request,
        "partials/note_edit_form.html",
        {"request": request, "note": note, "detection_id": detection_id},
    )


@router.post("/detections/{detection_id}/notes/{note_id}/edit", response_class=HTMLResponse)
def note_edit_submit(
    request: Request,
    detection_id: int,
    note_id: int,
    user: User = Depends(require_supervisor_or_investigator),
    db: Session = Depends(get_db),
    body: str = Form(...),
):
    note = get_note(db, detection_id=detection_id, note_id=note_id)
    if note is None:
        raise HTTPException(status_code=404)
    if not can_modify_note(user, note, actor_name=operator_display_name(request, user)):
        msg = "Not allowed to edit this note."
        if _is_htmx(request):
            return templates.TemplateResponse(
                request, "partials/htmx_note_error.html", _ctx(request, current_user=user, message=msg)
            )
        return RedirectResponse(url=f"/detections/{detection_id}?error={quote(msg)}", status_code=303)
    try:
        note = update_note(db, detection_id=detection_id, note_id=note_id, body=body)
    except ValueError as e:
        msg = str(e)
        if _is_htmx(request):
            return templates.TemplateResponse(
                request, "partials/htmx_note_error.html", _ctx(request, current_user=user, message=msg)
            )
        return RedirectResponse(url=f"/detections/{detection_id}?error={quote(msg)}", status_code=303)
    if note is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request,
        "partials/note_row.html",
        _note_row_ctx(request, user, note, detection_id),
    )


@router.post("/detections/{detection_id}/notes/{note_id}/delete", response_class=HTMLResponse)
def note_delete(
    request: Request,
    detection_id: int,
    note_id: int,
    user: User = Depends(require_supervisor_or_investigator),
    db: Session = Depends(get_db),
):
    note = get_note(db, detection_id=detection_id, note_id=note_id)
    if note is None:
        raise HTTPException(status_code=404)
    if not can_modify_note(user, note, actor_name=operator_display_name(request, user)):
        raise HTTPException(status_code=403, detail="Not allowed to delete this note.")
    ok = delete_note(db, detection_id=detection_id, note_id=note_id)
    if not ok:
        raise HTTPException(status_code=404)
    return HTMLResponse(content="")


@router.get("/imports", response_class=HTMLResponse)
def imports_list(
    request: Request,
    user: User = Depends(require_supervisor),
    db: Session = Depends(get_db),
):
    batches = db.query(ImportBatch).order_by(ImportBatch.created_at.desc()).all()
    return templates.TemplateResponse(
        request, "imports_list.html", _ctx(request, current_user=user, batches=batches)
    )


@router.post("/imports", response_class=HTMLResponse)
async def imports_upload(
    request: Request,
    user: User = Depends(require_supervisor),
    db: Session = Depends(get_db),
    file: UploadFile = File(...),
):
    from app.config import get_settings

    try:
        raw = await _read_upload_capped(file, get_settings().max_upload_bytes)
        if not raw:
            return RedirectResponse(url=f"/imports?error={quote('Empty file.')}", status_code=303)
        batch = parse_upload_to_batch(db, filename=file.filename or "upload.xlsx", file_bytes=raw)
        return RedirectResponse(url=f"/imports/{batch.id}", status_code=303)
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        return RedirectResponse(url=f"/imports?error={quote(str(e))}", status_code=303)


@router.get("/imports/{batch_id}", response_class=HTMLResponse)
def imports_detail(
    request: Request,
    batch_id: int,
    user: User = Depends(require_supervisor),
    db: Session = Depends(get_db),
):
    batch = db.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404)
    det_count = db.query(Detection).filter(Detection.import_batch_id == batch_id).count()
    return templates.TemplateResponse(
        request,
        "import_detail.html",
        _ctx(request, current_user=user, batch=batch, det_count=det_count),
    )


@router.post("/imports/{batch_id}/run", response_class=HTMLResponse)
def imports_run(
    request: Request,
    batch_id: int,
    user: User = Depends(require_supervisor),
    db: Session = Depends(get_db),
    period: str = Form("both"),
):
    batch = db.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404)
    if batch.status != ImportBatchStatus.ready.value:
        raise HTTPException(status_code=400, detail="Import is not ready for scenario run.")
    try:
        res = run_scenarios_for_batch(db, batch_id=batch_id, period=period)
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        return RedirectResponse(url=f"/imports/{batch_id}?error={quote(str(e))}", status_code=303)
    if not res.get("ok"):
        err = str(res.get("error", "run failed"))
        return RedirectResponse(url=f"/imports/{batch_id}?error={quote(err)}", status_code=303)
    n = int(res.get("detections_created") or 0)
    # Automatic rolling weekly refresh after any successful run.
    rolling_err = None
    rolling_n = None
    try:
        rres = run_scenarios_for_rolling(db, days=7, period="weekly")
        if rres.get("ok"):
            rolling_n = int(rres.get("detections_created") or 0)
        else:
            rolling_err = str(rres.get("error") or "rolling run failed")
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        rolling_err = str(e)

    if rolling_err:
        return RedirectResponse(
            url=f"/imports/{batch_id}?notice=scenarios_run&n={n}&rolling_error={quote(rolling_err)}",
            status_code=303,
        )
    if rolling_n is not None:
        return RedirectResponse(
            url=f"/imports/{batch_id}?notice=scenarios_run&n={n}&rolling_weekly_n={rolling_n}",
            status_code=303,
        )
    return RedirectResponse(url=f"/imports/{batch_id}?notice=scenarios_run&n={n}", status_code=303)


@router.get("/thresholds", response_class=HTMLResponse)
def thresholds_legacy_redirect() -> RedirectResponse:
    return RedirectResponse(url="/scenarios", status_code=308)


@router.post("/scenarios/retry-external-enrichment", response_class=HTMLResponse)
def scenarios_retry_external_enrichment(
    user: User = Depends(require_supervisor),
    db: Session = Depends(get_db),
):
    try:
        res = retry_wallet_and_risk_enrichment(db)
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        return RedirectResponse(url=f"/scenarios?error={quote(str(e))}", status_code=303)
    q = urlencode(
        {
            "notice": "enrichment_retry",
            "updated": str(int(res.get("updated") or 0)),
            "failed": str(int(res.get("failed") or 0)),
            "skipped": str(int(res.get("skipped_ok") or 0)),
        }
    )
    return RedirectResponse(url=f"/scenarios?{q}", status_code=303)


@router.post("/scenarios/run-rolling", response_class=HTMLResponse)
def scenarios_run_rolling(
    request: Request,
    user: User = Depends(require_supervisor),
    db: Session = Depends(get_db),
    days: str = Form("7"),
    date_from: str = Form(""),
    date_to: str = Form(""),
):
    try:
        d = int(str(days).strip() or "7")
    except ValueError:
        d = 7
    if d <= 0 or d > 365:
        return RedirectResponse(url=f"/scenarios?error={quote('Invalid rolling window days.')}", status_code=303)
    df_raw = str(date_from or "").strip()
    dt_raw = str(date_to or "").strip()
    try:
        res = run_scenarios_for_rolling(
            db,
            days=d,
            period="weekly",
            date_from=df_raw or None,
            date_to=dt_raw or None,
        )
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        return RedirectResponse(url=f"/scenarios?error={quote(str(e))}", status_code=303)
    if not res.get("ok"):
        return RedirectResponse(url=f"/scenarios?error={quote(str(res.get('error') or 'run failed'))}", status_code=303)
    n = int(res.get("detections_created") or 0)
    return RedirectResponse(url=f"/detections?notice=rolling_run&scope=rolling&bulk_n={n}", status_code=303)


@router.get("/scenarios", response_class=HTMLResponse)
def scenarios_manager(
    request: Request,
    user: User = Depends(require_supervisor),
    db: Session = Depends(get_db),
    saved: str | None = Query(None),
    notice: str | None = Query(None),
    updated: str | None = Query(None),
    failed: str | None = Query(None),
    skipped: str | None = Query(None),
):
    row = get_or_create_scenario_config(db)
    enabled_map = scenario_enabled_normalized(getattr(row, "scenario_enabled", None))
    labels = scenario_label_map(db)
    scenario_rows: list[dict[str, object]] = []
    for sid in SCENARIO_CODES:
        enabled = bool(enabled_map.get(sid, True))
        scenario_rows.append(
            {
                "id": sid,
                "label": labels.get(sid, sid),
                "monitored": monitored_bank_for_scenario(row, sid) or "",
                "status": "Enabled" if enabled else "Disabled",
            }
        )
    upd = 0
    fail_n = 0
    try:
        upd = int((updated or "").strip() or "0")
    except ValueError:
        upd = 0
    try:
        fail_n = int((failed or "").strip() or "0")
    except ValueError:
        fail_n = 0
    skip_n = 0
    try:
        skip_n = int((skipped or "").strip() or "0")
    except ValueError:
        skip_n = 0
    return templates.TemplateResponse(
        request,
        "scenario_manager.html",
        _ctx(
            request,
            current_user=user,
            scenario_rows=scenario_rows,
            list_saved=(saved or "").strip() == "1",
            flash_notice=(notice or "").strip() or None,
            enrichment_updated=upd,
            enrichment_failed=fail_n,
            enrichment_skipped=skip_n,
        ),
    )


def _shared_threshold_note(scenario_id: str) -> str | None:
    sid = scenario_id.strip().upper()
    if sid in {"D1", "D2"}:
        return "Daily amount thresholds (min per txn and min total per group) are shared between D1 and D2."
    return None


def _high_risk_field_keys(scenario_id: str) -> set[str]:
    sid = scenario_id.strip().upper()
    if sid == "D1":
        return {"d1_risk_min_total_amount", "d1_risk_min_expenditure_pct"}
    if sid == "D2":
        return {"d2_risk_min_total_amount", "d2_risk_min_wallet_expenditure_pct", "d2_risk_min_wallets_pct"}
    return set()


@router.get("/scenarios/{scenario_id}", response_class=HTMLResponse)
def scenario_detail_page(
    request: Request,
    scenario_id: str,
    user: User = Depends(require_supervisor),
    db: Session = Depends(get_db),
    saved: str | None = Query(None),
):
    sid = scenario_id.strip().upper()
    if sid not in SCENARIO_CODES:
        raise HTTPException(status_code=404)
    row = get_or_create_scenario_config(db)
    enabled_map = scenario_enabled_normalized(getattr(row, "scenario_enabled", None))
    scenario_enabled = bool(enabled_map.get(sid, True))
    batches = (
        db.query(ImportBatch)
        .filter(ImportBatch.status == ImportBatchStatus.ready.value)
        .order_by(ImportBatch.created_at.desc())
        .limit(25)
        .all()
    )
    fields = get_threshold_fields_for_scenario(sid)
    risk_keys = _high_risk_field_keys(sid)
    threshold_fields = [(k, lab) for k, lab in fields if k not in risk_keys]
    risk_fields = [(k, lab) for k, lab in fields if k in risk_keys]
    vals: dict[str, object] = {}
    for k, _ in fields:
        v = getattr(row, k)
        # Amount thresholds are stored as Numeric and may render with trailing decimals.
        if "amount" in k:
            try:
                vals[k] = int(v)
            except Exception:
                try:
                    vals[k] = int(float(v))
                except Exception:
                    vals[k] = v
        else:
            vals[k] = v
    monitored = monitored_bank_for_scenario(row, sid) or ""
    labels = scenario_label_map(db)
    return templates.TemplateResponse(
        request,
        "scenario_detail.html",
        _ctx(
            request,
            current_user=user,
            scenario_id=sid,
            scenario_label=labels.get(sid, sid),
            scenario_label_input=labels.get(sid, sid),
            ready_batches=batches,
            fields=threshold_fields,
            risk_fields=risk_fields,
            values=vals,
            monitored_bank_input=monitored,
            scenario_enabled=scenario_enabled,
            shared_note=_shared_threshold_note(sid),
            scenario_saved=(saved or "").strip() == "1",
        ),
    )


@router.post("/scenarios/{scenario_id}", response_class=HTMLResponse)
async def scenario_detail_save(
    request: Request, scenario_id: str, user: User = Depends(require_supervisor), db: Session = Depends(get_db)
):
    sid = scenario_id.strip().upper()
    if sid not in SCENARIO_CODES:
        raise HTTPException(status_code=404)
    form = await request.form()
    enabled_raw = str(form.get("scenario_status") or "").strip().lower()
    enable_flag = None
    if enabled_raw in {"enabled", "enable", "on", "true", "1"}:
        enable_flag = True
    elif enabled_raw in {"disabled", "disable", "off", "false", "0"}:
        enable_flag = False
    label_in = str(form.get("scenario_label") or "")
    data = {
        str(k): str(v)
        for k, v in form.items()
        if str(k) not in {"monitored_bank", "scenario_status", "scenario_label"}
    }
    monitored_bank = str(form.get("monitored_bank") or "")
    try:
        if enable_flag is not None:
            set_scenario_enabled(db, scenario_id=sid, enabled=enable_flag)
        set_scenario_label(db, scenario_id=sid, label=label_in)
        update_scenario_partial(db, scenario_id=sid, values=data, monitored_bank=monitored_bank)
    except Exception as e:
        return RedirectResponse(url=f"/scenarios/{sid}?error={quote(str(e))}", status_code=303)
    return RedirectResponse(url=f"/scenarios/{sid}?saved=1", status_code=303)


@router.post("/scenarios/{scenario_id}/test", response_class=HTMLResponse)
async def scenario_test_run(
    request: Request, scenario_id: str, user: User = Depends(require_supervisor), db: Session = Depends(get_db)
):
    sid = scenario_id.strip().upper()
    if sid not in SCENARIO_CODES:
        raise HTTPException(status_code=404)
    form = await request.form()
    batch_raw = str(form.get("batch_id") or "").strip()
    if not batch_raw.isdigit():
        return RedirectResponse(url=f"/scenarios/{sid}?error={quote('Pick an import batch to test against.')}", status_code=303)
    batch_id = int(batch_raw)
    batch = db.get(ImportBatch, batch_id)
    if batch is None:
        return RedirectResponse(url=f"/scenarios/{sid}?error={quote('Import batch not found.')}", status_code=303)
    if batch.status != ImportBatchStatus.ready.value:
        return RedirectResponse(url=f"/scenarios/{sid}?error={quote('Import batch is not ready.')}", status_code=303)
    res = run_single_scenario_for_batch(db, batch_id=batch_id, scenario_id=sid, status="test")
    if not res.get("ok"):
        return RedirectResponse(url=f"/scenarios/{sid}?error={quote(str(res.get('error','test failed')))}", status_code=303)
    n = int(res.get("detections_created") or 0)
    return RedirectResponse(
        url=f"/detections?notice=test_created&status=test&scenario_id={sid}&batch_id={batch_id}&bulk_n={n}",
        status_code=303,
    )


@router.post("/thresholds", response_class=HTMLResponse)
async def thresholds_save_legacy(
    request: Request, user: User = Depends(require_supervisor), db: Session = Depends(get_db)
):
    form = await request.form()
    data = {str(k): str(v) for k, v in form.items()}
    try:
        update_scenario_config(db, values=data)
    except Exception as e:
        return RedirectResponse(url=f"/scenarios?error={quote(str(e))}", status_code=303)
    return RedirectResponse(url="/scenarios?saved=1", status_code=303)
