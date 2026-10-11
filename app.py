"""Interface de Bot son : token, choix des applis diffusées, volume, démarrage du bot."""

import queue
import sys
import threading
import webbrowser

import comtypes
import customtkinter as ctk
import pystray
from PIL import Image, ImageDraw

import config
from bot_core import BotController, invite_url
from mixer import Mixer
from sessions import list_audio_apps

APP_TITLE = "Bot son"
SCAN_SECONDS = 3
PORTAL_URL = "https://discord.com/developers/applications"
HELP_STEPS = (
    "1. Ouvre le portail développeur Discord et clique sur « New Application ».\n"
    "   Le nom que tu choisis sera celui du bot.\n\n"
    "2. Onglet « Bot » → « Reset Token », copie le token et colle-le dans Bot son.\n"
    "   Ne le partage jamais : il donne le contrôle total du bot.\n\n"
    "3. Clique sur « Démarrer », puis sur « Inviter sur un serveur » et choisis ton serveur.\n\n"
    "4. Rejoins un salon vocal et tape /join dans Discord."
)


def make_icon_image(size=64):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 64
    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=14 * s, fill=(88, 101, 242))
    d.polygon([(14 * s, 25 * s), (22 * s, 25 * s), (32 * s, 16 * s), (32 * s, 48 * s), (22 * s, 39 * s), (14 * s, 39 * s)], fill="white")
    for r in (8, 15):
        d.arc((32 * s - r * s, 32 * s - r * s, 32 * s + r * s, 32 * s + r * s), -45, 45, fill="white", width=max(1, int(3 * s)))
    return img


class AudioAppScanner(threading.Thread):
    """Liste en arrière-plan les applis qui ont une session audio (l'appel COM prend ~200 ms)."""

    def __init__(self, out):
        super().__init__(daemon=True)
        self._out = out
        self._stop = threading.Event()

    def run(self):
        comtypes.CoInitialize()
        while not self._stop.is_set():
            try:
                self._out.put(("apps", list_audio_apps()))
            except Exception as e:
                print(f"Liste des applis impossible : {e}")
            self._stop.wait(SCAN_SECONDS)

    def stop(self):
        self._stop.set()


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("460x640")
        self.minsize(400, 520)

        self.settings = config.load_settings()
        self.events = queue.Queue()
        self.mixer = Mixer()
        self.mixer.volume = self.settings["volume"] / 100
        self.mixer.set_apps(self.settings["apps"])
        self.bot = BotController(self.mixer, lambda kind, data: self.events.put((kind, data)))
        self.application_id = None
        self.audio_apps = {}
        self.app_vars = {}
        self._shown_apps = None
        self._tray_hint_shown = False

        self._build_ui()
        self._set_window_icon()
        self.tray = pystray.Icon(
            APP_TITLE,
            make_icon_image(),
            APP_TITLE,
            menu=pystray.Menu(
                pystray.MenuItem("Ouvrir", lambda: self.events.put(("show", None)), default=True),
                pystray.MenuItem("Quitter", lambda: self.events.put(("quit", None))),
            ),
        )
        self.tray.run_detached()
        self.scanner = AudioAppScanner(self.events)
        self.scanner.start()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll_events)

    # ---------- construction ----------

    def _build_ui(self):
        pad = {"padx": 16}

        ctk.CTkLabel(self, text="Bot son", font=ctk.CTkFont(size=22, weight="bold")).pack(anchor="w", pady=(16, 0), **pad)
        ctk.CTkLabel(self, text="Diffuse le son de tes applis dans un salon vocal Discord", text_color="gray").pack(anchor="w", **pad)

        # Token
        token_frame = ctk.CTkFrame(self)
        token_frame.pack(fill="x", pady=(14, 0), **pad)
        header = ctk.CTkFrame(token_frame, fg_color="transparent")
        header.pack(fill="x", padx=12, pady=(10, 4))
        ctk.CTkLabel(header, text="Token du bot", font=ctk.CTkFont(weight="bold")).pack(side="left")
        ctk.CTkButton(header, text="Comment créer un bot ?", width=10, fg_color="transparent", hover=False,
                      text_color=("#1f6aa5", "#5aa0e0"), command=self._show_help).pack(side="right")
        row = ctk.CTkFrame(token_frame, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(0, 12))
        self.token_entry = ctk.CTkEntry(row, show="•", placeholder_text="Colle le token ici")
        self.token_entry.pack(side="left", fill="x", expand=True)
        token = config.load_token()
        if token:
            self.token_entry.insert(0, token)
        self.show_token_btn = ctk.CTkButton(row, text="Afficher", width=80, command=self._toggle_token)
        self.show_token_btn.pack(side="left", padx=(8, 0))

        # Applis
        apps_frame = ctk.CTkFrame(self)
        apps_frame.pack(fill="both", expand=True, pady=(12, 0), **pad)
        ctk.CTkLabel(apps_frame, text="Applis à diffuser", font=ctk.CTkFont(weight="bold")).pack(anchor="w", padx=12, pady=(10, 0))
        ctk.CTkLabel(apps_frame, text="Les applis apparaissent ici dès qu'elles ont joué du son.",
                     text_color="gray", font=ctk.CTkFont(size=12)).pack(anchor="w", padx=12)
        self.apps_list = ctk.CTkScrollableFrame(apps_frame, fg_color="transparent")
        self.apps_list.pack(fill="both", expand=True, padx=6, pady=(4, 6))

        vol_row = ctk.CTkFrame(apps_frame, fg_color="transparent")
        vol_row.pack(fill="x", padx=12, pady=(0, 12))
        ctk.CTkLabel(vol_row, text="Volume").pack(side="left")
        self.volume_label = ctk.CTkLabel(vol_row, text=f"{self.settings['volume']} %", width=50, anchor="e")
        self.volume_label.pack(side="right")
        self.volume_slider = ctk.CTkSlider(vol_row, from_=0, to=200, number_of_steps=40, command=self._on_volume)
        self.volume_slider.set(self.settings["volume"])
        self.volume_slider.pack(side="left", fill="x", expand=True, padx=10)

        # Bas : état + actions
        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.pack(fill="x", pady=14, **pad)
        self.status_label = ctk.CTkLabel(bottom, text="Arrêté", anchor="w", justify="left", wraplength=420)
        self.status_label.pack(fill="x")
        self.voice_label = ctk.CTkLabel(bottom, text="", anchor="w", text_color="gray")
        self.voice_label.pack(fill="x")
        buttons = ctk.CTkFrame(bottom, fg_color="transparent")
        buttons.pack(fill="x", pady=(8, 0))
        self.start_btn = ctk.CTkButton(buttons, text="Démarrer", height=36, command=self._toggle_bot)
        self.start_btn.pack(side="left", fill="x", expand=True)
        self.invite_btn = ctk.CTkButton(buttons, text="Inviter sur un serveur", height=36, state="disabled",
                                        fg_color="gray30", command=self._open_invite)
        self.invite_btn.pack(side="left", fill="x", expand=True, padx=(8, 0))

        self._render_apps()

    def _set_window_icon(self):
        try:
            path = config.CONFIG_DIR / "icon.ico"
            config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            make_icon_image(256).save(path, sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
            # CustomTkinter impose son icône peu après le démarrage : on repasse derrière.
            self.after(300, lambda: self.iconbitmap(str(path)))
        except Exception:
            pass

    # ---------- applis ----------

    def _render_apps(self):
        selected = set(self.settings["apps"])
        captured = self.mixer.captured_apps() if self.bot.running else set()
        names = sorted(set(self.audio_apps) | selected, key=str.lower)
        state = [(n, n in selected, self.audio_apps.get(n), n in captured) for n in names]
        if state == self._shown_apps:
            return
        self._shown_apps = state

        for child in self.apps_list.winfo_children():
            child.destroy()
        self.app_vars = {}
        if not names:
            ctk.CTkLabel(self.apps_list, text="Aucune appli n'a encore joué de son.", text_color="gray").pack(pady=20)
            return
        for name, is_selected, playing, is_captured in state:
            row = ctk.CTkFrame(self.apps_list, fg_color="transparent")
            row.pack(fill="x", pady=2)
            var = ctk.BooleanVar(value=is_selected)
            self.app_vars[name] = var
            label = name[:-4] if name.lower().endswith(".exe") else name
            ctk.CTkCheckBox(row, text=label, variable=var, command=self._on_apps_changed).pack(side="left", padx=6)
            if is_captured:
                info, color = "diffusée", ("#2e8b57", "#5fd38d")
            elif playing is None:
                info, color = "pas lancée", "gray"
            elif playing:
                info, color = "joue du son", ("#1f6aa5", "#5aa0e0")
            else:
                info, color = "silencieuse", "gray"
            ctk.CTkLabel(row, text=info, text_color=color, font=ctk.CTkFont(size=12)).pack(side="right", padx=8)

    def _on_apps_changed(self):
        self.settings["apps"] = sorted(n for n, v in self.app_vars.items() if v.get())
        config.save_settings(self.settings)
        self.mixer.set_apps(self.settings["apps"])

    def _on_volume(self, value):
        pourcent = int(round(value))
        self.volume_label.configure(text=f"{pourcent} %")
        self.mixer.volume = pourcent / 100
        self.settings["volume"] = pourcent
        config.save_settings(self.settings)

    # ---------- bot ----------

    def _toggle_token(self):
        hidden = self.token_entry.cget("show") == "•"
        self.token_entry.configure(show="" if hidden else "•")
        self.show_token_btn.configure(text="Masquer" if hidden else "Afficher")

    def _toggle_bot(self):
        if self.bot.running:
            self.start_btn.configure(state="disabled", text="Arrêt…")
            self.bot.stop()
            return
        token = self.token_entry.get().strip()
        if not token:
            self.status_label.configure(text="Colle d'abord le token de ton bot.", text_color=("#c0392b", "#ff6b6b"))
            return
        if token.startswith("http"):
            self.status_label.configure(
                text="C'est un lien, pas un token : le token se trouve dans l'onglet « Bot » du portail.",
                text_color=("#c0392b", "#ff6b6b"),
            )
            return
        config.save_token(token)
        self.token_entry.configure(state="disabled")
        self.start_btn.configure(text="Arrêter")
        self.bot.start(token)

    def _open_invite(self):
        if self.application_id:
            webbrowser.open(invite_url(self.application_id))

    def _show_help(self):
        win = ctk.CTkToplevel(self)
        win.title("Créer un bot Discord")
        win.geometry("520x330")
        win.transient(self)
        ctk.CTkLabel(win, text="Créer ton bot Discord", font=ctk.CTkFont(size=18, weight="bold")).pack(anchor="w", padx=16, pady=(16, 8))
        ctk.CTkLabel(win, text=HELP_STEPS, justify="left", anchor="w", wraplength=480).pack(fill="x", padx=16)
        ctk.CTkButton(win, text="Ouvrir le portail développeur", command=lambda: webbrowser.open(PORTAL_URL)).pack(pady=16)
        win.after(100, win.focus)

    # ---------- événements ----------

    def _poll_events(self):
        try:
            while True:
                kind, data = self.events.get_nowait()
                self._handle_event(kind, data)
        except queue.Empty:
            pass
        self._render_apps()
        self.after(150, self._poll_events)

    def _handle_event(self, kind, data):
        if kind == "apps":
            self.audio_apps = data
        elif kind == "status":
            self.status_label.configure(text=data, text_color=("gray10", "gray90"))
        elif kind == "ready":
            self.application_id = data
            self.invite_btn.configure(state="normal", fg_color=("#3a7ebf", "#1f538d"))
        elif kind == "voice":
            self.voice_label.configure(text=f"🔊 Dans le salon vocal « {data} »" if data else "")
        elif kind == "volume":
            self.volume_slider.set(data)
            self._on_volume(data)
        elif kind == "stopped":
            self.token_entry.configure(state="normal")
            self.start_btn.configure(state="normal", text="Démarrer")
            self.invite_btn.configure(state="disabled", fg_color="gray30")
            if data:
                self.status_label.configure(text=data, text_color=("#c0392b", "#ff6b6b"))
            else:
                self.status_label.configure(text="Arrêté", text_color=("gray10", "gray90"))
        elif kind == "show":
            self.deiconify()
            self.lift()
            self.focus_force()
        elif kind == "quit":
            self._quit()

    def _on_close(self):
        if not self.bot.running:
            self._quit()
            return
        self.withdraw()
        if not self._tray_hint_shown:
            self._tray_hint_shown = True
            self.tray.notify("Le bot continue en arrière-plan. Clic droit sur l'icône pour quitter.", APP_TITLE)

    def _quit(self):
        self.scanner.stop()
        if self.bot.running:
            self.bot.stop()
            self.bot.join(timeout=5)
        self.tray.stop()
        self.destroy()


if __name__ == "__main__":
    if sys.stdout is None:  # lancé sans console (pythonw / exe) : on garde une trace des erreurs
        config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = open(config.CONFIG_DIR / "bot_son.log", "a", encoding="utf-8", buffering=1)
    ctk.set_appearance_mode("system")
    App().mainloop()
