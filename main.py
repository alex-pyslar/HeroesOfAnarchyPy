import sys
import requests
import websocket
import json
import threading
import time
import jwt

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QDialog, QMessageBox, QGraphicsView, QGraphicsScene
)
from PyQt5.QtGui import QPainter, QBrush, QColor
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QRectF, QObject

# --- Конфигурация клиента (теперь по умолчанию) ---
DEFAULT_SERVER_IP = "127.0.0.1"
DEFAULT_SERVER_PORT = "3000"
TILE_SIZE = 30  # Размер одной "клетки" на поле в пикселях
MAP_WIDTH_TILES = 40  # Ширина поля в клетках (должна совпадать с Rust-сервером)
MAP_HEIGHT_TILES = 20  # Высота поля в клетках (должна совпадать с Rust-сервером)

# --- Глобальные переменные для состояния игры ---
current_user_id = None
jwt_token = None
player_x, player_y, player_z = 0.0, 0.0, 0.0

# Глобальные переменные для URL'ов сервера, чтобы их можно было использовать после AuthDialog
SERVER_HOST_URL = None
WEBSOCKET_URL_FOR_GAME = None


# --- Класс-посредник для сигналов WebSocket, наследуется от QObject ---
class WebSocketSignals(QObject):
    message_received = pyqtSignal(str)
    error_occurred = pyqtSignal(str)
    closed = pyqtSignal()
    opened = pyqtSignal()


# --- Класс для WebSocket клиента в отдельном потоке ---
class WebSocketClientThread(threading.Thread):
    def __init__(self, url, token, user_id):
        super().__init__()
        self.url = url
        self.token = token
        self.user_id = user_id
        self.ws = None
        self._running = True
        self.signals = WebSocketSignals()

    def on_message(self, ws, message):
        self.signals.message_received.emit(message)

    def on_error(self, ws, error):
        print(f"WebSocket Error: {error}")
        self.signals.error_occurred.emit(str(error))
        self.signals.closed.emit()  # Эмитируем сигнал закрытия при ошибке

    def on_close(self, ws, close_status_code, close_msg):
        print(f"WebSocket Closed: {close_status_code} - {close_msg}")
        self.signals.closed.emit()
        self._running = False  # Устанавливаем флаг в False при закрытии

    def on_open(self, ws):
        print("WebSocket Opened!")
        self.signals.opened.emit()
        # Сообщение Auth больше не отправляется здесь, так как токен передается в заголовке

    def run(self):
        print(f"DEBUG: WebSocketClientThread for user {self.user_id} starting.")
        self._running = True
        while self._running:
            try:
                headers = {'Authorization': f'Bearer {self.token}'}  # Передача JWT в заголовке
                self.ws = websocket.WebSocketApp(
                    self.url,
                    on_open=self.on_open,
                    on_message=self.on_message,
                    on_error=self.on_error,
                    on_close=self.on_close,
                    header=headers  # Передача заголовков для аутентификации
                )
                self.ws.run_forever(
                    ping_interval=10,
                    ping_timeout=5,
                )
                if self._running:  # Если run_forever завершился, но _running все еще True, пытаемся переподключиться
                    print("DEBUG: WebSocket run_forever finished. Attempting to reconnect in 5 seconds...")
                    time.sleep(5)
            except Exception as e:
                print(f"DEBUG: WebSocketApp creation or run_forever failed: {e}. Retrying in 5 seconds...")
                time.sleep(5)

    def send_message(self, message):
        if self.ws and self.ws.sock and self.ws.sock.connected:
            try:
                self.ws.send(message)
                print(f"DEBUG: Message sent: {message}")
            except websocket._exceptions.WebSocketConnectionClosedException:
                print("WebSocket connection closed, cannot send message.")
                self.signals.closed.emit()  # Эмитируем сигнал закрытия
            except Exception as e:
                print(f"Error sending message: {e}")
        else:
            print("WebSocket not connected, cannot send message.")

    def stop(self):
        print("Stopping WebSocket thread...")
        self._running = False
        if self.ws:
            try:
                self.ws.close()  # Закрываем соединение безопасно
            except Exception as e:
                print(f"DEBUG: Error closing WebSocket: {e}")  # Логируем ошибку, но не прерываем
            finally:
                self.ws = None  # Очищаем ссылку на WebSocket


# --- Окно игры ---
class GameWindow(QMainWindow):
    def __init__(self, user_id, jwt_token, websocket_url):
        super().__init__()
        self.user_id = user_id
        self.jwt_token = jwt_token
        self.websocket_url = websocket_url

        self.player_x = 0.0
        self.player_y = 0.0
        self.player_z = 0.0

        self.other_players = {}

        self.init_map()
        self.init_ui()

        self.websocket_thread = WebSocketClientThread(
            self.websocket_url,
            self.jwt_token,
            self.user_id
        )
        self.websocket_thread.signals.message_received.connect(self.websocket_on_message)
        self.websocket_thread.signals.error_occurred.connect(self.handle_websocket_error)
        self.websocket_thread.signals.closed.connect(self.handle_websocket_close)
        self.websocket_thread.signals.opened.connect(self.handle_websocket_open)

        self.websocket_thread.start()

        self.position_send_timer = QTimer(self)
        self.position_send_timer.timeout.connect(self.send_current_position)
        self._position_timer_started = False

        self.last_sent_x, self.last_sent_y, self.last_sent_z = -1.0, -1.0, -1.0

    def init_map(self):
        self.scene = QGraphicsScene()
        self.view = QGraphicsView(self.scene)
        self.view.setRenderHint(QPainter.Antialiasing)
        self.view.setFixedSize(MAP_WIDTH_TILES * TILE_SIZE, MAP_HEIGHT_TILES * TILE_SIZE)

        self.scene.setSceneRect(0, 0, MAP_WIDTH_TILES * TILE_SIZE, MAP_HEIGHT_TILES * TILE_SIZE)
        for x in range(0, MAP_WIDTH_TILES * TILE_SIZE + 1, TILE_SIZE):
            self.scene.addLine(x, 0, x, MAP_HEIGHT_TILES * TILE_SIZE, QColor(200, 200, 200))
        for y in range(0, MAP_HEIGHT_TILES * TILE_SIZE + 1, TILE_SIZE):
            self.scene.addLine(0, y, MAP_WIDTH_TILES * TILE_SIZE, y, QColor(200, 200, 200))

    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        main_layout.addWidget(self.view)

        control_layout = QHBoxLayout()
        self.position_label = QLabel(f"Ваша позиция: ({int(self.player_x)}, {int(self.player_y)})")
        control_layout.addWidget(self.position_label)

        # Добавляем кнопку "Выйти"
        self.exit_button = QPushButton("Выйти")
        self.exit_button.clicked.connect(self.exit_game)
        control_layout.addWidget(self.exit_button)

        main_layout.addLayout(control_layout)

    def handle_websocket_open(self):
        print("DEBUG: WebSocket connection opened. Awaiting InitialPlayers.")
        self.statusBar().showMessage("Connected to server.")

    def keyPressEvent(self, event):
        moved = False
        step = 1.0

        new_x, new_y = self.player_x, self.player_y

        if event.key() == Qt.Key_W:
            new_y -= step
            moved = True
        elif event.key() == Qt.Key_S:
            new_y += step
            moved = True
        elif event.key() == Qt.Key_A:
            new_x -= step
            moved = True
        elif event.key() == Qt.Key_D:
            new_x += step
            moved = True

        new_x = max(0.0, min(new_x, MAP_WIDTH_TILES - 1.0))
        new_y = max(0.0, min(new_y, MAP_HEIGHT_TILES - 1.0))

        if moved:
            self.player_x = new_x
            self.player_y = new_y
            self.update_player_visual(self.user_id, self.player_x, self.player_y, self.player_z)
            self.position_label.setText(f"Ваша позиция: ({int(self.player_x)}, {int(self.player_y)})")
            self.send_current_position()

    def send_current_position(self):
        if (self.last_sent_x != self.player_x or
                self.last_sent_y != self.player_y or
                self.last_sent_z != self.player_z):
            message = {
                "type": "PlayerPosition",
                "payload": {
                    "user_id": self.user_id,
                    "x": self.player_x,
                    "y": self.player_y,
                    "z": self.player_z
                }
            }
            self.websocket_thread.send_message(json.dumps(message))
            self.last_sent_x = self.player_x
            self.last_sent_y = self.player_y
            self.last_sent_z = self.player_z

    def update_player_visual(self, user_id, x, y, z):
        rect = QRectF(x * TILE_SIZE, y * TILE_SIZE, TILE_SIZE, TILE_SIZE)

        if user_id == self.user_id:
            if not hasattr(self, 'my_player_item') or self.my_player_item is None:
                self.my_player_item = self.scene.addEllipse(rect, QColor(0, 0, 0), QBrush(QColor(255, 0, 0)))  # Красный
                self.my_player_text_item = self.scene.addSimpleText("Я")
                self.my_player_text_item.setParentItem(self.my_player_item)
            else:
                self.my_player_item.setRect(rect)
            self.my_player_text_item.setPos(x * TILE_SIZE + TILE_SIZE / 4, y * TILE_SIZE + TILE_SIZE / 4)
            self.position_label.setText(f"Ваша позиция: ({int(self.player_x)}, {int(self.player_y)})")
        else:
            if user_id in self.other_players:
                item_group = self.other_players[user_id]
                ellipse_item = item_group.childItems()[0]
                text_item = item_group.childItems()[1]
                ellipse_item.setRect(rect)
                text_item.setPos(x * TILE_SIZE + TILE_SIZE / 4, y * TILE_SIZE + TILE_SIZE / 4)
            else:
                item_group = self.scene.createItemGroup([])
                ellipse_item = self.scene.addEllipse(rect, QColor(0, 0, 0), QBrush(QColor(0, 0, 255)))  # Синий
                text_item = self.scene.addSimpleText(str(user_id))
                text_item.setPos(x * TILE_SIZE + TILE_SIZE / 4, y * TILE_SIZE + TILE_SIZE / 4)
                item_group.addToGroup(ellipse_item)
                item_group.addToGroup(text_item)
                self.other_players[user_id] = item_group
        self.scene.update()

    def remove_player_visual(self, user_id):
        if user_id == self.user_id:
            print(f"DEBUG: Ignoring PlayerDisconnected for self (user_id: {user_id})")
            return
        if user_id in self.other_players:
            print(f"DEBUG: Attempting to remove player {user_id}. Current other_players: {self.other_players.keys()}")
            item_group = self.other_players.pop(user_id)
            if item_group:
                # Явное удаление всех дочерних элементов перед уничтожением группы
                for item in item_group.childItems():
                    self.scene.removeItem(item)
                self.scene.destroyItemGroup(item_group)
                print(f"DEBUG: Successfully removed player {user_id} from the scene. Updated other_players: {self.other_players.keys()}")
            else:
                print(f"DEBUG: Item group for player {user_id} is None")
            self.scene.update()  # Принудительное обновление сцены
        else:
            print(f"DEBUG: Player {user_id} not found in other_players for removal")

    def websocket_on_message(self, message):
        try:
            data = json.loads(message)
            msg_type = data.get("type")
            payload = data.get("payload")

            if msg_type == "PlayerPosition" and payload:
                user_id = payload.get("user_id")
                if user_id is None:
                    print(f"DEBUG: Missing user_id in PlayerPosition message: {message}")
                    return
                x = float(payload.get("x"))
                y = float(payload.get("y"))
                z = float(payload.get("z", 0.0))

                if user_id == self.user_id:
                    self.player_x = x
                    self.player_y = y
                    self.player_z = z

                self.update_player_visual(user_id, x, y, z)

            elif msg_type == "PlayerDisconnected" and payload:
                disconnected_user_id = payload.get("user_id")
                if disconnected_user_id is None:
                    print(f"DEBUG: Missing user_id in PlayerDisconnected message: {message}")
                    return
                print(f"DEBUG: Received PlayerDisconnected for user {disconnected_user_id}")
                self.remove_player_visual(disconnected_user_id)

            elif msg_type == "InitialPlayers":
                print("DEBUG: Received initial players data")
                if payload:
                    for player_data in payload:
                        user_id = player_data.get("user_id")
                        if user_id is None:
                            print(f"DEBUG: Missing user_id in InitialPlayers data: {player_data}")
                            continue
                        x = float(player_data.get("x"))
                        y = float(player_data.get("y"))
                        z = float(player_data.get("z", 0.0))

                        if user_id == self.user_id:
                            self.player_x = x
                            self.player_y = y
                            self.player_z = z
                            print(
                                f"DEBUG: Set initial player position for user {self.user_id} to ({self.player_x}, {self.player_y}, {self.player_z})")
                            if not self._position_timer_started:
                                self.position_send_timer.start(100)
                                self._position_timer_started = True
                                print("DEBUG: Position send timer started after InitialPlayers received")
                                self.send_current_position()

                        self.update_player_visual(user_id, x, y, z)
            else:
                print(f"DEBUG: Received unknown message type or invalid payload: {data}")
        except json.JSONDecodeError as e:
            print(f"DEBUG: JSON decode error: {e}, message: {message}")
            QMessageBox.critical(self, "Ошибка JSON",
                                 f"Произошла ошибка декодирования JSON. Получено: '{message}'. Ошибка: {e}")
        except Exception as e:
            print(f"DEBUG: Error handling WebSocket message: {e}, message: {message}")
            QMessageBox.critical(self, "Ошибка обработки сообщения", f"Произошла ошибка: {e}")

    def handle_websocket_error(self, error_message):
        print(f"DEBUG: WebSocket error: {error_message}")
        self.handle_websocket_close()

    def handle_websocket_close(self):
        print("DEBUG: WebSocket connection closed")
        if self._position_timer_started:
            self.position_send_timer.stop()
            self._position_timer_started = False
        self.statusBar().showMessage("Disconnected from server.")

    def exit_game(self):
        print("DEBUG: Exit button clicked")
        if self.websocket_thread:
            # Отправляем сообщение PlayerLogout перед закрытием
            message = {
                "type": "PlayerLogout",
                "payload": {
                    "user_id": self.user_id
                }
            }
            self.websocket_thread.send_message(json.dumps(message))
            print(f"DEBUG: Sent PlayerLogout for user {self.user_id}")
            # Ждем достаточно времени, чтобы сервер успел обработать и разослать сообщение
            time.sleep(0.5)  # Задержка 500 мс
            # Закрываем WebSocket безопасно
            self.websocket_thread.stop()
            self.websocket_thread.join(timeout=5)
            if self.websocket_thread.is_alive():
                print("DEBUG: WebSocket thread did not terminate in time")
            else:
                print("DEBUG: WebSocket thread terminated successfully")
        # Закрываем окно программы
        self.close()

    def closeEvent(self, event):
        print("DEBUG: Closing game window")
        self.exit_game()  # Вызываем метод выхода при закрытии окна
        event.accept()


# --- Диалог аутентификации ---
class AuthDialog(QDialog):
    login_successful = pyqtSignal(int, str, str, str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Вход / Регистрация")
        self.setGeometry(200, 200, 300, 150)
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout()

        self.login_input = QLineEdit(self)
        self.login_input.setPlaceholderText("Логин")
        self.login_input.setText("123")  # Стандартный логин
        layout.addWidget(self.login_input)

        self.password_input = QLineEdit(self)
        self.password_input.setPlaceholderText("Пароль")
        self.password_input.setEchoMode(QLineEdit.Password)
        self.password_input.setText("123")  # Стандартный пароль
        layout.addWidget(self.password_input)

        self.server_ip_input = QLineEdit(self)
        self.server_ip_input.setPlaceholderText(f"IP сервера (по умолчанию: {DEFAULT_SERVER_IP})")
        self.server_ip_input.setText(DEFAULT_SERVER_IP)
        layout.addWidget(self.server_ip_input)

        self.server_port_input = QLineEdit(self)
        self.server_port_input.setPlaceholderText(f"Порт сервера (по умолчанию: {DEFAULT_SERVER_PORT})")
        self.server_port_input.setText(DEFAULT_SERVER_PORT)
        layout.addWidget(self.server_port_input)

        button_layout = QHBoxLayout()
        login_btn = QPushButton("Вход")
        login_btn.clicked.connect(self.login)
        button_layout.addWidget(login_btn)

        register_btn = QPushButton("Регистрация")
        register_btn.clicked.connect(self.register)
        button_layout.addWidget(register_btn)

        layout.addLayout(button_layout)
        self.setLayout(layout)

    def get_base_url(self):
        ip = self.server_ip_input.text() or DEFAULT_SERVER_IP
        port = self.server_port_input.text() or DEFAULT_SERVER_PORT
        return f"http://{ip}:{port}"

    def get_websocket_url(self):
        ip = self.server_ip_input.text() or DEFAULT_SERVER_IP
        port = self.server_port_input.text() or DEFAULT_SERVER_PORT
        return f"ws://{ip}:{port}/api/ws"

    def register(self):
        login = self.login_input.text()
        password = self.password_input.text()
        if not login or not password:
            QMessageBox.warning(self, "Ошибка", "Логин и пароль не могут быть пустыми.")
            return

        url = f"{self.get_base_url()}/api/register"
        try:
            response = requests.post(url, json={"login": login, "password": password})
            if response.status_code == 200:
                QMessageBox.information(self, "Успех", "Регистрация прошла успешно!")
            else:
                QMessageBox.warning(self, "Ошибка", f"Ошибка регистрации: {response.text}")
        except requests.exceptions.ConnectionError:
            QMessageBox.critical(self, "Ошибка соединения", "Не удалось подключиться к серверу.")
        except Exception as e:
            QMessageBox.critical(self, "Неизвестная ошибка", f"Произошла ошибка: {e}")

    def login(self):
        login = self.login_input.text()
        password = self.password_input.text()
        if not login or not password:
            QMessageBox.warning(self, "Ошибка", "Логин и пароль не могут быть пустыми.")
            return

        url = f"{self.get_base_url()}/api/login"
        try:
            response = requests.post(url, json={"login": login, "password": password})
            if response.status_code == 200:
                data = response.json()
                token = data.get("token")
                if token:
                    try:
                        decoded_token = jwt.decode(token, options={"verify_signature": False})
                        user_id = int(decoded_token.get("sub"))
                        self.login_successful.emit(user_id, token, self.get_base_url(), self.get_websocket_url())
                        self.accept()
                    except Exception as e:
                        QMessageBox.critical(self, "Ошибка токена", f"Не удалось декодировать токен: {e}")
                else:
                    QMessageBox.warning(self, "Ошибка", "Токен не получен.")
            else:
                QMessageBox.warning(self, "Ошибка", f"Ошибка входа: {response.text}")
        except requests.exceptions.ConnectionError:
            QMessageBox.critical(self, "Ошибка соединения", "Не удалось подключиться к серверу.")
        except Exception as e:
            QMessageBox.critical(self, "Неизвестная ошибка", f"Произошла ошибка: {e}")


# --- Точка входа в приложение ---
if __name__ == "__main__":
    app = QApplication(sys.argv)


    def handle_login_successful(user_id, token, server_host_url_param, websocket_url_param):
        global current_user_id, jwt_token, SERVER_HOST_URL, WEBSOCKET_URL_FOR_GAME
        current_user_id = user_id
        jwt_token = token
        SERVER_HOST_URL = server_host_url_param
        WEBSOCKET_URL_FOR_GAME = websocket_url_param
        print(f"DEBUG: Login successful signal received: User ID = {user_id}, Server: {SERVER_HOST_URL}")


    auth_dialog = AuthDialog()
    auth_dialog.login_successful.connect(handle_login_successful)

    if auth_dialog.exec_() == QDialog.Accepted:
        if current_user_id is not None and jwt_token is not None and \
                WEBSOCKET_URL_FOR_GAME is not None:
            game_window = GameWindow(current_user_id, jwt_token, WEBSOCKET_URL_FOR_GAME)
            game_window.show()
            sys.exit(app.exec_())
        else:
            QMessageBox.critical(None, "Ошибка запуска", "Данные для запуска игры отсутствуют.")
            sys.exit(1)
    else:
        sys.exit(0)