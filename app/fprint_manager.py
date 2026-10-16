#!/usr/bin/env python3
"""Менеджер отпечатков: GTK4/libadwaita-оболочка над fprintd и собственной сборкой libfprint."""

import os
import re
import subprocess
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

APP_ID = "io.github.fprintmanager"
HELPER = "/usr/libexec/fprint-manager-helper"
DRIVER_PKG = "libfprint-egis-sdcp"
FPRINTD_BIN = "/usr/libexec/fprintd"
LOCAL_LIB = "/opt/libfprint-egis-sdcp/lib/libfprint-2.so.2"
CLEAR_TOOL = "/opt/libfprint-egis-sdcp/libexec/clear-storage"
DEBUG_DROPIN = "/etc/systemd/system/fprintd.service.d/fprint-manager-debug.conf"
PAM_PROFILE = "/usr/share/pam-configs/fprintd"

FPRINT_BUS = "net.reactivated.Fprint"
MANAGER_IFACE = "net.reactivated.Fprint.Manager"
DEVICE_IFACE = "net.reactivated.Fprint.Device"

# Производители USB-сканеров, которые поддерживает libfprint (для раздела «Сенсор»)
FP_VENDORS = {"1c7a": "Egis Technology", "27c6": "Goodix", "06cb": "Synaptics",
              "04f3": "Elan", "138a": "Validity", "10a5": "FPC", "2808": "Focaltech",
              "298d": "Next Biometrics", "147e": "UPEK", "045e": "Microsoft"}

HANDS = [
    ("left", "Левая рука", [
        ("left-thumb", "Большой палец"), ("left-index-finger", "Указательный палец"),
        ("left-middle-finger", "Средний палец"), ("left-ring-finger", "Безымянный палец"),
        ("left-little-finger", "Мизинец")]),
    ("right", "Правая рука", [
        ("right-thumb", "Большой палец"), ("right-index-finger", "Указательный палец"),
        ("right-middle-finger", "Средний палец"), ("right-ring-finger", "Безымянный палец"),
        ("right-little-finger", "Мизинец")]),
]
FINGER_TITLES = {f: f"{t} ({'левая' if f.startswith('left') else 'правая'} рука)"
                 for _, _, fingers in HANDS for f, t in fingers}

ENROLL_MESSAGES = {
    "enroll-stage-passed": "Отлично! Уберите палец и приложите снова",
    "enroll-retry-scan": "Не удалось считать, приложите палец ещё раз",
    "enroll-swipe-too-short": "Слишком короткое касание, попробуйте ещё раз",
    "enroll-finger-not-centered": "Палец не по центру, приложите ровнее",
    "enroll-remove-and-retry": "Уберите палец и приложите снова",
    "enroll-completed": "Отпечаток записан",
    "enroll-failed": "Запись не удалась",
    "enroll-data-full": "Память сенсора заполнена",
    "enroll-duplicate": "Этот палец уже записан",
    "enroll-disconnected": "Сенсор отключился",
    "enroll-unknown-error": "Неизвестная ошибка сенсора",
}
VERIFY_MESSAGES = {
    "verify-match": "Отпечаток распознан",
    "verify-no-match": "Отпечаток не совпал",
    "verify-retry-scan": "Не удалось считать, приложите палец ещё раз",
    "verify-swipe-too-short": "Слишком короткое касание, попробуйте ещё раз",
    "verify-finger-not-centered": "Палец не по центру, приложите ровнее",
    "verify-remove-and-retry": "Уберите палец и приложите снова",
    "verify-disconnected": "Сенсор отключился",
    "verify-unknown-error": "Неизвестная ошибка сенсора",
}
DBUS_ERRORS = {
    "PermissionDenied": "Нет прав (отменено в окне авторизации?)",
    "AlreadyInUse": "Сенсор занят другой программой",
    "Internal": "Внутренняя ошибка fprintd",
    "NoEnrolledPrints": "Нет записанных отпечатков",
    "NoActionInProgress": "Нет активной операции",
    "InvalidFingername": "Неверное имя пальца",
    "NoSuchDevice": "Сканер не найден",
    "PrintsNotDeleted": "Не удалось удалить отпечатки",
    "PrintsNotDeletedFromDevice": "Отпечаток удалён, но не из памяти сенсора",
}


# ---------------------------------------------------------------- утилиты

def dbus_error_text(err):
    """Переводит ошибку D-Bus fprintd в понятный текст."""
    remote = Gio.DBusError.get_remote_error(err) if isinstance(err, GLib.Error) else None
    if remote and remote.startswith("net.reactivated.Fprint.Error."):
        short = remote.rsplit(".", 1)[-1]
        if short in DBUS_ERRORS:
            return DBUS_ERRORS[short]
    if isinstance(err, GLib.Error):
        Gio.DBusError.strip_remote_error(err)
        return err.message
    return str(err)


def is_dbus_error(err, short_name):
    remote = Gio.DBusError.get_remote_error(err) if isinstance(err, GLib.Error) else None
    return remote == f"net.reactivated.Fprint.Error.{short_name}"


def run_quiet(argv):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def read_file(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""


def run_async(argv, on_done):
    """Запускает команду асинхронно; on_done(ok, exit_code, output)."""
    try:
        proc = Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE)
    except GLib.Error as e:
        on_done(False, -1, e.message)
        return

    def finished(p, res):
        try:
            _, out, _ = p.communicate_utf8_finish(res)
        except GLib.Error as e:
            on_done(False, -1, e.message)
            return
        code = p.get_exit_status() if p.get_if_exited() else -1
        on_done(code == 0, code, out or "")

    proc.communicate_utf8_async(None, None, finished)


# ---------------------------------------------------------------- состояние системы

class SystemInfo:
    """Снимок состояния: PAM, служба, библиотека, сенсор."""

    def __init__(self):
        common_auth = read_file("/etc/pam.d/common-auth")
        self.pam_enabled = "pam_fprintd.so" in common_auth
        profile = read_file(PAM_PROFILE)
        m = re.search(r"max-tries=(\d+)", profile)
        self.pam_max_tries = m.group(1) if m else "?"
        m = re.search(r"timeout=(\d+)", profile)
        self.pam_timeout = m.group(1) if m else "?"
        self.pam_profile_present = bool(profile)
        self.gdm_fingerprint = os.path.exists("/etc/pam.d/gdm-fingerprint")

        self.service_state = run_quiet(["systemctl", "is-active", "fprintd.service"]) or "неизвестно"
        self.debug_enabled = os.path.exists(DEBUG_DROPIN)

        ldd = run_quiet(["ldd", FPRINTD_BIN])
        m = re.search(r"libfprint-2\.so\.2 => (\S+)", ldd)
        self.lib_in_use = os.path.realpath(m.group(1)) if m else "не найдена"
        self.local_lib = os.path.exists(LOCAL_LIB)
        self.using_local = self.lib_in_use.startswith("/opt/libfprint-egis-sdcp/")
        self.clear_tool = os.access(CLEAR_TOOL, os.X_OK)
        self.pkg_driver = run_quiet(["dpkg-query", "-W", "-f=${Version}", DRIVER_PKG]) or ""
        self.pkg_fprintd = run_quiet(["dpkg-query", "-W", "-f=${Version}", "fprintd"]) or "не установлен"
        self.pkg_libfprint = run_quiet(["dpkg-query", "-W", "-f=${Version}", "libfprint-2-2"]) or "не установлена"
        self.helper_installed = os.access(HELPER, os.X_OK)

        self.usb = self._find_sensor()

    @staticmethod
    def _find_sensor():
        base = "/sys/bus/usb/devices"
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            return None
        for e in entries:
            d = os.path.join(base, e)
            vid = read_file(os.path.join(d, "idVendor"))
            if vid in FP_VENDORS:
                return {
                    "id": f"{vid}:{read_file(os.path.join(d, 'idProduct'))}",
                    "vendor": read_file(os.path.join(d, "manufacturer")) or FP_VENDORS[vid],
                    "product": read_file(os.path.join(d, "product")),
                    "serial": read_file(os.path.join(d, "serial")),
                    "path": e,
                }
        return None


# ---------------------------------------------------------------- клиент fprintd

class FprintClient:
    """Тонкая асинхронная обёртка над D-Bus API fprintd."""

    def __init__(self):
        self.bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        self.device = None
        self.device_path = None

    def connect_device(self, on_done):
        """Находит сканер по умолчанию; on_done(error_text_or_None)."""
        self.device = None

        def got_device(bus, res):
            try:
                (path,) = bus.call_finish(res).unpack()
            except GLib.Error as e:
                on_done(dbus_error_text(e))
                return
            self.device_path = path
            Gio.DBusProxy.new(self.bus, Gio.DBusProxyFlags.NONE, None, FPRINT_BUS, path,
                              DEVICE_IFACE, None, proxy_ready)

        def proxy_ready(_src, res):
            try:
                self.device = Gio.DBusProxy.new_finish(res)
            except GLib.Error as e:
                on_done(dbus_error_text(e))
                return
            on_done(None)

        self.bus.call(FPRINT_BUS, "/net/reactivated/Fprint/Manager", MANAGER_IFACE, "GetDefaultDevice",
                      None, GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE, 30000, None, got_device)

    def prop(self, name, default=None):
        if not self.device:
            return default
        v = self.device.get_cached_property(name)
        return v.unpack() if v is not None else default

    def call(self, method, args, on_done, timeout=60000):
        """on_done(result_tuple_or_None, error_or_None)."""
        def done(proxy, res):
            try:
                result = proxy.call_finish(res)
            except GLib.Error as e:
                on_done(None, e)
                return
            on_done(result.unpack() if result is not None else (), None)

        self.device.call(method, args, Gio.DBusCallFlags.NONE, timeout, None, done)

    def list_fingers(self, on_done):
        def done(result, err):
            if err is not None and is_dbus_error(err, "NoEnrolledPrints"):
                on_done([], None)
            elif err is not None:
                on_done(None, err)
            else:
                on_done(list(result[0]), None)
        self.call("ListEnrolledFingers", GLib.Variant("(s)", ("",)), done)

    def with_claim(self, action, on_done):
        """Claim -> action(release_cb) -> Release. action вызывает finish(err) по завершении."""
        def claimed(_r, err):
            if err is not None:
                on_done(err)
                return

            def finish(action_err):
                self.call("Release", None, lambda _r2, _e2: on_done(action_err))
            action(finish)
        self.call("Claim", GLib.Variant("(s)", ("",)), claimed)


# ---------------------------------------------------------------- диалоги операций

class ScanDialog(Adw.Dialog):
    """Общий диалог для записи и проверки отпечатка."""

    def __init__(self, title):
        super().__init__(title=title, content_width=420)
        self.closed = False
        self.connect("closed", self._on_closed)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      margin_top=12, margin_bottom=24, margin_start=24, margin_end=24)

        self.icon = Gtk.Image(icon_name="auth-fingerprint-symbolic", pixel_size=96)
        self.icon.add_css_class("dim-label")
        box.append(self.icon)

        self.status = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.status.add_css_class("title-3")
        box.append(self.status)

        self.hint = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.hint.add_css_class("dim-label")
        box.append(self.hint)

        self.progress = Gtk.ProgressBar(show_text=True)
        box.append(self.progress)

        self.button = Gtk.Button(label="Отмена", halign=Gtk.Align.CENTER)
        self.button.add_css_class("pill")
        self.button.connect("clicked", lambda _b: self.close())
        box.append(self.button)

        toolbar.set_content(box)
        self.set_child(toolbar)

    def set_state(self, status, hint="", style=None):
        self.status.set_label(status)
        self.hint.set_label(hint)
        self.hint.set_visible(bool(hint))
        for cls in ("success", "error", "accent", "dim-label"):
            self.icon.remove_css_class(cls)
        self.icon.add_css_class(style or "dim-label")

    def finished(self):
        self.button.set_label("Готово")
        self.button.add_css_class("suggested-action")

    def _on_closed(self, _d):
        self.closed = True
        self.on_cancel()

    def on_cancel(self):
        pass


class EnrollDialog(ScanDialog):
    def __init__(self, client, finger, on_finished):
        super().__init__(f"Запись: {FINGER_TITLES[finger]}")
        self.client, self.finger, self.on_finished = client, finger, on_finished
        self.stages = max(1, int(client.prop("num-enroll-stages", 15) or 15))
        self.stage = 0
        self.active = False
        self.done = False
        self.sig = None
        self.release = None
        self._update_progress()
        self.set_state("Подготовка сенсора…", "Может появиться запрос пароля администратора")
        GLib.idle_add(self._start)

    def _update_progress(self):
        self.progress.set_fraction(min(1.0, self.stage / self.stages))
        self.progress.set_text(f"{self.stage} из {self.stages}")

    def _start(self):
        def action(release):
            self.release = release
            if self.closed:
                release(None)
                return
            self.sig = self.client.device.connect("g-signal", self._on_signal)

            def started(_r, err):
                if err is not None:
                    self._finish(err)
                    return
                self.active = True
                self.set_state("Приложите палец к сенсору",
                               "Прикладывайте палец несколько раз, немного меняя положение", "accent")
            self.client.call("EnrollStart", GLib.Variant("(s)", (self.finger,)), started)

        self.client.with_claim(action, self._released)
        return False

    def _on_signal(self, _proxy, _sender, name, params):
        if name != "EnrollStatus":
            return
        result, done = params.unpack()
        if result == "enroll-stage-passed":
            self.stage += 1
            self._update_progress()
        msg = ENROLL_MESSAGES.get(result, result)
        if not done:
            style = "accent" if result == "enroll-stage-passed" else "error"
            self.set_state(msg, "Прикладывайте палец, пока шкала не заполнится", style)
            return
        if result == "enroll-duplicate" and not self.closed:
            self._restart_after_duplicate()
            return
        self.done = result == "enroll-completed"
        if self.done:
            self.stage = self.stages
            self._update_progress()
            self.set_state(msg, f"{FINGER_TITLES[self.finger]} теперь можно использовать для входа", "success")
        else:
            self.set_state(msg, "Попробуйте ещё раз или выберите другой палец", "error")
        self._stop()

    def _restart_after_duplicate(self):
        """Приложили уже записанный палец: проверка дубликата идёт до первого касания,
        так что просто начинаем запись заново, не закрывая диалог."""
        self.stage = 0
        self._update_progress()
        self.set_state("Этот палец уже записан",
                       f"Приложите {FINGER_TITLES[self.finger].lower()} — запись продолжается", "error")

        def restarted(_r, err):
            if err is not None:
                self.active = False
                self._finish(err)
        def stopped(_r, _e):
            if self.closed:
                self.active = False
                self._finish(None)
                return
            self.client.call("EnrollStart", GLib.Variant("(s)", (self.finger,)), restarted)
        self.client.call("EnrollStop", None, stopped)

    def _stop(self):
        if not self.active:
            return
        self.active = False
        self.client.call("EnrollStop", None, lambda _r, _e: self._finish(None))

    def _finish(self, err):
        if self.sig is not None:
            self.client.device.disconnect(self.sig)
            self.sig = None
        if err is not None:
            self.set_state("Не удалось начать запись", dbus_error_text(err), "error")
        if self.release:
            release, self.release = self.release, None
            release(err)

    def _released(self, err):
        if err is not None and not self.closed and not self.status.get_label().startswith("Не удалось"):
            self.set_state("Не удалось начать запись", dbus_error_text(err), "error")
        self.finished()
        self.on_finished(self.done)

    def on_cancel(self):
        if self.active:
            self._stop()


class VerifyDialog(ScanDialog):
    def __init__(self, client):
        super().__init__("Проверка отпечатка")
        self.client = client
        self.active = False
        self.sig = None
        self.release = None
        self.progress.set_visible(False)
        self.set_state("Подготовка сенсора…")
        GLib.idle_add(self._start)

    def _start(self):
        def action(release):
            self.release = release
            if self.closed:
                release(None)
                return
            self.sig = self.client.device.connect("g-signal", self._on_signal)

            def started(_r, err):
                if err is not None:
                    self._finish(err)
                    return
                self.active = True
                self.set_state("Приложите любой записанный палец", "", "accent")
            self.client.call("VerifyStart", GLib.Variant("(s)", ("any",)), started)

        self.client.with_claim(action, self._released)
        return False

    def _on_signal(self, _proxy, _sender, name, params):
        if name == "VerifyFingerSelected":
            (finger,) = params.unpack()
            if finger in FINGER_TITLES:
                self.set_state("Приложите палец", FINGER_TITLES[finger], "accent")
            return
        if name != "VerifyStatus":
            return
        result, done = params.unpack()
        msg = VERIFY_MESSAGES.get(result, result)
        if not done:
            self.set_state(msg, "", "error")
            return
        if result == "verify-match":
            self.set_state(msg, "Сканер работает", "success")
        else:
            self.set_state(msg, "Закройте окно и попробуйте снова", "error")
        self._stop()

    def _stop(self):
        if not self.active:
            return
        self.active = False
        self.client.call("VerifyStop", None, lambda _r, _e: self._finish(None))

    def _finish(self, err):
        if self.sig is not None:
            self.client.device.disconnect(self.sig)
            self.sig = None
        if err is not None:
            self.set_state("Не удалось начать проверку", dbus_error_text(err), "error")
        if self.release:
            release, self.release = self.release, None
            release(err)

    def _released(self, err):
        if err is not None and not self.closed and not self.status.get_label().startswith("Не удалось"):
            self.set_state("Не удалось начать проверку", dbus_error_text(err), "error")
        self.finished()

    def on_cancel(self):
        if self.active:
            self._stop()


class OutputDialog(Adw.Dialog):
    """Окно с выводом команды (журнал, сборка)."""

    def __init__(self, title, text=""):
        super().__init__(title=title, content_width=760, content_height=520)
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        copy_btn = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text="Копировать")
        copy_btn.connect("clicked", self._copy)
        header.pack_start(copy_btn)
        self.spinner = Adw.Spinner(visible=False)
        header.pack_end(self.spinner)
        toolbar.add_top_bar(header)
        self.buffer = Gtk.TextBuffer(text=text)
        self.view = Gtk.TextView(buffer=self.buffer, editable=False, monospace=True,
                                 wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                 top_margin=12, bottom_margin=12, left_margin=12, right_margin=12)
        scroller = Gtk.ScrolledWindow(child=self.view, vexpand=True)
        toolbar.set_content(scroller)
        self.set_child(toolbar)

    def set_busy(self, busy):
        self.spinner.set_visible(busy)

    def append(self, text):
        self.buffer.insert(self.buffer.get_end_iter(), text)
        GLib.idle_add(self._scroll_end)

    def _scroll_end(self):
        self.view.scroll_to_iter(self.buffer.get_end_iter(), 0, False, 0, 0)
        return False

    def _copy(self, _b):
        start, end = self.buffer.get_bounds()
        self.get_clipboard().set(self.buffer.get_text(start, end, False))


# ---------------------------------------------------------------- страницы

def action_row(title, subtitle, button_label, callback, style=None, icon=None):
    row = Adw.ActionRow(title=title, subtitle=subtitle)
    btn = Gtk.Button(label=button_label, valign=Gtk.Align.CENTER)
    if icon:
        btn = Gtk.Button(icon_name=icon, valign=Gtk.Align.CENTER, tooltip_text=button_label)
        btn.add_css_class("flat")
    if style:
        btn.add_css_class(style)
    btn.connect("clicked", lambda _b: callback())
    row.add_suffix(btn)
    row.set_activatable_widget(btn)
    return row, btn


def info_row(title, value=""):
    row = Adw.ActionRow(title=title, subtitle=value, subtitle_selectable=True)
    row.add_css_class("property")
    return row


class FingersPage(Adw.PreferencesPage):
    def __init__(self, window):
        super().__init__(title="Пальцы", icon_name="auth-fingerprint-symbolic")
        self.win = window
        self.enrolled = set()
        self.rows = {}

        self.banner_group = Adw.PreferencesGroup()
        self.status_page = Adw.StatusPage(icon_name="auth-fingerprint-symbolic",
                                          title="Сканер не найден", visible=False)
        self.status_page.add_css_class("compact")
        self.banner_group.add(self.status_page)
        self.add(self.banner_group)

        self.device_group = Adw.PreferencesGroup(title="Сканер")
        self.device_row = info_row("Устройство", "…")
        self.device_group.add(self.device_row)
        self.count_row = info_row("Записано пальцев", "…")
        self.device_group.add(self.count_row)
        verify_row, self.verify_btn = action_row("Проверить отпечаток", "Приложите любой записанный палец",
                                                 "Проверить", self.win.open_verify)
        self.device_group.add(verify_row)
        self.add(self.device_group)

        self.hand_groups = []
        for _hand, hand_title, fingers in HANDS:
            group = Adw.PreferencesGroup(title=hand_title)
            for finger, title in fingers:
                row = Adw.ActionRow(title=title)
                ok_icon = Gtk.Image(icon_name="object-select-symbolic", tooltip_text="Записан")
                ok_icon.add_css_class("success")
                add_btn = Gtk.Button(label="Записать", valign=Gtk.Align.CENTER)
                add_btn.connect("clicked", lambda _b, f=finger: self.win.open_enroll(f))
                del_btn = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                                     tooltip_text="Удалить отпечаток")
                del_btn.add_css_class("flat")
                del_btn.connect("clicked", lambda _b, f=finger: self.win.confirm_delete(f))
                row.add_prefix(ok_icon)
                row.add_suffix(add_btn)
                row.add_suffix(del_btn)
                group.add(row)
                self.rows[finger] = (row, ok_icon, add_btn, del_btn)
            self.hand_groups.append(group)
            self.add(group)

        danger = Adw.PreferencesGroup()
        del_all_row, self.del_all_btn = action_row(
            "Удалить все отпечатки", "Удаляет все ваши пальцы из fprintd и из памяти сенсора",
            "Удалить все", self.win.confirm_delete_all, "destructive-action")
        danger.add(del_all_row)
        self.add(danger)
        self.danger_group = danger

    def set_device(self, error):
        ok = error is None
        self.status_page.set_visible(not ok)
        if not ok:
            self.status_page.set_description(f"{error}\n\nПроверьте вкладку «Драйвер».")
        for g in [self.device_group, self.danger_group, *self.hand_groups]:
            g.set_visible(ok)
        if ok:
            c = self.win.client
            self.device_row.set_subtitle(f"{c.prop('name', '?')} · касание · {c.prop('num-enroll-stages', '?')} касаний для записи"
                                         if c.prop("scan-type") == "press" else
                                         f"{c.prop('name', '?')} · проведение пальцем")

    def set_fingers(self, fingers):
        self.enrolled = set(fingers)
        for finger, (row, ok_icon, add_btn, del_btn) in self.rows.items():
            enrolled = finger in self.enrolled
            row.set_subtitle("Записан" if enrolled else "")
            ok_icon.set_opacity(1.0 if enrolled else 0.0)
            add_btn.set_visible(not enrolled)
            del_btn.set_visible(enrolled)
        n = len(self.enrolled)
        self.count_row.set_subtitle(str(n) if n else "нет — добавьте палец ниже")
        self.verify_btn.set_sensitive(n > 0)
        self.del_all_btn.set_sensitive(n > 0)


class PowerButtonLock:
    """Кнопка питания (со сканером) блокирует экран, как Super+L — настройки GNOME текущего пользователя."""

    MEDIA_KEYS = "org.gnome.settings-daemon.plugins.media-keys"
    POWER = "org.gnome.settings-daemon.plugins.power"
    KEY = "XF86PowerOff"

    def __init__(self):
        source = Gio.SettingsSchemaSource.get_default()
        self.available = all(source.lookup(s, True) for s in (self.MEDIA_KEYS, self.POWER))
        if self.available:
            self.media = Gio.Settings.new(self.MEDIA_KEYS)
            self.power = Gio.Settings.new(self.POWER)

    def enabled(self):
        return self.available and self.KEY in self.media.get_strv("screensaver")

    def set_enabled(self, on, on_done):
        lock = [k for k in self.media.get_strv("screensaver") if k != self.KEY]
        if on:
            self.power.set_string("power-button-action", "nothing")
            self.media.set_strv("power-static", [""])
            self.media.set_strv("screensaver", (lock or ["<Super>l"]) + [self.KEY])
        else:
            self.power.reset("power-button-action")
            self.media.reset("power-static")
            if lock == list(self.media.get_default_value("screensaver").unpack()):
                self.media.reset("screensaver")
            else:
                self.media.set_strv("screensaver", lock)
        Gio.Settings.sync()
        # Служба горячих клавиш не отпускает старый захват кнопки питания — перезапускаем её
        run_async(["systemctl", "--user", "restart", "org.gnome.SettingsDaemon.MediaKeys.target"],
                  lambda ok, _code, out: on_done(ok, out))


class LoginPage(Adw.PreferencesPage):
    def __init__(self, window):
        super().__init__(title="Вход в систему", icon_name="system-lock-screen-symbolic")
        self.win = window
        self._updating = False

        group = Adw.PreferencesGroup(
            title="Аутентификация по отпечатку",
            description="Отпечаток вместо пароля в sudo, при входе в консоль и в окнах «Требуется авторизация». "
                        "Если палец не распознан, система спросит пароль.")
        self.pam_row = Adw.SwitchRow(title="Вход и sudo по отпечатку",
                                     subtitle="Модуль pam_fprintd в /etc/pam.d/common-auth")
        self.pam_row.connect("notify::active", self._on_pam_toggled)
        group.add(self.pam_row)
        self.tries_row = info_row("Попыток приложить палец")
        group.add(self.tries_row)
        self.timeout_row = info_row("Время ожидания пальца")
        group.add(self.timeout_row)
        self.add(group)

        gnome = Adw.PreferencesGroup(
            title="Экран входа и блокировки GNOME",
            description="GNOME использует отдельную службу gdm-fingerprint и предлагает палец сам, "
                        "если в fprintd есть записанные отпечатки. Этот переключатель на неё не влияет.")
        self.gdm_row = info_row("Служба gdm-fingerprint")
        gnome.add(self.gdm_row)
        self.add(gnome)

        self.power_lock = PowerButtonLock()
        button = Adw.PreferencesGroup(
            title="Кнопка питания со сканером",
            description="Ноутбук передаёт нажатие кнопки только после удержания около секунды — "
                        "так касание сканера для разблокировки не срабатывает как нажатие.")
        self.power_row = Adw.SwitchRow(title="Нажатие блокирует экран",
                                       subtitle="Как Super+L. Выключено — кнопка снова усыпляет ноутбук")
        self.power_row.connect("notify::active", self._on_power_toggled)
        button.add(self.power_row)
        self.add(button)

    def update(self, info):
        self._updating = True
        self.pam_row.set_active(info.pam_enabled)
        self.pam_row.set_sensitive(info.pam_profile_present)
        self.power_row.set_active(self.power_lock.enabled())
        self.power_row.set_sensitive(self.power_lock.available)
        self._updating = False
        self.tries_row.set_subtitle(info.pam_max_tries)
        self.timeout_row.set_subtitle(f"{info.pam_timeout} с")
        self.gdm_row.set_subtitle("есть" if info.gdm_fingerprint else "нет")

    def _on_pam_toggled(self, row, _pspec):
        if self._updating:
            return
        cmd = "pam-enable" if row.get_active() else "pam-disable"
        self.win.run_helper([cmd], "Вход по отпечатку " + ("включён" if row.get_active() else "выключен"))

    def _on_power_toggled(self, row, _pspec):
        if self._updating:
            return
        on = row.get_active()
        row.set_sensitive(False)

        def done(ok, out):
            row.set_sensitive(True)
            if ok:
                self.win.toast("Кнопка питания " + ("блокирует экран" if on else "усыпляет ноутбук"))
            else:
                self.win.toast("Настройки изменены, но служба клавиш не перезапустилась — выйдите и войдите снова")
        self.power_lock.set_enabled(on, done)


class DriverPage(Adw.PreferencesPage):
    def __init__(self, window):
        super().__init__(title="Драйвер", icon_name="applications-engineering-symbolic")
        self.win = window
        self._updating = False

        sensor = Adw.PreferencesGroup(title="Сенсор")
        self.usb_row = info_row("USB-устройство")
        self.usb_id_row = info_row("Идентификатор")
        self.serial_row = info_row("Серийный номер")
        for r in (self.usb_row, self.usb_id_row, self.serial_row):
            sensor.add(r)
        self.add(sensor)

        lib = Adw.PreferencesGroup(title="Библиотека libfprint")
        self.lib_row = info_row("Используется fprintd")
        self.local_row = info_row("Драйвер libfprint-egis-sdcp (SDCP, Egis 05aa)")
        self.pkg_row = info_row("Пакеты Debian")
        for r in (self.lib_row, self.local_row, self.pkg_row):
            lib.add(r)
        self.add(lib)

        svc = Adw.PreferencesGroup(title="Служба fprintd")
        self.svc_row = info_row("Состояние")
        svc.add(self.svc_row)
        self.debug_row = Adw.SwitchRow(title="Отладочный журнал",
                                       subtitle="Подробный лог libfprint в журнале systemd")
        self.debug_row.connect("notify::active", self._on_debug_toggled)
        svc.add(self.debug_row)
        restart_row, _ = action_row("Перезапустить службу", "Помогает, если сенсор «завис»",
                                    "Перезапустить", lambda: self.win.run_helper(["restart"], "fprintd перезапущен"))
        svc.add(restart_row)
        journal_row, _ = action_row("Журнал службы", "Последние 400 строк", "Показать", self.win.show_journal)
        svc.add(journal_row)
        self.add(svc)

        maint = Adw.PreferencesGroup(title="Обслуживание",
                                     description="Для этих действий нужны права администратора.")
        self.clear_row, self.clear_btn = action_row(
            "Очистить память сенсора",
            "Удаляет все отпечатки из чипа, в том числе записанные в Windows (нужен драйвер libfprint-egis-sdcp)",
            "Очистить", self.win.confirm_clear_storage, "destructive-action")
        maint.add(self.clear_row)
        self.add(maint)

    def update(self, info):
        if info.usb:
            self.usb_row.set_subtitle(f"{info.usb['vendor']} {info.usb['product']}".strip())
            self.usb_id_row.set_subtitle(info.usb["id"])
            self.serial_row.set_subtitle(info.usb["serial"] or "—")
        else:
            self.usb_row.set_subtitle("сканер на USB не найден")
            self.usb_id_row.set_subtitle("—")
            self.serial_row.set_subtitle("—")

        if info.using_local:
            self.lib_row.set_subtitle(info.lib_in_use)
        else:
            self.lib_row.set_subtitle(info.lib_in_use + "  (системная — сенсор 05aa с ней не работает)")
        self.local_row.set_subtitle(f"версия {info.pkg_driver}" if info.local_lib and info.pkg_driver else
                                   "не установлен — поставьте пакет libfprint-egis-sdcp")
        self.pkg_row.set_subtitle(f"fprintd {info.pkg_fprintd}, libfprint-2-2 {info.pkg_libfprint}")
        states = {"active": "работает", "inactive": "остановлена (запускается по запросу)",
                  "failed": "ошибка"}
        self.svc_row.set_subtitle(states.get(info.service_state, info.service_state))

        self._updating = True
        self.debug_row.set_active(info.debug_enabled)
        self._updating = False

        self.clear_btn.set_sensitive(info.clear_tool)

    def _on_debug_toggled(self, row, _pspec):
        if self._updating:
            return
        on = row.get_active()
        self.win.run_helper(["debug-on" if on else "debug-off"],
                            "Отладочный журнал " + ("включён" if on else "выключен"))


# ---------------------------------------------------------------- окно

class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Отпечатки пальцев", default_width=640, default_height=760)
        self.client = FprintClient()
        self.info = None

        self.toasts = Adw.ToastOverlay()
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Обновить")
        refresh.connect("clicked", lambda _b: self.refresh())
        header.pack_start(refresh)
        toolbar.add_top_bar(header)

        self.helper_banner = Adw.Banner(title="Не установлен помощник для действий администратора — "
                                              "переустановите пакет fprint-manager")
        toolbar.add_top_bar(self.helper_banner)

        self.fingers = FingersPage(self)
        self.login = LoginPage(self)
        self.driver = DriverPage(self)
        for page, name in ((self.fingers, "fingers"), (self.login, "login"), (self.driver, "driver")):
            self.stack.add_titled_with_icon(page, name, page.get_title(), page.get_icon_name())

        bottom = Adw.ViewSwitcherBar(stack=self.stack)
        toolbar.add_bottom_bar(bottom)
        toolbar.set_content(self.stack)
        self.toasts.set_child(toolbar)
        self.set_content(self.toasts)

        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 520sp"))
        bp.add_setter(bottom, "reveal", True)
        bp.add_setter(switcher, "visible", False)
        self.add_breakpoint(bp)

        self.refresh()

    # -- обновление

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=4))

    def refresh(self):
        self.info = SystemInfo()
        self.helper_banner.set_revealed(not self.info.helper_installed)
        self.login.update(self.info)
        self.driver.update(self.info)

        def connected(err):
            self.fingers.set_device(err)
            if err is None:
                self.refresh_fingers()
        self.client.connect_device(connected)

    def refresh_fingers(self):
        def got(fingers, err):
            if err is not None:
                self.toast(f"Не удалось получить список: {dbus_error_text(err)}")
                fingers = []
            self.fingers.set_fingers(fingers)
        self.client.list_fingers(got)

    # -- пальцы

    def open_enroll(self, finger):
        def finished(ok):
            if ok:
                self.toast("Отпечаток записан")
            self.refresh_fingers()
        EnrollDialog(self.client, finger, finished).present(self)

    def open_verify(self):
        VerifyDialog(self.client).present(self)

    def confirm_delete(self, finger):
        self._confirm(f"Удалить отпечаток?",
                      f"{FINGER_TITLES[finger]} будет удалён из fprintd и из памяти сенсора.",
                      "Удалить", lambda: self._delete(finger))

    def confirm_delete_all(self):
        self._confirm("Удалить все отпечатки?",
                      "Все ваши пальцы будут удалены. Вход по отпечатку перестанет работать, пока вы не запишете новые.",
                      "Удалить все", lambda: self._delete(None))

    def _delete(self, finger):
        def action(release):
            if finger:
                self.client.call("DeleteEnrolledFinger", GLib.Variant("(s)", (finger,)),
                                 lambda _r, e: release(e))
            else:
                self.client.call("DeleteEnrolledFingers2", None, lambda _r, e: release(e))

        def done(err):
            if err is not None and not is_dbus_error(err, "PrintsNotDeletedFromDevice"):
                self.toast(f"Не удалось удалить: {dbus_error_text(err)}")
            else:
                self.toast("Отпечаток удалён" if finger else "Все отпечатки удалены")
            self.refresh_fingers()
        self.client.with_claim(action, done)

    # -- администрирование

    def run_helper(self, args, success_text, show_output=False, title=None):
        if not self.info.helper_installed:
            self.toast("Не установлен помощник — переустановите пакет fprint-manager")
            self.refresh()
            return
        dialog = None
        if show_output:
            dialog = OutputDialog(title or "Выполнение")
            dialog.set_busy(True)
            dialog.present(self)

        def done(ok, code, out):
            if dialog:
                dialog.set_busy(False)
                dialog.append(out)
            if ok:
                if not show_output:
                    self.toast(success_text)
            elif code in (126, 127):
                self.toast("Отменено")
            else:
                self.toast("Ошибка: " + (out.strip().splitlines() or ["?"])[-1])
            self.refresh()
        run_async(["pkexec", HELPER, *args], done)

    def show_journal(self):
        self.run_helper(["journal"], "", show_output=True, title="Журнал fprintd")

    def confirm_clear_storage(self):
        self._confirm("Очистить память сенсора?",
                      "Из чипа будут удалены все отпечатки — и записанные здесь, и записанные в Windows Hello. "
                      "Потом пальцы придётся записать заново и здесь, и в Windows.",
                      "Очистить",
                      lambda: self.run_helper(["clear-storage"], "Память сенсора очищена",
                                              show_output=True, title="Очистка памяти сенсора"))

    def _confirm(self, heading, body, action_label, on_confirm):
        d = Adw.AlertDialog(heading=heading, body=body)
        d.add_response("cancel", "Отмена")
        d.add_response("ok", action_label)
        d.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE)
        d.set_default_response("cancel")
        d.set_close_response("cancel")
        d.connect("response", lambda _d, r: on_confirm() if r == "ok" else None)
        d.present(self)


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        win = self.get_active_window() or MainWindow(self)
        win.present()


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
