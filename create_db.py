import os, sqlite3
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bank.db")
DB_PATH = r"C:\Users\MSQ-LAP\Desktop\project-BNK\bank.db"
import sqlite3
conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

# ---------------- USERS ----------------
c.execute("""
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE,
    password TEXT
)
""")

# ---------------- CUSTOMERS ----------------
c.execute("""
CREATE TABLE IF NOT EXISTS customers(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_number TEXT UNIQUE,
    name TEXT,
    mobile TEXT UNIQUE,
    address TEXT,
    created TEXT
)
""")

# ---------------- TRANSACTIONS ----------------
# Added 'service_charge' to allowed types
c.execute("""
CREATE TABLE IF NOT EXISTS transactions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER,
    type TEXT CHECK(type IN ('deposit','withdraw','service_charge')),
    amount REAL,
    date TEXT,
    FOREIGN KEY(customer_id) REFERENCES customers(id)
)
""")

# ---------------- LOANS ----------------
# Added tenure, guarantor1_id, guarantor2_id columns
c.execute("""
CREATE TABLE IF NOT EXISTS loans(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER,
    loan_account_number TEXT UNIQUE,
    loan_amount REAL,
    interest REAL,
    months INTEGER,
    service_charge REAL,
    created TEXT,
    date TEXT,
    tenure INTEGER,
    guarantor1_id INTEGER,
    guarantor2_id INTEGER,
    FOREIGN KEY(customer_id) REFERENCES customers(id)
)
""")

# ---------------- LOAN PAYMENTS ----------------
c.execute("""
CREATE TABLE IF NOT EXISTS loan_payments(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    loan_id INTEGER,
    amount REAL,
    date TEXT,
    FOREIGN KEY(loan_id) REFERENCES loans(id)
)
""")

# ---------------- SERVICE EXPENSE ----------------
# Added description and remark columns
c.execute("""
CREATE TABLE IF NOT EXISTS service_expense(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    amount REAL,
    note TEXT,
    description TEXT,
    remark TEXT,
    date TEXT
)
""")

# ---------------- OPENING SERVICE CHARGES ----------------
c.execute("""
CREATE TABLE IF NOT EXISTS opening_service_charges(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER,
    amount REAL,
    date TEXT
)
""")

# ---------------- ANNUAL SERVICE CHARGES ----------------
c.execute("""
CREATE TABLE IF NOT EXISTS annual_service_charges(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER,
    amount REAL,
    date TEXT,
    year INTEGER
)
""")

# ---------------- INDEXES ----------------
c.execute("CREATE INDEX IF NOT EXISTS idx_customer_id ON transactions(customer_id)")
c.execute("CREATE INDEX IF NOT EXISTS idx_loan_customer ON loans(customer_id)")

# ---------------- DEFAULT USERS ----------------
c.execute("DELETE FROM users")
c.execute("INSERT INTO users(username,password) VALUES('admin','admin123')")
c.execute("INSERT INTO users(username,password) VALUES('staff','staff123')")

conn.commit()
conn.close()
print("✅ Full database created successfully (Bank + Loan System)")