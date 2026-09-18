import sqlite3

from flask import Flask, render_template, request, redirect, url_for, session, flash
from functools import wraps

from laboratorysystem import (
    init_db,
    AuthController,
    InventoryController,
    DB_NAME,
    sync_sqlite_to_supabase
)


app = Flask(__name__)

# Change this later to a stronger secret key.
app.secret_key = "campus-hardware-inventory-secret-key"


# ==========================================
# DATABASE INITIALIZATION
# ==========================================

init_db()


# ==========================================
# CONTROLLERS
# ==========================================

auth_controller = AuthController()
inventory_controller = InventoryController()


# ==========================================
# LOGIN REQUIRED DECORATOR
# ==========================================

def login_required(view_function):

    @wraps(view_function)
    def wrapped_view(*args, **kwargs):

        if "username" not in session:
            flash("Please log in first.", "warning")
            return redirect(url_for("login"))

        return view_function(*args, **kwargs)

    return wrapped_view


# ==========================================
# HOME
# ==========================================

@app.route("/")
def index():
    if "username" in session:
        return redirect(url_for("dashboard"))

    return redirect(url_for("account_type"))


# ==========================================
# LOGIN
# ==========================================

@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        success, message = auth_controller.login_user(
            username,
            password
        )

        if success:

            session.clear()
            session["username"] = username

            profile = auth_controller.get_user_profile(username)

            if profile:
                session["user_id"] = profile[0]
                session["email"] = profile[1]
                session["role"] = profile[2]

            return redirect(url_for("dashboard"))

        if message == "ACCOUNT_LOCKED":
            flash(
                "Your account has been locked due to 3 failed attempts. "
                "Please request a password reset.",
                "danger"
            )
        else:
            flash(message, "danger")

    return render_template("login.html")

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        success, message = auth_controller.login_user(username, password)

        if success:
            profile = auth_controller.get_user_profile(username)

            if profile and profile[2] == "Admin":
                session.clear()
                session["username"] = username
                session["role"] = "Administrator"
                session["email"] = profile[1]
            
                return redirect(url_for("dashboard"))

            flash("This account is not a Lab Technician account.", "danger")
        else:
            if message == "ACCOUNT_LOCKED":
                flash(
                    "Your account has been locked due to 3 failed attempts. "
                    "Please request a password reset.",
                    "danger"
                )
            else:
                flash(message, "danger")

    return render_template("admin_login.html")

# ==========================================
# REGISTER
# ==========================================

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip()
        student_number = request.form.get("student_number", "").strip()
        year_level = request.form.get("year_level", "").strip()
        program = request.form.get("program", "").strip()
        password = request.form.get("password", "")

        success, message = auth_controller.register_user(
            full_name,
            username,
            email,
            student_number,
            year_level,
            program,
            password,
            "User"
        )

        if success:
            flash(message, "success")
            return redirect(url_for("login"))

        flash(message, "danger")

    return render_template("register.html")

@app.route("/admin/register", methods=["GET", "POST"])
def admin_register():
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip()
        employee_id = request.form.get("employee_id", "").strip()
        building = request.form.get("building", "").strip()
        laboratory_room = request.form.get("laboratory_room", "").strip()
        password = request.form.get("password", "")

        success, message = auth_controller.register_lab_technician(
            full_name,
            username,
            email,
            employee_id,
            building,
            laboratory_room,
            password
        )

        if success:
            flash(message, "success")
            return redirect(url_for("admin_login"))

        flash(message, "danger")

    return render_template("admin_register.html")

# ==========================================
# PASSWORD RESET REQUEST
# ==========================================

@app.route("/reset-request", methods=["GET", "POST"])
def reset_request():

    if request.method == "POST":

        username = request.form.get(
            "username",
            ""
        ).strip()

        email = request.form.get(
            "email",
            ""
        ).strip()

        success, message = (
            auth_controller.request_password_reset(
                username,
                email
            )
        )

        if success:

            flash(message, "success")
            return redirect(url_for("login"))

        flash(message, "danger")

    return render_template("reset_request.html")


# ==========================================
# DASHBOARD
# ==========================================

@app.route("/dashboard")
@login_required
def dashboard():
    username = session["username"]
    role = session.get("role", "User")

    items = inventory_controller.fetch_all_items()

    total_stocks = sum(
        int(item[4])
        for item in items
    )

    # ======================================================
    # STUDENT PROFILE INFORMATION
    # ======================================================

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            full_name,
            student_number,
            year_level,
            program,
            employee_id,
            building,
            laboratory_room
        FROM users
        WHERE username = ?
    """, (username,))

    user_profile = cursor.fetchone()

        # ======================================================
    # STUDENT DASHBOARD DATA
    # ======================================================

    borrowed_items = 0
    pending_borrows = 0
    pending_returns = 0
    password_reset_requests = 0
    active_holds = 0

    if role != "Administrator":

        # ----------------------------------------------
        # ACTIVE BORROWED QUANTITY
        # ----------------------------------------------
        cursor.execute("""
            SELECT COALESCE(SUM(quantity), 0)
            FROM borrow_requests
            WHERE username = ?
              AND status IN ('Pending', 'Approved')
              AND (
                  return_status IS NULL
                  OR return_status = 'Rejected'
              )
        """, (username,))

        borrowed_items = cursor.fetchone()[0] or 0

        # ----------------------------------------------
        # PENDING BORROW REQUESTS
        # ----------------------------------------------
        cursor.execute("""
            SELECT COUNT(*)
            FROM borrow_requests
            WHERE username = ?
              AND status = 'Pending'
        """, (username,))

        pending_borrows = cursor.fetchone()[0] or 0

        # ----------------------------------------------
        # PENDING RETURNS
        # ----------------------------------------------
        cursor.execute("""
            SELECT COUNT(*)
            FROM borrow_requests
            WHERE username = ?
              AND return_status = 'Pending'
        """, (username,))

        pending_returns = cursor.fetchone()[0] or 0

        cursor.execute("""
            SELECT COUNT(*)
            FROM item_holds
            WHERE username = ?
            AND status IN ('Active', 'Ready for Pickup')
        """, (username,))

        active_holds = cursor.fetchone()[0] or 0

    # ======================================================
    # ADMIN DASHBOARD DATA
    # ======================================================

    active_borrowers = 0
    pending_holds = 0
    pending_item_requests = 0
    admin_active_borrows = []

    if role != "Administrator":
        cursor.execute("""
            SELECT COUNT(*)
            FROM item_requests
            WHERE username = ?
            AND status = 'Pending'
        """, (username,))
        pending_item_requests = cursor.fetchone()[0] or 0

    if role == "Administrator":

        # Number of students currently borrowing items
        cursor.execute("""
            SELECT COALESCE(SUM(quantity), 0)
            FROM borrow_requests
            WHERE status IN ('Pending', 'Approved')
            AND (
                return_status IS NULL
                OR return_status = 'Rejected'
            )
        """)
        active_borrowers = cursor.fetchone()[0] or 0

        # Pending Holds
        cursor.execute("""
            SELECT COUNT(*)
            FROM item_holds
            WHERE status IN ('Active', 'Ready for Pickup')
        """)

        pending_holds = cursor.fetchone()[0] or 0

        # Pending item requests
        cursor.execute("""
            SELECT COUNT(*)
            FROM item_requests
            WHERE status = 'Pending'
        """)

        pending_item_requests = cursor.fetchone()[0] or 0

        # All students currently borrowing items
        cursor.execute("""
            SELECT
                br.username,
                h.item_name,
                br.quantity,
                br.borrowed_at
            FROM borrow_requests br
            JOIN hardware h
                ON br.item_id = h.item_id
            WHERE br.status IN ('Pending', 'Approved')
              AND (br.return_status IS NULL OR br.return_status = 'Rejected')
            ORDER BY br.borrowed_at ASC
        """)

        rows = cursor.fetchall()

        for row in rows:
            admin_active_borrows.append({
                "username": row[0],
                "item_name": row[1],
                "quantity": row[2],
                "borrowed_at": row[3] or "-"
            })

    conn.close()

    # ======================================================
    # PROFILE VALUES
    # ======================================================

    if user_profile:
        full_name = user_profile[0]
        student_number = user_profile[1] or ""
        year_level = user_profile[2] or ""
        program = user_profile[3] or ""
        employee_id = user_profile[4] or ""
        building = user_profile[5] or ""
        laboratory_room = user_profile[6] or ""
    else:
        full_name = username
        student_number = ""
        year_level = ""
        program = ""
        employee_id = ""
        building = ""
        laboratory_room = ""

    return render_template(
        "dashboard.html",

        username=username,
        role=role,

        total_stocks=total_stocks,

        # Student dashboard
        borrowed_items=borrowed_items,
        pending_borrows=pending_borrows,
        pending_returns=pending_returns,
        password_reset_requests=password_reset_requests,
        active_holds=active_holds,

        # Student profile
        full_name=full_name,
        student_number=student_number,
        year_level=year_level,
        program=program,

        # Admin profile
        employee_id=employee_id,
        building=building,
        laboratory_room=laboratory_room,

        # Admin dashboard
        active_borrowers=active_borrowers,
        pending_holds=pending_holds,
        pending_item_requests=pending_item_requests,
        admin_active_borrows=admin_active_borrows
    )

# ==========================================
# PROFILE
# ==========================================

@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    username = session["username"]

    if request.method == "POST":
        current_password = request.form.get("old_password", "")
        new_password = request.form.get("new_password", "")

        success, message = auth_controller.update_password(
            username,
            current_password,
            new_password
        )

        if success:
            flash(message, "success")
            return redirect(url_for("profile"))

        flash(message, "danger")
        return redirect(url_for("profile"))

    profile_data = auth_controller.get_user_profile(username)

    if not profile_data:
        flash("Unable to load profile.", "danger")
        return redirect(url_for("dashboard"))

    profile = {
        "username": profile_data[0],
        "email": profile_data[1],
        "role": profile_data[2]
    }

    return render_template(
        "profile.html",
        profile=profile
    )


# ==========================================
# HARDWARE CATALOG
# ==========================================

@app.route("/catalog")
@login_required
def catalog():
    search = request.args.get("search", "").strip()
    category = request.args.get("category", "All")

    items = inventory_controller.fetch_all_items(
        search,
        category
    )

    categories = inventory_controller.get_categories()
    hardware_items = []

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    for item in items:
        cursor.execute("""
            SELECT COALESCE(SUM(quantity), 0)
            FROM borrow_requests
            WHERE username = ?
              AND item_id = ?
              AND status IN ('Pending', 'Approved')
              AND (
                  return_status IS NULL
                  OR return_status = 'Rejected'
              )
        """, (
            session["username"],
            item[0]
        ))

        borrowed_quantity = cursor.fetchone()[0] or 0

        hardware_items.append({
            "id": item[0],
            "name": item[1],
            "category": item[2],
            "initial_quantity": item[3],
            "quantity": item[4],
            "unit_price": item[5],
            "status": item[6],
            "borrowed_quantity": borrowed_quantity
        })

    conn.close()

    return render_template(
        "catalog.html",
        items=hardware_items,
        categories=categories,
        search=search,
        selected_category=category
    )

@app.route("/borrow", methods=["POST"])
@login_required
def borrow_item():
    item_id = request.form.get("item_id", "").strip()
    quantity = request.form.get("quantity", "").strip()

    success, message = inventory_controller.request_borrow(
        session["username"],
        item_id,
        quantity
    )

    if success:
        flash(message, "success")
    else:
        flash(message, "danger")

    return redirect(url_for("catalog"))

@app.route("/place-hold", methods=["POST"])
@login_required
def place_hold():
    item_id = request.form.get("item_id", "").strip()
    quantity = request.form.get("quantity", "").strip()

    success, message = inventory_controller.place_hold(
        session["username"],
        item_id,
        quantity
    )

    if success:
        flash(message, "success")
    else:
        flash(message, "danger")

    return redirect(url_for("catalog"))

@app.route("/return", methods=["POST"])
@login_required
def return_item():
    item_id = request.form.get("item_id", "").strip()
    quantity = request.form.get("quantity", "").strip()

    success, message = inventory_controller.request_return(
        session["username"],
        item_id,
        quantity
    )

    if success:
        flash(message, "success")
    else:
        flash(message, "danger")

    return redirect(url_for("catalog"))

@app.route("/admin/equipment", methods=["GET", "POST"])
@login_required
def admin_equipment():
    if session.get("role") != "Administrator":
        flash("Administrator access required.", "danger")
        return redirect(url_for("dashboard"))

    if request.method == "POST":

        action = request.form.get("action", "").strip()

        item_id = request.form.get("item_id", "").strip()
        item_name = request.form.get("item_name", "").strip()
        category = request.form.get("category", "").strip()
        quantity = request.form.get("quantity", "").strip()
        unit_price = request.form.get("unit_price", "").strip()

        if action == "update":
            result, message = inventory_controller.update_item_record(
                item_id,
                item_name,
                category,
                quantity,
                unit_price
            )

            if result == "updated":
                flash(message, "success")
            else:
                flash(message, "danger")

        else:
            result, message = inventory_controller.add_item(
                item_name,
                category,
                quantity,
                unit_price
            )

            if result == "added":
                flash(message, "success")
            else:
                flash(message, "danger")

        return redirect(url_for("admin_equipment"))

    search = request.args.get("search", "").strip()
    category = request.args.get("category", "All")

    items = inventory_controller.fetch_all_items(search, category)
    categories = inventory_controller.get_categories()

    hardware_items = []

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    for item in items:
        cursor.execute("""
            SELECT COALESCE(SUM(quantity), 0)
            FROM borrow_requests
            WHERE item_id = ?
            AND status IN ('Pending', 'Approved')
            AND (
                return_status IS NULL
                OR return_status = 'Rejected'
            )
        """, (item[0],))

        borrowed_quantity = cursor.fetchone()[0] or 0

        hardware_items.append({
            "id": item[0],
            "name": item[1],
            "category": item[2],
            "initial_quantity": item[3],
            "quantity": item[4],
            "unit_price": item[5],
            "status": item[6],
            "borrowed_quantity": borrowed_quantity
        })

    conn.close()

    total_stock = sum(
        (item["quantity"] or 0) +
        (item["borrowed_quantity"] or 0)
        for item in hardware_items
    )
        
    return render_template(
        "admin_equipment.html",
        items=hardware_items,
        categories=categories,
        search=search,
        selected_category=category
    )

@app.route("/admin/borrows")
@login_required
def admin_borrows():
    if session.get("role") != "Administrator":
        flash("Administrator access required.", "danger")
        return redirect(url_for("dashboard"))

    loans = inventory_controller.get_all_loans_history()

    borrow_history = []

    for row in loans:
        borrow_history.append({
            "id": row[0],
            "username": row[1],
            "item_name": row[2],
            "quantity": row[3],
            "borrowed_at": row[4] or "-",
            "due_date": row[5] or "-",
            "status": row[6],
            "renewal_count": row[7] or 0,
            "return_status": row[8],
            "returned_at": row[9] or "-"
        })

    return render_template(
        "admin_borrows.html",
        borrow_history=borrow_history
    )
@app.route("/admin/queue-holds-manager", methods=["GET", "POST"])
def queue_holds_manager():
    if "username" not in session:
        return redirect(url_for("login"))

    if session.get("role") != "Administrator":
        flash("Administrator access required.", "danger")
        return redirect(url_for("dashboard"))

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    if request.method == "POST":
        action = request.form.get("action", "").strip()
        hold_ids = request.form.getlist("hold_ids")

        if not hold_ids:
            conn.close()
            flash("Please select at least one hold.", "warning")
            return redirect(url_for("queue_holds_manager"))

        if action == "ready_for_pickup":
            for hold_id in hold_ids:
                cursor.execute("""
                    UPDATE item_holds
                    SET status = 'Ready for Pickup'
                    WHERE rowid = ?
                      AND status = 'Active'
                """, (hold_id,))

            conn.commit()
            sync_sqlite_to_supabase()
            conn.close()

            flash("Selected hold(s) are ready for pickup.", "success")
            return redirect(url_for("queue_holds_manager"))

        elif action == "claimed":
            try:
                for hold_id in hold_ids:

                    # Get the selected hold and its hardware item
                    cursor.execute("""
                        SELECT
                            ih.username,
                            ih.item_id,
                            ih.quantity,
                            ih.status,
                            h.quantity
                        FROM item_holds ih
                        JOIN hardware h
                            ON ih.item_id = h.item_id
                        WHERE ih.rowid = ?
                    """, (hold_id,))

                    hold = cursor.fetchone()

                    if not hold:
                        raise Exception("Selected hold was not found.")

                    username = hold[0]
                    item_id = hold[1]
                    hold_quantity = hold[2]
                    hold_status = hold[3]
                    available_quantity = hold[4]

                    # Only Ready for Pickup holds can be claimed
                    if hold_status != "Ready for Pickup":
                        raise Exception(
                            f"Hold #{hold_id} is not ready for pickup."
                        )

                    # Make sure there is enough stock
                    if hold_quantity > available_quantity:
                        raise Exception(
                            f"Not enough stock available for hold #{hold_id}."
                        )

                    # Decrease hardware stock
                    new_quantity = available_quantity - hold_quantity

                    if new_quantity > 5:
                        new_status = "In Stock"
                    elif new_quantity >= 1:
                        new_status = "Low Stock"
                    else:
                        new_status = "Out of Stock"

                    cursor.execute("""
                        UPDATE hardware
                        SET quantity = ?,
                            status = ?
                        WHERE item_id = ?
                    """, (
                        new_quantity,
                        new_status,
                        item_id
                    ))

                    # Create the actual borrow record
                    cursor.execute("""
                        INSERT INTO borrow_requests
                        (
                            username,
                            item_id,
                            quantity,
                            status,
                            borrowed_at,
                            due_date
                        )
                        VALUES (
                            ?,
                            ?,
                            ?,
                            'Pending',
                            datetime('now', 'localtime'),
                            datetime('now', 'localtime', '+3 days')
                        )
                    """, (
                        username,
                        item_id,
                        hold_quantity
                    ))

                    # Mark the hold as Claimed
                    cursor.execute("""
                        UPDATE item_holds
                        SET status = 'Claimed'
                        WHERE rowid = ?
                        AND status = 'Ready for Pickup'
                    """, (hold_id,))

                conn.commit()
                sync_sqlite_to_supabase()

                conn.close()

                flash(
                    "Selected hold(s) have been claimed and converted to borrow requests.",
                    "success"
                )

            except Exception as e:
                conn.rollback()
                conn.close()

                flash(
                    f"Unable to claim selected hold(s): {e}",
                    "danger"
                )

            return redirect(url_for("queue_holds_manager"))

        else:
            conn.close()
            flash("Invalid queue action.", "danger")
            return redirect(url_for("queue_holds_manager"))

    cursor.execute("""
        SELECT
            ih.rowid,
            ih.username,
            h.item_name,
            ih.quantity,
            ih.hold_date,
            ih.status
        FROM item_holds ih
        JOIN hardware h
            ON ih.item_id = h.item_id
        WHERE ih.status IN ('Active', 'Ready for Pickup')
        ORDER BY ih.hold_date ASC, ih.rowid ASC
    """)

    rows = cursor.fetchall()
    conn.close()

    holds = []

    for index, row in enumerate(rows, start=1):
        holds.append({
            "hold_id": row[0],
            "queue_position": index,
            "username": row[1],
            "item_name": row[2],
            "quantity": row[3],
            "hold_date": row[4] or "-",
            "pickup_status": row[5]
        })

    return render_template(
        "queue_holdsmanager.html",
        holds=holds
    )
# ==========================================
# ADMIN ITEM REQUESTS
# ==========================================
@app.route("/admin/item-requests", methods=["GET", "POST"])
@login_required
def admin_item_requests():
    if session.get("role") != "Administrator":
        flash("Administrator access required.", "danger")
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        action = request.form.get("action", "").strip()
        request_ids = request.form.getlist("request_ids")

        if not request_ids:
            flash("Please select at least one item request.", "warning")
            return redirect(url_for("admin_item_requests"))

        if action == "approve":
            approve_quantity = request.form.get(
                "approve_qty",
                ""
            ).strip()

            unit_price = request.form.get(
                "unit_price",
                ""
            ).strip()

            try:
                approve_quantity = int(approve_quantity)

                if approve_quantity < 1:
                    raise ValueError

                unit_price = float(unit_price)

                if unit_price < 0:
                    raise ValueError

            except ValueError:
                flash(
                    "Quantity must be a whole number of at least 1, "
                    "and unit price must be 0 or greater.",
                    "danger"
                )
                return redirect(url_for("admin_item_requests"))

            success_count = 0
            error_messages = []

            for request_id in request_ids:
                success, message = (
                    inventory_controller.approve_item_request(
                        request_id,
                        approve_quantity,
                        unit_price
                    )
                )

                if success:
                    success_count += 1
                else:
                    error_messages.append(message)

            if success_count > 0:
                flash(
                    f"{success_count} item request(s) approved "
                    "and added to inventory.",
                    "success"
                )

            if error_messages:
                flash(
                    "Some requests could not be approved: "
                    + " | ".join(error_messages),
                    "danger"
                )

            return redirect(url_for("admin_item_requests"))

        elif action == "decline":
            success_count = 0
            error_messages = []

            for request_id in request_ids:
                success, message = (
                    inventory_controller.reject_item_request(
                        request_id
                    )
                )

                if success:
                    success_count += 1
                else:
                    error_messages.append(message)

            if success_count > 0:
                flash(
                    f"{success_count} item request(s) declined.",
                    "success"
                )

            if error_messages:
                flash(
                    "Some requests could not be declined: "
                    + " | ".join(error_messages),
                    "danger"
                )

            return redirect(url_for("admin_item_requests"))

        else:
            flash("Invalid item request action.", "danger")
            return redirect(url_for("admin_item_requests"))

    # ------------------------------------------
    # LOAD ITEM REQUESTS
    # ------------------------------------------
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            rowid AS id,
            username,
            item_name,
            category,
            quantity,
            status,
            requested_at,
            unit_price
        FROM item_requests
        ORDER BY rowid ASC
    """)

    rows = cursor.fetchall()
    conn.close()

    item_requests = []

    for row in rows:
        item_requests.append({
            "id": row[0],
            "username": row[1],
            "item_name": row[2],
            "category": row[3],
            "quantity": row[4],
            "status": row[5],
            "request_date": row[6] or "-",
            "unit_price": row[7] or 0
        })

    return render_template(
        "admin_item_requests.html",
        item_requests=item_requests
    )

@app.route("/admin/approve-borrow", methods=["POST"])
@login_required
def approve_borrow():
    if session.get("role") != "Administrator":
        flash("Administrator access required.", "danger")
        return redirect(url_for("dashboard"))

    request_id = request.form.get("request_id", "").strip()

    success, message = inventory_controller.approve_borrow(request_id)

    flash(message, "success" if success else "danger")
    return redirect(url_for("admin_approvals"))

@app.route("/admin/return-item", methods=["POST"])
@login_required
def admin_return_item():
    if session.get("role") != "Administrator":
        flash("Administrator access required.", "danger")
        return redirect(url_for("dashboard"))

    borrow_id = request.form.get("borrow_id", "").strip()

    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()

        # Get the borrowed item
        cursor.execute("""
            SELECT item_id, quantity, status, return_status
            FROM borrow_requests
            WHERE id = ?
        """, (borrow_id,))

        borrow = cursor.fetchone()

        if not borrow:
            conn.close()
            flash("Borrow record not found.", "danger")
            return redirect(url_for("admin_borrows"))

        item_id, borrow_quantity, borrow_status, return_status = borrow

        # Prevent returning an already returned item
        if borrow_status == "Returned":
            conn.close()
            flash("This item has already been returned.", "danger")
            return redirect(url_for("admin_borrows"))

        # Return the equipment quantity to inventory
        cursor.execute("""
            SELECT quantity
            FROM hardware
            WHERE item_id = ?
        """, (item_id,))

        hardware = cursor.fetchone()

        if not hardware:
            conn.close()
            flash("Hardware item not found.", "danger")
            return redirect(url_for("admin_borrows"))

        current_quantity = hardware[0]
        new_quantity = current_quantity + borrow_quantity
        new_status = inventory_controller.calculate_status(new_quantity)

        cursor.execute("""
            UPDATE hardware
            SET quantity = ?,
                status = ?
            WHERE item_id = ?
        """, (
            new_quantity,
            new_status,
            item_id
        ))

        # Mark the borrow as returned
        cursor.execute("""
            UPDATE borrow_requests
            SET status = 'Returned',
                return_status = 'Approved',
                return_quantity = ?,
                returned_at = datetime('now')
            WHERE id = ?
        """, (
            borrow_quantity,
            borrow_id
        ))

        conn.commit()
        sync_sqlite_to_supabase()
        conn.close()

        flash("Item returned successfully.", "success")

    except sqlite3.Error:
        flash("Database error occurred while returning the item.", "danger")

    return redirect(url_for("admin_borrows"))

@app.route("/admin/reject-borrow", methods=["POST"])
@login_required
def reject_borrow():
    if session.get("role") != "Administrator":
        flash("Administrator access required.", "danger")
        return redirect(url_for("dashboard"))

    request_id = request.form.get("request_id", "").strip()

    success, message = inventory_controller.reject_borrow(request_id)

    flash(message, "success" if success else "danger")
    return redirect(url_for("admin_approvals"))

@app.route("/admin/approve-return", methods=["POST"])
@login_required
def approve_return():
    if session.get("role") != "Administrator":
        flash("Administrator access required.", "danger")
        return redirect(url_for("dashboard"))

    request_id = request.form.get("request_id", "").strip()

    success, message = inventory_controller.approve_return(request_id)

    flash(message, "success" if success else "danger")
    return redirect(url_for("admin_approvals"))

@app.route("/admin/reject-return", methods=["POST"])
@login_required
def reject_return():
    if session.get("role") != "Administrator":
        flash("Administrator access required.", "danger")
        return redirect(url_for("dashboard"))

    request_id = request.form.get("request_id", "").strip()

    success, message = inventory_controller.reject_return(request_id)

    flash(message, "success" if success else "danger")
    return redirect(url_for("admin_approvals"))

@app.route("/admin/approvals")
@login_required
def admin_approvals():
    if session.get("role") != "Administrator":
        flash("Administrator access required.", "danger")
        return redirect(url_for("dashboard"))

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            br.id,
            br.username,
            h.item_name,
            br.quantity,
            br.status,
            br.borrowed_at
        FROM borrow_requests br
        JOIN hardware h ON br.item_id = h.item_id
        WHERE br.status = 'Pending'
        ORDER BY br.id ASC
    """)
    borrow_requests = cursor.fetchall()

    cursor.execute("""
        SELECT
            id,
            username,
            email,
            status
        FROM password_resets
        WHERE status = 'Pending'
        ORDER BY id DESC
    """)
    password_reset_requests = cursor.fetchall()

    cursor.execute("""
        SELECT
            br.id,
            br.username,
            h.item_name,
            br.return_quantity,
            br.return_status,
            br.returned_at
        FROM borrow_requests br
        JOIN hardware h ON br.item_id = h.item_id
        WHERE br.return_status = 'Pending'
        ORDER BY br.id ASC
    """)

    return_requests = cursor.fetchall()

    conn.close()

    reset_result = session.pop(
    "reset_result",
    None
)

    return render_template(
        "admin_approvals.html",
        borrow_requests=borrow_requests,
        return_requests=return_requests,
        password_reset_requests=password_reset_requests,
        reset_result=reset_result
)

@app.route("/admin/reset-password", methods=["POST"])
@login_required
def admin_reset_password():

    if session.get("role") != "Administrator":

        flash(
            "Administrator access required.",
            "danger"
        )

        return redirect(
            url_for("dashboard")
        )


    request_id = request.form.get(
        "request_id",
        ""
    ).strip()


    action = request.form.get(
        "action",
        ""
    ).strip()


    # =====================================================
    # APPROVE
    # =====================================================

    if action == "approve":

        success, message, temporary_password = (
            auth_controller.approve_password_reset(
                request_id
            )
        )


        if success:

            # Store the generated password temporarily
            # so the admin page can display it after redirect.

            session["reset_result"] = {
                "username": request.form.get(
                    "username",
                    ""
                ).strip(),

                "password": temporary_password
            }


            flash(
                message,
                "success"
            )

        else:

            flash(
                message,
                "danger"
            )


    # =====================================================
    # DECLINE
    # =====================================================

    elif action == "decline":

        success, message = (
            auth_controller.reject_password_reset(
                request_id
            )
        )


        flash(
            message,
            "success" if success else "danger"
        )


    else:

        flash(
            "Invalid password reset action.",
            "danger"
        )


    return redirect(
        url_for("admin_approvals")
    )

@app.route("/borrow-history")
@login_required
def borrow_history():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            h.item_name,
            br.quantity,
            br.status,
            br.return_status,
            br.borrowed_at,
            br.returned_at
        FROM borrow_requests br
        JOIN hardware h ON br.item_id = h.item_id
        WHERE br.username = ?
        ORDER BY br.id ASC
    """, (session["username"],))

    rows = cursor.fetchall()
    conn.close()

    history = []

    for row in rows:
        history.append({
            "item_name": row[0],
            "quantity": row[1],
            "borrow_status": row[2],
            "return_status": row[3] or "Not Returned",
            "borrowed_at": row[4] or "-",
            "returned_at": row[5] or "-"
        })

    return render_template(
        "borrow_history.html",
        history=history
    )
# ==========================================
# ACTIVE HOLDS
# ==========================================

# ==========================================
# ACTIVE HOLDS
# ==========================================
@app.route("/active-holds")
@login_required
def active_holds():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            ih.rowid,
            h.item_name,
            ih.hold_date,
            ih.status
        FROM item_holds ih
        JOIN hardware h
            ON ih.item_id = h.item_id
        WHERE ih.username = ?
          AND ih.status IN ('Active', 'Ready for Pickup')
        ORDER BY ih.rowid ASC
    """, (session["username"],))

    rows = cursor.fetchall()
    conn.close()

    holds = []

    for index, row in enumerate(rows, start=1):
        holds.append({
            "hold_id": index,
            "item_name": row[1],
            "quantity": 1,
            "hold_date": row[2] or "-",
            "queue_status": "Active Hold",
            "pickup_status": row[3]
        })

    return render_template(
        "active_holds.html",
        holds=holds
    )

@app.route("/request-history", methods=["GET", "POST"])
@login_required
def request_history():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    if request.method == "POST":
        item_name = request.form.get("item_name", "").strip()
        category = request.form.get("category", "").strip()
        quantity = request.form.get("quantity", "").strip()

        if not item_name or not category or not quantity:
            flash("Please complete all request fields.", "danger")
            conn.close()
            return redirect(url_for("request_history"))

        try:
            quantity = int(quantity)

            if quantity < 1:
                raise ValueError

            cursor.execute("""
                INSERT INTO item_requests (
                    username,
                    item_name,
                    category,
                    quantity,
                    status,
                    requested_at
                )
                VALUES (?, ?, ?, ?, 'Pending', datetime('now', 'localtime'))
            """, (
                session["username"],
                item_name,
                category,
                quantity
            ))
            conn.commit()
            sync_sqlite_to_supabase()
            flash("Item request submitted successfully.", "success")

        except ValueError:
            flash("Quantity must be a whole number of at least 1.", "danger")

        conn.close()
        return redirect(url_for("request_history"))

    cursor.execute("""
        SELECT item_name, category, quantity, status, requested_at AS request_date
        FROM item_requests
        WHERE username = ?
        ORDER BY requested_at ASC
    """, (session["username"],))

    rows = cursor.fetchall()
    conn.close()

    requests = []

    for row in rows:
        requests.append({
            "item_name": row[0],
            "category": row[1],
            "quantity": row[2],
            "status": row[3],
            "request_date": row[4]
        })

    return render_template(
        "request_history.html",
        requests=requests
    )

@app.route("/export")
@login_required
def export_inventory():
    success, message = inventory_controller.export_to_csv()

    if success:
        flash(message, "success")
    else:
        flash(message, "danger")

    return redirect(url_for("catalog"))

# ==========================================
# LOGOUT
# ==========================================
@app.route("/account-type", endpoint="account_type")
def account_type():
    return render_template("choose_account.html")

@app.route("/logout")
def logout():

    session.clear()
    return redirect(url_for("account_type"))

# ==========================================
# RUN APPLICATION
# ==========================================

if __name__ == "__main__":

    print()
    print("=" * 58)
    print("       CAMPUS HARDWARE INVENTORY - WEB PORTAL")
    print("=" * 58)
    print("Running at: http://127.0.0.1:5000")
    print("Also try: http://localhost:5000")
    print()
    print(" Press CTRL+C to stop the server.")
    print("=" * 58)
    print()

    app.run(
        host="127.0.0.1",
        port=5000,
        debug=True
    )
