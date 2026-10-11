"""Bot Discord exécuté dans son propre thread, piloté par l'interface."""

import asyncio
import threading

import discord
from discord import app_commands

from mixer import MixerSource

# Connect, Speak, View Channel, Send Messages
INVITE_PERMISSIONS = 3148800


def invite_url(application_id):
    return (
        f"https://discord.com/oauth2/authorize?client_id={application_id}"
        f"&scope=bot+applications.commands&permissions={INVITE_PERMISSIONS}"
    )


class BotController:
    """Démarre/arrête le bot. `on_event(kind, data)` est appelé depuis le thread du bot :
    ("status", texte), ("ready", application_id), ("voice", nom_du_salon|None),
    ("volume", pourcent), ("stopped", message_d_erreur|None).
    """

    def __init__(self, mixer, on_event):
        self.mixer = mixer
        self._on_event = on_event
        self._client = None
        self._loop = None
        self._thread = None

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, token):
        if self.running:
            return
        self._thread = threading.Thread(target=self._run, args=(token,), daemon=True)
        self._thread.start()

    def stop(self):
        if self._client and self._loop and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._client.close(), self._loop)

    def join(self, timeout=None):
        if self._thread:
            self._thread.join(timeout)

    def _run(self, token):
        error = None
        self.mixer.start()
        try:
            asyncio.run(self._main(token))
        except discord.LoginFailure:
            error = "Token invalide"
        except discord.PrivilegedIntentsRequired:
            error = "Intents refusés par Discord"
        except Exception as e:
            error = f"Erreur : {e}"
        finally:
            self.mixer.stop()
            self._client = None
            self._on_event("voice", None)
            self._on_event("stopped", error)

    async def _main(self, token):
        self._loop = asyncio.get_running_loop()
        self._client = client = self._build_client()
        self._on_event("status", "Connexion…")
        async with client:
            await client.start(token)

    def _build_client(self):
        client = discord.Client(intents=discord.Intents.default())
        tree = app_commands.CommandTree(client)
        mixer = self.mixer
        emit = self._on_event

        @tree.command(name="join", description="Rejoint ton salon vocal et diffuse le son des applis choisies")
        async def join(interaction: discord.Interaction):
            if not interaction.user.voice or not interaction.user.voice.channel:
                await interaction.response.send_message("Rejoins d'abord un salon vocal.", ephemeral=True)
                return
            await interaction.response.defer()
            channel = interaction.user.voice.channel
            vc = interaction.guild.voice_client
            if vc:
                await vc.move_to(channel)
            else:
                vc = await channel.connect(self_deaf=True)
            if not vc.is_playing():
                vc.play(MixerSource(mixer))
            apps = ", ".join(sorted(mixer.captured_apps())) or "aucune appli pour l'instant"
            await interaction.followup.send(f"Diffusion dans {channel.mention} : **{apps}**")

        @tree.command(name="leave", description="Arrête la diffusion et quitte le salon vocal")
        async def leave(interaction: discord.Interaction):
            vc = interaction.guild.voice_client
            if not vc:
                await interaction.response.send_message("Je ne suis dans aucun salon.", ephemeral=True)
                return
            await vc.disconnect()
            await interaction.response.send_message("Diffusion arrêtée.")

        @tree.command(name="volume", description="Règle le volume de la diffusion")
        @app_commands.describe(pourcent="De 0 à 200 (100 = volume d'origine)")
        async def volume(interaction: discord.Interaction, pourcent: app_commands.Range[int, 0, 200]):
            mixer.volume = pourcent / 100
            emit("volume", pourcent)
            await interaction.response.send_message(f"Volume réglé à {pourcent} %")

        commands = tree.get_commands()

        async def sync_guild(guild):
            # Commandes par serveur : elles apparaissent tout de suite, contrairement aux globales.
            for cmd in commands:
                tree.add_command(cmd, guild=guild, override=True)
            await tree.sync(guild=guild)

        synced = False

        @client.event
        async def on_ready():
            nonlocal synced
            if synced:  # on_ready se redéclenche après chaque reconnexion
                return
            synced = True
            tree.clear_commands(guild=None)
            await tree.sync()  # retire d'éventuelles commandes globales en double
            for guild in client.guilds:
                await sync_guild(guild)
            emit("ready", client.application_id)
            if client.guilds:
                emit("status", f"Connecté : {client.user} — tape /join dans Discord")
            else:
                emit("status", f"Connecté : {client.user} — invite-le sur un serveur")

        @client.event
        async def on_guild_join(guild):
            await sync_guild(guild)
            emit("status", f"Connecté : {client.user} — tape /join dans Discord")

        @client.event
        async def on_voice_state_update(member, before, after):
            if member.id == client.user.id:
                emit("voice", after.channel.name if after.channel else None)

        return client
