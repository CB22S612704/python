import pandas as pd
import pyotp
import time

EXCEL_FILE = r"C:\Users\mages\OneDrive\Desktop\Automation\AR Untouched.xlsx"

# Second sheet: login
df = pd.read_excel(
    EXCEL_FILE,
    sheet_name="Login",
    dtype=str
).fillna("")

for index, row in df.iterrows():
    username = row["Username"].strip()
    password = row["Password"].strip()
    security_key = row["Security Key"].strip().replace(" ", "").upper()

    if not username or not security_key:
        print(f"Row {index + 2}: Username or Security Key missing")
        continue

    try:
        totp = pyotp.TOTP(security_key)
        otp = totp.now()
        remaining = 30 - (int(time.time()) % 30)

        print(f"\nUsername : {username}")
        print(f"OTP      : {otp}")
        print(f"Valid for: {remaining} seconds")

        # Selenium OTP field:
        # otp_box.send_keys(otp)

    except Exception as e:
        print(f"Row {index + 2}: Invalid Security Key - {e}")