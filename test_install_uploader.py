import sys

import install_uploader


def test_command_for_quotes_paths_with_spaces():
    cmd = install_uploader.command_for(
        r"C:\Program Files\Python314\pythonw.exe", r"C:\Users\aryan\credit agent\uploader.py", "%1")
    assert cmd == r'"C:\Program Files\Python314\pythonw.exe" "C:\Users\aryan\credit agent\uploader.py" "%1"'


def test_pythonw_path_swaps_exe(monkeypatch):
    monkeypatch.setattr(sys, "executable", r"C:\Program Files\Python314\python.exe")
    assert install_uploader.pythonw_path() == r"C:\Program Files\Python314\pythonw.exe"
