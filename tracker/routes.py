import io
import json
import secrets
from datetime import timedelta
from functools import wraps

from flask import (Blueprint, abort, flash, redirect, render_template, request,
                   send_file, url_for)
from flask_login import current_user, login_required, login_user, logout_user
from openpyxl import Workbook
from sqlalchemy import case, func, or_

from .extensions import db
from .models import (BRANCHES, DIST_STATUSES, KYC_STATUSES, REMARK_OPTIONS,
                     AuditLog, PendingUpload, Record, User, apply_branch,
                     branch_allowed, log_event, now_ist, scoped_records)
from .parser import ParseError, import_rows, parse_file, preview_rows

bp = Blueprint("main", __name__)

PER_PAGE = 50
AUDIT_PER_PAGE = 100
MAX_BULK = 500
MAX_FAILED_LOGINS = 5
LOCK_MINUTES = 15
TRACKED_FIELDS = ["due", "policy_amt", "dist_status", "delivered_to", "mob", "kyc_status", "remark"]
EXPORT_COLUMNS = [
    ("Branch Name", "branch_name"), ("Member Code", "member_code"),
    ("Member Name", "member_name"), ("Member Mobile Number", "member_mobile"),
    ("Account Number", "account_number"), ("Open Date", "open_date"),
    ("Close Date", "close_date"), ("Due", "due"), ("Policy Amt", "policy_amt"),
    ("Amount Due", "amount_due"), ("Dist Status", "dist_status"),
    ("Delivered To", "delivered_to"), ("Mob", "mob"), ("KYC Status", "kyc_status"),
    ("Remark", "remark"),
]


# ---------------------------------------------------------------- helpers
def admin_required(view):
    @wraps(view)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)

    return wrapper


def _safe_next(target):
    return target if target and target.startswith("/") and not target.startswith("//") else None


def _fmt(value):
    return "-" if value in (None, "") else str(value)


def _filtered(q, status):
    """Records the current user may see, filtered by search text and status."""
    query = scoped_records(current_user)
    if q:
        like = f"%{q}%"
        query = query.filter(
            or_(
                Record.member_code.ilike(like),
                Record.member_name.ilike(like),
                Record.member_mobile.ilike(like),
                Record.account_number.ilike(like),
            )
        )
    if status in DIST_STATUSES:
        query = query.filter(Record.dist_status == status)
    return query.order_by(Record.member_code, Record.account_number)


def _mark_delivered(rec, delivered_to=""):
    rec.dist_status = "Delivered"
    rec.delivered_at = now_ist()
    if delivered_to:
        rec.delivered_to = delivered_to
    rec.updated_by = current_user.username
    rec.updated_at = now_ist()
    log_event(current_user.username, "delivered", f"delivered_to: {_fmt(rec.delivered_to)}", rec)


def _mark_pending(rec):
    rec.dist_status = "Pending"
    rec.delivered_at = None
    rec.updated_by = current_user.username
    rec.updated_at = now_ist()
    log_event(current_user.username, "undo delivery", "dist_status: Delivered → Pending", rec)


# ---------------------------------------------------------------- auth
@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.index"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter(func.lower(User.username) == username.lower()).first()
        now = now_ist()
        if user and user.locked_until and user.locked_until > now:
            minutes = int((user.locked_until - now).total_seconds() // 60) + 1
            flash(f"Too many failed attempts. Try again in {minutes} minute(s).", "error")
        elif user and user.check_password(password):
            if not user.is_active:
                flash("This account is disabled. Contact the admin.", "error")
            else:
                user.failed_logins = 0
                user.locked_until = None
                db.session.commit()
                login_user(user)
                return redirect(_safe_next(request.args.get("next")) or url_for("main.index"))
        else:
            if user:
                user.failed_logins = (user.failed_logins or 0) + 1
                if user.failed_logins >= MAX_FAILED_LOGINS:
                    user.locked_until = now + timedelta(minutes=LOCK_MINUTES)
                    user.failed_logins = 0
                db.session.commit()
            flash("Invalid username or password.", "error")
    return render_template("login.html")


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return redirect(url_for("main.login"))


@bp.route("/account", methods=["GET", "POST"])
@login_required
def account():
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        if not current_user.check_password(current):
            flash("Current password is wrong.", "error")
        elif len(new) < 6:
            flash("New password needs 6+ characters.", "error")
        elif new != confirm:
            flash("New password and confirmation do not match.", "error")
        else:
            current_user.set_password(new)
            log_event(current_user.username, "password changed", "own password")
            db.session.commit()
            flash("Password changed.", "success")
            return redirect(url_for("main.index"))
    return render_template("account.html")


# ---------------------------------------------------------------- search
@bp.route("/")
@login_required
def index():
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "")
    page = request.args.get("page", 1, type=int)

    base = scoped_records(current_user)
    stats = {
        "total": base.count(),
        "delivered": base.filter(Record.dist_status == "Delivered").count(),
        "due_total": base.with_entities(func.coalesce(func.sum(Record.amount_due), 0)).scalar(),
    }
    stats["pending"] = stats["total"] - stats["delivered"]

    pagination = _filtered(q, status).paginate(page=page, per_page=PER_PAGE, error_out=False)
    return render_template("index.html", pagination=pagination, q=q, status=status,
                           stats=stats, statuses=DIST_STATUSES)


# ---------------------------------------------------------------- dashboard
@bp.route("/dashboard")
@login_required
def dashboard():
    base = scoped_records(current_user)
    delivered_case = case((Record.dist_status == "Delivered", 1), else_=0)
    due_case = case((Record.amount_due > 0, 1), else_=0)
    kyc_case = case((func.coalesce(Record.kyc_status, "") != "Done", 1), else_=0)
    result = (
        base.with_entities(
            Record.branch_name,
            func.count(Record.id),
            func.sum(delivered_case),
            func.sum(due_case),
            func.coalesce(func.sum(Record.amount_due), 0),
            func.sum(kyc_case),
        )
        .group_by(Record.branch_name)
        .order_by(Record.branch_name)
        .all()
    )
    branches = []
    for name, total, delivered, with_due, due_total, kyc_open in result:
        delivered = int(delivered or 0)
        branches.append({
            "name": name or "(blank)",
            "total": total,
            "delivered": delivered,
            "pending": total - delivered,
            "percent": round(delivered * 100 / total) if total else 0,
            "with_due": int(with_due or 0),
            "due_total": float(due_total or 0),
            "kyc_open": int(kyc_open or 0),
        })
    totals = {
        key: sum(b[key] for b in branches)
        for key in ("total", "delivered", "pending", "with_due", "due_total", "kyc_open")
    }
    totals["percent"] = round(totals["delivered"] * 100 / totals["total"]) if totals["total"] else 0
    recent = []
    if current_user.is_admin:
        recent = AuditLog.query.order_by(AuditLog.at.desc(), AuditLog.id.desc()).limit(10).all()
    return render_template("dashboard.html", branches=branches, totals=totals, recent=recent)


# ---------------------------------------------------------------- export / backup
@bp.route("/export")
@login_required
def export():
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "")
    records = _filtered(q, status).all()

    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet("Gift Tracker")
    sheet.append([header for header, _ in EXPORT_COLUMNS] + ["Delivered On", "Updated By"])
    for rec in records:
        row = [getattr(rec, field) for _, field in EXPORT_COLUMNS]
        row.append(rec.delivered_at.strftime("%d/%m/%Y %H:%M") if rec.delivered_at else "")
        row.append(rec.updated_by or "")
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)

    log_event(current_user.username, "export", f"{len(records)} rows (search: {q or '-'}, status: {status or '-'})")
    db.session.commit()
    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"gift_tracker_{now_ist():%Y%m%d_%H%M}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ---------------------------------------------------------------- edit / deliver
@bp.route("/record/<int:rid>", methods=["GET", "POST"])
@login_required
def edit(rid):
    rec = scoped_records(current_user).filter(Record.id == rid).first_or_404()
    if request.method == "POST":
        form = request.form
        try:
            due = int(form.get("due") or 0)
            policy = float(form.get("policy_amt") or 0)
            if due < 0 or policy < 0:
                raise ValueError
        except ValueError:
            flash("Due and Policy amount must be non-negative numbers.", "error")
            return redirect(url_for("main.edit", rid=rid))

        before = {field: getattr(rec, field) for field in TRACKED_FIELDS}
        rec.due, rec.policy_amt = due, policy
        status = form.get("dist_status")
        if status not in DIST_STATUSES:
            status = rec.dist_status
        if status == "Delivered" and rec.dist_status != "Delivered":
            rec.delivered_at = now_ist()
        elif status == "Pending":
            rec.delivered_at = None
        rec.dist_status = status
        rec.delivered_to = form.get("delivered_to", "").strip()[:120]
        rec.mob = form.get("mob", "").strip()[:30]
        rec.kyc_status = form.get("kyc_status", "").strip()[:40]

        remark = form.get("remark", "").strip()[:255]
        rec.remark_manual = False
        rec.recalc()  # Amount Due = Policy x Due, auto remark
        if remark and remark != rec.remark:
            rec.remark, rec.remark_manual = remark, True

        rec.updated_by = current_user.username
        rec.updated_at = now_ist()
        changes = [
            f"{field}: {_fmt(before[field])} → {_fmt(getattr(rec, field))}"
            for field in TRACKED_FIELDS
            if before[field] != getattr(rec, field)
        ]
        if changes:
            log_event(current_user.username, "edit", "; ".join(changes), rec)
        db.session.commit()
        flash(f"Saved {rec.member_name} ({rec.member_code}).", "success")
        return redirect(url_for("main.index", q=rec.member_code))

    history = (
        AuditLog.query.filter_by(record_id=rec.id)
        .order_by(AuditLog.at.desc(), AuditLog.id.desc())
        .limit(15)
        .all()
    )
    return render_template("edit.html", rec=rec, statuses=DIST_STATUSES,
                           kyc_statuses=KYC_STATUSES, remark_options=REMARK_OPTIONS,
                           history=history)


@bp.route("/record/<int:rid>/deliver", methods=["POST"])
@login_required
def deliver(rid):
    rec = scoped_records(current_user).filter(Record.id == rid).first_or_404()
    if rec.dist_status == "Delivered":
        when = rec.delivered_at.strftime("%d/%m/%Y %H:%M") if rec.delivered_at else "earlier"
        flash(f"{rec.member_name} was already delivered ({when}"
              f"{', to ' + rec.delivered_to if rec.delivered_to else ''}). Nothing changed.", "warning")
    else:
        _mark_delivered(rec, request.form.get("delivered_to", "").strip()[:120])
        db.session.commit()
        flash(f"{rec.member_name} marked as delivered.", "success")
    return redirect(_safe_next(request.form.get("next")) or url_for("main.index"))


@bp.route("/record/<int:rid>/undo", methods=["POST"])
@login_required
def undo_delivery(rid):
    rec = scoped_records(current_user).filter(Record.id == rid).first_or_404()
    if rec.dist_status != "Delivered":
        flash(f"{rec.member_name} is not marked as delivered.", "warning")
    else:
        _mark_pending(rec)
        db.session.commit()
        flash(f"{rec.member_name} set back to pending.", "success")
    return redirect(_safe_next(request.form.get("next")) or url_for("main.index"))


@bp.route("/record/<int:rid>/delete", methods=["POST"])
@admin_required
def delete_record(rid):
    rec = db.get_or_404(Record, rid)
    log_event(current_user.username, "delete record", f"account: {_fmt(rec.account_number)}", rec)
    name, code = rec.member_name, rec.member_code
    db.session.delete(rec)
    db.session.commit()
    flash(f"Deleted {name} ({code}).", "success")
    return redirect(url_for("main.index"))


@bp.route("/bulk", methods=["POST"])
@login_required
def bulk():
    ids = [int(x) for x in request.form.getlist("ids") if x.isdigit()][:MAX_BULK]
    action = request.form.get("action")
    delivered_to = request.form.get("delivered_to", "").strip()[:120]
    if not ids or action not in ("delivered", "pending"):
        flash("Select at least one row and an action.", "error")
        return redirect(url_for("main.index"))

    records = scoped_records(current_user).filter(Record.id.in_(ids)).all()
    done = skipped = 0
    for rec in records:
        if action == "delivered":
            if rec.dist_status == "Delivered":
                skipped += 1  # block double delivery
                continue
            _mark_delivered(rec, delivered_to)
        else:
            if rec.dist_status != "Delivered":
                skipped += 1
                continue
            _mark_pending(rec)
        done += 1
    db.session.commit()
    message = f"{done} record(s) updated."
    if skipped:
        message += f" {skipped} skipped (already {'delivered' if action == 'delivered' else 'pending'})."
    flash(message, "success" if done else "warning")
    return redirect(url_for("main.index"))


# ---------------------------------------------------------------- upload (preview -> confirm)
def _purge_pending():
    PendingUpload.query.filter(
        PendingUpload.created_at < now_ist() - timedelta(hours=2)
    ).delete(synchronize_session=False)


@bp.route("/upload", methods=["GET", "POST"])
@login_required
def upload():
    if request.method == "POST":
        file = request.files.get("file")
        if not file or not file.filename:
            flash("Choose a file first.", "error")
            return redirect(url_for("main.upload"))
        user = current_user._get_current_object()
        try:
            rows, skipped = parse_file(file)
            if not rows:
                raise ParseError("No valid rows found (Member Code and Member Name are required).")
            allowed = [r for r in rows if branch_allowed(user, r["branch_name"])]
            other_branch = len(rows) - len(allowed)
            if not allowed:
                raise ParseError("None of the rows belong to your branch, so nothing can be imported.")
            stats = preview_rows(allowed, can_touch=lambda b: branch_allowed(user, b))
        except ParseError as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.upload"))

        _purge_pending()
        token = secrets.token_urlsafe(24)
        db.session.add(PendingUpload(
            token=token,
            user_id=user.id,
            filename=file.filename[:200],
            payload=json.dumps({"rows": allowed, "skipped": skipped, "other_branch": other_branch}),
        ))
        db.session.commit()
        return render_template(
            "upload_preview.html",
            token=token,
            filename=file.filename,
            stats=stats,
            skipped=skipped,
            other_branch=other_branch,
            total=len(allowed),
            sample=allowed[:10],
            missing_policy=sum(1 for r in allowed if r["due"] > 0 and not r["policy_amt"]),
        )
    return render_template("upload.html", branches=BRANCHES)


@bp.route("/upload/confirm", methods=["POST"])
@login_required
def upload_confirm():
    pending = db.session.get(PendingUpload, request.form.get("token", ""))
    if not pending or pending.user_id != current_user.id:
        flash("That upload expired. Please upload the file again.", "error")
        return redirect(url_for("main.upload"))
    data = json.loads(pending.payload)
    user = current_user._get_current_object()
    added, updated, blocked = import_rows(
        data["rows"], can_touch=lambda b: branch_allowed(user, b)
    )
    ignored = blocked + data["other_branch"]
    log_event(
        user.username, "upload",
        f"{pending.filename}: {added} added, {updated} updated, "
        f"{ignored} ignored (other branch), {data['skipped']} skipped (blank code/name)",
    )
    db.session.delete(pending)
    db.session.commit()
    message = f"Import done: {added} added, {updated} updated, {data['skipped']} skipped (blank code/name)."
    if ignored:
        message += f" {ignored} row(s) ignored because they belong to another branch."
    flash(message, "success")
    return redirect(url_for("main.index"))


@bp.route("/upload/cancel", methods=["POST"])
@login_required
def upload_cancel():
    pending = db.session.get(PendingUpload, request.form.get("token", ""))
    if pending and pending.user_id == current_user.id:
        db.session.delete(pending)
        db.session.commit()
    flash("Upload cancelled. Nothing was saved.", "info")
    return redirect(url_for("main.upload"))


@bp.route("/data/clear", methods=["POST"])
@admin_required
def clear_data():
    scope = request.form.get("scope", "")
    if request.form.get("confirm", "").strip() != "DELETE":
        flash("Type DELETE in the box to confirm.", "error")
    elif scope == "all":
        count = Record.query.delete(synchronize_session=False)
        log_event(current_user.username, "clear data", f"all branches: {count} records deleted")
        db.session.commit()
        flash(f"Deleted all {count} records.", "success")
    elif scope in BRANCHES:
        count = apply_branch(Record.query, scope).delete(synchronize_session=False)
        log_event(current_user.username, "clear data", f"{scope}: {count} records deleted")
        db.session.commit()
        flash(f"Deleted {count} records of {scope}.", "success")
    else:
        flash("Pick what to delete.", "error")
    return redirect(url_for("main.upload"))


# ---------------------------------------------------------------- history
@bp.route("/audit")
@admin_required
def audit():
    q = request.args.get("q", "").strip()
    page = request.args.get("page", 1, type=int)
    query = AuditLog.query
    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            AuditLog.user.ilike(like), AuditLog.action.ilike(like),
            AuditLog.member_code.ilike(like), AuditLog.member_name.ilike(like),
            AuditLog.details.ilike(like),
        ))
    pagination = query.order_by(AuditLog.at.desc(), AuditLog.id.desc()).paginate(
        page=page, per_page=AUDIT_PER_PAGE, error_out=False
    )
    return render_template("audit.html", pagination=pagination, q=q)


# ---------------------------------------------------------------- users
@bp.route("/users")
@admin_required
def users():
    return render_template("users.html", users=User.query.order_by(User.role, User.username).all(),
                           branches=BRANCHES, now=now_ist())


@bp.route("/users/add", methods=["POST"])
@admin_required
def add_user():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    role = request.form.get("role", "user")
    branch = request.form.get("branch", "")
    if role not in ("user", "admin"):
        role = "user"
    if role == "admin":
        branch = "All"
    if len(username) < 3 or len(password) < 6:
        flash("Username needs 3+ characters and password 6+ characters.", "error")
    elif branch not in BRANCHES + ["All"]:
        flash("Pick a valid branch.", "error")
    elif User.query.filter(func.lower(User.username) == username.lower()).first():
        flash("That username already exists.", "error")
    else:
        user = User(username=username, role=role, branch=branch)
        user.set_password(password)
        db.session.add(user)
        log_event(current_user.username, "user added", f"{username} ({role}, {branch})")
        db.session.commit()
        flash(f"User '{username}' created ({branch}).", "success")
    return redirect(url_for("main.users"))


@bp.route("/users/<int:uid>/password", methods=["POST"])
@admin_required
def reset_password(uid):
    user = db.get_or_404(User, uid)
    password = request.form.get("password", "")
    if len(password) < 6:
        flash("Password needs 6+ characters.", "error")
    else:
        user.set_password(password)
        user.failed_logins = 0
        user.locked_until = None
        log_event(current_user.username, "password reset", user.username)
        db.session.commit()
        flash(f"Password changed for '{user.username}'.", "success")
    return redirect(url_for("main.users"))


@bp.route("/users/<int:uid>/toggle", methods=["POST"])
@admin_required
def toggle_user(uid):
    user = db.get_or_404(User, uid)
    if user.id == current_user.id:
        flash("You cannot disable your own account.", "error")
    else:
        user.enabled = not user.is_active
        state = "enabled" if user.enabled else "disabled"
        log_event(current_user.username, f"user {state}", user.username)
        db.session.commit()
        flash(f"User '{user.username}' {state}.", "success")
    return redirect(url_for("main.users"))


@bp.route("/users/<int:uid>/delete", methods=["POST"])
@admin_required
def delete_user(uid):
    user = db.get_or_404(User, uid)
    if user.id == current_user.id:
        flash("You cannot delete your own account.", "error")
    else:
        log_event(current_user.username, "user deleted", user.username)
        db.session.delete(user)
        db.session.commit()
        flash(f"User '{user.username}' deleted.", "success")
    return redirect(url_for("main.users"))