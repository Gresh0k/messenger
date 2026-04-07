from flask import Flask, render_template, request, redirect, url_for, flash, session, send_from_directory, jsonify
from datetime import datetime
import os
import sqlite3
import bcrypt
import re
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
    # Таблица чатов
    cur.execute("CREATE TABLE IF NOT EXISTS chats (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, is_group INTEGER)")
    # Таблица участников чатов
    cur.execute(
        "CREATE TABLE IF NOT EXISTS chat_members (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, user_id INTEGER)")
    # Таблица сообщений
    cur.execute("""CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, sender_id INTEGER, 
        text TEXT, file_path TEXT, file_type TEXT, timestamp TEXT)""")
    # Индексы для ускорения выборок в диалогах и сообщениях
    cur.execute("CREATE INDEX IF NOT EXISTS idx_chat_members_chat_user ON chat_members(chat_id, user_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_chat_id_id ON messages(chat_id, id)")
    conn.commit()
    conn.close()


# ================= АВТОРИЗАЦИЯ =================
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)

    return decorated


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
        SELECT
            c.id,
            u_other.username AS other_username,
            m.text AS last_text,
            m.file_path AS last_file_path,
            m.file_type AS last_file_type,
            m.timestamp AS last_timestamp
        FROM chats c
        JOIN chat_members my_cm ON my_cm.chat_id = c.id AND my_cm.user_id = ?
        JOIN chat_members other_cm ON other_cm.chat_id = c.id AND other_cm.user_id != ?
        JOIN users u_other ON u_other.id = other_cm.user_id
        LEFT JOIN messages m ON m.id = (
            SELECT id FROM messages mx WHERE mx.chat_id = c.id ORDER BY mx.id DESC LIMIT 1
        )
        WHERE c.is_group = 0
          AND (SELECT COUNT(*) FROM chat_members x WHERE x.chat_id = c.id) = 2
        ORDER BY COALESCE(m.id, 0) DESC, c.id DESC
        """,
        (current_user_id, current_user_id),
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


def chat_room(chat_id):
    return f"chat_{chat_id}"


def serialize_message(row, current_user_id):
    return {
        "id": row["id"],
        "sender_id": row["sender_id"],
        "sender_name": row["sender_name"],
        "text": row["text"] or "",
        "file_path": row["file_path"],
        "file_type": row["file_type"],
        "timestamp": row["timestamp"],
        "is_mine": row["sender_id"] == current_user_id,
    }


# ================= МАРШРУТЫ (ROUTES) =================
@app.route("/")
def index():
    if 'user_id' in session: return redirect(url_for("chats_list"))
    return redirect(url_for("login"))


@app.route("/terms")
def terms():
    now_date = datetime.now().strftime("%d.%m.%Y")
    return render_template("terms.html", date=now_date)


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        agreement = request.form.get("agreement")  # Проверка чекбокса соглашения

        # 1. Проверка чекбокса
        if not agreement:
            flash("Вы должны принять условия соглашения для регистрации!")
            return render_template("register.html")

        # 2. Валидация логина (4-20 символов, латиница, цифры, _)
        if not re.match(r'^[a-zA-Z0-9_]{4,20}$', username):
            flash("Логин должен быть от 4 до 20 символов (только латиница, цифры и '_')")
            return render_template("register.html")

        # 3. Валидация пароля (мин 8 знаков, заглавная, строчная, цифра)
        if len(password) < 8 or not re.search(r'[A-Z]', password) or not re.search(r'[a-z]', password) or not re.search(
                r'\d', password):
            flash("Пароль слишком простой! Нужно: 8+ знаков, заглавная буква, строчная буква и цифра.")
            return render_template("register.html")

        conn = get_db()
        hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        try:
            conn.execute("INSERT INTO users (username, password) VALUES (?, ?)", (username, hashed))
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


@app.route("/chats", methods=["GET", "POST"])
@login_required
def chats_list():
    conn = get_db()
    current_user_id = session["user_id"]

    if request.method == "POST":
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

    # Защита: проверяем, что юзер в этом чате
    if not can_access_private_chat(conn, chat_id, current_user_id):
        conn.close()
        return "У вас нет доступа к этому чату", 403

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
                "INSERT INTO messages (chat_id, sender_id, text, file_path, file_type, timestamp) VALUES (?,?,?,?,?,?)",
                (chat_id, current_user_id, text, f_path, f_type, ts))
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
    conn.close()
    return render_template("chat.html", chat_id=chat_id, messages=messages, partner_username=partner["username"])


@app.route("/chat/<int:chat_id>/messages")
@login_required
def chat_messages(chat_id):
    conn = get_db()
    current_user_id = session["user_id"]

    if not can_access_private_chat(conn, chat_id, current_user_id):
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
        allowed = can_access_private_chat(conn, chat_id, session["user_id"])
        conn.close()
        if not allowed:
            return
        join_room(chat_room(chat_id))
        emit("joined_chat", {"chat_id": chat_id})


@app.route('/uploads/<name>')
def download_file(name):
    return send_from_directory(app.config["UPLOAD_FOLDER"], name)


if __name__ == "__main__":
    init_db()
    if socketio:
        socketio.run(app, debug=True)
    else:
        app.run(debug=True)
