"""Liste des applis qui ont une session audio, sur toutes les sorties (pas seulement celle par défaut)."""

import os

import psutil
from pycaw.constants import DEVICE_STATE, EDataFlow
from pycaw.pycaw import IAudioSessionControl2
from pycaw.utils import AudioUtilities

AUDIO_SESSION_STATE_ACTIVE = 1


def list_audio_apps():
    """Renvoie {nom_exe: joue_du_son} pour chaque appli ayant ouvert une session audio."""
    apps = {}
    for device in AudioUtilities.GetAllDevices(EDataFlow.eRender.value, DEVICE_STATE.ACTIVE.value):
        try:
            sessions = device.AudioSessionManager.GetSessionEnumerator()
            count = sessions.GetCount()
        except Exception:
            continue
        for i in range(count):
            try:
                ctl = sessions.GetSession(i).QueryInterface(IAudioSessionControl2)
                pid = ctl.GetProcessId()
                if not pid or pid == os.getpid():
                    continue  # pid 0 = sons système
                name = psutil.Process(pid).name()
                active = ctl.GetState() == AUDIO_SESSION_STATE_ACTIVE
            except Exception:
                continue
            apps[name] = apps.get(name, False) or active
    return apps
