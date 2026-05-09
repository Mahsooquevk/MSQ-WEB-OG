from flask import Flask, render_template, request, redirect, session, flash
import sqlite3
DB_PATH = r"C:\Users\MSQ-LAP\Desktop\project-BNK\bank.db"
from datetime import datetime

app = Flask(__name__)
app.secret_key = "secret123"
# ---------------- msq date and time update temporary) ----------------
TEST_DATE = None  # e.g. datetime(2030, 5, 27) — set None for production

def get_now():
    return TEST_DATE if TEST_DATE is not None else datetime.now()

# ---------------- DB MIGRATION (runs once on startup) ----------------
def migrate_db():
    """Expand transactions CHECK constraint to allow service_charge type."""
    import sqlite3 as _sq
    conn = sqlite3.connect(DB_PATH)
    try:
        # Test if service_charge is already allowed
        conn.execute("INSERT INTO transactions(customer_id,type,amount,date) VALUES(0,'service_charge',0,'test')")
        conn.execute("DELETE FROM transactions WHERE customer_id=0 AND date='test'")
        conn.commit()
    except _sq.IntegrityError:
        # Still blocked by old CHECK — recreate the table
        conn.executescript("""
            PRAGMA foreign_keys=OFF;
            CREATE TABLE transactions_new (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER,
                type        TEXT CHECK(type IN ('deposit','withdraw','service_charge')),
                amount      REAL,
                date        TEXT
            );
            INSERT INTO transactions_new SELECT * FROM transactions;
            DROP TABLE transactions;
            ALTER TABLE transactions_new RENAME TO transactions;
            PRAGMA foreign_keys=ON;
        """)
        conn.commit()
    finally:
        conn.close()

    # Add description and remark columns to service_expense if not already present
    conn2 = _sq.connect(DB_PATH)
    try:
        conn2.execute("ALTER TABLE service_expense ADD COLUMN description TEXT")
        conn2.commit()
    except Exception:
        pass  # Column already exists
    try:
        conn2.execute("ALTER TABLE service_expense ADD COLUMN remark TEXT")
        conn2.commit()
    except Exception:
        pass  # Column already exists
    finally:
        conn2.close()

    # Add account closure columns to customers table
    conn3 = _sq.connect(DB_PATH)
    try:
        conn3.execute("ALTER TABLE customers ADD COLUMN is_closed INTEGER DEFAULT 0")
        conn3.commit()
    except Exception:
        pass
    try:
        conn3.execute("ALTER TABLE customers ADD COLUMN closed_date TEXT")
        conn3.commit()
    except Exception:
        pass
    try:
        conn3.execute("ALTER TABLE customers ADD COLUMN settlement_amount REAL DEFAULT 0")
        conn3.commit()
    except Exception:
        pass
    try:
        conn3.execute("ALTER TABLE customers ADD COLUMN closure_note TEXT")
        conn3.commit()
    except Exception:
        pass
    finally:
        conn3.close()

migrate_db()

# ---------------- DB CONNECTION ----------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# ---------------- SESSION TIMEOUT ----------------
@app.before_request
def session_timeout():
    if "user" in session:
        now = get_now().timestamp()
        last_activity = session.get("last_activity", now)
        if now - last_activity > 300:
            session.clear()
            return redirect("/")
        session["last_activity"] = now

# ---------------- LOGIN ----------------
@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = request.form["username"]
        pwd = request.form["password"]
        db = get_db()
        result = db.execute(
            "SELECT * FROM users WHERE username=? AND password=?",
            (user, pwd)
        ).fetchone()
        if result:
            session["user"] = user
            session["last_activity"] = get_now().timestamp()
            return redirect("/dashboard")
        else:
            return render_template("login.html", error=True)
    return render_template("login.html")

# ---------------- LOGOUT ----------------
@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

# ---------------- DASHBOARD ----------------
@app.route("/dashboard")
def dashboard():
    if "user" not in session:
        return redirect("/")
 
    db = get_db()
 
    # CHANGED: ensure opening_service_charges table exists
    db.execute("""
        CREATE TABLE IF NOT EXISTS opening_service_charges (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER,
            amount      REAL,
            date        TEXT
        )
    """)
    db.commit()
    apply_annual_service_charges()
    
    total_customers = db.execute("SELECT COUNT(*) FROM customers").fetchone()[0] or 0
 
    deposits = db.execute(
        "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE LOWER(type)='deposit'"
    ).fetchone()[0]
 
    withdraw = db.execute(
        "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE LOWER(type)='withdraw'"
    ).fetchone()[0]
 
    total_loans = db.execute("""
        SELECT COUNT(*) FROM loans
        JOIN customers ON customers.id = loans.customer_id
        WHERE (customers.is_closed = 0 OR customers.is_closed IS NULL)
        AND (
            SELECT COALESCE(SUM(amount), 0) FROM loan_payments WHERE loan_id = loans.id
        ) < loans.loan_amount
    """).fetchone()[0] or 0

    total_loan_amount = db.execute("""
        SELECT COALESCE(SUM(loans.loan_amount),0) FROM loans
        JOIN customers ON customers.id = loans.customer_id
    """).fetchone()[0]

    loan_paid = db.execute("""
        SELECT COALESCE(SUM(lp.amount),0) FROM loan_payments lp
        JOIN loans ON loans.id = lp.loan_id
        JOIN customers ON customers.id = loans.customer_id
    """).fetchone()[0]

    outstanding_loan = total_loan_amount - loan_paid
 
    # CHANGED: service_collected now includes opening deposit service charges
    loan_service = db.execute(
        "SELECT COALESCE(SUM(service_charge),0) FROM loans"
    ).fetchone()[0]
 
    opening_service = db.execute(
        "SELECT COALESCE(SUM(amount),0) FROM opening_service_charges"
    ).fetchone()[0]
 
    annual_service = db.execute(
        "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE type='service_charge'"
    ).fetchone()[0]
    service_collected = loan_service + opening_service + annual_service
 
    service_spent = db.execute(
        "SELECT COALESCE(SUM(amount),0) FROM service_expense"
    ).fetchone()[0]
 
    service_balance = service_collected - service_spent

    total_service = db.execute(
        "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE type='service_charge'"
    ).fetchone()[0]
    available_loan = deposits - withdraw - total_loan_amount + loan_paid - total_service
    balance = deposits - withdraw
 
    # Pending dues count - ensure tenure column exists
    try:
        db.execute("ALTER TABLE loans ADD COLUMN tenure INTEGER")
        db.commit()
    except Exception:
        pass
 
    from datetime import datetime as dt
    # for testing loan due make below from now = get_now() to now = dt(2026, 7, 26)
    now = get_now()
    all_tenure_loans = db.execute("""
        SELECT loans.id, loans.loan_amount, loans.date, loans.tenure,
               COALESCE(SUM(lp.amount), 0) as total_paid
        FROM loans
        JOIN customers ON customers.id = loans.customer_id
        LEFT JOIN loan_payments lp ON lp.loan_id = loans.id
        WHERE loans.tenure IS NOT NULL AND loans.tenure > 0
        AND (customers.is_closed = 0 OR customers.is_closed IS NULL)
        GROUP BY loans.id
        HAVING (loans.loan_amount - total_paid) > 0
    """).fetchall()
 
    # Service charge due count
    all_customers = db.execute("SELECT id, created FROM customers WHERE is_closed=0 OR is_closed IS NULL").fetchall()
    service_charge_due_count = 0
    for c in all_customers:
        if not c["created"]:
            continue
        try:
            created_dt = dt.fromisoformat(str(c["created"]))
            years_active = (get_now() - created_dt).days // 365
        except Exception:
            continue
        already_charged = db.execute(
            "SELECT COUNT(*) FROM annual_service_charges WHERE customer_id=?", (c["id"],)
        ).fetchone()[0]
        if years_active > already_charged:
            service_charge_due_count += 1
 
 
 
    pending_dues_count = 0
    for ln in all_tenure_loans:
        try:
            loan_date = dt.strptime(ln['date'][:19], '%Y-%m-%d %H:%M:%S')
            monthly_due = ln['loan_amount'] / ln['tenure']
            months_passed = (now.year - loan_date.year) * 12 + (now.month - loan_date.month)
            if now.day > loan_date.day:
                months_passed += 1
            months_due = min(months_passed, ln['tenure'])
            expected = monthly_due * months_due
            if expected - ln['total_paid'] > 0:
                pending_dues_count += 1
        except Exception:
            pass
 
 
    return render_template(
        "dashboard.html",
        customers=total_customers,
        deposits=deposits,
        withdraw=withdraw,
        loans=total_loans,
        total_loan_amount=total_loan_amount,
        loan_paid=loan_paid,
        outstanding_loan=outstanding_loan,
        balance=balance,
        available_loan=available_loan,
        service_collected=service_collected,
        service_spent=service_spent,        
        service_balance=service_balance,
        pending_dues=pending_dues_count,
        service_charge_due=service_charge_due_count
    )
 
# ---------------- ACTIVE LOANS REPORT ----------------
@app.route('/active_loans_report')
def active_loans_report():
    if "user" not in session:
        return redirect("/")

    db = get_db()
    active_loans = db.execute("""
        SELECT
            loans.id,
            loans.loan_account_number,
            customers.account_number,
            customers.name,
            customers.mobile,
            loans.loan_amount,
            COALESCE(SUM(lp.amount), 0) as paid,
            loans.loan_amount - COALESCE(SUM(lp.amount), 0) as remaining,
            loans.date
        FROM loans
        JOIN customers ON customers.id = loans.customer_id
        LEFT JOIN loan_payments lp ON lp.loan_id = loans.id
        WHERE customers.is_closed = 0 OR customers.is_closed IS NULL
        GROUP BY loans.id
        HAVING remaining > 0
        ORDER BY loans.date DESC
    """).fetchall()

    return render_template("active_loans_report.html", loans=active_loans)

# ---------------- CREATE ACCOUNT ----------------
@app.route("/create_account", methods=["GET", "POST"])
def create_account():
    if "user" not in session:
        return redirect("/")

    db = get_db()

    # CHANGED: ensure opening_service_charges table exists
    db.execute("""
        CREATE TABLE IF NOT EXISTS opening_service_charges (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER,
            amount      REAL,
            date        TEXT
        )
    """)
    db.commit()

    error = None

    used_numbers = db.execute("SELECT account_number FROM customers").fetchall()
    used_numbers = [int(row["account_number"]) for row in used_numbers if row["account_number"].isdigit()]

    next_acc = None
    for i in range(100, 1001):
        if i not in used_numbers:
            next_acc = i
            break

    if request.method == "POST":
        acc_no  = request.form["account_number"]
        name    = request.form["name"]
        mobile  = request.form["mobile"]
        address = request.form["address"]

        # CHANGED: read opening deposit from form
        opening_deposit = request.form.get("opening_deposit", "0").strip()
        try:
            opening_deposit = float(opening_deposit)
            if opening_deposit < 0:
                raise ValueError
        except ValueError:
            opening_deposit = 0.0

        if not acc_no.isdigit() or not (100 <= int(acc_no) <= 1000):
            error = "Account number must be between 100 and 1000"
        else:
            existing_acc = db.execute(
                "SELECT * FROM customers WHERE account_number=?", (acc_no,)
            ).fetchone()
            existing_mobile = db.execute(
                "SELECT * FROM customers WHERE mobile=?", (mobile,)
            ).fetchone()

            if existing_acc:
                error = f"Account exists! Try {next_acc}"
            elif existing_mobile:
                error = "Mobile number already exists!"
            else:
                now = get_now()

                # 1. Insert customer (same as original)
                db.execute(
                    "INSERT INTO customers(account_number,name,mobile,address,created) VALUES (?,?,?,?,?)",
                    (acc_no, name, mobile, address, now)
                )
                db.commit()

                # 2. Record opening deposit + service charge
                if opening_deposit > 0:
                    new_customer_id = db.execute(
                        "SELECT id FROM customers WHERE account_number=?", (acc_no,)
                    ).fetchone()["id"]

                    # Record the full opening deposit amount
                    db.execute(
                        "INSERT INTO transactions(customer_id,type,amount,date) VALUES (?,?,?,?)",
                        (new_customer_id, "deposit", opening_deposit, now)
                    )

                    # ₹100 service charge deducted separately — balance = deposit - service_charge
                    if opening_deposit >= 100.0:
                        db.execute(
                            "INSERT INTO transactions(customer_id,type,amount,date) VALUES (?,?,?,?)",
                            (new_customer_id, "service_charge", 100.0, now)
                        )
                    db.commit()



                return redirect(f"/create_account?success=1&dep={int(opening_deposit)}")

    return render_template("create_account.html", error=error, next_acc=next_acc)

# ---------------- DEPOSIT ----------------
@app.route("/deposit", methods=["GET", "POST"])
def deposit():
    if "user" not in session:
        return redirect("/")

    db = get_db()
    customers = db.execute("SELECT * FROM customers").fetchall()
    error = None

    if request.method == "POST":
        customer_id = request.form.get("customer")
        amount = request.form.get("amount")

        if not customer_id or not amount:
            error = "❌ All fields are required"
        else:
            # Check if account is closed
            cust_check = db.execute("SELECT is_closed, name FROM customers WHERE id=?", (customer_id,)).fetchone()
            if cust_check and cust_check["is_closed"]:
                error = f"❌ Account is closed. No transactions allowed for {cust_check['name']}."
            else:
                amount = float(amount)
                if amount <= 0 or amount > 1000000:
                    error = "❌ Invalid amount"
                else:
                    db.execute(
                        "INSERT INTO transactions(customer_id,type,amount,date) VALUES (?,?,?,?)",
                        (customer_id, "deposit", amount, get_now())
                    )
                    db.commit()
                    return redirect("/deposit?success=1")

    return render_template("deposit.html", customers=customers, error=error)

# ---------------- WITHDRAW ----------------
@app.route("/withdraw", methods=["GET", "POST"])
def withdraw():
    if "user" not in session:
        return redirect("/")

    db = get_db()
    customers = db.execute("SELECT * FROM customers").fetchall()
    error = None

    if request.method == "POST":
        customer_id = request.form["customer"]
        amount = float(request.form["amount"])

        if amount <= 0:
            error = "❌ Invalid amount"
        else:
            # Check if account is closed
            cust_check = db.execute("SELECT is_closed, name FROM customers WHERE id=?", (customer_id,)).fetchone()
            if cust_check and cust_check["is_closed"]:
                error = f"❌ Account is closed. No transactions allowed for {cust_check['name']}."
            else:
              deposit_amt = db.execute(
                "SELECT SUM(amount) FROM transactions WHERE customer_id=? AND LOWER(type)='deposit'",
                (customer_id,)
              ).fetchone()[0] or 0

              withdraw_amt = db.execute(
                "SELECT SUM(amount) FROM transactions WHERE customer_id=? AND LOWER(type)='withdraw'",
                (customer_id,)
              ).fetchone()[0] or 0

              balance = deposit_amt - withdraw_amt

              if amount > balance:
                error = f"❌ Insufficient balance! Available: ₹{balance}"
              else:
                db.execute(
                  "INSERT INTO transactions(customer_id,type,amount,date) VALUES (?,?,?,?)",
                  (customer_id, "withdraw", amount, get_now())
                )
                db.commit()
                return redirect("/withdraw?success=1")

    return render_template("withdraw.html", customers=customers, error=error)

# ---------------- LOAN REPAYMENT PAGE ----------------
@app.route('/loan_payment', methods=['GET', 'POST']) # 1. Added methods
def loan_payment():
    if "user" not in session:
        return redirect("/")

    db = get_db()

    # --- 2. HANDLE FORM SUBMISSION (POST) ---
    if request.method == 'POST':
        loan_id = request.form.get('loan_id')
        amount = request.form.get('amount')
        
        if loan_id and amount:
            db.execute("INSERT INTO loan_payments (loan_id, amount, date) VALUES (?, ?, ?)", (loan_id, amount, get_now()))
            db.commit()
            # Redirect with success=1 ONLY after a successful payment
            return {"status": "success"}

    # --- 3. VIEW THE PAGE (GET) ---
    loans = db.execute("""
        SELECT
            loans.id,
            customers.name,
            customers.account_number,
            customers.id as customer_id,
            loans.loan_amount,
            loans.loan_account_number,
            COALESCE(SUM(lp.amount),0) as paid
        FROM loans
        LEFT JOIN loan_payments lp ON loans.id = lp.loan_id
        JOIN customers ON customers.id = loans.customer_id
        GROUP BY loans.id
    """).fetchall()
    
    customers_with_loans = db.execute("""
        SELECT DISTINCT customers.id, customers.name, customers.account_number, customers.mobile
        FROM loans
        JOIN customers ON customers.id = loans.customer_id
        WHERE (
            SELECT COALESCE(SUM(amount), 0) FROM loan_payments WHERE loan_id = loans.id
        ) < loans.loan_amount
        ORDER BY customers.name
    """).fetchall()

    # Calculation logic for balance
    deposits = db.execute("SELECT COALESCE(SUM(amount),0) FROM transactions WHERE LOWER(type)='deposit'").fetchone()[0]
    withdraw = db.execute("SELECT COALESCE(SUM(amount),0) FROM transactions WHERE LOWER(type)='withdraw'").fetchone()[0]
    total_loan = db.execute("""
        SELECT COALESCE(SUM(loans.loan_amount),0) FROM loans
        JOIN customers ON customers.id = loans.customer_id
    """).fetchone()[0]
    loan_paid = db.execute("""
        SELECT COALESCE(SUM(lp.amount),0) FROM loan_payments lp
        JOIN loans ON loans.id = lp.loan_id
        JOIN customers ON customers.id = loans.customer_id
    """).fetchone()[0]

    balance = deposits - withdraw - total_loan + loan_paid
    
    # Return the template normally for viewing
    return render_template(
        "loan_payment.html",
        loans=loans,
        customers_with_loans=customers_with_loans,
        balance=balance,
        total_loan=total_loan,
        loan_paid=loan_paid
    )

# ---------------- ADD PAYMENT ----------------
@app.route('/add_payment', methods=['POST'])
def add_payment():
    if "user" not in session:
        return redirect("/")

    db = get_db()

    loan_id = request.form.get('loan_id')
    amount = request.form.get('amount')

    if not loan_id or not amount:
        return "❌ Invalid input"

    loan_id = int(loan_id)
    payment_amount = float(amount)

    loan = db.execute(
        "SELECT loan_amount FROM loans WHERE id=?",
        (loan_id,)
    ).fetchone()

    if not loan:
        return "❌ Loan not found"

    total_loan_amount = loan[0]

    paid = db.execute(
        "SELECT COALESCE(SUM(amount),0) FROM loan_payments WHERE loan_id=?",
        (loan_id,)
    ).fetchone()[0]

    remaining = total_loan_amount - paid

    if payment_amount > remaining:
        return f"❌ Payment exceeds remaining loan. Remaining: ₹{remaining}"

    if payment_amount <= 0:
        return "❌ Invalid payment amount"

    db.execute(
        "INSERT INTO loan_payments (loan_id, amount, date) VALUES (?,?,?)",
        (loan_id, payment_amount, get_now())
    )
    db.commit()
  
    return redirect("/loan_transactions_report?success=1")

# ---------------- CUSTOMER BALANCE REPORT ----------------
@app.route('/customer_balance_report')
def customer_balance_report():
    if "user" not in session:
        return redirect("/")

    db = get_db()
    customers = db.execute("""
        SELECT
            customers.account_number,
            customers.name,
            customers.mobile,
            COALESCE(SUM(CASE WHEN LOWER(t.type)='deposit' THEN t.amount ELSE 0 END), 0) as total_deposit,
            COALESCE(SUM(CASE WHEN LOWER(t.type)='withdraw' THEN t.amount ELSE 0 END), 0) as total_withdraw,
            COALESCE(SUM(CASE WHEN LOWER(t.type)='service_charge' THEN t.amount ELSE 0 END), 0) as total_service_charge,
            COALESCE(SUM(CASE WHEN LOWER(t.type)='deposit' THEN t.amount ELSE 0 END), 0) -
            COALESCE(SUM(CASE WHEN LOWER(t.type)='withdraw' THEN t.amount ELSE 0 END), 0) -
            COALESCE(SUM(CASE WHEN LOWER(t.type)='service_charge' THEN t.amount ELSE 0 END), 0) as balance
        FROM customers
        LEFT JOIN transactions t ON t.customer_id = customers.id
        GROUP BY customers.id
        HAVING balance > 0
        ORDER BY customers.account_number
    """).fetchall()

    total_balance = sum(c['balance'] for c in customers)

    return render_template("customer_balance_report.html", customers=customers, total_balance=total_balance)

# ---------------- GET CUSTOMER DATA ----------------
@app.route("/get_customer_data/<int:customer_id>")
def get_customer_data(customer_id):
    if "user" not in session:
        return {}

    db = get_db()

    deposit = db.execute(
        "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE customer_id=? AND LOWER(type)='deposit'",
        (customer_id,)
    ).fetchone()[0]

    withdraw = db.execute(
        "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE customer_id=? AND LOWER(type)='withdraw'",
        (customer_id,)
    ).fetchone()[0]

    balance = deposit - withdraw

    loan = db.execute(
        "SELECT id, loan_amount FROM loans WHERE customer_id=? ORDER BY id DESC LIMIT 1",
        (customer_id,)
    ).fetchone()

    loan_data = {"loan_amount": 0, "paid": 0, "remaining": 0}

    if loan:
        paid = db.execute(
            "SELECT COALESCE(SUM(amount),0) FROM loan_payments WHERE loan_id=?",
            (loan["id"],)
        ).fetchone()[0]
        remaining = loan["loan_amount"] - paid
        loan_data = {"loan_amount": loan["loan_amount"], "paid": paid, "remaining": remaining}

    svc_charged = db.execute(
        "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE customer_id=? AND LOWER(type)='service_charge'",
        (customer_id,)
    ).fetchone()[0]
    net_balance = deposit - withdraw - svc_charged

    cust = db.execute("SELECT created FROM customers WHERE id=?", (customer_id,)).fetchone()
    years_active = 0
    if cust and cust["created"]:
        try:
            from datetime import datetime as dt2
            created = dt2.fromisoformat(str(cust["created"]))
            years_active = (get_now() - created).days // 365
        except Exception:
            pass
    already_charged = db.execute(
        "SELECT COUNT(*) FROM annual_service_charges WHERE customer_id=?", (customer_id,)
    ).fetchone()[0]
    pending_svc = max(0, years_active - already_charged) * 100

    return {"balance": net_balance, "loan": loan_data, "pending_service_charge": pending_svc}

# ---------------- NEW LOAN ----------------
@app.route('/new_loan', methods=['GET', 'POST'])
def new_loan():
    if "user" not in session:
        return redirect("/")

    db = get_db()
    customers = db.execute("SELECT * FROM customers").fetchall()

    deposits = db.execute(
        "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE LOWER(type)='deposit'"
    ).fetchone()[0]

    withdraw = db.execute(
        "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE LOWER(type)='withdraw'"
    ).fetchone()[0]

    total_loan_amount = db.execute("""
        SELECT COALESCE(SUM(loans.loan_amount),0) FROM loans
        JOIN customers ON customers.id = loans.customer_id
    """).fetchone()[0]

    loan_paid = db.execute("""
        SELECT COALESCE(SUM(lp.amount),0) FROM loan_payments lp
        JOIN loans ON loans.id = lp.loan_id
        JOIN customers ON customers.id = loans.customer_id
    """).fetchone()[0]

    balance = deposits - withdraw - total_loan_amount + loan_paid
    available_loan = balance

    if request.method == "POST":
        customer_id  = request.form.get("customer")
        amount       = request.form.get("amount")
        guarantor1   = request.form.get("guarantor1") or None
        guarantor2   = request.form.get("guarantor2") or None

        if not customer_id or not amount:
            return "❌ Missing data"

        # Check if account is closed
        cust_status = db.execute("SELECT is_closed, name FROM customers WHERE id=?", (customer_id,)).fetchone()
        if cust_status and cust_status["is_closed"]:
            return redirect(f"/new_loan?error=Account+is+closed.+No+loans+can+be+issued+to+{cust_status['name'].replace(' ', '+')}.")

        amount = float(amount)

        if amount <= 0:
            return "❌ Invalid amount"
        cust_deposits = db.execute(
            "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE customer_id=? AND type='deposit'",
            (customer_id,)
        ).fetchone()[0]
        cust_withdrawals = db.execute(
            "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE customer_id=? AND type='withdraw'",
            (customer_id,)
        ).fetchone()[0]
        cust_service = db.execute(
            "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE customer_id=? AND type='service_charge'",
            (customer_id,)
        ).fetchone()[0]
        cust_balance = cust_deposits - cust_withdrawals - cust_service
        
        if cust_balance < 0:
            return redirect(f"/new_loan?error=Customer+account+balance+is+negative+%28%E2%82%B9{int(cust_balance)}%29.+Cannot+issue+loan.")

        # Check pending service charge
        years_active = 0
        from datetime import datetime as dt2
        cust_created = db.execute("SELECT created FROM customers WHERE id=?", (customer_id,)).fetchone()
        if cust_created and cust_created["created"]:
            try:
                created_dt = dt2.fromisoformat(str(cust_created["created"]))
                #----current --years_active = (dt2.now() - created_dt).days // 365--testing---years_active = (dt2(2034, 4, 27, 12, 0, 0) - created_dt).days // 365
                years_active = (dt2.now() - created_dt).days // 365
            except Exception:
                pass
        already_charged = db.execute(
            "SELECT COUNT(*) FROM annual_service_charges WHERE customer_id=?", (customer_id,)
        ).fetchone()[0]
        pending_svc = max(0, years_active - already_charged) * 100
        if pending_svc > 0:
            return redirect(f"/new_loan?error=Pending+service+charge+of+%E2%82%B9{int(pending_svc)}+is+due.+Please+clear+dues+before+issuing+loan.")

        if amount > balance:
            return f"❌ Not enough bank balance. Available: ₹{balance}"

        last_loan = db.execute("SELECT MAX(id) FROM loans").fetchone()[0] or 0
        loan_acc_no = f"LN{1001 + last_loan}"

        # Add columns if they don't exist yet
        for col in ["loan_account_number TEXT", "guarantor1_id INTEGER", "guarantor2_id INTEGER"]:
            try:
                db.execute(f"ALTER TABLE loans ADD COLUMN {col}")
                db.commit()
            except Exception:
                pass

        tenure = request.form.get("tenure") or None

        # Add tenure column if not exists
        try:
            db.execute("ALTER TABLE loans ADD COLUMN tenure INTEGER")
            db.commit()
        except Exception:
            pass

        db.execute(
            "INSERT INTO loans (customer_id, loan_amount, date, loan_account_number, guarantor1_id, guarantor2_id, tenure) VALUES (?,?,?,?,?,?,?)",
            (customer_id, amount, get_now(), loan_acc_no, guarantor1, guarantor2, tenure)
        )
        db.commit()
        return redirect("/new_loan?success=1")

    last_loan_id = db.execute("SELECT MAX(id) FROM loans").fetchone()[0] or 0

    # Check which customers already have an open loan
    open_loan_customers = db.execute("""
        SELECT customer_id FROM loans
        WHERE (
            SELECT COALESCE(SUM(amount), 0) FROM loan_payments WHERE loan_id = loans.id
        ) < loans.loan_amount
    """).fetchall()
    open_loan_customer_ids = [str(row["customer_id"]) for row in open_loan_customers]

    next_loan_acc = f"LN{1001 + last_loan_id}"

    return render_template(
        "new_loan.html",
        customers=customers,
        available_loan=available_loan,
        balance=balance,
        next_loan_acc=next_loan_acc,
        open_loan_customer_ids=open_loan_customer_ids
    )

# ---------------- GET LOANS BY CUSTOMER ----------------
@app.route('/get_loans_by_customer/<int:customer_id>')
def get_loans_by_customer(customer_id):
    if "user" not in session:
        return {}

    db = get_db()
    loans = db.execute("""
        SELECT
            loans.id,
            loans.loan_account_number,
            loans.loan_amount,
            COALESCE(SUM(lp.amount),0) as paid
        FROM loans
        LEFT JOIN loan_payments lp ON loans.id = lp.loan_id
        WHERE loans.customer_id = ?
        GROUP BY loans.id
        ORDER BY loans.id DESC
    """, (customer_id,)).fetchall()

    result = []
    for l in loans:
        remaining = l["loan_amount"] - l["paid"]
        result.append({
            "id": l["id"],
            "loan_account_number": l["loan_account_number"] or f"LN{1000 + l['id']}",
            "loan_amount": l["loan_amount"],
            "paid": l["paid"],
            "remaining": remaining,
            "fully_paid": remaining <= 0
        })

    return {"loans": result}

# ---------------- GET LOAN DATA ----------------
@app.route('/get_loan_data/<int:loan_id>')
def get_loan_data(loan_id):
    db = get_db()

    # Get loan + customer_id
    loan = db.execute(
        "SELECT loan_amount, customer_id FROM loans WHERE id=?",
        (loan_id,)
    ).fetchone()

    if not loan:
        return {}

    customer_id = loan["customer_id"]

    # ✅ Customer deposits ONLY
    deposits = db.execute(
        "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE customer_id=? AND LOWER(type)='deposit'",
        (customer_id,)
    ).fetchone()[0]

    # ✅ Customer withdrawals ONLY
    withdraw = db.execute(
        "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE customer_id=? AND LOWER(type)='withdraw'",
        (customer_id,)
    ).fetchone()[0]

    # ✅ Customer balance
    balance = deposits - withdraw

    # Loan paid
    paid = db.execute(
        "SELECT COALESCE(SUM(amount),0) FROM loan_payments WHERE loan_id=?",
        (loan_id,)
    ).fetchone()[0]

    remaining = loan["loan_amount"] - paid

    return {
        "balance": balance,
        "loan": {
            "loan_amount": loan["loan_amount"],
            "paid": paid,
            "remaining": remaining
        }
    }

# ---------------- CUSTOMERS ----------------
@app.route('/customers')
def customers():
    if "user" not in session:
        return redirect("/")

    db = get_db()

    # ✅ Latest entry on top
    customers = db.execute(
        "SELECT * FROM customers ORDER BY id DESC"
    ).fetchall()

    return render_template("customers.html", customers=customers)

# ---------------- LOAN REPORT ----------------
@app.route('/loan_report')
def loan_report():
    if "user" not in session:
        return redirect("/")

    db = get_db()
    loans = db.execute("""
        SELECT
            loans.id,
            loans.loan_account_number,
            customers.account_number,
            customers.name,
            customers.mobile,
            loans.loan_amount,
            COALESCE(SUM(lp.amount), 0) as paid,
            loans.loan_amount - COALESCE(SUM(lp.amount), 0) as remaining,
            loans.date
        FROM loans
        JOIN customers ON customers.id = loans.customer_id
        LEFT JOIN loan_payments lp ON lp.loan_id = loans.id
        GROUP BY loans.id
        ORDER BY loans.date DESC
    """).fetchall()

    return render_template("loan_report.html", loans=loans)

# ---------------- DELETE CUSTOMER ----------------
ADMIN_PASSWORD = "admin@123"# for delete entry this password is using 

@app.route('/api/delete-customer', methods=['DELETE'])
def delete_customer_api():
    data = request.get_json()
    customer_id = data.get("id")
    password = data.get("password")

    if "user" not in session:
        return {"success": False, "message": "Unauthorized"}

    if password != ADMIN_PASSWORD:
        return {"success": False, "message": "Incorrect password"}

    db = get_db()

    # Only latest customer allowed
    latest = db.execute(
        "SELECT id FROM customers ORDER BY id DESC LIMIT 1"
    ).fetchone()

    if not latest or customer_id != latest["id"]:
        return {"success": False, "message": "Only latest customer can be deleted"}

    # delete logic...
    db.execute("DELETE FROM customers WHERE id=?", (customer_id,))
    db.commit()

    return {"success": True}

# ---------------- REPORTS ----------------
@app.route('/report')
def report():
    if "user" not in session:
        return redirect("/")
    return render_template("reports.html")

# ---------------- DEPOSIT & WITHDRAWAL REPORT ----------------
@app.route('/deposit_report')
def deposit_report():
    if "user" not in session:
        return redirect("/")

    db = get_db()
    transactions = db.execute("""
        SELECT
            transactions.id,
            customers.account_number,
            customers.name,
            customers.mobile,
            transactions.type,
            transactions.amount,
            transactions.date
        FROM transactions
        JOIN customers ON customers.id = transactions.customer_id
        WHERE LOWER(transactions.type) IN ('deposit', 'withdraw', 'service_charge')
        ORDER BY transactions.date DESC
    """).fetchall()

    total_deposits    = sum(t['amount'] for t in transactions if t['type'].lower() == 'deposit')
    total_withdrawals = sum(t['amount'] for t in transactions if t['type'].lower() == 'withdraw')
    total_service_charges = sum(t['amount'] for t in transactions if t['type'].lower() == 'service_charge')

    remark_map = {
        'deposit': 'Deposit',
        'withdraw': 'Withdrawal',
        'service_charge': 'Yearly Service Charge',
    }
    transactions = [dict(t) | {'remark': remark_map.get(t['type'].lower(), t['type'])} for t in transactions]

    # Find customers with negative balance
    all_customers = db.execute("""
        SELECT customers.id, customers.name, customers.account_number,
            COALESCE(SUM(CASE WHEN type='deposit' THEN amount ELSE 0 END), 0) -
            COALESCE(SUM(CASE WHEN type='withdraw' THEN amount ELSE 0 END), 0) -
            COALESCE(SUM(CASE WHEN type='service_charge' THEN amount ELSE 0 END), 0) as balance
        FROM customers
        LEFT JOIN transactions ON transactions.customer_id = customers.id
        GROUP BY customers.id
        HAVING balance < 0
        ORDER BY balance ASC
    """).fetchall()
    negative_customers = [dict(c) for c in all_customers]

    return render_template("deposit_report.html",
                           transactions=transactions,
                           total_deposits=total_deposits,
                           total_withdrawals=total_withdrawals,
                           total_service_charges=total_service_charges,
                           total_transactions=len(transactions),
                           bank_balance=total_deposits - total_withdrawals - total_service_charges,
                           negative_customers=negative_customers)

# ---------------- LOAN TRANSACTIONS REPORT ----------------
@app.route('/loan_transactions_report')
def loan_transactions_report():
    if "user" not in session:
        return redirect("/")

    db = get_db()

    new_loans = db.execute("""
        SELECT
            loans.loan_account_number,
            customers.name,
            customers.mobile,
            loans.id,
            loans.loan_amount as amount,
            loans.date,
            'new_loan' as txn_type,
            g1.name        as guarantor1_name,
            g1.mobile as guarantor1_mobile,
            g2.name        as guarantor2_name,
            g2.mobile as guarantor2_mobile
        FROM loans
        JOIN customers ON customers.id = loans.customer_id
        LEFT JOIN customers g1 ON g1.id = loans.guarantor1_id
        LEFT JOIN customers g2 ON g2.id = loans.guarantor2_id
    """).fetchall()

    repayments = db.execute("""
        SELECT
            loans.loan_account_number,
            customers.name,
            customers.mobile,
            loan_payments.id,
            loan_payments.amount,
            loan_payments.date,
            'repayment' as txn_type,
            NULL as guarantor1_name,
            NULL as guarantor1_mobile,
            NULL as guarantor2_name,
            NULL as guarantor2_mobile
        FROM loan_payments
        JOIN loans ON loans.id = loan_payments.loan_id
        JOIN customers ON customers.id = loans.customer_id
    """).fetchall()

    transactions = sorted(
        list(new_loans) + list(repayments),
        key=lambda x: x['date'] or '',
        reverse=True
    )

    total_loaned = sum(t['amount'] for t in transactions if t['txn_type'] == 'new_loan')
    total_repaid = sum(t['amount'] for t in transactions if t['txn_type'] == 'repayment')
    outstanding  = total_loaned - total_repaid

    return render_template("loan_transactions_report.html",
                           transactions=transactions,
                           total_loaned=total_loaned,
                           total_repaid=total_repaid,
                           outstanding=outstanding)

# ---------------- CUSTOMER REPORT ----------------
@app.route('/customer_report')
def customer_report():
    if "user" not in session:
        return redirect("/")

    query = request.args.get('q', '').strip()
    customer = None
    error = None
    matches = []
    all_transactions = []
    total_deposits = total_withdrawals = deposit_balance = 0
    total_loaned = total_repaid = loan_outstanding = 0
    total_tenures = tenures_paid = tenures_pending = monthly_due_amount = 0
    total_service_charged = pending_service_charge = pending_dues_years = 0
    latest_loan_amount = 0
    latest_loan_date = ''
    tenures_paid_amount = 0

    if query:
        db = get_db()

        customer = db.execute("""
            SELECT * FROM customers
            WHERE account_number = ? OR mobile = ?
            LIMIT 1
        """, (query, query)).fetchone()

        if not customer:
            matches = db.execute("""
                SELECT * FROM customers
                WHERE LOWER(name) LIKE ?
                   OR account_number LIKE ?
                   OR mobile LIKE ?
                ORDER BY name
            """, (f'%{query.lower()}%', f'%{query}%', f'%{query}%')).fetchall()

            if not matches:
                error = "❌ No customer found. Try account number, mobile or name."
        else:
            cid = customer['id']

            dep_with = db.execute("""
                SELECT transactions.id, type as txn_type, amount, date,
                       account_number as reference,
                       CAST(NULL AS TEXT) as guarantor1_name, CAST(NULL AS TEXT) as guarantor1_mobile,
                       CAST(NULL AS TEXT) as guarantor2_name, CAST(NULL AS TEXT) as guarantor2_mobile,
                       CASE LOWER(type)
                           WHEN 'service_charge' THEN 'Yearly Service Charge'
                           ELSE NULL
                       END as remark,
                       'transactions' as source_table
                FROM transactions
                JOIN customers ON customers.id = transactions.customer_id
                WHERE transactions.customer_id = ?
                  AND LOWER(type) IN ('deposit', 'withdraw', 'service_charge')
                ORDER BY date DESC
            """, (cid,)).fetchall()

            new_loans = db.execute("""
                SELECT loans.id, 'new_loan' as txn_type, loans.loan_amount as amount, loans.date,
                       loans.loan_account_number as reference,
                       g1.name as guarantor1_name, g1.mobile as guarantor1_mobile,
                       g2.name as guarantor2_name, g2.mobile as guarantor2_mobile,
                       NULL as remark,
                       'loans' as source_table
                FROM loans
                LEFT JOIN customers g1 ON g1.id = loans.guarantor1_id
                LEFT JOIN customers g2 ON g2.id = loans.guarantor2_id
                WHERE loans.customer_id = ?
            """, (cid,)).fetchall()

            repayments = db.execute("""
                SELECT loan_payments.id, 'repayment' as txn_type, loan_payments.amount, loan_payments.date,
                       loans.loan_account_number as reference,
                       CAST(NULL AS TEXT) as guarantor1_name, CAST(NULL AS TEXT) as guarantor1_mobile,
                       CAST(NULL AS TEXT) as guarantor2_name, CAST(NULL AS TEXT) as guarantor2_mobile,
                       NULL as remark,
                       'loan_payments' as source_table
                FROM loan_payments
                JOIN loans ON loans.id = loan_payments.loan_id
                WHERE loans.customer_id = ?
            """, (cid,)).fetchall()

            all_transactions = sorted(
                list(dep_with) + list(new_loans) + list(repayments),
                key=lambda x: x['date'] or '',
                reverse=True
            )

            total_deposits = db.execute(
                "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE customer_id=? AND LOWER(type)='deposit'",
                (cid,)
            ).fetchone()[0]

            total_withdrawals = db.execute(
                "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE customer_id=? AND LOWER(type)='withdraw'",
                (cid,)
            ).fetchone()[0]

            total_service_charged = db.execute(
                "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE customer_id=? AND LOWER(type)='service_charge'",
                (cid,)
            ).fetchone()[0]

            deposit_balance = total_deposits - total_withdrawals - total_service_charged

            # Pending service charge (if balance went negative due to service charge)
            years_active = 0
            if customer['created']:
                from datetime import datetime as dt2
                try:
                    created = dt2.fromisoformat(str(customer['created']))
                    #---------------current---years_active = (dt2.now() - created).days // 365-----testing ----years_active = (dt2(2034, 4, 27, 12, 0, 0) - created).days // 365----
                    years_active = (get_now() - created).days // 365
                except Exception:
                    pass

            already_charged = db.execute(
                "SELECT COUNT(*) FROM annual_service_charges WHERE customer_id=?", (cid,)
            ).fetchone()[0]

            pending_dues_years = max(0, years_active - already_charged)
            pending_service_charge = pending_dues_years * 100

            total_loaned = db.execute(
                "SELECT COALESCE(SUM(loan_amount),0) FROM loans WHERE customer_id=?",
                (cid,)
            ).fetchone()[0]

            total_repaid = db.execute("""
                SELECT COALESCE(SUM(lp.amount),0)
                FROM loan_payments lp
                JOIN loans ON loans.id = lp.loan_id
                WHERE loans.customer_id = ?
            """, (cid,)).fetchone()[0]

            loan_outstanding = total_loaned - total_repaid

            # Latest loan
            latest_loan = db.execute(
                "SELECT loan_amount, date FROM loans WHERE customer_id=?"
                " ORDER BY date DESC LIMIT 1", (cid,)
            ).fetchone()
            latest_loan_amount = latest_loan['loan_amount'] if latest_loan else 0
            latest_loan_date = str(latest_loan['date'])[:10] if latest_loan else ''

            # Tenure pending calculation
            from datetime import datetime as dt
            now = get_now()
            tenure_info = db.execute("""
                SELECT loans.id, loans.loan_amount, loans.date, loans.tenure,
                       COALESCE(SUM(lp.amount), 0) as total_paid
                FROM loans
                LEFT JOIN loan_payments lp ON lp.loan_id = loans.id
                WHERE loans.customer_id = ?
                  AND loans.tenure IS NOT NULL AND loans.tenure > 0
                GROUP BY loans.id
                HAVING (loans.loan_amount - total_paid) > 0
            """, (cid,)).fetchall()

            total_tenures = 0
            tenures_paid = 0
            tenures_pending = 0
            monthly_due_amount = 0
            tenures_paid_amount = 0

            for ln in tenure_info:
                tenure = ln['tenure']
                monthly_due = round(ln['loan_amount'] / tenure, 2)
                total_paid = ln['total_paid']
                loan_date = dt.strptime(ln['date'][:19], '%Y-%m-%d %H:%M:%S')

                months_passed = (now.year - loan_date.year) * 12 + (now.month - loan_date.month)
                if now.day > loan_date.day:
                    months_passed += 1
                months_due = min(months_passed, tenure)
                actual_months_passed = months_passed

                paid_tenures = int(total_paid / monthly_due) if monthly_due > 0 else 0
                pending = months_due - paid_tenures

                total_tenures += tenure
                tenures_paid += paid_tenures
                tenures_pending += max(pending, 0)
                monthly_due_amount += monthly_due
                tenures_paid_amount += total_paid

    return render_template("customer_report.html",
                           query=query,
                           customer=customer,
                           matches=matches,
                           error=error,
                           all_transactions=all_transactions,
                           total_deposits=total_deposits,
                           total_withdrawals=total_withdrawals,
                           deposit_balance=deposit_balance,
                           total_loaned=total_loaned,
                           total_repaid=total_repaid,
                           loan_outstanding=loan_outstanding,
                           total_tenures=total_tenures,
                           tenures_paid=tenures_paid,
                           tenures_pending=tenures_pending,
                           monthly_due_amount=monthly_due_amount,
                           total_service_charged=total_service_charged,
                           pending_service_charge=pending_service_charge,
                           pending_dues_years=pending_dues_years,
                           latest_loan_amount=latest_loan_amount,
                           latest_loan_date=latest_loan_date,
                           tenures_paid_amount=tenures_paid_amount)

# ---------------- DELETE CUSTOMER TRANSACTION ----------------
@app.route('/delete_customer_transaction', methods=['POST'])
def delete_customer_transaction():
    if "user" not in session:
        return redirect("/")

    record_id      = request.form.get('record_id')
    source_table   = request.form.get('source_table')
    admin_password = request.form.get('admin_password', '')
    account_number = request.form.get('account_number', '').strip()
    redirect_to    = request.form.get('redirect_to', '').strip()

    # Determine where to redirect back
    if account_number:
        redirect_url = f'/customer_report?q={account_number}'
    elif redirect_to:
        redirect_url = f'/{redirect_to}'
    else:
        redirect_url = '/loan_transactions_report'

    if admin_password != ADMIN_PASSWORD:
        flash('Incorrect password. Delete cancelled.', 'error')
        return redirect(redirect_url)

    allowed_tables = ('transactions', 'loan_payments', 'loans')
    if source_table not in allowed_tables or not record_id:
        flash('Invalid request.', 'error')
        return redirect(redirect_url)

    db = get_db()
    db.execute(f"DELETE FROM {source_table} WHERE id=?", (record_id,))
    db.commit()

    flash('Transaction deleted successfully.', 'success')
    return redirect(redirect_url + ('&' if '?' in redirect_url else '?') + 'deleted=1')

# ---------------- EDIT CUSTOMER ----------------
@app.route('/api/edit-customer', methods=['POST'])
def edit_customer_api():
    if "user" not in session:
        return {"success": False, "message": "Unauthorized"}

    data = request.get_json()
    customer_id = data.get("id")
    name    = data.get("name", "").strip()
    mobile  = data.get("mobile", "").strip()
    address = data.get("address", "").strip()
    password = data.get("password", "")

    if password != ADMIN_PASSWORD:
        return {"success": False, "message": "Incorrect password"}

    if not name or not mobile:
        return {"success": False, "message": "Name and mobile are required"}

    db = get_db()

    # Check mobile not taken by another customer
    existing = db.execute(
        "SELECT id FROM customers WHERE mobile=? AND id!=?", (mobile, customer_id)
    ).fetchone()
    if existing:
        return {"success": False, "message": "Mobile number already used by another customer"}

    db.execute(
        "UPDATE customers SET name=?, mobile=?, address=? WHERE id=?",
        (name, mobile, address, customer_id)
    )
    db.commit()
    return {"success": True}
# ---------------- PENDING DUES REPORT ----------------    
@app.route('/pending_dues')
def pending_dues():
    if "user" not in session:
        return redirect("/")
    db = get_db()
    from datetime import datetime
    now = get_now()
    loans = db.execute("""
        SELECT loans.id, loans.loan_account_number, loans.loan_amount,
               loans.date, loans.tenure,
               customers.name, customers.account_number, customers.mobile,
               COALESCE(SUM(lp.amount), 0) as total_paid
        FROM loans
        JOIN customers ON customers.id = loans.customer_id
        LEFT JOIN loan_payments lp ON lp.loan_id = loans.id
        WHERE loans.tenure IS NOT NULL AND loans.tenure > 0
        AND (customers.is_closed = 0 OR customers.is_closed IS NULL)
        GROUP BY loans.id
        HAVING (loans.loan_amount - total_paid) > 0
    """).fetchall()
    overdue_list = []
    for loan in loans:
        loan_date = datetime.strptime(loan['date'][:19], '%Y-%m-%d %H:%M:%S')
        tenure = loan['tenure']
        monthly_due = round(loan['loan_amount'] / tenure, 2)
        total_paid = loan['total_paid']
        months_passed = (now.year - loan_date.year) * 12 + (now.month - loan_date.month)
        if now.day > loan_date.day:
            months_passed += 1
        months_due = min(months_passed, tenure)
        expected_paid = round(monthly_due * months_due, 2)
        overdue_amount = round(expected_paid - total_paid, 2)
        months_paid = round(total_paid / monthly_due) if monthly_due > 0 else 0
        overdue_months = months_due - months_paid
        if overdue_amount > 0:
            overdue_list.append({
                'loan_account_number': loan['loan_account_number'],
                'name': loan['name'],
                'account_number': loan['account_number'],
                'mobile': loan['mobile'],
                'loan_amount': loan['loan_amount'],
                'tenure': tenure,
                'monthly_due': monthly_due,
                'total_paid': total_paid,
                'expected_paid': expected_paid,
                'overdue_amount': overdue_amount,
                'months_due': months_due,
                'pending_tenure': max(0, tenure - months_due) if months_due < tenure else 'Overdue',
                'overdue_months': overdue_months,
            })
    return render_template("pending_dues.html", overdue_list=overdue_list)
# ---------------- SERVICE CHARGE REPORT ----------------
@app.route('/service_report')
def service_report():
    if "user" not in session:
        return redirect("/")

    db = get_db()

    # Collections from account opening
    collections = db.execute("""
        SELECT osc.id, c.account_number, c.name, osc.amount, osc.date,
               'Account Opening' as source
        FROM opening_service_charges osc
        JOIN customers c ON c.id = osc.customer_id
        ORDER BY osc.date DESC
    """).fetchall()

    # Loan service charges
    loan_charges = db.execute("""
        SELECT loans.id, c.account_number, c.name,
               loans.service_charge, loans.date,
               'Loan Service' as source
        FROM loans
        JOIN customers c ON c.id = loans.customer_id
        WHERE loans.service_charge IS NOT NULL AND loans.service_charge > 0
        ORDER BY loans.date DESC
    """).fetchall()

    # Expenses
    expenses = db.execute("""
        SELECT * FROM service_expense ORDER BY date DESC
    """).fetchall()

    annual_charges = db.execute("""
        SELECT t.id, c.account_number, c.name, t.amount, t.date
        FROM transactions t
        JOIN customers c ON c.id = t.customer_id
        WHERE t.type = 'service_charge'
        ORDER BY t.date DESC
    """).fetchall()

    total_collected = (
        sum(r['amount'] for r in collections) +
        sum(r['service_charge'] for r in loan_charges) +
        sum(r['amount'] for r in annual_charges)
    )
    total_spent = sum(e['amount'] for e in expenses)
    service_balance = total_collected - total_spent

    return render_template(
        "service_report.html",
        collections=collections,
        loan_charges=loan_charges,
        annual_charges=annual_charges,
        expenses=expenses,
        total_collected=total_collected,
        total_spent=total_spent,
        service_balance=service_balance
    )

    
# ---------------- UPDATE EXPENSE REMARK ----------------
@app.route('/update_expense_remark', methods=['POST'])
def update_expense_remark():
    if "user" not in session:
        return redirect("/")

    expense_id = request.form.get('expense_id')
    remark = request.form.get('remark', '').strip()

    if not expense_id:
        flash('Invalid request.', 'error')
        return redirect('/service_report')

    db = get_db()
    db.execute(
        "UPDATE service_expense SET remark=? WHERE id=?",
        (remark, expense_id)
    )
    db.commit()

    flash('Remark updated successfully.', 'success')
    return redirect('/service_report')
# ---------------- DELETE SERVICE EXPENSE ----------------
@app.route('/delete_service_expense', methods=['POST'])
def delete_service_expense():
    if "user" not in session:
        return redirect("/")

    expense_id = request.form.get('record_id')
    if not expense_id:
        flash('Invalid request.', 'error')
        return redirect('/service_report')

    db = get_db()
    db.execute("DELETE FROM service_expense WHERE id=?", (expense_id,))
    db.commit()

    flash('Expense deleted successfully.', 'success')
    return redirect('/service_report')

# ---------------- DELETE OPENING SERVICE CHARGE ----------------
@app.route('/delete_opening_charge', methods=['POST'])
def delete_opening_charge():
    if "user" not in session:
        return redirect("/")

    record_id = request.form.get('record_id')
    if not record_id:
        flash('Invalid request.', 'error')
        return redirect('/service_report')

    db = get_db()
    db.execute("DELETE FROM opening_service_charges WHERE id=?", (record_id,))
    db.commit()

    flash('Account opening charge deleted successfully.', 'success')
    return redirect('/service_report')
# ---------------- ADD SERVICE EXPENSE ----------------
@app.route('/add_service_expense', methods=['GET', 'POST'])
def add_service_expense():
    if "user" not in session:
        return redirect("/")

    db = get_db()

    # Ensure description and remark columns exist
    try:
        db.execute("ALTER TABLE service_expense ADD COLUMN description TEXT")
        db.commit()
    except Exception:
        pass
    try:
        db.execute("ALTER TABLE service_expense ADD COLUMN remark TEXT")
        db.commit()
    except Exception:
        pass

    # Fetch live balance figures for the card
    loan_service = db.execute(
        "SELECT COALESCE(SUM(service_charge),0) FROM loans"
    ).fetchone()[0]
    opening_service = db.execute(
        "SELECT COALESCE(SUM(amount),0) FROM opening_service_charges"
    ).fetchone()[0]
    annual_service = db.execute(
        "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE type='service_charge'"
    ).fetchone()[0]
    service_collected = loan_service + opening_service + annual_service
    service_spent = db.execute(
        "SELECT COALESCE(SUM(amount),0) FROM service_expense"
    ).fetchone()[0]
    service_balance = service_collected - service_spent

    if request.method == "POST":
        description = request.form.get("description", "").strip()
        remark      = description  # remark field removed from form; reuse description
        amount      = request.form.get("amount", "0").strip()

        try:
            amount = float(amount)
        except ValueError:
            amount = 0.0

        if description and amount > 0:
            db.execute(
                "INSERT INTO service_expense (description, amount, remark, date) VALUES (?,?,?,?)",
                (description, amount, remark, get_now())
            )
            db.commit()
            from urllib.parse import quote
            return redirect(
                f"/add_service_expense?success=1"
                f"&amt={amount}"
                f"&desc={quote(description)}"
                f"&rmk={quote(remark)}"
            )

    return render_template(
        "add_service_expense.html",
        service_collected=service_collected,
        service_spent=service_spent,
        service_balance=service_balance
    )
# ---------------- ANNUAL SERVICE CHARGE ----------------
def apply_annual_service_charges():
    """Auto-deduct ₹100 service charge on each account anniversary."""
    db = get_db()

    # Ensure tracking table exists
    db.execute("""
        CREATE TABLE IF NOT EXISTS annual_service_charges (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER,
            amount      REAL,
            date        TEXT,
            year        INTEGER
        )
    """)
    db.commit()

    customers = db.execute("SELECT id, created FROM customers WHERE is_closed=0 OR is_closed IS NULL").fetchall()
    # ---------------- after on year auto move balance to service charge test (current =====now = get_now()====tonow = datetime(2027, 4, 27, 12, 0, 0)) ---MSQ TEST DT
    now = get_now()

    for c in customers:
        if not c["created"]:
            continue
        try:
            created = datetime.fromisoformat(str(c["created"]))
        except Exception:
            continue

        years_active = (now - created).days // 365
        if years_active < 1:
            continue

        for year in range(1, years_active + 1):
            already = db.execute(
                "SELECT id FROM annual_service_charges WHERE customer_id=? AND year=?",
                (c["id"], year)
            ).fetchone()
            if already:
                continue

            # Check current balance before charging
            cust_dep = db.execute(
                "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE customer_id=? AND type='deposit'",
                (c["id"],)
            ).fetchone()[0]
            cust_with = db.execute(
                "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE customer_id=? AND type='withdraw'",
                (c["id"],)
            ).fetchone()[0]
            cust_svc = db.execute(
                "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE customer_id=? AND type='service_charge'",
                (c["id"],)
            ).fetchone()[0]
            cust_balance = cust_dep - cust_with - cust_svc

            if cust_balance < 100:
                # Not enough balance — skip, will show as pending
                continue

            charge_date = created.replace(year=created.year + year)
            db.execute(
                "INSERT INTO transactions(customer_id,type,amount,date) VALUES (?,?,?,?)",
                (c["id"], "service_charge", 100.0, charge_date)
            )
            db.execute(
                "INSERT INTO annual_service_charges(customer_id,amount,date,year) VALUES (?,?,?,?)",
                (c["id"], 100.0, charge_date, year)
            )
        db.commit()
        
# ---------------- service_charge_due_report ----------------        
@app.route('/service_charge_due_report')
def service_charge_due_report():
    if "user" not in session:
        return redirect("/")

    db = get_db()
    from datetime import datetime as dt
    customers = db.execute("SELECT id, name, account_number, mobile, created FROM customers WHERE is_closed=0 OR is_closed IS NULL").fetchall()

    due_list = []
    for c in customers:
        if not c["created"]:
            continue
        try:
            created_dt = dt.fromisoformat(str(c["created"]))
            #------actual---years_active = (get_now() - created_dt).days // 365 --tes--years_active = (dt(2034, 4, 27, 12, 0, 0) - created_dt).days // 365--msq DT test
            years_active = (get_now() - created_dt).days // 365
        except Exception:
            continue

        already_charged = db.execute(
            "SELECT COUNT(*) FROM annual_service_charges WHERE customer_id=?", (c["id"],)
        ).fetchone()[0]

        years_due = years_active - already_charged
        if years_due <= 0:
            continue

        due_list.append({
            "name": c["name"],
            "account_number": c["account_number"],
            "mobile": c["mobile"],
            "member_since": str(c["created"])[:10],
            "years_due": years_due,
            "due_amount": years_due * 100
        })

    due_list.sort(key=lambda x: x["due_amount"], reverse=True)
    return render_template("service_charge_due_report.html", due_list=due_list)        
        

# ---------------- CLOSE ACCOUNT ----------------
@app.route('/close_account/<int:customer_id>', methods=['GET'])
def close_account_page(customer_id):
    if "user" not in session:
        return redirect("/")

    db = get_db()
    customer = db.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
    if not customer:
        return redirect("/customers")

    if customer["is_closed"]:
        return redirect(f"/customer_report?q={customer['account_number']}")

    # Calculate current balance
    deposits = db.execute(
        "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE customer_id=? AND type='deposit'",
        (customer_id,)
    ).fetchone()[0]
    withdrawals = db.execute(
        "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE customer_id=? AND type='withdraw'",
        (customer_id,)
    ).fetchone()[0]
    svc_charged = db.execute(
        "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE customer_id=? AND type='service_charge'",
        (customer_id,)
    ).fetchone()[0]
    balance = deposits - withdrawals - svc_charged

    # Pending service charge
    years_active = 0
    if customer["created"]:
        from datetime import datetime as dt2
        try:
            created = dt2.fromisoformat(str(customer["created"]))
            years_active = (get_now() - created).days // 365
        except Exception:
            pass
    already_charged = db.execute(
        "SELECT COUNT(*) FROM annual_service_charges WHERE customer_id=?", (customer_id,)
    ).fetchone()[0]
    pending_svc = max(0, years_active - already_charged) * 100

    # Outstanding loan
    total_loaned = db.execute(
        "SELECT COALESCE(SUM(loan_amount),0) FROM loans WHERE customer_id=?", (customer_id,)
    ).fetchone()[0]
    total_repaid = db.execute("""
        SELECT COALESCE(SUM(lp.amount),0) FROM loan_payments lp
        JOIN loans ON loans.id = lp.loan_id WHERE loans.customer_id=?
    """, (customer_id,)).fetchone()[0]
    loan_outstanding = total_loaned - total_repaid

    # Settlement = balance after clearing all dues
    settlement = balance - pending_svc - loan_outstanding

    return render_template("close_account.html",
        customer=customer,
        balance=balance,
        pending_svc=pending_svc,
        loan_outstanding=loan_outstanding,
        settlement=settlement
    )


@app.route('/close_account_confirm', methods=['POST'])
def close_account_confirm():
    if "user" not in session:
        return redirect("/")

    customer_id = request.form.get("customer_id")
    admin_password = request.form.get("admin_password", "")
    closure_note = request.form.get("closure_note", "").strip()

    if admin_password != ADMIN_PASSWORD:
        return redirect(f"/close_account/{customer_id}?error=wrong_password")

    db = get_db()
    customer = db.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
    if not customer or customer["is_closed"]:
        return redirect("/customers")

    # Recalculate settlement at time of closure
    deposits = db.execute(
        "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE customer_id=? AND type='deposit'",
        (customer_id,)
    ).fetchone()[0]
    withdrawals = db.execute(
        "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE customer_id=? AND type='withdraw'",
        (customer_id,)
    ).fetchone()[0]
    svc_charged = db.execute(
        "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE customer_id=? AND type='service_charge'",
        (customer_id,)
    ).fetchone()[0]
    balance = deposits - withdrawals - svc_charged

    years_active = 0
    if customer["created"]:
        from datetime import datetime as dt2
        try:
            created = dt2.fromisoformat(str(customer["created"]))
            years_active = (get_now() - created).days // 365
        except Exception:
            pass
    already_charged = db.execute(
        "SELECT COUNT(*) FROM annual_service_charges WHERE customer_id=?", (customer_id,)
    ).fetchone()[0]
    pending_svc = max(0, years_active - already_charged) * 100

    total_loaned = db.execute(
        "SELECT COALESCE(SUM(loan_amount),0) FROM loans WHERE customer_id=?", (customer_id,)
    ).fetchone()[0]
    total_repaid = db.execute("""
        SELECT COALESCE(SUM(lp.amount),0) FROM loan_payments lp
        JOIN loans ON loans.id = lp.loan_id WHERE loans.customer_id=?
    """, (customer_id,)).fetchone()[0]
    loan_outstanding = total_loaned - total_repaid

    settlement = balance - pending_svc - loan_outstanding

    now = get_now()

    # Mark account as closed
    db.execute("""
        UPDATE customers
        SET is_closed=1, closed_date=?, settlement_amount=?, closure_note=?
        WHERE id=?
    """, (now, settlement, closure_note, customer_id))

    # 1. Deduct any pending service charge as a transaction
    if pending_svc > 0:
        db.execute(
            "INSERT INTO transactions(customer_id,type,amount,date) VALUES (?,?,?,?)",
            (customer_id, "service_charge", pending_svc, now)
        )
        db.execute(
            "INSERT INTO annual_service_charges(customer_id,amount,date,year) VALUES (?,?,?,?)",
            (customer_id, pending_svc, now, already_charged + 1)
        )

    # 2. Mark all outstanding loans as fully paid
    outstanding_loans = db.execute(
        "SELECT id, loan_amount FROM loans WHERE customer_id=?", (customer_id,)
    ).fetchall()
    for ln in outstanding_loans:
        already_paid = db.execute(
            "SELECT COALESCE(SUM(amount),0) FROM loan_payments WHERE loan_id=?", (ln["id"],)
        ).fetchone()[0]
        remaining = ln["loan_amount"] - already_paid
        if remaining > 0:
            db.execute(
                "INSERT INTO loan_payments(loan_id, amount, date) VALUES (?,?,?)",
                (ln["id"], remaining, now)
            )
            if balance >= remaining:
                # Loan repaid from existing deposit balance — record as withdraw only
                db.execute(
                    "INSERT INTO transactions(customer_id,type,amount,date) VALUES (?,?,?,?)",
                    (customer_id, "withdraw", remaining, now)
                )
            else:
                # Customer brought cash to repay loan — record deposit in, then withdraw out
                db.execute(
                    "INSERT INTO transactions(customer_id,type,amount,date) VALUES (?,?,?,?)",
                    (customer_id, "deposit", remaining, now)
                )
                db.execute(
                    "INSERT INTO transactions(customer_id,type,amount,date) VALUES (?,?,?,?)",
                    (customer_id, "withdraw", remaining, now)
                )

    # 3. Record settlement withdrawal for remaining deposit balance
    if settlement > 0:
        db.execute(
            "INSERT INTO transactions(customer_id,type,amount,date) VALUES (?,?,?,?)",
            (customer_id, "withdraw", settlement, now)
        ) 

    db.commit()

    return redirect(f"/customer_report?q={customer['account_number']}&closed=1")


# ---------------- RUN ----------------
if __name__ == "__main__":
    app.run(debug=True)
