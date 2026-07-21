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
from app.services.pending_evidence_sla import apply_pending_evidence_auto_escalation
from app.services.policy_service import get_pending_evidence_max_days
from app.services.note_permissions import can_modify_note
from app.services.external_enrichment_retry import retry_wallet_and_risk_enrichment
from app.services.scenario_run import metrics_row, run_scenarios_for_batch, run_scenarios_for_rolling, run_single_scenario_for_batch
from app.services.scenarios_service import (
    GROUP_TYPE_LABELS,
    PERIOD_UNIT_LABELS,
    TRANSACTION_FILTER_LABELS,
    create_scenario,
    default_transaction_filter_for_group,
    get_scenario_by_code,
    list_active_scenarios,
    period_display,
    risk_threshold_fields_for_group,
    scenario_label_map,
    soft_delete_scenario,
    threshold_fields_for_group,
    update_scenario,
)
from app.services.detection_transactions import default_card_id, wallet_msisdns_from_detection
from app.services.detection_tx_table import (
    hosted_row_after_detection,
    normalize_hosted_sort,
    normalize_sort_dir,
    normalize_wallet_sort,
    paginate_rows,
    sort_wallet_tx_rows,
)
from app.services.minitrans_window import (
    BEFORE_PRESETS,
    _coerce_utc_ts,
    compute_minitrans_window,
    resolve_detection_anchor,
)
from app.services.transactions_export import build_transactions_export_workbook
from app.services.thresholds_service import update_scenario_config

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
    card_id: str | None = Query(None),
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
    cid_in = (card_id or "").strip()
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
    if cid_in:
        export_q["card_id"] = cid_in
    export_q["per_page"] = str(pp)
    export_query = urlencode(export_q)
    labels = scenario_label_map(db)

    apply_pending_evidence_auto_escalation(db)
    pending_evidence_max_days = get_pending_evidence_max_days(db)

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
        card_id=cid_in or None,
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
        card_id=cid_in or None,
        risk=risk_q,
        limit=pp,
        offset=offset,
    )
    previous_counts = {d.id: n for d, n, _pd in det_pairs}
    pending_evidence_days = {d.id: pd for d, _n, pd in det_pairs}
    dets = [d for d, _n, _pd in det_pairs]

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
            pending_evidence_days=pending_evidence_days,
            pending_evidence_max_days=pending_evidence_max_days,
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
            card_id_input=cid_in,
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
    card_id: str | None = Query(None),
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
    cid_in = (card_id or "").strip()
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
        card_id=cid_in or None,
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
    apply_pending_evidence_auto_escalation(db)
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


@router.get("/detections/{detection_id}/transactions-popup", response_class=HTMLResponse)
def detection_transactions_popup_shell(
    request: Request,
    detection_id: int,
    user: User = Depends(require_supervisor_or_investigator),
    db: Session = Depends(get_db),
    tab: str | None = Query("hosted"),
):
    det = db.get(Detection, detection_id)
    if det is None:
        raise HTTPException(status_code=404)
    metrics = dict(det.metrics or {})
    tab_in = (tab or "hosted").strip().lower()
    if tab_in not in ("hosted", "wallet"):
        tab_in = "hosted"
    return templates.TemplateResponse(
        request,
        "partials/detection_transactions_popup.html",
        _ctx(
            request,
            current_user=user,
            detection_id=detection_id,
            active_tab=tab_in,
            default_msisdn=str(metrics.get("WalletId") or "").strip(),
            default_card_id=default_card_id(det),
            default_batch_id=det.import_batch_id,
            before_presets=BEFORE_PRESETS,
        ),
    )


def _detection_anchor_and_payloads(db: Session, det: Detection) -> tuple[object | None, list[dict]]:
    tx_rows = transactions_for_detection(db, det)
    payloads = [dict(r.payload or {}) for r in tx_rows]
    anchor = resolve_detection_anchor(dict(det.metrics or {}), payloads, det.created_at)
    return anchor, payloads


@router.get("/detections/{detection_id}/hosted-transactions", response_class=HTMLResponse)
def detection_hosted_transactions(
    request: Request,
    detection_id: int,
    user: User = Depends(require_supervisor_or_investigator),
    db: Session = Depends(get_db),
    batch_id: str | None = Query(None),
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
    sort_by: str | None = Query("timestamp"),
    sort_dir: str | None = Query("desc"),
):
    det = db.get(Detection, detection_id)
    if det is None:
        raise HTTPException(status_code=404)

    anchor, _ = _detection_anchor_and_payloads(db, det)
    anchor_iso = anchor.isoformat() if anchor is not None and hasattr(anchor, "isoformat") else None
    sort_key = normalize_hosted_sort(sort_by)
    sort_direction = normalize_sort_dir(sort_dir)

    batch_raw = (batch_id or "").strip()
    batch_id_int = int(batch_raw) if batch_raw.isdigit() else None
    metrics = dict(det.metrics or {})
    ms_in = (msisdn or "").strip() or str(metrics.get("WalletId") or "").strip() or None
    cid_in = (card_id or "").strip() or default_card_id(det) or None

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
        batch_id=batch_id_int,
        msisdn=ms_in,
        card_id=cid_in,
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
        sort_by=sort_key,
        sort_dir=sort_direction,
    )
    pages = max(1, (total + pp - 1) // pp)
    if p > pages:
        p = pages

    display_rows = [
        {
            "row": row,
            "after_detection": hosted_row_after_detection(dict(row.payload or {}), anchor),
        }
        for row in rows
    ]

    batch_options = _ready_batches(db)

    return templates.TemplateResponse(
        request,
        "partials/detection_hosted_transactions.html",
        _ctx(
            request,
            current_user=user,
            detection_id=detection_id,
            display_rows=display_rows,
            rows=rows,
            total=total,
            page=p,
            pages=pages,
            per_page=pp,
            per_page_options=(20, 50, 100, 200),
            sort_by=sort_key,
            sort_dir=sort_direction,
            msisdn_input=ms_in or "",
            card_id_input=cid_in or "",
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
            batch_id_input=batch_raw,
            batch_options=batch_options,
            anchor_iso=anchor_iso or "",
        ),
    )


@router.get("/detections/{detection_id}/minitrans-transactions", response_class=HTMLResponse)
def detection_minitrans_transactions(
    request: Request,
    detection_id: int,
    user: User = Depends(require_supervisor_or_investigator),
    db: Session = Depends(get_db),
    before_preset: str | None = Query("last_week"),
    custom_date_from: str | None = Query(None),
    custom_time_from: str | None = Query(None),
    custom_date_to: str | None = Query(None),
    custom_time_to: str | None = Query(None),
    include_after: str | None = Query(None),
    page: int | None = Query(1),
    per_page: int | None = Query(20),
    sort_by: str | None = Query("timestamp"),
    sort_dir: str | None = Query("desc"),
):
    det = db.get(Detection, detection_id)
    if det is None:
        raise HTTPException(status_code=404)

    wallets = wallet_msisdns_from_detection(det)
    anchor, _ = _detection_anchor_and_payloads(db, det)
    sort_key = normalize_wallet_sort(sort_by)
    sort_direction = normalize_sort_dir(sort_dir)
    pp = int(per_page or 20)
    if pp not in (20, 50, 100, 200):
        pp = 20
    p = int(page or 1)
    if p < 1:
        p = 1

    preset = (before_preset or "last_week").strip().lower()
    if preset not in BEFORE_PRESETS:
        preset = "last_week"

    custom_from = None
    custom_to = None
    cdf = (custom_date_from or "").strip()
    ctf = (custom_time_from or "").strip()
    cdt = (custom_date_to or "").strip()
    ctt = (custom_time_to or "").strip()
    if cdf:
        custom_from = cdf + "T" + (ctf if ctf else "00:00:00")
    if cdt:
        custom_to = cdt + "T" + (ctt if ctt else "23:59:59")

    inc_after = str(include_after or "").strip().lower() in {"1", "true", "yes", "on"}

    error_msg: str | None = None
    rows: list[dict[str, object]] = []
    all_rows: list[dict[str, object]] = []
    total = 0
    pages = 1
    dt_from_display = ""
    dt_to_display = ""

    if not wallets:
        error_msg = "No wallet MSISDN on this detection."
    elif anchor is None or (hasattr(anchor, "__class__") and str(anchor) == "NaT"):
        error_msg = "Could not determine detection anchor time."
    else:
        dt_from, dt_to = compute_minitrans_window(
            anchor,
            before_preset=preset,
            custom_from=custom_from,
            custom_to=custom_to,
            include_after=inc_after,
        )
        dt_from_display = dt_from.isoformat() if dt_from else ""
        dt_to_display = dt_to.isoformat() if dt_to else ""
        try:
            from io_utils import fetch_wallet_transactions_range

            df = fetch_wallet_transactions_range(wallets, dt_from, dt_to)
            anchor_ts = _coerce_utc_ts(anchor)
            for _, r in df.iterrows():
                ts = r.get("timestamp")
                ts_norm = _coerce_utc_ts(ts)
                after_det = bool(
                    ts_norm is not None and anchor_ts is not None and ts_norm > anchor_ts
                )
                all_rows.append(
                    {
                        "timestamp": ts,
                        "transactionId": r.get("transactionId"),
                        "transactionAmount": r.get("transactionAmount"),
                        "creditedMSISDN": r.get("creditedMSISDN"),
                        "debitedMSISDN": r.get("debitedMSISDN"),
                        "transactionType": r.get("transactionType"),
                        "transactionDescription": r.get("transactionDescription"),
                        "after_detection": after_det,
                    }
                )
            all_rows = sort_wallet_tx_rows(all_rows, sort_by=sort_key, sort_dir=sort_direction)
            rows, total, p, pages = paginate_rows(all_rows, page=p, per_page=pp)
        except Exception as e:
            error_msg = str(e)

    return templates.TemplateResponse(
        request,
        "partials/minitrans_transactions_popup.html",
        _ctx(
            request,
            current_user=user,
            detection_id=detection_id,
            rows=rows,
            total=total,
            page=p,
            pages=pages,
            per_page=pp,
            per_page_options=(20, 50, 100, 200),
            sort_by=sort_key,
            sort_dir=sort_direction,
            error_msg=error_msg,
            before_preset=preset,
            custom_date_from=cdf,
            custom_time_from=ctf,
            custom_date_to=cdt,
            custom_time_to=ctt,
            include_after=inc_after,
            wallets=wallets,
            anchor_display=str(anchor or ""),
            dt_from_display=dt_from_display,
            dt_to_display=dt_to_display,
            before_presets=BEFORE_PRESETS,
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
    user: User = Depends(require_supervisor_or_investigator),
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
            rows=rows,
            total=total,
            page=p,
            pages=pages,
            per_page=pp,
            per_page_options=allowed_pp,
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
            approved_input=(approved or "").strip(),
            page_url=_page_url,
            export_query=urlencode({k: v for k, v in {
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
            }.items() if str(v).strip() != ""}),
        ),
    )


@router.get("/transactions/export")
def transactions_export(
    user: User = Depends(require_supervisor_or_investigator),
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
):
    batch_raw = (batch_id or "").strip()
    batch_id_int = int(batch_raw) if batch_raw.isdigit() else None
    df = (date_from or "").strip()
    tf = (time_from or "").strip()
    dt = (date_to or "").strip()
    tt = (time_to or "").strip()
    dt_from_iso = df + "T" + (tf if tf else "00:00:00") if df else None
    dt_to_iso = dt + "T" + (tt if tt else "23:59:59") if dt else None
    data, fname = build_transactions_export_workbook(
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
    )
    return StreamingResponse(
        iter([data]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
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
    rolling_ref = None
    try:
        rres = run_scenarios_for_rolling(db, days=7, period="weekly")
        if rres.get("ok"):
            rolling_n = int(rres.get("detections_created") or 0)
            rolling_ref = int(rres.get("detections_refreshed") or 0)
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
        q = urlencode(
            {
                "notice": "scenarios_run",
                "n": str(n),
                "rolling_weekly_n": str(rolling_n),
                "rolling_weekly_ref": str(rolling_ref or 0),
            }
        )
        return RedirectResponse(url=f"/imports/{batch_id}?{q}", status_code=303)
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
    ref = int(res.get("detections_refreshed") or 0)
    q = urlencode({"notice": "rolling_run", "scope": "rolling", "bulk_n": str(n), "bulk_ref": str(ref)})
    return RedirectResponse(url=f"/detections?{q}", status_code=303)


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
    error: str | None = Query(None),
):
    scenario_rows: list[dict[str, object]] = []
    for s in list_active_scenarios(db):
        scenario_rows.append(
            {
                "id": s.code,
                "label": s.name,
                "group": GROUP_TYPE_LABELS.get(s.group_type, s.group_type),
                "period": period_display(s.period_unit, s.period_value),
                "monitored": s.monitored_bank or "",
                "status": "Enabled" if s.enabled else "Disabled",
            }
        )
    upd = int((updated or "").strip() or "0") if (updated or "").strip().isdigit() else 0
    fail_n = int((failed or "").strip() or "0") if (failed or "").strip().isdigit() else 0
    skip_n = int((skipped or "").strip() or "0") if (skipped or "").strip().isdigit() else 0
    return templates.TemplateResponse(
        request,
        "scenario_manager.html",
        _ctx(
            request,
            current_user=user,
            scenario_rows=scenario_rows,
            list_saved=(saved or "").strip() == "1",
            flash_notice=(notice or "").strip() or None,
            flash_error=(error or "").strip() or None,
            enrichment_updated=upd,
            enrichment_failed=fail_n,
            enrichment_skipped=skip_n,
        ),
    )


def _ready_batches(db: Session) -> list[ImportBatch]:
    return (
        db.query(ImportBatch)
        .filter(ImportBatch.status == ImportBatchStatus.ready.value)
        .order_by(ImportBatch.created_at.desc())
        .limit(25)
        .all()
    )


def _scenario_form_values(scenario) -> dict[str, object]:
    vals: dict[str, object] = dict(scenario.thresholds or {})
    for k, v in vals.items():
        if "amount" in k:
            try:
                vals[k] = int(float(v))
            except (TypeError, ValueError):
                pass
    return vals


@router.get("/scenarios/new", response_class=HTMLResponse)
def scenario_new_page(
    request: Request,
    user: User = Depends(require_supervisor),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        request,
        "scenario_form.html",
        _ctx(
            request,
            current_user=user,
            is_new=True,
            scenario_id="",
            scenario_name="",
            group_type=next(iter(GROUP_TYPE_LABELS.keys())),
            period_unit="day",
            period_value=1,
            scenario_enabled=True,
            monitored_bank_input="",
            transaction_filter=default_transaction_filter_for_group(next(iter(GROUP_TYPE_LABELS.keys()))),
            transaction_filter_labels=TRANSACTION_FILTER_LABELS,
            fields=threshold_fields_for_group(next(iter(GROUP_TYPE_LABELS.keys()))),
            risk_fields=[],
            values={},
            group_type_labels=GROUP_TYPE_LABELS,
            period_unit_labels=PERIOD_UNIT_LABELS,
            ready_batches=_ready_batches(db),
        ),
    )


@router.post("/scenarios", response_class=HTMLResponse)
async def scenario_create(
    request: Request,
    user: User = Depends(require_supervisor),
    db: Session = Depends(get_db),
):
    form = await request.form()
    form_dict = {str(k): str(v) for k, v in form.items()}
    enabled_raw = str(form.get("scenario_status") or "enabled").strip().lower()
    enabled = enabled_raw not in {"disabled", "disable", "off", "false", "0"}
    try:
        create_scenario(
            db,
            name=str(form.get("scenario_name") or ""),
            group_type=str(form.get("group_type") or ""),
            period_unit=str(form.get("period_unit") or "day"),
            period_value=int(str(form.get("period_value") or "1")),
            thresholds=_parse_form_thresholds(str(form.get("group_type") or ""), form_dict),
            monitored_bank=str(form.get("monitored_bank") or ""),
            enabled=enabled,
            transaction_filter=str(form.get("transaction_filter") or ""),
        )
    except Exception as e:
        return RedirectResponse(url=f"/scenarios/new?error={quote(str(e))}", status_code=303)
    return RedirectResponse(url="/scenarios?saved=1", status_code=303)


def _parse_form_thresholds(group_type: str, form: dict[str, str]) -> dict[str, float | int]:
    from app.services.scenarios_service import _normalize_thresholds

    raw = {k: form[k] for k in form if k not in {
        "scenario_name", "group_type", "period_unit", "period_value",
        "monitored_bank", "scenario_status", "batch_id", "transaction_filter",
    }}
    return _normalize_thresholds(group_type, raw)


@router.get("/scenarios/{scenario_id}", response_class=HTMLResponse)
def scenario_detail_page(
    request: Request,
    scenario_id: str,
    user: User = Depends(require_supervisor),
    db: Session = Depends(get_db),
    saved: str | None = Query(None),
    error: str | None = Query(None),
):
    sid = scenario_id.strip().upper()
    scenario = get_scenario_by_code(db, sid)
    if scenario is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request,
        "scenario_form.html",
        _ctx(
            request,
            current_user=user,
            is_new=False,
            scenario_id=scenario.code,
            scenario_name=scenario.name,
            group_type=scenario.group_type,
            period_unit=scenario.period_unit,
            period_value=scenario.period_value,
            scenario_enabled=scenario.enabled,
            monitored_bank_input=scenario.monitored_bank or "",
            transaction_filter=scenario.transaction_filter,
            transaction_filter_labels=TRANSACTION_FILTER_LABELS,
            fields=threshold_fields_for_group(scenario.group_type),
            risk_fields=risk_threshold_fields_for_group(scenario.group_type),
            values=_scenario_form_values(scenario),
            group_type_labels=GROUP_TYPE_LABELS,
            period_unit_labels=PERIOD_UNIT_LABELS,
            ready_batches=_ready_batches(db),
            scenario_saved=(saved or "").strip() == "1",
            flash_error=(error or "").strip() or None,
        ),
    )


@router.post("/scenarios/{scenario_id}", response_class=HTMLResponse)
async def scenario_detail_save(
    request: Request, scenario_id: str, user: User = Depends(require_supervisor), db: Session = Depends(get_db)
):
    sid = scenario_id.strip().upper()
    scenario = get_scenario_by_code(db, sid)
    if scenario is None:
        raise HTTPException(status_code=404)
    form = await request.form()
    form_dict = {str(k): str(v) for k, v in form.items()}
    enabled_raw = str(form.get("scenario_status") or "").strip().lower()
    enable_flag = None
    if enabled_raw in {"enabled", "enable", "on", "true", "1"}:
        enable_flag = True
    elif enabled_raw in {"disabled", "disable", "off", "false", "0"}:
        enable_flag = False
    try:
        update_scenario(
            db,
            scenario,
            name=str(form.get("scenario_name") or scenario.name),
            period_unit=str(form.get("period_unit") or scenario.period_unit),
            period_value=int(str(form.get("period_value") or scenario.period_value)),
            thresholds=_parse_form_thresholds(scenario.group_type, form_dict),
            monitored_bank=str(form.get("monitored_bank") or ""),
            enabled=enable_flag if enable_flag is not None else scenario.enabled,
            transaction_filter=str(form.get("transaction_filter") or scenario.transaction_filter),
        )
    except Exception as e:
        return RedirectResponse(url=f"/scenarios/{sid}?error={quote(str(e))}", status_code=303)
    return RedirectResponse(url=f"/scenarios/{sid}?saved=1", status_code=303)


@router.post("/scenarios/{scenario_id}/delete", response_class=HTMLResponse)
async def scenario_delete(
    scenario_id: str,
    user: User = Depends(require_supervisor),
    db: Session = Depends(get_db),
):
    sid = scenario_id.strip().upper()
    scenario = get_scenario_by_code(db, sid)
    if scenario is None:
        raise HTTPException(status_code=404)
    soft_delete_scenario(db, scenario)
    return RedirectResponse(url="/scenarios?saved=1", status_code=303)


@router.post("/scenarios/{scenario_id}/test", response_class=HTMLResponse)
async def scenario_test_run(
    request: Request, scenario_id: str, user: User = Depends(require_supervisor), db: Session = Depends(get_db)
):
    sid = scenario_id.strip().upper()
    scenario = get_scenario_by_code(db, sid)
    if scenario is None:
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
