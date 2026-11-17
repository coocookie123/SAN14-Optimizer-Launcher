"""Create the desktop shortcut "三国志14 跳过报告" that starts the game through Steam with the helper.

Target: pythonw (the stable App Execution Alias in %LOCALAPPDATA%\\Microsoft\\WindowsApps, so a Python
update does not break it), arguments: -P <report_button.py> --steam, icon: the game's own icon.
Usage: python -P tools/make_shortcut.py [--remove]
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GAME_ICON = r'E:\Steam\steamapps\common\Romance_of_the_Three_Kingdoms_14\SAN14CE_SC.exe'
NAME = '三国志14 跳过报告.lnk'


def desktop():
    out = subprocess.run(['powershell', '-NoProfile', '-Command', "[Environment]::GetFolderPath('Desktop')"],
                         capture_output=True, text=True, encoding='utf-8')
    return out.stdout.strip()


def main():
    lnk = os.path.join(desktop(), NAME)
    if sys.argv[1:] == ['--remove']:
        if os.path.exists(lnk):
            os.remove(lnk)
        print('removed', lnk)
        return
    pythonw = os.path.join(os.environ['LOCALAPPDATA'], 'Microsoft', 'WindowsApps', 'pythonw.exe')
    if not os.path.exists(pythonw):
        pythonw = os.path.join(os.path.dirname(sys.executable), 'pythonw.exe')
    script = os.path.join(HERE, 'report_button.py')
    ps = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:LNK);"
        "$s.TargetPath = $env:PYW; $s.Arguments = $env:ARGS; $s.WorkingDirectory = $env:ROOT;"
        "$s.IconLocation = $env:ICON + ',0'; $s.Description = $env:DESC; $s.Save();"
        "$t = (New-Object -ComObject WScript.Shell).CreateShortcut($env:LNK); $t.TargetPath; $t.Arguments"
    )
    env = dict(os.environ, LNK=lnk, PYW=pythonw, ARGS='-P "%s" --steam' % script, ROOT=ROOT, ICON=GAME_ICON,
               DESC='通过 Steam 启动三国志14，并跳过回合开头的报告、显示「回看报告」按钮')
    out = subprocess.run(['powershell', '-NoProfile', '-Command', ps], env=env, capture_output=True, text=True,
                         encoding='utf-8')
    print(out.stdout.strip(), out.stderr.strip())
    print('shortcut:', lnk)


if __name__ == '__main__':
    main()
