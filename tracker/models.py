from datetime import datetime, timedelta

from flask_login import UserMixin
from sqlalchemy import func
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db, login_manager

BRANCHES = ["Laxmi Nagar", "Burari", "Dwarka", "Other"]
NAMED_BRANCHES = BRANCHES[:3]
DIST_STATUSES = ["Pending", "Delivered"]
KYC_STATUSES = ["Pending", "Done"]
REMARK_OPTIONS = [
    "Completely paid",
    "Clear amount",
    "Amount pending",
    "Policy amount missing",
    "KYC pending",
    "Gift delivered",
    "Not reachable",
    "Come later",
]


def now_ist():
    """Naive datetime in Indian Standard Time."""
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(60), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(10), nullable=False, default="user")  # admin / user
    branch = db.Column(db.String(40), nullable=False, default="All")
    created_at = db.Column(db.DateTime, default=now_ist)
    enabled = db.Column(db.Boolean, default=True)  # NULL (old rows) counts as enabled
    failed_logins = db.Column(db.Integer, default=0)
    locked_until = db.Column(db.DateTime)

    @property
    def is_admin(self):
        return self.role == "admin"

    @property
    def is_active(self):  # used by Flask-Login
        return self.enabled is not False

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


@login_manager.user_loader
def load_user(user_id):
    user = db.session.get(User, int(user_id))
    return user if user and user.is_active else None  # disabled users are logged out


class Record(db.Model):
    __tablename__ = "records"
    __table_args__ = (
        db.UniqueConstraint("member_code", "account_number", name="uq_member_account"),
    )

    id = db.Column(db.Integer, primary_key=True)
    branch_name = db.Column(db.String(120), default="")
    member_code = db.Column(db.String(40), nullable=False, index=True)
    member_name = db.Column(db.String(150), nullable=False, index=True)
    member_mobile = db.Column(db.String(30), default="")
    account_number = db.Column(db.String(60), nullable=False, default="")
    open_date = db.Column(db.String(20), default="")
    close_date = db.Column(db.String(20), default="")
    due = db.Column(db.Integer, default=0)
    policy_amt = db.Column(db.Float, default=0)
    amount_due = db.Column(db.Float, default=0)
    dist_status = db.Column(db.String(20), default="Pending")
    delivered_to = db.Column(db.String(120), default="")
    mob = db.Column(db.String(30), default="")
    kyc_status = db.Column(db.String(40), default="")
    remark = db.Column(db.String(255), default="")
    remark_manual = db.Column(db.Boolean, default=False)
    delivered_at = db.Column(db.DateTime)
    updated_by = db.Column(db.String(60), default="")
    updated_at = db.Column(db.DateTime)

    def auto_remark(self):
        due = int(self.due or 0)
        if due == 0:
            return "Completely paid"
        if not (self.policy_amt or 0):
            return "Policy amount missing"
        return f"Amount pending: Rs. {self.amount_due:,.0f} ({due} due)"

    def recalc(self):
        """Amount Due = Policy Amt x Due; refresh auto remark unless user-written."""
        self.amount_due = round(float(self.policy_amt or 0) * int(self.due or 0), 2)
        if not self.remark_manual:
            self.remark = self.auto_remark()


class AuditLog(db.Model):
    """Who did what and when (record edits, deliveries, uploads, user changes)."""

    __tablename__ = "audit_log"

    id = db.Column(db.Integer, primary_key=True)
    at = db.Column(db.DateTime, default=now_ist, index=True)
    user = db.Column(db.String(60), default="")
    action = db.Column(db.String(40), default="")
    record_id = db.Column(db.Integer, index=True)  # not a foreign key: history outlives the record
    member_code = db.Column(db.String(40), default="")
    member_name = db.Column(db.String(150), default="")
    details = db.Column(db.Text, default="")


class PendingUpload(db.Model):
    """Parsed upload waiting for the user to confirm the preview."""

    __tablename__ = "pending_uploads"

    token = db.Column(db.String(64), primary_key=True)
    user_id = db.Column(db.Integer, nullable=False)
    filename = db.Column(db.String(200), default="")
    payload = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=now_ist)


def log_event(username, action, details="", record=None):
    """Queue an audit entry; the caller commits."""
    db.session.add(
        AuditLog(
            user=username,
            action=action,
            details=(details or "")[:2000],
            record_id=record.id if record is not None else None,
            member_code=record.member_code if record is not None else "",
            member_name=record.member_name if record is not None else "",
        )
    )


def apply_branch(query, branch):
    """Restrict a Record query to one branch (matched on the sheet's Branch Name text)."""
    name = func.coalesce(Record.branch_name, "")
    if branch == "Other":
        for named in NAMED_BRANCHES:
            query = query.filter(~name.ilike(f"%{named}%"))
        return query
    return query.filter(name.ilike(f"%{branch}%"))


def scoped_records(user):
    """Query of records the given user is allowed to see (admin: all)."""
    query = Record.query
    if user.is_admin:
        return query
    return apply_branch(query, user.branch)


def branch_allowed(user, branch_name):
    """True if the user may create/modify rows of this branch (admin: always)."""
    if user.is_admin:
        return True
    name = (branch_name or "").lower()
    if user.branch == "Other":
        return not any(b.lower() in name for b in NAMED_BRANCHES)
    return user.branch.lower() in name