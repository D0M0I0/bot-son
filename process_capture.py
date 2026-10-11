"""Capture du son d'un seul processus Windows (WASAPI process loopback, Windows 10 2004+).

Pas de bibliothèque Python qui expose cette API : on appelle les interfaces COM
directement via ctypes, en indexant leurs vtables.
"""

import ctypes
import threading
from ctypes import POINTER, WINFUNCTYPE, byref, c_longlong, c_uint32, c_ulong, c_ushort, c_void_p, wintypes

import psutil

ole32 = ctypes.windll.ole32
kernel32 = ctypes.windll.kernel32
mmdevapi = ctypes.windll.Mmdevapi

HRESULT = ctypes.HRESULT
S_OK = 0
E_NOINTERFACE = 0x80004002 - (1 << 32)

COINIT_MULTITHREADED = 0
VT_BLOB = 65
AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0
AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"


class GUID(ctypes.Structure):
    _fields_ = [("Data1", c_ulong), ("Data2", c_ushort), ("Data3", c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    def __init__(self, text):
        super().__init__()
        ole32.CLSIDFromString(ctypes.c_wchar_p(text), byref(self))

    def __eq__(self, other):
        return bytes(self) == bytes(other)


IID_IUnknown = GUID("{00000000-0000-0000-C000-000000000046}")
IID_IAgileObject = GUID("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}")
IID_IActivateAudioInterfaceCompletionHandler = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
IID_IAudioClient = GUID("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}")
IID_IAudioCaptureClient = GUID("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")


class WAVEFORMATEX(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("wFormatTag", wintypes.WORD),
        ("nChannels", wintypes.WORD),
        ("nSamplesPerSec", wintypes.DWORD),
        ("nAvgBytesPerSec", wintypes.DWORD),
        ("nBlockAlign", wintypes.WORD),
        ("wBitsPerSample", wintypes.WORD),
        ("cbSize", wintypes.WORD),
    ]


class AUDIOCLIENT_ACTIVATION_PARAMS(ctypes.Structure):
    _fields_ = [("ActivationType", ctypes.c_int), ("TargetProcessId", wintypes.DWORD), ("ProcessLoopbackMode", ctypes.c_int)]


class PROPVARIANT_BLOB(ctypes.Structure):
    _fields_ = [
        ("vt", c_ushort),
        ("wReserved1", c_ushort),
        ("wReserved2", c_ushort),
        ("wReserved3", c_ushort),
        ("cbSize", c_ulong),
        ("pBlobData", c_void_p),
    ]


def _com_method(ptr, index, *argtypes):
    """Méthode n° `index` de la vtable de l'objet COM `ptr` ; lève OSError si le HRESULT échoue."""
    vtbl = ctypes.cast(ptr, POINTER(POINTER(c_void_p)))[0]
    fn = WINFUNCTYPE(HRESULT, c_void_p, *argtypes)(vtbl[index])
    return lambda *args: fn(ptr, *args)


def _release(ptr):
    if ptr:
        vtbl = ctypes.cast(ptr, POINTER(POINTER(c_void_p)))[0]
        WINFUNCTYPE(c_ulong, c_void_p)(vtbl[2])(ptr)


class _CompletionHandler:
    """Implémentation minimale de IActivateAudioInterfaceCompletionHandler (+ IAgileObject)."""

    _QI = WINFUNCTYPE(ctypes.c_long, c_void_p, POINTER(GUID), POINTER(c_void_p))
    _REF = WINFUNCTYPE(c_ulong, c_void_p)
    _DONE = WINFUNCTYPE(ctypes.c_long, c_void_p, c_void_p)

    def __init__(self):
        self.done = threading.Event()
        self.operation = None
        # Les callbacks doivent rester référencés tant que l'objet COM vit.
        self._fns = [self._QI(self._query_interface), self._REF(lambda _: 1), self._REF(lambda _: 1), self._DONE(self._completed)]
        self._vtbl = (c_void_p * 4)(*[ctypes.cast(f, c_void_p) for f in self._fns])
        self._obj = ctypes.pointer(c_void_p(ctypes.addressof(self._vtbl)))

    @property
    def ptr(self):
        return ctypes.cast(self._obj, c_void_p)

    def _query_interface(self, this, riid, ppv):
        if riid.contents in (IID_IUnknown, IID_IAgileObject, IID_IActivateAudioInterfaceCompletionHandler):
            ppv[0] = this
            return S_OK
        ppv[0] = None
        return E_NOINTERFACE

    def _completed(self, this, operation):
        self.operation = operation
        self.done.set()
        return S_OK


def find_pid(name):
    """PID du premier processus dont le nom contient `name` (insensible à la casse)."""
    for proc in psutil.process_iter(["pid", "name"]):
        if proc.info["name"] and name.lower() in proc.info["name"].lower():
            return proc.info["pid"], proc.info["name"]
    raise RuntimeError(f"Aucun processus en cours ne contient « {name} »")


class ProcessCapture:
    """Capture le son d'un processus (et de ses enfants) en PCM 48 kHz stéréo 16 bits.

    `on_data(bytes)` est appelé depuis un thread dédié à chaque paquet reçu.
    """

    def __init__(self, pid, on_data, rate=48000):
        self._pid = pid
        self._on_data = on_data
        self._rate = rate
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._error = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait()
        if self._error:
            raise self._error

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2)

    def _activate(self):
        params = AUDIOCLIENT_ACTIVATION_PARAMS(
            AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK, self._pid, PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE
        )
        prop = PROPVARIANT_BLOB(vt=VT_BLOB, cbSize=ctypes.sizeof(params), pBlobData=ctypes.addressof(params))
        handler = _CompletionHandler()
        operation = c_void_p()
        hr = mmdevapi.ActivateAudioInterfaceAsync(
            ctypes.c_wchar_p(VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK),
            byref(IID_IAudioClient),
            byref(prop),
            handler.ptr,
            byref(operation),
        )
        if hr < 0:
            raise OSError(f"ActivateAudioInterfaceAsync a échoué (0x{hr & 0xFFFFFFFF:08X})")
        if not handler.done.wait(5):
            raise TimeoutError("Windows n'a pas répondu à l'activation de la capture")
        try:
            activate_hr = ctypes.c_long()
            client = c_void_p()
            _com_method(operation, 3, POINTER(ctypes.c_long), POINTER(c_void_p))(byref(activate_hr), byref(client))
            if activate_hr.value < 0:
                raise OSError(f"Activation de la capture refusée (0x{activate_hr.value & 0xFFFFFFFF:08X})")
            return client
        finally:
            _release(operation)

    def _run(self):
        ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
        client = capture = None
        event = kernel32.CreateEventW(None, False, False, None)
        try:
            client = self._activate()
            fmt = WAVEFORMATEX(1, 2, self._rate, self._rate * 4, 4, 16, 0)
            _com_method(client, 3, ctypes.c_int, wintypes.DWORD, c_longlong, c_longlong, POINTER(WAVEFORMATEX), c_void_p)(
                AUDCLNT_SHAREMODE_SHARED,
                AUDCLNT_STREAMFLAGS_LOOPBACK
                | AUDCLNT_STREAMFLAGS_EVENTCALLBACK
                | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM
                | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY,
                1_000_000,  # tampon de 100 ms (unités de 100 ns)
                0,
                byref(fmt),
                None,
            )
            _com_method(client, 13, wintypes.HANDLE)(event)
            capture = c_void_p()
            _com_method(client, 14, POINTER(GUID), POINTER(c_void_p))(byref(IID_IAudioCaptureClient), byref(capture))
            _com_method(client, 10)()
        except Exception as e:
            self._error = e
            self._ready.set()
            _release(capture)
            _release(client)
            kernel32.CloseHandle(event)
            ole32.CoUninitialize()
            return
        self._ready.set()

        get_buffer = _com_method(capture, 3, POINTER(c_void_p), POINTER(c_uint32), POINTER(wintypes.DWORD), c_void_p, c_void_p)
        release_buffer = _com_method(capture, 4, c_uint32)
        next_packet_size = _com_method(capture, 5, POINTER(c_uint32))
        data, frames, flags, packet = c_void_p(), c_uint32(), wintypes.DWORD(), c_uint32()
        try:
            while not self._stop.is_set():
                kernel32.WaitForSingleObject(event, 100)
                next_packet_size(byref(packet))
                while packet.value:
                    get_buffer(byref(data), byref(frames), byref(flags), None, None)
                    size = frames.value * 4
                    chunk = bytes(size) if flags.value & AUDCLNT_BUFFERFLAGS_SILENT else ctypes.string_at(data, size)
                    release_buffer(frames.value)
                    self._on_data(chunk)
                    next_packet_size(byref(packet))
        except OSError as e:
            print(f"Capture du processus {self._pid} interrompue : {e}")
        finally:
            _com_method(client, 11)()
            _release(capture)
            _release(client)
            kernel32.CloseHandle(event)
            ole32.CoUninitialize()
