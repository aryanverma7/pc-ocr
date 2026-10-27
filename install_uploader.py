"""
Adds "Send to editor" to Explorer's right-click menu for video files, and runs
the uploader at logon so a queue cut short by a power cut carries on.

Current user only (HKCU), so no admin rights are needed.
    python install_uploader.py              install
    python install_uploader.py --uninstall  remove both again
"""
import os
import sys

VERB_KEY = r"Software\Classes\SystemFileAssociations\video\shell\DualBladeXSendToEditor"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "DualBladeXUploader"
UPLOADER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploader.py")


def command_for(pythonw: str, script: str, arg: str) -> str:
    return f'"{pythonw}" "{script}" "{arg}"'


def pythonw_path() -> str:
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        exe = exe[: -len("python.exe")] + "pythonw.exe"  # no console flashing up per click
    return exe


def install() -> None:
    import winreg

    pythonw = pythonw_path()
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, VERB_KEY) as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "Send to editor")
        winreg.SetValueEx(k, "Icon", 0, winreg.REG_SZ, pythonw)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, VERB_KEY + r"\command") as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, command_for(pythonw, UPLOADER, "%1"))
    print('Added "Send to editor" to the right-click menu for video files.')
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
        winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, f'"{pythonw}" "{UPLOADER}" --resume')
    print("Unfinished uploads will resume at every login.")


def uninstall() -> None:
    import winreg

    for key in (VERB_KEY + r"\command", VERB_KEY):  # a key with subkeys cannot be deleted
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        except FileNotFoundError:
            pass
    print('Removed "Send to editor" from the right-click menu.')
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, RUN_VALUE)
    except FileNotFoundError:
        pass
    print("Removed the resume-at-login entry.")


if __name__ == "__main__":
    uninstall() if "--uninstall" in sys.argv[1:] else install()
