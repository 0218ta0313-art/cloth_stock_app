from werkzeug.security import generate_password_hash

# admin 用
password_admin = "testpass"
print("admin のハッシュ：")
print(generate_password_hash(password_admin))

print("------")

# staff1 用
password_staff = "staffpass"   # ← staff のパスワードを好きに設定
print("staff1 のハッシュ：")
print(generate_password_hash(password_staff))
