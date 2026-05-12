from flask import Flask, render_template, request, redirect, url_for, flash, session, send_from_directory, jsonify
from datetime import datetime
import os
import sqlite3
import bcrypt
import re
import secrets
from werkzeug.utils import secure_filename
from functools import wraps
try:
    from flask_socketio import SocketIO, join_room, emit  # type: ignore[reportMissingModuleSource]
except ImportError:
    SocketIO = None

app = Flask(__name__)
app.secret_key = 'keepy_ultra_secure_safe_key_2024'
socketio = SocketIO(app, async_mode="threading") if SocketIO else None

# Настройки загрузки файлов
UPLOAD_FOLDER = 'static/uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'mp4', 'avi', 'mov', 'pdf', 'doc', 'docx', 'zip'}

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


# ================= БАЗА ДАННЫХ =================
def get_db():
    conn = sqlite3.connect("keepy_messenger.db")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()
    # Таблица пользователей
    cur.execute(
        "CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE, password TEXT)")
    # Мягкая миграция профиля для уже существующей базы
    existing_cols = {row["name"] for row in cur.execute("PRAGMA table_info(users)").fetchall()}
    if "phone" not in existing_cols:
        cur.execute("ALTER TABLE users ADD COLUMN phone TEXT")
    if "email" not in existing_cols:
        cur.execute("ALTER TABLE users ADD COLUMN email TEXT")
    if "birth_date" not in existing_cols:
        cur.execute("ALTER TABLE users ADD COLUMN birth_date TEXT")
    if "first_name" not in existing_cols:
        cur.execute("ALTER TABLE users ADD COLUMN first_name TEXT")
    if "last_name" not in existing_cols:
        cur.execute("ALTER TABLE users ADD COLUMN last_name TEXT")
    if "agreement_accepted_at" not in existing_cols:
        cur.execute("ALTER TABLE users ADD COLUMN agreement_accepted_at TEXT")
    if "privacy_accepted_at" not in existing_cols:
        cur.execute("ALTER TABLE users ADD COLUMN privacy_accepted_at TEXT")
    # Таблица чатов
    cur.execute("CREATE TABLE IF NOT EXISTS chats (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, is_group INTEGER)")
    chat_cols = {row["name"] for row in cur.execute("PRAGMA table_info(chats)").fetchall()}
    if "invite_code" not in chat_cols:
        cur.execute("ALTER TABLE chats ADD COLUMN invite_code TEXT")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_chats_invite_code ON chats(invite_code)")
    # Таблица участников чатов
    cur.execute(
        "CREATE TABLE IF NOT EXISTS chat_members (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, user_id INTEGER)")
    chat_member_cols = {row["name"] for row in cur.execute("PRAGMA table_info(chat_members)").fetchall()}
    if "is_favorite" not in chat_member_cols:
        cur.execute("ALTER TABLE chat_members ADD COLUMN is_favorite INTEGER DEFAULT 0")
    cur.execute("UPDATE chat_members SET is_favorite = COALESCE(is_favorite, 0)")
    # Таблица сообщений
    cur.execute("""CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, sender_id INTEGER, 
        text TEXT, file_path TEXT, file_type TEXT, timestamp TEXT)""")
    # Адресная книга
    cur.execute(
        """CREATE TABLE IF NOT EXISTS contacts (
           id INTEGER PRIMARY KEY AUTOINCREMENT,
           owner_user_id INTEGER NOT NULL,
           contact_user_id INTEGER NOT NULL,
           note TEXT,
           created_at TEXT,
           UNIQUE(owner_user_id, contact_user_id)
        )"""
    )
    msg_cols = {row["name"] for row in cur.execute("PRAGMA table_info(messages)").fetchall()}
    if "delivered_at" not in msg_cols:
        cur.execute("ALTER TABLE messages ADD COLUMN delivered_at TEXT")
    if "read_at" not in msg_cols:
        cur.execute("ALTER TABLE messages ADD COLUMN read_at TEXT")
    # Индексы для ускорения выборок в диалогах и сообщениях
    cur.execute("CREATE INDEX IF NOT EXISTS idx_chat_members_chat_user ON chat_members(chat_id, user_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_chat_id_id ON messages(chat_id, id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_contacts_owner ON contacts(owner_user_id)")
    conn.commit()
    conn.close()


init_db()


# ================= АВТОРИЗАЦИЯ =================
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)

    return decorated


def validate_username(username):
    lowered = username.lower()
    reserved = {"admin", "root", "support", "system", "keepy"}
    if lowered in reserved:
        return "Этот логин зарезервирован."
    if not re.match(r'^[a-zA-Z0-9_]{3,20}$', username):
        return "Логин должен быть от 3 до 20 символов (латиница, цифры и '_')."
    if username.startswith("_") or username.endswith("_") or "__" in username:
        return "Логин не должен начинаться/заканчиваться '_' или содержать '__'."
    if username.isdigit():
        return "Логин не может состоять только из цифр."
    return None


def find_private_chat_id(conn, user_a_id, user_b_id):
    chat = conn.execute(
        """
        SELECT c.id
        FROM chats c
        JOIN chat_members cm1 ON cm1.chat_id = c.id AND cm1.user_id = ?
        JOIN chat_members cm2 ON cm2.chat_id = c.id AND cm2.user_id = ?
        WHERE c.is_group = 0
          AND (SELECT COUNT(*) FROM chat_members x WHERE x.chat_id = c.id) = 2
        LIMIT 1
        """,
        (user_a_id, user_b_id),
    ).fetchone()
    return chat["id"] if chat else None


def create_private_chat(conn, user_a_id, user_b_id):
    cur = conn.cursor()
    cur.execute("INSERT INTO chats (name, is_group) VALUES (?, 0)", ("private",))
    chat_id = cur.lastrowid
    cur.execute("INSERT INTO chat_members (chat_id, user_id) VALUES (?, ?)", (chat_id, user_a_id))
    cur.execute("INSERT INTO chat_members (chat_id, user_id) VALUES (?, ?)", (chat_id, user_b_id))
    conn.commit()
    return chat_id


def get_or_create_private_chat(conn, user_a_id, user_b_id):
    chat_id = find_private_chat_id(conn, user_a_id, user_b_id)
    if chat_id:
        return chat_id
    return create_private_chat(conn, user_a_id, user_b_id)


def get_dialog_list(conn, current_user_id):
    return conn.execute(
        """
        SELECT *
        FROM (
            SELECT
                c.id,
                c.is_group,
                u_other.username AS display_name,
                COALESCE(my_cm.is_favorite, 0) AS is_favorite,
                m.text AS last_text,
                m.file_path AS last_file_path,
                m.file_type AS last_file_type,
                m.timestamp AS last_timestamp,
                COALESCE(m.id, 0) AS last_message_id,
                (
                    SELECT COUNT(*)
                    FROM messages mx
                    WHERE mx.chat_id = c.id
                      AND mx.sender_id != ?
                      AND mx.read_at IS NULL
                ) AS unread_count
            FROM chats c
            JOIN chat_members my_cm ON my_cm.chat_id = c.id AND my_cm.user_id = ?
            JOIN chat_members other_cm ON other_cm.chat_id = c.id AND other_cm.user_id != ?
            JOIN users u_other ON u_other.id = other_cm.user_id
            LEFT JOIN messages m ON m.id = (
                SELECT id FROM messages mx WHERE mx.chat_id = c.id ORDER BY mx.id DESC LIMIT 1
            )
            WHERE c.is_group = 0
              AND (SELECT COUNT(*) FROM chat_members x WHERE x.chat_id = c.id) = 2

            UNION ALL

            SELECT
                c.id,
                c.is_group,
                COALESCE(NULLIF(TRIM(c.name), ''), 'Группа #' || c.id) AS display_name,
                COALESCE(my_cm.is_favorite, 0) AS is_favorite,
                m.text AS last_text,
                m.file_path AS last_file_path,
                m.file_type AS last_file_type,
                m.timestamp AS last_timestamp,
                COALESCE(m.id, 0) AS last_message_id,
                (
                    SELECT COUNT(*)
                    FROM messages mx
                    WHERE mx.chat_id = c.id
                      AND mx.sender_id != ?
                      AND mx.read_at IS NULL
                ) AS unread_count
            FROM chats c
            JOIN chat_members my_cm ON my_cm.chat_id = c.id AND my_cm.user_id = ?
            LEFT JOIN messages m ON m.id = (
                SELECT id FROM messages mx WHERE mx.chat_id = c.id ORDER BY mx.id DESC LIMIT 1
            )
            WHERE c.is_group = 1
        ) dialogs
        ORDER BY dialogs.last_message_id DESC, dialogs.id DESC
        """,
        (current_user_id, current_user_id, current_user_id, current_user_id, current_user_id),
    ).fetchall()


def get_chat_messages(conn, chat_id):
    return conn.execute(
        """SELECT m.*, u.username as sender_name FROM messages m
           JOIN users u ON m.sender_id = u.id WHERE m.chat_id = ?
           ORDER BY m.id ASC""",
        (chat_id,),
    ).fetchall()


def can_access_private_chat(conn, chat_id, current_user_id):
    is_member = conn.execute(
        "SELECT 1 FROM chat_members WHERE chat_id=? AND user_id=?",
        (chat_id, current_user_id),
    ).fetchone()
    if not is_member:
        return False

    chat_row = conn.execute("SELECT id, is_group FROM chats WHERE id = ?", (chat_id,)).fetchone()
    return bool(chat_row and chat_row["is_group"] == 0)


def can_access_chat(conn, chat_id, current_user_id):
    row = conn.execute(
        """
        SELECT c.id, c.is_group, c.name, c.invite_code
        FROM chats c
        JOIN chat_members cm ON cm.chat_id = c.id
        WHERE c.id = ? AND cm.user_id = ?
        """,
        (chat_id, current_user_id),
    ).fetchone()
    return row


def generate_group_invite_code(conn):
    while True:
        code = secrets.token_urlsafe(8)
        exists = conn.execute("SELECT 1 FROM chats WHERE invite_code = ?", (code,)).fetchone()
        if not exists:
            return code


def create_group_chat(conn, creator_id, group_name):
    invite_code = generate_group_invite_code(conn)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO chats (name, is_group, invite_code) VALUES (?, 1, ?)",
        (group_name.strip(), invite_code),
    )
    chat_id = cur.lastrowid
    cur.execute("INSERT INTO chat_members (chat_id, user_id) VALUES (?, ?)", (chat_id, creator_id))
    conn.commit()
    return chat_id


def get_group_members(conn, chat_id):
    return conn.execute(
        """
        SELECT u.id AS user_id, u.username
        FROM chat_members cm
        JOIN users u ON u.id = cm.user_id
        WHERE cm.chat_id = ?
        ORDER BY u.username COLLATE NOCASE ASC
        """,
        (chat_id,),
    ).fetchall()


def get_chat_favorite_status(conn, chat_id, user_id):
    row = conn.execute(
        "SELECT COALESCE(is_favorite, 0) AS is_favorite FROM chat_members WHERE chat_id = ? AND user_id = ?",
        (chat_id, user_id),
    ).fetchone()
    return bool(row and row["is_favorite"] == 1)


def chat_room(chat_id):
    return f"chat_{chat_id}"


def serialize_message(row, current_user_id):
    if row["sender_id"] == current_user_id:
        if row["read_at"]:
            status = "read"
        elif row["delivered_at"]:
            status = "delivered"
        else:
            status = "sent"
    else:
        status = ""

    return {
        "id": row["id"],
        "sender_id": row["sender_id"],
        "sender_name": row["sender_name"],
        "text": row["text"] or "",
        "file_path": row["file_path"],
        "file_type": row["file_type"],
        "timestamp": row["timestamp"],
        "is_mine": row["sender_id"] == current_user_id,
        "status": status,
    }


def mark_messages_delivered_for_user(conn, current_user_id):
    pending = conn.execute(
        """
        SELECT m.id
        FROM messages m
        JOIN chat_members cm ON cm.chat_id = m.chat_id
        WHERE cm.user_id = ?
          AND m.sender_id != ?
          AND m.delivered_at IS NULL
        """,
        (current_user_id, current_user_id),
    ).fetchall()
    if not pending:
        return []

    now_ts = datetime.now().strftime("%H:%M")
    ids = [row["id"] for row in pending]
    conn.execute(
        f"UPDATE messages SET delivered_at = ? WHERE id IN ({','.join('?' for _ in ids)})",
        [now_ts, *ids],
    )
    return ids


def mark_chat_messages_read(conn, chat_id, current_user_id):
    pending = conn.execute(
        """
        SELECT id
        FROM messages
        WHERE chat_id = ?
          AND sender_id != ?
          AND read_at IS NULL
        ORDER BY id ASC
        """,
        (chat_id, current_user_id),
    ).fetchall()
    if not pending:
        return []

    now_ts = datetime.now().strftime("%H:%M")
    ids = [row["id"] for row in pending]
    conn.execute(
        f"""UPDATE messages
            SET delivered_at = COALESCE(delivered_at, ?),
                read_at = ?
            WHERE id IN ({','.join('?' for _ in ids)})""",
        [now_ts, now_ts, *ids],
    )
    return ids


# ================= МАРШРУТЫ (ROUTES) =================
@app.route("/")
def index():
    if 'user_id' in session:
        return redirect(url_for("chats_list"))
    return render_template("landing.html")


@app.route("/terms")
def terms():
    now_date = datetime.now().strftime("%d.%m.%Y")
    return render_template("terms.html", date=now_date)


@app.route("/privacy")
def privacy():
    now_date = datetime.now().strftime("%d.%m.%Y")
    return render_template("privacy.html", date=now_date)


@app.route("/developers")
def developers():
    promo_screenshots = ["piar.png"]
    promo_gallery_urls = [
        url_for("static", filename="images/" + name) for name in promo_screenshots
    ]
    return render_template(
        "developers.html",
        promo_screenshots=promo_screenshots,
        promo_gallery_urls=promo_gallery_urls,
    )


@app.route("/gear")
def gear_page():
    return render_template("gear.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        agreement = request.form.get("agreement")
        privacy_agreement = request.form.get("privacy_agreement")

        if not agreement or not privacy_agreement:
            flash("Для регистрации нужно принять соглашение и политику обработки данных.")
            return render_template("register.html")

        username_error = validate_username(username)
        if username_error:
            flash(username_error)
            return render_template("register.html")

        # 3. Валидация пароля (мин 5 знаков, заглавная, строчная, цифра)
        if len(password) < 5 or not re.search(r'[A-Z]', password) or not re.search(r'[a-z]', password) or not re.search(
                r'\d', password):
            flash("Пароль слишком простой! Нужно: 5+ знаков, заглавная буква, строчная буква и цифра.")
            return render_template("register.html")

        conn = get_db()
        hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        try:
            now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                """INSERT INTO users
                   (username, password, agreement_accepted_at, privacy_accepted_at)
                   VALUES (?, ?, ?, ?)""",
                (username, hashed, now_ts, now_ts),
            )
            conn.commit()
            flash("Регистрация успешна! Теперь вы можете войти.")
            return redirect(url_for("login"))
        except sqlite3.IntegrityError:
            flash("Этот логин уже занят.")
        finally:
            conn.close()
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        conn.close()
        if user and bcrypt.checkpw(password.encode(), user["password"].encode()):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            return redirect(url_for("chats_list"))
        flash("Неверный логин или пароль")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    conn = get_db()
    current_user_id = session["user_id"]

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        phone = request.form.get("phone", "").strip()
        email = request.form.get("email", "").strip()
        birth_date = request.form.get("birth_date", "").strip()

        if not username:
            flash("Логин не может быть пустым.")
            conn.close()
            return redirect(url_for("profile"))
        username_error = validate_username(username)
        if username_error:
            flash(username_error)
            conn.close()
            return redirect(url_for("profile"))

        if phone and not re.match(r'^\+?[0-9\-\s\(\)]{7,20}$', phone):
            flash("Неверный формат телефона.")
        elif email and not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
            flash("Неверный формат email.")
        elif birth_date and not re.match(r'^\d{4}-\d{2}-\d{2}$', birth_date):
            flash("Неверный формат даты рождения.")
        else:
            try:
                conn.execute(
                    """UPDATE users
                       SET username = ?, first_name = ?, last_name = ?, phone = ?, email = ?, birth_date = ?
                       WHERE id = ?""",
                    (
                        username,
                        first_name or None,
                        last_name or None,
                        phone or None,
                        email or None,
                        birth_date or None,
                        current_user_id,
                    ),
                )
                conn.commit()
                session["username"] = username
                flash("Профиль обновлен.")
                conn.close()
                return redirect(url_for("profile"))
            except sqlite3.IntegrityError:
                flash("Этот логин уже занят.")

    user = conn.execute(
        "SELECT username, first_name, last_name, phone, email, birth_date FROM users WHERE id = ?",
        (current_user_id,),
    ).fetchone()
    conn.close()
    return render_template("profile.html", user=user)


@app.route("/favorites")
@login_required
def favorites():
    conn = get_db()
    current_user_id = session["user_id"]
    dialogs = [dialog for dialog in get_dialog_list(conn, current_user_id) if dialog["is_favorite"] == 1]
    conn.close()
    return render_template("favorites.html", dialogs=dialogs)


@app.route("/settings")
@login_required
def app_settings():
    return render_template("settings.html")


@app.route("/contacts", methods=["GET", "POST"])
@login_required
def contacts():
    conn = get_db()
    current_user_id = session["user_id"]

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        note = request.form.get("note", "").strip()
        target_user = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()

        if not username:
            flash("Введите username контакта.")
        elif not target_user:
            flash("Пользователь не найден.")
        elif target_user["id"] == current_user_id:
            flash("Нельзя добавить самого себя.")
        else:
            try:
                conn.execute(
                    "INSERT INTO contacts (owner_user_id, contact_user_id, note, created_at) VALUES (?, ?, ?, ?)",
                    (current_user_id, target_user["id"], note or None, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                )
                conn.commit()
                flash("Контакт добавлен в адресную книгу.")
            except sqlite3.IntegrityError:
                flash("Этот контакт уже есть в адресной книге.")

    contact_rows = conn.execute(
        """
        SELECT c.id, u.username, c.note, c.created_at
        FROM contacts c
        JOIN users u ON u.id = c.contact_user_id
        WHERE c.owner_user_id = ?
        ORDER BY u.username ASC
        """,
        (current_user_id,),
    ).fetchall()
    users = conn.execute("SELECT username FROM users WHERE id != ? ORDER BY username ASC", (current_user_id,)).fetchall()
    conn.close()
    return render_template("contacts.html", contacts=contact_rows, users=users)


@app.route("/contacts/delete/<int:contact_id>", methods=["POST"])
@login_required
def delete_contact(contact_id):
    conn = get_db()
    conn.execute(
        "DELETE FROM contacts WHERE id = ? AND owner_user_id = ?",
        (contact_id, session["user_id"]),
    )
    conn.commit()
    conn.close()
    flash("Контакт удален.")
    return redirect(url_for("contacts"))


@app.route("/start-chat/<username>")
@login_required
def start_chat(username):
    conn = get_db()
    current_user_id = session["user_id"]
    target_user = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if not target_user:
        conn.close()
        flash("Пользователь не найден.")
        return redirect(url_for("contacts"))
    if target_user["id"] == current_user_id:
        conn.close()
        flash("Нельзя начать диалог с самим собой.")
        return redirect(url_for("contacts"))
    chat_id = get_or_create_private_chat(conn, current_user_id, target_user["id"])
    conn.close()
    return redirect(url_for("view_chat", chat_id=chat_id))


@app.route("/chats", methods=["GET", "POST"])
@login_required
def chats_list():
    conn = get_db()
    current_user_id = session["user_id"]

    delivered_ids = mark_messages_delivered_for_user(conn, current_user_id)
    if delivered_ids:
        conn.commit()

    if request.method == "POST":
        action = request.form.get("action", "private")

        if action == "create_group":
            group_name = request.form.get("group_name", "").strip()

            if not group_name:
                flash("Введите название группы.")
            elif len(group_name) < 3:
                flash("Название группы должно быть не короче 3 символов.")
            else:
                chat_id = create_group_chat(conn, current_user_id, group_name)
                conn.close()
                flash("Группа создана. Ссылку-приглашение можно найти в разделе 'Участники'.")
                return redirect(url_for("view_chat", chat_id=chat_id))
        else:
            username = request.form.get("username", "").strip()
            target_user = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()

            if not username:
                flash("Введите username пользователя.")
            elif not target_user:
                flash("Пользователь не найден.")
            elif target_user["id"] == current_user_id:
                flash("Нельзя начать диалог с самим собой.")
            else:
                chat_id = get_or_create_private_chat(conn, current_user_id, target_user["id"])
                conn.close()
                return redirect(url_for("view_chat", chat_id=chat_id))

    dialogs = get_dialog_list(conn, current_user_id)
    users = conn.execute(
        "SELECT username FROM users WHERE id != ? ORDER BY username ASC",
        (current_user_id,),
    ).fetchall()
    conn.close()
    return render_template("chats.html", dialogs=dialogs, users=users)


@app.route("/chat/<int:chat_id>", methods=["GET", "POST"])
@login_required
def view_chat(chat_id):
    conn = get_db()
    current_user_id = session["user_id"]

    chat_info = can_access_chat(conn, chat_id, current_user_id)
    if not chat_info:
        conn.close()
        return "У вас нет доступа к этому чату", 403

    chat_title = ""
    if chat_info["is_group"] == 1:
        chat_title = chat_info["name"] or f"Группа #{chat_id}"
    else:
        partner = conn.execute(
            """
            SELECT u.username
            FROM users u
            JOIN chat_members cm ON cm.user_id = u.id
            WHERE cm.chat_id = ? AND u.id != ?
            LIMIT 1
            """,
            (chat_id, current_user_id),
        ).fetchone()
        if not partner:
            conn.close()
            return "Собеседник не найден", 404
        chat_title = partner["username"]

    read_ids = mark_chat_messages_read(conn, chat_id, current_user_id)
    if read_ids:
        conn.commit()
        if socketio:
            for message_id in read_ids:
                socketio.emit(
                    "message_status",
                    {"message_id": message_id, "status": "read"},
                    room=chat_room(chat_id),
                )

    if request.method == "POST":
        text = request.form.get("text", "").strip()
        file = request.files.get("file")
        f_path, f_type = None, None

        if file and file.filename != "" and allowed_file(file.filename):
            fname = secure_filename(file.filename)
            f_path = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{fname}"
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], f_path))
            ext = f_path.rsplit('.', 1)[1].lower()
            if ext in ['png', 'jpg', 'jpeg', 'gif']:
                f_type = 'image'
            elif ext in ['mp4', 'avi', 'mov']:
                f_type = 'video'
            else:
                f_type = 'doc'

        if not text and not f_path:
            flash("Нельзя отправить пустое сообщение.")
        else:
            ts = datetime.now().strftime("%H:%M")
            cur = conn.execute(
                "INSERT INTO messages (chat_id, sender_id, text, file_path, file_type, timestamp, delivered_at, read_at) VALUES (?,?,?,?,?,?,?,?)",
                (chat_id, current_user_id, text, f_path, f_type, ts, None, None))
            message_id = cur.lastrowid
            conn.commit()

            if socketio:
                new_message = conn.execute(
                    """SELECT m.*, u.username as sender_name FROM messages m
                       JOIN users u ON m.sender_id = u.id WHERE m.id = ?""",
                    (message_id,),
                ).fetchone()
                socketio.emit("new_message", serialize_message(new_message, current_user_id), room=chat_room(chat_id))

            conn.close()
            return redirect(url_for("view_chat", chat_id=chat_id))

    messages = get_chat_messages(conn, chat_id)
    dialogs = get_dialog_list(conn, current_user_id)
    is_favorite = get_chat_favorite_status(conn, chat_id, current_user_id)
    conn.close()
    return render_template(
        "chat.html",
        chat_id=chat_id,
        messages=messages,
        chat_title=chat_title,
        chat_is_group=bool(chat_info["is_group"] == 1),
        dialogs=dialogs,
        is_favorite=is_favorite,
    )


@app.route("/chat/<int:chat_id>/participants", methods=["GET", "POST"])
@login_required
def chat_participants(chat_id):
    conn = get_db()
    current_user_id = session["user_id"]
    chat_info = can_access_chat(conn, chat_id, current_user_id)
    if not chat_info:
        conn.close()
        return "У вас нет доступа к этому чату", 403
    if chat_info["is_group"] != 1:
        conn.close()
        return "Список участников доступен только для групп.", 400

    if request.method == "POST":
        action = request.form.get("action", "").strip()

        if action == "add_member":
            username = request.form.get("username", "").strip()
            if not username:
                flash("Введите username пользователя.")
            else:
                target_user = conn.execute(
                    "SELECT id, username FROM users WHERE username = ?",
                    (username,),
                ).fetchone()
                if not target_user:
                    flash("Пользователь не найден.")
                else:
                    already_member = conn.execute(
                        "SELECT 1 FROM chat_members WHERE chat_id = ? AND user_id = ?",
                        (chat_id, target_user["id"]),
                    ).fetchone()
                    if already_member:
                        flash("Этот пользователь уже в беседе.")
                    else:
                        conn.execute(
                            "INSERT INTO chat_members (chat_id, user_id, is_favorite) VALUES (?, ?, 0)",
                            (chat_id, target_user["id"]),
                        )
                        conn.commit()
                        flash(f"Пользователь {target_user['username']} добавлен в беседу.")

        elif action == "remove_member":
            user_id_raw = request.form.get("user_id", "").strip()
            try:
                target_user_id = int(user_id_raw)
            except ValueError:
                target_user_id = 0

            if target_user_id <= 0:
                flash("Некорректный пользователь.")
            elif target_user_id == current_user_id:
                flash("Нельзя удалить самого себя. Используйте 'Выйти из беседы'.")
            else:
                existing_member = conn.execute(
                    "SELECT 1 FROM chat_members WHERE chat_id = ? AND user_id = ?",
                    (chat_id, target_user_id),
                ).fetchone()
                if not existing_member:
                    flash("Пользователь уже не состоит в беседе.")
                else:
                    conn.execute(
                        "DELETE FROM chat_members WHERE chat_id = ? AND user_id = ?",
                        (chat_id, target_user_id),
                    )
                    conn.commit()
                    flash("Пользователь удален из беседы.")

        elif action == "leave_group":
            member_count_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM chat_members WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
            member_count = member_count_row["cnt"] if member_count_row else 0
            if member_count <= 1:
                conn.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
            conn.execute(
                "DELETE FROM chat_members WHERE chat_id = ? AND user_id = ?",
                (chat_id, current_user_id),
            )
            conn.commit()
            conn.close()
            flash("Вы вышли из беседы.")
            return redirect(url_for("chats_list"))

        return redirect(url_for("chat_participants", chat_id=chat_id))

    members = get_group_members(conn, chat_id)
    users = conn.execute(
        "SELECT username FROM users WHERE id != ? ORDER BY username COLLATE NOCASE ASC",
        (current_user_id,),
    ).fetchall()
    invite_link = url_for("join_group_by_link", invite_code=chat_info["invite_code"], _external=True)
    conn.close()
    return render_template(
        "group_settings.html",
        chat_id=chat_id,
        group_name=chat_info["name"] or f"Группа #{chat_id}",
        members=members,
        users=users,
        invite_link=invite_link,
    )


@app.route("/join-group/<invite_code>")
@login_required
def join_group_by_link(invite_code):
    conn = get_db()
    current_user_id = session["user_id"]
    chat = conn.execute(
        "SELECT id, is_group FROM chats WHERE invite_code = ?",
        (invite_code,),
    ).fetchone()
    if not chat or chat["is_group"] != 1:
        conn.close()
        flash("Ссылка приглашения недействительна.")
        return redirect(url_for("chats_list"))

    is_member = conn.execute(
        "SELECT 1 FROM chat_members WHERE chat_id = ? AND user_id = ?",
        (chat["id"], current_user_id),
    ).fetchone()
    if not is_member:
        conn.execute(
            "INSERT INTO chat_members (chat_id, user_id, is_favorite) VALUES (?, ?, 0)",
            (chat["id"], current_user_id),
        )
        conn.commit()
        flash("Вы вступили в группу.")
    else:
        flash("Вы уже состоите в этой группе.")
    conn.close()
    return redirect(url_for("view_chat", chat_id=chat["id"]))


@app.route("/chat/<int:chat_id>/messages")
@login_required
def chat_messages(chat_id):
    conn = get_db()
    current_user_id = session["user_id"]

    if not can_access_chat(conn, chat_id, current_user_id):
        conn.close()
        return jsonify({"error": "forbidden"}), 403

    after_id = request.args.get("after_id", "0")
    try:
        after_id = int(after_id)
    except ValueError:
        after_id = 0

    rows = conn.execute(
        """SELECT m.*, u.username as sender_name FROM messages m
           JOIN users u ON m.sender_id = u.id
           WHERE m.chat_id = ? AND m.id > ?
           ORDER BY m.id ASC""",
        (chat_id, after_id),
    ).fetchall()
    read_ids = mark_chat_messages_read(conn, chat_id, current_user_id)
    if read_ids:
        conn.commit()
        if socketio:
            for message_id in read_ids:
                socketio.emit(
                    "message_status",
                    {"message_id": message_id, "status": "read"},
                    room=chat_room(chat_id),
                )
    conn.close()

    payload = []
    for m in rows:
        payload.append(
            {
                "id": m["id"],
                "sender_id": m["sender_id"],
                "sender_name": m["sender_name"],
                "text": m["text"] or "",
                "file_path": m["file_path"],
                "file_type": m["file_type"],
                "timestamp": m["timestamp"],
                "is_mine": m["sender_id"] == current_user_id,
            }
        )

    return jsonify({"messages": payload})


@app.route("/chat/<int:chat_id>/favorite-toggle", methods=["POST"])
@login_required
def toggle_chat_favorite(chat_id):
    conn = get_db()
    current_user_id = session["user_id"]
    if not can_access_chat(conn, chat_id, current_user_id):
        conn.close()
        return "У вас нет доступа к этому чату", 403

    current_state = get_chat_favorite_status(conn, chat_id, current_user_id)
    conn.execute(
        "UPDATE chat_members SET is_favorite = ? WHERE chat_id = ? AND user_id = ?",
        (0 if current_state else 1, chat_id, current_user_id),
    )
    conn.commit()
    conn.close()
    return redirect(request.form.get("next") or url_for("view_chat", chat_id=chat_id))


if socketio:
    @socketio.on("join_chat")
    def on_join_chat(data):
        if "user_id" not in session:
            return
        if not isinstance(data, dict):
            return
        chat_id = data.get("chat_id")
        if not isinstance(chat_id, int):
            return
        conn = get_db()
        allowed = can_access_chat(conn, chat_id, session["user_id"])
        conn.close()
        if not allowed:
            return
        join_room(chat_room(chat_id))
        emit("joined_chat", {"chat_id": chat_id})
        conn = get_db()
        read_ids = mark_chat_messages_read(conn, chat_id, session["user_id"])
        if read_ids:
            conn.commit()
            for message_id in read_ids:
                socketio.emit(
                    "message_status",
                    {"message_id": message_id, "status": "read"},
                    room=chat_room(chat_id),
                )
        conn.close()


@app.route('/uploads/<name>')
def download_file(name):
    return send_from_directory(app.config["UPLOAD_FOLDER"], name)


if __name__ == "__main__":
    socketio.run(app,host ="0.0.0.0", port=5000, debug=True, allow_unsafe_werkzeug=True)
