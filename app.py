from flask import Flask, render_template, request, redirect, url_for, flash, session
import sqlite3
from datetime import datetime
from functools import wraps
from werkzeug.security import check_password_hash, generate_password_hash


# ==== 設定 ====
DB_NAME = "cloth_stock.db"   # DBファイル名

# 在庫アラートのしきい値（この数以下なら要注意表示）
LOW_STOCK_THRESHOLD = 5

app = Flask(__name__)
app.secret_key = "change_this_secret_key"  # 適当な長めの文字列でOK


# ==== DB接続用ヘルパー ====
def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row  # 行を dict 風に扱えるようにする
    return conn

def ensure_users_table():
    """USERS テーブルと admin ユーザーを保証する"""
    conn = get_db_connection()

    # USERS テーブルが無ければ作る
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS USERS (
            user_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at    TEXT NOT NULL,
            role          TEXT
        );
        """
    )

    # admin ユーザーが 1 件もなければ作る（パスワード: testpass）
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM USERS WHERE username = 'admin'"
    ).fetchone()

    if row["cnt"] == 0:
        conn.execute(
            """
            INSERT INTO USERS (username, password_hash, created_at, role)
            VALUES (?, ?, datetime('now','localtime'), ?)
            """,
            ("admin", generate_password_hash("testpass"), "admin"),
        )

    conn.commit()
    conn.close()


# ==== ログイン必須デコレーター ====
def login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            flash("ログインしてください。", "error")
            from flask import request
            return redirect(url_for("login", next=request.path))
        return view_func(*args, **kwargs)
    return wrapped


# ==== ログイン ====
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        error = None

        # 入力チェック
        if not username or not password:
            error = "ユーザー名とパスワードを入力してください。"
        else:
            # DB からユーザー情報取得（role も含める）
            conn = get_db_connection()
            user = conn.execute(
                "SELECT user_id, username, password_hash, role FROM USERS WHERE username = ?",
                (username,),
            ).fetchone()
            conn.close()

            # ユーザーが存在しない or パスワード不一致
            if user is None or not check_password_hash(user["password_hash"], password):
                error = "ユーザー名またはパスワードが違います。"

        if error:
            flash(error, "error")
        else:
            # ログイン成功
            session.clear()
            session["user_id"] = user["user_id"]
            session["username"] = user["username"]
            session["role"] = user["role"] or "staff"   # 念のためデフォルトstaff
            flash("ログインしました。", "success")

            next_url = request.args.get("next")
            return redirect(next_url or url_for("item_list"))

    # GET のときはログイン画面を表示
    return render_template("login.html")


def admin_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        # まだログインしてない
        if not session.get("user_id"):
            flash("ログインしてください。", "error")
            return redirect(url_for("login", next=request.path))

        # role が admin 以外
        if session.get("role") != "admin":
            flash("この操作を行う権限がありません。", "error")
            return redirect(url_for("item_list"))

        # OK のときだけ本来の処理へ
        return view_func(*args, **kwargs)
    return wrapped


# ==== ログアウト ====
@app.route("/logout")
def logout():
    session.clear()
    flash("ログアウトしました。", "success")
    return redirect(url_for("login"))


# ==== トップページ：とりあえず商品一覧へリダイレクト ====
@app.route("/")
@login_required
def index():
    return redirect(url_for("item_list"))


# ==== 商品一覧 ====
@app.route("/items")
@login_required
def item_list():
    conn = get_db_connection()

    # フィルタ用カテゴリ一覧（プルダウン用）
    categories = conn.execute(
        "SELECT category_id, name FROM CATEGORIES ORDER BY name"
    ).fetchall()

    # クエリパラメータから category_id を取得（例: /items?category_id=1）
    selected_category_id = request.args.get("category_id", type=int)

    params = []
    where_clause = ""
    if selected_category_id:
        where_clause = "WHERE i.category_id = ?"
        params.append(selected_category_id)

    # 在庫数を集計しながら商品一覧を取得
    items = conn.execute(
        f"""
        SELECT
            i.item_id,
            i.name,
            i.sku,
            c.name AS category_name,
            i.base_price,
            i.size,
            i.color,
            i.material,
            i.is_active,
            COALESCE(
                SUM(
                    CASE
                        WHEN m.movement_type = 'IN' THEN m.quantity
                        WHEN m.movement_type = 'OUT' THEN -m.quantity
                        WHEN m.movement_type = 'ADJUST' THEN m.quantity
                        ELSE 0
                    END
                ),
                0
            ) AS stock_quantity
        FROM ITEMS i
        LEFT JOIN CATEGORIES c
            ON i.category_id = c.category_id
        LEFT JOIN STOCK_MOVEMENTS m
            ON m.item_id = i.item_id
        {where_clause}
        GROUP BY
            i.item_id, i.name, i.sku, i.base_price,
            i.size, i.color, i.material, i.is_active,
            c.name
        ORDER BY i.item_id DESC
        """,
        params,
    ).fetchall()

    conn.close()
    
    return render_template(
        "item_list.html",
        items=items,
        categories=categories,
        selected_category_id=selected_category_id,
        low_stock_threshold=LOW_STOCK_THRESHOLD,
    )


# ==== 商品登録（GET:フォーム表示 / POST:登録処理） ====
@app.route("/items/new", methods=["GET", "POST"])
@login_required
def add_item():
    conn = get_db_connection()

    # プルダウン用にカテゴリ一覧を取得
    categories = conn.execute(
        "SELECT category_id, name FROM CATEGORIES ORDER BY name"
    ).fetchall()

    if request.method == "POST":
        # フォームから取得
        name = request.form.get("name", "").strip()
        sku = request.form.get("sku", "").strip()
        category_id = request.form.get("category_id") or None
        base_price = request.form.get("base_price") or None
        size = request.form.get("size", "").strip() or None
        color = request.form.get("color", "").strip() or None
        material = request.form.get("material", "").strip() or None
        note = request.form.get("note", "").strip() or None
        is_active = 1 if request.form.get("is_active") == "1" else 0

        # 簡単なバリデーション
        errors = []
        if not name:
            errors.append("商品名は必須です。")

        base_price_int = None
        if base_price:
            try:
                base_price_int = int(base_price)
            except ValueError:
                errors.append("標準価格は数字で入力してください。")

        category_id_int = None
        if category_id:
            try:
                category_id_int = int(category_id)
            except ValueError:
                errors.append("カテゴリIDが不正です。")

        if errors:
            for e in errors:
                flash(e, "error")
            conn.close()
            # 入力内容を維持するため form=request.form を渡す
            return render_template(
                "add_item.html",
                categories=categories,
                form=request.form,
            )

        # 日付（created_at / updated_at）を現在時刻で設定
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        conn.execute(
            """
            INSERT INTO ITEMS
                (name, sku, category_id, base_price, size, color, material, note,
                 created_at, updated_at, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                sku,
                category_id_int,
                base_price_int,
                size,
                color,
                material,
                note,
                now,
                now,
                is_active,
            ),
        )
        conn.commit()
        conn.close()

        flash("商品を登録しました。", "success")
        return redirect(url_for("item_list"))

    # GETのときは空フォーム
    conn.close()
    return render_template(
        "add_item.html",
        categories=categories,
        form={},
    )


# ==== 商品編集 ====
@app.route("/items/<int:item_id>/edit", methods=["GET", "POST"])
@login_required
def edit_item(item_id):
    conn = get_db_connection()

    # 対象商品の取得
    item = conn.execute(
        "SELECT * FROM ITEMS WHERE item_id = ?",
        (item_id,),
    ).fetchone()

    if item is None:
        conn.close()
        flash("指定された商品が見つかりません。", "error")
        return redirect(url_for("item_list"))

    # カテゴリ一覧（プルダウン用）
    categories = conn.execute(
        "SELECT category_id, name FROM CATEGORIES ORDER BY name"
    ).fetchall()

    if request.method == "POST":
        # フォームから取得
        name = request.form.get("name", "").strip()
        sku = request.form.get("sku", "").strip()
        category_id = request.form.get("category_id") or None
        base_price = request.form.get("base_price") or None
        size = request.form.get("size", "").strip() or None
        color = request.form.get("color", "").strip() or None
        material = request.form.get("material", "").strip() or None
        note = request.form.get("note", "").strip() or None
        is_active = 1 if request.form.get("is_active") == "1" else 0

        errors = []
        if not name:
            errors.append("商品名は必須です。")

        base_price_int = None
        if base_price:
            try:
                base_price_int = int(base_price)
            except ValueError:
                errors.append("標準価格は数字で入力してください。")

        category_id_int = None
        if category_id:
            try:
                category_id_int = int(category_id)
            except ValueError:
                errors.append("カテゴリIDが不正です。")

        if errors:
            for e in errors:
                flash(e, "error")
            conn.close()
            # 入力内容を維持して再表示
            return render_template(
                "edit_item.html",
                categories=categories,
                item_id=item_id,
                form=request.form,
            )

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        conn.execute(
            """
            UPDATE ITEMS
            SET
                name = ?,
                sku = ?,
                category_id = ?,
                base_price = ?,
                size = ?,
                color = ?,
                material = ?,
                note = ?,
                updated_at = ?,
                is_active = ?
            WHERE item_id = ?
            """,
            (
                name,
                sku,
                category_id_int,
                base_price_int,
                size,
                color,
                material,
                note,
                now,
                is_active,
                item_id,
            ),
        )
        conn.commit()
        conn.close()

        flash("商品を更新しました。", "success")
        return redirect(url_for("item_list"))

    # GET のとき：現在値をフォーム用の dict に詰める
    form_data = {
        "name": item["name"],
        "sku": item["sku"] or "",
        "category_id": str(item["category_id"]) if item["category_id"] is not None else "",
        "base_price": item["base_price"] if item["base_price"] is not None else "",
        "size": item["size"] or "",
        "color": item["color"] or "",
        "material": item["material"] or "",
        "note": item["note"] or "",
        "is_active": "1" if item["is_active"] == 1 else "0",
    }

    conn.close()
    return render_template(
        "edit_item.html",
        categories=categories,
        item_id=item_id,
        form=form_data,
    )

# ==== 商品ごとの在庫履歴 ====
@app.route("/items/<int:item_id>/history")
@login_required
def item_history(item_id):
    conn = get_db_connection()

    # 商品情報
    item = conn.execute(
        """
        SELECT
            i.item_id,
            i.name,
            c.name AS category_name
        FROM ITEMS i
        LEFT JOIN CATEGORIES c ON i.category_id = c.category_id
        WHERE i.item_id = ?
        """,
        (item_id,),
    ).fetchone()

    if item is None:
        conn.close()
        flash("指定された商品が見つかりません。", "error")
        return redirect(url_for("item_list"))

    # 在庫移動を取得（古い順）
    movements = conn.execute(
        """
        SELECT
            m.movement_id,
            m.movement_type,
            m.quantity,
            m.memo,
            m.created_at,
            s.name AS supplier_name
        FROM STOCK_MOVEMENTS m
        LEFT JOIN SUPPLIERS s ON m.supplier_id = s.supplier_id
        WHERE m.item_id = ?
        ORDER BY m.created_at ASC, m.movement_id ASC
        """,
        (item_id,),
    ).fetchall()
    conn.close()

    # Python側で在庫推移（残高）を計算
    history = []
    stock = 0
    for m in movements:
        if m["movement_type"] == "IN":
            delta = m["quantity"]
        elif m["movement_type"] == "OUT":
            delta = -m["quantity"]
        elif m["movement_type"] == "ADJUST":
            # とりあえず「調整もプラスマイナス扱い」で
            delta = m["quantity"]
        else:
            delta = 0

        stock += delta

        history.append({
            "movement_id": m["movement_id"],
            "created_at": m["created_at"],
            "movement_type": m["movement_type"],
            "quantity": m["quantity"],
            "delta": delta,
            "stock_after": stock,
            "memo": m["memo"],
            "supplier_name": m["supplier_name"],
        })

    return render_template(
        "item_history.html",
        item=item,
        history=history,
    )


# ==== 商品削除 ====
@app.route("/items/<int:item_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_item(item_id):
    conn = get_db_connection()

    # 在庫移動で使用されているかチェック
    count_row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM STOCK_MOVEMENTS WHERE item_id = ?",
        (item_id,),
    ).fetchone()

    if count_row["cnt"] > 0:
        conn.close()
        flash("この商品は在庫移動の履歴があるため、削除できません。", "error")
        return redirect(url_for("item_list"))

    conn.execute(
        "DELETE FROM ITEMS WHERE item_id = ?",
        (item_id,),
    )
    conn.commit()
    conn.close()

    flash("商品を削除しました。", "success")
    return redirect(url_for("item_list"))


# ==== 仕入先一覧 ====
@app.route("/suppliers")
@login_required
def supplier_list():
    conn = get_db_connection()
    suppliers = conn.execute(
        """
        SELECT
            supplier_id,
            name,
            phone,
            email,
            address,
            note,
            created_at
        FROM SUPPLIERS
        ORDER BY supplier_id DESC
        """
    ).fetchall()
    conn.close()
    return render_template("supplier_list.html", suppliers=suppliers)


# ==== 仕入先登録 ====
@app.route("/suppliers/new", methods=["GET", "POST"])
@login_required
def add_supplier():
    conn = get_db_connection()

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip() or None
        email = request.form.get("email", "").strip() or None
        address = request.form.get("address", "").strip() or None
        note = request.form.get("note", "").strip() or None

        errors = []
        if not name:
            errors.append("仕入先名は必須です。")

        if errors:
            for e in errors:
                flash(e, "error")
            conn.close()
            return render_template(
                "add_supplier.html",
                form=request.form,
            )

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        conn.execute(
            """
            INSERT INTO SUPPLIERS
                (name, phone, email, address, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (name, phone, email, address, note, now),
        )
        conn.commit()
        conn.close()

        flash("仕入先を登録しました。", "success")
        return redirect(url_for("supplier_list"))

    conn.close()
    return render_template(
        "add_supplier.html",
        form={},
    )


# ==== 仕入先編集 ====
@app.route("/suppliers/<int:supplier_id>/edit", methods=["GET", "POST"])
@login_required
def edit_supplier(supplier_id):
    conn = get_db_connection()

    supplier = conn.execute(
        """
        SELECT supplier_id, name, phone, email, address, note, created_at
        FROM SUPPLIERS
        WHERE supplier_id = ?
        """,
        (supplier_id,),
    ).fetchone()

    if supplier is None:
        conn.close()
        flash("指定された仕入先が見つかりません。", "error")
        return redirect(url_for("supplier_list"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip() or None
        email = request.form.get("email", "").strip() or None
        address = request.form.get("address", "").strip() or None
        note = request.form.get("note", "").strip() or None

        errors = []
        if not name:
            errors.append("仕入先名は必須です。")

        if errors:
            for e in errors:
                flash(e, "error")
            conn.close()
            return render_template(
                "edit_supplier.html",
                supplier_id=supplier_id,
                form=request.form,
            )

        conn.execute(
            """
            UPDATE SUPPLIERS
            SET name = ?, phone = ?, email = ?, address = ?, note = ?
            WHERE supplier_id = ?
            """,
            (name, phone, email, address, note, supplier_id),
        )
        conn.commit()
        conn.close()

        flash("仕入先を更新しました。", "success")
        return redirect(url_for("supplier_list"))

    # GET のとき既存データをフォームに詰める
    form_data = {
        "name": supplier["name"],
        "phone": supplier["phone"] or "",
        "email": supplier["email"] or "",
        "address": supplier["address"] or "",
        "note": supplier["note"] or "",
    }
    conn.close()
    return render_template(
        "edit_supplier.html",
        supplier_id=supplier_id,
        form=form_data,
    )


# ==== 仕入先削除 ====
@app.route("/suppliers/<int:supplier_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_supplier(supplier_id):
    conn = get_db_connection()

    # 在庫移動で使用されているかチェック
    count_row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM STOCK_MOVEMENTS WHERE supplier_id = ?",
        (supplier_id,),
    ).fetchone()

    if count_row["cnt"] > 0:
        conn.close()
        flash("この仕入先を使用している在庫移動があるため、削除できません。", "error")
        return redirect(url_for("supplier_list"))

    conn.execute(
        "DELETE FROM SUPPLIERS WHERE supplier_id = ?",
        (supplier_id,),
    )
    conn.commit()
    conn.close()

    flash("仕入先を削除しました。", "success")
    return redirect(url_for("supplier_list"))


# ==== カテゴリ一括登録 ====
@app.route("/categories/bulk_new", methods=["GET", "POST"])
@login_required
@admin_required
def bulk_add_categories():
    conn = get_db_connection()

    if request.method == "POST":
        raw_text = request.form.get("lines", "")
        lines = raw_text.splitlines()

        inserted_count = 0
        errors = []

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for idx, line in enumerate(lines, start=1):
            line = line.strip()
            if not line:
                continue  # 空行はスキップ

            # 「カテゴリ名, 説明」形式を想定（カンマがなければ全部カテゴリ名）
            parts = [p.strip() for p in line.split(",", 1)]
            name = parts[0]
            description = parts[1] if len(parts) > 1 and parts[1] else None

            if not name:
                errors.append(f"{idx}行目：カテゴリ名が空です。")
                continue

            conn.execute(
                """
                INSERT INTO CATEGORIES (name, description, created_at)
                VALUES (?, ?, ?)
                """,
                (name, description, now),
            )
            inserted_count += 1

        conn.commit()
        conn.close()

        if inserted_count > 0:
            flash(f"{inserted_count}件のカテゴリを登録しました。", "success")
        for e in errors:
            flash(e, "error")

        return redirect(url_for("category_list"))

    conn.close()
    return render_template("bulk_add_categories.html")


# ==== カテゴリ一覧 ====
@app.route("/categories")
@login_required
def category_list():
    conn = get_db_connection()
    categories = conn.execute(
        """
        SELECT
            category_id,
            name,
            description,
            created_at
        FROM CATEGORIES
        ORDER BY category_id DESC
        """
    ).fetchall()
    conn.close()
    return render_template("category_list.html", categories=categories)


# ==== カテゴリ登録（GET:フォーム表示 / POST:登録処理） ====
@app.route("/categories/new", methods=["GET", "POST"])
@login_required
def add_category():
    conn = get_db_connection()

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip() or None

        errors = []
        if not name:
            errors.append("カテゴリ名は必須です。")

        if errors:
            for e in errors:
                flash(e, "error")
            conn.close()
            return render_template(
                "add_category.html",
                form=request.form,
            )

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        conn.execute(
            """
            INSERT INTO CATEGORIES
                (name, description, created_at)
            VALUES (?, ?, ?)
            """,
            (name, description, now),
        )
        conn.commit()
        conn.close()

        flash("カテゴリを登録しました。", "success")
        return redirect(url_for("category_list"))

    conn.close()
    return render_template(
        "add_category.html",
        form={},
    )

# ==== 在庫移動一覧 ====
@app.route("/movements")
@login_required
def movement_list():
    conn = get_db_connection()
    movements = conn.execute(
        """
        SELECT
            m.movement_id,
            m.movement_type,
            m.quantity,
            m.memo,
            m.created_at,
            i.name AS item_name,
            s.name AS supplier_name
        FROM STOCK_MOVEMENTS m
        LEFT JOIN ITEMS i ON m.item_id = i.item_id
        LEFT JOIN SUPPLIERS s ON m.supplier_id = s.supplier_id
        ORDER BY m.movement_id DESC
        """
    ).fetchall()
    conn.close()
    return render_template("stock_movement_list.html", movements=movements)


# ==== 在庫移動登録（入庫・出庫・調整） ====
@app.route("/movements/new", methods=["GET", "POST"])
@login_required
def add_movement():
    conn = get_db_connection()

    # プルダウン用に商品・仕入先を取得
    items = conn.execute(
        "SELECT item_id, name FROM ITEMS WHERE is_active = 1 ORDER BY name"
    ).fetchall()

    suppliers = conn.execute(
        "SELECT supplier_id, name FROM SUPPLIERS ORDER BY name"
    ).fetchall()

    if request.method == "POST":
        item_id = request.form.get("item_id") or None
        movement_type = request.form.get("movement_type", "").strip()
        quantity = request.form.get("quantity") or None
        supplier_id = request.form.get("supplier_id") or None
        memo = request.form.get("memo", "").strip() or None

        errors = []

        # 必須チェック
        if not item_id:
            errors.append("商品は必須です。")

        if not movement_type:
            errors.append("移動種別は必須です。")

        qty_int = None
        if quantity:
            try:
                qty_int = int(quantity)
                if qty_int <= 0:
                    errors.append("数量は1以上の整数で入力してください。")
            except ValueError:
                errors.append("数量は整数で入力してください。")
        else:
            errors.append("数量は必須です。")

        item_id_int = None
        if item_id:
            try:
                item_id_int = int(item_id)
            except ValueError:
                errors.append("商品IDが不正です。")

        supplier_id_int = None
        if supplier_id:
            try:
                supplier_id_int = int(supplier_id)
            except ValueError:
                errors.append("仕入先IDが不正です。")

        # movement_type の簡易チェック
        if movement_type not in ("IN", "OUT", "ADJUST"):
            errors.append("移動種別が不正です。")

        if errors:
            for e in errors:
                flash(e, "error")
            conn.close()
            return render_template(
                "add_stock_movement.html",
                items=items,
                suppliers=suppliers,
                form=request.form,
            )

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        conn.execute(
            """
            INSERT INTO STOCK_MOVEMENTS
                (item_id, movement_type, quantity, supplier_id, memo, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (item_id_int, movement_type, qty_int, supplier_id_int, memo, now),
        )
        conn.commit()
        conn.close()

        flash("在庫移動を登録しました。", "success")
        return redirect(url_for("movement_list"))

    conn.close()
    return render_template(
        "add_stock_movement.html",
        items=items,
        suppliers=suppliers,
        form={},
    )


# ==== カテゴリ編集 ====
@app.route("/categories/<int:category_id>/edit", methods=["GET", "POST"])
@login_required
def edit_category(category_id):
    conn = get_db_connection()

    category = conn.execute(
        "SELECT category_id, name, description, created_at FROM CATEGORIES WHERE category_id = ?",
        (category_id,),
    ).fetchone()

    if category is None:
        conn.close()
        flash("指定されたカテゴリが見つかりません。", "error")
        return redirect(url_for("category_list"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip() or None

        errors = []
        if not name:
            errors.append("カテゴリ名は必須です。")

        if errors:
            for e in errors:
                flash(e, "error")
            conn.close()
            # 入力内容を維持して再表示
            return render_template(
                "edit_category.html",
                category_id=category_id,
                form=request.form,
            )

        conn.execute(
            """
            UPDATE CATEGORIES
            SET name = ?, description = ?
            WHERE category_id = ?
            """,
            (name, description, category_id),
        )
        conn.commit()
        conn.close()

        flash("カテゴリを更新しました。", "success")
        return redirect(url_for("category_list"))

    # GET のとき：現在の値をフォームに詰める
    form_data = {
        "name": category["name"],
        "description": category["description"] or "",
    }
    conn.close()
    return render_template(
        "edit_category.html",
        category_id=category_id,
        form=form_data,
    )


# ==== カテゴリ削除 ====
@app.route("/categories/<int:category_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_category(category_id):
    conn = get_db_connection()

    # まずはこのカテゴリを使っている商品があるかチェック
    count_row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM ITEMS WHERE category_id = ?",
        (category_id,),
    ).fetchone()

    if count_row["cnt"] > 0:
        conn.close()
        flash("このカテゴリを使用している商品があるため、削除できません。", "error")
        return redirect(url_for("category_list"))

    # 商品に使われていなければ削除
    conn.execute(
        "DELETE FROM CATEGORIES WHERE category_id = ?",
        (category_id,),
    )
    conn.commit()
    conn.close()

    flash("カテゴリを削除しました。", "success")
    return redirect(url_for("category_list"))


# ==== アプリ起動時に一度だけユーザーテーブルを確認＆admin作成 ====
ensure_users_table()

if __name__ == "__main__":
    app.run(debug=True)
