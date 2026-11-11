"""Relocatable installed entry point; no developer environment is required."""
import os
from pathlib import Path
import runpy
import sys
import traceback


def main():
    root = Path(__file__).resolve().parent
    sys.path.insert(0, str(root))
    os.chdir(root)
    os.environ.update(PYTHONUTF8='1', PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1',
                      PLAYWRIGHT_BROWSERS_PATH=str(root / '.local/browsers'))
    temporary = root / '.local/temp'
    temporary.mkdir(parents=True, exist_ok=True)
    os.environ['TEMP'] = os.environ['TMP'] = str(temporary)
    arguments = sys.argv[1:]
    if arguments and arguments[0] == '--cli':
        sys.argv = [str(root/'sau_cli.py'), *arguments[1:]]
        runpy.run_path(str(root/'sau_cli.py'), run_name='__main__')
    elif arguments and arguments[0] == '--self-test':
        from installed_self_test import main as self_test
        raise SystemExit(self_test(arguments[1:]))
    else:
        script = 'video_channels_gui.py' if arguments and arguments[0] == '--single' else 'video_channels_batch.py'
        forwarded = arguments if arguments and arguments[0] in ('--inspect', '--resume') else []
        sys.argv = [str(root/script), *forwarded]
        runpy.run_path(str(root/script), run_name='__main__')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        root = Path(__file__).resolve().parent
        log = root / '.local/gui/startup-error.txt'
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(traceback.format_exc(), encoding='utf-8')
        if sys.stderr is not None:
            traceback.print_exc()
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, f'程序未能启动。错误记录：\n{log}', '视频号发布助手', 0x10)
        raise SystemExit(1)
